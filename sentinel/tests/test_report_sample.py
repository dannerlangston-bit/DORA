"""The 9.3 filled sample (PR 4812, the worked example of 1.11) as records, and its golden test.

`sample()` builds the inputs the Reporter would read for run run-2026-11-09-pr-4812; other
report tests import it and vary one thing at a time.
"""
from __future__ import annotations

from pathlib import Path

from sentinel.records import Classification, CheckpointOutcome, Component, Decision, DiffSummary, Finding, \
    RegionDiff, RunOutcome, Signature
from sentinel.report import ReportContext, render_pr_comment

GOLDEN = Path(__file__).parent / "golden" / "pr-4812.md"
RUN_ID = "run-2026-11-09-pr-4812"
AT = "charts.flexible-grid.active-trader-cell"
MODEL, PROMPT = "claude-pinned-2026-06", "7c1e2a"
ENTRY = "tos-48120-1"
SUMMARY = "Flexible Grid cells lose 6 px of chrome; hosted content gets the full remaining cell height"
INV_TEXT = ("INV-01 sibling bounds intersect: the ladder viewport (y 38 to 352) overlaps the order-control strip "
            "(y 22 to 52) by 14 px across the strip's width; the first price row draws over BUY MKT and SELL MKT. "
            "INV-03 text clipped: the MKT word box is truncated at its lower edge.")
SWEEP_DESC = "Ladder viewport starts 6 px higher than base; order controls unchanged; no overlap at this size."
CENSUS_AT_DESC = ("Cell header 6 px shorter; ladder viewport 6 px higher than base; order controls unchanged; "
                  "no overlap at this workspace's cell size.")
CELLS_DESC = "Cell header 28 to 22 px; content area 6 px taller; no invariant fired."
CHART_DESC = "Header 28 to 22 px and plot area 6 px taller match the entry; one more price label per chart."
WIDTHS, HEIGHTS = (320, 480, 640, 960), (360, 480, 720)
# 9.3 artifact hashes are shortened in the spec; the Reporter prints whatever hash it is given.
ARTS = {
    f"sweep/{AT}/flexgrid/480x360": ("3f9c", "a81d"),
    f"sweep/{AT}/flexgrid/640x480": ("91b0", "5e27"),
    f"census/{AT}/ws.tier-a.active-trader": ("4d60", "e3f8"),
    "census/charts.flexible-grid.level2-cell/ws.tier-a.active-trader": ("77aa", "b2c9"),
    "census/charts.flexible-grid.chart-cell/ws.tier-a.active-trader": ("c044", "0dd3"),
}
FINDING_IDS = {
    f"sweep/{AT}/flexgrid/480x360": "F-20931",
    f"sweep/{AT}/flexgrid/640x480": "F-20932",
    "census/charts.flexible-grid.level2-cell/ws.tier-a.active-trader": "F-20933",
    f"census/{AT}/ws.tier-a.active-trader": "F-20934",
}


def _outcome(cid: str, outcome: str, *, tier: int, persona: str = "active_trader", workspace: str | None = None,
             reason: str | None = None, recaptures: int = 0, classification: Classification | None = None,
             primitive: str | None = None, entry: str | None = None, region: str | None = None,
             invariant_ids: list[str] | None = None, decision_ref: str | None = None,
             box: tuple[int, int, int, int] | None = None) -> CheckpointOutcome:
    parts = cid.split("/")
    ws = workspace or (parts[2] if parts[0] == "census" else "ws.tier-c.sweep-harness")
    base, cand = ARTS.get(cid, (None, None))
    artifacts = {"base": base, "candidate": cand} if base else None
    diff = None
    if region:
        diff = DiffSummary(whole_pixel_fraction=0.01, whole_ssim=0.97,
                           regions=[RegionDiff(region, 0.08, 0.9, True)],
                           components=[Component(1200, box or (0, 0, 120, 6), region)])
    return CheckpointOutcome(
        run_id=RUN_ID, checkpoint_id=cid, outcome=outcome, reason=reason, workspace_id=ws, persona=persona,
        data_mode="frozen", tier=tier, recaptures=recaptures, stability="stable" if recaptures else None,
        classification=classification, primitive=primitive, manifest_entry_ref=entry, diff=diff,
        artifacts=artifacts, invariant_ids=invariant_ids, decision_ref=decision_ref,
        threshold_source="calibrated" if region else None)


def _finding(cid: str, fid: str, kind: str, description: str, *, tier: int, region: str, phash: str,
             evidence: dict, category: str | None = None, decision_ref: str | None = None,
             primitive: str | None = "FlexibleGrid", entry: str | None = ENTRY) -> Finding:
    return Finding(finding_id=fid, run_id=RUN_ID, kind=kind, description=description, first_seen_run_id=RUN_ID,
                   checkpoint_id=cid, tier=tier, region_id=region, primitive=primitive, manifest_entry_ref=entry,
                   phash=phash, category=category or kind, evidence=evidence, decision_ref=decision_ref)


