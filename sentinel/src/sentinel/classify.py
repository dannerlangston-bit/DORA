"""Classification: the decision table of 3.11, encoded as data.

One outcome per checkpoint, by a first-match-wins rule list over ten inputs (G, H, B, D, S, M, T,
P, X, R). The row order implements the precedence of 1.10 as amended by F27: `env_error`, app
failure (`assertion_failed (assertion: app_alive)`), `harness_error`, `not_run`,
`not_comparable`, `not_settled`, `landmark_missing`, `invariant_violation`, `assertion_failed`,
`needs_baseline`, then the comparison outcomes.

- `derive_inputs(facts)` turns what the run knows (gate failures, harness state, base state, diff
  level against floors, stability, decision memory with ticket scope, threshold source, path,
  manifest declaration, reviewer state) into the ten table inputs.
- `match_row(inputs)` is the table alone: `RULES` is the rule list in spec order.
- `classify(facts)` returns one `Classified`: the `CheckpointOutcome` field set plus the row.
- `apply_late_verdict` is F28: an arriving Reviewer verdict re-evaluates a pending item through
  the table and never overrides an existing human decision. `close_pending` is the end of review
  for an item that never got a verdict (4.13, third later change).

The Reviewer can move nothing outside rows 20 to 25a (3.16): only those rows read R, so no verdict
can change an outcome produced by an invariant, an assertion or rows 1 to 19, move a checkpoint to
or from `collateral`, or produce `noise`, `identical`, `not_comparable`, `not_settled` or
`not_run`.

Choices where the spec is ambiguous (each cites the section it reads):

- G from several facts (3.11 input G, 1.10). When more than one gate-level fact holds (an
  environment fault and an app failure, a harness fault and an app failure), G takes the value
  whose row comes first in `RULES`, so the row order alone decides precedence. The facts not
  chosen are kept in `evidence`.
- An invariant violation the confirming re-capture did not reproduce sets G to `not_settled
  (reason: unstable)` (3.10, 2.7), with the violation in `detail` as evidence.
- R when no review exists yet is `pending`: every row 23 item is queued (3.15). A verdict of
  `not_applicable` on a classification item fails the client-side checks of 3.13 and is
  `unavailable`.
- `review_state` (4.13, 3.11 rows 23 and 25): `pending` for R `pending`; `done` when the row read
  a verdict or R `unavailable` (the job gave up); for R `capped`, `done` when the item was queued
  when model calls stopped and `none` when the cap excluded it from the start (never queued).
- Decision memory (3.11 input M, 9.7). Only active decisions count; an `intended` decision counts
  only on its build pair (read from the decision record and the match; whichever is set must equal
  the run's pair) or when the run's loaded manifest carries its entry or ticket, and never on a
  run without a manifest, where `intended` is disabled (4.3). Matches
  with different verdicts inherit nothing and are listed as conflicting. Otherwise the nearest
  by Hamming distance, then the newest, is inherited. A `confirmed_bug` match is not a row
  selector: the computed outcome stands, the decision is referenced (`decision_ref`) and a
  computed `collateral` takes reason `decision:<id>` (4.13). On rows 7 and 8 a matching decision
  is a waiver: it is referenced and the outcome never changes. A rescued capture ignores M
  entirely: no inheritance, no known-bug reference, no waiver (3.11 notes, 4.14).
- Other applicable `needs_human` reasons (3.11 notes, F47) go in `detail` as "also: ..." in the
  fixed order of 3.11 on every row from 9 on. `uncalibrated`, `no_manifest` and `global_shift`
  apply only to an above-threshold diff.
- Global shift (3.7, 3.11 notes) is an exception applied after the table: when the matched row is
  16 to 23 and the diff sits under a `global_rendering_shift` run finding, the outcome is
  `needs_human (reason: global_shift)`, `row` keeps the table row and `exception` is
  `global_shift`.
- `classification` is set on every comparison outcome: by `rule` except the rows that read a
  verdict (20, 21, 22, 24 and 25a with a verdict), which are by `reviewer`. On rows 7 and 8 the
  lower result the table would give without the invariant or assertion is kept as evidence and,
  when it is a rule classification, as `classification` (4.13 worked example); a reviewer
  classification never appears there (L-OUT-03).
- A late verdict on an item that already carries a human decision is an annotation: outcome,
  reason, detail and classification stay, and `review_state` moves to `done` so the run can reach
  `reviewed`. It joins the eval development split (3.15) when it disagrees with the decision:
  `intended` agrees with a confident match, `confirmed_bug` with a confident no-match and `noise`
  with no visible difference.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from .invariants import INVARIANT_IDS
from .records import Classification, Decision
from .taxonomy import TaxonomyError, load

ROUTING_THRESHOLD = 0.80                # 3.11 input R, until 3.14 derives it

INPUT_VALUES: dict[str, tuple[str, ...]] = {
    "G": ("env", "app", "harness", "not_run", "not_comparable", "not_settled", "landmark", "ok"),
    "H": ("invariant", "assertion", "unreadable", "none"),
    "B": ("present", "absent", "removed"),
    "D": ("zero", "below", "above"),
    "S": ("stable", "unstable", "unknown"),
    "M": ("noise", "intended", "none"),
    "T": ("calibrated", "uncalibrated"),
    "P": ("scripted", "rescued"),
    "X": ("declared", "undeclared", "no_manifest_ui", "no_manifest_nonui"),
    "R": ("match_hi", "match_lo", "no_match", "nothing", "unsure", "unavailable", "capped", "pending"),
}


@dataclass(frozen=True)
class Inputs:
    """The ten inputs of the 3.11 table, one value each."""

    G: str
    H: str
    B: str
    D: str
    S: str
    M: str
    T: str
    P: str
    X: str
    R: str

    def __post_init__(self) -> None:
        for name, allowed in INPUT_VALUES.items():
            if getattr(self, name) not in allowed:
                raise ValueError(f"input {name}={getattr(self, name)!r} is not one of {allowed}")


@dataclass(frozen=True)
class Row:
    """One row of the rule list: the allowed values per input (an input not listed is `*`)."""

    id: str
    outcome: str
    when: Mapping[str, frozenset[str]]

    def matches(self, inputs: Inputs) -> bool:
        return all(getattr(inputs, k) in v for k, v in self.when.items())


def _row(row_id: str, outcome: str, **when: str | tuple[str, ...]) -> Row:
    return Row(row_id, outcome, {k: frozenset((v,) if isinstance(v, str) else v) for k, v in when.items()})


_ABOVE = dict(G="ok", H=("none", "unreadable"), B="present", D="above")
_SETTLED = dict(_ABOVE, S=("stable", "unknown"))
_DECLARED = dict(_SETTLED, T="calibrated", P="scripted", X="declared")
_REMOVED = dict(G="ok", B="removed")

#: The rule list of 3.11, in spec order (first match wins).
RULES: tuple[Row, ...] = (
    _row("1", "env_error", G="env"),
    _row("1a", "assertion_failed", G="app"),
    _row("2", "harness_error", G="harness"),
    _row("3", "not_run", G="not_run"),
    _row("4", "not_comparable", G="not_comparable"),
    _row("5", "not_settled", G="not_settled"),
    _row("6", "landmark_missing", G="landmark"),
    _row("7", "invariant_violation", G="ok", H="invariant"),
    _row("8", "assertion_failed", G="ok", H="assertion"),
    _row("9", "needs_baseline", G="ok", H=("none", "unreadable"), B="absent"),
    _row("9a", "needs_human", G="ok", H="unreadable", B="present", D=("zero", "below")),
    _row("10", "identical", G="ok", H="none", B="present", D="zero", P="scripted"),
    _row("11", "noise", G="ok", H="none", B="present", D="below", P="scripted"),
    _row("11a", "needs_human", G="ok", H="none", B="present", D=("zero", "below"), P="rescued"),
    _row("12", "not_settled", **_ABOVE, S="unstable"),
    _row("13", "noise", **_SETTLED, M="noise", P="scripted"),
    _row("14", "intended", **_SETTLED, M="intended", P="scripted"),
    _row("15", "needs_human", **_SETTLED, T="uncalibrated"),
    _row("16", "collateral", **_SETTLED, T="calibrated", X="undeclared"),
    _row("17", "collateral", **_SETTLED, T="calibrated", X="no_manifest_ui"),
    _row("18", "needs_human", **_SETTLED, T="calibrated", X="no_manifest_nonui"),
    _row("19", "needs_human", **_SETTLED, T="calibrated", P="rescued", X="declared"),
    _row("20", "intended", **_DECLARED, R="match_hi"),
    _row("21", "needs_human", **_DECLARED, R="no_match"),
    _row("22", "needs_human", **_DECLARED, R=("match_lo", "nothing", "unsure")),
    _row("23", "needs_human", **_DECLARED, R=("unavailable", "capped", "pending")),
    _row("24", "intended", **_REMOVED, X="declared", R="match_hi"),
    _row("25", "needs_human", **_REMOVED, X="declared", R="pending"),
    _row("25a", "needs_human", **_REMOVED, X="declared",
         R=("match_lo", "no_match", "nothing", "unsure", "unavailable", "capped")),
    _row("26", "collateral", **_REMOVED),
)

ROW_IDS: tuple[str, ...] = tuple(r.id for r in RULES)
#: Rows that read R: the only rows a Reviewer verdict can decide (3.16 item 3).
REVIEWER_ROWS = frozenset(r.id for r in RULES if "R" in r.when)
#: Rows the global-shift exception replaces (3.7, 3.11 notes).
GLOBAL_SHIFT_ROWS = frozenset(str(n) for n in range(16, 24))
#: Rows that read S; an above-threshold diff reaching them must carry its stability (3.10).
STABILITY_ROWS = frozenset(r.id for r in RULES if "S" in r.when)
#: Gate values in the order their rows appear: the precedence of 1.10 among gate-level facts.
G_ORDER: tuple[str, ...] = tuple(dict.fromkeys(v for r in RULES if "G" in r.when and len(r.when["G"]) == 1
                                              for v in r.when["G"]))
#: The fixed order of needs_human reasons (3.11 notes), global_shift in place of rows 16 to 23.
NEEDS_HUMAN_ORDER: tuple[str, ...] = ("oracle_unreadable", "bounds_unreadable", "rescued_capture", "uncalibrated",
                                      "global_shift", "no_manifest", "reviewer_no_match", "reviewer_uncertain",
                                      "reviewer_unavailable", "reviewer_cap")


def match_row(inputs: Inputs) -> Row:
    """The first row of `RULES` that matches; the list is exhaustive, so one always does."""
    for row in RULES:
        if row.matches(inputs):
            return row
    raise AssertionError(f"3.11 is exhaustive; no row matched {inputs}")


# --------------------------------------------------------------------------------------------
# Facts


@dataclass(frozen=True)
class EnvFailure:
    """G `env`: a gate failed before the checkpoint, or the checkpoint was unreached in an
    `aborted_safety` run (gate and abort reason). Every `env_error` names its gate (F65)."""

    gate: str
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class AppFailure:
    """G `app`: process gone or EDT frozen past max wait while capturing (10.1)."""

    cause: str                          # jvm_crash | hang | oom | process_exit
    detail: str | None = None

    def __post_init__(self) -> None:
        load().check_member("app_alive_causes", self.cause)


@dataclass(frozen=True)
class HarnessFailure:
    """G `harness`: a harness fault at capture or diff, with its Watchdog event id."""

    reason: str
    event_id: str


@dataclass(frozen=True)
class Condition:
    """G `not_run`, `not_comparable`, `not_settled` or `landmark`: the reason, side and detail."""

    reason: str
    side: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class DecisionMatch:
    """A persisted human decision whose signature matches this checkpoint (9.7)."""

    decision: Decision
    hamming: int
    build_pair_id: str | None = None    # the build pair the decision was made on


@dataclass(frozen=True)
class Review:
    """What the Reviewer said, or why it has not (input R; 3.13, 3.15)."""

    state: str                          # answered | pending | unavailable | capped
    verdict: str | None = None
    confidence: float | None = None
    cause: str | None = None            # capped: "call_cap" or "dollar_cap"; unavailable: why
    queued: bool = False                # capped: the item was awaiting a verdict when calls stopped
    threshold: float = ROUTING_THRESHOLD
    model_id: str | None = None
    prompt_hash: str | None = None
    description: str | None = None
    region_pointer: str | None = None
    defect_class: str | None = None

    def __post_init__(self) -> None:
        if self.state not in ("answered", "pending", "unavailable", "capped"):
            raise ValueError(f"review state {self.state!r}")
        if self.state == "answered":
            load().check_member("reviewer_verdicts", self.verdict or "")
            if self.confidence is None or not 0.0 <= self.confidence <= 1.0:
                raise ValueError("an answered review carries a confidence in [0, 1] (3.13)")
        elif self.verdict is not None:
            raise ValueError("only an answered review carries a verdict")


@dataclass(frozen=True)
class Facts:
    """Everything 3.11 reads about one checkpoint. Defaults are the clean path."""

    # G
    env: EnvFailure | None = None
    app: AppFailure | None = None
    harness: HarnessFailure | None = None
    not_run: Condition | None = None
    not_comparable: Condition | None = None
    not_settled: Condition | None = None
    landmark: Condition | None = None
    # H
    invariant_ids: tuple[str, ...] = ()
    invariant_stability: str | None = None          # the confirming re-capture of 3.10
    invariant_stability_cause: str | None = None
    assertion_ids: tuple[str, ...] = ()             # failed assertions other than app_alive
    assertion_detail: str | None = None             # expected and extracted values
    oracle_unreadable: tuple[str, ...] = ()         # Oracle fields below their floor on every source
    bounds_unreadable: tuple[str, ...] = ()         # elements whose bounds could not be read
    # B
    base: str = "present"
    base_reason: str | None = None                  # with base absent: a needs_baseline reason
    # D, S
    diff: str = "zero"
    stability: str | None = None
    stability_cause: str | None = None
    # M
    decisions: tuple[DecisionMatch, ...] = ()
    build_pair_id: str | None = None
    manifest_entries: frozenset[str] = frozenset()  # entry ids in the run's loaded manifests
    manifest_tickets: frozenset[str] = frozenset()
    # T, P, X, R
    threshold_source: str = "calibrated"
    fix_list_item: str | None = None                # row 15: what withholds the threshold
    rescued: bool = False
    manifest: str = "undeclared"
    review: Review | None = None
    # the 3.7 exception and pass-through record fields
    global_shift: bool = False
    primitive: str | None = None
    manifest_entry_ref: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("B", self.base), ("D", self.diff), ("T", self.threshold_source), ("X", self.manifest)):
            if value not in INPUT_VALUES[name]:
                raise ValueError(f"{name}={value!r}")
        if self.stability is not None and self.stability not in INPUT_VALUES["S"]:
            raise ValueError(f"stability {self.stability!r}")
        if self.base == "absent" and not self.base_reason:
            raise ValueError("an absent base needs its needs_baseline reason (3.11 input B)")
        if "app_alive" in self.assertion_ids:
            raise ValueError("app_alive is G app (row 1a), never H assertion")


# --------------------------------------------------------------------------------------------
# Deriving the ten inputs


@dataclass(frozen=True)
class _Memory:
    value: str                          # M: noise | intended | none
    decision: Decision | None = None    # the inherited noise or intended decision
    known_bug: Decision | None = None   # a confirmed_bug match: referenced, never a row selector
    conflict: tuple[Decision, ...] = ()


def _in_scope(m: DecisionMatch, facts: Facts) -> bool:
    d = m.decision
    if d.verdict != "intended":
        return True
    # L-02, 4.3 Missing manifest: the `intended` category is disabled for a no-manifest run, so an
    # intended decision is not in scope there and the item falls through to rows 15 to 18.
    if facts.manifest in ("no_manifest_ui", "no_manifest_nonui"):
        return False
    # L-01, F38: the pair the decision was made on is on the Decision record; a DecisionMatch may
    # repeat it. Whichever is set must equal this run's pair.
    pairs = {p for p in (m.build_pair_id, d.build_pair_id) if p is not None}
    return (pairs == {facts.build_pair_id}
            or (d.manifest_entry_ref is not None and d.manifest_entry_ref in facts.manifest_entries)
            or (d.ticket is not None and d.ticket in facts.manifest_tickets))


def _memory(facts: Facts) -> _Memory:
    if facts.rescued:       # L-03, 3.11 notes rows 13 and 14: a rescued checkpoint ignores decision memory
        return _Memory("none")
    live = [m for m in facts.decisions if m.decision.status == "active" and _in_scope(m, facts)]
    if not live:
        return _Memory("none")
    if len({m.decision.verdict for m in live}) > 1:
        return _Memory("none", conflict=tuple(m.decision for m in live))
    newest_first = sorted(live, key=lambda m: m.decision.decided_at, reverse=True)
    best = min(newest_first, key=lambda m: m.hamming).decision
    if best.verdict == "confirmed_bug":
        return _Memory("none", known_bug=best)
    return _Memory(best.verdict, decision=best)


def reviewer_input(review: Review | None) -> str:
    """Input R from the Reviewer's state, verdict and confidence (3.11, threshold 0.80)."""
    if review is None or review.state == "pending":
        return "pending"
    if review.state in ("unavailable", "capped"):
        return review.state
    v, hi = review.verdict, (review.confidence or 0.0) >= review.threshold
    if v == "matches_manifest":
        return "match_hi" if hi else "match_lo"
    if v == "partially_matches_manifest":
        return "match_lo"
    if v == "does_not_match_manifest":
        return "no_match" if hi else "unsure"
    if v == "no_visible_difference":
        return "nothing"
    if v == "cannot_judge":
        return "unsure"
    return "unavailable"                # not_applicable fails the 3.13 client-side checks


