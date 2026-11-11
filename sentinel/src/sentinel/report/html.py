"""A self-contained static HTML page for one run (9.5 Report UI pages, as a file).

The page carries the run detail of 9.5 for a PR run: provenance, the PR comment content
(Unexpected and Intended with base and candidate crops side by side at 1:1 and a heatmap,
Suppressed, Coverage with the surface x persona matrix of 2.12, Run health), the Safety proof
section of 9.4 item 8 (each ORDER SAFETY PROOF block, the last Nightly's fire drill and safety
canary rows, open incidents and overdue postmortems), the Alerts panel of 9.5 and the run
history. It is built from the same `RunReport` as the PR comment, so the two never disagree.

- Standard library only for the HTML; Pillow (already a dependency) reads image sizes and, when
  the Diff Engine stored no heatmap, computes one from the two crops (absolute difference in red).
- Crops are shown at 1:1: `width` and `height` equal the image's pixel size and
  `image-rendering: pixelated`; a wide crop scrolls instead of shrinking (9.1).
- Images are found under `objects_dir/<aa>/<sha256>.<ext>` (8.4 Artifact layout on disk) and
  embedded as data URIs, or linked by a path relative to the page when `embed=False`.
- Light and dark follow `prefers-color-scheme`. Every string is HTML-escaped; the only links
  are report UI URLs from `inline.Urls` and the page's own anchors.

`PageSections` adds what a merge, RC or Nightly page carries beyond the PR comment (9.4, 9.5), each optional: the
6.6 Safety proof section of `report.safety.render` (9.4 item 8; it replaces the model's Safety proof blocks), the "Not
detectable by design" table of `report.blindspots.compute` (9.4 item 6, after the matrix; its B06 and B07 lines also
go in the header, 9.2), the effective-sensitivity section of `report.sensitivity.compute` (3.8, 3.9), the run-health
breakdown of `report.breakdown.query` (5.13, appended to Run health), the change manifest's declared surfaces for the
matrix's red rows and each surface's tier for its tier filter.

Coverage matrix choices (9.5, 2.12; reported): the matrix has one column group per entitlement profile with the
personas inside it, so cells of several profiles never overwrite each other; summary cells without an entitlement
(the short form) form one unnamed group. A surface the manifest declares with no checkpoint in this run is a red row,
"in manifest, no covering checkpoint", added when the matrix has no row for it. The tier filter is CSS only (radio
inputs, no script); a row's tier is its summary cell's, else `PageSections.surface_tiers`, else the lowest tier of the
surface's outcome records.
"""
from __future__ import annotations

import base64
import html as _html
import io
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image as PILImage
from PIL import ImageChops

from ..records import CheckpointOutcome, Decision, Finding, RunOutcome, StepOutcome
from .blindspots import BlindSpots
from .breakdown import Breakdown
from .inline import Code, Image, Line, Link, Text, is_artifact_hash
from .model import FIELD_EVIDENCE, G2_WITHHELD, Evidence, ReportContext, RunReport, SafetyRows, build_report
from .safety import SafetySection
from .sensitivity import SHARE_SAID, SHARES, SensitivityReport

_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
          ".gif": "image/gif"}
# 2.12 covered-cell order, worst first.
_COVERED_ORDER = ("invariant_violation", "assertion_failed", "needs_baseline", "collateral", "needs_human",
                  "intended", "noise", "identical")
_BLOCKING = ("not_run", "not_settled", "not_comparable", "landmark_missing", "env_error", "harness_error")

