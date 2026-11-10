"""The Orchestrator's gate checks and run-outcome rules (5.2 State machine; 12.7; 7.3, 7.6, 7.7).

States in order: `provisioned` -> `launched` -> `authenticated` -> `paper_mode_asserted` ->
`settings_attested` -> `workspace_loaded` -> `fixture_verified` -> `canary_flow_passed` -> `running`
-> `teardown` -> `reported`. Each check here is a pure function of what the rig observed and returns a
`ControlResult` with the exact outcome, reason, gate and 9.10 route of the 5.2 table's "On failure" and
"Who sees" columns. `paper_mode_asserted` is identity.judge_identity and `settings_attested` is
attestation.attest. A gate failure before `running` sets every scoped checkpoint `env_error` with the gate
(reason `gate` when 4.13 has no finer code, 12.7) and the run `complete_with_env_errors`, except where
5.2 names a safety or harness abort.

Choices (reported):

1. Preflight precedence: G1 (`config_invalid`) first, then the H items in checklist order, then the R, B,
   G2 to G4, N1 and N6 items (the first in checklist order names the gate, the rest go in `detail`). N3,
   N4 and G5 apply to their subsets and the run continues; N2 and N5 are the degraded mode; N7 proceeds.
   A G5 failure keeps capturing (12.7 "captures still collected"; 5.2 says "without capturing").
2. A missing main frame at a relaunch (not the shard's first launch) is a `hang` app failure charged to
   the relaunch budget, never `env_error` (10.2 sampling 3).
3. The `authenticated` checks run in the LOGIN flow's order (7.7): mode switch, Live still selected,
   remember-password, vault lease, MFA challenge, authentication error, session kicked, main-window
   timeout, post-login anchor. The accounts owner is added to the route whenever the login itself is the
   cause (Live selected, challenge, authentication error, session kicked; 7.7); an authentication error
   pages them.
4. `fixture_verified`: a working order in either account aborts at once; golden drift (or a passed
   `reseed_due`) and empty-account drift after the one reset give `env_error` on the `account_state`
   checkpoints and the run continues.
5. Run reasons (5.2, 1.10): a `complete_with_env_errors` run carries `unproven_proof`, `relogin_budget` or
   `teardown_fault` only when no checkpoint is `env_error`; with several, that order.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from . import alerts
from .budgets import ShardBudgets
from .results import (ControlResult, FindingSpec, Outcome, RunVerdict, SubsetOutcome, harness_abort_outcome,
                      safety_abort_outcome, worse)
from .rig import (AccountState, CanaryFlowObservation, CrashSignal, LaunchObservation, LoginObservation,
                  PreflightReport, TeardownObservation, WorkspaceObservation)
from .watchdog import app_failure, dead_man_finalize

GATES = ("provisioned", "launched", "authenticated", "paper_mode_asserted", "settings_attested", "workspace_loaded",
         "fixture_verified", "canary_flow_passed", "running", "teardown", "reported")
PREFLIGHT_ITEMS = tuple([f"R{i}" for i in range(1, 8)] + [f"B{i}" for i in range(1, 4)]
                        + [f"H{i}" for i in range(1, 11)] + [f"N{i}" for i in range(1, 8)]
                        + [f"G{i}" for i in range(1, 6)])
ENV_ITEMS = tuple(i for i in PREFLIGHT_ITEMS if i[0] in "RB" or i in ("G2", "G3", "G4", "N1", "N6"))
LAUNCH_WAIT_S = 300.0
LOGIN_WAIT_S = 120.0


def _env_fail(control: str, gate: str, reason: str = "gate", *, detail: str | None = None,
              alert: alerts.AlertRoute | None = None, **kw) -> ControlResult:
    return ControlResult(control, False, gate, run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", reason, gate, detail), alert=alert or alerts.env_error(),
                         detail=detail, **kw)


# -- provisioned ----------------------------------------------------------------------------------------
def check_provisioned(report: PreflightReport, *, calibration_record: bool = True,
                      repeat_gates: frozenset[str] = frozenset()) -> ControlResult:
    """Gate `provisioned`: every 12.7 item passes (nothing skipped, nothing passes by default)."""
    missing = [i for i in PREFLIGHT_ITEMS if i not in report.items]
    if missing:
        raise ValueError(f"preflight items missing from the report: {missing}")
    failed = [i for i in PREFLIGHT_ITEMS if not report.items[i]]
    ev = {"failed": failed, "detail": dict(report.detail)}
    if "G1" in failed:
        run, un = harness_abort_outcome("config_invalid")
        return ControlResult("preflight", False, "provisioned", run=run, unreached=un, alert=alerts.aborted_harness(),
                             detail="sentinel lint error (G1)", evidence=ev)
    h = [i for i in failed if i.startswith("H")]
    if h:
        control = report.detail.get(h[0])
        run, un = harness_abort_outcome(f"preflight.{h[0]}", detail=control)
        return ControlResult("preflight", False, "provisioned", run=run, unreached=un, alert=alerts.aborted_harness(),
                             findings=(FindingSpec("hardening", f"{control} blocked preflight {h[0]}"),) if control else (),
                             detail="harness self-test failed before any product action: " + ", ".join(h), evidence=ev)
    env = [i for i in failed if i in ENV_ITEMS]
    if env:
        gate = f"preflight.{env[0]}"
        return _env_fail("preflight", gate, detail="failed: " + ", ".join(env), evidence=ev,
                         alert=alerts.env_error(repeat=gate in repeat_gates))
    subsets: list[SubsetOutcome] = []
    notes: list[str] = []
    acts: list[str] = []
    alert = None
    if "N3" in failed:
        subsets.append(SubsetOutcome("api_within", Outcome("env_error", "gate", "preflight.N3")))
    if "N4" in failed:
        subsets.append(SubsetOutcome("frozen", Outcome("env_error", "gate", "preflight.N4")))
    if "G5" in failed:
        subsets.append(SubsetOutcome("all", Outcome("not_comparable", "fingerprint_mismatch",
                                                    detail=report.detail.get("G5"))))
    if "N2" in failed or "N5" in failed:
        notes.append("degraded: review deferred, agent fallback and the Explorer disabled")
        alert = alerts.model_degraded()
    if "N7" in failed:
        notes.append("integrations unreachable: publication retries under the dead-man deadline")
    if not calibration_record:
        acts.append("queue_calibration")
        notes.append("no calibration for this fingerprint: threshold 0, above-zero diffs needs_human (uncalibrated)")
    if subsets and alert is None:
        alert = alerts.env_error()
    return ControlResult("preflight", True, "provisioned", outcomes=tuple(subsets), alert=alert, actions=tuple(acts),
                         detail="; ".join(notes) or None, evidence=ev)


# -- launched -------------------------------------------------------------------------------------------
def check_launched(obs: LaunchObservation, *, expected_build: str, first_launch: bool,
                   budgets: ShardBudgets) -> ControlResult:
    """Gate `launched`: a visible main frame whose title build id equals the build under test, within 5 minutes."""
    phase = "first_launch" if first_launch else "relaunch_gates"
    if obs.crash is not None:
        return app_failure(obs.crash, budgets, phase=phase)
    ev = {"windows": [w.title for w in obs.windows], "processes": list(obs.processes),
          "screenshot": obs.screenshot_sha256}
    if obs.update_progress:
        return _env_fail("launch", "launched", "version_mismatch", detail="launcher update progress (POP-01)",
                         evidence=ev)
    if not obs.frame_found or obs.waited_s > LAUNCH_WAIT_S:
        if not first_launch:
            return app_failure(CrashSignal("hang", "main_frame_missing"), budgets, phase=phase)
        return _env_fail("launch", "launched", detail="no main frame within 5 minutes", evidence=ev)
    build = obs.title_build_id if obs.title_build_id is not None else obs.about_build_id
    if build != expected_build:
        reason = "version_mismatch" if first_launch else "version_changed_mid_run"
        return _env_fail("launch", "launched", reason, detail=f"build {build!r}, expected {expected_build!r}",
                         evidence=ev)
    return ControlResult("launch", True, "launched", evidence={**ev, "build_id": build,
                                                               "source": "title" if obs.title_build_id else "about"})


def check_build_unchanged(title_build_id: str | None, expected_build: str) -> ControlResult:
    """The build id re-read before each flow (5.2): a change is `env_error (gate: launched, reason:
    version_changed_mid_run)` for the rest of the shard."""
    if title_build_id == expected_build:
        return ControlResult("build_reread", True, "launched")
    return _env_fail("build_reread", "launched", "version_changed_mid_run",
                     detail=f"build {title_build_id!r}, expected {expected_build!r}")


# -- authenticated --------------------------------------------------------------------------------------
def check_authenticated(obs: LoginObservation) -> ControlResult:
    """Gate `authenticated`: one LOGIN attempt per run, never retried, never handed to agent mode (7.7)."""
    login_owner = ("login_owner",)
    login_cause = login_owner + ("accounts_owner",)
    checks: list[tuple[bool, str, str, tuple[str, ...]]] = [
        (not obs.mode_switch_located, "gate", "the paperMoney / Live switch could not be located", login_owner),
        (obs.live_selected, "live_mode_selected", "Live still selected after one click on paperMoney", login_cause),
        (not obs.remember_password_off, "gate", "remember-password not verified off before submit", login_owner),
        (not obs.vault_lease, "vault_unreachable", "no vault lease at typing time", login_owner),
        (obs.challenge, "authentication_challenge", "authentication challenge", login_cause),
        (obs.auth_error, "authentication_failed", "authentication error; the rig is held", login_cause),
        (obs.session_kicked, "session_kicked", "logged in from another location (POP-04)", login_cause),
        (obs.main_window_s is None or obs.main_window_s > LOGIN_WAIT_S, "timeout",
         "main window absent within the login max wait", login_owner),
        (not (obs.login_dialog_gone and obs.post_login_anchor), "gate", "post-login anchor missing", login_owner),
    ]
    for failed, reason, detail, notified in checks:
        if failed:
            route = alerts.env_error(notified=notified)
            if reason == "authentication_failed":
                route = route.plus(paged=("accounts_owner",))
            return _env_fail("login", "authenticated", reason, detail=detail, alert=route,
                             evidence={"screenshot": obs.screenshot_sha256})
    return ControlResult("login", True, "authenticated", evidence={"screenshot": obs.screenshot_sha256})


# -- workspace_loaded -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class WorkspaceExpectation:
    """The corpus entry of the work item (4.6): file hash, tab set and top-level window count."""

    sha256: str
    tabs: tuple[str, ...]
    window_count: int


def check_workspace(obs: WorkspaceObservation, expected: WorkspaceExpectation) -> ControlResult:
    """Gate `workspace_loaded`; an unlocated registry probe is `landmark_missing (reason: probe:<Pn>)` and the run
    continues with the registry as truth there (2.2.3)."""
    if obs.file_sha256 != expected.sha256 or not obs.anchor_present or tuple(obs.tabs) != tuple(expected.tabs) \
            or obs.window_count != expected.window_count:
        return _env_fail("workspace", "workspace_loaded", detail="workspace hash, anchor, tab set or window count")
    probes = tuple(SubsetOutcome(f"probe:{p}", Outcome("landmark_missing", f"probe:{p}")) for p in obs.probes_unlocated)
    return ControlResult("workspace", True, "workspace_loaded", outcomes=probes)


# -- fixture_verified -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class AccountExpectation:
    """One fixture account (4.9, 7.6): role, positions by symbol, cash and its tolerance."""

    role: str
    positions: Mapping[str, float]
    cash: float
    cash_tolerance: float


def _drift(a: AccountState, e: AccountExpectation) -> bool:
    return dict(a.positions) != dict(e.positions) or abs(a.cash - e.cash) > e.cash_tolerance


def check_fixture(first: Sequence[AccountState], expected: Mapping[str, AccountExpectation], *,
                  after_reset: Sequence[AccountState] | None = None, reseed_due_passed: bool = False) -> ControlResult:
    """Gate `fixture_verified` (7.6 stage 2): working orders exactly zero, positions exact, cash within tolerance.

    Call it with the first `verify_account_fixture`; when it asks for `reset_empty_account`, reset and call it
    again with the re-verification as `after_reset`.
    """
    reads = list(first) + list(after_reset or ())
    if any(a.working_orders > 0 for a in reads):
        run, un = safety_abort_outcome("fixture_verified", "unexpected_working_order")
        return ControlResult("fixture", False, "fixture_verified", run=run, unreached=un, alert=alerts.safety_abort(),
                             actions=("kill_tree", "kill_switch"), incident="S1",
                             detail="working orders present; a sent order is never fixture drift")
    by_role = {a.role: a for a in first}
    subsets: list[SubsetOutcome] = []
    acts: list[str] = []
    golden = by_role.get("golden")
    if golden is not None:
        if _drift(golden, expected[golden.account_id]):
            subsets.append(SubsetOutcome("account_state:golden", Outcome("env_error", "fixture_drift", "fixture_verified")))
            acts.append("maintenance_task")
        elif reseed_due_passed:
            subsets.append(SubsetOutcome("account_state:golden",
                                         Outcome("env_error", "fixture_maintenance_needed", "fixture_verified")))
            acts.append("maintenance_task")
    empty = by_role.get("empty")
    if empty is not None and _drift(empty, expected[empty.account_id]):
        if after_reset is None:
            return ControlResult("fixture", False, "fixture_verified", actions=("reset_empty_account",),
                                 detail="EMPTY account drift: reset once, then re-verify")
        again = {a.role: a for a in after_reset}.get("empty")
        if again is None or _drift(again, expected[empty.account_id]):
            subsets.append(SubsetOutcome("account_state", Outcome("env_error", "fixture_drift", "fixture_verified")))
    if subsets:
        return ControlResult("fixture", False, "fixture_verified", outcomes=tuple(subsets),
                             alert=alerts.env_error(notified=("accounts_owner",) if acts else ()), actions=tuple(acts),
                             detail="fixture drift on account_state checkpoints; the rest of the run proceeds")
    return ControlResult("fixture", True, "fixture_verified")


# -- canary_flow_passed ---------------------------------------------------------------------------------
def check_canary_flow(obs: CanaryFlowObservation) -> ControlResult:
    """Gate `canary_flow_passed` (5.2): rung 1 or 2, settled within floor, the calibration panel read exactly."""
    if obs.locator_rung not in (1, 2) or not obs.settled or not obs.within_floor or obs.zoom_scale_ok is False:
        return _env_fail("canary_flow", "canary_flow_passed", detail="smoke flow failed")
    if not obs.calibration_exact:
        return ControlResult("canary_flow", True, "canary_flow_passed",
                             outcomes=(SubsetOutcome("ocr_assertion", Outcome("harness_error", "ocr_calibration")),),
                             alert=alerts.harness_error(), detail="OCR calibration panel miss: OCR assertions only")
    return ControlResult("canary_flow", True, "canary_flow_passed")


# -- teardown and reported ------------------------------------------------------------------------------
def check_teardown(obs: TeardownObservation) -> ControlResult:
    """Gate `teardown` apart from the Monitor diff, which is the post-run proof (killswitch.post_run_proof)."""
    if obs.secret_found:
        run, un = harness_abort_outcome("secret_leaked")
        return ControlResult("teardown", False, "teardown", run=run, unreached=un, alert=alerts.safety_abort(),
                             actions=("withhold_report", "rotate_credential"), incident="S1",
                             detail="plaintext secret found: report withheld, objects quarantined")
    if not obs.secret_scan_ran:
        run, un = harness_abort_outcome("secret_scan_unavailable")
        return ControlResult("teardown", False, "teardown", run=run, unreached=un, alert=alerts.aborted_harness(),
                             actions=("withhold_report",), detail="report withheld until a human runs the scan")
    faults = [n for n, ok in (("reset_input_state", obs.reset_input_state_ok), ("logout", obs.logout_ok),
                              ("log collection", obs.logs_collected)) if not ok]
    if faults and obs.cancel_all_verified:
        return ControlResult("teardown", False, "teardown", run=RunVerdict("complete_with_env_errors", "teardown_fault"),
                             alert=alerts.env_error(), detail="teardown fault: " + ", ".join(faults))
    return ControlResult("teardown", obs.cancel_all_verified, "teardown",
                         detail=None if obs.cancel_all_verified else "Cancel All not verified: the proof decides")


def check_reported(acked: bool, *, now: float, deadline: float, run_type: str) -> ControlResult:
    """Gate `reported`: the control host acknowledges the run id and every artifact hash; retry with backoff until
    the deadline, then the dead-man (`aborted_harness (reason: dead_man)`, 5.14)."""
    if acked:
        return ControlResult("report", True, "reported")
    if now < deadline:
        return ControlResult("report", False, "reported", detail="not acknowledged: retry with backoff")
    return dead_man_finalize("reported", True, run_type=run_type)


# -- run outcome ----------------------------------------------------------------------------------------
CWEE_REASONS = ("unproven_proof", "relogin_budget", "teardown_fault")


def decide_run_outcome(verdicts: Iterable[RunVerdict | None], *, any_env_error: bool,
                       any_fingerprint_mismatch: bool = False, scope_finished: bool = True) -> RunVerdict:
    """The run outcome rules of 5.2 and 1.10 over every verdict the run's controls set."""
    vs = [v for v in verdicts if v is not None]
    worst = None
    for v in vs:
        worst = worse(worst, v)
    if worst is None:
        if any_env_error or any_fingerprint_mismatch:
            return RunVerdict("complete_with_env_errors")
        if not scope_finished:
            raise ValueError("an unfinished scope needs an abort or a budget outcome (5.2)")
        return RunVerdict("complete")
    if worst.status == "complete_with_env_errors":
        if any_env_error:
            return RunVerdict("complete_with_env_errors")
        reasons = {v.reason for v in vs if v.status == "complete_with_env_errors"}
        return RunVerdict("complete_with_env_errors", next((r for r in CWEE_REASONS if r in reasons), None))
    return worst
