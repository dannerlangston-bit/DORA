"""The static HTML run report (9.5 as a file): sections, 1:1 crops, heatmap, matrix, escaping."""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from PIL import Image

from sentinel.records import RunOutcome
from sentinel.report import build_report, render_run_report, render_run_report_html
from sentinel.report.html import PageSections

from test_report_sample import ARTS, RUN_ID, sample


def _objects(tmp_path: Path, heatmap: str | None = None) -> Path:
    """Crops of the 9.3 sample under the 8.4 layout `objects/<aa>/<sha256>.png`."""
    root = tmp_path / "artifacts" / "objects"
    for i, (base, cand) in enumerate(ARTS.values()):
        for sha, shade in ((base, 40), (cand, 40)):
            img = Image.new("RGB", (40 + i, 20), (shade, shade, shade))
            if sha == cand:
                img.paste((250, 250, 250), (5, 5, 15, 10))
            (root / sha[:2]).mkdir(parents=True, exist_ok=True)
            img.save(root / sha[:2] / f"{sha}.png")
    if heatmap:
        (root / heatmap[:2]).mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 20), (255, 0, 0)).save(root / heatmap[:2] / f"{heatmap}.png")
    return root


def _write(tmp_path: Path, **overrides) -> str:
    s = sample()
    s.update(overrides)
    out = render_run_report(**s, out_path=tmp_path / "report" / "index.html", objects_dir=_objects(tmp_path))
    assert out.exists()
    return out.read_text(encoding="utf-8")


def test_report_has_every_section_and_both_color_schemes(tmp_path):
    page = _write(tmp_path)
    assert page.startswith("<!doctype html>")
    for section in ("summary", "unexpected", "intended", "suppressed", "coverage", "health"):
        assert f'<section id="{section}">' in page
    assert "Unexpected (4)" in page and "Intended (1)" in page and "Suppressed by decision (1)" in page
    assert "@media (prefers-color-scheme: dark)" in page and "color-scheme: light dark" in page
    assert "failure</span> (informational). 3 tier-1 findings have no decision" in page
    assert "claude-pinned-2026-06, 7c1e2a" in page  # provenance: reviewer model and prompt hash (9.5)


def test_crops_are_embedded_side_by_side_at_1_to_1_with_a_heatmap(tmp_path):
    page = _write(tmp_path)
    item1 = page.split('<article class="item">')[1].split("</article>")[0]
    imgs = re.findall(r'<img class="crop" src="([^"]+)" width="(\d+)" height="(\d+)" alt="([^"]+)">', item1)
    assert [(alt, w, h) for _, w, h, alt in imgs] == [("base", "40", "20"), ("candidate", "40", "20"),
                                                     ("heatmap", "40", "20")]
    assert all(src.startswith("data:image/png;base64,") for src, *_ in imgs)
    assert '<div class="evidence">' in item1 and "computed: pixels that differ, in red" in item1
    assert "image-rendering: pixelated" in page and "max-width: none" in page
    assert 'href="https://sentinel.internal/cp/F-20931">heatmap, flicker</a>' in item1


def test_a_stored_heatmap_is_preferred(tmp_path):
    s = sample()
    s["outcomes"] = [replace(o, artifacts={**o.artifacts, "heatmap": "beef01"}) if o.artifacts else o
                     for o in s["outcomes"]]
    objects = _objects(tmp_path, heatmap="beef01")
    page = render_run_report_html(build_report(**s), objects_dir=objects)
    assert "heatmap at 1:1, 40x20 px" in page and "computed:" not in page


def test_relative_paths_instead_of_data_uris(tmp_path):
    s = sample()
    out = render_run_report(**s, out_path=tmp_path / "report" / "index.html", objects_dir=_objects(tmp_path),
                            embed=False)
    page = out.read_text(encoding="utf-8")
    assert 'src="../artifacts/objects/3f/3f9c.png"' in page


def test_missing_artifacts_get_a_placeholder_and_a_report_ui_link(tmp_path):
    s = sample()
    page = render_run_report_html(build_report(**s), objects_dir=tmp_path / "nothing-here")
    assert "<code>3f9c</code> not in this artifact set" in page
    assert 'href="https://sentinel.internal/a/3f9c"' in page


