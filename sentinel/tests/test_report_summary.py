"""The Reporter's reads of the run's summary_json and the Safety store rows (9.2, 9.4 item 8, 9.5).

Each test varies the 9.3 sample by one summary key or one set of store rows: the post-run proof's declared sends and
buying power (6.6), the incident id (9.2), G2 (1.4), the withheld report (L-OUT-07), the manifest lint output and
template link (4.3), the owner warnings and expired records (3.8, 4.8), the redaction counts and misses (3.6), the
Safety proof section, the safety canary banner (6.5, 6.12, 10.3) and the Alerts panel (9.5, 6.6).
"""
from __future__ import annotations

import re
from dataclasses import replace

import pytest

from sentinel.canary import CanaryResult
from sentinel.orchestrator import safety_canaries as sc
from sentinel.records import Finding, RunOutcome
from sentinel.report import (ReportInputError, ReportInputs, SafetyRows, build_report, render_pr_comment,
                             render_report_markdown, render_run_report_html)
from sentinel.report.model import buying_power_line, proof_detail_lines
from sentinel.store import PostRunProof, RunPlan, ShardPlan, Store
from sentinel.store import safety as S

from test_report_sample import RUN_ID, golden_text, sample

BASE = "https://sentinel.internal"
T0 = "2026-11-09T14:00:00+00:00"
SENDS = [{"row_key": "o-1", "flow_id": "flow.paper.send", "step_id": "s03", "token_id": 7,
          "sent_at": "2026-11-09T14:05:00+00:00", "symbol": "SPY", "qty": 1, "working": "o-1", "canceled": "o-1",
          "left_working": False}]
BP = {"start": 25000.0, "end": 24990.0, "drift": 10.0, "verdict": "ok"}


def proof(shard: str | None = f"{RUN_ID}/1", verdict: str = "proven_declared_sends", **counts) -> PostRunProof:
    counts.setdefault("rows", {"working orders": {"start": 0, "end": 0, "new": 1, "expected": 1, "verdict": "ok"},
                               "buying power": BP})
    counts.setdefault("sends", SENDS)
    counts.setdefault("undeclared", [])
    return PostRunProof(shard_id=shard, run_id=RUN_ID, login_alias="login-01" if shard else None, verdict=verdict,
                        send_policy_ref="paper_send_ok:dec-000600", counts=counts, cancel_all="not_needed",
                        paper_mode_assertions=4, paper_mode_mismatches=0, gateway_events=1912, gateway_drops=0,
                        produced_at=T0, attestation_ref="att-77")


def health_row(text: str, label: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"| {label} |"))


def intended_block(text: str) -> list[str]:
    return text.split("**Intended (")[1].split("**Suppressed by decision")[0].splitlines()


def links(text: str) -> list[str]:
    return re.findall(r"(?<!\\)\]\(([^)\s]*)\)", text)


def test_an_empty_summary_and_no_safety_rows_keep_the_9_3_golden():
    assert render_pr_comment(**sample(), summary={}, safety=SafetyRows()) == golden_text()


# ---- post-run proof (6.6, 6.7) --------------------------------------------------------------------------------------

def test_the_safety_row_lists_each_declared_send_and_the_buying_power_line():
    s = sample()
    s["health"]["safety"]["proof"] = "PROVEN: only declared sends"
    text = render_pr_comment(**s, safety=SafetyRows(proofs=(proof(),)))
    row = health_row(text, "Safety")
    assert ("proof PROVEN: only declared sends; send flow.paper.send s03 at 2026-11-09T14:05:00+00:00: Working row "
            "o-1, Canceled row o-1; buying power 25,000.00 to 24,990.00, drift 10.00, ok") in row
    assert buying_power_line(BP) == "buying power 25,000.00 to 24,990.00, drift 10.00, ok"