def _invariant_unstable(facts: Facts) -> bool:
    return bool(facts.invariant_ids) and facts.invariant_stability == "unstable"


def _gate_present(facts: Facts) -> dict[str, bool]:
    return {
        "env": facts.env is not None, "app": facts.app is not None, "harness": facts.harness is not None,
        "not_run": facts.not_run is not None, "not_comparable": facts.not_comparable is not None,
        "not_settled": facts.not_settled is not None or _invariant_unstable(facts),
        "landmark": facts.landmark is not None, "ok": True,
    }


def derive_inputs(facts: Facts) -> Inputs:
    """The ten table inputs of 3.11 from the facts."""
    present = _gate_present(facts)
    g = next(v for v in G_ORDER if present[v])
    if facts.invariant_ids:
        h = "invariant"
    elif facts.assertion_ids:
        h = "assertion"
    elif facts.oracle_unreadable or facts.bounds_unreadable:
        h = "unreadable"
    else:
        h = "none"
    return Inputs(G=g, H=h, B=facts.base, D=facts.diff, S=facts.stability or "unknown", M=_memory(facts).value,
                  T=facts.threshold_source, P="rescued" if facts.rescued else "scripted", X=facts.manifest,
                  R=reviewer_input(facts.review))


# --------------------------------------------------------------------------------------------
# The result


