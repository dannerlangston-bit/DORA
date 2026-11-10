"""Clustering (9.7) and Unexpected ranking (9.2)."""
from __future__ import annotations

from dataclasses import replace

from sentinel.records import (Classification, CheckpointOutcome, Component, Decision, DiffSummary, Finding,
                              RegionDiff, Signature, StepOutcome)
from sentinel.report import build_report, render_pr_comment
from sentinel.report.cluster import Member, cluster_members, hamming
from sentinel.report.inline import plain
from sentinel.report.model import COLLATERAL

from test_report_sample import AT, ENTRY, RUN_ID, sample

WIDTHS, HEIGHTS = (320, 480, 640, 960), (360, 480, 720)


def _o(cid: str, outcome: str = "collateral", tier: int | None = 1, **kw) -> CheckpointOutcome:
    kw.setdefault("workspace_id", "ws.tier-a.active-trader")
    return CheckpointOutcome(run_id=RUN_ID, checkpoint_id=cid, outcome=outcome, persona="active_trader",
                             data_mode="frozen", tier=tier, **kw)


def _f(cid: str | None, fid: str, kind: str = "collateral", tier: int | None = 1, region: str | None = "rgn.a",
       phash: str | None = "0000000000000000", **kw) -> Finding:
    kw.setdefault("first_seen_run_id", RUN_ID)
    return Finding(finding_id=fid, run_id=RUN_ID, kind=kind, description=f"{kind} at {cid}", checkpoint_id=cid,
                   tier=tier, region_id=region, phash=phash, **kw)


def _census_member(cid: str, phash: str, box=(10, 10, 100, 20), region="rgn.a", primitive="FlexibleGrid") -> Member:
    o = _o(cid, primitive=primitive,
           diff=DiffSummary(regions=[RegionDiff(region, 0.1, 0.9, True)], components=[Component(500, box, region)]))
    return Member(o, _f(cid, "F-" + phash[-4:], region=region, phash=phash), "collateral", COLLATERAL, region)


def _sweep_sample(decided: dict[str, str] | None = None) -> dict:
    """The 9.3 inputs with all 12 Ladder S steps `collateral` (no overlap), optionally some decided."""
    s = sample()
    decided = decided or {}
    s["outcomes"] = [o for o in s["outcomes"] if o.test_kind != "sweep"]
    s["findings"] = [f for f in s["findings"] if not f.checkpoint_id.startswith("sweep/")]
    cls = Classification("collateral", "rule", model_id="claude-pinned-2026-06", prompt_hash="7c1e2a",
                         description="Ladder viewport starts 6 px higher than base.")
    for i, (w, h) in enumerate((w, h) for w in WIDTHS for h in HEIGHTS):
        cid = f"sweep/{AT}/flexgrid/{w}x{h}"
        dec = decided.get(f"{w}x{h}")
        s["outcomes"].append(_o(cid, "noise" if dec else "collateral", reason=f"decision:{dec}" if dec else None,
                                decision_ref=dec, workspace_id="ws.tier-c.sweep-harness", primitive="FlexibleGrid",
                                manifest_entry_ref=ENTRY, classification=None if dec else cls, recaptures=2,
                                stability="stable", artifacts={"base": f"{i:04x}", "candidate": f"{i + 100:04x}"}))
        s["findings"].append(_f(cid, f"F-7{i:04d}", region="rgn.ladder.viewport", phash="0f1e2d3c4b5a6978",
                                primitive="FlexibleGrid", decision_ref=dec,
                                evidence={"layout": {"grid": "2x2", "cell": "(0,0)"}}))
    return s


def _item_headers(text: str) -> list[str]:
    return [line for line in text.splitlines() if line[:3].strip("*").split(".")[0].isdigit()]


# ---- 9.7 clustering -------------------------------------------------------------------------

def test_a_12_step_sweep_is_one_item_with_member_count():
    text = render_pr_comment(**_sweep_sample())
    headers = _item_headers(text)
    assert headers[0] == (f"**1. [Tier 1] collateral on `{AT}`, checkpoint `sweep/{AT}/flexgrid/640x360`, "
                          f"cluster of 12 checkpoints**")
    assert "All three heights at all four widths (12 of 12 steps)." in text
    assert "one call for the sweep cluster with the crops of its smallest and largest steps, 320x360 and 960x720" \
        in text
    assert "**Unexpected (3)**" in text  # the sweep cluster, the AT census and the tier-2 cluster