def test_several_proofs_prefix_their_shard_and_a_proof_without_buying_power_has_no_line():
    two = (proof(), proof(f"{RUN_ID}/2", "proven_no_sends", rows={}, sends=[]), proof(None, "proven_no_session",
                                                                                         rows={}, sends=[]))
    lines = proof_detail_lines(two)
    assert lines == [f"shard {RUN_ID}/1: send flow.paper.send s03 at 2026-11-09T14:05:00+00:00: Working row o-1, "
                     "Canceled row o-1", f"shard {RUN_ID}/1: buying power 25,000.00 to 24,990.00, drift 10.00, ok"]
    assert proof_detail_lines((proof(rows={}, sends=[]),)) == []


def test_the_abort_block_prints_the_sends_after_the_proof_and_the_summary_incident():
    s = sample()
    s["run"] = RunOutcome(run_id=RUN_ID, run_type="pr", status="aborted_safety", reason="kill_switch",
                          last_gate="running")
    s["context"] = replace(s["context"], abort={"trigger": "kill switch (file flag)", "rig": "rig-01"})
    summary = {"abort": {"incident": 17, "trigger": "never printed: the context wins"}}
    report = build_report(**s, summary=summary, safety=SafetyRows(proofs=(proof(),)))
    text = render_report_markdown(report)
    assert report.status.reason == "aborted_safety: incident 17"
    abort = text.split("**Abort**")[1].split("**Suppressed")[0]
    assert "- Trigger: kill switch (file flag)" in abort and "Incident: 17" in abort
    assert abort.index("Post-run proof:") < abort.index("send flow.paper.send s03") < abort.index("buying power")


def test_without_an_incident_id_the_status_says_unknown():
    s = sample()
    s["run"] = RunOutcome(run_id=RUN_ID, run_type="pr", status="aborted_safety", reason="kill_switch",
                          last_gate="running")
    assert build_report(**s, summary={}).status.reason == "aborted_safety: incident unknown"


# ---- G2 and the withheld report (1.4, 4.13 L-OUT-07) ----------------------------------------------------------------

def cells(n: int, *, short: bool = False) -> list[dict]:
    rows = [{"surface_id": f"s.{i}", "persona": "active_trader", "entitlement": "margin", "state": "covered",
             "worst_outcome": "identical", "checkpoint_count": 2, "not_run_reason": None} for i in range(n)]
    if short:
        rows = [{"surface": r["surface_id"], "persona": r["persona"], "state": "covered", "worst": "identical",
                 "n": 2} for r in rows]
    return rows


def test_a_matrix_whose_cells_do_not_sum_is_replaced_by_the_g2_abort_block(tmp_path):
    s = sample()
    s["coverage"].update(matrix={"surfaces": 3, "personas": 1, "profiles": 1}, cells=cells(2))
    report = build_report(**s, summary={})
    text = render_report_markdown(report)
    cov = text.split("**Coverage**")[1].split("**Run health**")[0]
    assert "Coverage matrix withheld" in cov and "- G2 matrix: 2 cells, expected 3 surfaces x 1 personas x 1 " \
                                                 "profiles = 3" in cov
    assert "| Tier |" not in cov and "[Matrix]" not in cov
    page = render_run_report_html(report)
    assert "Coverage matrix withheld" in page and "<h3>Coverage matrix</h3>" not in page
    assert report.coverage_rows == [] and report.coverage_cells == []


def test_a_matrix_that_sums_is_published_in_either_cell_form():
    for short in (False, True):
        s = sample()
        s["coverage"].update(matrix={"surfaces": 3, "personas": 1, "profiles": 1}, cells=cells(3, short=short))
        report = build_report(**s, summary={})
        assert report.g2_failures == [] and "| Tier |" in render_report_markdown(report)
        page = render_run_report_html(report)
        assert "<code>s.2</code>" in page and "identical (2)" in page
    s = sample()
    s["coverage"].update(matrix={"surfaces": "many"}, cells=cells(1))
    with pytest.raises(ReportInputError, match="coverage.matrix"):
        build_report(**s)


