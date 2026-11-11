"""Exceptions into the closed taxonomy (8.9, 10.1, 10.2; executor/failures.py) and the 1.10 precedence helpers of
orchestrator/results.py."""
from __future__ import annotations

import errno
import sqlite3

import pytest

from sentinel.executor.failures import (TRACE_LIMIT, FailureLedger, Fault, FaultInjector, faults_from, guard,
                                        map_exception)
from sentinel.orchestrator.results import (Outcome, RunVerdict, outranks, rank, rest_of_flow, step_failed,
                                           write_unreached)
from sentinel.orchestrator.watchdog import HeartbeatUnwritable
from sentinel.records import CheckpointOutcome, RunOutcome
from sentinel.store import (ArtifactPersistError, ArtifactStore, Redaction, RedactionRefused, RunNotComplete, RunPlan,
                            ScopeEntry, SecretLeaked, Store, StoreWriteFailed)
from sentinel.store import artifacts as artifacts_mod

RUN = "run-01"
WS = "ws.tier-a.active-trader"
CP1 = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s06"
CP2 = "flow/dialog.order-confirmation/flow.trade.order-ticket.stock.limit/s07"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _plan():
    return RunPlan(run_id=RUN, run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md", budget_sec=2700,
                   scope=(ScopeEntry(CP1, WS, "active_trader", 1, "frozen", flow_class="standard"),
                          ScopeEntry(CP2, WS, "active_trader", 1, "frozen", flow_class="standard")))


def _outcome(cid, o: Outcome):
    return CheckpointOutcome(run_id=RUN, checkpoint_id=cid, outcome=o.outcome, reason=o.reason, gate=o.gate,
                             workspace_id=WS, persona="active_trader", data_mode="frozen")


# -- 8.9 ------------------------------------------------------------------------------------------------------------
def test_an_artifact_read_back_failing_twice_is_artifact_persist_failed_and_the_run_cannot_be_complete(
        tmp_path, monkeypatch):
    calls = []

    def broken(path):
        calls.append(path)
        raise OSError(errno.EIO, "read-back failed")

    monkeypatch.setattr(artifacts_mod, "_read_back", broken)
    arts = ArtifactStore(tmp_path / "shard", "run-01/1")
    ledger = FailureLedger()
    value, r = guard(lambda: arts.put(PNG, name="cap.png", kind="capture", media_type="image/png",
                                      redaction=Redaction("v4")),
                     step_id="s06", checkpoint_id=CP1, ledger=ledger)
    assert value is None and len(calls) == 2                      # 8.4: retried once
    assert r.checkpoint == Outcome("harness_error", "artifact_persist_failed")
    assert not r.has("free_space_check")                          # not ENOSPC
    assert ledger.run_cannot_complete and ledger.persist_failed == [CP1]
    with Store(tmp_path / "run.db") as s:
        s.begin_run(_plan())
        s.write_checkpoint(_outcome(CP1, r.checkpoint))
        s.write_checkpoint(_outcome(CP2, Outcome("identical")))
        assert any("store-failure" in b for b in s.complete_blockers(RUN))
        with pytest.raises(RunNotComplete):
            s.end_run(RunOutcome(RUN, "pr", "complete", "reported"))
    end = ledger.at_teardown(None)
    assert (end.run_status, end.run_reason) == ("aborted_harness", "artifact_persist_failed")
    assert end.unreached == Outcome("harness_error", "artifact_persist_failed")


def test_enospc_on_an_artifact_write_asks_for_the_free_space_check():
    r = map_exception(ArtifactPersistError("full", enospc=True), step_id="s06", checkpoint_id=CP1)
    assert r.checkpoint.reason == "artifact_persist_failed" and r.has("free_space_check")
    assert r.run is None and r.has("resume_next_flow")


def test_the_free_space_check_ending_the_run_first_leaves_disk_full_alone():
    ledger = FailureLedger()
    ledger.note(map_exception(ArtifactPersistError("full", enospc=True), step_id="s06"), CP1)
    assert ledger.at_teardown(RunVerdict("aborted_harness", "disk_full")) is None
    assert ledger.at_teardown(RunVerdict("complete_with_env_errors")) is not None
    assert FailureLedger().at_teardown(None) is None