def test_coverage_tiers_and_matrix(tmp_path):
    page = _write(tmp_path)
    cov = page.split('<section id="coverage">')[1].split("</section>")[0]
    assert "<td>14 (100 percent of comparable scope)</td>" in cov
    assert '<td class="cell-bad">invariant_violation (13)</td>' in cov   # 12 sweep steps + the AT census
    assert "<td class=\"\">intended (1)</td>" in cov and "no checkpoint in this run" in cov
    assert ">passed<" not in page   # 2.12: a cell never reads "passed"


def test_supplied_matrix_cells_are_used(tmp_path):
    s = sample()
    s["coverage"]["cells"] = [{"surface": "trade.order-entry.stock", "persona": "beginner", "state": "not_run",
                               "reason": "entitlement"}]
    page = render_run_report_html(build_report(**s))
    assert '<td class="cell-notrun">not_run (entitlement)</td>' in page


def test_run_health_rows_match_the_comment(tmp_path):
    page = _write(tmp_path)
    health = page.split('<section id="health">')[1]
    assert ("<td>18 (18 stable, 0 unstable), 32 re-captures: 1 each for the 4 <code>invariant_violation</code> "
            "steps, 2 each for the 14 other above-threshold checkpoints; 0 <code>rerun_budget_exhausted</code></td>"
            ) in health
    assert "proof PROVEN: no orders sent" in health


def test_screen_and_model_text_is_escaped(tmp_path):
    s = sample()
    s["manifest"]["entries"][0]["summary"] = '<script>alert(1)</script> "quoted" <a href="https://evil">x</a>'
    page = render_run_report_html(build_report(**s))
    assert "<script>" not in page and 'href="https://evil"' not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert all(u.startswith("https://sentinel.internal/") for u in re.findall(r'href="([^"]+)"', page))


def test_withheld_report_shows_no_findings(tmp_path):
    run = RunOutcome(run_id=RUN_ID, run_type="pr", status="aborted_harness", reason="secret_leaked",
                     last_gate="teardown")
    page = _write(tmp_path, run=run)
    assert "report withheld; security contact notified" in page
    for section in ("unexpected", "intended", "coverage", "health"):
        assert f'<section id="{section}">' not in page


