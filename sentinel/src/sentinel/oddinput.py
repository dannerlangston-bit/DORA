"""The odd-input corpus: `corpus/odd-inputs.yaml`, its seeded nightly sample and the values `type_fixture` steps type
(4.1 layout, 4.7 `type_fixture`, 2.6.3 Odd input, 2.9 Steering).

2.6.3: a "versioned corpus for symbol box, order fields, Analyze inputs, study parameters; sampled nightly with a
logged seed; pre-Send surfaces only. A halted or delisted symbol comes from the day's halt list read in a scripted
step, never from a fixed symbol; no list that night means the entry is `not_run (reason: scope)`". Pass is a visible
rejection or a clamped value shown; a crash, freeze, silent acceptance, NaN, Infinity, null or a third value fails
(the Oracle's assertions). 4.7: a step references an entry as `value_ref: odd_inputs.<id>`, and "an odd-input value
carries the corpus seed on the step record so a nightly sample is reproducible".

Section 4 writes no schema for the file, so the shape is chosen here (reported), from those sentences:

    schema_version: 1
    corpus_version: int             # >= 1; bumped on every change ("versioned")
    seed: int                       # the default sampling seed; a nightly run may draw its own and logs it
    sample_size: int?               # entries a nightly sample takes; default every entry
    entries:
      - id: str                     # unique in the file, ^[a-z0-9][a-z0-9_.-]*$, referenced as odd_inputs.<id>
        field_class: enum[symbol_box|order_field|analyze_input|study_parameter]   # the four corpus kinds of 2.6.3
        field: str?                 # the field within the class (qty, price, strike, length, ...)
        value: str?                 # the literal typed; exactly one of value and source
        source: enum[halt_list]?    # the day's halt list, read in a scripted step (2.6.3)
        expect: enum[rejected|clamped|rejected_or_clamped]   # the pass rule of 2.6.3 for this entry
        note: str?

`validate(data)` lists the problems of a document as (location, message); `OddInputCorpus.from_dict` refuses an
invalid one. `sample(corpus, seed)` draws the nightly sample with `random.Random(seed)`, so the same seed and corpus
version give the same entries, and records the seed. `resolve(corpus, ref, seed, halt_list)` gives the value a step
types: an `OddInputValue` carrying the seed and corpus version for the step record (`step_fields()`), or an
`OddInputNotRun` (`not_run (reason: scope)`) for a halt-list entry on a night without a list.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

PATH = "corpus/odd-inputs.yaml"                 # 4.1 layout
REF_PREFIX = "odd_inputs."                       # 4.7 type_fixture value_ref
SCHEMA_VERSION = 1
FIELD_CLASSES = ("symbol_box", "order_field", "analyze_input", "study_parameter")     # 2.6.3
EXPECTS = ("rejected", "clamped", "rejected_or_clamped")                               # 2.6.3 pass rule
SOURCES = ("halt_list",)                                                               # 2.6.3 halted symbols
ENTRY_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_TOP = frozenset({"schema_version", "corpus_version", "seed", "sample_size", "entries"})
_ENTRY = frozenset({"id", "field_class", "field", "value", "source", "expect", "note"})
NOT_RUN_REASON = "scope"                         # taxonomy.yaml reasons.not_run: no halt list that night (2.6.3)


class OddInputError(ValueError):
    """An odd-input corpus or reference the Executor cannot use."""


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate(data: Any) -> list[tuple[str, str]]:
    """Every problem of a corpus document, as (location, message); empty when it is valid."""
    if not isinstance(data, Mapping):
        return [("", "the odd-input corpus is a mapping with schema_version, corpus_version, seed and entries")]
    out: list[tuple[str, str]] = []
    for k in sorted(set(data) - _TOP):
        out.append((k, f"unknown field {k}"))
    if data.get("schema_version") != SCHEMA_VERSION:
        out.append(("schema_version", f"schema_version must be {SCHEMA_VERSION}"))
    if not (_int(data.get("corpus_version")) and data["corpus_version"] >= 1):
        out.append(("corpus_version", "corpus_version is an integer of at least 1 (the corpus is versioned)"))
    if not _int(data.get("seed")):
        out.append(("seed", "seed is an integer (the sampling seed, 2.6.3)"))
    if "sample_size" in data and not (_int(data["sample_size"]) and data["sample_size"] >= 1):
        out.append(("sample_size", "sample_size is an integer of at least 1"))
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        return out + [("entries", "entries is a non-empty list")]
    seen: set[str] = set()
    for i, e in enumerate(entries):
        at = f"entries[{i}]"
        if not isinstance(e, Mapping):
            out.append((at, "an entry is a mapping"))
            continue
        for k in sorted(set(e) - _ENTRY):
            out.append((f"{at}.{k}", f"unknown field {k}"))
        eid = e.get("id")
        if not (isinstance(eid, str) and ENTRY_ID.match(eid)):
            out.append((f"{at}.id", f"id {eid!r} does not match {ENTRY_ID.pattern}"))
        elif eid in seen:
            out.append((f"{at}.id", f"id {eid} is not unique"))
        else:
            seen.add(eid)
        if e.get("field_class") not in FIELD_CLASSES:
            out.append((f"{at}.field_class", f"field_class {e.get('field_class')!r} is not one of {', '.join(FIELD_CLASSES)}"))
        if e.get("expect") not in EXPECTS:
            out.append((f"{at}.expect", f"expect {e.get('expect')!r} is not one of {', '.join(EXPECTS)}"))
        if ("value" in e) == ("source" in e):
            out.append((at, "an entry has exactly one of value and source"))
        if "value" in e and not isinstance(e["value"], str):
            out.append((f"{at}.value", "value is the literal string typed"))
        if "source" in e and e["source"] not in SOURCES:
            out.append((f"{at}.source", f"source {e['source']!r} is not one of {', '.join(SOURCES)}"))
        if "source" in e and e.get("field_class") != "symbol_box":
            out.append((f"{at}.source", "a halt-list entry is a symbol_box entry (2.6.3)"))
        for k in ("field", "note"):
            if k in e and not isinstance(e[k], str):
                out.append((f"{at}.{k}", f"{k} is a string"))
    return out


@dataclass(frozen=True)
class OddInputEntry:
    """One corpus entry (module docstring)."""
    id: str
    field_class: str
    expect: str
    value: str | None = None
    source: str | None = None
    field: str | None = None

    @property
    def ref(self) -> str:
        """`odd_inputs.<id>`, the `value_ref` a type_fixture step names (4.7)."""
        return REF_PREFIX + self.id


@dataclass(frozen=True)
class OddInputCorpus:
    """A valid `corpus/odd-inputs.yaml`."""
    corpus_version: int
    seed: int
    entries: tuple[OddInputEntry, ...]
    sample_size: int | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "OddInputCorpus":
        problems = validate(data)
        if problems:
            loc, msg = problems[0]
            raise OddInputError(f"{PATH}: {loc + ': ' if loc else ''}{msg}")
        entries = tuple(OddInputEntry(str(e["id"]), str(e["field_class"]), str(e["expect"]), e.get("value"),
                                      e.get("source"), e.get("field")) for e in data["entries"])
        return cls(int(data["corpus_version"]), int(data["seed"]), entries, data.get("sample_size"))

    @classmethod
    def load(cls, path: str | Path) -> "OddInputCorpus":
        from .lint.repo import load_yaml_text          # the config loader: YAML 1.2 booleans, dates kept as text

        try:
            return cls.from_dict(load_yaml_text(Path(path).read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError) as exc:
            raise OddInputError(f"cannot read {path}: {exc}") from exc

    def ids(self) -> frozenset[str]:
        return frozenset(e.id for e in self.entries)

    def entry(self, ref_or_id: str) -> OddInputEntry:
        """The entry an `odd_inputs.<id>` reference (or a bare id) names; OddInputError when there is none."""
        eid = ref_or_id[len(REF_PREFIX):] if ref_or_id.startswith(REF_PREFIX) else ref_or_id
        for e in self.entries:
            if e.id == eid:
                return e
        raise OddInputError(f"{REF_PREFIX}{eid} is not an entry of {PATH}")


@dataclass(frozen=True)
class OddInputSample:
    """One nightly sample: the seed it was drawn with, the corpus version and the entries, in draw order."""
    seed: int
    corpus_version: int
    entries: tuple[OddInputEntry, ...]

    def run_health_line(self) -> str:
        """The logged seed (2.6.3): `odd-input sample: seed 20261110, corpus version 3, 6 of 12 entries`."""
        return f"odd-input sample: seed {self.seed}, corpus version {self.corpus_version}, {len(self.entries)} entries"


def sample(corpus: OddInputCorpus, seed: int | None = None, size: int | None = None) -> OddInputSample:
    """The nightly sample (2.6.3): `size` entries (default the corpus's `sample_size`, else all) drawn without
    replacement by `random.Random(seed)` over the entries in file order; `seed` defaults to the corpus's. The same
    seed and corpus give the same sample."""
    s = corpus.seed if seed is None else seed
    if not _int(s):
        raise OddInputError(f"the sampling seed is an integer, not {s!r}")
    k = min(len(corpus.entries), size or corpus.sample_size or len(corpus.entries))
    return OddInputSample(s, corpus.corpus_version, tuple(random.Random(s).sample(list(corpus.entries), k)))


@dataclass(frozen=True)
class OddInputValue:
    """The value a type_fixture step types for an odd-input reference, with what the step record carries (4.7)."""
    ref: str
    value: str
    seed: int
    corpus_version: int
    field_class: str
    expect: str

    def step_fields(self) -> dict[str, Any]:
        """The fields the step record carries so a nightly sample is reproducible (4.7)."""
        return {"odd_input_ref": self.ref, "odd_input_seed": self.seed, "odd_input_corpus_version": self.corpus_version}


@dataclass(frozen=True)
class OddInputNotRun:
    """A halt-list entry on a night without a halt list: the entry is `not_run (reason: scope)` (2.6.3)."""
    ref: str
    seed: int
    reason: str = NOT_RUN_REASON
    detail: str = "no halt list read for this night (2.6.3)"


def resolve(corpus: OddInputCorpus, ref: str, seed: int, halt_list: Sequence[str] | None = None
            ) -> OddInputValue | OddInputNotRun:
    """The value a step types for `ref` (`odd_inputs.<id>`) in a run sampled with `seed`. A literal entry types its
    value; a halt-list entry types one symbol of the day's halt list, chosen by the same seed (sorted list, so the
    choice is reproducible), and is `not_run (reason: scope)` when no list was read or it is empty."""
    entry = corpus.entry(ref)
    if entry.source == "halt_list":
        symbols = sorted({str(s).strip() for s in halt_list or () if str(s).strip()})
        if not symbols:
            return OddInputNotRun(entry.ref, seed)
        value = random.Random(f"{seed}:{entry.id}").choice(symbols)
    else:
        value = str(entry.value)
    return OddInputValue(entry.ref, value, seed, corpus.corpus_version, entry.field_class, entry.expect)