def test_an_image_without_a_redaction_version_is_redaction_version_missing(tmp_path):
    arts = ArtifactStore(tmp_path / "shard", "run-01/1")
    value, r = guard(lambda: arts.put(PNG, name="cap.png", kind="capture", media_type="image/png"),
                     step_id="s06", checkpoint_id=CP1)
    assert value is None
    assert r.checkpoint == Outcome("harness_error", "redaction_version_missing")
    assert {o.selector: o.outcome for o in r.outcomes} == {
        "in_progress": Outcome("harness_error", "redaction_version_missing"), "rest_of_flow": step_failed("s06")}


def test_a_locked_database_ends_the_shard_store_write_failed(tmp_path):
    db = tmp_path / "run.db"
    with Store(db, busy_timeout_ms=10) as s:
        s.begin_run(_plan())
        other = sqlite3.connect(db, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        try:
            value, r = guard(lambda: s.write_checkpoint(_outcome(CP1, Outcome("identical"))), step_id="s06",
                             checkpoint_id=CP1)
        finally:
            other.execute("ROLLBACK")
            other.close()
    assert value is None
    assert (r.run_status, r.run_reason) == ("aborted_harness", "store_write_failed")
    assert r.unreached == Outcome("harness_error", "store_write_failed") and r.has("teardown")

    class Sink:
        def fill_unreached(self, run_id, outcome, reason=None, *, gate=None, detail=None):
            self.got = (outcome, reason)
            return [CP2]

    sink = Sink()
    assert write_unreached(sink, RUN, r) == [CP2] and sink.got == ("harness_error", "store_write_failed")


def test_a_put_that_refuses_a_secret_ends_the_run_secret_leaked():
    r = map_exception(SecretLeaked("leak", ("login.password",)), step_id="s06")
    assert (r.run_status, r.run_reason) == ("aborted_harness", "secret_leaked")
    assert r.has("withhold_report") and r.has("rotate_credential")
    assert [f.kind for f in r.findings] == ["secret_leaked"]


# -- 10.1 and 10.2 --------------------------------------------------------------------------------------------------
def test_an_unhandled_exception_is_exception_class_with_the_trace_and_the_flow_resumes():
    def evaluate():
        return {}["missing"]

    value, r = guard(evaluate, step_id="s06", checkpoint_id=CP1, last_screenshot="c" * 64,
                     watchdog_event_id="wd-17")
    assert value is None
    assert r.checkpoint == Outcome("harness_error", "exception:KeyError") and r.step == "failed"
    assert {o.selector: o.outcome for o in r.outcomes}["rest_of_flow"] == step_failed("s06")
    assert r.has("resume_next_flow") and r.run is None and r.alert.rule == "harness_error"
    assert "KeyError" in r.evidence["stack_trace"] and "evaluate" in r.evidence["stack_trace"]
    assert r.evidence["last_screenshot"] == "c" * 64 and r.evidence["watchdog_event_id"] == "wd-17"


def test_a_long_stack_trace_is_capped():
    def deep(n):
        if n == 0:
            raise RuntimeError("x" * (TRACE_LIMIT * 2))
        deep(n - 1)

    with pytest.raises(RuntimeError) as info:
        deep(3)
    r = map_exception(info.value, step_id="s1")
    assert len(r.evidence["stack_trace"]) <= TRACE_LIMIT + 10 and "..." in r.evidence["stack_trace"]


def test_an_exception_while_an_environment_failure_stands_is_evidence_not_a_second_outcome():
    env = Outcome("env_error", "gate", "connectivity")
    r = map_exception(ValueError("while handling the drop"), step_id="s06", pending=env)
    assert r.checkpoint == env
    assert r.evidence["superseded"] == "harness_error (exception:ValueError)"
    app = Outcome("assertion_failed", "assertion:app_alive", detail="hang")
    assert map_exception(KeyError("k"), step_id="s06", pending=app).checkpoint == app
    lower = Outcome("not_settled", "frames_unstable")
    assert map_exception(KeyError("k"), step_id="s06", pending=lower).checkpoint.outcome == "harness_error"


def test_heartbeat_failures_map_to_the_heartbeat_rule():
    r = map_exception(HeartbeatUnwritable(errno.EROFS, "ro"), step_id="s1")
    assert (r.run_status, r.run_reason) == ("aborted_harness", "heartbeat_unwritable") and r.has("self_abort")
    r = map_exception(HeartbeatUnwritable(errno.ENOSPC, "full"), step_id="s1")
    assert r.run is None and r.has("free_space_check")


def test_an_enospc_os_error_asks_for_the_free_space_check():
    r = map_exception(OSError(errno.ENOSPC, "No space left on device"), step_id="s1")
    assert r.checkpoint.reason == "exception:OSError" and r.has("free_space_check")
    assert not map_exception(OSError(errno.EIO, "io"), step_id="s1").has("free_space_check")


# -- fault injection ------------------------------------------------------------------------------------------------
def test_faults_fire_at_their_site_and_key_the_planned_number_of_times():
    inj = FaultInjector().add(FaultInjector.artifact_persist(CP1)).add(Fault("evaluate", lambda: KeyError("k"),
                                                                             times=2))
    inj.check("artifact_put", CP2)                                # another checkpoint: nothing
    with pytest.raises(ArtifactPersistError):
        inj.check("artifact_put", CP1)
    inj.check("artifact_put", CP1)                                # spent
    for _ in range(2):
        with pytest.raises(KeyError):
            inj.check("evaluate", CP2)
    inj.check("evaluate", CP2)
    assert inj.fired == [("artifact_put", CP1, "ArtifactPersistError"), ("evaluate", CP2, "KeyError"),
                         ("evaluate", CP2, "KeyError")]


def test_guard_maps_an_injected_fault_and_passes_values_through():
    inj = faults_from([("store_write", CP1, ""), ("redaction_missing", CP2, ""), ("exception:capture", None, "KeyError"),
                       ("artifact_persist", "s9", "enospc")])
    assert guard(lambda: 7, step_id="s1", site="evaluate", faults=inj) == (7, None)
    _, r = guard(lambda: 7, step_id="s06", checkpoint_id=CP1, site="store_write", faults=inj)
    assert r.run_reason == "store_write_failed"
    _, r = guard(lambda: 7, step_id="s07", checkpoint_id=CP2, site="artifact_put", faults=inj)
    assert r.checkpoint.reason == "redaction_version_missing"
    _, r = guard(lambda: 7, step_id="s07", checkpoint_id=CP2, site="capture", faults=inj)
    assert r.checkpoint.reason == "exception:KeyError"
    _, r = guard(lambda: 7, step_id="s9", site="artifact_put", faults=inj)
    assert r.has("free_space_check")


def test_fault_plans_are_checked():
    with pytest.raises(ValueError):
        Fault("nowhere", lambda: KeyError("k"))
    with pytest.raises(ValueError):
        Fault("capture", lambda: KeyError("k"), times=0)
    with pytest.raises(ValueError):
        faults_from([("exception:capture", None, "NotAnException")])
    with pytest.raises(ValueError):
        faults_from([("melt", None, "")])


def test_redaction_refused_is_the_store_class():
    assert issubclass(RedactionRefused, Exception) and issubclass(StoreWriteFailed, Exception)


# -- results.py: the 1.10 precedence ----------------------------------------------------------------------------------
def test_precedence_follows_1_10():
    env = Outcome("env_error", "gate", "connectivity")
    app = Outcome("assertion_failed", "assertion:app_alive", detail="jvm_crash")
    harness = Outcome("harness_error", "hook_lost")
    landmark = Outcome("landmark_missing", "locator:s04")
    assertion = Outcome("assertion_failed", "assertion:qty")
    assert outranks(env, app) and outranks(app, harness) and outranks(harness, step_failed("s1"))
    assert outranks(landmark, assertion) and not outranks(assertion, landmark)
    assert rank(app) == 1 and rank(assertion) == 8
    with pytest.raises(ValueError):
        rank(Outcome("unregistered_surface"))
    assert rest_of_flow("s04").selector == "rest_of_flow" and rest_of_flow("s04").outcome == step_failed("s04")
