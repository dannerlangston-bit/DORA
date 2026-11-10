"""PR comment (9.2): every run state of the table, banners, no-manifest lines, escaping and size."""
from __future__ import annotations

import re
from dataclasses import replace

import pytest

from sentinel.records import Classification, CheckpointOutcome, Finding, RunOutcome
from sentinel.report import ReportContext, comment_marker, render_pr_comment
from sentinel.report.model import ReportInputError
from sentinel.report.pr_comment import ZWSP

from test_report_sample import AT, RUN_ID, render, sample

BASE = "https://sentinel.internal"


def _run(status: str, reason: str | None = None, last_gate: str = "reported") -> RunOutcome:
    return RunOutcome(run_id=RUN_ID, run_type="pr", status=status, reason=reason, last_gate=last_gate)


def _ctx(**kw) -> ReportContext:
    return replace(sample()["context"], **kw)


def _links(text: str) -> list[str]:
    """Targets of every markdown link or image that can form (an escaped `\\]` cannot)."""
    return re.findall(r"(?<!\\)\]\(([^)\s]*)\)", text)


def _extra_outcome(cid: str, outcome: str, **kw) -> CheckpointOutcome:
    kw.setdefault("workspace_id", "ws.tier-a.active-trader")
    kw.setdefault("persona", "active_trader")
    kw.setdefault("data_mode", "frozen")
    return CheckpointOutcome(run_id=RUN_ID, checkpoint_id=cid, outcome=outcome, **kw)


def test_marker_is_the_first_line_and_stable():
    text = render()
    assert text.splitlines()[0] == comment_marker(4812) == "<!-- sentinel:pr:4812 -->"
    assert render() == text


def test_every_link_is_a_report_ui_url():
    assert all(u.startswith(BASE + "/") for u in _links(render()))


# ---- run states (9.2 table) -------------------------------------------------------------------

def test_complete_with_pending_review_lists_pending_items_and_edits_in_place():
    s = sample()
    cid = "census/trade.active-trader.ladder/ws.tier-a.active-trader"
    s["outcomes"] = [o for o in s["outcomes"] if o.checkpoint_id != cid]
    s["outcomes"].append(_extra_outcome(cid, "needs_human", reason="reviewer_unavailable", review_state="pending",
                                        tier=1, artifacts={"base": "ab12", "candidate": "cd34"}))
    s["findings"].append(Finding("F-30001", RUN_ID, "needs_human", "awaiting review", RUN_ID, checkpoint_id=cid,
                                 tier=1, region_id="rgn.ladder.price", phash="0123456789abcdef",
                                 evidence={"reviewer": {"pending_since": "10:58 ET", "batch": "rb-77"}}))
    text = render_pr_comment(**s)
    assert "Result: **complete (unreviewed: 1 item pending review)** | 5 unexpected" in text
    assert "Reviewer pending: 1 item; this comment is edited in place when verdicts land" in text
    assert "needs_human (review_state: pending) on `trade.active-trader.ladder`" in text
    assert "- What moved: Reviewer: pending since 10:58 ET, batch rb-77" in text
    item = text.split("needs_human (review_state: pending)")[1].split("\n\n")[0]
    assert "- Reviewer: pending" in item
    assert "act/confirm_bug?f=F-30001" in item and "act/mark_intended?f=F-30001" in item \
        and "act/request_mask?f=F-30001" in item
    assert "Check `sentinel/pr-run`: failure (informational). 4 tier-1 findings have no decision" in text
    # the pending tier-1 item ranks 4th, so the tier-2 cluster is now item 5
    assert "| Reviewer | 4 calls (items 2, 3 and 5 and the intended chart cell), 1 pending, 0 unavailable" in text


def test_complete_with_env_errors_names_gates_shards_and_not_compared():
    s = sample()
    s["run"] = _run("complete_with_env_errors")
    s["outcomes"].append(_extra_outcome("census/monitor.account-statement/ws.tier-a.active-trader", "env_error",
                                        gate="fixture_verified", reason="fixture_drift", tier=1))
    s["coverage"]["tiers"][1]["not_compared"] = {"env_error": 1}
    s["coverage"]["tiers"][1]["covered"] = 13
    s["health"]["env_errors"] = [{"gate": "fixture_verified", "shard": 2, "screenshot": "beef01"}]
    s["health"]["shard_rerun_queued"] = True
    text = render_pr_comment(**s)
    assert "Result: **complete_with_env_errors** |" in text
    assert ("| Environment errors | fixture_verified on shard 2 ([screenshot](https://sentinel.internal/a/beef01)); "
            "shard re-run queued |") in text
    assert "| env_error / harness_error | 1 (fixture_verified) / 0 |" in text
    assert "| 1 | 41 | 20 | 13 (100 percent of comparable scope) | 1 (env_error: 1) |" in text
    assert "**Unexpected (4)**" in text and "**Coverage**" in text


