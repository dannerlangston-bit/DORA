"""The shared segment of 5.4.2 and the hook decision over it (5.4.4): layout, seqlock, ring, probe, and parity
with `orchestrator.gateway.hook_decide`."""
from __future__ import annotations

import importlib.util
import inspect
import random
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from sentinel.gateway import segment as sg
from sentinel.gateway.segment import (INT64_MIN, LAYOUT_VERSION, MAGIC, RING_SLOTS, SEGMENT_SIZE, Decision, HookEvent,
                                      LayoutMismatch, RingReader, Segment, SegmentError, SegmentWriter,
                                      append_record, apply_token_states, answer_reinstall, decide_from_segment,
                                      init_segment, layout_ok, read_header, read_snapshot, read_tokens, record_for,
                                      stamp_alive, to_ns, to_seconds, token_states)
from sentinel.orchestrator import gateway as gw
from sentinel.orchestrator import simrig
from sentinel.orchestrator.gateway import (Chord, DenyRect, Flag, GatewayState, InputEvent, Rect, hook_decide)

T = 100.0
TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"


def fresh() -> bytearray:
    buf = bytearray(SEGMENT_SIZE)
    init_segment(buf, orchestrator_pid=4242, hook_host_pid=777)
    return buf


def armed(flag: Flag = Flag.CLEAR) -> GatewayState:
    g = GatewayState(armed=True)
    g.stamp_watcher(flag, T)
    return g


def published(g: GatewayState) -> tuple[bytearray, SegmentWriter]:
    buf = fresh()
    w = SegmentWriter(buf)
    w.publish(g)
    return buf, w


def ev(kind: str, x: int = 0, y: int = 0, **kw) -> HookEvent:
    if kind.startswith("button"):
        kw.setdefault("button", "left")
    return HookEvent(kind, x, y, **kw)


# -- layout --------------------------------------------------------------------------------------------------
def test_tables_match_the_orchestrator_gateway():
    assert sg.MODIFIER_VK == gw.MODIFIER_VK
    assert sg.CHORD_MODS == gw.CHORD_MODS
    assert (sg.TOKEN_SLOTS, sg.DENY_RECT_SLOTS, sg.CHORD_SLOTS) == (gw.TOKEN_SLOTS, gw.DENY_RECT_SLOTS, gw.CHORD_SLOTS)
    assert sg.WATCHER_STALE_NS == to_ns(gw.WATCHER_STALE_S)
    hook_codes = set(sg.REASONS) - {"emitted", "late_up"}
    assert hook_codes <= gw.GATEWAY_REASONS
    assert hook_codes <= gw.HOOK_SAFETY | gw.HOOK_INTEGRITY | {"no_token", "hook_reinstalled"}


def test_layout_constants_and_sizes():
    assert (sg.OFF_MOVE_ALLOW, sg.OFF_DENY_RECTS, sg.OFF_BLOCKED_VK, sg.OFF_CHORDS, sg.SLOW_END) == (
        768, 800, 960, 992, 1056)
    assert sg.TOKEN_SIZE == 40 and sg.RING_RECORD_SIZE == sg._RECORD.size == 32
    assert SEGMENT_SIZE == sg.OFF_RING + RING_SLOTS * 32
    assert sg.OFF_RING >= sg.SLOW_END and sg.OFF_ARMED + sg._HEADER_SLOW.size <= sg.OFF_LAST_PROBE


def test_init_writes_a_fail_closed_header():
    h = read_header(fresh())
    assert (h.magic, h.layout_version, h.seq) == (MAGIC, LAYOUT_VERSION, 0)
    assert (h.armed, h.deny_all, h.flag_ticket) == (False, False, sg.FLAG_UNKNOWN)
    assert (h.orchestrator_pid, h.hook_host_pid) == (4242, 777)
    assert h.watcher_mono_ts == h.last_probe_mono_ts == h.hook_alive_mono_ts == INT64_MIN
    assert decide_from_segment(fresh(), ev("move"), 0).reason == "not_armed"
    with pytest.raises(SegmentError):
        init_segment(bytearray(100), orchestrator_pid=1)


def test_magic_or_layout_version_mismatch_is_detected():
    buf = fresh()
    assert layout_ok(buf)
    struct.pack_into("<I", buf, sg.OFF_VERSION, LAYOUT_VERSION + 1)
    assert not layout_ok(buf)
    with pytest.raises(LayoutMismatch):
        SegmentWriter(buf)
    buf = fresh()
    struct.pack_into("<I", buf, sg.OFF_MAGIC, 0xDEADBEEF)
    assert not layout_ok(buf)
    assert not layout_ok(bytearray(64))


