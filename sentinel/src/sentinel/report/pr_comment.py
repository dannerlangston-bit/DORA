"""The PR comment of 9.2, rendered from the run's records (9.1, 9.2, 9.3, 9.7, 9.11).

`render_pr_comment` returns the whole comment. Its first line is the hidden marker
`<!-- sentinel:pr:{pr} -->` (9.1) by which the Reporter finds and edits the one comment of a PR
in place; `comment_marker` gives it alone. Section labels are bold lines, not headings (9.1).

Escaping (9.1, 5.13). Every `Text` segment is data: `&`, `<` and `>` are HTML-escaped; the
markdown punctuation that can start a link, image, code span, emphasis, strikethrough or table
cell is backslash-escaped (`_` only at a word boundary, so `invariant_violation` stays readable);
`@` mentions, `#` references, bare URLs and `www.` autolinks are broken with a zero-width space;
newlines collapse to spaces so no text can begin a heading or a list. The only links are the
`Link` and `Image` segments built from report UI URLs (`inline.Urls`).

Size (9.1). GitHub caps a comment at 65,536 characters; the comment is rendered, measured and
shrunk in this order until it fits: inline Unexpected and Intended items 10, then 5, then 3;
the Suppressed table collapses to a count and a link; the run history collapses to a link.
"""
from __future__ import annotations

import re

from ..records import CheckpointOutcome, Decision, Finding, RunOutcome, StepOutcome
from .inline import Code, Image, Line, Link, Text
from .model import (FIELD_EVIDENCE, CommitStatus, Evidence, ReportContext, RunReport, build_report,
                    commit_status, pending_status)

__all__ = ["render_pr_comment", "render_report_markdown", "comment_marker", "commit_status",
           "pending_status", "CommitStatus", "ReportContext", "GITHUB_COMMENT_LIMIT", "md_text"]

GITHUB_COMMENT_LIMIT = 65_536
INLINE_LIMITS = (10, 5, 3)
_ZWSP = "​"
_ESCAPE = set("\\`*[]|~")
_AUTOLINK = re.compile(r"(?i)(://|www\.)")


def comment_marker(pr: int) -> str:
    """The hidden marker that identifies the PR's one Sentinel comment (9.1)."""
    return f"<!-- sentinel:pr:{int(pr)} -->"


def md_text(s: str) -> str:
    """Render screen or model text as inert markdown text (9.1)."""
    s = " ".join(str(s).split())
    out = []
    n = len(s)
    for i, ch in enumerate(s):
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch in _ESCAPE:
            out.append("\\" + ch)
        elif ch == "_":
            inner = 0 < i < n - 1 and s[i - 1].isalnum() and s[i + 1].isalnum()
            out.append("_" if inner else "\\_")
        elif ch in "@#":
            out.append(ch + _ZWSP)
        else:
            out.append(ch)
    return _AUTOLINK.sub(lambda m: m.group(1)[:-1] + _ZWSP + m.group(1)[-1], "".join(out))


def md_code(s: str) -> str:
    """A code span whose content cannot close it or break a table row."""
    body = " ".join(str(s).split()).replace("`", "'").replace("|", "\\|")
    return f"`{body}`"


def _md(line: Line) -> str:
    out = []
    for seg in line:
        if isinstance(seg, Text):
            out.append(md_text(seg.text))
        elif isinstance(seg, Code):
            out.append(md_code(seg.text))
        elif isinstance(seg, Link):
            out.append(f"[{md_text(seg.label)}]({seg.url})")
        elif isinstance(seg, Image):
            out.append(f"![{md_text(seg.alt)}]({seg.url})")
    return "".join(out)


def _evidence(ev: Evidence) -> str:
    parts = [_md((img,)) for img in (ev.base, ev.candidate) if img is not None]
    if ev.checkpoint_url:
        parts.append(f"[heatmap, flicker]({ev.checkpoint_url})")
    text = " ".join(parts) or "no crops recorded"
    if ev.note:
        text += f" ({md_text(ev.note)})"
    return text


def _fields(fields) -> list[str]:
    out = []
    for label, value in fields:
        if label == FIELD_EVIDENCE and isinstance(value, Evidence):
            out.append(f"- {label}: {_evidence(value)}")
        else:
            out.append(f"- {label}: {_md(value)}")
    return out


def _row(cells: list[str]) -> str:
    """A table row; an empty cell renders as `| |`, as in 9.3."""
    return "|" + "|".join(f" {c} " if c else " " for c in cells) + "|"


def _fence(text: str) -> list[str]:
    ticks = max([len(m) for m in re.findall(r"`+", text)] + [2]) + 1
    return ["`" * ticks + "text", *text.splitlines(), "`" * ticks]


def _header(r: RunReport, with_counts: bool = True) -> list[str]:
    ctx, run = r.context, r.run
    lines = [comment_marker(ctx.pr)]
    if r.banner:
        lines += _fence(r.banner)
    lines += [_md(b) for b in r.banner_lines]
    lines.append(f"**Sentinel PR run {md_text(run.run_id)}: {md_text(ctx.candidate_build)} vs "
                 f"{md_text(ctx.base_build)}**")
    result = f"Result: **{md_text(r.result)}**"
    if with_counts:
        result += (f" | {r.n_unexpected} unexpected | {r.n_intended} intended | {r.n_suppressed} suppressed "
                   f"by decision | [Full report]({r.urls.report})")
    lines.append(result)
    st = r.status
    lines.append(f"Check `{st.context}`: {st.state} ({st.mode}). {md_text(st.reason)}")
    if r.review_line:
        lines.append(_md(r.review_line))
    return lines