@dataclass(frozen=True)
class Classified:
    """One checkpoint's classification: the `CheckpointOutcome` field set plus the row that fired."""

    row: str
    outcome: str
    reason: str | None = None
    detail: str | None = None
    review_state: str = "none"
    classification: Classification | None = None
    gate: str | None = None
    side: str | None = None
    invariant_ids: tuple[str, ...] = ()
    assertion_ids: tuple[str, ...] = ()
    decision_ref: str | None = None
    stability: str | None = None
    stability_cause: str | None = None
    rescued: bool = False
    threshold_source: str | None = None
    primitive: str | None = None
    manifest_entry_ref: str | None = None
    findings: tuple[str, ...] = ()      # finding kinds the row adds (app_crash, manifest_missing)
    evidence: tuple[str, ...] = ()      # lower results that also applied (1.10)
    exception: str | None = None        # "global_shift" when 3.7 replaced rows 16 to 23
    inputs: Inputs | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        t = load()
        t.check_checkpoint(self.outcome, self.reason, self.gate)
        t.check_member("review_states", self.review_state)
        if self.review_state == "pending" and not (
                self.outcome == "needs_human" and self.reason in ("reviewer_unavailable", "global_shift")):
            raise TaxonomyError("review_state pending only on needs_human reviewer_unavailable or global_shift (4.13)")
        if (self.classification is not None and self.classification.by == "reviewer"
                and self.outcome in ("invariant_violation", "assertion_failed")):
            raise TaxonomyError("classification by reviewer never on invariant_violation or assertion_failed (L-OUT-03)")
        for kind in self.findings:
            t.check_member("finding_kinds", kind)

    def fields(self) -> dict:
        """Keyword arguments for `records.CheckpointOutcome`."""
        return {
            "outcome": self.outcome, "reason": self.reason, "detail": self.detail, "gate": self.gate,
            "side": self.side, "review_state": self.review_state, "classification": self.classification,
            "invariant_ids": list(self.invariant_ids) or None, "assertion_ids": list(self.assertion_ids) or None,
            "decision_ref": self.decision_ref, "stability": self.stability, "stability_cause": self.stability_cause,
            "rescued": self.rescued, "threshold_source": self.threshold_source, "primitive": self.primitive,
            "manifest_entry_ref": self.manifest_entry_ref,
        }