def test_unproven_proof_blocks_the_login_until_a_human_verifies():
    s = sample()
    s["run"] = _run("complete_with_env_errors", "unproven_proof")
    s["health"]["safety"].update(proof="UNPROVEN: session_unavailable", login="paper-07")
    text = render_pr_comment(**s)
    assert ("proof UNPROVEN: session_unavailable; next run on login paper-07 blocked until a human verifies "
            "the account |") in text


def test_no_session_established_blocks_nothing():
    s = sample()
    s["run"] = _run("complete_with_env_errors", "unproven_proof")
    s["health"]["safety"]["proof"] = "PROVEN: no session established"
    text = render_pr_comment(**s)
    assert "proof PROVEN: no session established |" in text and "blocked until" not in text


def test_aborted_budget_shows_what_ran_and_the_unfinished_scope():
    s = sample()
    s["run"] = _run("aborted_budget", "wall_clock", "running")
    for i, tier in enumerate((1, 1, 2)):
        s["outcomes"].append(_extra_outcome(f"census/monitor.s{i}/ws.tier-a.active-trader", "not_run",
                                            reason="budget", tier=tier))
    text = render_pr_comment(**s)
    assert "Result: **aborted_budget (reason: wall_clock)** | 4 unexpected" in text
    assert "Scope not finished: 3 checkpoints not_run (budget), 2 of them tier 1" in text
    assert "**Unexpected (4)**" in text and "**Intended (1)**" in text and "**Run health**" in text
    assert "failure (informational)" in text  # findings are triaged as usual


def test_aborted_budget_dollar_cap_names_the_spend_ceiling():
    s = sample()
    s["run"] = _run("aborted_budget", "dollar_cap", "running")
    s["context"] = _ctx(abort={"cost": "2.00 USD", "cap": "2.00 USD"})
    text = render_pr_comment(**s)
    assert ("Scope not finished: 0 checkpoints not_run (budget), 0 of them tier 1; per-run model spend ceiling "
            "reached (2.00 USD of 2.00 USD, 11.4 Cost model)") in text


def test_aborted_budget_pool_saturated_is_the_abort_block_alone():
    s = sample()
    s["run"] = _run("aborted_budget", "pool_saturated", "provisioned")
    s["outcomes"], s["findings"] = [], []
    s["context"] = _ctx(abort={"runs_ahead": 3})
    text = render_pr_comment(**s)
    assert "Sentinel did not run: pool saturated, 3 runs ahead; push again or re-run the build job" in text
    assert "Check `sentinel/pr-run`: error (informational). Sentinel did not run" in text
    for absent in ("**Unexpected", "**Intended", "**Coverage**", "**Run health**"):
        assert absent not in text


def test_aborted_safety_replaces_findings_with_the_abort_block():
    s = sample()
    s["run"] = _run("aborted_safety", "kill_switch", "running")
    s["context"] = _ctx(abort={"trigger": "kill switch", "gate": "running", "screenshot": "5afe01",
                               "incident": "INC-2026-031", "rig": "rig-2"})
    s["health"]["safety"]["proof"] = "PROVEN: no orders sent"
    text = render_pr_comment(**s)
    assert "Result: **aborted_safety (reason: kill_switch)**" in text
    block = text.split("**Abort**\n", 1)[1].split("\n\n", 1)[0]
    assert block.splitlines() == [
        "- Trigger: kill switch",
        "- Gate: running",
        "- Last screenshot: [screenshot](https://sentinel.internal/a/5afe01)",
        "- Post-run proof: PROVEN: no orders sent",
        "- Incident: INC-2026-031",
        "- No run starts on rig rig-2 until the incident closes.",
        "- 15 findings recorded before the abort, collapsed on the "
        "[run page](https://sentinel.internal/runs/run-2026-11-09-pr-4812#unexpected)",
    ]
    assert "**Unexpected" not in text and "**Intended" not in text
    assert "**Suppressed by decision (1)**" in text and "**Coverage**" in text and "| Safety |" in text
    assert "Check `sentinel/pr-run`: error (informational). aborted_safety: incident INC-2026-031" in text


def test_aborted_harness_names_reason_gate_and_requeue():
    s = sample()
    s["run"] = _run("aborted_harness", "config_invalid", "provisioned")
    s["context"] = _ctx(abort={"rule_ids": ["L-MAN-03", "L-FLW-02"], "requeued": "run-2026-11-09-pr-4812-r1"})
    text = render_pr_comment(**s)
    block = text.split("**Abort**\n", 1)[1].split("\n\n", 1)[0]
    assert block.splitlines() == [
        "- Reason: `config_invalid` (rules L-MAN-03, L-FLW-02)",
        "- Last gate reached: provisioned",
        "- Re-queued once: run-2026-11-09-pr-4812-r1",
    ]
    assert "error (informational). aborted_harness: config_invalid" in text
    s["context"] = _ctx(abort={})
    assert "- Re-queued once: already re-queued" in render_pr_comment(**s)