def test_a_withheld_report_is_the_abort_block_alone():
    s = sample()
    s["run"] = RunOutcome(run_id=RUN_ID, run_type="pr", status="aborted_harness", reason="taxonomy_check_failed",
                          last_gate="reported")
    summary = {"post_run_check": {"failures": ["L-OUT-07: 1 scoped checkpoint(s) without an outcome record, first x",
                                               "G2 matrix: 2 cells, expected 3"], "report_withheld": True}}
    report = build_report(**s, summary=summary)
    text = render_report_markdown(report)
    assert report.special == "withheld" and report.status.state == "error"
    for gone in ("**Unexpected", "**Intended", "**Coverage**", "**Run health**", "unexpected |"):
        assert gone not in text
    assert "- Reason: `taxonomy_check_failed`" in text and "- Post-run check: G2 matrix: 2 cells, expected 3" in text
    assert "- Report withheld: the post-run check failed" in text
    page = render_run_report_html(report)
    assert 'id="unexpected"' not in page and 'id="coverage"' not in page and "Post-run check: L-OUT-07" in page


def test_a_withheld_report_of_a_run_that_did_not_abort_is_an_error_status():
    s = sample()
    report = build_report(**s, summary={"post_run_check": {"failures": ["x"], "report_withheld": True}})
    assert (report.status.state, report.status.reason) == ("error", "Sentinel did not report")


# ---- Intended: manifest lint, template link, empty_reason (4.3) -----------------------------------------------------

def test_a_manifest_that_failed_the_lint_prints_its_lint_lines_and_the_template_link():
    s = sample()
    s["manifest"] = None
    lint = ["L-MAN-06 error manifests/branch-1998.3.0-7be42d0.yaml: entries: entries empty requires empty_reason",
            "L-MAN-09 error manifests/branch-1998.3.0-7be42d0.yaml: candidate: names another build"]
    text = render_pr_comment(**s, summary={"manifest_lint": lint})
    block = intended_block(text)
    assert block[1].startswith("No manifest and no UI code touched")
    assert block[2] == "The manifest failed the lint and is treated as missing (4.3):"
    assert block[3] == f"- `{lint[0]}`" and block[4] == f"- `{lint[1]}`"
    assert block[5] == f"[Manifest template]({BASE}/docs/manifest-template) (4.3)"
    assert all(u.startswith(BASE + "/") for u in links(text))
    page = render_run_report_html(build_report(**s, summary={"manifest_lint": lint}))
    assert "<li><code>L-MAN-09 error" in page and 'href="https://sentinel.internal/docs/manifest-template"' in page


def test_a_missing_manifest_links_the_template_and_a_loaded_one_does_not():
    s = sample()
    assert "manifest-template" not in render_pr_comment(**s)
    s["manifest"] = None
    block = intended_block(render_pr_comment(**s))
    assert "The manifest failed the lint" not in "\n".join(block)
    assert block[2] == f"[Manifest template]({BASE}/docs/manifest-template) (4.3)"


def test_a_manifest_with_no_entries_prints_its_empty_reason():
    s = sample()
    s["manifest"] = {**s["manifest"], "entries": [], "empty_reason": "no PR manifests in range"}
    assert "No manifest entries: no PR manifests in range" in intended_block(render_pr_comment(**s))


# ---- Run health: owner warnings, expired records, redaction (3.6, 3.8, 4.8) -----------------------------------------

def test_the_seven_day_owner_warnings_and_the_expired_records_are_run_health_rows():
    s = sample()
    s["health"]["expired"] = ["mask expired: msk.ps.last-update-01", "floor exception expired: rgn.ps.pl"]
    summary = {"expiring": ["mask msk.x expires in 5 days (2026-11-14T00:00:00Z); owner tos-ui"]}
    text = render_pr_comment(**s, summary=summary)
    assert health_row(text, "Expired records") == \
        "| Expired records | mask expired: msk.ps.last-update-01; floor exception expired: rgn.ps.pl |"
    assert "| Masks expiring within 7 days | mask msk.x expires in 5 days (2026-11-14T00:00:00Z); owner tos-ui |" \
        in text
    assert "Expired records" not in render_pr_comment(**sample())


def redaction_miss(fid: str, *, candidate_only: bool, tier: int | None = 2) -> Finding:
    cid = "census/charts.main/ws.tier-a.active-trader"
    return Finding(fid, RUN_ID, "redaction_miss", f"the OCR sweep found an account id outside the anchors ({fid})",
                   RUN_ID, checkpoint_id=cid, tier=tier, side="candidate" if candidate_only else "base",
                   detail="candidate_only" if candidate_only else None,
                   evidence={"crop": [1, 2, 30, 8], "side": "candidate", "candidate_only": candidate_only})