def required_field_errors(c: Classified) -> list[str]:
    """The 4.13 required-fields table for what classification knows (L-OUT-02). `diff` and
    `artifacts` belong to the Diff Engine and Capture and are not checked here."""
    errs: list[str] = []
    cls = c.classification

    def need(ok: object, what: str) -> None:
        if not ok:
            errs.append(f"{c.outcome}: {what}")

    o = c.outcome
    if o == "env_error":
        need(c.gate and c.reason, "gate and reason")
    elif o == "harness_error":
        need(c.reason and c.detail and "watchdog_event" in c.detail, "reason and a Watchdog event id in detail")
    elif o in ("not_run", "needs_human"):
        need(c.reason, "reason")
    elif o == "not_comparable":
        need(c.reason and c.detail, "reason and the differing fields in detail")
    elif o in ("not_settled", "landmark_missing"):
        need(c.reason and c.side and c.detail, "reason, side and detail")
    elif o == "needs_baseline":
        need(c.reason and c.side, "reason and side")
    elif o == "invariant_violation":
        need(c.invariant_ids and c.reason == c.invariant_ids[0], "invariant_ids with the first id as reason")
    elif o == "assertion_failed":
        need(c.assertion_ids and c.reason == f"assertion:{c.assertion_ids[0]}" and c.detail,
             "assertion_ids, reason assertion:<id> and detail")
    elif o == "noise":
        need(cls is not None and cls.by == "rule", "classification by rule")
        need(c.reason == "below_floor" or (c.reason or "").startswith("decision:") and c.decision_ref,
             "below_floor, or decision:<id> with decision_ref")
    elif o == "intended":
        need((cls is not None and cls.by == "reviewer" and cls.confidence is not None)
             or ((c.reason or "").startswith("decision:") and c.decision_ref),
             "a reviewer classification with confidence, or decision:<id> with decision_ref")
        need(c.manifest_entry_ref, "manifest_entry_ref")
    elif o == "collateral":
        need(cls is not None and cls.by == "rule", "classification by rule")
        if c.row in ("16", "17"):
            need(c.primitive, "primitive")
            need(c.manifest_entry_ref or c.reason == "manifest_missing", "manifest_entry_ref")
    return errs


