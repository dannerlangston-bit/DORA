"""The gateway files of 4.16: dismiss allowlist and default order hotkeys (5.4.3, 5.4.7, 6.2, 6.13 item 10)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.orchestrator.drill import DrillRecord, expected_records
from sentinel.orchestrator.gateway import (Action, Chord, Flag, GatewayState, InputEvent, SafetyContext, hook_decide,
                                           slow_half)
from sentinel.orchestrator.gateway_config import (DEFAULT_ORDER_HOTKEYS_PATH, DISMISS_ALLOWLIST_PATH,
                                                  GatewayConfigError, chord_problems, label_problems,
                                                  load_default_order_hotkeys, load_dismiss_allowlist,
                                                  order_hotkey_list)
from sentinel.orchestrator.ports import SessionSafety

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
PIN = "read_on_build: main-1998.2.0-4f1c9a2\nread_by: system-owner\nread_at: '2026-10-05T14:00:00Z'\n"


def write(tmp_path: Path, text: str, name: str = "file.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


# -- dismiss allowlist ----------------------------------------------------------------------------------------
def test_the_allowlist_is_empty_until_phase_0_records_its_read(tmp_path):
    example = load_dismiss_allowlist(EXAMPLE / DISMISS_ALLOWLIST_PATH)
    assert example.labels == frozenset() and not example.pinned
    assert load_dismiss_allowlist(tmp_path / "absent.yaml").labels == frozenset()
    unpinned = load_dismiss_allowlist(write(tmp_path, "schema_version: 1\nlabels: [Delete]\n"))
    assert unpinned.labels == frozenset() and unpinned.file_labels == ("Delete",) and not unpinned.pinned
    pinned = load_dismiss_allowlist(write(tmp_path, "schema_version: 1\nlabels: [Delete]\n" + PIN))
    assert pinned.labels == frozenset({"Delete"}) and pinned.pinned


def test_the_loaded_empty_allowlist_stops_ticket_flows_before_the_dialog():
    allow = load_dismiss_allowlist(EXAMPLE / DISMISS_ALLOWLIST_PATH)
    ctx = SafetyContext(flag=Flag.SET, dismiss_allowlist=allow.labels,
                        session=SessionSafety("a3_a9", 999.0, "sha"), now=1000.0)
    s = slow_half(Action("click", point=(1500, 900), resolved_name="Confirm and Send",
                         under_point_text="Confirm and Send", order_stage_step=True), ctx)
    assert s.refused and s.reason == "deny_profile" and not s.attempted_send


@pytest.mark.parametrize("text", ["labels: [Send]", "labels: [Confirm]", "labels: ['Del.*']", "labels: [' Delete']",
                                  "labels: [Submit Order]", "labels: Delete", "labels: [Delete", "- Delete"])
def test_a_send_like_pattern_or_malformed_allowlist_fails_closed(tmp_path, text):
    with pytest.raises(GatewayConfigError) as err:
        load_dismiss_allowlist(write(tmp_path, "schema_version: 1\n" + text + "\n" + PIN))
    assert err.value.rule == "L-XREF-18"


def test_label_problems():
    assert label_problems("Delete") == [] and label_problems("Edit") == []
    assert any("send-like" in m for m in label_problems("Replace"))
    assert any("send-like" in m for m in label_problems("Confirm and Send"))
    assert any("pattern" in m for m in label_problems("(Delete|Edit)"))
    assert label_problems("") and label_problems(None)


# -- default order hotkeys ------------------------------------------------------------------------------------
def test_the_example_hotkeys_load_and_feed_the_chord_list_and_drill_item_3():
    hk = load_default_order_hotkeys(EXAMPLE / DEFAULT_ORDER_HOTKEYS_PATH)
    assert hk.chords == (Chord.parse("Shift+B"), Chord.parse("Shift+S")) and hk.actions == ("buy", "sell")
    a6 = (Chord.parse("Ctrl+Shift+C"), Chord.parse("shift+b"))
    chords = order_hotkey_list(a6, hk)
    assert chords == (Chord.parse("Ctrl+Shift+C"), Chord.parse("Shift+B"), Chord.parse("Shift+S"))
    g = GatewayState(armed=True)
    g.load_chords(list(chords))
    g.stamp_watcher(Flag.SET, 10.0)
    g.issue_key(ord("S"), 10.0)
    assert hook_decide(g, InputEvent("key_down", vk=ord("S"), mods=frozenset({"shift"})), 10.0).reason == \
        "blocked_chord_dialog"
    item3 = expected_records(hk.chords)
    assert item3[DrillRecord(3, "raw", "dropped", "blocked_chord_dialog")] == len(hk.chords)


def test_a_missing_hotkeys_file_fails_closed_at_settings_attested(tmp_path):
    with pytest.raises(GatewayConfigError) as err:
        load_default_order_hotkeys(tmp_path / "gateway" / "default_order_hotkeys.yaml")
    r = err.value.result
    assert (r.run_status, r.run_reason, r.gate) == ("aborted_harness", "config_invalid", "settings_attested")
    assert (r.unreached.outcome, r.unreached.reason) == ("harness_error", "config_invalid")
    assert r.alert.paged and "L-XREF-17" in r.detail


@pytest.mark.parametrize("chords", ["[{chord: Hyper+B, action: buy}]", "[{chord: Shift+, action: buy}]",
                                    "[{chord: Shift+B, action: buy}, {chord: shift+b, action: buy}]",
                                    "[{action: buy}]", "nope"])
def test_bad_or_repeated_chords_fail_closed(tmp_path, chords):
    with pytest.raises(GatewayConfigError) as err:
        load_default_order_hotkeys(write(tmp_path, f"schema_version: 1\nbuild: b-1\nchords: {chords}\n"))
    assert err.value.rule == "L-XREF-17"


def test_at_most_sixteen_chords():
    many = [{"chord": f"Shift+{c}", "action": "buy"} for c in "ABCDEFGHIJKLMNOPQ"]
    assert chord_problems(many[:16]) == []
    assert any("16" in m for _, m in chord_problems(many))
    hk = load_default_order_hotkeys(EXAMPLE / DEFAULT_ORDER_HOTKEYS_PATH)
    with pytest.raises(GatewayConfigError):
        order_hotkey_list([Chord.parse(e["chord"]) for e in many[2:17]], hk)
    assert chord_problems([{"chord": "Ctrl+Shift+B"}, {"chord": "Shift+Ctrl+B"}])[0][0] == 1