_CSS = """
:root { --bg:#ffffff; --fg:#1b1f24; --muted:#57606a; --line:#d0d7de; --panel:#f6f8fa; --accent:#0969da;
        --bad:#cf222e; --warn:#9a6700; --ok:#1a7f37; color-scheme: light dark; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0d1117; --fg:#e6edf3; --muted:#8d96a0; --line:#30363d; --panel:#161b22; --accent:#4493f8;
          --bad:#f85149; --warn:#d29922; --ok:#3fb950; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font:14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 16px; }
h1 { font-size: 20px; margin: 8px 0; } h2 { font-size: 17px; margin: 28px 0 8px; border-bottom: 1px solid var(--line); }
h3 { font-size: 14px; margin: 0 0 6px; }
a { color: var(--accent); }
code { font: 12.5px ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--panel);
  padding: 0 3px; border-radius: 3px; overflow-wrap: anywhere; }
.muted { color: var(--muted); } .bad { color: var(--bad); } .ok { color: var(--ok); } .warn { color: var(--warn); }
pre.banner { background: var(--panel); border-left: 4px solid var(--bad); padding: 8px 12px; white-space: pre-wrap; }
.item { border: 1px solid var(--line); border-radius: 6px; padding: 12px; margin: 12px 0; background: var(--bg); }
.fields { margin: 0; padding-left: 18px; } .fields li { margin: 2px 0; }
.label { font-weight: 600; }
.evidence { display: flex; gap: 12px; overflow-x: auto; padding: 6px 0; align-items: flex-start; }
.evidence figure { margin: 0; flex: none; } .evidence figcaption { font-size: 12px; color: var(--muted); }
img.crop { image-rendering: pixelated; max-width: none; display: block; border: 1px solid var(--line); }
.missing { border: 1px dashed var(--line); padding: 8px; color: var(--muted); font-size: 12px; }
table { border-collapse: collapse; margin: 8px 0; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; }
th { background: var(--panel); }
td.cell-bad { color: var(--bad); } td.cell-notrun { color: var(--warn); }
tr.row-bad td { color: var(--bad); }
pre.proof { background: var(--panel); padding: 8px 12px; overflow-x: auto; margin: 6px 0 12px;
  font: 12.5px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; }
.tier-filter label { margin-right: 10px; }
#tf-1:checked ~ table tbody tr:not([data-tier="1"]), #tf-2:checked ~ table tbody tr:not([data-tier="2"]),
#tf-3:checked ~ table tbody tr:not([data-tier="3"]) { display: none; }
dl.prov { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 8px 0; }
dl.prov dt { color: var(--muted); } dl.prov dd { margin: 0; }
"""


def _e(s: object) -> str:
    return _html.escape(str(s), quote=True)


@dataclass(frozen=True)
class PageSections:
    """The merge, RC and Nightly sections a page adds to the run report (module docstring); all optional."""

    safety: SafetySection | None = None
    blindspots: BlindSpots | None = None
    sensitivity: SensitivityReport | None = None
    breakdown: Breakdown | None = None
    manifest_surfaces: frozenset[str] = frozenset()        # declared surfaces of the run's manifest (red rows)
    surface_tiers: Mapping[str, int] = field(default_factory=dict)   # registry tier per surface (tier filter)


class _Images:
    """Resolves artifact hashes to `<img>` sources under the 8.4 object layout."""

    def __init__(self, objects_dir: Path | None, embed: bool, page_dir: Path | None):
        self.objects_dir, self.embed, self.page_dir = objects_dir, embed, page_dir

    def path(self, sha: str) -> Path | None:
        if self.objects_dir is None or not is_artifact_hash(sha):
            return None
        folder = self.objects_dir / sha[:2]
        if not folder.is_dir():
            return None
        for p in sorted(folder.glob(f"{sha}.*")):
            if p.suffix.lower() in _MEDIA:
                return p
        return None

    def src(self, path: Path) -> str:
        if self.embed or self.page_dir is None:
            raw = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{_MEDIA[path.suffix.lower()]};base64,{raw}"
        return Path(os.path.relpath(path, self.page_dir)).as_posix()

    def figure(self, img: Image | None, caption: str) -> str:
        if img is None:
            return (f'<figure><div class="missing">no {_e(caption)} crop recorded</div>'
                    f'<figcaption>{_e(caption)}</figcaption></figure>')
        path = self.path(img.sha)
        if path is None:
            return (f'<figure><div class="missing">{_e(caption)} <code>{_e(img.sha)}</code> not in this '
                    f'artifact set; <a href="{_e(img.url)}">open in the report UI</a></div>'
                    f'<figcaption>{_e(caption)}</figcaption></figure>')
        with PILImage.open(path) as im:
            w, h = im.size
        return (f'<figure><img class="crop" src="{_e(self.src(path))}" width="{w}" height="{h}" '
                f'alt="{_e(caption)}"><figcaption>{_e(caption)} at 1:1, {w}x{h} px</figcaption></figure>')

    def computed_heatmap(self, base: Image | None, cand: Image | None) -> str | None:
        """Absolute difference of the two crops in red over the dimmed candidate."""
        pb, pc = (self.path(base.sha) if base else None), (self.path(cand.sha) if cand else None)
        if pb is None or pc is None:
            return None
        with PILImage.open(pb) as a, PILImage.open(pc) as b:
            if a.size != b.size:
                return None
            ra, rb = a.convert("RGB"), b.convert("RGB")
        diff = ImageChops.difference(ra, rb).convert("L").point(lambda v: 255 if v else 0)
        dim = PILImage.blend(rb, PILImage.new("RGB", rb.size, (0, 0, 0)), 0.6)
        red = PILImage.new("RGB", rb.size, (255, 0, 0))
        out = PILImage.composite(red, dim, diff)
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        w, h = out.size
        src = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
        return (f'<figure><img class="crop" src="{src}" width="{w}" height="{h}" alt="heatmap">'
                f'<figcaption>heatmap at 1:1 (computed: pixels that differ, in red)</figcaption></figure>')