# --------------------------------------------------------------------------------------------
# Row builders


@dataclass(frozen=True)
class _Ctx:
    facts: Facts
    inputs: Inputs
    memory: _Memory


def _join(*parts: str | None) -> str | None:
    text = "; ".join(p for p in parts if p)
    return text or None


def _rule(category: str, c: _Ctx) -> Classification:
    r = c.facts.review
    described = r is not None and r.state == "answered"
    return Classification(category, "rule", description=r.description if described else None,
                          region_pointer=r.region_pointer if described else None)


def _by_reviewer(category: str, c: _Ctx) -> Classification:
    r = c.facts.review
    assert r is not None and r.state == "answered"
    return Classification(category, "reviewer", confidence=r.confidence, model_id=r.model_id,
                          prompt_hash=r.prompt_hash, description=r.description, region_pointer=r.region_pointer)


def _cond(c: Condition) -> dict:
    return {"reason": c.reason, "side": c.side, "detail": c.detail}


def _b_env(c: _Ctx) -> dict:
    e = c.facts.env
    return {"gate": e.gate, "reason": e.reason, "detail": e.detail}


def _b_app(c: _Ctx) -> dict:
    a = c.facts.app
    return {"reason": "assertion:app_alive", "assertion_ids": ("app_alive",),
            "detail": _join(f"cause: {a.cause}", a.detail), "findings": ("app_crash",)}