def test_every_field_round_trips_through_the_segment():
    g = armed(Flag.SET)
    g.watcher_mono_ts = 123.456789
    g.deny_all = True
    click = g.issue_click(100, 200, T, button="right", dialog_ok=True, send_ok=True)
    key = g.issue_key(0x41, T, secret=True)
    wheel = g.issue_wheel(300, 310, -1, T)
    rel = g.issue_release(T)
    g.open_move_allow(Rect(10, 20, 30, 40), T, 500)
    rects = [(Rect(900, 600, 80, 28), "Send")] + [(Rect(i * 10, 5, 4, 4), f"Flatten {i}") for i in range(7)]
    g.write_deny_rects(rects)
    g.load_chords([Chord.parse("Ctrl+Shift+B"), Chord.parse("Alt+F4"), Chord.parse("B")])
    buf, w = published(g)
    w.set_pids(orchestrator_pid=11, hook_host_pid=22)
    h = read_header(buf)
    assert (h.armed, h.deny_all, h.flag_ticket, h.orchestrator_pid, h.hook_host_pid) == (True, True, 1, 11, 22)
    assert to_seconds(h.watcher_mono_ts) == pytest.approx(123.456789, abs=1e-9)
    assert h.seq % 2 == 0 and h.seq >= 4
    toks = {t.id: t for t in read_tokens(buf)}
    assert len(toks) == 3 + len(rel)
    c = toks[click.id]
    assert (c.kind, c.button_or_vk, c.x, c.y, c.tol_px, c.state) == (sg.KIND_BUTTON, 2, 100, 200, 2, sg.ARMED)
    assert c.flags == sg.TF_DIALOG_OK | sg.TF_SEND_OK and c.expires_ns == to_ns(click.expires)
    k = toks[key.id]
    assert (k.kind, k.button_or_vk, k.flags) == (sg.KIND_KEY, 0x41, sg.TF_SECRET)
    wh = toks[wheel.id]
    assert (wh.kind, wh.sign, wh.x, wh.y) == (sg.KIND_WHEEL, -1, 300, 310)
    kinds = sorted((toks[t.id].kind, toks[t.id].button_or_vk) for t in rel)
    assert all(toks[t.id].release for t in rel)
    assert kinds == [(1, 1), (1, 2), (1, 3), (2, 0x10), (2, 0x11), (2, 0x12), (2, 0x5B)]
    active, until, *bbox = sg._MOVE_ALLOW.unpack_from(buf, sg.OFF_MOVE_ALLOW)
    assert (active, until, bbox) == (1, to_ns(T + 1.5), [10, 20, 30, 40])
    got = [sg._DENY_RECT.unpack_from(buf, sg.OFF_DENY_RECTS + i * 20) for i in range(8)]
    assert got[0] == (888, 588, 104, 52, 1)                                        # inflated 12 px, the Send rect
    assert all(r[4] == 0 for r in got[1:]) and buf[sg.OFF_DENY_COUNT] == 8
    blocked = {vk for vk in range(256) if buf[sg.OFF_BLOCKED_VK + (vk >> 3)] & (1 << (vk & 7))}
    assert blocked == set(gw.BLOCKED_VK)
    chords = {sg._CHORD.unpack_from(buf, sg.OFF_CHORDS + i * 4) for i in range(buf[sg.OFF_CHORD_COUNT])}
    assert chords == {(3, ord("B")), (4, gw.VK_F1 + 3), (0, ord("B"))}
    # the hook's fields
    stamp_alive(buf, 5_000)
    answer_reinstall(buf, 3, True)
    h = read_header(buf)
    assert (h.hook_alive_mono_ts, h.reinstall_done, h.reinstall_ok) == (5_000, 3, True)
    assert w.request_reinstall() == 1 and read_header(buf).reinstall_request == 1


def test_publish_never_writes_the_hooks_token_states_and_frees_slots_of_dropped_tokens():
    g = armed()
    t = g.issue_click(10, 10, T)
    buf, w = published(g)
    assert decide_from_segment(buf, ev("button_down", 10, 10), to_ns(T)).allowed
    w.publish(g)                                                                   # republish: state stays the hook's
    assert token_states(buf)[t.id] == ("half_consumed", None)
    apply_token_states(buf, g)
    assert g.tokens[t.id].state == "half_consumed"
    del g.tokens[t.id]
    t2 = g.issue_key(0x41, T)
    w.publish(g)
    assert set(token_states(buf)) == {t2.id}
    assert token_states(buf)[t2.id] == ("armed", None)                              # a new id in a reused slot


def test_publish_limits_fail_closed():
    g = armed()
    g.deny_rects = [DenyRect(Rect(i, 0, 1, 1), "Send") for i in range(9)]
    with pytest.raises(SegmentError):
        published(g)
    g = armed()
    g.chords = [Chord(frozenset({"shift"}), 0x41 + i) for i in range(17)]
    with pytest.raises(SegmentError):
        published(g)
    g = armed()
    g.blocked_vk = frozenset({300})
    with pytest.raises(SegmentError):
        published(g)
    g = armed()
    g.issue_click(1, 1, T, button="side")
    with pytest.raises(SegmentError):
        published(g)
    g = armed()
    for _ in range(16):
        g.issue_key(0x41, T)
    buf, w = published(g)
    with pytest.raises(SegmentError):
        w.issue_probe(5, 5, to_ns(T))                                              # no 17th slot for the probe


# -- seqlock -------------------------------------------------------------------------------------------------
def test_reader_retries_on_odd_seq_and_fails_closed_when_it_never_settles():
    g = armed()
    g.issue_key(0x41, T)
    buf, w = published(g)
    seq = read_header(buf).seq
    struct.pack_into("<Q", buf, sg.OFF_SEQ, seq + 1)                               # a write in progress
    assert read_snapshot(buf, max_spins=8) is None
    d = decide_from_segment(buf, ev("key_down", vk=0x41), to_ns(T))
    assert (d.allowed, d.reason) == (False, "deny_all")                            # choice 4
    spins = []

    def finish():
        spins.append(1)
        if len(spins) == 3:
            struct.pack_into("<Q", buf, sg.OFF_SEQ, seq + 2)

    assert read_snapshot(buf, spin=finish) is not None and len(spins) == 3