def _abort(r: RunReport) -> list[str]:
    if not r.abort_lines:
        return []
    if r.abort_title:
        return ["", f"**{r.abort_title}**", *[f"- {_md(x)}" for x in r.abort_lines]]
    return [_md(x) for x in r.abort_lines]


def _unexpected(r: RunReport, limit: int) -> list[str]:
    lines = ["", f"**Unexpected ({r.n_unexpected})**"]
    for i, item in enumerate(r.unexpected[:limit]):
        if i:
            lines.append("")
        lines.append(f"**{_md(item.header)}**")
        lines += _fields(item.fields)
    more = r.n_unexpected - limit
    if more > 0:
        lines.append(f"... and {more} more in the [full report]({r.urls.anchor('unexpected')})")
    return lines


def _intended(r: RunReport, limit: int) -> list[str]:
    lines = ["", f"**Intended ({r.n_intended})**"]
    if r.manifest_line:
        lines.append(_md(r.manifest_line))
    for i, item in enumerate(r.intended[:limit]):
        if i:
            lines.append("")
        lines.append(f"**{_md(item.header)}**")
        lines += _fields(item.fields)
    more = r.n_intended - limit
    if more > 0:
        lines.append(f"... and {more} more in the [full report]({r.urls.anchor('intended')})")
    if r.reconciliation:
        lines.append(_md(r.reconciliation))
    if r.needs_baseline_line:
        lines.append(_md(r.needs_baseline_line))
    return lines


def _suppressed(r: RunReport, collapsed: bool) -> list[str]:
    n = r.n_suppressed
    lines = ["", f"**Suppressed by decision ({n})**"]
    noun = "finding" if n == 1 else "findings"
    if n == 0:
        return lines + ["0 findings matched an earlier decision"]
    if collapsed:
        return lines + [f"{n} {noun} matched an earlier decision: [listed in the full report]"
                        f"({r.urls.anchor('suppressed')})"]
    lines += [f"<details><summary>{n} {noun} matched an earlier decision</summary>", "",
              "| Checkpoint | Region | Decision | Action (verdict) | By | Ticket |",
              "|---|---|---|---|---|---|"]
    for row in r.suppressed:
        d: Decision | None = row.decision
        action = f"{d.action} ({d.verdict})" if d else "decision record not loaded"
        by = f"{md_text(d.decided_by)}, {md_text(d.decided_at[:10])}" if d else ""
        lines.append(_row([md_code(row.checkpoint_id), md_code(row.region) if row.region else "",
                           md_text(row.decision_id), action, by, md_text(d.ticket) if d and d.ticket else ""]))
    lines.append("</details>")
    return lines


def _coverage(r: RunReport) -> list[str]:
    lines = ["", "**Coverage**", "| Tier | Registered | In scope | Covered | Not compared | not_run |",
             "|---|---|---|---|---|---|"]
    lines += [_row(list(row)) for row in r.coverage_rows]
    return lines + ["", _md(r.scope_line)]


def _health(r: RunReport) -> list[str]:
    lines = ["", "**Run health**", "| Item | Value |", "|---|---|"]
    return lines + [_row([label, _md(value)]) for label, value in r.health_rows]


def _history(r: RunReport, collapsed: bool) -> list[str]:
    if not r.history:
        return []
    if collapsed:
        return ["", f"[Run history]({r.urls.anchor('history')})"]
    lines = ["", "<details><summary>Run history</summary>", "",
             "| Run | Candidate | Result | Counts | Reported |", "|---|---|---|---|---|"]
    for h in r.history:
        counts = h.get("counts", "")
        if isinstance(counts, dict):
            counts = ", ".join(f"{v} {k}" for k, v in counts.items())
        lines.append(_row([md_text(h.get("run_id", "")), md_text(h.get("candidate", "")),
                           md_text(h.get("result", "")), md_text(counts), md_text(h.get("reported", ""))]))
    return lines + ["</details>"]


def _render(r: RunReport, limit: int, collapse_suppressed: bool, collapse_history: bool) -> str:
    if r.special == "withheld":
        lines = _header(r, with_counts=False) + _abort(r)
    elif r.special in ("pool_saturated", "dead_man"):
        lines = _header(r) + _abort(r)
    else:
        lines = _header(r) + _abort(r)
        if r.show_findings:
            lines += _unexpected(r, limit) + _intended(r, limit)
        lines += _suppressed(r, collapse_suppressed) + _coverage(r) + _health(r)
        lines += _history(r, collapse_history)
    return "\n".join(lines) + "\n"


def render_report_markdown(report: RunReport, limit_chars: int = GITHUB_COMMENT_LIMIT) -> str:
    """Render a built report, shrinking in the order of 9.1 until it fits `limit_chars`."""
    steps = [(n, False, False) for n in INLINE_LIMITS] + [(INLINE_LIMITS[-1], True, False),
                                                          (INLINE_LIMITS[-1], True, True)]
    text = ""
    for limit, sup, hist in steps:
        text = _render(report, limit, sup, hist)
        if len(text) <= limit_chars:
            return text
    return text


def render_pr_comment(run: RunOutcome, outcomes: list[CheckpointOutcome], findings: list[Finding],
                      decisions: list[Decision], coverage: dict | None, health: dict | None,
                      manifest: dict | None, context: ReportContext, *, steps: list[StepOutcome] = (),
                      limit_chars: int = GITHUB_COMMENT_LIMIT) -> str:
    """The 9.2 PR comment for one run, marker first, edited in place by later runs (9.1).

    `manifest` is the loaded change manifest (4.3) as a plain dict, or None when no manifest
    was loaded; `coverage` and `health` are the plain dicts described in `report.model`.
    """
    report = build_report(run, outcomes, findings, decisions, coverage, health, manifest, context, steps)
    return render_report_markdown(report, limit_chars)