def _b_harness(c: _Ctx) -> dict:
    h = c.facts.harness
    return {"reason": h.reason, "detail": f"watchdog_event: {h.event_id}"}


def _b_not_settled(c: _Ctx) -> dict:
    if c.facts.not_settled is not None:
        return _cond(c.facts.not_settled)
    ids = ", ".join(c.facts.invariant_ids)
    return {"reason": "unstable", "side": "candidate", "stability": "unstable",
            "detail": f"invariant violation not reproduced on the re-capture, kept as evidence: {ids}"}


def _b_invariant(c: _Ctx) -> dict:
    ids = tuple(sorted(set(c.facts.invariant_ids), key=INVARIANT_IDS.index))
    return {"reason": ids[0], "invariant_ids": ids,
            "stability": c.facts.invariant_stability, "stability_cause": c.facts.invariant_stability_cause}


def _b_assertion(c: _Ctx) -> dict:
    ids = c.facts.assertion_ids
    return {"reason": f"assertion:{ids[0]}", "assertion_ids": ids, "detail": c.facts.assertion_detail}


def _b_baseline(c: _Ctx) -> dict:
    return {"reason": c.facts.base_reason, "side": "base"}


def _b_unreadable(c: _Ctx) -> dict:
    f = c.facts
    reason = "oracle_unreadable" if f.oracle_unreadable else "bounds_unreadable"
    detail = _join(f"oracle fields: {', '.join(f.oracle_unreadable)}" if f.oracle_unreadable else None,
                   f"elements: {', '.join(f.bounds_unreadable)}" if f.bounds_unreadable else None)
    return {"reason": reason, "detail": detail, "classification": _rule("needs_human", c)}


def _b_noise_floor(c: _Ctx) -> dict:
    return {"reason": "below_floor", "classification": _rule("noise", c)}


def _b_needs_human(reason: str, detail: Callable[[_Ctx], str | None] = lambda c: None) -> Callable[[_Ctx], dict]:
    return lambda c: {"reason": reason, "detail": detail(c), "classification": _rule("needs_human", c)}


def _b_unstable(c: _Ctx) -> dict:
    return {"reason": "unstable", "side": "candidate", "stability": "unstable",
            "detail": "a re-capture differed above threshold; both captures attached"}


def _b_decision(category: str) -> Callable[[_Ctx], dict]:
    def build(c: _Ctx) -> dict:
        d = c.memory.decision
        assert d is not None
        out = {"reason": f"decision:{d.decision_id}", "decision_ref": d.decision_id,
               "detail": f"suppressed by decision #{d.decision_id}", "classification": _rule(category, c)}
        if category == "intended":
            out["manifest_entry_ref"] = d.manifest_entry_ref or c.facts.manifest_entry_ref
        return out
    return build


def _b_collateral(c: _Ctx) -> dict:
    return {"classification": _rule("collateral", c)}


def _b_manifest_missing(c: _Ctx) -> dict:
    return {"reason": "manifest_missing", "classification": _rule("collateral", c), "findings": ("manifest_missing",),
            "detail": "no manifest; every change is reported as collateral until one is added"}


def _b_intended_by_reviewer(note: str) -> Callable[[_Ctx], dict]:
    return lambda c: {"classification": _by_reviewer("intended", c), "review_state": "done", "detail": note}


def _verdict_note(c: _Ctx) -> str:
    r = c.facts.review
    assert r is not None
    return f"verdict: {r.verdict} ({r.confidence:.2f})"


def _b_no_match(c: _Ctx) -> dict:
    r = c.facts.review
    return {"reason": "reviewer_no_match", "classification": _by_reviewer("needs_human", c), "review_state": "done",
            "detail": _join(_verdict_note(c), f"defect class: {r.defect_class}" if r.defect_class else None)}


def _b_uncertain(c: _Ctx) -> dict:
    return {"reason": "reviewer_uncertain", "classification": _by_reviewer("needs_human", c),
            "review_state": "done", "detail": _verdict_note(c)}


def _unanswered(c: _Ctx) -> dict:
    """R `unavailable`, `capped` or `pending` (rows 23, 25, 25a)."""
    r, value = c.facts.review, c.inputs.R
    out = {"classification": _rule("needs_human", c)}
    if value == "pending":
        out.update(reason="reviewer_unavailable", review_state="pending", detail="awaiting the Reviewer")
    elif value == "unavailable":
        out.update(reason="reviewer_unavailable", review_state="done",
                   detail=_join("the Reviewer job gave up", r.cause if r is not None else None))
    else:
        queued = r is not None and r.queued
        cause = r.cause if r is not None else None
        out.update(reason="reviewer_cap", review_state="done" if queued else "none",
                   detail="cause: dollar_cap" if cause == "dollar_cap" else "beyond the run's reviewer call cap")
    return out