def _inline(line: Line) -> str:
    out = []
    for seg in line:
        if isinstance(seg, Text):
            out.append(_e(seg.text))
        elif isinstance(seg, Code):
            out.append(f"<code>{_e(seg.text)}</code>")
        elif isinstance(seg, Link):
            out.append(f'<a href="{_e(seg.url)}">{_e(seg.label)}</a>')
        elif isinstance(seg, Image):
            out.append(f'<a href="{_e(seg.url)}">{_e(seg.alt)}</a>')
    return "".join(out)


def _evidence(ev: Evidence, images: _Images) -> str:
    figs = [images.figure(ev.base, "base"), images.figure(ev.candidate, "candidate")]
    if ev.heatmap is not None:
        figs.append(images.figure(ev.heatmap, "heatmap"))
    else:
        computed = images.computed_heatmap(ev.base, ev.candidate)
        if computed:
            figs.append(computed)
    extra = []
    if ev.checkpoint_url:
        extra.append(f'<a href="{_e(ev.checkpoint_url)}">heatmap, flicker</a>')
    if ev.note:
        extra.append(f'<span class="muted">({_e(ev.note)})</span>')
    return f'<div class="evidence">{"".join(figs)}</div>' + (" ".join(extra))


def _fields(fields, images: _Images) -> str:
    lis = []
    for label, value in fields:
        if label == FIELD_EVIDENCE and isinstance(value, Evidence):
            lis.append(f'<li><span class="label">{_e(label)}:</span> {_evidence(value, images)}</li>')
        else:
            lis.append(f'<li><span class="label">{_e(label)}:</span> {_inline(value)}</li>')
    return f'<ul class="fields">{"".join(lis)}</ul>'


def _article(header: Line, fields, images: _Images) -> str:
    return f'<article class="item"><h3>{_inline(header)}</h3>{_fields(fields, images)}</article>'


def _table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(c if c.startswith("<td") else f"<td>{c}</td>" for c in r) + "</tr>"
                   for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


_Cell = tuple[str, str, str]                          # (surface, persona, entitlement profile; "" when not named)
NO_COVERING = "in manifest, no covering checkpoint"   # 9.5 Coverage matrix red rows


def _grid(report: RunReport) -> tuple[dict[_Cell, tuple[str, str]], dict[str, int]]:
    """The matrix cells (2.12) keyed by surface, persona and profile, and the tiers the cells carry: the summary's
    cells, else derived from this run's outcomes."""
    grid: dict[_Cell, tuple[str, str]] = {}
    tiers: dict[str, int] = {}
    if report.coverage_cells:                         # `model.coverage_cell` form
        for c in report.coverage_cells:
            key = (c["surface"], c["persona"], c.get("entitlement") or "")
            if c.get("tier") is not None:
                tiers[c["surface"]] = int(c["tier"])
            if c["state"] == "covered":
                grid[key] = (f"{c['worst'] or 'identical'} ({c['n']})", "")
            elif c["state"] == "not_run":
                grid[key] = (f"not_run ({c['reason'] or 'scope'})", "cell-notrun")
            else:
                grid[key] = ("not_registered", "cell-notrun")
        return grid, tiers
    by_cell: dict[_Cell, list[CheckpointOutcome]] = {}
    for o in report.outcomes:
        by_cell.setdefault((o.surface_id, o.persona, o.profile_id or ""), []).append(o)
    for key, os_ in by_cell.items():
        usable = [o for o in os_ if o.outcome in _COVERED_ORDER]
        if usable:
            worst = min(usable, key=lambda o: _COVERED_ORDER.index(o.outcome)).outcome
            grid[key] = (f"{worst} ({len(usable)})", "cell-bad" if _COVERED_ORDER.index(worst) < 5 else "")
        else:
            blocking = Counter(o.outcome for o in os_ if o.outcome in _BLOCKING).most_common(1)
            if blocking and blocking[0][0] == "not_run":
                reason = Counter(o.reason for o in os_ if o.outcome == "not_run").most_common(1)[0][0]
                grid[key] = (f"not_run ({reason})", "cell-notrun")
            elif blocking:
                grid[key] = (f"not_run (blocked:{blocking[0][0]})", "cell-notrun")
    return grid, tiers