class TornBuffer:
    """A sliceable buffer whose body copy runs one writer step, as a concurrent writer would mid-read."""

    def __init__(self, buf: bytearray, writer_steps: list):
        self.buf, self.steps, self.copies = buf, writer_steps, 0

    def __len__(self):
        return len(self.buf)

    def __getitem__(self, s):
        if isinstance(s, slice) and s.start == 0:
            self.copies += 1
            if self.steps:
                self.steps.pop(0)()
        return self.buf[s]


def test_reader_retries_when_seq_changes_during_the_copy():
    g = armed()
    t = g.issue_key(0x41, T)
    buf, w = published(g)

    def write_a_new_token():
        g.issue_key(0x42, T)
        w.publish(g)

    torn = TornBuffer(buf, [write_a_new_token])
    snap = read_snapshot(torn)
    assert torn.copies == 2                                                        # the first copy was discarded
    assert {s.id for s in sg._tokens(snap)} == {t.id, t.id + 1}


def test_a_writer_that_raises_leaves_seq_odd_so_the_hook_fails_closed():
    g = armed()
    g.issue_key(0x41, T)
    buf, w = published(g)
    with pytest.raises(RuntimeError):
        with w.transaction() as b:
            b[sg.OFF_ARMED] = 0
            raise RuntimeError("writer died mid-write")
    assert read_header(buf).seq % 2 == 1
    assert decide_from_segment(buf, ev("key_down", vk=0x41), to_ns(T)).reason == "deny_all"
    w.publish(g)                                                                   # the next good write recovers
    assert decide_from_segment(buf, ev("key_down", vk=0x41), to_ns(T)).allowed


WRITER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from sentinel.gateway import segment as sg
seg = sg.Segment.attach(sys.argv[2])
w = sg.SegmentWriter(seg.buf)
print("started", flush=True)
for k in range(1, int(sys.argv[3]) + 1):
    with w.transaction() as b:
        for i in range(sg.TOKEN_SLOTS):                      # every slot carries k
            sg._TOKEN_SLOW.pack_into(b, sg.OFF_TOKENS + i * sg.TOKEN_SIZE, k, 1, 0, 1, k, k, 2, k)
    gap = time.perf_counter_ns() + 20_000                    # writes come in bursts, not back to back
    while time.perf_counter_ns() < gap:                      # (a seqlock starves its readers then)
        pass