def test_redaction_misses_are_unexpected_items_and_counted_with_the_boxes_per_side():
    s = sample()
    s["findings"] += [redaction_miss("fnd-leak", candidate_only=True, tier=None),
                      redaction_miss("fnd-anchor", candidate_only=False, tier=3)]
    s["health"]["redaction"] = {"base": 12, "candidate": 13}
    report = build_report(**s)
    text = render_report_markdown(report)
    by_id = {i.finding.finding_id: i for i in report.unexpected if i.finding is not None and i.cluster is None}
    leak, anchor = by_id["fnd-leak"], by_id["fnd-anchor"]
    assert leak.tier == 1 and anchor.tier == 3
    tier1 = [i for i in report.unexpected if i.tier == 1]
    assert tier1[-1] is leak                             # ranked after every listed tier-1 kind (9.2 does not list it)
    assert "[Tier 1] redaction_miss on `charts.main`" in text
    assert health_row(text, "Redaction") == \
        "| Redaction | boxes: base 12, candidate 13; 2 `redaction_miss` (1 candidate only, tier 1) |"
    assert report.status.reason == build_report(**sample()).status.reason    # never fails the check


def test_a_redaction_miss_is_never_a_checkpoints_primary_finding():
    s = sample()
    cid = "census/charts.main/ws.tier-a.active-trader"
    s["outcomes"] = [replace(o, outcome="needs_human", reason="oracle_unreadable", tier=3) if o.checkpoint_id == cid
                     else o for o in s["outcomes"]]
    s["findings"].append(redaction_miss("fnd-anchor", candidate_only=False, tier=3))
    item = next(i for i in build_report(**s).unexpected if i.cluster is not None
                and i.cluster.representative.checkpoint_id == cid)
    assert item.cluster.representative.finding is None    # the needs_human item keeps its own evidence and actions
    assert f"f={cid.replace('/', '%2F')}" in str(dict(item.fields)["Actions"])


# ---- Safety: drill and canary rows, banner, incidents, overdue postmortems (6.5, 6.12, 10.3) -----------------------

def drill(*states: str) -> list[CanaryResult]:
    return [CanaryResult(mutant_id=f"drill-{n}", catalog_version="1", checkpoint_id=sc.DRILL_CHECKPOINT,
                         defect_class="fire_drill", money_cell=False, seed=0, expected=["blocked"], state=st,
                         produced_outcome=None if st == "detected" else "harness_error",
                         produced_reason=None if st == "detected" else f"drill_failed:item{n}",
                         absorbed_by=None if st == "detected" else "the drop set differs", kind="fire_drill")
            for n, st in enumerate(states, 1)]


def canaries(blind: str | None = None) -> list[sc.SafetyCanaryResult]:
    return [sc.score_canary(n, [sc.CanaryCase("case", "x", "y" if n == blind else "x")]) for n in sc.CANARIES]


def incident(iid: int, severity: str, trigger: str, *, due: str | None = None) -> S.Incident:
    return S.Incident(iid, RUN_ID, "rig-01", "login-01", severity, trigger, T0, None, None, None, None, None, due)