def _matrix(report: RunReport, sections: PageSections) -> str:
    """Surface x persona x entitlement matrix (2.12, 9.5): one column group per profile, red rows for declared surfaces
    with no covering checkpoint, and a tier filter (module docstring).

    A cell never reads "passed"; a surface without a checkpoint for a persona says so.
    """
    grid, cell_tiers = _grid(report)
    checked = {o.surface_id for o in report.outcomes}
    red = {s for s in sections.manifest_surfaces if s not in checked}
    if not grid and not red:
        return '<p class="muted">No coverage cells recorded for this run.</p>'
    surfaces = sorted({s for s, _, _ in grid} | red)
    personas = sorted({p for _, p, _ in grid})
    profiles = list(dict.fromkeys(f for _, _, f in grid))     # the summary's profile order (`Cell.to_rows`)
    if not personas:
        personas, profiles = [""], [""]
    outcome_tiers: dict[str, int] = {}
    for o in report.outcomes:
        if o.tier is not None:
            outcome_tiers[o.surface_id] = min(o.tier, outcome_tiers.get(o.surface_id, o.tier))
    tiers = {**outcome_tiers, **sections.surface_tiers, **cell_tiers}
    named = profiles != [""]
    head = ['<th rowspan="2">Surface</th><th rowspan="2">Tier</th>' if named else "<th>Surface</th><th>Tier</th>"]
    if named:
        head += [f'<th colspan="{len(personas)}">{_e(f or "profile not named")}</th>' for f in profiles]
        head.append("</tr><tr>" + "".join(f"<th>{_e(p.replace('_', ' '))}</th>" for _ in profiles for p in personas))
    else:
        head += [f"<th>{_e(p.replace('_', ' '))}</th>" for p in personas]
    body = []
    for s in surfaces:
        tier = tiers.get(s)
        attrs = (f' data-tier="{tier}"' if tier is not None else "") + (' class="row-bad"' if s in red else "")
        label = f"<code>{_e(s)}</code>" + (f" ({NO_COVERING})" if s in red else "")
        cells = []
        for f in profiles:
            for p in personas:
                text, cls = grid.get((s, p, f), ("no checkpoint in this run", "muted"))
                cells.append(f'<td class="{cls}">{_e(text)}</td>')
        body.append(f"<tr{attrs}><td>{label}</td><td>{_e(tier if tier is not None else '')}</td>{''.join(cells)}</tr>")
    table = f'<table class="matrix"><thead><tr>{"".join(head)}</tr></thead><tbody>{"".join(body)}</tbody></table>'
    present = sorted({tiers[s] for s in surfaces if tiers.get(s) in (1, 2, 3)})
    if not present:
        return table
    radios = ['<input type="radio" name="tier-filter" id="tf-all" checked /><label for="tf-all">all tiers</label>']
    radios += [f'<input type="radio" name="tier-filter" id="tf-{t}" /><label for="tf-{t}">tier {t}</label>'
               for t in present]
    return f'<div class="tier-filter">Filter: {"".join(radios)}{table}</div>'


