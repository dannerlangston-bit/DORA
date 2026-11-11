"""Rig Manager logic: vmoptions render and verify, install outcomes, the rig pool with the spare rule, heartbeats
(5.3, 7.1, 7.3, 10.7, 11.2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.orchestrator.clock import ManualClock
from sentinel.rigfiles import vmoptions_sha256
from sentinel.rigmanager import (HeartbeatMonitor, HeartbeatPacer, PoolError, RigHeartbeat, RigPool, RigSlot,
                                 first_launch_agreement, install_outcome, placeholders, render_vmoptions,
                                 verify_vmoptions, vmoptions_failure)

TEMPLATE = (Path(__file__).resolve().parents[1] / "config" / "example" / "rig" /
            "thinkorswim.vmoptions.template").read_text(encoding="utf-8")
VALUES = {"heap_min": "1g", "heap_max": "4g", "crash_dir": "C:\\sentinel\\crash"}


def test_render_fills_every_placeholder_and_verifies():
    assert placeholders(TEMPLATE) == {"heap_min", "heap_max", "crash_dir"}
    text = render_vmoptions(TEMPLATE, VALUES)
    assert "-Xmx4g" in text and "-XX:HeapDumpPath=C:\\sentinel\\crash" in text and "${" not in text
    assert verify_vmoptions(text, pinned_sha256=vmoptions_sha256(text), jdk_major=11) == ()


def test_render_refuses_a_missing_or_unused_value():
    with pytest.raises(ValueError, match="heap_max"):
        render_vmoptions(TEMPLATE, {"heap_min": "1g", "crash_dir": "C:\\c"})
    with pytest.raises(ValueError, match="heap_maxx"):
        render_vmoptions(TEMPLATE, {**VALUES, "heap_maxx": "4g"})


def test_verify_names_a_missing_crash_flag_and_a_hash_mismatch():
    text = render_vmoptions(TEMPLATE, VALUES)
    pinned = vmoptions_sha256(text)
    stripped = text.replace("-XX:+CreateCoredumpOnCrash\n", "")
    problems = verify_vmoptions(stripped, pinned_sha256=pinned, jdk_major=11)
    assert problems[0].startswith("vmoptions hash") and any("CreateCoredumpOnCrash is missing" in p for p in problems)


def test_a_rewritten_vmoptions_before_a_run_is_b3_env_error_and_at_creation_install_failed():
    text = render_vmoptions(TEMPLATE, VALUES)
    problems = verify_vmoptions(text.replace("4g", "2g"), pinned_sha256=vmoptions_sha256(text))
    r = vmoptions_failure(problems, at_creation=False)
    assert (r.unreached.outcome, r.unreached.gate, r.run.status) == ("env_error", "preflight.B3",
                                                                    "complete_with_env_errors")
    c = vmoptions_failure(problems, at_creation=True, installer_log_sha256="sha256:" + "1" * 64)
    assert c.unreached.reason == "install_failed" and c.evidence["installer_log"] == "sha256:" + "1" * 64
    assert vmoptions_failure((), at_creation=False) is None


def test_install_outcomes():
    assert install_outcome(0, True, None) is None
    r = install_outcome(1603, True, "sha256:" + "2" * 64)
    assert r.unreached.reason == "install_failed" and "installer exit 1603" in r.detail
    assert "missing" in install_outcome(0, False, None).detail
    a = first_launch_agreement("j.doe")
    assert (a.unreached.reason, a.gate, a.evidence["operator"]) == ("agreement_pending", "authenticated", "j.doe")


# -- the pool ------------------------------------------------------------------------------------------------------
def fleet() -> RigPool:
    slots = [RigSlot(f"rig-0{i}", "pool", f"login-{i}", "g1", f"app.main.g1-r{i}") for i in range(1, 5)]
    slots += [RigSlot("rig-05", "nightly", "login-5", "g1", "app.main.g1-r5"),
              RigSlot("rig-06", "explorer", "login-explorer", "g1", "explorer.main.g1")]
    return RigPool.of(slots)


def test_one_login_on_two_rigs_raises():
    pool = fleet()
    with pytest.raises(PoolError, match="never live on two rigs"):
        pool.assign("rig-03", "login-2", operator="ops", at="2026-11-10T07:00:00Z")
    assert pool.rigs["rig-03"].login == "login-3"                   # nothing changed
    with pytest.raises(PoolError):
        RigPool.of([RigSlot("rig-01", "pool", "x", "g1", "s1"), RigSlot("rig-06", "explorer", "x", "g1", "s6")])


def test_substituting_rig_6_for_rig_2_moves_the_login_records_the_operator_and_disables_the_explorer():
    pool = fleet()
    assert pool.explorer_allowed
    ev = pool.substitute("rig-02", operator="a.operator", snapshot_id="app.main.g1-r6-login2",
                         at="2026-11-10T07:05:00Z")
    assert (ev.operator, ev.from_rig, ev.login) == ("a.operator", "rig-02", "login-2")
    assert pool.rigs["rig-06"].login == "login-2" and pool.rigs["rig-06"].role == "pool"
    assert not pool.rigs["rig-02"].active and not pool.explorer_allowed
    with pytest.raises(PoolError, match="no Explorer session"):
        pool.require_explorer()
    pool.release_spare(explorer_login="login-explorer", snapshot_id="explorer.main.g1-b", operator="a.operator",
                       at="2026-11-10T19:00:00Z")
    assert pool.explorer_allowed and pool.rigs["rig-06"].role == "explorer"
    assert [e.action for e in pool.events] == ["substitute", "release_spare"]


def test_a_substitution_needs_a_fresh_snapshot_an_operator_and_a_pool_rig():
    pool = fleet()
    with pytest.raises(PoolError, match="new snapshot id"):
        pool.substitute("rig-02", operator="ops", snapshot_id="explorer.main.g1", at="t")
    with pytest.raises(PoolError, match="operator"):
        pool.substitute("rig-02", operator="", snapshot_id="new", at="t")
    with pytest.raises(PoolError, match="pool rig"):
        pool.substitute("rig-05", operator="ops", snapshot_id="new", at="t")
    pool.substitute("rig-01", operator="ops", snapshot_id="new", at="t")
    with pytest.raises(PoolError, match="already stands in"):
        pool.substitute("rig-03", operator="ops", snapshot_id="newer", at="t")


def test_requeue_target_is_a_free_pool_rig_of_the_same_generation():
    pool = fleet()
    pool.rigs["rig-03"] = RigSlot("rig-03", "pool", "login-3", "g2", "app.main.g2-r3")
    assert pool.requeue_target("rig-01", busy=("rig-02",)) == "rig-04"
    assert pool.requeue_target("rig-03") is None                    # no other g2 pool rig
    assert pool.requeue_target("rig-01", busy=("rig-02", "rig-04")) is None


# -- heartbeats ----------------------------------------------------------------------------------------------------
class Sink:
    def __init__(self):
        self.sent: list[RigHeartbeat] = []

    def send(self, hb: RigHeartbeat) -> None:
        self.sent.append(hb)


def test_the_pacer_sends_every_30_s_and_the_monitor_loses_a_rig_after_3_minutes():
    clock = ManualClock()
    sink, pacer, monitor = Sink(), HeartbeatPacer("rig-01", clock), HeartbeatMonitor(clock)
    assert pacer.tick(sink, "running", "run-01")
    clock.advance(20)
    assert not pacer.tick(sink, "running")
    clock.advance(10)
    assert pacer.tick(sink, "running")
    for hb in sink.sent:
        monitor.receive(hb)
    monitor.watch("rig-02")                                         # watched now, never heard from
    clock.advance(179)
    assert monitor.lost() == ()
    clock.advance(1)
    assert monitor.lost() == ("rig-01", "rig-02")
    assert len(sink.sent) == 2 and sink.sent[0].run_id == "run-01"
