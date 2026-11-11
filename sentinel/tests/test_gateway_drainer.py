"""The drop-log drainer: ring records and refusals as 8.3 step_events rows and gateway_drops.jsonl (5.4.8, 6.2)."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone

import pytest

from sentinel.gateway import segment as sg
from sentinel.gateway.drainer import JOURNAL_NAME, DrainContext, Drainer, TokenNote, ingest_journal, wall_from
from sentinel.gateway.segment import (HookEvent, SegmentWriter, append_record, decide_from_segment, record_for,
                                      reinstall_record, to_ns)
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.gateway import (GATEWAY_REASONS, Action, Flag, GatewayState, InputEvent, SafetyContext,
                                           hook_decide, slow_half)
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

T = 100.0
RUN, SHARD = "run-01", "run-01/1"
CROP = "sha256:" + "c" * 64
CAPTURE = "sha256:" + "a" * 64


def drainer(tmp_path, **ctx) -> Drainer:
    counter = itertools.count(1)
    base = dict(run_id=RUN, shard_id=SHARD, side="candidate", mode="scripted", flow_id="f.ladder", step_id="s4",
                pre_hash=CAPTURE)
    return Drainer(DrainContext(**{**base, **ctx}), journal=tmp_path / JOURNAL_NAME, next_seq=lambda: next(counter),
                   wall_at=lambda ns: datetime.fromtimestamp(1_700_000_000 + ns / 1e9, timezone.utc).isoformat(),
                   crop_at=lambda x, y: f"sha256:crop-{x}-{y}")


def segment(g: GatewayState) -> bytearray:
    buf = bytearray(sg.SEGMENT_SIZE)
    sg.init_segment(buf, orchestrator_pid=1)
    SegmentWriter(buf).publish(g)
    return buf


def journal_lines(tmp_path) -> list[dict]:
    return [json.loads(x) for x in (tmp_path / JOURNAL_NAME).read_text().splitlines()]


def test_ring_records_become_rows_with_every_5_4_8_field(tmp_path):
    g = GatewayState(armed=True)
    g.stamp_watcher(Flag.CLEAR, T)
    click = g.issue_click(400, 300, T)
    key = g.issue_key(0x41, T, secret=True)
    buf = segment(g)
    d = drainer(tmp_path)
    d.note_token(TokenNote(click.id, "scripted", "f.ladder", "s3", attempt=2, target_text="Buy Custom",
                           click_crop_sha256=CROP, rect_w=60, rect_h=22))
    d.note_token(TokenNote(key.id, "scripted", "f.login", "s1", target_text="<redacted:login.password>"))
    for e, t in ((HookEvent("button_down", 401, 299, button="left"), T),
                 (HookEvent("button_up", 401, 299, button="left"), T + 0.8),
                 (HookEvent("key_down", vk=0x41), T), (HookEvent("key_up", vk=0x41), T),
                 (HookEvent("button_down", 10, 20, button="right", origin="foreign_process"), T),
                 (HookEvent("key_down", vk=0x0D, mods=frozenset({"ctrl"}), origin="console"), T)):
        decide_from_segment(buf, e, to_ns(t))
    rows = d.drain_ring(buf)
    assert [(r.seq, r.gate_decision, r.gate_reason, r.gate_class) for r in rows] == [
        (1, "emitted", None, "allowed"), (2, "late_up", None, "allowed"), (3, "emitted", None, "allowed"),
        (4, "emitted", None, "allowed"), (5, "dropped", "untokened_injected", "safety"),
        (6, "dropped", "foreign_input", "safety")]
    down, up, kd, _ku, foreign, console = rows
    assert (down.run_id, down.shard_id, down.side, down.source, down.action) == (
        RUN, SHARD, "candidate", "gateway_hook", "gateway_decision")
    assert (down.flow_id, down.step_id, down.mode, down.attempt) == ("f.ladder", "s3", "scripted", 2)
    assert (down.screen_x, down.screen_y, down.keys, down.injected, down.token_id) == (401, 299, "button_down:left",
                                                                                      True, click.id)
    assert (down.target_text, down.click_crop_sha256, down.pre_hash, down.rect_w, down.rect_h) == (
        "Buy Custom", CROP, CAPTURE, 60, 22)
    assert down.mono_ms == 100_000 and down.wall_at.startswith("2023-11-14T22:15:")
    assert up.duration_ms == 300 and up.token_id == click.id                        # the late_up delay
    assert (kd.keys, kd.screen_x, kd.target_text, kd.flow_id) == ("key_down:<redacted>", None,
                                                                  "<redacted:login.password>", "f.login")
    assert "0x41" not in json.dumps([r.to_dict() for r in rows])                     # the secret vk never stored
    assert (foreign.flow_id, foreign.step_id, foreign.mode, foreign.token_id, foreign.injected) == (
        "f.ladder", "s4", "scripted", None, True)
    assert foreign.click_crop_sha256 == "sha256:crop-10-20" and foreign.keys == "button_down:right"
    assert (console.injected, console.keys, console.screen_x) == (False, "key_down:ctrl+0x0D", None)
    assert all(r.gate_reason is None or r.gate_reason in GATEWAY_REASONS for r in rows)


def test_decisions_without_a_segment_drain_through_record_for(tmp_path):
    g = GatewayState(armed=True)
    g.stamp_watcher(Flag.CLEAR, T)
    move = InputEvent("move", 640, 480)
    recs = [record_for(move, hook_decide(g, move, T), to_ns(T))]                   # the absolute move: no record
    t = g.issue_click(640, 480, T)                                                  # as SimulatedRig emits a click
    d = drainer(tmp_path)
    d.note_token(TokenNote(t.id, "scripted", "f.ladder", "s3", target_text="Buy"))
    for e in (InputEvent("button_down", 640, 480, button="left"), InputEvent("button_up", 640, 480, button="left")):
        recs.append(record_for(e, hook_decide(g, e, T), to_ns(T)))
    assert recs[0] is None
    rows = d.drain([r for r in recs if r is not None])
    assert [(r.gate_decision, r.screen_x, r.screen_y, r.keys, r.token_id, r.target_text) for r in rows] == [
        ("emitted", 640, 480, "button_down:left", t.id, "Buy"), ("emitted", 640, 480, "button_up:left", t.id, "Buy")]


def test_journal_lines_equal_the_dropped_rows(tmp_path):
    g = GatewayState(armed=True)
    g.stamp_watcher(Flag.SET, T)
    g.issue_key(0x0D, T)
    g.issue_click(50, 50, T)                                                        # pending until its down
    buf = segment(g)
    d = drainer(tmp_path)
    decide_from_segment(buf, HookEvent("key_down", vk=0x0D), to_ns(T))              # blocked under the flag
    decide_from_segment(buf, HookEvent("move", 5, 5), to_ns(T))                     # click token pending: dropped
    g2_rows = d.drain_ring(buf)
    dropped = [r for r in g2_rows if r.gate_decision == "dropped"]
    assert [r.gate_reason for r in dropped] == ["blocked_key_dialog", "move_outside_allow"]
    assert journal_lines(tmp_path) == [r.to_dict() for r in dropped]
    assert ingest_journal(tmp_path / JOURNAL_NAME) == dropped
    assert d.drain_ring(buf) == [] and len(journal_lines(tmp_path)) == 2            # nothing drained twice


def test_drill_phase_drops_are_tagged_and_not_untokened_injected(tmp_path):
    g = GatewayState(armed=True)
    g.stamp_watcher(Flag.SET, T)
    buf = segment(g)
    raw = HookEvent("key_down", vk=0x0D, origin="drill")
    d = drainer(tmp_path, mode="scripted", flow_id="drill.fire", drill_phase=True)
    decide_from_segment(buf, raw, to_ns(T))
    decide_from_segment(buf, HookEvent("button_down", 7, 7, button="left", origin="drill"), to_ns(T))
    inside = d.drain_ring(buf)
    assert [(r.gate_reason, r.drill) for r in inside] == [("blocked_key_dialog", True), ("no_token", True)]
    d.set_context(drill_phase=False)                                                # outside the declared phase
    decide_from_segment(buf, HookEvent("button_down", 7, 7, button="left", origin="drill"), to_ns(T))
    outside = d.drain_ring(buf)
    assert [(r.gate_reason, r.gate_class, r.drill) for r in outside] == [("untokened_injected", "safety", False)]


def test_probe_and_hook_reinstalled_records(tmp_path):
    g = GatewayState(armed=True)
    g.stamp_watcher(Flag.CLEAR, T)
    buf = segment(g)
    pid = SegmentWriter(buf).issue_probe(5, 5, to_ns(T))
    decide_from_segment(buf, HookEvent("move", 5, 5), to_ns(T))
    append_record(buf, reinstall_record(to_ns(T), True))
    append_record(buf, reinstall_record(to_ns(T), False))
    probe, ok, failed = drainer(tmp_path).drain_ring(buf)
    assert (probe.mode, probe.reason, probe.token_id, probe.gate_decision, probe.keys) == (
        "system", "liveness_probe", pid, "emitted", "move")
    assert (ok.action, ok.gate_decision, ok.gate_reason, ok.gate_class, ok.mode, ok.injected) == (
        "hook_reinstalled", "none", "hook_reinstalled", "integrity", "system", False)
    assert (ok.reason, failed.reason) == ("reinstall ok", "reinstall failed")
    assert not (tmp_path / JOURNAL_NAME).exists()                                    # none of them is a drop


def test_refusals_are_rows_and_journaled(tmp_path):
    d = drainer(tmp_path, mode="agent")
    s = slow_half(Action("click", profile="agent", point=(920, 610), resolved_name="Send"), SafetyContext())
    row = d.refused(s, mono_ns=to_ns(T), keys="button_down:left", point=(920, 610), target_text="Send",
                    click_crop_sha256=CROP, rect=(80, 28))
    assert (row.source, row.action, row.gate_decision, row.gate_reason, row.gate_class) == (
        "orchestrator", "gateway_decision", "refused", "never_tokenize", "safety")
    assert (row.mode, row.screen_x, row.screen_y, row.injected, row.token_id, row.pre_hash) == (
        "agent", 920, 610, False, None, CAPTURE)
    assert (row.click_crop_sha256, row.rect_w, row.rect_h, row.target_text) == (CROP, 80, 28, "Send")
    assert row.reason == s.detail and journal_lines(tmp_path) == [row.to_dict()]
    with pytest.raises(ValueError):
        d.refused(slow_half(Action("click", point=(1, 1), resolved_name="OK"), SafetyContext()), mono_ns=1)


def test_rows_are_stored_with_write_events(tmp_path):
    store = Store(tmp_path / "run.db")
    store.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
    ws = "ws.tier-a.active-trader"
    store.begin_run(RunPlan(run_id=RUN, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=2700, scope=(ScopeEntry(f"census/trade.active-trader.ladder/{ws}", ws,
                                                               "active_trader", 1, "frozen"),),
                            shards=(ShardPlan(SHARD, "rig-a1"),), base_commit_distance=2,
                            authorization_sha256="sha256:auth", pr_committers=("ann", "bob")))
    try:
        g = GatewayState(armed=True)
        g.stamp_watcher(Flag.CLEAR, T)
        t = g.issue_click(50, 50, T)
        buf = segment(g)
        d = drainer(tmp_path)
        d.note_token(TokenNote(t.id, "scripted", "f.ladder", "s3", target_text="Buy", click_crop_sha256=CROP))
        decide_from_segment(buf, HookEvent("button_down", 50, 50, button="left"), to_ns(T))
        decide_from_segment(buf, HookEvent("button_up", 50, 50, button="left"), to_ns(T))
        decide_from_segment(buf, HookEvent("button_down", 9, 9, button="left", origin="foreign_process"), to_ns(T))
        rows = d.drain_ring(buf)
        store.write_events(rows)
        store.write_events(ingest_journal(tmp_path / JOURNAL_NAME))                 # a resume replays: a no-op
        assert store.step_events(RUN) == rows
        assert store.gateway_drops(RUN) == 1
    finally:
        store.close()


def test_ingest_skips_a_torn_last_line_only(tmp_path):
    d = drainer(tmp_path)
    g = GatewayState(armed=True)
    g.stamp_watcher(Flag.CLEAR, T)
    buf = segment(g)
    for _ in range(2):
        decide_from_segment(buf, HookEvent("key_down", vk=0x41), to_ns(T))
    rows = d.drain_ring(buf)
    path = tmp_path / JOURNAL_NAME
    path.write_text(path.read_text() + '{"run_id": "run-01", "se')                 # died mid-write
    assert ingest_journal(path) == rows
    path.write_text('{"broken\n' + path.read_text())
    with pytest.raises(json.JSONDecodeError):
        ingest_journal(path)
    assert ingest_journal(tmp_path / "missing.jsonl") == []


def test_wall_from_converts_the_events_own_monotonic_time():
    clock = ManualClock(datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc), mono=1000.0)
    at = wall_from(clock)
    assert at(to_ns(999.5)) == "2026-11-09T18:59:59.500000+00:00"
    clock.advance(10)
    assert at(to_ns(999.5)) == "2026-11-09T18:59:59.500000+00:00"                  # the drainer's lag is not in it


def test_token_notes_are_bounded(tmp_path):
    d = drainer(tmp_path)
    for i in range(1, 5000):
        d.note_token(TokenNote(i, "scripted"))
    assert len(d.notes) == 4096 and 1 not in d.notes and 4999 in d.notes


def test_a_secret_steps_keys_stay_redacted_when_the_kill_switch_drops_them(tmp_path):
    """6.9 (choice 3): the kill switch fires while a secret step types; no row and no journal line carries a vk of
    the secret, the up of the key that passed before the trip included."""
    g = GatewayState(armed=True)
    g.stamp_watcher(Flag.CLEAR, T)
    toks = [g.issue_key(vk, T, secret=True) for vk in (0x50, 0x57, 0x39)]
    buf = segment(g)
    d = drainer(tmp_path, flow_id="f.login", step_id="s2")
    for t in toks:
        d.note_token(TokenNote(t.id, "scripted", "f.login", "s2", target_text="<redacted:login.password>"))
    decide_from_segment(buf, HookEvent("key_down", vk=0x50), to_ns(T))
    g.deny_all = True
    SegmentWriter(buf).publish(g)
    for e in (HookEvent("key_up", vk=0x50), HookEvent("key_down", vk=0x57), HookEvent("key_up", vk=0x57),
              HookEvent("key_down", vk=0x39), HookEvent("key_up", vk=0x39)):
        decide_from_segment(buf, e, to_ns(T))
    rows = d.drain_ring(buf)
    assert [r.keys for r in rows] == ["key_down:<redacted>", "key_up:<redacted>"] * 3
    assert [r.gate_reason for r in rows] == [None] + ["deny_all"] * 5
    text = json.dumps([r.to_dict() for r in rows]) + (tmp_path / JOURNAL_NAME).read_text()
    assert not any(code in text for code in ("0x50", "0x57", "0x39"))


def test_the_up_of_a_secret_down_is_redacted_even_when_its_own_record_is_not_flagged(tmp_path):
    """Choice 3: a key up the hook could not flag (its secret token's slot was freed between the down and the up)
    follows a secret down of the same key, so it is redacted too, once."""
    down = sg.RingRecord(0, "key_down", "emitter", "deny_all", frozenset({"shift"}), True, True, to_ns(T), 0, 0,
                         0x50, 0, 7)
    up = sg.RingRecord(1, "key_up", "emitter", "deny_all", frozenset({"shift"}), True, False, to_ns(T), 0, 0, 0x50, 0,
                       None)
    rows = drainer(tmp_path).drain([down, up, up._replace(seq=2)])
    assert [r.keys for r in rows] == ["key_down:<redacted>", "key_up:<redacted>", "key_up:shift+0x50"]
