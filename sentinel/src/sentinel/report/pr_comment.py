"""The PR comment of 9.2, rendered from the run's records (9.1, 9.2, 9.3, 9.7, 9.11).

`render_pr_comment` returns the whole comment. Its first line is the hidden marker
`<!-- sentinel:pr:{pr} -->` (9.1) by which the Reporter finds and edits the one comment of a PR
in place; `comment_marker` gives it alone. Section labels are bold lines, not headings (9.1).

Escaping (9.1, 5.13). Every `Text` segment is data: `&`, `<` and `>` are HTML-escaped (no raw
HTML, no `<url>` autolink); backslash, backtick, `*` and `~` are backslash-escaped (no code span,
emphasis or strikethrough), and `_` too at a word boundary (so `invariant_violation` stays
readable); `]` is escaped where it could close a link, image or reference definition (before
`(`, `[` or `:`, or at the end of a text run), which leaves `[Tier 1]` as 9.3 prints it; `|` is
escaped inside table cells; `@` mentions, `#` references, bare URLs and `www.` autolinks are
broken with a zero-width space; whitespace runs collapse to one space, so no text can begin a
heading or a list. Adjacent `Text` segments are escaped as one run, so no pattern can straddle
two of them. The only links are the `Link` and `Image` segments built from report UI URLs.

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
           "pending_status", "CommitStatus", "ReportContext", "GITHUB_COMMENT_LIMIT", "md_text", "md_code"]

GITHUB_COMMENT_LIMIT = 65_536
INLINE_LIMITS = (10, 5, 3)
ZWSP = chr(0x200B)
_BACKSLASHED = set("\\`*~")
_AUTOLINK = re.compile(r"(?i)(://|www\.)")
_WS = re.compile(r"\s+")


def comment_marker(pr: int) -> str:
    """The hidden marker that identifies the PR's one Sentinel comment (9.1)."""
    return f"<!-- sentinel:pr:{int(pr)} -->"


def md_text(s: str, table: bool = False) -> str:
    """Render screen or model text as inert markdown text (9.1); `table` also escapes `|`."""
    s = _WS.sub(" ", str(s))
    out = []
    n = len(s)
    for i, ch in enumerate(s):
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ch in _BACKSLASHED or (ch == "|" and table):
            out.append("\\" + ch)
        elif ch == "]":
            out.append("\\]" if i == n - 1 or s[i + 1] in "([:" else "]")
        elif ch == "_":
            inner = 0 < i < n - 1 and s[i - 1].isalnum() and s[i + 1].isalnum()
            out.append("_" if inner else "\\_")
        elif ch in "@#":
            out.append(ch + ZWSP)
        else:
            out.append(ch)
    return _AUTOLINK.sub(lambda m: m.group(1)[:-1] + ZWSP + m.group(1)[-1], "".join(out))


def md_code(s: str, table: bool = False) -> str:
    """A code span whose content cannot close it (nor, in a table, end the cell)."""
    body = _WS.sub(" ", str(s)).replace("`", "'")
    return "`" + (body.replace("|", "\\|") if table else body) + "`"


def _md(line: Line, table: bool = False) -> str:
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append(md_text("".join(run), table))
            run.clear()

    for seg in line:
        if isinstance(seg, Text):
            run.append(seg.text)
            continue
        flush()
        if isinstance(seg, Code):
            out.append(md_code(seg.text, table))
        elif isinstance(seg, Link):
            out.append(f"[{md_text(seg.label, table)}]({seg.url})")
        elif isinstance(seg, Image):
            out.append(f"![{md_text(seg.alt, table)}]({seg.url})")
    flush()
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
    """The 10.3 banner verbatim, in a fence longer than any backtick run inside it."""
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


def _items(items, limit: int) -> list[str]:
    lines: list[str] = []
    for i, item in enumerate(items[:limit]):
        if i:
            lines.append("")
        lines.append(f"**{_md(item.header)}**")
        lines += _fields(item.fields)
    return lines


def _unexpected(r: RunReport, limit: int) -> list[str]:
    lines = ["", f"**Unexpected ({r.n_unexpected})**", *_items(r.unexpected, limit)]
    more = r.n_unexpected - limit
    if more > 0:
        lines.append(f"... and {more} more in the [full report]({r.urls.anchor('unexpected')})")
    return lines


def _intended(r: RunReport, limit: int) -> list[str]:
    lines = ["", f"**Intended ({r.n_intended})**"]
    if r.manifest_line:
        lines.append(_md(r.manifest_line))
    lines += _items(r.intended, limit)
    more = r.n_intended - limit
    if more > 0:
        lines.append(f"... and {more} more in the [full report]({r.urls.anchor('intended')})")
    for line in (r.reconciliation, r.needs_baseline_line):
        if line:
            lines.append(_md(line))
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
        by = f"{md_text(d.decided_by, True)}, {md_text(d.decided_at[:10], True)}" if d else ""
        lines.append(_row([md_code(row.checkpoint_id, True), md_code(row.region, True) if row.region else "",
                           md_text(row.decision_id, True), action, by,
                           md_text(d.ticket, True) if d and d.ticket else ""]))
    lines.append("</details>")
    return lines


def _coverage(r: RunReport) -> list[str]:
    not_run = [] if r.not_run_line is None else ["", _md(r.not_run_line)]   # checkpoints, under the 2.12 table
    if not r.coverage_rows:                           # the run stopped before scope: no table to show
        return ["", "**Coverage**", _md(r.scope_line)] + not_run
    lines = ["", "**Coverage**", "| Tier | Registered | In scope | Covered | Not compared | not_run |",
             "|---|---|---|---|---|---|"]
    lines += [_row(list(row)) for row in r.coverage_rows]
    return lines + not_run + ["", _md(r.scope_line)]


def _health(r: RunReport) -> list[str]:
    lines = ["", "**Run health**", "| Item | Value |", "|---|---|"]
    return lines + [_row([label, _md(value, table=True)]) for label, value in r.health_rows]


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
        lines.append(_row([md_text(h.get("run_id", ""), True), md_text(h.get("candidate", ""), True),
                           md_text(h.get("result", ""), True), md_text(counts, True),
                           md_text(h.get("reported", ""), True)]))
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
    last = INLINE_LIMITS[-1]
    steps = [(n, False, False) for n in INLINE_LIMITS] + [(last, True, False), (last, True, True)]
    text = ""
    for limit, sup, hist in steps:
        text = _render(report, limit, sup, hist)
        if len(text) <= limit_chars:
            break
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
