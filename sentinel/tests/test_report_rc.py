"""The RC report's sections, sign-off, frozen versions, superseded label, lineage candidates and escapes (9.4 RC
report, 9.8, 9.11, 10.9, 10.11; SD-86), `sentinel.report.rc`. The store builder is test_release_gate's `World`."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
import yaml

from sentinel import release_gate as rg
from sentinel.report import rc
from sentinel.store import ScopeEntry, Store
from sentinel.store.artifacts import ArtifactStore
from sentinel.store.store import Roster
from test_release_gate import NOW, POLICY, RC_BUILD, RELEASED, SOAK, SOAK_WS, SW, T1, T1B, WS, World, ny, utc

ONCALL = yaml.safe_load("""
roles:
  - {role: system_owner, person: asmith, deputy: bkhan}
  - {role: release_manager, person: glee, deputy: cliu}
""")
ROSTER = Roster.from_oncall(ONCALL)
SW2 = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/640x480"
SW3 = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360-asc"
EXP = "flow/trade.positions/flow.explorer.f1/s01"
CAP = "c" * 64


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    yield s
    s.close()


@pytest.fixture
def world(store):
    return World(store)


def decide(store, run_id: str, cid: str = T1, ticket: str = "TOS-77") -> int:
    """A confirm_bug decision on `cid` made on `run_id` (8.3 decisions), as a later triage decision."""
    return store.insert("decisions", {
        "checkpoint_id": cid, "region_id": "rgn.ladder.viewport", "phash": bytes.fromhex("0123456789abcdef"),
        "category": "collateral", "action": "confirm_bug", "verdict": "confirmed_bug", "source": "report_ui",
        "decided_by": "triage.lead", "decided_at": utc(NOW + timedelta(days=1)), "run_id": run_id,
        "reason": "ladder overlap after release", "ticket_ref": ticket})


def signable(world: World, run_id: str = "rc1", *, findings: tuple[tuple[str, str | None], ...] = ()) -> str:
    """A green RC run with tier-2 sweep findings `(finding id, ticket)` written before the run finished."""
    world.rc(run_id)
    for cid in (T1, T1B, SOAK, SW):
        world.put(run_id, cid, "identical")
    for fid, ticket in findings:
        world.finding(run_id, fid, cid=SW, tier=2, ticket=ticket)
    world.cell(run_id, "trade.active-trader.ladder")
    world.proof(run_id)
    world.finish(run_id)
    world.nightly()
    return run_id


def arts(tmp_path, run_id: str = "rc1") -> ArtifactStore:
    return ArtifactStore.at(tmp_path / "artifacts", f"runs/{run_id}", require_local=False)


# -- sign-off refusals (9.4, 9.8, 10.9) -------------------------------------------------------------------------------

def test_sign_off_is_refused_while_a_tier1_candidate_is_pending(world, store):
    world.green()
    store.insert("baselines", {"checkpoint_id": T1, "lineage": "released", "build_id": RELEASED,
                               "image_sha256": "a" * 64, "state": "candidate", "proposed_at": utc(NOW)})
    with pytest.raises(rc.SignOffRefused) as e:
        rc.sign_off(store, "rc1", "asmith", "glee", now=NOW)
    assert e.value.codes == ("baselines",) and e.value.refusals[0].detail == f"{T1}: candidate #1 pending"
    assert rc.signoff_of(store, "rc1") is None and rc.versions(store, "rc1") == ()


def test_sign_off_is_refused_while_the_detector_is_blind(world, store):
    world.green()
    world.nightly("n2", day=7, state="detector_blind")
    with pytest.raises(rc.SignOffRefused) as e:
        rc.sign_off(store, "rc1", "asmith", "glee", now=NOW)
    assert e.value.codes == ("detector",)
    assert "detector_state detector_blind (Nightly n2)" in str(e.value)


def test_a_superseded_run_is_labelled_and_cannot_be_signed(world, store):
    world.green()
    world.rc("rc2", build="rc-2026.12.1-3333333", started=ny(2026, 12, 2, 9))
    store.update("runs", {"run_id": "rc1"}, {"outcome": "aborted_budget", "abort_reason": "superseded"})
    report = rc.build_rc_report(store, "rc1", now=NOW)
    assert report.superseded_by == "rc-2026.12.1-3333333" and not report.signable
    assert report.header[1].startswith("superseded by rc-2026.12.1-3333333: partial report for the release manager")
    assert report.section("signoff").lines == ("Cannot be signed: superseded by rc-2026.12.1-3333333",)
    with pytest.raises(rc.SignOffRefused) as e:
        rc.sign_off(store, "rc1", "asmith", "glee", now=NOW)
    assert e.value.codes == ("superseded",)


def test_people_and_accepted_findings_are_checked(world, store):
    signable(world, findings=(("F-acc", "TOS-5"), ("F-bare", None)))
    with pytest.raises(rc.SignOffRefused) as e:
        rc.sign_off(store, "rc1", "asmith", "asmith", [rc.AcceptedFinding("F-bare"), rc.AcceptedFinding("F-none")],
                    now=NOW)
    assert e.value.codes == ("same_person", "accepted_without_ticket", "accepted_unknown")
    with pytest.raises(rc.SignOffRefused) as e:
        rc.sign_off(store, "rc1", "system", "cliu", now=NOW, roster=ROSTER)
    assert e.value.codes == ("not_a_person", "not_role_holder")
    block = rc.sign_off(store, "rc1", "bkhan", "cliu", [rc.AcceptedFinding("F-acc"),
                                                         rc.AcceptedFinding("F-bare", "TOS-6")],
                        now=NOW, roster=ROSTER)
    assert block.accepted == (("F-acc", "TOS-5"), ("F-bare", "TOS-6"))
    with pytest.raises(rc.SignOffRefused) as e:
        rc.sign_off(store, "rc1", "asmith", "glee", now=NOW)
    assert e.value.codes == ("already_signed",)


# -- sign-off, frozen versions (9.4) ---------------------------------------------------------------------------------

def test_sign_off_freezes_v1_and_a_later_decision_freezes_v2_naming_v1(world, store, tmp_path):
    world.green()
    store.insert("baselines", {"checkpoint_id": T1, "lineage": "released", "build_id": RELEASED,
                               "image_sha256": "b" * 64, "state": "candidate", "proposed_at": utc(NOW),
                               "source_run_id": "rc1"})
    store.update("baselines", {"baseline_id": 1}, {"state": "approved", "approved_by": "tos-trade-ui",
                                                   "approved_at": utc(NOW), "approval_reason": "lineage"})
    a = arts(tmp_path)
    block = rc.sign_off(store, "rc1", "asmith", "glee", now=NOW, roster=ROSTER, artifacts=a)
    assert (block.version, block.rc_build, block.late, block.signoff_due) == (1, RC_BUILD, False,
                                                                            "2026-12-07T22:00:00+00:00")
    assert block.approved_set == ((T1, 1, "b" * 64),) and rc.signoff_of(store, "rc1") == block
    (v1,) = rc.versions(store, "rc1")
    assert (v1.version, v1.reason, v1.persisted) == (1, "sign-off", True)
    text = rc.frozen_text(store, "rc1", 1, a)
    assert text.startswith("Version 1, frozen Mon 2026-12-07 12:00 EST; version signed: 1\n")
    assert "Report version signed: 1" in text and "System owner: asmith" in text
    assert "Approved baseline set, the next lineage baseline of released: 1 checkpoint, sha256 " in text
    assert "Known residuals, accepted (6.13):\n  - The 5.4.6 race between the flag re-check" in text
    assert store.query("SELECT kind, pinned, set_path FROM artifacts WHERE sha256 = ?", (v1.sha256,))[0][:] == (
        "report", 1, "runs/rc1")

    assert rc.new_version(store, "rc1", now=NOW + timedelta(hours=1)) is None      # nothing changed
    decide(store, "rc1")
    v2 = rc.new_version(store, "rc1", now=NOW + timedelta(days=1), artifacts=a)
    assert v2 is not None and (v2.version, v2.reason) == (2, "later decision")
    text2 = rc.frozen_text(store, "rc1", 2, a)
    assert text2.startswith("Version 2, frozen Tue 2026-12-08 12:00 EST; version signed: 1\n")
    assert "Report version signed: 1" in text2
    assert f"#1 confirm_bug (confirmed_bug) on {T1} by triage.lead" in text2
    assert [v.version for v in rc.versions(store, "rc1")] == [1, 2]
    with pytest.raises(rc.RcReportError, match="no frozen version 3"):
        rc.frozen_text(store, "rc1", 3, a)


def test_versions_need_a_sign_off_and_a_late_sign_off_says_so(world, store):
    world.green()
    with pytest.raises(rc.RcReportError, match="not signed"):
        rc.new_version(store, "rc1", now=NOW)
    late = rc.sign_off(store, "rc1", "asmith", "glee", now=ny(2026, 12, 8, 10))
    assert late.late
    assert "(due Mon 2026-12-07 17:00 EST, signed late)" in rc.build_rc_report(store, "rc1", now=NOW).section(
        "signoff").lines[2]
    with pytest.raises(rc.RcReportError, match="without an artifact store"):
        rc.frozen_text(store, "rc1", 1, ArtifactStore.at("/nonexistent-never", "x", require_local=False))


# -- the header: N from the train entry, else 3 (9.4, 11.7 row 12) ---------------------------------------------------

def test_n_comes_from_the_train_entry_else_three_with_the_assumption_line(world, store):
    world.rc("rc1", train=None)
    report = rc.build_rc_report(store, "rc1", now=NOW)
    assert "N = 3 and RC cut at G-7 are assumed: no policy/release_gate.yaml" in report.header
    assert report.stamp == "Live report at Mon 2026-12-07 12:00 EST, not frozen"
    policy = rg.ReleaseGatePolicy(trains=(rg.Train("2027.01", date(2027, 1, 14), date(2027, 1, 5), 4),))
    dl = rc.deadlines_for(store, "rc1", rc.RcInputs(train="2027.01", policy=policy))
    assert (dl.business_days, dl.assumed) == (4, ())
    world.rc("rc2", build="rc-2026.12.1-3333333")
    assert rc.build_rc_report(store, "rc2", now=NOW).header[1] == POLICY.rc_deadlines(
        "2026.12", cut_at=ny(2026, 12, 1, 9)).line()


def test_the_rc_report_is_for_rc_runs(world, store):
    world.nightly()
    with pytest.raises(rc.RcReportError, match="nightly run"):
        rc.build_rc_report(store, "n1", now=NOW)
    with pytest.raises(rc.RcReportError, match="only an RC run is signed"):
        rc.sign_off(store, "n1", "asmith", "glee", now=NOW)
    with pytest.raises(rc.RcReportError, match="unknown run"):
        rc.build_rc_report(store, "nope", now=NOW)


# -- the RC sections (9.4) -------------------------------------------------------------------------------------------

def test_the_rc_sections_print_what_the_run_recorded(world, store):
    scope = (ScopeEntry(T1, WS, "active_trader", 1, "frozen"), ScopeEntry(T1B, WS, "active_trader", 1, "frozen"),
             ScopeEntry(SOAK, SOAK_WS, "active_trader", 1, "masked_live", flow_class="standard"),
             ScopeEntry(SW, WS, "active_trader", 2, "frozen"), ScopeEntry(SW2, WS, "active_trader", 2, "frozen"),
             ScopeEntry(SW3, WS, "active_trader", 2, "frozen"),
             ScopeEntry(EXP, WS, "active_trader", 3, "frozen", flow_class="explorer_candidate"))
    world.rc(scope=scope)
    world.put("rc1", T1, "intended", manifest_entry_ref="tos-48213-1")
    world.put("rc1", T1B, "not_run", "budget")
    world.put("rc1", SOAK, "assertion_failed", "assertion:soak.gdi_objects")
    world.put("rc1", SW, "identical")
    world.put("rc1", SW2, "collateral")
    world.put("rc1", SW3, "identical")
    world.put("rc1", EXP, "invariant_violation", "INV-03")
    world.finding("rc1", "X-1", cid=EXP, kind="explorer_candidate", tier=3, category=None)
    world.finding("rc1", "X-2", cid=EXP, kind="explorer_candidate", tier=3, category=None, held=True)
    world.finding("rc1", "L-1", cid=SOAK, kind="new_log_signature", tier=2, category=None)
    store.write_soak_samples("rc1", [{"sampled_at": utc(ny(2026, 12, 1, 10, m)), "pid": pid, "metric": "gdi_objects",
                                      "value": v} for m, pid, v in ((0, 1, 4000), (0, 2, 100), (1, 1, 9100))])
    inputs = rc.RcInputs(manifest={"entries": [{"entry_id": "tos-48213-1", "tickets": ["TOS-48213"]}]},
                         ticket_export={"TOS-48213": "TOS-100", "TOS-48300": "TOS-100"},
                         migration_workspaces=frozenset({SOAK_WS}), soak_budgets={"gdi_objects": "under 9,000"})
    report = rc.build_rc_report(store, "rc1", inputs=inputs, now=NOW)
    lines = {s.key: s.lines for s in report.sections}
    assert [s.key for s in report.sections] == [k for k, _ in rc.SECTIONS]
    assert lines["lineage"][0] == f"Base: {RELEASED}, the last released build on lineage released; 0 approved " \
                                  "baselines in the lineage set"
    assert lines["intended"] == (
        "Epic TOS-100:", "  tos-48213-1 (TOS-48213): 1 intended checkpoint on trade.active-trader.ladder",
        "Reconciliation against the ticket export: 2 tickets, 1 with a manifest entry; not in the manifest: "
        "TOS-48300; in the manifest, not in the export: none")
    assert lines["sweeps"] == (
        "charts.flexible-grid.active-trader-cell (flexgrid): 3 steps, 1 not identical; first in ladder order "
        "640x480 collateral",
        "  640x480 collateral; 480x360 identical; 480x360 asc identical")
    assert lines["soak"][0] == "1 cycle: c01 assertion_failed (reason: assertion:soak.gdi_objects)"
    assert lines["soak"][1] == "Budget breaches: soak.gdi_objects 1"
    assert "gdi_objects: candidate first 4100, last 9100, ceiling 9100 (2 samples); lineage curve not available; " \
           "budget under 9,000" in lines["soak"]
    assert lines["soak"][-1].startswith(f"New log signatures: 1; first L-1 on {SOAK}")
    assert lines["explorer"] == ("X-1: explorer_candidate on " + EXP + "; replayed flow outcome invariant_violation "
                                 "(reason: INV-03)", "Explorer: not reproduced", f"  X-2: explorer_candidate on {EXP}")
    assert lines["migration"] == (f"{SOAK_WS}: 1 checkpoint, 1 not identical",
                                  f"  {SOAK}: assertion_failed (reason: assertion:soak.gdi_objects)")
    assert lines["escape_seed"] == ("1 tier-1 surface not compared in this RC; G+1 escape tracking starts from these",
                                    "  trade.order-entry.stock: not_run (reason: budget) 1")
    assert "[FAIL] soak within budget or its findings dispositioned" in lines["release_gate"]
    assert lines["signoff"][0].startswith("Not signed; sign-off due Mon 2026-12-07 17:00 EST; release gate blocked")


def test_explorer_not_run_and_missing_inputs_print_one_line_each(world, store):
    world.green()
    store.update_summary("rc1", {"explorer": {"outcome": "not_run", "reason": "model_unavailable"}})
    report = rc.build_rc_report(store, "rc1", now=NOW, inputs=rc.RcInputs(log_signatures_available=False))
    assert report.section("explorer").lines == ("Explorer: not run, model endpoint unavailable",)
    assert report.section("migration").lines == ("No tier B migration workspaces supplied",)
    assert report.section("intended").lines == (
        "No intended checkpoint", "Reconciliation against the ticket export: not possible, no ticket export supplied")
    assert report.section("soak").lines[-1] == "Log signatures: not available"
    assert report.section("decisions").lines == ("No decision on this run",)


# -- lineage candidates (SD-86, 9.8) ---------------------------------------------------------------------------------

def test_the_rc_run_queues_lineage_candidates_once_and_they_refuse_sign_off_until_approved(world, store):
    world.rc()
    store.insert("artifacts", {"sha256": CAP, "kind": "capture", "media_type": "image/png", "bytes": 1,
                               "redaction_passed": 1, "set_path": "runs/rc1", "created_at": utc(NOW)})
    for cid in (T1, SOAK, SW):
        world.put("rc1", cid, "identical")
    world.put("rc1", T1B, "needs_baseline", "new_surface", artifacts={"candidate": CAP})
    world.cell("rc1", "trade.active-trader.ladder")
    world.proof("rc1")
    world.finish("rc1")
    world.nightly()
    assert rc.queue_lineage_candidates(store, "rc1", now=NOW) == (1,)
    assert rc.queue_lineage_candidates(store, "rc1", now=NOW) == ()
    row = store.query("SELECT checkpoint_id, build_id, image_sha256, state, source_run_id FROM baselines")[0]
    assert tuple(row) == (T1B, RC_BUILD, CAP, "candidate", "rc1")
    with pytest.raises(rc.SignOffRefused) as e:
        rc.sign_off(store, "rc1", "asmith", "glee", now=NOW)
    assert e.value.codes == ("baselines",)
    store.update("baselines", {"baseline_id": 1}, {"state": "approved", "approved_by": "tos-trade-ui",
                                                   "approved_at": utc(NOW), "approval_reason": "new surface"})
    assert rc.sign_off(store, "rc1", "asmith", "glee", now=NOW).approved_set == ((T1B, 1, CAP),)
    assert "From this RC run: 1 candidates queued, 1 approved, 0 retired" in rc.build_rc_report(
        store, "rc1", now=NOW).section("baselines").lines
    world.nightly("n9", day=8)
    with pytest.raises(rc.RcReportError, match="not an RC run"):
        rc.queue_lineage_candidates(store, "n9")


# -- escapes (10.9 G+1, 9.11) ---------------------------------------------------------------------------------------

def test_escapes_are_tagged_from_the_signed_rc_run_and_counted(world, store):
    world.rc()
    world.put("rc1", T1, "identical")
    world.put("rc1", T1B, "not_run", "budget")
    rc.record_escape(store, release=RC_BUILD, ticket="TOS-1", entered_by="triage.lead", checkpoint_id=T1,
                     rc_run_id="rc1", at=NOW)
    e2 = rc.record_escape(store, release=RC_BUILD, ticket="TOS-2", entered_by="triage.lead", checkpoint_id=T1B,
                          rc_run_id="rc1", at=NOW)
    rc.record_escape(store, release=RC_BUILD, ticket="TOS-3", entered_by="triage.lead", at=NOW)
    rc.record_escape(store, release=RC_BUILD, ticket="TOS-4", entered_by="triage.lead", checkpoint_id=SW,
                     escape_class="not_detectable", at=NOW)
    assert (e2.escape_class, e2.surface_id, e2.tier) == ("not_run", "trade.order-entry.stock", 1)
    assert e2.escape_id is not None
    rate = rc.escape_rate(store, RC_BUILD)
    assert (rate.total, rate.counted, rate.tier1_covered_missed) == (4, 2, 1)
    assert rate.by_class == {"covered_missed": 1, "not_run": 1, "not_registered": 1, "not_detectable": 1}
    assert rate.by_tier == {1: 2, None: 1, 2: 1}
    assert rc.classify_escape(store, "rc1", SW) == "not_run"                        # not in the run's outcomes
    with pytest.raises(rc.RcReportError, match="give rc_run_id"):
        rc.classify_escape(store, None, T1)
    with pytest.raises(rc.RcReportError, match="unknown checkpoint"):
        rc.record_escape(store, release=RC_BUILD, ticket="TOS-5", entered_by="x", checkpoint_id="census/a.b/c.d",
                         rc_run_id="rc1")
