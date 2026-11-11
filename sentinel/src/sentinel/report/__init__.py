"""The Reporter's outputs: the PR comment and commit status (9.2) and the static run report (9.5).

The Reporter arranges records; it never computes or changes an outcome (9 preamble). For a finished run,
`ReportInputs.from_store(store, run_id).build(manifest, context)` reads its records, its `summary_json` and the
Safety rows, and `render_report_markdown` and `write_run_report` render the built report.
"""
from .html import render_run_report, render_run_report_html, write_run_report
from .model import (CommitStatus, ReportContext, ReportInputError, ReportInputs, RunReport, SafetyRows, build_report,
                    pending_status)
from .pr_comment import comment_marker, commit_status, render_pr_comment, render_report_markdown

__all__ = [
    "CommitStatus", "ReportContext", "ReportInputError", "ReportInputs", "RunReport", "SafetyRows", "build_report",
    "comment_marker", "commit_status", "pending_status", "render_pr_comment", "render_report_markdown",
    "render_run_report", "render_run_report_html", "write_run_report",
]