def _provenance(report: RunReport) -> str:
    ctx = report.context
    items = [("Build pair", f"{ctx.candidate_build} vs {ctx.base_build}")]
    for key in ("harness_commit", "config_commit", "vm_image_id"):
        if ctx.provenance.get(key):
            items.append((key.replace("_", " "), ctx.provenance[key]))
    models = sorted({(o.classification.model_id, o.classification.prompt_hash or "") for o in report.outcomes
                     if o.classification and o.classification.model_id})
    if models:
        items.append(("Reviewer model, prompt", "; ".join(f"{m}, {p}" for m, p in models)))
    return '<dl class="prov">' + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in items) + "</dl>"


def _lines(lines: tuple[str, ...]) -> str:
    return "<br>".join(_e(x) for x in lines)


def _blindspots(b: BlindSpots) -> str:
    """9.4 item 6: the "Not detectable by design" table of 2.13, then the tables of B02, B03 and B16."""
    rows = [[f"<code>{_e(r.id)}</code>", _e(r.title), _e(r.mechanism), _lines(r.lines)] for r in b.rows]
    out = ['<section id="not-detectable"><h2>Not detectable by design</h2>',
           _table(["Id", "Not detectable", "How it is labeled", "This run"], rows)]
    for r in b.rows:
        if r.table:
            out.append(f"<h3>{_e(r.id)}: {_e(r.title)}</h3>"
                       + _table(list(r.head), [[f"<code>{_e(c)}</code>" if i == 0 else _e(c) for i, c in enumerate(row)]
                                               for row in r.table]))
    return "".join(out) + "</section>"


def _sensitivity(s: SensitivityReport) -> str:
    """3.8 effective sensitivity per checkpoint, with the week-over-week change; 3.9 floor exceptions."""
    since = f"Change since {s.previous_run_id}" if s.previous_run_id else "Week-over-week change"
    rows = [[f"<code>{_e(r.checkpoint_id)}</code>", _e(r.tier if r.tier is not None else ""),
             *(_e(x) for x in r.shares_text()), _e("; ".join(p.text() for p in r.panels) or "no budgeted panel"),
             _e(r.change_text(s.previous_run_id is not None))] for r in s.rows]
    head = ["Checkpoint", "Tier", *(f"{SHARE_SAID[k]} percent" for k in SHARES), "Budget use per panel", since]
    out = ['<section id="sensitivity"><h2>Effective sensitivity</h2>',
           _table(head, rows) if rows else '<p class="muted">No checkpoint measured its sensitivity in this run.</p>',
           "<h3>Regions with a floor exception</h3>"]
    fe = [[f"<code>{_e(f.checkpoint_id)}</code>", f"<code>{_e(f.region_id)}</code>", _e(f.approved_by), _e(f.reason),
           _e(f.ticket), _e(f.expires_at)] for f in s.floor_exceptions]
    out.append(_table(["Checkpoint", "Region", "Approved by", "Reason", "Ticket", "Expires"], fe) if fe
               else '<p class="muted">No region carries a floor exception.</p>')
    return "".join(out) + "</section>"


def _safety(s: SafetySection) -> str:
    """9.4 item 8: each 6.6 block as printed, the A1 to A11 results with evidence hashes, the previous night's drill
    and safety canaries, incidents, and on an RC report the 6.13 residuals for the sign-off."""
    out = ['<section id="safety"><h2>Safety proof</h2>']
    out += [f'<h3>{_e(b.title)}</h3><pre class="proof">{_e(b.text)}</pre>' for b in s.blocks]
    if s.attestation:
        rows = [[_e(a.shard_id or "run level"), _e(a.item), _e(a.check),
                 f'<td class="{"" if a.state in ("match", "not_read") else "cell-bad"}">{_e(a.state)}</td>',
                 f"<code>{_e(a.evidence_sha256)}</code>" if a.evidence_sha256 else ""] for a in s.attestation]
        profiles = "; ".join(f"{k or 'run level'}: {v}" for k, v in s.profile_sha256.items() if v)
        out.append("<h3>Attestation A1 to A11</h3>" + (f"<p>Settings profile sha256 {_e(profiles)}</p>" if profiles
                                                        else "")
                   + _table(["Shard", "Item", "Check", "Result", "Evidence sha256"], rows))
    for title, lines in s.groups:
        out.append(f"<h3>{_e(title)}</h3><ul>" + "".join(f"<li>{_e(x)}</li>" for x in lines) + "</ul>")
    if s.residuals:
        out.append("<h3>Known residuals (6.13), listed in the RC sign-off</h3><ul>"
                   + "".join(f"<li>{_e(x)}</li>" for x in s.residuals) + "</ul>")
    return "".join(out) + "</section>"


