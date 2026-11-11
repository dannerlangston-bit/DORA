"""The report's wired inputs (9.2, 9.4, 9.5, 9.6, 9.9, 9.10, 6.6, 11.7): triage notes on Unexpected items, alert
shards and acknowledgements, the reconciliation's flow-task words, the matrix cell tier, the 6.6 table order, the
assumed release gate on RC runs, the pending ticket note and the page sections built from the store."""
from __future__ import annotations

from dataclasses import replace

from sentinel.records import Decision
from sentinel.report import build_report, render_report_markdown
from sentinel.report.html import render_run_report_html
from sentinel.report.model import _proof_block, coverage_cell
from sentinel.store.store import PostRunProof
from sentinel.report.safety import TABLE_ROWS
from sentinel.report.sections import declared_surfaces

from test_report_sample import sample


def _report(**overrides):
    s = sample()
    s.update(overrides)
    return build_report(s["run"], s["outcomes"], s["findings"], s["decisions"], s["coverage"], s["health"],
                        s["manifest"], s["context"], summary=s.get("summary"))


def test_an_unexpected_item_shows_its_finding_triage_note():
    s = sample()

    findings = [replace(f, evidence={**f.evidence, "triage_note": "mask requested, PR 12"}) for f in s["findings"]]
    text = render_report_markdown(_report(findings=findings))
    assert "Triage note" in text and "mask requested, PR 12" in text


def test_an_alert_line_lists_its_shards_and_who_acknowledged_it():
    alert = {"rule": "env_error", "priority": "P2", "control": "gate", "gate": "launched", "detail": "x",
             "key": "run:env_error:launched", "shards": ["run/1", "run/2"]}
    summary = {"alerts": [alert], "alert_acks": [{"key": "run:env_error:launched", "by": "bkhan",
                                                  "at": "2026-11-10T15:05:00+00:00", "via": "paging_service"}]}
    rep = _report(summary=summary)
    (line,) = rep.alerts
    text = "".join(getattr(p, "text", "") for p in line)
    assert "shards run/1, run/2" in text and "acknowledged by bkhan at 2026-11-10T15:05:00+00:00" in text


def test_the_matrix_cell_keeps_its_tier_and_the_proof_rows_keep_the_6_6_order():
    assert coverage_cell({"surface_id": "s", "persona": "p", "profile": "cash", "state": "covered", "tier": 1,
                          "checkpoint_count": 2})["tier"] == 1
    counts = {"rows": {name: {"start": 0, "end": 0, "new": 0, "expected": 0, "verdict": "ok"}
                       for name in sorted(TABLE_ROWS)}}
    p = PostRunProof("run-1/1", "run-1", "login-a", "proven_no_sends", "OPEN", counts, "not_needed", 4, 0, 10, 0,
                     "2026-11-10T15:00:00+00:00")
    _, lines = _proof_block(p)
    names = [ln[0].text.split(":")[0] for ln in lines if ln[0].text.split(":")[0] in TABLE_ROWS]
    assert names == list(TABLE_ROWS)


def test_an_rc_run_prints_the_assumed_release_gate_and_a_pr_run_does_not():
    note = "release gate assumed: N = 5 business days and an RC cut at 09:00 (policy/release_gate.yaml is missing)"
    s = sample()
    rc_run = replace(s["run"], run_type="rc")
    rc = render_report_markdown(_report(run=rc_run, context=replace(s["context"], release_gate_note=note)))
    pr = render_report_markdown(_report(context=replace(s["context"], release_gate_note=note)))
    assert note in rc and note not in pr


def test_a_suppressing_confirm_bug_without_a_ticket_shows_the_pending_note():
    s = sample()
    d = s["decisions"][0]
    bug = Decision(d.decision_id, "confirm_bug", "confirmed_bug", d.signature, decided_by="j.alvarez",
                   decided_at=d.decided_at)
    rep = build_report(s["run"], s["outcomes"], s["findings"], [bug], s["coverage"], s["health"], s["manifest"],
                       s["context"], ticket_notes={d.decision_id: "ticket pending (Jira unreachable since 10:02 ET)"})
    assert "ticket pending (Jira unreachable since 10:02 ET)" in render_report_markdown(rep)
    assert "ticket pending (Jira unreachable since 10:02 ET)" in render_run_report_html(rep)


def test_the_declared_surfaces_of_a_manifest():
    assert declared_surfaces(sample()["manifest"]) == frozenset({"charts.flexible-grid.chart-cell"})
    assert declared_surfaces(None) == frozenset()
