"""The odd-input corpus, its seeded nightly sample and the values steps type (4.1, 4.7 type_fixture, 2.6.3)."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from sentinel import oddinput as O
from sentinel.taxonomy import load

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
DOC = {"schema_version": 1, "corpus_version": 3, "seed": 7, "sample_size": 3, "entries": [
    {"id": "sym-empty", "field_class": "symbol_box", "value": "", "expect": "rejected"},
    {"id": "sym-halted", "field_class": "symbol_box", "source": "halt_list", "expect": "rejected"},
    {"id": "qty-half", "field_class": "order_field", "field": "qty", "value": "0.5", "expect": "rejected"},
    {"id": "qty-huge", "field_class": "order_field", "field": "qty", "value": "999999999", "expect": "rejected_or_clamped"},
    {"id": "strike-neg", "field_class": "analyze_input", "value": "-5", "expect": "clamped"},
    {"id": "len-zero", "field_class": "study_parameter", "value": "0", "expect": "rejected_or_clamped"},
]}


def corpus() -> O.OddInputCorpus:
    return O.OddInputCorpus.from_dict(copy.deepcopy(DOC))


def test_the_example_corpus_is_valid():
    c = O.OddInputCorpus.load(EXAMPLE / O.PATH)
    assert c.corpus_version >= 1 and {e.field_class for e in c.entries} == set(O.FIELD_CLASSES)
    assert any(e.source == "halt_list" for e in c.entries)


def test_the_same_seed_gives_the_same_sample_and_logs_the_seed():
    a, b = O.sample(corpus(), seed=20261110), O.sample(corpus(), seed=20261110)
    assert a == b and a.seed == 20261110 and len(a.entries) == 3
    assert "seed 20261110" in a.run_health_line() and "corpus version 3" in a.run_health_line()
    others = {tuple(e.id for e in O.sample(corpus(), seed=s).entries) for s in range(20)}
    assert len(others) > 1                                             # the seed decides the draw
    assert O.sample(corpus()).seed == 7                                # the corpus seed by default
    assert len(O.sample(corpus(), seed=1, size=99).entries) == len(DOC["entries"])


def test_a_resolved_value_carries_the_seed_for_the_step_record():
    v = O.resolve(corpus(), "odd_inputs.qty-half", seed=11)
    assert isinstance(v, O.OddInputValue) and v.value == "0.5" and v.expect == "rejected"
    assert v.step_fields() == {"odd_input_ref": "odd_inputs.qty-half", "odd_input_seed": 11, "odd_input_corpus_version": 3}


def test_a_halted_symbol_comes_from_the_days_halt_list():
    """2.6.3: never a fixed symbol; no list that night means not_run (reason: scope)."""
    halt = ["XYZQ", "ABCD", "LMNO"]
    v = O.resolve(corpus(), "odd_inputs.sym-halted", seed=5, halt_list=halt)
    assert isinstance(v, O.OddInputValue) and v.value in halt
    assert O.resolve(corpus(), "odd_inputs.sym-halted", seed=5, halt_list=list(reversed(halt))).value == v.value
    for none in (None, [], ["  "]):
        nr = O.resolve(corpus(), "odd_inputs.sym-halted", seed=5, halt_list=none)
        assert isinstance(nr, O.OddInputNotRun) and nr.reason == "scope"
    assert load().reason_allowed("not_run", O.NOT_RUN_REASON)


def test_an_unknown_reference_is_refused():
    with pytest.raises(O.OddInputError, match="odd_inputs.nope is not an entry"):
        O.resolve(corpus(), "odd_inputs.nope", seed=1)


@pytest.mark.parametrize("change,needle", [
    (lambda d: d.update(schema_version=2), "schema_version must be 1"),
    (lambda d: d.update(corpus_version=0), "corpus_version"),
    (lambda d: d.pop("seed"), "seed is an integer"),
    (lambda d: d.update(entries=[]), "non-empty list"),
    (lambda d: d["entries"].append(dict(d["entries"][0])), "id sym-empty is not unique"),
    (lambda d: d["entries"][0].update(field_class="chart"), "field_class 'chart'"),
    (lambda d: d["entries"][0].update(expect="accepted"), "expect 'accepted'"),
    (lambda d: d["entries"][0].update(source="halt_list"), "exactly one of value and source"),
    (lambda d: d["entries"][2].update(source="halt_list", value=None) or d["entries"][2].pop("value"),
     "a halt-list entry is a symbol_box entry"),
    (lambda d: d["entries"][0].update(extra=1), "unknown field extra"),
])
def test_invalid_corpora(change, needle):
    d = copy.deepcopy(DOC)
    change(d)
    problems = O.validate(d)
    assert any(needle in msg for _loc, msg in problems), problems
    with pytest.raises(O.OddInputError):
        O.OddInputCorpus.from_dict(d)