def test_census_rule_phash_within_6_and_boxes_within_4px():
    a = _census_member("census/charts.a/ws.x", "0000000000000000")
    b = _census_member("census/charts.b/ws.x", "000000000000003f", box=(14, 6, 96, 24))  # 6 bits, 4 px
    assert hamming("0000000000000000", "000000000000003f") == 6
    assert [len(c.members) for c in cluster_members([a, b])] == [2]
    far = _census_member("census/charts.c/ws.x", "000000000000007f")  # 7 bits
    assert sorted(len(c.members) for c in cluster_members([a, far])) == [1, 1]
    moved = _census_member("census/charts.d/ws.x", "0000000000000000", box=(15, 10, 100, 20))  # 5 px
    assert sorted(len(c.members) for c in cluster_members([a, moved])) == [1, 1]
    other_region = _census_member("census/charts.e/ws.x", "0000000000000000", region="rgn.b")
    other_primitive = _census_member("census/charts.f/ws.x", "0000000000000000", primitive="SplitPane")
    assert len(cluster_members([a, other_region, other_primitive])) == 3


def test_census_clusters_are_transitive_and_order_independent():
    a = _census_member("census/charts.a/ws.x", "0000000000000000")
    b = _census_member("census/charts.b/ws.x", "000000000000000f")   # 4 from a
    c = _census_member("census/charts.c/ws.x", "00000000000000ff")   # 4 from b, 8 from a
    for order in ([a, b, c], [c, a, b], [b, c, a]):
        assert [len(cl.members) for cl in cluster_members(order)] == [3]


def test_a_census_checkpoint_never_joins_a_sweep_cluster():
    s = _sweep_sample()
    clusters = cluster_members(build_report(**{k: v for k, v in s.items()}).unexpected[0].cluster.members)
    assert len(clusters) == 1 and clusters[0].kind == "sweep"
    report = build_report(**s)
    kinds = [(i.cluster.kind, len(i.cluster.members)) for i in report.unexpected]
    assert kinds == [("sweep", 12), ("census", 1), ("census", 2)]


def test_partly_suppressed_cluster_is_listed_with_k_of_n():
    s = _sweep_sample(decided={"320x360": "dec-000600", "480x360": "dec-000600", "640x360": "dec-000600"})
    sig = Signature(f"sweep/{AT}/flexgrid/320x360", "rgn.ladder.viewport", "0f1e2d3c4b5a6978", "collateral")
    s["decisions"].append(Decision("dec-000600", "noise_signature", "noise", sig, decided_by="k.osei",
                                   decided_at="2026-11-01T09:00:00+00:00"))
    text = render_pr_comment(**s)
    assert (f"**1. [Tier 1] collateral on `{AT}`, checkpoint `sweep/{AT}/flexgrid/480x720`, cluster of 12 "
            f"checkpoints, 3 of 12 members suppressed by decision dec-000600**") in text
    assert "**Suppressed by decision (4)**" in text
    assert text.count("| dec-000600 | noise_signature (noise) | k.osei, 2026-11-01 | |") == 3
    item = text.split("**1. ")[1].split("\n\n")[0]
    assert "act/confirm_bug?f=F-70005" in item  # 480x720: the lower median of the 9 unsuppressed members


def test_fully_suppressed_cluster_with_one_verdict_leaves_unexpected():
    every = {f"{w}x{h}": "dec-000600" for w in WIDTHS for h in HEIGHTS}
    s = _sweep_sample(decided=every)
    sig = Signature(f"sweep/{AT}/flexgrid/320x360", "rgn.ladder.viewport", "0f1e2d3c4b5a6978", "collateral")
    s["decisions"].append(Decision("dec-000600", "noise_signature", "noise", sig, decided_by="k.osei"))
    text = render_pr_comment(**s)
    assert "**Unexpected (2)**" in text and "**Suppressed by decision (13)**" in text
    assert "sweep/" not in text.split("**Suppressed by decision")[0]


def test_fully_suppressed_cluster_with_two_verdicts_stays_listed_but_decided():
    half = {f"{w}x{h}": ("dec-000600" if h == 360 else "dec-000601") for w in WIDTHS for h in HEIGHTS}
    s = _sweep_sample(decided=half)
    sig = Signature(f"sweep/{AT}/flexgrid/320x360", "rgn.ladder.viewport", "0f1e2d3c4b5a6978", "collateral")
    s["decisions"] += [Decision("dec-000600", "noise_signature", "noise", sig, decided_by="k.osei"),
                       Decision("dec-000601", "mark_intended", "intended", sig, decided_by="j.alvarez",
                                ticket="TOS-48120")]
    s["outcomes"] = [replace(o, outcome="intended") if o.decision_ref == "dec-000601" else o for o in s["outcomes"]]
    report = build_report(**s)
    first = report.unexpected[0]
    assert len(first.cluster.members) == 12 and first.decided
    assert "12 of 12 members suppressed by decision dec-000600 and dec-000601" in "".join(
        getattr(seg, "text", "") for seg in first.header)
    assert report.status.reason == "1 tier-1 finding has no decision"  # only the AT census item remains open


# ---- 9.2 ranking ----------------------------------------------------------------------------