def test_aborted_harness_preflight_reason():
    s = sample()
    s["run"] = _run("aborted_harness", "preflight.H8", "provisioned")
    text = render_pr_comment(**s)
    assert "- Reason: `preflight.H8`" in text and "aborted_harness: preflight.H8" in text


def test_dead_man_comment_is_written_by_the_reporter_host():
    s = sample()
    s["run"] = _run("aborted_harness", "dead_man", "running")
    s["outcomes"], s["findings"] = [], []
    s["context"] = _ctx(abort={"deadline": "11:16 ET", "last_heartbeat": "10:49 ET"})
    text = render_pr_comment(**s)
    assert "Sentinel did not report: no run-completed event by 11:16 ET; last heartbeat 10:49 ET" in text
    assert "Check `sentinel/pr-run`: error (informational). Sentinel did not report" in text
    assert "**Coverage**" not in text


def test_secret_leak_withholds_the_report():
    s = sample()
    s["run"] = _run("aborted_harness", "secret_leaked", "teardown")
    text = render_pr_comment(**s)
    assert text.splitlines() == [
        "<!-- sentinel:pr:4812 -->",
        "**Sentinel PR run run-2026-11-09-pr-4812: branch-1998.3.0-7be42d0 vs main-1998.3.0-2c91ae4**",
        "Result: **aborted_harness (reason: secret_leaked)**",
        "Check `sentinel/pr-run`: error (informational). Sentinel did not report",
        "report withheld; security contact notified",
    ]


# ---- banners and manifest lines ----------------------------------------------------------------

def test_detector_banner_is_first_and_reviewer_and_global_shift_lines_follow():
    s = sample()
    banner = "DETECTOR BLIND since 2026-11-09 03:12 ET.\nMissed 1 of 24 money-cell mutants.\nRC sign-off is blocked."
    s["context"] = _ctx(detector_banner=banner, reviewer_disabled={"since": "2026-11-02", "gate": "intended_precision"})
    s["health"]["canary"]["detector_state"] = "detector_blind"
    for i in range(3):
        cid = f"census/monitor.g{i}/ws.tier-a.active-trader"
        s["outcomes"].append(_extra_outcome(cid, "needs_human", reason="global_shift", tier=2))
    s["findings"].append(Finding("F-40000", RUN_ID, "global_rendering_shift", "text edges moved", RUN_ID))
    lines = render_pr_comment(**s).splitlines()
    assert lines[1:6] == ["```text", *banner.splitlines(), "```"]
    assert lines[6] == "Reviewer disabled since 2026-11-02: eval gate intended_precision failed; 0 items await a human"
    assert lines[7] == ("Global rendering shift: 3 checkpoints collapsed into one `needs_human (reason: global_shift)` "
                        "cluster; no reviewer calls made")
    assert lines[8].startswith("**Sentinel PR run ")
    text = "\n".join(lines)
    assert text.count("needs_human (reason: global_shift) on") == 1
    assert "cluster of 3 checkpoints" in text
    assert "detector state detector_blind" in text


def test_detector_state_without_text_still_raises_a_banner():
    s = sample()
    s["health"]["canary"].update(detector_state="generation_failed", since="2026-11-09 02:40 ET")
    lines = render_pr_comment(**s).splitlines()
    assert lines[1:4] == ["```text", "GENERATION FAILED since 2026-11-09 02:40 ET.", "```"]


def test_no_manifest_and_ui_code_touched():
    s = sample()
    s["manifest"] = None
    s["findings"].append(Finding("F-50000", RUN_ID, "manifest_missing", "UI code touched with no manifest entry",
                                 RUN_ID, tier=1))
    text = render_pr_comment(**s)
    intended = text.split("**Intended (")[1].splitlines()
    assert intended[1] == ("No manifest entry and the PR touches UI code (`manifest_missing`): every diff is reported "
                           "as collateral until you add one")
    assert "] manifest_missing" in text  # the finding itself is ranked in Unexpected


def test_no_manifest_and_no_ui_code():
    s = sample()
    s["manifest"] = None
    text = render_pr_comment(**s)
    assert text.split("**Intended (")[1].splitlines()[1] == \
        "No manifest and no UI code touched: diffs are `needs_human (reason: no_manifest)`"


