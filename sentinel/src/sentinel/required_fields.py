"""The 4.13 required-fields table (L-OUT-02) and the capture provenance rule of SD-48, in one validator.

Classification (`classify.required_field_errors`) and the store (`Store.record_errors`, and `write_checkpoint` on a
store opened with `strict_records`) both call `errors`. It reads the record's fields by name, so it takes a
`records.CheckpointOutcome` or a `classify.Classified` (which has no diff, artifacts or flow id; the caller passes
`evidence=False` and leaves `flow_steps` unset).

Choices where 4.13 is ambiguous or silent (each cites what it reads):
- "Required beyond the common fields": the common fields are the record's own construction checks (taxonomy, ids,
  persona); this module checks the per-outcome column of the table, plus the conditional common fields of 4.13 that
  depend on the record's evidence: `fingerprint_id` and `data_epoch_id` when the record references a capture
  artifact (SD-48). `paper_assertion` (L-OUT-06) needs the session and is the store's check.
- `intended` needs `diff` and `manifest_entry_ref` in both of its forms, and one of a reviewer classification with
  its confidence or `decision:<id>` with `decision_ref` (the table's commas bind tighter than its "or").
- `collateral`: a record without `diff` is the undeclared removed surface of 3.11 row 26, whose two probe
  screenshots (`artifacts.base`, the label on base, and `artifacts.candidate`, absent on candidate) replace `diff`
  and which needs `primitive` and `manifest_entry_ref` only when linked, so not here. Any other collateral needs
  `diff`, `primitive` and `manifest_entry_ref` (absent only with reason `manifest_missing`). Classification passes
  `removed` from its row instead.
- `invariant_violation`: `reason` is the first of `invariant_ids` by catalog order (4.13 reason vocabulary: "the
  outcome names the first by catalog order"). `assertion_failed`: `reason` is `assertion:<first of assertion_ids>`.
- `env_error`: "a screen was captured before the failure" is read from `captured_at`; with it set the record needs
  `artifacts.candidate` (the last screenshot).
- `harness_error`: "a Watchdog event id in detail" is `watchdog_event: <id>` (the form classification writes).
- `not_run (reason: step_failed:<step>)`: with `flow_steps` (flow id to its step ids) given, the record needs its
  `flow_id` and the step must be one of that flow's steps; without it (classification) the step is not checked.
- Synthetic records (canary mutants, 10.3) are generated, not captured: the capture provenance rule skips them.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

CAPTURE_KEYS = ("candidate", "base", "heatmap")
WATCHDOG_EVENT = re.compile(r"\bwatchdog_event:\s*\S+")
DIFF_OUTCOMES = ("identical", "noise", "intended", "needs_human")     # 4.13: `diff` required (collateral below)
PENDING_REASONS = ("reviewer_unavailable", "global_shift")


def references_capture(rec: Any) -> bool:
    """True when the record names a capture artifact: base, candidate, heatmap or a crop (4.13 `artifacts`)."""
    arts = getattr(rec, "artifacts", None) or {}
    return any(arts.get(k) for k in CAPTURE_KEYS) or bool(arts.get("crops"))


def _decision_form(rec: Any) -> bool:
    return (getattr(rec, "reason", None) or "").startswith("decision:") and bool(getattr(rec, "decision_ref", None))


def _first_by_catalog(ids: Collection[str]) -> str:
    def key(i: str) -> tuple[int, str]:
        m = re.fullmatch(r"INV-(\d+)", i)
        return (int(m.group(1)) if m else 1 << 30, i)
    return min(ids, key=key)


def errors(rec: Any, *, evidence: bool = True, flow_steps: Mapping[str, Collection[str]] | None = None,
           removed: bool | None = None) -> list[str]:
    """What the record lacks under the 4.13 required-fields table for its outcome; empty when it is complete.

    `evidence=False` skips what the Diff Engine and Capture own (`diff`, `artifacts`, the capture provenance), for a
    caller that has not attached them yet. `flow_steps` maps a flow id to its step ids, for `step_failed:<step>`.
    `removed` says whether a `collateral` is the undeclared removed surface of 3.11 row 26; None infers it from a
    missing `diff`.
    """
    out: list[str] = []
    o = rec.outcome
    reason = getattr(rec, "reason", None)
    detail = getattr(rec, "detail", None)
    cls = getattr(rec, "classification", None)
    diff = getattr(rec, "diff", None)
    arts = getattr(rec, "artifacts", None) or {}

    def need(ok: object, what: str) -> None:
        if not ok:
            out.append(f"{o}: {what} (4.13 required fields, L-OUT-02)")

    if evidence and o in DIFF_OUTCOMES:
        need(diff is not None, "diff")
    if o == "noise":
        need(cls is not None and cls.by == "rule", "classification by rule")
        need(reason == "below_floor" or _decision_form(rec), "reason below_floor, or decision:<id> with decision_ref")
    elif o == "intended":
        need(getattr(rec, "manifest_entry_ref", None), "manifest_entry_ref")
        need((cls is not None and cls.by == "reviewer" and cls.confidence is not None) or _decision_form(rec),
             "a reviewer classification with its confidence, or decision:<id> with decision_ref")
    elif o == "collateral":
        need(cls is not None and cls.by == "rule", "classification by rule")
        is_removed = (diff is None) if removed is None else removed
        if is_removed:
            if evidence:
                need(arts.get("base") and arts.get("candidate"),
                     "diff, or the two probe screenshots of a removed surface (artifacts base and candidate)")
        else:
            if evidence:
                need(diff is not None, "diff")
            need(getattr(rec, "primitive", None), "primitive")
            need(getattr(rec, "manifest_entry_ref", None) or reason == "manifest_missing",
                 "manifest_entry_ref (absent only with reason manifest_missing)")
    elif o == "needs_human":
        need(reason, "reason")
        if getattr(rec, "review_state", None) == "pending":
            need(reason in PENDING_REASONS, "review_state pending only with reviewer_unavailable or global_shift")
    elif o == "invariant_violation":
        ids = list(getattr(rec, "invariant_ids", None) or ())
        need(ids and reason == _first_by_catalog(ids) and reason in ids,
             "invariant_ids, with reason the first id by catalog order")
        if evidence:
            need(arts.get("candidate"), "artifacts.candidate")
    elif o == "assertion_failed":
        ids = list(getattr(rec, "assertion_ids", None) or ())
        need(ids and reason == f"assertion:{ids[0]}", "assertion_ids, with reason assertion:<id>")
        need(detail, "expected and extracted values in detail")
    elif o in ("not_settled", "landmark_missing"):
        need(reason and getattr(rec, "side", None) and detail,
             "reason, side and detail naming the anchor or landmark and the wait used")
    elif o == "not_comparable":
        need(reason and detail, "reason and the differing field names in detail")
    elif o == "env_error":
        need(getattr(rec, "gate", None) and reason, "gate and reason")
        if evidence and getattr(rec, "captured_at", None):
            need(arts.get("candidate"), "artifacts.candidate (the last screenshot; a screen was captured)")
    elif o == "harness_error":
        need(reason and detail and WATCHDOG_EVENT.search(detail), "reason and a Watchdog event id in detail")
    elif o == "not_run":
        need(reason, "reason")
        if flow_steps is not None and (reason or "").startswith("step_failed:"):
            step, flow = reason.split(":", 1)[1], getattr(rec, "flow_id", None)
            need(flow and step in flow_steps.get(flow, ()), f"step_failed:{step} names an existing step of flow_id "
                                                            f"{flow}")
    elif o == "unregistered_surface":
        need((reason or "").startswith("probe:") and detail, "reason probe:<Pn> and detail with the label and where")
    elif o == "needs_baseline":
        need(reason and getattr(rec, "side", None), "reason and side")
    if evidence and references_capture(rec) and not getattr(rec, "synthetic", False):
        need(getattr(rec, "fingerprint_id", None) and getattr(rec, "data_epoch_id", None),
             "fingerprint_id and data_epoch_id on a record that references a capture artifact (SD-48)")
    return out