def _b_removed_other(c: _Ctx) -> dict:
    if c.inputs.R in ("unavailable", "capped"):
        out = _unanswered(c)
        return {**out, "detail": _join(out["detail"], "both probe screenshots")}
    return {"reason": "reviewer_no_match", "classification": _by_reviewer("needs_human", c), "review_state": "done",
            "detail": _join(_verdict_note(c), "the verdict does not confirm the removal; both probe screenshots")}


_BUILDERS: dict[str, Callable[[_Ctx], dict]] = {
    "1": _b_env,
    "1a": _b_app,
    "2": _b_harness,
    "3": lambda c: _cond(c.facts.not_run),
    "4": lambda c: _cond(c.facts.not_comparable),
    "5": _b_not_settled,
    "6": lambda c: _cond(c.facts.landmark),
    "7": _b_invariant,
    "8": _b_assertion,
    "9": _b_baseline,
    "9a": _b_unreadable,
    "10": lambda c: {},
    "11": _b_noise_floor,
    "11a": _b_needs_human("rescued_capture", lambda c: "reached through passed_via_agent_fallback; see the locator_failure finding"),
    "12": _b_unstable,
    "13": _b_decision("noise"),
    "14": _b_decision("intended"),
    "15": _b_needs_human("uncalibrated", lambda c: c.facts.fix_list_item),
    "16": _b_collateral,
    "17": _b_manifest_missing,
    "18": _b_needs_human("no_manifest", lambda c: "no manifest, no UI code touched; changes need a human"),
    "19": _b_needs_human("rescued_capture", lambda c: "reached through passed_via_agent_fallback; see the locator_failure finding"),
    "20": _b_intended_by_reviewer("Reviewer confirmed the change against the manifest entry"),
    "21": _b_no_match,
    "22": _b_uncertain,
    "23": _unanswered,
    "24": _b_intended_by_reviewer("removal confirmed against the entry"),
    "25": _unanswered,
    "25a": _b_removed_other,
    "26": lambda c: {"classification": _rule("collateral", c),
                     "detail": "removed surface no loaded manifest entry declares; both probe screenshots"},
}


# --------------------------------------------------------------------------------------------
# Classification


def _applicable_reasons(c: _Ctx) -> list[str]:
    f, i = c.facts, c.inputs
    present = {
        "oracle_unreadable": bool(f.oracle_unreadable),
        "bounds_unreadable": bool(f.bounds_unreadable),
        "rescued_capture": i.P == "rescued",
        "uncalibrated": i.D == "above" and i.T == "uncalibrated",
        "global_shift": i.D == "above" and f.global_shift,
        "no_manifest": i.D == "above" and i.X == "no_manifest_nonui",
    }
    return [r for r in NEEDS_HUMAN_ORDER if present.get(r)]


def _gate_evidence(c: _Ctx, chosen: str, row_id: str) -> list[str]:
    f = c.facts
    notes = {
        "env": lambda: f"env_error (gate: {f.env.gate}, reason: {f.env.reason})",
        "app": lambda: f"app failure ({f.app.cause})",
        "harness": lambda: f"harness_error ({f.harness.reason})",
        "not_run": lambda: f"not_run ({f.not_run.reason})",
        "not_comparable": lambda: f"not_comparable ({f.not_comparable.reason})",
        "not_settled": lambda: f"not_settled ({f.not_settled.reason if f.not_settled else 'unstable'})",
        "landmark": lambda: f"landmark_missing ({f.landmark.reason})",
    }
    present = _gate_present(f)
    out = [notes[g]() for g in G_ORDER if g not in ("ok", chosen) and present[g]]
    if f.invariant_ids and row_id != "7":
        out.append(f"invariant_violation ({', '.join(f.invariant_ids)})")
    if f.assertion_ids and row_id != "8":
        out.append(f"assertion_failed ({', '.join('assertion:' + a for a in f.assertion_ids)})")
    return out


def _shadow(facts: Facts) -> Classified:
    """What the table gives without the invariant and assertion results (1.10: lower results stay
    on the record as evidence)."""
    lower = replace(facts, invariant_ids=(), invariant_stability=None, invariant_stability_cause=None,
                    assertion_ids=(), assertion_detail=None, decisions=())
    return _classify(lower, strict=False)