seg.close()
"""


def _slot_values(snap: bytes) -> set[int]:
    out = set()
    for i in range(sg.TOKEN_SLOTS):
        off = sg.OFF_TOKENS + i * sg.TOKEN_SIZE
        out |= {struct.unpack_from("<I", snap, off)[0], *struct.unpack_from("<ii", snap, off + 8)}
    return out


def test_seqlock_across_two_processes_never_yields_a_torn_picture():
    seg = Segment.create(orchestrator_pid=1)
    try:
        p = subprocess.Popen([sys.executable, "-I", "-c", WRITER, str(SRC), seg.name, "5000"],
                             stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "started"
        seen = set()
        while p.poll() is None:
            snap = read_snapshot(seg.buf, max_spins=1_000)
            if snap is None:                       # starved while the writer was descheduled mid-write: retry
                continue
            vals = _slot_values(snap)
            assert len(vals) == 1, f"torn read: {vals}"
            seen |= vals
        assert p.wait(30) == 0 and len(seen) > 1
        p.stdout.close()
    finally:
        seg.close()
        seg.unlink()


def test_shared_memory_create_and_attach_see_the_same_bytes():
    seg = Segment.create(orchestrator_pid=9)
    try:
        other = Segment.attach(seg.name)
        assert layout_ok(other.buf) and read_header(other.buf).orchestrator_pid == 9
        stamp_alive(other.buf, 77)
        assert read_header(seg.buf).hook_alive_mono_ts == 77
        other.close()
    finally:
        seg.close()
        seg.unlink()


# -- the drop ring -------------------------------------------------------------------------------------------
def _rec(buf, i):
    rec = record_for(HookEvent("key_down", vk=0x41, mods=frozenset({"ctrl"}), origin="foreign_process"),
                     Decision(False, "no_token"), i)
    return append_record(buf, rec)


def test_ring_wraps_at_4096_and_keeps_order_by_seq():
    buf = fresh()
    r = RingReader()
    for i in range(10):
        _rec(buf, i)
    first = r.read(buf)
    assert [x.seq for x in first] == list(range(10)) and r.lost == 0
    x = first[0]
    assert (x.event_type, x.origin, x.reason, x.mods, x.injected, x.vk, x.decision) == (
        "key_down", "foreign_process", "no_token", frozenset({"ctrl"}), True, 0x41, "dropped")
    for i in range(10, 10 + RING_SLOTS + 50):                                      # lap the reader by 50
        _rec(buf, i)
    rest = r.read(buf)
    assert len(rest) == RING_SLOTS and r.lost == 50
    assert [x.mono_ns for x in rest] == list(range(60, 10 + RING_SLOTS + 50))
    assert r.read(buf) == [] and read_header(buf).ring_head == 10 + RING_SLOTS + 50


def test_ring_reader_skips_a_record_being_overwritten():
    buf = fresh()
    for i in range(3):
        _rec(buf, i)
    struct.pack_into("<I", buf, sg.OFF_RING + 32, 0xFFFF_FFFF)                     # record 1 mid-overwrite
    r = RingReader()
    assert [x.seq for x in r.read(buf)] == [0, 2] and r.lost == 1


def test_drops_and_consumed_tokens_write_records_and_allowed_moves_do_not():
    g = armed()
    g.issue_click(50, 50, T)
    k = g.issue_key(0x41, T, secret=True)
    g.issue_wheel(10, 10, 1, T)
    buf, _ = published(g)
    assert decide_from_segment(buf, ev("move", 3, 3, origin="console"), to_ns(T)).reason == "foreign_input"
    assert decide_from_segment(buf, ev("button_down", 50, 50), to_ns(T)).allowed
    late = decide_from_segment(buf, ev("button_up", 50, 50), to_ns(T + 1.25))
    assert late.late_up and late.decision == "late_up"
    assert decide_from_segment(buf, ev("key_down", vk=0x41), to_ns(T)).allowed
    assert decide_from_segment(buf, ev("wheel", 10, 10, delta=240), to_ns(T)).allowed
    assert decide_from_segment(buf, ev("move", 5, 5), to_ns(T)).reason == "move_outside_allow"   # key half-consumed
    assert decide_from_segment(buf, ev("key_up", vk=0x41), to_ns(T)).allowed
    assert decide_from_segment(buf, ev("move", 5, 5), to_ns(T)).allowed            # no token pending: no record
    recs = RingReader().read(buf)
    assert [(x.event_type, x.decision, x.reason) for x in recs] == [
        ("move", "dropped", "foreign_input"), ("button_down", "emitted", "emitted"),
        ("button_up", "late_up", "late_up"), ("key_down", "emitted", "emitted"), ("wheel", "emitted", "emitted"),
        ("move", "dropped", "move_outside_allow"), ("key_up", "emitted", "emitted")]
    assert recs[0].injected is False and recs[1].injected is True
    assert recs[2].aux == 1250 - 500 and recs[2].button == "left"                  # the late_up delay in ms
    assert recs[3].secret and recs[3].token_id == k.id and recs[4].aux == 240


def test_another_processs_injected_move_is_dropped_and_written_to_the_ring():
    """6.2 rule 4: the move a foreign process (or the drill injector) sends is dropped `no_token` and leaves a ring
    record, so the drainer logs it `untokened_injected` (or tags it `drill`); the emitter's untokened move writes none."""
    buf, _ = published(armed())
    for origin in ("foreign_process", "drill"):
        assert decide_from_segment(buf, ev("move", 300, 300, origin=origin), to_ns(T)) == Decision(False, "no_token")
    assert decide_from_segment(buf, ev("move", 300, 300), to_ns(T)).allowed
    assert [(r.event_type, r.origin, r.reason, r.injected) for r in RingReader().read(buf)] == [
        ("move", "foreign_process", "no_token", True), ("move", "drill", "no_token", True)]


def test_record_for_is_the_hooks_record_rule_without_a_segment():
    gd = gw.HookDecision(True, token_id=7, late_up=True)
    rec = record_for(InputEvent("button_up", 3, 4, button="right", mods=frozenset({"shift", "capslock"})), gd, 99,
                     secret=False, delay_ms=40)
    assert rec == sg.RingRecord(0, "button_up", "emitter", "late_up", frozenset({"shift"}), True, False, 99, 3, 4,
                                2, 40, 7)
    assert record_for(InputEvent("move", 1, 1), gw.HookDecision(True), 1) is None             # untokened allowed
    wheel = InputEvent("wheel", 1, 1, delta=-120, origin="console")
    drop = record_for(wheel, gw.HookDecision(False, "foreign_input"), 1)
    assert (drop.reason, drop.decision, drop.injected, drop.aux, drop.token_id) == ("foreign_input", "dropped", False,
                                                                                  -120, None)


# -- the probe -----------------------------------------------------------------------------------------------
def test_probe_move_stamps_last_probe_and_is_consumed_once():
    g = armed()
    buf, w = published(g)
    pid = w.issue_probe(5, 5, to_ns(T))
    assert pid & sg.PROBE_ID_BIT
    assert decide_from_segment(buf, ev("move", 900, 900), to_ns(T)).allowed       # a pending probe is no token
    assert read_header(buf).last_probe_mono_ts == INT64_MIN
    d = decide_from_segment(buf, ev("move", 6, 4), to_ns(T + 0.01))
    assert d == Decision(True, None, pid, False)
    assert read_header(buf).last_probe_mono_ts == to_ns(T + 0.01)
    assert token_states(buf)[pid] == ("consumed", None)
    decide_from_segment(buf, ev("move", 5, 5), to_ns(T + 0.02))
    assert read_header(buf).last_probe_mono_ts == to_ns(T + 0.01)                  # once
    assert [x.token_id for x in RingReader().read(buf)] == [pid]
    pid2 = w.issue_probe(5, 5, to_ns(T + 5))
    assert pid2 != pid and len(read_tokens(buf)) == 1                              # the probe slot is reused


