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
6. H3 (7.1, 12.7): the harness's DPI awareness is passed in as `dpi_awareness` (the logged
   `GetProcessDpiAwareness`, `ports.DesktopPort.process_dpi_awareness`); any value other than
   `PER_MONITOR_AWARE_V2` makes H3 a failed item whose outcome is `aborted_harness (reason: dpi_awareness)`, ahead
   of `preflight.H3`, with the H items still taken in checklist order.
7. G5 (10.8, 12.7, SD-78): the rig names G5's cause in `report.detail["G5"]`: `no_calibration` (the rigs are equal
   and only the Calibration for their rendering key is missing) queues a Calibration and alerts the on-call
   channel, with no `not_comparable` subset (the Diff Engine's uncalibrated path gives `needs_human (reason:
   uncalibrated)`); anything else is the differing field list, optionally after `fingerprint_mismatch:`, and every
   checkpoint is `not_comparable (reason: fingerprint_mismatch)` with those fields in `detail`. A failed G5 that
   names neither is refused (`ValueError`): SD-78 requires the fields. `calibration_record=False` with G5 passed
   is the same no-Calibration case, alerted the same way ("either way on-call is alerted", 10.7).
8. Build id (7.3): both reads missing (the title regex did not match and the About OCR did not either) is
   `env_error (gate: launched, reason: fingerprint_unreadable:build_id)`. A `version_mismatch` asks for
   `disable_rig`, the scheduler's hold ("nothing else runs on that rig until a human looks"); the POP-01 update
   progress is that case too. `check_build_unchanged` takes the About read once when the title misses, and a
   change carries both ids and the launcher log hash in `evidence` for the on-call channel.
9. Login (7.7, SD-62): `agreement_pending` (POP-03, a `ports.LoginObservation` field) is checked after the session
   checks, in the popup step 5, and notifies the accounts owner. `authentication_failed` and
   `authentication_challenge` page the login owner and the accounts owner; `authentication_failed` also asks for
   `block_login`, the hold the accounts owner clears; `vault_unreachable` pages the system owner. `repeat` (the
   `authenticated` gate failed in the login's previous run, 9.10) makes the route P1. The Branch B trusted-device
   prompt is decided by `mfa_recovery` in the MFA position: the accounts owner's code inside the window (default
   600 s), once, passes; else `authentication_challenge`. Under Branch A any prompt is the challenge.
10. Workspace (7.5, SD-77): `WorkspaceExpectation.windows` lists the corpus entry's windows (title regex and DWM
    rect, the main frame first); when it is non-empty the caller passes the enumerated windows
    (`ports.WindowInfo`, with `rect`) and each expected window must match one window, title by `re.search` and
    every rect edge within 4 px, with no window left over. A window without a readable rect matches nothing.
11. Fixture accounts (7.6 stage 2): buying power follows the expectation's bounds when the read carries it
    (`ports.AccountState.buying_power`; a read of that type with no value is drift, since nothing passes by
    default); a read of the base `rig.AccountState` has no buying power cell and is compared without it. Under
    `paper_send_ok` working rows that all carry the run's own tag ask for `cancel_all` (the verified Cancel All)
    and a re-verification passed as `after_cancel=True`; a foreign row, or any row left after that cancel, aborts.
    An `OnDemand` toolbar seen after login is `check_ondemand` (7.7 step 8, 7.9).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import alerts
from .budgets import ShardBudgets
from .results import (ControlResult, FindingSpec, Outcome, RunVerdict, SubsetOutcome, harness_abort_outcome,
                      safety_abort_outcome, worse)
from .rig import (AccountState, CanaryFlowObservation, CrashSignal, LaunchObservation, LoginObservation,
                  PreflightReport, TeardownObservation, WindowInfo, WorkspaceObservation)
from .watchdog import app_failure, dead_man_finalize

GATES = ("provisioned", "launched", "authenticated", "paper_mode_asserted", "settings_attested", "workspace_loaded",
         "fixture_verified", "canary_flow_passed", "running", "teardown", "reported")
PREFLIGHT_ITEMS = tuple([f"R{i}" for i in range(1, 8)] + [f"B{i}" for i in range(1, 4)]
                        + [f"H{i}" for i in range(1, 11)] + [f"N{i}" for i in range(1, 8)]
                        + [f"G{i}" for i in range(1, 6)])
ENV_ITEMS = tuple(i for i in PREFLIGHT_ITEMS if i[0] in "RB" or i in ("G2", "G3", "G4", "N1", "N6"))
LAUNCH_WAIT_S = 300.0
LOGIN_WAIT_S = 120.0
EXPECTED_DPI_AWARENESS = "PER_MONITOR_AWARE_V2"     # 7.1 Harness DPI awareness row
NO_CALIBRATION = "no_calibration"                   # G5's cause when only the Calibration is missing (choice 7)
BUILD_UNREADABLE = "fingerprint_unreadable:build_id"
MFA_WINDOW_S = 600.0                                # 7.7 step 4 Branch B: the configured window, default 10 minutes
TRUSTED_DEVICE_CHECKPOINT = "trusted_device_prompt"  # 7.7 step 4 Branch B: the prompt is captured as this checkpoint
RECT_TOLERANCE_PX = 4                               # 7.5, SD-77


def _env_fail(control: str, gate: str, reason: str = "gate", *, detail: str | None = None,
              alert: alerts.AlertRoute | None = None, **kw) -> ControlResult:
    return ControlResult(control, False, gate, run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", reason, gate, detail), alert=alert or alerts.env_error(),
                         detail=detail, **kw)


# -- provisioned ----------------------------------------------------------------------------------------
def _g5_fields(detail: str | None) -> str:
    """The differing fields G5 names (SD-78: never empty), with an optional `fingerprint_mismatch:` prefix."""
    fields = (detail or "").removeprefix("fingerprint_mismatch:").strip()
    if not fields:
        raise ValueError("G5 failed without naming the differing fields or no_calibration (12.7, SD-78)")
    return fields


def check_provisioned(report: PreflightReport, *, calibration_record: bool = True,
                      repeat_gates: frozenset[str] = frozenset(), dpi_awareness: str | None = None) -> ControlResult:
    """Gate `provisioned`: every 12.7 item passes (nothing skipped, nothing passes by default). `dpi_awareness` is the
    awareness H3 logged (choice 6); G5's cause is `report.detail["G5"]` (choice 7)."""
    missing = [i for i in PREFLIGHT_ITEMS if i not in report.items]
    if missing:
        raise ValueError(f"preflight items missing from the report: {missing}")
    wrong_dpi = dpi_awareness is not None and dpi_awareness != EXPECTED_DPI_AWARENESS
    failed = [i for i in PREFLIGHT_ITEMS if not report.items[i] or (i == "H3" and wrong_dpi)]
    ev = {"failed": failed, "detail": dict(report.detail), "dpi_awareness": dpi_awareness}
    if "G1" in failed:
        run, un = harness_abort_outcome("config_invalid")
        return ControlResult("preflight", False, "provisioned", run=run, unreached=un, alert=alerts.aborted_harness(),
                             detail="sentinel lint error (G1)", evidence=ev)
    h = [i for i in failed if i.startswith("H")]
    if h and h[0] == "H3" and wrong_dpi:
        run, un = harness_abort_outcome("dpi_awareness", detail=f"GetProcessDpiAwareness {dpi_awareness}")
        return ControlResult("preflight", False, "provisioned", run=run, unreached=un, alert=alerts.aborted_harness(),
                             detail=f"harness DPI awareness {dpi_awareness}, expected {EXPECTED_DPI_AWARENESS} (H3)",
                             evidence=ev)
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
    uncalibrated = not calibration_record
    if "G5" in failed:
        if (report.detail.get("G5") or "").strip() == NO_CALIBRATION:
            uncalibrated = True
        else:
            subsets.append(SubsetOutcome("all", Outcome("not_comparable", "fingerprint_mismatch",
                                                        detail=_g5_fields(report.detail.get("G5")))))
    if "N2" in failed or "N5" in failed:
        notes.append("degraded: review deferred, agent fallback and the Explorer disabled")
        alert = alerts.model_degraded()
    if "N7" in failed:
        notes.append("integrations unreachable: publication retries under the dead-man deadline")
    if uncalibrated:
        acts.append("queue_calibration")
        notes.append("no calibration for this fingerprint: threshold 0, above-zero diffs needs_human (uncalibrated)")
    if (subsets or uncalibrated) and alert is None:
        alert = alerts.env_error()
    return ControlResult("preflight", True, "provisioned", outcomes=tuple(subsets), alert=alert, actions=tuple(acts),
                         detail="; ".join(notes) or None, evidence=ev)


# -- launched -------------------------------------------------------------------------------------------
def check_launched(obs: LaunchObservation, *, expected_build: str, first_launch: bool,
                   budgets: ShardBudgets, launcher_log_sha256: str | None = None) -> ControlResult:
    """Gate `launched`: a visible main frame whose title build id equals the build under test, within 5 minutes; the
    About read stands in once when the title regex did not match (7.3, choice 8)."""
    phase = "first_launch" if first_launch else "relaunch_gates"
    if obs.crash is not None:
        return app_failure(obs.crash, budgets, phase=phase)
    ev = {"windows": [w.title for w in obs.windows], "processes": list(obs.processes),
          "screenshot": obs.screenshot_sha256}
    if obs.update_progress:
        return _env_fail("launch", "launched", "version_mismatch", detail="launcher update progress (POP-01)",
                         evidence=ev, actions=("disable_rig",))
    if not obs.frame_found or obs.waited_s > LAUNCH_WAIT_S:
        if not first_launch:
            return app_failure(CrashSignal("hang", "main_frame_missing"), budgets, phase=phase)
        return _env_fail("launch", "launched", detail="no main frame within 5 minutes", evidence=ev)
    if obs.title_build_id is None and obs.about_build_id is None:
        return _env_fail("launch", "launched", BUILD_UNREADABLE,
                         detail="neither the title regex nor the Help > About OCR matched (7.3)",
                         evidence={**ev, "launcher_log": launcher_log_sha256})
    build = obs.title_build_id if obs.title_build_id is not None else obs.about_build_id
    if build != expected_build:
        ids = {**ev, "build_id": build, "expected_build": expected_build, "launcher_log": launcher_log_sha256}
        if first_launch:
            return _env_fail("launch", "launched", "version_mismatch", evidence=ids, actions=("disable_rig",),
                             detail=f"build {build!r}, expected {expected_build!r}; the rig is held until a "
                                    "human looks")
        return _env_fail("launch", "launched", "version_changed_mid_run", evidence=ids,
                         detail=f"build {build!r}, expected {expected_build!r}")
    return ControlResult("launch", True, "launched", evidence={**ev, "build_id": build,
                                                               "source": "title" if obs.title_build_id else "about"})


def check_build_unchanged(title_build_id: str | None, expected_build: str, *, about_build_id: str | None = None,
                          launcher_log_sha256: str | None = None) -> ControlResult:
    """The build id re-read before each flow (5.2, 7.3): the title, or the About read taken once when the title misses.
    A change is `env_error (gate: launched, reason: version_changed_mid_run)` for the rest of the shard, with both ids
    and the launcher log for the on-call channel; both reads missing is `fingerprint_unreadable:build_id`."""
    build = title_build_id if title_build_id is not None else about_build_id
    if build == expected_build:
        return ControlResult("build_reread", True, "launched")
    if build is None:
        return _env_fail("build_reread", "launched", BUILD_UNREADABLE,
                         detail="neither the title regex nor the Help > About OCR matched (7.3)",
                         evidence={"launcher_log": launcher_log_sha256})
    return _env_fail("build_reread", "launched", "version_changed_mid_run",
                     detail=f"build {build!r}, expected {expected_build!r}",
                     evidence={"build_id": build, "expected_build": expected_build,
                               "launcher_log": launcher_log_sha256})


# -- authenticated --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MfaPrompt:
    """A two-step prompt seen after the credentials were submitted (7.7 step 4): when it appeared, when the accounts
    owner's code was entered through the hypervisor console (None: not entered), and how many prompts the login saw."""

    seen_mono: float
    code_entered_mono: float | None = None
    prompts: int = 1


def mfa_recovery(prompt: MfaPrompt, *, branch: str, window_s: float = MFA_WINDOW_S,
                 repeat: bool = False) -> ControlResult:
    """The MFA branch of the LOGIN flow (7.7 step 4, DR-03). Branch A expects no prompt: any is
    `authentication_challenge`. Branch B (trusted device) runs the recovery once: the prompt is captured as the
    `trusted_device_prompt` checkpoint and posted to the accounts owner, and a code entered inside the window passes;
    no code inside the window, or a second prompt, is `authentication_challenge`."""
    if branch not in ("A", "B"):
        raise ValueError(f"login_mfa branch {branch!r}; A or B (phase0/branches.yaml)")
    ev = {"checkpoint": TRUSTED_DEVICE_CHECKPOINT, "branch": branch, "prompts": prompt.prompts}
    waited = None if prompt.code_entered_mono is None else prompt.code_entered_mono - prompt.seen_mono
    if branch == "B" and prompt.prompts == 1 and waited is not None and 0 <= waited <= window_s:
        return ControlResult("mfa_recovery", True, "authenticated", evidence={**ev, "waited_s": waited},
                             detail=f"trusted-device prompt answered by the accounts owner after {waited:.0f} s")
    if branch == "A":
        detail = "a challenge screen under Branch A (POP-21)"
    elif prompt.prompts > 1:
        detail = "a second trusted-device prompt: the recovery runs once"
    else:
        detail = f"no code inside the {window_s:.0f} s window"
    return _env_fail("login", "authenticated", "authentication_challenge", detail=detail, evidence=ev,
                     alert=alerts.env_error(repeat=repeat, notified=("login_owner", "accounts_owner")).plus(
                         paged=("login_owner", "accounts_owner")))


def check_authenticated(obs: LoginObservation, *, repeat: bool = False, mfa_branch: str = "A",
                        prompt: MfaPrompt | None = None, mfa_window_s: float = MFA_WINDOW_S) -> ControlResult:
    """Gate `authenticated`: one LOGIN attempt per run, never retried, never handed to agent mode (7.7). `repeat` is
    True when the `authenticated` gate failed in the login's previous run (9.10, P1); `prompt` is a two-step prompt
    seen at step 4, decided by `mfa_recovery` under `mfa_branch` (choice 9)."""
    login_owner = ("login_owner",)
    login_cause = login_owner + ("accounts_owner",)
    shot = {"screenshot": obs.screenshot_sha256}
    checks: list[tuple[bool, str, str, tuple[str, ...]]] = [
        (not obs.mode_switch_located, "gate", "the paperMoney / Live switch could not be located", login_owner),
        (obs.live_selected, "live_mode_selected", "Live still selected after one click on paperMoney", login_cause),
        (not obs.remember_password_off, "gate", "remember-password not verified off before submit", login_owner),
        (not obs.vault_lease, "vault_unreachable", "no vault lease at typing time; nothing typed", login_owner),
        (prompt is not None, "mfa", "", login_cause),
        (obs.challenge, "authentication_challenge", "authentication challenge (POP-21)", login_cause),
        (obs.auth_error, "authentication_failed", "authentication error; the login is held until the accounts "
                                                  "owner clears it", login_cause),
        (obs.session_kicked, "session_kicked", "logged in from another location (POP-04)", login_cause),
        (bool(getattr(obs, "agreement_pending", False)), "agreement_pending",
         "an agreement prompt (POP-03): accepted only by the accounts owner at the console", login_cause),
        (obs.main_window_s is None or obs.main_window_s > LOGIN_WAIT_S, "timeout",
         "main window absent within the login max wait", login_owner),
        (not (obs.login_dialog_gone and obs.post_login_anchor), "gate", "post-login anchor missing", login_owner),
    ]
    for failed, reason, detail, notified in checks:
        if not failed:
            continue
        if reason == "mfa" and prompt is not None:
            r = mfa_recovery(prompt, branch=mfa_branch, window_s=mfa_window_s, repeat=repeat)
            if r.passed:
                continue
            return r
        route = alerts.env_error(repeat=repeat, notified=notified)
        acts: tuple[str, ...] = ()
        if reason in ("authentication_failed", "authentication_challenge"):
            route = route.plus(paged=login_cause)              # SD-62
        elif reason == "vault_unreachable":
            route = route.plus(paged=("system_owner",))       # SD-62, 6.9
        if reason == "authentication_failed":
            acts = ("block_login",)                            # a lockout blocks every later run on the rig (7.7)
        return _env_fail("login", "authenticated", reason, detail=detail, alert=route, actions=acts, evidence=shot)
    return ControlResult("login", True, "authenticated", evidence=shot)


# -- workspace_loaded -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class WindowExpectation:
    """One top-level window of a corpus entry (4.6 `windows`, SD-77): its title regex and DWM rect x, y, w, h."""

    title_regex: str
    rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class WorkspaceExpectation:
    """The corpus entry of the work item (4.6): file hash, tab set, top-level window count and, when the entry lists
    them, its windows (SD-77)."""

    sha256: str
    tabs: tuple[str, ...]
    window_count: int
    windows: tuple[WindowExpectation, ...] = ()

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> "WorkspaceExpectation":
        """From a 4.6 Workspace Corpus entry: `file_sha256` without its prefix, `tabs` and `windows`."""
        wins = tuple(WindowExpectation(str(w["title_regex"]), (int(w["rect"]["x"]), int(w["rect"]["y"]),
                                                                int(w["rect"]["w"]), int(w["rect"]["h"])))
                     for w in entry.get("windows") or ())
        return cls(str(entry["file_sha256"]).removeprefix("sha256:"), tuple(entry.get("tabs") or ()), len(wins), wins)


def _window_matches(w: WindowInfo, e: WindowExpectation) -> bool:
    rect = getattr(w, "rect", None)
    return rect is not None and re.search(e.title_regex, w.title) is not None \
        and all(abs(a - b) <= RECT_TOLERANCE_PX for a, b in zip(rect, e.rect))


def _window_misses(windows: Sequence[WindowInfo], expected: Sequence[WindowExpectation]) -> list[str]:
    """The corpus windows no enumerated window matches, then the enumerated windows left over (choice 10)."""
    left = list(windows)
    misses = []
    for e in expected:
        hit = next((w for w in left if _window_matches(w, e)), None)
        if hit is None:
            misses.append(f"no window {e.title_regex!r} at {e.rect} within {RECT_TOLERANCE_PX} px")
        else:
            left.remove(hit)
    return misses + [f"extra window {w.title!r} at {getattr(w, 'rect', None)}" for w in left]


def check_workspace(obs: WorkspaceObservation, expected: WorkspaceExpectation, *,
                    windows: Sequence[WindowInfo] | None = None) -> ControlResult:
    """Gate `workspace_loaded`: file hash, anchor, tab set and the top-level windows (title regex, DWM rect within
    4 px; 7.5, SD-77). The registry probes run later, at entry to `running` (SD-15, `validation`): an unlocated probe
    anchor is that probe's own `landmark_missing (reason: probe:<Pn>)`, not this gate's."""
    problems = [what for bad, what in ((obs.file_sha256 != expected.sha256, "workspace hash"),
                                       (not obs.anchor_present, "workspace anchor"),
                                       (tuple(obs.tabs) != tuple(expected.tabs), f"tab set {list(obs.tabs)}"),
                                       (obs.window_count != expected.window_count, f"{obs.window_count} windows"))
                if bad]
    if expected.windows:
        if windows is None:
            raise ValueError("the corpus entry lists windows: pass the enumerated windows with their rects (7.5)")
        problems += _window_misses(windows, expected.windows)
    if problems:
        return _env_fail("workspace", "workspace_loaded", detail="; ".join(problems))
    return ControlResult("workspace", True, "workspace_loaded")


# -- fixture_verified -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class AccountExpectation:
    """One fixture account (4.9, 7.6): role, positions by symbol, cash and its tolerance, and the buying power bounds
    of the rule (choice 11; None: no bound)."""

    role: str
    positions: Mapping[str, float]
    cash: float
    cash_tolerance: float
    buying_power_min: float | None = None
    buying_power_max: float | None = None


def drift_of(a: AccountState, e: AccountExpectation) -> list[str]:
    """What differs between one `verify_account_fixture` read and its fixture account (7.6 stage 2 step 1): positions
    exact on symbol and quantity, cash within `cash_tolerance`, buying power by rule; [] when nothing does."""
    out = []
    if dict(a.positions) != dict(e.positions):
        out.append(f"positions {dict(a.positions)} != {dict(e.positions)}")
    if abs(a.cash - e.cash) > e.cash_tolerance:
        out.append(f"cash {a.cash:.2f} outside {e.cash:.2f} +/- {e.cash_tolerance:.2f}")
    if hasattr(a, "buying_power") and (e.buying_power_min is not None or e.buying_power_max is not None):
        bp = getattr(a, "buying_power")
        if bp is None:
            out.append("buying power unread")
        elif (e.buying_power_min is not None and bp < e.buying_power_min) or \
                (e.buying_power_max is not None and bp > e.buying_power_max):
            out.append(f"buying power {bp:.2f} outside [{e.buying_power_min}, {e.buying_power_max}]")
    return out


def _own_rows(a: AccountState, run_tag: str | None) -> int:
    return sum(1 for r in getattr(a, "tagged_working", ()) if run_tag is not None and r.tag == run_tag)


def check_fixture(first: Sequence[AccountState], expected: Mapping[str, AccountExpectation], *,
                  after_reset: Sequence[AccountState] | None = None, reseed_due_passed: bool = False,
                  paper_send_ok: bool = False, run_tag: str | None = None, after_cancel: bool = False) -> ControlResult:
    """Gate `fixture_verified` (7.6 stage 2): working orders exactly zero, positions exact, cash within tolerance,
    buying power by rule.

    Call it with the first `verify_account_fixture`; when it asks for `reset_empty_account`, reset and call it
    again with the re-verification as `after_reset`. Under `paper_send_ok`, working rows all carrying `run_tag` ask for
    `cancel_all`; cancel, re-read and call it again with that read as `first` and `after_cancel=True` (choice 11).
    """
    reads = list(first) + list(after_reset or ())
    working = sum(a.working_orders for a in reads)
    if working:
        own = sum(_own_rows(a, run_tag) for a in reads)
        if paper_send_ok and not after_cancel and own == working:
            return ControlResult("fixture", False, "fixture_verified", actions=("cancel_all",),
                                 detail=f"{own} working order(s) carry the run's own tag: verified Cancel All, "
                                        "then re-verify (7.6 stage 2 step 2)")
        run, un = safety_abort_outcome("fixture_verified", "unexpected_working_order")
        return ControlResult("fixture", False, "fixture_verified", run=run, unreached=un, alert=alerts.safety_abort(),
                             actions=("kill_tree", "kill_switch"), incident="S1",
                             detail="working orders present; a sent order is never fixture drift"
                             + ("; still working after the own-tag Cancel All" if after_cancel else ""))
    by_role = {a.role: a for a in first}
    subsets: list[SubsetOutcome] = []
    acts: list[str] = []
    notes: list[str] = []
    golden = by_role.get("golden")
    if golden is not None:
        drift = drift_of(golden, expected[golden.account_id])
        if drift:
            subsets.append(SubsetOutcome("account_state:golden",
                                         Outcome("env_error", "fixture_drift", "fixture_verified")))
            acts.append("maintenance_task")
            notes.append("GOLDEN " + "; ".join(drift) + ": re-seed task to the accounts owner")
        elif reseed_due_passed:
            subsets.append(SubsetOutcome("account_state:golden",
                                         Outcome("env_error", "fixture_maintenance_needed", "fixture_verified")))
            acts.append("maintenance_task")
            notes.append("GOLDEN reseed_due has passed")
    empty = by_role.get("empty")
    empty_drift = drift_of(empty, expected[empty.account_id]) if empty is not None else []
    if empty is not None and empty_drift:
        if after_reset is None:
            return ControlResult("fixture", False, "fixture_verified", actions=("reset_empty_account",),
                                 detail="EMPTY account drift: reset once, then re-verify: " + "; ".join(empty_drift))
        again = {a.role: a for a in after_reset}.get("empty")
        still = ["not re-read"] if again is None else drift_of(again, expected[empty.account_id])
        if still:
            subsets.append(SubsetOutcome("account_state", Outcome("env_error", "fixture_drift", "fixture_verified")))
            notes.append("EMPTY after the one reset: " + "; ".join(still))
    if subsets:
        return ControlResult("fixture", False, "fixture_verified", outcomes=tuple(subsets),
                             alert=alerts.env_error(notified=("accounts_owner",) if acts else ()), actions=tuple(acts),
                             detail="fixture drift on account_state checkpoints; the rest of the run proceeds: "
                                    + " | ".join(notes))
    return ControlResult("fixture", True, "fixture_verified")


def check_ondemand(toolbar_present: bool) -> ControlResult:
    """7.7 step 8 and 7.9: the OnDemand toolbar is absent by template after login; present is `env_error (gate:
    fixture_verified, reason: ondemand_state)` for the shard."""
    if toolbar_present:
        return _env_fail("ondemand", "fixture_verified", "ondemand_state",
                         detail="the OnDemand toolbar is visible after login")
    return ControlResult("ondemand", True, "fixture_verified")


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