def test_manifest_reconciliation_not_observed_and_no_covering_checkpoint():
    s = sample()
    s["manifest"]["entries"] += [
        {"entry_id": "tos-48120-2", "tickets": ["TOS-48121"], "summary": "Ladder font",
         "declared_surfaces": ["trade.active-trader.ladder"]},
        {"entry_id": "tos-48120-3", "tickets": ["TOS-48122"], "summary": "New gadget",
         "declared_surfaces": ["charts.new-gadget"]},
    ]
    s["coverage"]["flow_tasks"] = {"tos-48120-3": "SENT-912"}
    text = render_pr_comment(**s)
    assert ("Manifest reconciliation: tos-48120-1: verified visible on 2 checkpoints; tos-48120-2: covering "
            "checkpoints `census/trade.active-trader.ladder/ws.tier-a.active-trader` showed no change above floor "
            "(`intended_change_not_observed`; a question for jdoe and the feature owner, not a bug); tos-48120-3: no "
            "covering checkpoint; flow-authoring task SENT-912 opened") in text


# ---- escaping (9.1) ---------------------------------------------------------------------------

def test_screen_and_model_text_cannot_inject_markup():
    s = sample()
    evil = ("![pwn](https://evil.example/x.png) [click](https://evil.example) <img src=x> **bold** `code` "
            "@octocat #12 www.evil.example _em_ ~~strike~~ | pipe")
    s["manifest"]["entries"][0]["summary"] = evil
    s["outcomes"] = [replace(o, classification=replace(o.classification, description=evil))
                     if o.classification and o.classification.description else o for o in s["outcomes"]]
    text = render_pr_comment(**s)
    assert all(u.startswith(BASE + "/") for u in _links(text))
    for bad in ("<img", "![pwn]", "[click](", "**bold**", "`code`", "@octocat", "#12", "www.evil", "_em_",
                "~~strike~~", "https://evil"):
        assert bad not in text, bad
    assert "![pwn\\](https:/" + ZWSP + "/evil.example/x.png)" in text  # printed literally, no image forms
    assert "&lt;img src=x&gt;" in text and "\\*\\*bold\\*\\*" in text


def test_table_cells_escape_pipes():
    s = sample()
    s["decisions"] = [replace(s["decisions"][0], decided_by="a|b", ticket="T|1")]
    text = render_pr_comment(**s)
    assert "| a\\|b, 2026-10-21 | T\\|1 |" in text


# ---- size (9.1) -------------------------------------------------------------------------------

def _many_items(n: int) -> dict:
    s = sample()
    for i in range(n):
        cid = f"census/monitor.panel-{i:02d}/ws.tier-a.active-trader"
        s["outcomes"].append(_extra_outcome(cid, "collateral", tier=2, primitive="FlexibleGrid",
                                            manifest_entry_ref="tos-48120-1",
                                            classification=Classification("collateral", "rule"),
                                            artifacts={"base": "aa00", "candidate": "bb00"}))
        s["findings"].append(Finding(f"F-6{i:04d}", RUN_ID, "collateral", "moved " * 40, RUN_ID, checkpoint_id=cid,
                                     tier=2, region_id=f"rgn.p{i}", phash=f"{i:016x}"))
    s["context"] = _ctx(history=tuple({"run_id": f"run-old-{i}", "candidate": "branch-x", "result": "complete",
                                       "counts": {"unexpected": 3}, "reported": "10:00 ET"} for i in range(40)))
    return s


def test_comment_shrinks_in_the_9_1_order():
    s = _many_items(30)
    full = render_pr_comment(**s, limit_chars=10**9)
    assert "**Unexpected (34)**" in full and "... and 24 more in the [full report]" in full  # 10 inline
    assert "<details><summary>Run history</summary>" in full
    for limit, inline in ((len(full) - 1, 5), (len(full) // 2, 3)):
        text = render_pr_comment(**s, limit_chars=limit)
        assert f"... and {34 - inline} more in the [full report]" in text
    tiny = render_pr_comment(**s, limit_chars=1000)
    assert "1 finding matched an earlier decision: [listed in the full report]" in tiny
    assert "[Run history](https://sentinel.internal/runs/run-2026-11-09-pr-4812#history)" in tiny
    for kept in ("**Sentinel PR run", "Result: **", "Check `sentinel/pr-run`", "**Coverage**", "| Safety |"):
        assert kept in tiny


def test_synthetic_outcomes_never_appear():
    s = sample()
    cid = "sweep/charts.flexible-grid.active-trader-cell/flexgrid/480x360"
    s["outcomes"].append(replace(next(o for o in s["outcomes"] if o.checkpoint_id == cid),
                                 checkpoint_id=f"census/{AT}/ws.canary", test_kind="census", synthetic=True,
                                 workspace_id="ws.canary"))
    assert render_pr_comment(**s) == render()


def test_unknown_check_mode_is_refused():
    s = sample()
    s["context"] = _ctx(check_mode="advisory")
    with pytest.raises(ReportInputError):
        render_pr_comment(**s)