def test_the_safety_rows_and_banner_print_in_run_health_and_the_safety_proof_section():
    results = canaries(blind="proof")
    rows = SafetyRows(proofs=(proof(),), canaries=tuple(drill("detected", "missed", "detected", "detected",
                                                              "detected") + [r.row for r in results]),
                      nightly_run_id="nightly-2026-11-09",
                      incidents=(incident(12, "S1", "undeclared_order_row", due="2026-11-16T14:00:00Z"),),
                      overdue=(incident(9, "S2", "kill_switch: file_flag", due="2026-11-05T14:00:00Z"),))
    s = sample()
    report = build_report(**s, summary={"safety": {"banner": sc.banner(results)}}, safety=rows)
    text = render_report_markdown(report)
    lines = text.splitlines()
    assert lines[1] == "```text" and lines[2] == "DETECTOR BLIND: a safety canary missed."
    assert "RC sign-off is blocked." in text.split("**Sentinel PR run")[0]
    assert health_row(text, "Fire drill (last Nightly)") == (
        "| Fire drill (last Nightly) | 4 of 5 detected (Nightly run nightly-2026-11-09); drill-2 missed: "
        "harness_error (reason: drill_failed:item2) |")
    assert health_row(text, "Safety canaries (last Nightly)") == (
        "| Safety canaries (last Nightly) | 4 of 5 detected (Nightly run nightly-2026-11-09); proof detector_blind: "
        "harness_error (reason: safety_canary_blind:proof) |")
    assert health_row(text, "Incidents") == (
        f"| Incidents | 1 open: 12 S1 (undeclared_order_row) on rig rig-01, run {RUN_ID}; postmortem overdue: 9 "
        "S2 (kill_switch: file_flag), due 2026-11-05T14:00:00Z |")
    titles = [t for t, _ in report.safety_proof]
    assert titles == [f"ORDER SAFETY PROOF, shard {RUN_ID}/1", "Fire drill (Nightly run nightly-2026-11-09)",
                      "Safety canaries (Nightly run nightly-2026-11-09)", "Safety canary banner", "Open incidents",
                      "Postmortems overdue"]
    page = render_run_report_html(report)
    safety = page.split('<section id="safety">')[1].split("</section>")[0]
    for shown in ("VERDICT PROVEN: only declared sends", "identity: paper mode asserted 4 times, 0 mismatches",
                  "attestation att-77", "gateway 1912 events allowed, 0 drops, 0 refusals",
                  "working orders: start 0, end 0, new 1, expected 1, ok",
                  "buying power 25,000.00 to 24,990.00, drift 10.00, ok", "cancel all not needed",
                  "send flow.paper.send s03", "drill-2: missed, harness_error (reason: drill_failed:item2): the drop "
                  "set differs", "proof: detector_blind", "12 S1 (undeclared_order_row) on rig rig-01",
                  "postmortem due 2026-11-16T14:00:00Z", "9 S2 (kill_switch: file_flag), due 2026-11-05T14:00:00Z, "
                  "no postmortem linked"):
        assert shown in safety, shown
    assert "login-01" not in safety                      # a proof's login alias is never printed (8.3)


def test_a_detector_banner_comes_before_the_safety_canary_banner():
    s = sample()
    s["context"] = replace(s["context"], detector_banner="DETECTOR BLIND since 2026-11-09 03:12 ET.")
    report = build_report(**s, summary={"safety": {"banner": "DETECTOR BLIND: a safety canary missed."}})
    assert report.banner == "DETECTOR BLIND since 2026-11-09 03:12 ET.\nDETECTOR BLIND: a safety canary missed."


def test_the_safety_section_takes_only_drill_and_safety_canary_rows():
    mutant = CanaryResult(mutant_id="M01", catalog_version="1", checkpoint_id="census/x/y", defect_class="overlap",
                          money_cell=False, seed=1, expected=["invariant_violation"], state="detected")
    with pytest.raises(ReportInputError, match="fire_drill"):
        SafetyRows(canaries=(mutant,))
    assert build_report(**sample()).safety_proof == []


# ---- the Alerts panel (9.5, 6.6 Cancel All failure) -----------------------------------------------------------------