def sample() -> dict:
    """Inputs of the 9.3 sample: 12 Ladder S steps, 5 Flexible Grid census checkpoints, one suppressed
    smoke census checkpoint and a few identical ones."""
    outcomes, findings = [], []
    sweep_cls = Classification("collateral", "rule", model_id=MODEL, prompt_hash=PROMPT, description=SWEEP_DESC)
    layout = {"grid": "2x2", "cell": "(0,0)"}
    n = 20935
    for w in WIDTHS:
        for h in HEIGHTS:
            cid = f"sweep/{AT}/flexgrid/{w}x{h}"
            fid = FINDING_IDS.get(cid)
            if fid is None:
                fid, n = f"F-{n}", n + 1
            if h == 360:
                outcomes.append(_outcome(cid, "invariant_violation", tier=1, reason="INV-01", recaptures=1,
                                         invariant_ids=["INV-01", "INV-03"], primitive="FlexibleGrid", entry=ENTRY,
                                         region="rgn.ladder.viewport",
                                         classification=Classification("collateral", "rule")))
                findings.append(_finding(cid, fid, "invariant_violation", INV_TEXT, tier=1,
                                         region="rgn.ladder.viewport", phash="a1b2c3d4e5f60718",
                                         evidence={"target_window": "main", "layout": layout,
                                                   "violation": "14 px intersection",
                                                   "overlay": "bounds overlaid, intersection hatched; on the base "
                                                              "the strip and viewport touch at y=58, 0 px gap"}))
            else:
                outcomes.append(_outcome(cid, "collateral", tier=1, recaptures=2, primitive="FlexibleGrid",
                                         entry=ENTRY, region="rgn.ladder.viewport", classification=sweep_cls))
                findings.append(_finding(cid, fid, "collateral", SWEEP_DESC, tier=1, region="rgn.ladder.viewport",
                                         phash="0f1e2d3c4b5a6978",
                                         evidence={"target_window": "main", "layout": layout,
                                                   "reviewer": {"change_types": ["shifted"]}}))
    at_census = f"census/{AT}/ws.tier-a.active-trader"
    outcomes.append(_outcome(at_census, "collateral", tier=1, recaptures=2, primitive="FlexibleGrid", entry=ENTRY,
                             region="rgn.ladder.viewport",
                             classification=Classification("collateral", "rule", model_id=MODEL, prompt_hash=PROMPT,
                                                           description=CENSUS_AT_DESC)))
    findings.append(_finding(at_census, "F-20934", "collateral", CENSUS_AT_DESC, tier=1, region="rgn.ladder.viewport",
                             phash="1122334455667788",
                             evidence={"target_window": "main", "reviewer": {"change_types": ["shifted"]}}))
    cells_cls = Classification("collateral", "rule", model_id=MODEL, prompt_hash=PROMPT, description=CELLS_DESC)
    for surface, fid, phash in (("charts.flexible-grid.level2-cell", "F-20933", "c0ffee00c0ffee00"),
                                ("charts.flexible-grid.time-sales-cell", "F-20936", "c0ffee00c0ffee03")):
        cid = f"census/{surface}/ws.tier-a.active-trader"
        outcomes.append(_outcome(cid, "collateral", tier=2, recaptures=2, primitive="FlexibleGrid", entry=ENTRY,
                                 region="rgn.cell.header", classification=cells_cls, box=(2, 0, 300, 6)))
        findings.append(_finding(cid, fid, "collateral", CELLS_DESC, tier=2, region="rgn.cell.header", phash=phash,
                                 evidence={"target_window": "main", "reviewer": {"change_types": ["resized"]}}))
    chart_cls = Classification("intended", "reviewer", confidence=0.93, model_id=MODEL, prompt_hash=PROMPT,
                               description=CHART_DESC)
    for persona in ("active_trader", "swing_trader"):
        ws = f"ws.tier-a.{persona.replace('_', '-')}"
        cid = f"census/charts.flexible-grid.chart-cell/{ws}"
        outcomes.append(_outcome(cid, "intended", tier=2, persona=persona, recaptures=2, entry=ENTRY,
                                 region="rgn.cell.header", classification=chart_cls, box=(0, 0, 400, 6)))
        findings.append(_finding(cid, f"F-2094{len(findings) % 10}", "intended", CHART_DESC, tier=2,
                                 region="rgn.cell.header", phash="5555aaaa5555aaaa", primitive=None,
                                 evidence={"target_window": "main"}))
    ticket = "census/trade.order-entry.stock/ws.tier-a.active-trader"
    outcomes.append(_outcome(ticket, "noise", tier=1, reason="decision:dec-000412", recaptures=2,
                             region="rgn.ticket.header", decision_ref="dec-000412"))
    findings.append(_finding(ticket, "F-20937", "collateral", "Ticket header differs", tier=1,
                             region="rgn.ticket.header", phash="9999888877776666", primitive=None, entry=None,
                             evidence={"target_window": "main"}, decision_ref="dec-000412"))
    for surface in ("trade.active-trader.ladder", "monitor.activity-and-positions", "charts.main"):
        outcomes.append(_outcome(f"census/{surface}/ws.tier-a.active-trader", "identical", tier=1))
    decisions = [Decision("dec-000412", "noise_signature", "noise",
                          Signature(ticket, "rgn.ticket.header", "9999888877776666", "collateral"),
                          decided_by="j.alvarez", decided_at="2026-10-21T14:03:00+00:00")]
    manifest = {
        "schema_version": 1, "manifest_id": "mf-branch-1998.3.0-7be42d0", "candidate": "branch-1998.3.0-7be42d0",
        "base": "main-1998.3.0-2c91ae4", "source": "manual", "author": "jdoe",
        "entries": [{"entry_id": ENTRY, "tickets": ["TOS-48120"], "summary": SUMMARY,
                     "declared_surfaces": ["charts.flexible-grid.chart-cell"], "touched_primitives": ["FlexibleGrid"],
                     "must_be_observed": True}],
    }
    coverage = {
        "tiers": {
            1: {"registered": 41, "in_scope": 20, "covered": 14, "not_compared": {},
                "not_run": {"scope": 21, "entitlement": 6}},
            2: {"registered": 68, "in_scope": 3, "covered": 3, "not_compared": {}, "not_run": {"scope": 65}},
            3: {"registered": 112, "in_scope": 0, "covered": 0, "not_compared": {}, "not_run": {"scope": 112}},
        },
        "scope": {"declared_surfaces": 1, "edges": 4, "primitives": ["FlexibleGrid"], "static_impact_added": 0,
                  "smoke_census": 60, "send_policy": "no_sends", "order_surfaces_entitlement": 6},
    }
    health = {
        "wall_clock": "41:12", "budget": "45:00", "shards": 2,
        "fingerprint": {"candidate": "9e1f03", "base": "9e1f03"},
        "reviewer": {"cost_usd": 0.039, "tier": "mid", "top_tier_usd": 0.078},
        "canary": {"money_detected": 24, "money_total": 24, "other_detected": 31, "other_total": 32,
                   "detector_state": "ok", "date": "2026-11-08"},
        "safety": {"attestation": "passed", "paper_mode_asserted": 4, "gateway_drops": 0, "cancel_all": "not needed",
                   "proof": "PROVEN: no orders sent"},
        "integrations": {"pr_comment": {"state": "posted", "time": "11:12 ET"}, "jira_queued": 0,
                         "slack": {"to": ["the PR author", "triage"],
                                   "posts": [{"tier": 1, "outcome": "invariant_violation", "items": [1]},
                                             {"tier": 1, "outcome": "collateral", "items": [2, 3]}]}},
    }
    context = ReportContext(pr=4812, candidate_build="branch-1998.3.0-7be42d0", base_build="main-1998.3.0-2c91ae4",
                            check_mode="informational", base_reused_from="run-2026-11-09-main-2c91ae4")
    run = RunOutcome(run_id=RUN_ID, run_type="pr", status="complete", last_gate="reported",
                     counts={"invariant_violation": 4, "collateral": 11, "intended": 2, "noise": 1, "identical": 3})
    return {"run": run, "outcomes": outcomes, "findings": findings, "decisions": decisions, "coverage": coverage,
            "health": health, "manifest": manifest, "context": context}


def golden_text() -> str:
    """The golden comment without its leading explanatory HTML comment."""
    text = GOLDEN.read_text(encoding="utf-8")
    head, sep, rest = text.partition("-->\n")
    assert head.startswith("<!-- golden"), "golden file must open with its explanatory comment"
    return rest


def render(**overrides) -> str:
    kw = sample()
    kw.update(overrides)
    return render_pr_comment(**kw)


def test_pr_comment_matches_9_3_golden():
    assert render() == golden_text()


SPEC_9 = Path(__file__).resolve().parents[2] / "sentinel-spec" / "sections" / "09-reporting-triage-integrations.md"


def test_golden_is_the_spec_sample_with_only_the_documented_differences():
    spec = SPEC_9.read_text(encoding="utf-8")
    block = spec.split("### 9.3", 1)[1].split("```markdown\n", 1)[1].split("\n```\n", 1)[0] + "\n"
    expected = "<!-- sentinel:pr:4812 -->\n" + block.replace("one census checkpoint per cell",
                                                           "one census checkpoint per surface")
    assert golden_text() == expected
