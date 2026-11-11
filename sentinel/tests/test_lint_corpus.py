"""L-WSP-08 and L-WSP-09 (2.3 Workspace Corpus) and the odd-input corpus references (4.1, 4.7): sentinel.lint.checks.corpus."""
from __future__ import annotations

import copy
import shutil
from datetime import date
from pathlib import Path
from typing import Callable

import pytest

from sentinel.lint import Repo, lint, lint_repo
from sentinel.lint.repo import Doc

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
TODAY = date(2026, 11, 10)
WS_AT = "corpus/workspaces/ws.tier-a.active-trader/entry.yaml"
WS_C = "corpus/workspaces/ws.tier-c.sweep-harness/entry.yaml"
TIER_B = sorted(str(p.relative_to(EXAMPLE)) for p in EXAMPLE.glob("corpus/workspaces/ws.tier-b.*/entry.yaml"))
LEVEL2 = "registry/surfaces/charts/charts.flexible-grid.level2-cell.yaml"
FLOW = "flows/tier-1/flow.trade.order-ticket.stock.limit.yaml"
ODD = "corpus/odd-inputs.yaml"


@pytest.fixture(scope="module")
def base() -> Repo:
    return Repo.load(EXAMPLE)


def found(base: Repo, fn: Callable[[Repo], None], phase: int = 0) -> list:
    r = copy.deepcopy(base)
    fn(r)
    return lint_repo(r, phase, TODAY).violations


def test_the_example_corpus_has_the_2_3_shape(base):
    assert len(TIER_B) == 4
    assert not [v for v in found(base, lambda r: None) if v.rule in ("L-WSP-08", "L-WSP-09")]


def test_a_streaming_gadget_in_tier_a_or_c_is_l_wsp_08(base):
    def fn(r: Repo) -> None:
        r.docs[LEVEL2].data["streaming"] = True
        r.docs[WS_C].data["gadgets_present"].append("charts.flexible-grid.level2-cell")
    got = [v for v in found(base, fn) if v.rule == "L-WSP-08"]
    assert {v.file for v in got} == {WS_AT, WS_C} and all(v.severity == "error" for v in got)
    assert any("charts.flexible-grid.level2-cell" in v.message for v in got)


def test_tier_b_may_host_streaming_gadgets(base):
    def fn(r: Repo) -> None:
        r.docs[LEVEL2].data["streaming"] = True
        r.docs[WS_AT].data["gadgets_present"].remove("charts.flexible-grid.level2-cell")
        r.docs[WS_AT].data["gadgets_present"].append("charts.flexible-grid.chart-cell")
    assert not [v for v in found(base, fn) if v.rule == "L-WSP-08" and "tier-b" in v.file]


def _second_tier_c(r: Repo) -> None:
    rel = "corpus/workspaces/ws.tier-c.second/entry.yaml"
    r.docs[rel] = Doc(rel, "workspace", {**copy.deepcopy(r.docs[WS_C].data), "id": "ws.tier-c.second"})


@pytest.mark.parametrize("fn,needle", [
    (_second_tier_c, "2 tier C entries; the corpus has exactly one"),
    (lambda r: [r.docs.pop(rel) for rel in TIER_B[:1]], "3 tier B entries; the corpus has 4 to 8"),
    (lambda r: [r.docs[rel].data.update(saved_under_build="released-1998.0.0-3c9e7a1") for rel in TIER_B],
     "0 tier B entries saved under the previous release released-1998.1.0-8b2d4f6; at least 2"),
])
def test_corpus_shape_warns_until_phase_3(base, fn, needle):
    early = [v for v in found(base, fn, phase=1) if v.rule == "L-WSP-09"]
    assert [(v.severity, v.file) for v in early if needle in v.message] == [("warn", "corpus/workspaces")], early
    late = [v for v in found(base, fn, phase=3) if v.rule == "L-WSP-09" and needle in v.message]
    assert [v.severity for v in late] == ["error"]


def test_one_tier_b_entry_on_the_previous_release_is_too_few(base):
    """2.3: at least two saved under the previous release (02-corpus-lint-rules)."""
    def fn(r: Repo) -> None:
        on_prev = [rel for rel in TIER_B if r.docs[rel].data["saved_under_build"] == "released-1998.1.0-8b2d4f6"]
        r.docs[on_prev[0]].data["saved_under_build"] = "released-1998.0.0-3c9e7a1"
    assert any("1 tier B entries saved under the previous release" in v.message
               for v in found(base, fn, phase=3) if v.rule == "L-WSP-09")


def _odd_doc(r: Repo, data: object) -> None:
    r.docs[ODD] = Doc(ODD, "odd_inputs", data)


def _type_odd(ref: str) -> Callable[[Repo], None]:
    def fn(r: Repo) -> None:
        st = next(s for s in r.docs[FLOW].data["steps"] if s["id"] == "s04")
        st.pop("value", None)
        st["value_ref"] = ref
    return fn


def test_odd_input_references_resolve(base):
    assert not [v for v in found(base, _type_odd("odd_inputs.qty-fractional")) if v.rule == "L-FLW-00"]
    bad = [v for v in found(base, _type_odd("odd_inputs.nope")) if v.rule == "L-FLW-00"]
    assert [(v.file, v.path) for v in bad] == [(FLOW, "steps[s04].value_ref")]
    assert "odd_inputs.nope is not an entry of corpus/odd-inputs.yaml" in bad[0].message


def test_a_fixture_reference_on_a_typing_step_resolves(base):
    bad = [v for v in found(base, _type_odd("fixture.symbols.nope.symbol")) if v.rule == "L-FLW-00"]
    assert any("value_ref does not resolve in the flow's fixture" in v.message for v in bad), bad


def test_the_corpus_file_is_validated(base):
    bad = [v for v in found(base, lambda r: _odd_doc(r, {"schema_version": 1, "corpus_version": 1, "seed": "x",
                                                         "entries": [{"id": "a", "field_class": "chart", "value": "1",
                                                                      "expect": "rejected"}]}))
           if v.rule == "L-WSP-00" and v.file == ODD]
    assert {v.path for v in bad} == {"seed", "entries[0].field_class"}


def test_a_missing_corpus_fails_its_references(tmp_path):
    root = tmp_path / "cfg"
    shutil.copytree(EXAMPLE, root)
    (root / ODD).unlink()
    p = root / FLOW
    p.write_text(p.read_text().replace("value: \"100\"", "value_ref: odd_inputs.qty-huge"), encoding="utf-8")
    got = [v for v in lint(root, today=TODAY).violations if v.rule == "L-FLW-00"]
    assert got and "corpus/odd-inputs.yaml is missing" in got[0].message