def render_run_report_html(report: RunReport, *, objects_dir: str | os.PathLike | None = None, embed: bool = True,
                           page_dir: Path | None = None, sections: PageSections | None = None) -> str:
    """The page as a string (see `render_run_report` to write it to disk); `sections` adds the merge, RC and Nightly
    sections (module docstring)."""
    sections = sections or PageSections()
    images = _Images(Path(objects_dir) if objects_dir else None, embed, page_dir)
    run, st = report.run, report.status
    title = f"Sentinel {run.run_type} run {run.run_id}"
    parts = ["<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><main>"]
    if report.banner:
        parts.append(f'<pre class="banner">{_e(report.banner)}</pre>')
    parts += [f"<p class=\"warn\">{_inline(b)}</p>" for b in report.banner_lines]
    state_cls = {"success": "ok", "failure": "bad", "error": "bad"}.get(st.state, "muted")
    parts.append(f'<section id="summary"><h1>{_e(title)}</h1>{_provenance(report)}'
                 f"<p>Result: <strong>{_e(report.result)}</strong>")
    if report.special != "withheld":
        parts.append(f" | {report.n_unexpected} unexpected | {report.n_intended} intended | "
                     f"{report.n_suppressed} suppressed by decision")
    parts.append(f'</p><p>Check <code>{_e(st.context)}</code>: <span class="{state_cls}">{_e(st.state)}</span> '
                 f"({_e(st.mode)}). {_e(st.reason)}</p>")
    if report.review_line:
        parts.append(f"<p>{_inline(report.review_line)}</p>")
    if sections.blindspots is not None:               # 9.2 header: B06 and B07 (2.13)
        parts.append(f"<p>{_e(' | '.join(sections.blindspots.header_lines()))}</p>")
    parts.append("</section>")
    if report.abort_lines:
        lis = "".join(f"<li>{_inline(x)}</li>" for x in report.abort_lines)
        parts.append(f'<section id="abort"><h2>{_e(report.abort_title or "Abort")}</h2><ul>{lis}</ul></section>')
    if report.special is None:
        if report.show_findings:
            parts.append(f'<section id="unexpected"><h2>Unexpected ({report.n_unexpected})</h2>')
            parts += [_article(item.header, item.fields, images) for item in report.unexpected]
            parts.append("</section>")
            parts.append(f'<section id="intended"><h2>Intended ({report.n_intended})</h2>')
            if report.manifest_line:
                parts.append(f"<p>{_inline(report.manifest_line)}</p>")
            if report.manifest_lint_lead:             # 4.3: the lint output of a manifest treated as missing
                parts.append(f"<p>{_inline(report.manifest_lint_lead)}</p><ul>"
                             + "".join(f"<li>{_inline(x)}</li>" for x in report.manifest_lint) + "</ul>")
            for line in (report.manifest_template, report.empty_reason_line):
                if line:
                    parts.append(f"<p>{_inline(line)}</p>")
            parts += [_article(it.header, it.fields, images) for it in report.intended]
            for line in (report.reconciliation, report.needs_baseline_line):
                if line:
                    parts.append(f"<p>{_inline(line)}</p>")
            parts.append("</section>")
        rows = []
        for r in report.suppressed:
            d: Decision | None = r.decision
            rows.append([f"<code>{_e(r.checkpoint_id)}</code>", f"<code>{_e(r.region or '')}</code>", _e(r.decision_id),
                         _e(f"{d.action} ({d.verdict})" if d else "decision record not loaded"),
                         _e(f"{d.decided_by}, {d.decided_at[:10]}" if d else ""), _e((d.ticket or "") if d else "")])
        parts.append(f'<section id="suppressed"><h2>Suppressed by decision ({report.n_suppressed})</h2>'
                     + (_table(["Checkpoint", "Region", "Decision", "Action (verdict)", "By", "Ticket"], rows)
                        if rows else '<p class="muted">0 findings matched an earlier decision.</p>') + "</section>")
        cov_rows = [[_e(c) for c in row] for row in report.coverage_rows]
        not_run = f"<p>{_inline(report.not_run_line)}</p>" if report.not_run_line else ""   # checkpoints (9.2)
        if report.g2_failures:                        # 1.4 G2: a matrix whose cells do not sum is never published
            lis = "".join(f"<li>{_e(f)}</li>" for f in report.g2_failures)
            parts.append(f'<section id="coverage"><h2>Coverage</h2><p class="bad">{_e(G2_WITHHELD)}</p><ul>{lis}</ul>'
                         f"{not_run}</section>")
        elif cov_rows:
            parts.append('<section id="coverage"><h2>Coverage</h2>'
                         + _table(["Tier", "Registered", "In scope", "Covered", "Not compared", "not_run"], cov_rows)
                         + not_run + f"<p>{_inline(report.scope_line)}</p><h3>Coverage matrix</h3>"
                         + _matrix(report, sections) + "</section>")
        else:                                         # the run stopped before scope
            parts.append(f'<section id="coverage"><h2>Coverage</h2><p>{_inline(report.scope_line)}</p>{not_run}'
                         '</section>')
        if sections.blindspots is not None:           # 9.4 item 6
            parts.append(_blindspots(sections.blindspots))
        health = [[_e(k), _inline(v)] for k, v in report.health_rows]
        if sections.breakdown is not None:            # 5.13, 5.4.8: drops by reason and mode and the rest
            health += [[_e(k), _e(v)] for k, v in sections.breakdown.rows()]
        parts.append('<section id="health"><h2>Run health</h2>' + _table(["Item", "Value"], health) + "</section>")
        if sections.sensitivity is not None:          # 3.8, 3.9
            parts.append(_sensitivity(sections.sensitivity))
        if sections.safety is not None:               # 9.4 item 8, the 6.6 blocks as printed
            parts.append(_safety(sections.safety))
        elif report.safety_proof:                     # 9.4 item 8 Safety proof
            blocks = "".join(f"<h3>{_e(title)}</h3><ul>" + "".join(f"<li>{_inline(x)}</li>" for x in lines) + "</ul>"
                             for title, lines in report.safety_proof)
            parts.append(f'<section id="safety"><h2>Safety proof</h2>{blocks}</section>')
        if report.alerts:                             # 9.5 run detail: the Alerts panel
            lis = "".join(f"<li>{_inline(x)}</li>" for x in report.alerts)
            parts.append(f'<section id="alerts"><h2>Alerts ({len(report.alerts)})</h2><ul>{lis}</ul></section>')
        if report.history:
            hrows = [[_e(h.get("run_id", "")), _e(h.get("candidate", "")), _e(h.get("result", "")),
                      _e(", ".join(f"{v} {k}" for k, v in h["counts"].items()) if isinstance(h.get("counts"), dict)
                         else h.get("counts", "")), _e(h.get("reported", ""))] for h in report.history]
            parts.append('<section id="history"><h2>Run history</h2>'
                         + _table(["Run", "Candidate", "Result", "Counts", "Reported"], hrows) + "</section>")
    parts.append("</main></body></html>\n")
    return "".join(parts)