def _classify(facts: Facts, *, strict: bool) -> Classified:
    inputs = derive_inputs(facts)
    row = match_row(inputs)
    if strict and row.id in STABILITY_ROWS and facts.stability is None:
        raise ValueError(f"row {row.id} reads S: an above-threshold diff needs its stability (3.10)")
    memory = _memory(facts)
    c = _Ctx(facts, inputs, memory)
    compared = inputs.G == "ok" and inputs.B == "present"
    out = {"stability": facts.stability, "stability_cause": facts.stability_cause, "rescued": facts.rescued,
           "threshold_source": facts.threshold_source if compared else None,
           "primitive": facts.primitive, "manifest_entry_ref": facts.manifest_entry_ref}
    out.update(_BUILDERS[row.id](c))
    if out.get("stability") != "unknown":
        out["stability_cause"] = None
    detail_parts: list[str | None] = [out.pop("detail", None)]
    evidence: list[str] = []

    if row.id in ("1", "1a", "2", "3", "4", "5", "6", "7", "8"):
        evidence += _gate_evidence(c, inputs.G, row.id)
    if row.id in ("7", "8"):
        shadow = _shadow(facts)
        reason = f" ({shadow.reason})" if shadow.reason else ""
        evidence.append(f"row {shadow.row}: {shadow.outcome}{reason}")
        if shadow.classification is not None and shadow.classification.by == "rule":
            out["classification"] = shadow.classification
        elif facts.review is not None and facts.review.state == "answered":
            evidence.append(f"reviewer verdict {facts.review.verdict} ({facts.review.confidence:.2f}) "
                            "attached to the finding; it never moves the outcome")
        if memory.decision is not None:
            out["decision_ref"] = memory.decision.decision_id
            evidence.append(f"finding suppressed by decision #{memory.decision.decision_id}; the outcome never changes")
    elif inputs.G == "ok":
        also = [r for r in _applicable_reasons(c) if r != out.get("reason")]
        if also:
            detail_parts.append("also: " + ", ".join(also))

    if memory.known_bug is not None and inputs.G == "ok":
        bug = memory.known_bug
        out["decision_ref"] = bug.decision_id
        if row.outcome == "collateral" and out.get("reason") is None:
            out["reason"] = f"decision:{bug.decision_id}"
        detail_parts.append(f"known bug: decision #{bug.decision_id}, ticket {bug.ticket}")
    if memory.conflict:
        evidence.append("conflicting decisions " + " and ".join(
            f"#{d.decision_id} ({d.verdict})" for d in memory.conflict))

    exception = None
    if row.id in GLOBAL_SHIFT_ROWS and facts.global_shift:
        exception = "global_shift"
        replaced = f"in place of row {row.id} ({row.outcome}{', ' + out['reason'] if out.get('reason') else ''})"
        out.update(reason="global_shift", classification=Classification("needs_human", "rule"))
        detail_parts = [replaced] + [p for p in detail_parts if p and not p.startswith("also:")]
        also = [r for r in _applicable_reasons(c) if r != "global_shift"]
        if also:
            detail_parts.append("also: " + ", ".join(also))
        outcome = "needs_human"
    else:
        outcome = row.outcome

    return Classified(row=row.id, outcome=outcome, detail=_join(*detail_parts), evidence=tuple(evidence),
                      exception=exception, inputs=inputs, **out)


def classify(facts: Facts) -> Classified:
    """Apply 3.11 to one checkpoint's facts: exactly one outcome, the row that fired, and the
    outcome record's fields."""
    return _classify(facts, strict=True)


# --------------------------------------------------------------------------------------------
# Later changes to a written outcome (4.13)


@dataclass(frozen=True)
class LateVerdict:
    """The result of applying an asynchronous verdict to an item (F28)."""

    result: Classified
    annotation_only: bool               # the verdict changed nothing but review_state
    eval_dev_split: bool                # disagrees with the human decision: joins the dev split (3.15)
    annotation: str


def _agrees(decision_verdict: str, r: str) -> bool:
    return {"intended": "match_hi", "confirmed_bug": "no_match", "noise": "nothing"}.get(decision_verdict) == r


def apply_late_verdict(facts: Facts, current: Classified, review: Review, *,
                       human_decision: Decision | None = None) -> LateVerdict:
    """F28 and 3.11 notes: a verdict landing on a pending item re-evaluates it through the table
    with the verdict as R, and `review_state` goes `pending` to `done`. With a human decision on
    the item, or on an item no longer pending, the verdict is an annotation only and never
    changes the outcome or the decision."""
    if review.state != "answered":
        raise ValueError("a late verdict is an answered review; use close_pending when the job gives up")
    note = f"Reviewer: {review.verdict} ({review.confidence:.2f})"
    if human_decision is not None:
        disagree = not _agrees(human_decision.verdict, reviewer_input(review))
        done = replace(current, review_state="done") if current.review_state == "pending" else current
        return LateVerdict(done, True, disagree, f"{note}; human decision #{human_decision.decision_id} stands")
    if current.review_state != "pending":
        return LateVerdict(current, True, False, f"{note}; the item was not awaiting a verdict")
    result = classify(replace(facts, review=review))
    return LateVerdict(result, False, False, note)


def close_pending(facts: Facts, current: Classified, *, cause: str) -> Classified:
    """End review for a pending item that never got a verdict (4.13, 3.15): `gave_up` keeps
    `reviewer_unavailable`; `dollar_cap` (the per-run spend ceiling ended the run) gives
    `reviewer_cap` with `cause: dollar_cap`. The outcome stays `needs_human`; `review_state`
    goes to `done`."""
    if current.review_state != "pending":
        raise ValueError("only a pending item can be closed")
    if cause == "gave_up":
        review = Review("unavailable", cause="the Reviewer job gave up")
    elif cause == "dollar_cap":
        review = Review("capped", cause="dollar_cap", queued=True)
    else:
        raise ValueError(f"cause {cause!r}: gave_up or dollar_cap")
    return classify(replace(facts, review=review))
