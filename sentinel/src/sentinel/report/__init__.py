"""The Reporter's outputs: the PR comment and commit status (9.2) and the static run report (9.5).

The Reporter arranges records; it never computes or changes an outcome (9 preamble).
"""
from .html import render_run_report, render_run_report_html
from .model import CommitStatus, ReportContext, ReportInputError, RunReport, build_report, pending_status
from .pr_comment import comment_marker, commit_status, render_pr_comment, render_report_markdown

__all__ = [
    "CommitStatus", "ReportContext", "ReportInputError", "RunReport", "build_report", "comment_marker",
    "commit_status", "pending_status", "render_pr_comment", "render_report_markdown", "render_run_report",
    "render_run_report_html",
]