def render_run_report(run: RunOutcome, outcomes: list[CheckpointOutcome], findings: list[Finding],
                      decisions: list[Decision], coverage: dict | None, health: dict | None,
                      manifest: dict | None, context: ReportContext, out_path: str | os.PathLike, *,
                      steps: list[StepOutcome] = (), objects_dir: str | os.PathLike | None = None,
                      embed: bool = True, summary: dict | None = None, safety: SafetyRows | None = None,
                      sections: PageSections | None = None) -> Path:
    """Write the static run report to `out_path` and return the path (9.5); `summary` and `safety` as for
    `model.build_report`, `sections` as for `render_run_report_html`."""
    report = build_report(run, outcomes, findings, decisions, coverage, health, manifest, context, steps,
                          summary=summary, safety=safety)
    return write_run_report(report, out_path, objects_dir=objects_dir, embed=embed, sections=sections)


def write_run_report(report: RunReport, out_path: str | os.PathLike, *,
                     objects_dir: str | os.PathLike | None = None, embed: bool = True,
                     sections: PageSections | None = None) -> Path:
    """Write a built report's page to `out_path` (for `ReportInputs.build`) and return the path (9.5)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_run_report_html(report, objects_dir=objects_dir, embed=embed, page_dir=out.parent,
                                          sections=sections), encoding="utf-8")
    return out