def test_probe_is_answered_under_deny_all_but_the_move_is_dropped():
    g = armed()
    g.deny_all = True
    buf, w = published(g)
    pid = w.issue_probe(5, 5, to_ns(T))
    assert decide_from_segment(buf, ev("move", 5, 5, origin="foreign_process"), to_ns(T)).reason == "deny_all"
    assert read_header(buf).last_probe_mono_ts == INT64_MIN                        # only the emitter's move
    d = decide_from_segment(buf, ev("move", 5, 5), to_ns(T))
    assert (d.allowed, d.reason, d.token_id) == (False, "deny_all", pid)
    assert read_header(buf).last_probe_mono_ts == to_ns(T)
    pid3 = w.issue_probe(5, 5, to_ns(T + 10))
    assert decide_from_segment(buf, ev("move", 5, 5), to_ns(T + 10.6)).reason == "deny_all"   # expired probe
    assert read_header(buf).last_probe_mono_ts == to_ns(T) and token_states(buf)[pid3][0] == "armed"


def test_probe_never_takes_the_slot_of_a_published_token():
    g = armed()
    tokens = [g.issue_key(0x41, T) for _ in range(15)]
    buf, w = published(g)
    for t in tokens:
        decide_from_segment(buf, ev("key_down", vk=0x41), to_ns(T))
        decide_from_segment(buf, ev("key_up", vk=0x41), to_ns(T))
    assert {s for s, _ in token_states(buf).values()} == {"consumed"}
    w.issue_probe(1, 1, to_ns(T))
    assert {s for s, _ in token_states(buf).values()} == {"consumed", "armed"}     # 15 consumed tokens kept
    w.publish(g)
    assert all(token_states(buf)[t.id] == ("consumed", None) for t in tokens)       # never re-armed


# -- token lifecycle on drops and expiry (choice 6) ----------------------------------------------------------
def test_a_click_dropped_under_deny_all_wedges_no_later_move_in_the_segment():
    """The wedge, read from the bytes: a click dropped `deny_all` left its slot armed, and every later move was
    dropped `move_outside_allow`. The drop now writes the token `dropped` and names it in the ring record."""
    g = armed()
    t = g.issue_click(100, 100, T)
    g.deny_all = True
    buf, w = published(g)
    assert decide_from_segment(buf, ev("button_down", 100, 100), to_ns(T)) == Decision(False, "deny_all", t.id)
    assert token_states(buf)[t.id] == ("dropped", "deny_all")
    g.deny_all = False
    w.publish(g)
    assert decide_from_segment(buf, ev("move", 5, 5), to_ns(T + 0.1)).allowed
    assert decide_from_segment(buf, ev("move", 5, 5), to_ns(T + 0.6)).allowed
    assert decide_from_segment(buf, ev("button_down", 100, 100), to_ns(T + 0.1)).reason == "no_token"
    recs = RingReader().read(buf)
    assert [(r.reason, r.token_id) for r in recs] == [("deny_all", t.id), ("no_token", None)]


def test_the_hook_hosts_own_latch_and_rule2_spend_tokens_and_keep_their_secret_flag():
    g = armed()
    click = g.issue_click(100, 100, T)
    buf, _ = published(g)
    d = decide_from_segment(buf, ev("button_down", 100, 100), to_ns(T), deny_all=True)
    assert d == Decision(False, "deny_all", click.id) and token_states(buf)[click.id] == ("dropped", "deny_all")
    g = armed(Flag.SET)
    key = g.issue_key(gw.VK_RETURN, T, secret=True)
    buf, _ = published(g)
    assert decide_from_segment(buf, ev("key_down", vk=gw.VK_RETURN), to_ns(T)).token_id == key.id
    assert token_states(buf)[key.id] == ("dropped", "blocked_key_dialog")
    rec = RingReader().read(buf)[0]
    assert (rec.reason, rec.token_id, rec.secret) == ("blocked_key_dialog", key.id, True)   # redacted downstream
    assert decide_from_segment(buf, ev("move", 5, 5), to_ns(T)).allowed


def test_an_expired_armed_slot_blocks_no_move_without_any_republish():
    g = armed()
    t = g.issue_click(100, 100, T)
    buf, _ = published(g)
    assert decide_from_segment(buf, ev("button_down", 110, 100), to_ns(T)).reason == "token_mismatch_coords"
    assert decide_from_segment(buf, ev("move", 5, 5), to_ns(T + 0.5)).reason == "move_outside_allow"
    assert decide_from_segment(buf, ev("move", 5, 5), to_ns(T + 0.6)).allowed       # the hook's own expiry rule
    assert token_states(buf)[t.id] == ("armed", None)


def test_the_segment_prefers_an_unexpired_match_in_any_slot_over_a_dead_token():
    """Choice 6 (orchestrator.gateway choice 18), read from the bytes: the fresh token sits in an earlier slot than
    the dead one, so neither slot order nor issue order decides; expiry does."""
    g = armed()
    used = g.issue_click(500, 500, T)
    dead = g.issue_click(100, 100, T)
    buf, w = published(g)
    for kind in ("button_down", "button_up"):
        assert decide_from_segment(buf, ev(kind, 500, 500), to_ns(T)).allowed
    assert decide_from_segment(buf, ev("button_down", 110, 100), to_ns(T)).reason == "token_mismatch_coords"
    apply_token_states(buf, g)
    g.stamp_watcher(Flag.CLEAR, T + 0.6)
    fresh = g.issue_click(101, 99, T + 0.6)                                        # takes the used token's slot
    w.publish(g)
    slots = {s.id: s.slot for s in read_tokens(buf)}
    assert slots[fresh.id] < slots[dead.id] and used.id not in slots
    d = decide_from_segment(buf, ev("button_down", 100, 100), to_ns(T + 0.7))
    assert d == Decision(True, None, fresh.id) and token_states(buf)[dead.id] == ("armed", None)
    d = decide_from_segment(buf, ev("button_down", 100, 100), to_ns(T + 0.7))
    assert d == Decision(False, "token_expired", dead.id)                          # only expired matches left
    assert token_states(buf)[dead.id] == ("expired", "token_expired")