def test_unexpected_ranking_follows_tier_then_kind_order_then_confidence_then_novelty():
    s = sample()
    s["outcomes"], s["findings"], s["decisions"] = [], [], []
    s["manifest"]["entries"][0]["declared_surfaces"] += ["charts.declared-new", "charts.declared"]

    def add(name: str, outcome: str, tier: int = 1, reason: str | None = None, kind: str | None = None,
            conf: float | None = None, seen: str = RUN_ID, **kw):
        cid = f"census/charts.{name}/ws.tier-a.active-trader"
        cls = Classification("needs_human", "reviewer", confidence=conf, model_id="m", prompt_hash="p") \
            if conf is not None else None
        s["outcomes"].append(_o(cid, outcome, tier=tier, reason=reason, classification=cls, **kw))
        s["findings"].append(_f(cid, f"F-{name}", kind or (outcome if outcome != "not_run" else "invariant_violation"),
                                tier=tier, region=f"rgn.{name}", first_seen_run_id=seen,
                                evidence={"reviewer": {"verdict": "partially_matches_manifest"}} if conf else {}))

    add("t2-invariant", "invariant_violation", tier=2, reason="INV-02", invariant_ids=["INV-02"])
    s["findings"] += [_f(None, "F-popup", "unknown_popup", region=None, phash=None),
                      _f(None, "F-manifest", "manifest_missing", region=None, phash=None),
                      _f(None, "F-gap", "dependency_map_gap", region=None, phash=None),
                      _f("flow/trade.ticket/flow.trade.ticket/s03", "F-loc1", "locator_failure", region=None,
                         phash=None),
                      _f("flow/trade.ticket/flow.trade.ticket/s04", "F-loc2", "locator_failure", region=None,
                         phash=None)]
    s["steps"] = [StepOutcome(RUN_ID, "flow.trade.ticket", "s04", "passed_via_agent_fallback", finding_ref="F-loc2"),
                  StepOutcome(RUN_ID, "flow.trade.ticket", "s03", "failed", finding_ref="F-loc1"),
                  StepOutcome(RUN_ID, "flow.trade.ticket", "s01", "passed")]
    add("declared-new", "needs_baseline", reason="new_surface")
    add("undeclared-new", "needs_baseline", reason="new_surface")
    add("uncertain-low", "needs_human", reason="reviewer_uncertain", conf=0.5)
    add("uncertain-high", "needs_human", reason="reviewer_uncertain", conf=0.7)
    add("unregistered", "unregistered_surface", reason="probe:P3")
    add("landmark-both", "landmark_missing", reason="landmark:hdr", side="both")
    add("landmark-cand", "landmark_missing", reason="landmark:hdr", side="candidate")
    add("declared", "needs_human", reason="reviewer_no_match", conf=0.9)
    add("uncalibrated", "needs_human", reason="uncalibrated")
    add("collateral-old", "collateral", seen="run-2026-10-30-pr-4700")
    add("collateral-new", "collateral")
    add("assertion", "assertion_failed", reason="assertion:qty-equals")
    add("base-failed", "not_run", reason="base_step_failed")
    add("invariant", "invariant_violation", reason="INV-01", invariant_ids=["INV-01"])
    add("crash", "assertion_failed", reason="assertion:app_alive", detail="jvm_crash")
    report = build_report(**s)
    order = [plain(i.header).split("] ", 1)[1] for i in report.unexpected]
    expected = [
        "assertion_failed (assertion: app_alive) on charts.crash",
        "invariant_violation (comparison not_run, reason: base_step_failed) on charts.base-failed",
        "invariant_violation on charts.invariant",
        "assertion_failed (assertion: qty-equals) on charts.assertion",
        "collateral on charts.collateral-new",       # uncalibrated ranks with collateral (9.2);
        "needs_human (reason: uncalibrated) on charts.uncalibrated",   # novel before seen-before
        "collateral on charts.collateral-old",
        "needs_human (reason: reviewer_no_match) on charts.declared",
        "landmark_missing (reason: landmark:hdr) on charts.landmark-cand",
        "unregistered_surface (reason: probe:P3) on charts.unregistered",
        "needs_human (reason: reviewer_uncertain) on charts.uncertain-high",   # confidence descending
        "needs_human (reason: reviewer_uncertain) on charts.uncertain-low",
        "needs_baseline (reason: new_surface) on charts.undeclared-new",
        "step failed on trade.ticket, flow flow.trade.ticket step s03",
        "step passed_via_agent_fallback on trade.ticket, flow flow.trade.ticket step s04",
        "dependency_map_gap",
        "manifest_missing",
        "unknown_popup",
        "invariant_violation on charts.t2-invariant",   # tier 2 after every tier-1 kind
    ]
    assert [o.split(", checkpoint")[0] for o in order] == expected
    text = render_pr_comment(**s)
    assert "charts.landmark-both" not in text.split("**Intended")[0]   # both sides: not compared, not Unexpected
    assert "1 checkpoints `needs_baseline` on declared new surfaces" in text
    assert "Check `sentinel/pr-run`: failure (informational). 12 tier-1 findings have no decision" in text
