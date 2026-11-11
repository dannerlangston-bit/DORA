"""Frozen Branch B, OnDemand: entry and clock check, the timeout counter, the toolbar-bound allowlist entry, replay
scope and the masked_live fallback (7.9, POP-12, 4.9)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sentinel.orchestrator.identity import Allowlist, classify, judge_identity, load_allowlist
from sentinel.orchestrator.ondemand import (OnDemandRun, check_clock, effective_data_mode, enter,
                                            frozen_fallback_active, replay_scope)
from sentinel.orchestrator.rig import SelectorRead

EST = timezone(timedelta(hours=-5))
TS = datetime(2026, 11, 2, 10, 0, 0, tzinfo=EST)


class FakeOnDemand:
    """OnDemandControls with scripted buffering and clock reads."""

    def __init__(self, buffered: bool = True, clock: str | None = "11/02/2026 10:00:00"):
        self.buffered, self.clock, self.calls = buffered, clock, []

    def open(self):
        self.calls.append("open")

    def set_timestamp(self, at):
        self.calls.append(("timestamp", at))

    def go(self):
        self.calls.append("go")

    def wait_buffering(self, timeout_s):
        self.calls.append(("wait", timeout_s))
        return self.buffered

    def pause(self):
        self.calls.append("pause")

    def read_clock(self):
        return self.clock

    def park_mouse(self):
        self.calls.append("park")


def test_entry_pauses_at_the_timestamp_and_parks_the_mouse():
    od = FakeOnDemand()
    r = enter(od, TS, OnDemandRun(), shard_id="run-01/1")
    assert r.passed and od.calls == ["open", ("timestamp", TS), "go", ("wait", 90.0), "pause", "park"]


def test_clock_1001_against_1000_is_data_epoch_mismatch():
    r = check_clock("10:01", TS, clock_format="%H:%M")
    assert not r.passed and (r.checkpoint.reason, r.checkpoint.gate) == ("data_epoch_mismatch", "fixture_verified")
    assert check_clock("10:00", TS, clock_format="%H:%M").passed
    assert check_clock("11/2/2026  10:00:00", TS).passed                 # zero padding and spacing do not matter
    assert check_clock(None, TS).checkpoint.reason == "data_epoch_mismatch"
    assert not check_clock("garbled", TS).passed
    od = FakeOnDemand(clock="11/02/2026 10:00:05")
    r = enter(od, TS, OnDemandRun(), shard_id="run-01/1")
    assert r.checkpoint.reason == "data_epoch_mismatch" and "park" not in od.calls


def test_the_third_consecutive_timeout_abandons_ondemand_with_one_alert():
    run, od = OnDemandRun(), FakeOnDemand(buffered=False)
    first = enter(od, TS, run, shard_id="run-01/1")
    second = enter(od, TS, run, shard_id="run-01/1")
    assert first.checkpoint.reason == second.checkpoint.reason == "ondemand_timeout"
    assert first.alert is None and second.alert is None and not first.outcomes and not run.abandoned
    third = enter(od, TS, run, shard_id="run-01/1")
    assert run.abandoned and third.alert.rule == "env_error"
    (sub,) = third.outcomes
    assert (sub.selector, sub.outcome.reason, sub.outcome.gate) == ("frozen", "ondemand_timeout", "fixture_verified")
    od.calls.clear()
    later = enter(FakeOnDemand(), TS, run, shard_id="run-01/2")          # another shard: not tried again
    assert later.outcomes[0].outcome.reason == "ondemand_timeout" and later.alert is None and od.calls == []


def test_a_successful_buffering_resets_the_streak():
    run = OnDemandRun()
    for _ in range(2):
        run.timeout("s1")
    run.buffered("s1")
    assert run.timeout("s1").outcomes == () and run.timeout("s1").outcomes == () and not run.abandoned
    assert run.timeout("s1").outcomes and run.timeout("s2").alert is None   # a second shard's streak: no new alert


# -- the toolbar-bound allowlist entry ---------------------------------------------------------------------------------
ALLOW = Allowlist("rig-01", ("D-20481133", "D-20481134"), "0" * 64, ondemand_account="OD-100000",
                  ondemand_template="templates/ondemand-toolbar.png")


def read(text: str) -> SelectorRead:
    return SelectorRead(text, landmark_found=True, confidence=99.0)


def test_the_virtual_account_passes_only_with_the_toolbar():
    ok = judge_identity([read("OD-100000")], ALLOW, ondemand_toolbar=True)
    assert ok.passed and ok.evidence["allowlist_entry"] == "ondemand_virtual_account"
    bad = judge_identity([read("OD-100000")], ALLOW)
    assert not bad.passed and bad.run.status == "aborted_safety" and bad.run.reason == "paper_mode_mismatch"
    assert classify("OD-100000", ALLOW) == "paper_mode_mismatch"
    assert judge_identity([read("D-20481133")], ALLOW, ondemand_toolbar=True).evidence["allowlist_entry"] == 1
    assert not judge_identity([read("OD-100000")], Allowlist("rig-01", ("D-1",), "0" * 64),
                              ondemand_toolbar=True).passed               # Branch A: no OnDemand entry at all


def test_load_allowlist_reads_the_ondemand_entry(tmp_path):
    p = tmp_path / "allowlist.yaml"
    p.write_text("schema_version: 1\nrigs:\n  - {rig: rig-01, account_ids: [D-1], login_ref: vault://x}\n"
                 "ondemand_virtual_account: {account_id: OD-100000, valid_while_template: templates/od.png}\n")
    a = load_allowlist(p, "rig-01")
    assert (a.ondemand_account, a.ondemand_template) == ("OD-100000", "templates/od.png")
    p.write_text("schema_version: 1\nrigs:\n  - {rig: rig-01, account_ids: [D-1], login_ref: vault://x}\n")
    assert load_allowlist(p, "rig-01").ondemand_account is None


# -- scope -------------------------------------------------------------------------------------------------------------
def test_gadgets_that_do_not_replay_are_not_run_scope():
    out = replay_scope({"c1": "chart.main", "c2": "scan.results", "c3": "watchlist"}, {"chart.main", "watchlist"})
    assert list(out) == ["c2"] and (out["c2"].outcome, out["c2"].reason) == ("not_run", "scope")


def test_frozen_falls_back_to_masked_live_only_under_branch_b_without_ondemand():
    assert frozen_fallback_active("B", ondemand_usable=False)
    assert not frozen_fallback_active("B", ondemand_usable=True) and not frozen_fallback_active("A", False)
    assert effective_data_mode("frozen", branch="B", ondemand_usable=False) == "masked_live"
    assert effective_data_mode("frozen", branch="A", ondemand_usable=False) == "frozen"
    assert effective_data_mode("masked_live", branch="B", ondemand_usable=False) == "masked_live"
    with pytest.raises(ValueError):
        effective_data_mode("live", branch="A", ondemand_usable=True)
