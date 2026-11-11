"""The starter sentinel-config (tools/gen_starter.py, 12-starter-registry-absent) and the example rows against 12.4
(12-example-rows-contradict-12.4): every 12.4 row and 12.9 edge present, L-DEP-05 clean, mutants bound as 12.5
binds them, and the example's 12.4 surfaces equal their rows except for the deviations listed here."""
from __future__ import annotations

import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
import yaml

from sentinel.lint import Repo, lint

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT.parent / "sentinel-spec" / "sections"
pytestmark = pytest.mark.skipif(not SPEC.exists(), reason="spec checkout not present")
sys.path.insert(0, str(ROOT / "tools"))

import gen_starter  # noqa: E402
import spec_tables as st  # noqa: E402

STARTER = ROOT / "config" / "starter"
EXAMPLE = ROOT / "config" / "example"
TODAY = date(2026, 11, 10)

# Rules the starter fails until Phase 0 and the team supply their inputs (gen_starter docstring): captured templates,
# labels and required controls, dialog titles, probe P8 sizes, the workspace corpus, flows, regions files, the
# redaction list and the team's own files (Phase 0 branches, hotkeys, windows, authorization, CODEOWNERS, vmoptions).
PHASE0_ERRORS = frozenset({
    "L-SRF-02", "L-SRF-03", "L-SRF-04", "L-SRF-06", "L-SRF-08", "L-SRF-10", "L-SRF-14", "L-XREF-02", "L-XREF-03",
    "L-XREF-04", "L-MUT-01", "L-MUT-04", "L-MUT-05", "L-MSK-03", "L-XREF-15", "L-XREF-17", "L-XREF-20", "L-XREF-24",
    "L-XREF-21", "L-XREF-26", "L-XREF-28",       # L-XREF-21: policy/reporting.yaml is the team's own (channels, projects)
})
PHASE0_WARNINGS = frozenset({"L-DEP-01", "L-XREF-01"})       # the TBD source packages; surfaces no census reaches yet

# Example surfaces that deviate from their 12.4 row, with the reason (also commented in each registry file)
DEVIATIONS = {
    ("trade.order-entry.stock", "resizable"): "the example's sweep harness does not host the stock ticket",
    ("trade.order-entry.stock", "data_mode"): "its census regions here carry no rule or mask region",
    ("monitor.activity.position-statement", "resizable"): "the example's sweep harness does not host it",
}


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> tuple[Path, dict[str, int]]:
    out = tmp_path_factory.mktemp("gen") / "starter"
    return out, gen_starter.generate(out)


def _surfaces(root: Path) -> dict[str, dict]:
    return {d.data["id"]: d.data for d in Repo.load(root).of("surface")}


def test_every_12_4_row_is_a_tier_1_surface(generated):
    out, counts = generated
    surfaces = _surfaces(out)
    tier1 = {sid for sid, s in surfaces.items() if s["tier"] == 1}
    spread = {s for s in tier1 if s.startswith("trade.order-entry.spread.")}
    advanced = {s for s in tier1 if s.startswith("ticket.advanced.")}
    rows = st.table_after(st.subsection("12.4"), "| Surface id | Primitives")
    assert len(rows) == 41 and (len(spread), len(advanced)) == (14, 5)
    assert len(tier1) == counts["tier1"] == 41 - 2 + 14 + 5
    assert surfaces["trade.order-entry.spread.back-ratio"]["entitlements"] == ["options_L3"]
    assert surfaces["trade.order-entry.spread.vertical"]["entitlements"] == ["options_L2"]
    assert surfaces["trade.active-trader.ladder"]["personas"][0] == "active_trader"
    assert surfaces["dialog.order-confirmation"]["data_mode"] == "masked_live"


def test_the_map_holds_every_12_9_edge_and_the_omitted_variant_edges(generated):
    out, counts = generated
    dm = yaml.safe_load((out / "registry" / "dependency-map.yaml").read_text())
    assert len(dm["primitives"]) == counts["primitives"] == 10
    edges = {(e["primitive"], e["surface"]) for e in dm["edges"]}
    listed = gen_starter.dependency_map()["edges"]
    assert len(listed) == 164 and {(e["primitive"], e["surface"]) for e in listed} <= edges
    assert len(edges) == counts["edges"] > 164
    assert ("QuoteStream", "trade.order-entry.spread.condor") in edges and ("FontMetrics", "*") in edges
    assert next(p for p in dm["primitives"] if p["id"] == "Docking")["description"].startswith("Detach, dock and")


def test_the_starter_lints_clean_of_schema_and_dependency_errors(generated):
    out, _ = generated
    report = lint(out, 0, TODAY)
    by_rule = Counter((v.rule, v.severity) for v in report.violations)
    errors = {r for r, sev in by_rule if sev == "error"}
    warnings = {r for r, sev in by_rule if sev == "warn"}
    assert not {r for r in errors | warnings if r.endswith("-00") or r.startswith("L-DEP")} - {"L-DEP-01"}
    assert errors <= PHASE0_ERRORS, sorted(errors - PHASE0_ERRORS)
    assert warnings <= PHASE0_WARNINGS, sorted(warnings - PHASE0_WARNINGS)
    assert "L-XREF-12" not in errors and "L-XREF-22" not in errors and "L-XREF-27" not in errors


def test_the_starter_mutants_are_bound_as_12_5_binds_them(generated):
    out, _ = generated
    cat = yaml.safe_load((out / "canaries" / "mutants.yaml").read_text())
    rows = {r[0].strip(): r for r in st.table_after(st.subsection("12.5"), "| Id | Class (10.3) |")}
    got = {m["mutant_id"]: m for m in cat["entries"]}
    assert cat["schema_version"] == 1 and sorted(got) == sorted(rows)
    for mid, m in got.items():
        want = "*" if mid == "M25" else st.CODE.findall(rows[mid][2])[0]
        assert m["checkpoint_id"] == want, mid
        assert (m["class"], m["money_cell"]) == st.mutant_table_12_5()[mid]
    assert got["M13"]["expected"] == got["M14"]["expected"] == {"outcomes": ["assertion_failed"], "assertion_id": "R04"}


def test_the_committed_starter_is_current():
    assert STARTER.is_dir()
    assert gen_starter.main(["--check"]) == 0


def test_the_example_rows_follow_12_4_except_the_listed_deviations():
    rows = {s["id"]: s for s in gen_starter.tier1_rows()}
    example = _surfaces(EXAMPLE)
    present = sorted(set(rows) & set(example))
    assert present == ["charts.flexible-grid.active-trader-cell", "dialog.order-confirmation",
                       "monitor.activity.position-statement", "trade.active-trader.ladder", "trade.order-entry.stock"]
    drift = set()
    for sid in present:
        want, got = rows[sid], example[sid]
        if set(got["primitives"]) != set(want["primitives"]):
            drift.add((sid, "primitives"))
        if got["data_mode"] != want["data_mode"]:
            drift.add((sid, "data_mode"))
        if got["resizable"] != want["resizable"]:
            drift.add((sid, "resizable"))
    assert drift == set(DEVIATIONS)
    for (sid, field), _why in DEVIATIONS.items():
        path = next(EXAMPLE.glob(f"registry/surfaces/*/{sid}.yaml"))
        line = next(ln for ln in path.read_text().splitlines() if ln.startswith(f"{field}:"))
        assert "deviation: 12.4" in line, (sid, field)