def test_the_segment_reuses_the_slots_of_expired_armed_tokens():
    g = armed()
    old = [g.issue_key(0x41, T) for _ in range(16)]
    buf, w = published(g)
    new = g.issue_key(0x42, T + 0.6)                                               # the slow half lets the dead go
    w.publish(g)
    slots = read_tokens(buf)
    assert [(s.id, s.state) for s in slots] == [(new.id, sg.ARMED)]
    assert slots[0].slot < 16 and new.id not in {t.id for t in old}
    assert decide_from_segment(buf, ev("key_down", vk=0x42), to_ns(T + 0.6)).allowed


# -- parity with orchestrator.gateway.hook_decide ------------------------------------------------------------
class Dual:
    """Runs `hook_decide` and `decide_from_segment` side by side on every call and checks they agree: the
    decision, every token's state and drop reason, and the Send-rectangle pass."""

    def __init__(self):
        self.segs: dict[int, tuple[GatewayState, bytearray, SegmentWriter]] = {}
        self.calls = 0

    def __call__(self, state: GatewayState, e: InputEvent, now: float):
        if id(state) not in self.segs:
            buf = fresh()
            self.segs[id(state)] = (state, buf, SegmentWriter(buf))
        _, buf, w = self.segs[id(state)]
        w.publish(state)
        want = hook_decide(state, e, now)
        got = decide_from_segment(buf, e, to_ns(now))
        assert tuple(got) == (want.allowed, want.reason, want.token_id, want.late_up), (e, now)
        assert got.decision == want.decision
        states = token_states(buf)
        for tid, tok in state.tokens.items():
            assert states[tid] == (tok.state, tok.drop_reason), (e, tid)
        assert bool(buf[sg.OFF_SEND_PASSED]) == state.send_rect_passed
        self.calls += 1
        return want