def test_page_is_well_formed(tmp_path):
    from html.parser import HTMLParser

    class Balance(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack: list[str] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag not in ("meta", "img"):
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            else:
                self.errors.append(tag)

    parser = Balance()
    parser.feed(_write(tmp_path))
    assert parser.stack == [] and parser.errors == []


# -- the merge, RC and Nightly sections (PageSections) --------------------------------------------------------------

def _cell(surface, persona, profile, state, *, tier=1, reason=None, worst=None, n=0):
    return {"surface_id": surface, "persona": persona, "entitlement": profile, "tier": tier, "state": state,
            "not_run_reason": reason, "worst_outcome": worst, "checkpoint_count": n}


def test_the_matrix_has_one_column_group_per_profile_red_manifest_rows_and_a_tier_filter():
    s = sample()
    s["coverage"]["cells"] = [
        _cell("trade.order-entry.stock", "swing_trader", "margin", "covered", worst="identical", n=3),
        _cell("trade.order-entry.stock", "swing_trader", "ira", "not_run", reason="entitlement"),
        _cell("charts.flexible-grid.chart-cell", "swing_trader", "margin", "covered", worst="noise", n=1, tier=2),
        _cell("charts.flexible-grid.chart-cell", "swing_trader", "ira", "covered", worst="identical", n=1, tier=2)]
    sections = PageSections(manifest_surfaces=frozenset({"dialog.order-confirmation"}),
                            surface_tiers={"dialog.order-confirmation": 1})
    page = render_run_report_html(build_report(**s), sections=sections)
    matrix = page.split("<h3>Coverage matrix</h3>")[1].split("</section>")[0]
    assert '<th colspan="1">margin</th><th colspan="1">ira</th>' in matrix   # neither profile overwrites the other
    row = matrix.split("<code>trade.order-entry.stock</code>")[1].split("</tr>")[0]
    assert row == '</td><td>1</td><td class="">identical (3)</td><td class="cell-notrun">not_run (entitlement)</td>'
    assert ('<tr data-tier="1" class="row-bad"><td><code>dialog.order-confirmation</code> (in manifest, no covering '
            'checkpoint)</td>') in matrix
    assert '<tr data-tier="2"><td><code>charts.flexible-grid.chart-cell</code>' in matrix
    assert 'id="tf-1"' in matrix and 'id="tf-2"' in matrix and 'id="tf-3"' not in matrix
    assert '#tf-1:checked ~ table tbody tr:not([data-tier="1"])' in page
    assert 'class="row-bad"' not in render_run_report_html(build_report(**s))     # no manifest given, no red row


def test_every_page_section_renders_in_9_4_order_and_the_page_stays_well_formed():
    from html.parser import HTMLParser

    from sentinel.pipeline.config import SentinelConfig
    from sentinel.report.blindspots import compute as blind
    from sentinel.report.breakdown import Breakdown, DropCount
    from sentinel.report.model import SafetyRows
    from sentinel.report.safety import render as safety_render
    from sentinel.report.sensitivity import compute as sens

    from test_report_safety import HEADER, proof, shard_safety

    report = build_report(**sample())
    cfg = SentinelConfig.load(Path(__file__).resolve().parents[1] / "config" / "example")
    sections = PageSections(
        blindspots=blind(cfg, report.outcomes),
        breakdown=Breakdown(drops=(DropCount("scripted", "refused", "deny_profile", 2),
                                   DropCount("system", "dropped", "untokened_injected", 1))),
        sensitivity=sens({"census/x/ws": {"diff_freeze": 90.0, "rule": 6.0, "mask_redaction": 4.0,
                                          "panels": [{"panel": "position_statement", "side": "candidate",
                                                      "share": 4.0, "budget": 10.0}]}}),
        safety=safety_render(SafetyRows(proofs=(proof(),)), shards=[shard_safety()], header=HEADER, run_type="rc"))
    page = render_run_report_html(report, sections=sections)
    order = [page.index(f'<section id="{x}">') for x in ("coverage", "not-detectable", "health", "sensitivity",
                                                         "safety")]
    assert order == sorted(order)
    assert "platform: Windows 11 desktop only" in page.split("</section>")[0]          # the header (9.2)
    blind_sec = page.split('<section id="not-detectable">')[1].split("</section>")[0]
    assert "<td><code>B15</code></td>" in blind_sec and "registered dialogs with zero flow checkpoints: 0 of 1" in \
        blind_sec and "<h3>B16: Behavior with no visible symptom at any checkpoint</h3>" in blind_sec
    health = page.split('<section id="health">')[1].split("</section>")[0]
    assert ("<td>Gateway drops by reason and mode</td><td>2 deny_profile refused (scripted); 1 untokened_injected "
            "dropped (system)</td>") in health
    sens_sec = page.split('<section id="sensitivity">')[1].split("</section>")[0]
    assert "<td>90.0</td><td>6.0</td><td>4.0</td><td>position_statement 4.0 of 10 percent</td>" in sens_sec
    assert "No region carries a floor exception." in sens_sec
    safety = page.split('<section id="safety">')[1].split("</section>")[0]
    assert '<pre class="proof">ORDER SAFETY PROOF        run 2026-11-09T14:02Z  rig tos-rig-03' in safety
    assert "VERDICT        PROVEN: no orders sent</pre>" in safety and "<h3>Attestation A1 to A11</h3>" in safety
    assert "Known residuals (6.13), listed in the RC sign-off" in safety
    assert "identity: paper mode asserted" not in safety          # the model's Safety proof blocks are replaced

    class Balance(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack: list[str] = []
            self.errors: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag not in ("meta", "img", "br"):
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1] == tag:
                self.stack.pop()
            else:
                self.errors.append(tag)

    parser = Balance()
    parser.feed(page)
    assert parser.stack == [] and parser.errors == []


def test_write_run_report_passes_the_sections_through(tmp_path):
    from sentinel.report.breakdown import Breakdown

    out = render_run_report(**sample(), out_path=tmp_path / "r" / "index.html",
                            sections=PageSections(breakdown=Breakdown()))
    assert "<td>Agent rescue cap</td><td>0 of 25 rescued steps used</td>" in out.read_text(encoding="utf-8")
