"""The post-run query of 1.4 G2 and 4.13 L-OUT-07, run on a finished run's rows before anything is reported.

`post_run_check(store, run_id, n_surfaces, n_personas, n_profiles)` returns the failures, empty when the run passes:
- L-OUT-07: every scoped checkpoint has exactly one non-synthetic outcome record (the key makes "at most one").
- the taxonomy: every stored outcome, reason (active in the `outcome_reasons` seed and listed for its outcome in
  `taxonomy.yaml`), gate, review state, finding kind and triage state, step outcome and decision value of the run is
  in the closed lists, and so is the run outcome once written (L-OUT-01 re-checked after the fact).
- G2: the coverage matrix has exactly surfaces x personas x entitlement profiles cells, one per key
  (`matrix_failures`).

A failure is `aborted_harness (reason: taxonomy_check_failed)` with the report withheld (4.13 L-OUT-07, 1.4 G2):
`end_taxonomy_check_failed` fills the silent checkpoints with `harness_error (reason: taxonomy_check_failed)` and ends
the run so, with the failures and `report_withheld: true` in its summary. Suppressing the PR comment and the report,
and posting to on-call, is the caller's (the pipeline reads the summary flag).

Choices where the spec is silent (1.4 G2, 2.12, 8.3, 8.7):
- The matrix is the run's `coverage_cells` rows (merge, RC, Nightly), else the `coverage.cells` list of its summary
  JSON (a PR run, 8.3), else the `cells` the caller passes (a PR run checked before teardown writes its summary);
  rows are `coverage.Cell.to_row` dicts, keyed (surface_id, persona, entitlement). `n_surfaces` counts every row of
  the matrix, the synthetic rows of unregistered surfaces included. A run with no matrix at all fails.
- The `harness_error` rows carry the Watchdog event id the caller logged for the failed query (4.13 required fields).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..records import RunOutcome
from .store import Store, seed_rows

FAILED_REASON = "taxonomy_check_failed"
CELL_KEY = ("surface_id", "persona", "entitlement")


def matrix_failures(cells: Iterable[Mapping[str, Any]], n_surfaces: int, n_personas: int,
                    n_profiles: int) -> list[str]:
    """G2: the matrix has surfaces x personas x entitlement profiles cells, one per (surface, persona, profile).
    The Reporter refuses to publish a matrix this fails (1.4 G2)."""
    keys = Counter(tuple(c.get(k) for k in CELL_KEY) for c in cells)
    want = n_surfaces * n_personas * n_profiles
    out = []
    if sum(keys.values()) != want:
        out.append(f"G2 matrix: {sum(keys.values())} cells, expected {n_surfaces} surfaces x {n_personas} personas x "
                   f"{n_profiles} profiles = {want}")
    dup = sorted(k for k, n in keys.items() if n > 1)
    if dup:
        out.append(f"G2 matrix: {len(dup)} cell key(s) appear more than once, first {dup[0]}")
    return out


def _matrix(store: Store, run_id: str, cells: Iterable[Mapping[str, Any]] | None) -> list[Mapping[str, Any]] | None:
    if cells is not None:
        return list(cells)
    rows = store.query("SELECT surface_id, persona, entitlement FROM coverage_cells WHERE run_id = ?", (run_id,))
    if rows:
        return [dict(r) for r in rows]
    coverage = store.run_summary(run_id).get("coverage")
    if isinstance(coverage, Mapping) and isinstance(coverage.get("cells"), list):
        return coverage["cells"]
    return None


def _taxonomy_failures(store: Store, run_id: str) -> list[str]:
    t = store.taxonomy
    allowed = {(o, code) for o, code, *_ in seed_rows(t)}
    out: list[str] = []

    def bad(what: str, rows: Sequence[Any]) -> None:
        if rows:
            out.append(f"taxonomy: {len(rows)} {what}, first {tuple(rows[0])}")

    q = store.query
    bad("outcome row(s) outside checkpoint_outcomes or review_states", [
        r for r in q("SELECT checkpoint_id, outcome, review_state FROM outcomes WHERE run_id = ?", (run_id,))
        if r["outcome"] not in t.checkpoint_outcomes or r["review_state"] not in t.list("review_states")])
    bad("outcome reason(s) retired or not listed for the outcome", [
        r for r in q("SELECT o.checkpoint_id, o.outcome, o.reason, o.reason_detail, s.active, s.parametric "
                     "FROM outcomes o LEFT JOIN outcome_reasons s ON s.outcome = o.outcome AND s.reason = o.reason "
                     "WHERE o.run_id = ? AND o.reason IS NOT NULL", (run_id,))
        if not r["active"] or (r["outcome"], r["reason"]) not in allowed
        or (r["parametric"] and not r["reason_detail"])])
    bad("env_error gate(s) outside gates", [
        r for r in q("SELECT checkpoint_id, gate FROM outcomes WHERE run_id = ? AND outcome = 'env_error'", (run_id,))
        if r["gate"] not in t.gates])
    bad("finding(s) with a kind or triage state outside the lists", [
        r for r in q("SELECT natural_key, kind, triage_state FROM findings WHERE run_id = ?", (run_id,))
        if r["kind"] not in t.finding_kinds or r["triage_state"] not in t.list("triage_states")])
    bad("step event(s) with a step outcome outside step_outcomes", [
        r for r in q("SELECT shard_id, seq, step_outcome FROM step_events WHERE run_id = ? "
                     "AND step_outcome IS NOT NULL", (run_id,)) if r["step_outcome"] not in t.step_outcomes])
    bad("decision(s) with an action, verdict, status or source outside the lists", [
        r for r in q("SELECT decision_id, action, verdict, status, source FROM decisions WHERE run_id = ?", (run_id,))
        if r["action"] not in t.list("decision_actions") or r["verdict"] not in t.list("decision_verdicts")
        or r["status"] not in t.decision_statuses or r["source"] not in t.decision_sources])
    bad("run outcome outside run_outcomes or its reasons", [
        r for r in q("SELECT r.outcome, r.abort_reason, s.active FROM runs r LEFT JOIN outcome_reasons s "
                     "ON s.outcome = r.outcome AND s.reason = r.abort_reason WHERE r.run_id = ? "
                     "AND r.outcome IS NOT NULL", (run_id,))
        if r["outcome"] not in t.run_outcomes
        or (r["abort_reason"] is not None and (not r["active"] or (r["outcome"], r["abort_reason"]) not in allowed))])
    return out


def post_run_check(store: Store, run_id: str, n_surfaces: int, n_personas: int, n_profiles: int, *,
                   cells: Iterable[Mapping[str, Any]] | None = None) -> list[str]:
    """The post-run query of 1.4 G2 and L-OUT-07 on `run_id`: the failures, empty when the run passes (module
    docstring). `cells` is the coverage matrix of a run that has not stored one yet."""
    if not store.query("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)):
        return [f"unknown run {run_id}"]
    out = []
    missing = store.missing_outcomes(run_id)
    if missing:
        out.append(f"L-OUT-07: {len(missing)} scoped checkpoint(s) without an outcome record, first {missing[0]}")
    out += _taxonomy_failures(store, run_id)
    matrix = _matrix(store, run_id, cells)
    if matrix is None:
        out.append("G2 matrix: the run has no coverage matrix")
    else:
        out += matrix_failures(matrix, n_surfaces, n_personas, n_profiles)
    return out


def end_taxonomy_check_failed(store: Store, run_id: str, failures: Sequence[str], *, last_gate: str,
                              watchdog_event_id: str, ended_at: str | None = None) -> RunOutcome:
    """End a run whose post-run check failed (4.13 L-OUT-07, 1.4 G2): every silent scoped checkpoint gets
    `harness_error (reason: taxonomy_check_failed)` with the Watchdog event id, and the run ends `aborted_harness
    (reason: taxonomy_check_failed)` with `post_run_check: {failures, report_withheld: true}` in its summary."""
    if not failures:
        raise ValueError("end_taxonomy_check_failed needs the failures post_run_check returned")
    run = store.query("SELECT run_type FROM runs WHERE run_id = ?", (run_id,))
    if not run:
        raise ValueError(f"unknown run {run_id}")
    detail = f"watchdog_event: {watchdog_event_id}; post-run check failed: {failures[0]}"
    store.fill_unreached(run_id, "harness_error", FAILED_REASON, detail=detail)
    return store.end_run(RunOutcome(run_id, run[0]["run_type"], "aborted_harness", last_gate, reason=FAILED_REASON,
                                    ended_at=ended_at),
                         summary={"post_run_check": {"failures": list(failures), "report_withheld": True}})