def test_the_alerts_panel_prints_the_hand_over_facts():
    sha = "ab" * 32
    alerts = [{"run_id": RUN_ID, "shard_id": f"{RUN_ID}/1", "rule": "safety_abort", "priority": "P1",
               "paged": ["on_call", "login_owner"], "notified": [], "safety_table": True, "control": "proof",
               "gate": "teardown", "incident": "S1", "run_outcome": None, "run_reason": None,
               "detail": "UNPROVEN: cancel_all_failed", "account": "account D-xxxxxxxx (allowlist entry 2) on rig "
               "rig-01", "working_orders_screenshot": sha, "instruction": "cancel by hand through the hypervisor "
               "console now"},
              {"alert": "dead_man", "closed_as": "late arrival", "at": "2026-11-09T15:00:00Z"}]
    report = build_report(**sample(), summary={"alerts": alerts})
    page = render_run_report_html(report)
    panel = page.split('<section id="alerts">')[1].split("</section>")[0]
    assert "<h2>Alerts (2)</h2>" in page
    assert ("P1 safety_abort: proof at teardown, incident S1: UNPROVEN: cancel_all_failed; paged on_call, "
            "login_owner; account: account D-xxxxxxxx (allowlist entry 2) on rig rig-01; ") in panel
    assert f'<a href="{BASE}/a/{sha}">Working Orders screenshot</a>' in panel
    assert "instruction: cancel by hand through the hypervisor console now" in panel
    assert "alert dead_man, closed_as late arrival, at 2026-11-09T15:00:00Z" in panel
    assert "Alerts" not in render_report_markdown(report)       # a PR comment has no Alerts block (9.2)


# ---- ReportInputs.from_store: the summary and the Safety rows read back from a store ---------------------------------

def test_report_inputs_read_a_finished_run_its_summary_and_its_safety_rows(tmp_path):
    st = Store(tmp_path / "central.db")
    st.register_rig("rig-01", login_alias="login-01", owner="ops", snapshot_id="snap-1", allowlist_entry=1)
    st.register_rig("rig-05", login_alias="login-05", owner="ops", snapshot_id="snap-5", allowlist_entry=2,
                    role="nightly")

    def plan(run_id: str, run_type: str, rig: str) -> RunPlan:
        return RunPlan(run_id=run_id, run_type=run_type, config_commit="c", config_tarball_sha256="t",
                       snapshot_set_sha256="s", harness_sha="h", authorization_ref="auth/v1", budget_sec=60, scope=(),
                       shards=(ShardPlan(f"{run_id}/1", rig),), started_at=T0)

    st.begin_run(plan("nightly-1", "nightly", "rig-05"))
    st.write_canary_results("nightly-1", drill("detected", "detected", "detected", "detected", "missed")
                            + [r.row for r in canaries()], created_at=T0)
    st.begin_run(plan(RUN_ID, "pr", "rig-01"))
    st.write_proof(proof())
    iid = S.open_incident(st, run_id=RUN_ID, rig_id="rig-01", login_alias="login-01", severity="S1",
                          trigger="undeclared_order_row", at=T0)
    with pytest.raises(ReportInputError, match="has not ended"):
        ReportInputs.from_store(st, RUN_ID)
    s = sample()
    summary = {"coverage": s["coverage"], "health": {**s["health"], "redaction": {"base": 3, "candidate": 3}},
               "abort": {"incident": iid}, "safety": {"nightly_run_id": "nightly-1", "banner": None},
               "expiring": ["mask msk.x expires in 5 days (2026-11-14T00:00:00Z); owner tos-ui"]}
    st.end_run(RunOutcome(RUN_ID, "pr", "aborted_safety", "teardown", reason="undeclared_order_row",
                          started_at=T0, ended_at="2026-11-09T14:40:00+00:00"), summary=summary)
    inputs = ReportInputs.from_store(st, RUN_ID, at="2026-11-30T00:00:00+00:00")
    st.close()
    assert [p.shard_id for p in inputs.safety.proofs] == [f"{RUN_ID}/1"]
    assert [r.mutant_id for r in inputs.safety.canaries] == [f"drill-{n}" for n in range(1, 6)] + list(sc.CANARIES)
    assert [i.incident_id for i in inputs.safety.incidents] == [iid] == [i.incident_id for i in inputs.safety.overdue]
    report = inputs.build(None, s["context"])
    text = render_report_markdown(report)
    assert report.status.reason == f"aborted_safety: incident {iid}"
    assert "| Redaction | boxes: base 3, candidate 3; 0 `redaction_miss` |" in text
    assert "| Fire drill (last Nightly) | 4 of 5 detected (Nightly run nightly-1); drill-5 missed" in text
    assert "| Masks expiring within 7 days | mask msk.x expires in 5 days" in text
    assert "buying power 25,000.00 to 24,990.00, drift 10.00, ok" in health_row(text, "Safety")