def _load_gateway_tests():
    spec = importlib.util.spec_from_file_location("_parity_gateway_tests", TESTS / "test_orchestrator_gateway.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cases(fn):
    """Call arguments for a test function: its parametrize rows, or one call when it takes none."""
    params = list(inspect.signature(fn).parameters)
    if not params:
        return [{}]
    marks = [m for m in getattr(fn, "pytestmark", []) if m.name == "parametrize"]
    if len(marks) != 1:
        return []
    names = marks[0].args[0]
    names = [n.strip() for n in names.split(",")] if isinstance(names, str) else list(names)
    if sorted(names) != sorted(params):
        return []
    rows = []
    for row in marks[0].args[1]:
        values = getattr(row, "values", row)
        rows.append(dict(zip(names, values if len(names) > 1 else (values,))))
    return rows


def test_parity_over_the_orchestrator_gateway_tests(monkeypatch):
    mod = _load_gateway_tests()
    dual = Dual()
    monkeypatch.setattr(mod, "hook_decide", dual)
    monkeypatch.setattr(simrig, "hook_decide", dual)
    ran = 0
    for name, fn in inspect.getmembers(mod, inspect.isfunction):
        if not name.startswith("test_") or fn.__module__ != mod.__name__:
            continue
        for kwargs in _cases(fn):
            fn(**kwargs)
            ran += 1
    assert ran >= 40 and dual.calls >= 60


def _random_state(rng: random.Random) -> GatewayState:
    g = GatewayState(armed=rng.random() < 0.9, deny_all=rng.random() < 0.05)
    g.stamp_watcher(rng.choice(list(Flag)), T - rng.choice((0.0, 0.1, 0.45, 0.6, 2.0)))
    points = [(100, 100), (102, 99), (400, 300), (920, 610)]
    for _ in range(rng.randint(0, 9)):
        try:
            k = rng.random()
            if k < 0.35:
                x, y = rng.choice(points)
                g.issue_click(x, y, T - rng.choice((0.0, 0.3, 0.7)), button=rng.choice(("left", "right")),
                              dialog_ok=rng.random() < 0.5, send_ok=rng.random() < 0.2,
                              hold_ms=rng.choice((None, None, 300)))
            elif k < 0.6:
                g.issue_key(rng.choice((0x41, 0x0D, 0x20, 0x09, 0x10, 0x70)), T - rng.choice((0.0, 0.7)),
                            secret=rng.random() < 0.2)
            elif k < 0.8:
                x, y = rng.choice(points)
                g.issue_wheel(x, y, rng.choice((1, -1)), T - rng.choice((0.0, 0.7)))
            else:
                g.issue_release(T)
        except gw.GatewayError:
            break
    if rng.random() < 0.4:
        g.open_move_allow(Rect(90, 90, 40, 30), T - rng.choice((0.0, 2.0)), 300)
    if rng.random() < 0.5:
        g.write_deny_rects([(Rect(900, 600, 80, 28), "Send"), (Rect(380, 290, 10, 10), "Flatten")][:rng.randint(1, 2)])
    if rng.random() < 0.5:
        g.load_chords([Chord.parse("B"), Chord.parse("Shift+B"), Chord.parse("Ctrl+A")][:rng.randint(1, 3)])
    return g


def _random_event(rng: random.Random) -> InputEvent:
    kind = rng.choice(("move", "button_down", "button_up", "wheel", "key_down", "key_up"))
    x, y = rng.choice(((100, 100), (101, 101), (104, 100), (400, 300), (920, 610), (110, 100), (5, 5)))
    kw = {"origin": rng.choice(("emitter",) * 6 + ("foreign_process", "console", "drill")),
          "mods": frozenset(rng.sample(["shift", "ctrl", "alt", "win"], rng.choice((0, 0, 0, 1, 2))))}
    if kind.startswith("button"):
        kw["button"] = rng.choice(("left", "left", "right"))
    if kind.startswith("key"):
        kw["vk"] = rng.choice((0x41, 0x42, 0x0D, 0x20, 0x09, 0x10, 0x11, 0x70, 0x5B))
    if kind == "wheel":
        kw["delta"] = rng.choice((120, -120, 0))
    return InputEvent(kind, x, y, **kw)


def test_parity_on_a_seeded_random_corpus():
    rng = random.Random(20261111)
    dual = Dual()
    for _ in range(400):
        g = _random_state(rng)
        for _ in range(rng.randint(1, 12)):
            dual(g, _random_event(rng), T + rng.choice((0.0, 0.2, 0.5, 0.6, 1.2, 2.5)))
    assert dual.calls > 2000


LIFE_POINTS = ((100, 100), (101, 99), (400, 300), (920, 610))


def _issue_any(g: GatewayState, rng: random.Random, now: float) -> None:
    """One token of a random kind at `now`, as the emitter writes them (stage 5), or a new token for the target of
    a click or key token that expired unconsumed, as the retry of 5.4.3 stage 7 re-tokenizes; a full segment issues
    nothing."""
    x, y = rng.choice(LIFE_POINTS)
    k = rng.random()
    retry = [t for t in g.tokens.values() if t.state == "armed" and t.kind in ("click", "key") and now > t.expires]
    try:
        if retry and rng.random() < 0.5:
            t = rng.choice(retry)
            if t.kind == "click":
                g.issue_click(t.x, t.y, now, button=t.button or "left", dialog_ok=t.dialog_ok)
            else:
                g.issue_key(t.vk or 0, now)
        elif k < 0.4:
            g.issue_click(x, y, now, button=rng.choice(("left", "right")), dialog_ok=rng.random() < 0.6,
                          hold_ms=rng.choice((None, None, None, 300)))
        elif k < 0.75:
            g.issue_key(rng.choice((0x41, 0x0D, 0x20, 0x09, 0x11, 0x70)), now, secret=rng.random() < 0.2)
        elif k < 0.95:
            g.issue_wheel(x, y, rng.choice((1, -1)), now)
        else:
            g.issue_release(now)
    except gw.GatewayError:
        pass


def _emitter_event(rng: random.Random, g: GatewayState) -> InputEvent:
    """Mostly the emitter's own events at token points, so rule 1 and rule 2 drops meet real tokens; often the down
    of a click or key token `g` holds, as the emitter sends it."""
    held = [t for t in g.tokens.values() if t.kind in ("click", "key")]
    if held and rng.random() < 0.4:
        t = rng.choice(held)
        mods = rng.choice((frozenset(),) * 4 + (frozenset({"shift"}),))
        if t.kind == "click":
            return InputEvent("button_down", t.x, t.y, button=t.button, mods=mods)
        return InputEvent("key_down", vk=t.vk, mods=mods)
    kind = rng.choice(("move", "button_down", "button_down", "button_up", "wheel", "key_down", "key_down", "key_up"))
    x, y = rng.choice(LIFE_POINTS + ((5, 5), (110, 100)))
    kw = {"origin": rng.choice(("emitter",) * 8 + ("foreign_process", "drill")),
          "mods": rng.choice((frozenset(),) * 3 + (frozenset({"shift"}), frozenset({"ctrl"}), frozenset({"alt"})))}
    if kind.startswith("button"):
        kw["button"] = rng.choice(("left", "left", "right"))
    if kind.startswith("key"):
        kw["vk"] = rng.choice((0x41, 0x0D, 0x20, 0x09, 0x11, 0x70))
    if kind == "wheel":
        kw["delta"] = rng.choice((120, -120))
    return InputEvent(kind, x, y, **kw)


def _downs_matching(g: GatewayState, e: InputEvent) -> list[gw.Token]:
    """The armed tokens the emitter's button or key down matches, in issue order (5.4.4), whatever their expiry."""
    if e.origin != "emitter" or e.type not in ("button_down", "key_down"):
        return []
    if e.type == "key_down":
        return [t for t in g.tokens.values() if t.kind == "key" and t.vk == e.vk and t.state == "armed"]
    return [t for t in g.tokens.values() if t.kind == "click" and t.button == e.button and t.state == "armed"
            and abs(t.x - e.x) <= t.tol_px and abs(t.y - e.y) <= t.tol_px]


def test_parity_on_a_seeded_random_corpus_with_expiry_and_spent_tokens():
    """Choice 6 and orchestrator.gateway choice 17 at random: tokens issued as time runs, the hook armed and disarmed,
    deny-all and the flag toggled, events dropped under rules 1 and 2 that spend their tokens, and time run past
    expiry. Both halves decide alike throughout; once every token has expired with none half consumed, the
    emitter's move is allowed (a dead token never wedges input); and a down that matches a dead token first and a
    live one later is decided on the live one (choice 18)."""
    rng = random.Random(20261112)
    dual = Dual()
    unwedged, preferred, spent = 0, 0, set()
    for _ in range(600):
        g = GatewayState(armed=True)
        now = T
        g.stamp_watcher(Flag.CLEAR, now)
        for _ in range(rng.randint(6, 30)):
            k = rng.random()
            if k < 0.25:
                _issue_any(g, rng, now)
            elif k < 0.33:
                g.deny_all, g.armed = rng.random() < 0.3, rng.random() < 0.85
            elif k < 0.43:
                g.stamp_watcher(rng.choice(list(Flag)), now)
            elif k < 0.55:
                now += rng.choice((0.1, 0.3, 0.6, 2.5))
            else:
                e = _emitter_event(rng, g)
                matching = _downs_matching(g, e)
                live = [t for t in matching if now <= t.expires]
                d = dual(g, e, now)
                if live and live[0] is not matching[0]:
                    assert d.token_id == live[0].id, (e, now)
                    preferred += 1
                if not d.allowed and d.token_id is not None and g.tokens[d.token_id].drop_reason == d.reason:
                    spent.add(d.reason)
            dead = all(t.state != "half_consumed" and now > t.expires for t in g.tokens.values())
            if dead and g.armed and not g.deny_all:
                assert dual(g, InputEvent("move", 5, 5), now).allowed
                unwedged += 1
    assert dual.calls > 6000 and unwedged > 1200 and preferred > 30
    assert {"deny_all", "not_armed", "blocked_key_dialog", "blocked_chord_dialog", "shift_click_dialog"} <= spent


def test_a_new_send_ok_token_reopens_the_send_rect_once_and_the_writer_never_sets_it():
    """6.7: each send_ok token passes the Send rectangle once; publishing clears the pass only for a new token."""
    g = gw.GatewayState(armed=True)
    g.deny_rects = (gw.DenyRect(gw.Rect(900, 600, 40, 20), "Send"),)
    seg = Segment.create(orchestrator_pid=1)
    try:
        w = SegmentWriter(seg.buf)
        click = gw.InputEvent("button_down", 920, 610, button="left", origin="emitter")
        for _ in range(2):                              # two declared sends, each with its own token
            g.issue_click(920, 610, T, send_ok=True, dialog_ok=True)
            w.publish(g)
            assert decide_from_segment(seg.buf, click, to_ns(T)).allowed
            w.publish(g)                                # re-publishing the same token does not reopen it
            assert seg.buf[sg.OFF_SEND_PASSED] == 1
    finally:
        seg.close()
        seg.unlink()


def test_a_secret_keys_up_is_flagged_secret_whatever_happened_to_its_down():
    """6.9, choice 7: the up of a secret key whose down passed before the kill switch, was dropped under deny-all or
    came after its token expired matches no half consumed token, yet its record carries the secret flag, so the
    drainer never writes its vk; a key event the hook has no consistent picture for is flagged too."""
    g = armed()
    for vk in (0x50, 0x57):
        g.issue_key(vk, T, secret=True)
    g.issue_key(0x41, T)                                                           # not secret: stays in clear
    buf, w = published(g)
    assert decide_from_segment(buf, ev("key_down", vk=0x50), to_ns(T)).allowed       # P passes, then the trip
    g.deny_all = True
    w.publish(g)
    for e in (ev("key_up", vk=0x50), ev("key_down", vk=0x57), ev("key_up", vk=0x57), ev("key_down", vk=0x41),
              ev("key_up", vk=0x41)):
        assert decide_from_segment(buf, e, to_ns(T)).reason == "deny_all"
    assert [(r.event_type, r.vk, r.secret) for r in RingReader().read(buf)] == [
        ("key_down", 0x50, True), ("key_up", 0x50, True), ("key_down", 0x57, True), ("key_up", 0x57, True),
        ("key_down", 0x41, False), ("key_up", 0x41, False)]
    g = armed()
    g.issue_key(0x39, T, secret=True)
    buf, _ = published(g)
    assert decide_from_segment(buf, ev("key_down", vk=0x39), to_ns(T + 0.6)).reason == "token_expired"
    assert decide_from_segment(buf, ev("key_up", vk=0x39), to_ns(T + 0.6)).reason == "no_token"
    assert [r.secret for r in RingReader().read(buf)] == [True, True]
    torn = fresh()
    struct.pack_into("<Q", torn, sg.OFF_SEQ, 1)                                    # a writer died mid-write
    for e in (ev("key_up", vk=0x52), ev("button_up", 1, 1)):
        assert decide_from_segment(torn, e, to_ns(T)).reason == "deny_all"
    assert [(r.event_type, r.secret) for r in RingReader().read(torn)] == [("key_up", True), ("button_up", False)]
