"""Who hears about the night's canary state: the 9.10 page and the SENT Bug of 9.9 (10.3 Scoring, Morning message).

`alert_route(state)` is the 9.10 "Canary failure" row: P1 to the harness on-call for `detector_blind`,
`generation_failed` and `canary_path_divergence`, P2 to the on-call channel for `detector_degraded`, none for `ok`.
`alert_entries(detector_state, run_id)` gives the `alerts.json` entries of a Nightly run in the shape
`pipeline.orchestration.alert_entry` writes. `sent_bug_payload(report, run_id=...)` builds the SENT Bugs of 9.9:
Highest for `detector_blind` and `generation_failed`, High for `detector_degraded`, deduplicated by mutant id, by
class for a floor miss, or by the stage for `generation_failed`. Posting them is the outbox's (9.1); these are the
payloads only.

Choices where the spec is ambiguous (reported):

1. Rule names: the three canary states share the 9.10 row, rule `canary_failure`; the diverged pair is its own
   rule, `canary_path_divergence` (9.10 harness_error row: "pages at once"), so a night with both pages twice.
   The safety canaries and drill items are paged by their own `drill_failure` route (6.5), not here.
2. A blind money-cell mutant and a blind redaction canary (M19) each get a Highest Bug keyed by the mutant id, and
   M19 is not filed again under its degraded class; the run state `generation_failed` gets one Highest Bug keyed by
   the stage, `stage:canary_generation`.
3. Labels: `sentinel`, and per checkpoint `sentinel-surface-<surface id>` and `sentinel-cp-<first 12 hex of the
   sha256 of the checkpoint id>` (9.9); a canary row carries no tier, so no `sentinel-tier-*` label. The
   description carries the 9.9 identity block with the dedup key under `canary`.
"""
from __future__ import annotations

import hashlib
from typing import Any

from .canary import CanaryReport, CanaryResult, DetectorState
from .orchestrator.alerts import ON_CALL_CHANNEL, P1, P2, AlertRoute
from .taxonomy import checkpoint_parts

PROJECT = "SENT"                                   # 9.9: the harness project (assumed key)
GENERATION_STAGE = "stage:canary_generation"       # choice 2


def alert_route(state: str) -> AlertRoute | None:
    """The 9.10 route of a canary state or of `canary_path_divergence`; None for `ok`."""
    if state in ("detector_blind", "generation_failed"):
        return AlertRoute("canary_failure", P1, ("harness_on_call",), (ON_CALL_CHANNEL,))
    if state == "canary_path_divergence":
        return AlertRoute("canary_path_divergence", P1, ("harness_on_call",), (ON_CALL_CHANNEL,))
    if state == "detector_degraded":
        return AlertRoute("canary_failure", P2, (), (ON_CALL_CHANNEL,))
    if state == "ok":
        return None
    raise ValueError(f"canary state {state!r} (10.3)")


def alert_entries(ds: DetectorState, run_id: str, shard_id: str | None = None) -> list[dict[str, Any]]:
    """The `alerts.json` entries the night's detector state raises (`pipeline.orchestration.alert_entry` shape):
    one for the mutants' canary state and one for a diverged path-equivalence pair."""
    out = []
    first = (ds.banner or "").split("\n")[0]
    for key, detail in ((ds.canary_state, first + (f" Classes: {', '.join(ds.classes)}." if ds.classes else "")),
                        ("canary_path_divergence" if ds.divergence else "ok",
                         "harness_error (reason: canary_path_divergence) on the re-evaluated merge-run pair")):
        a = alert_route(key)
        if a is not None:
            out.append({"run_id": run_id, "shard_id": shard_id, "rule": a.rule, "priority": a.priority,
                        "paged": list(a.paged), "notified": list(a.notified), "safety_table": a.is_safety,
                        "control": "canary", "gate": None, "incident": None, "run_outcome": None, "run_reason": None,
                        "detail": detail})
    return out


def _labels(checkpoints: list[str]) -> list[str]:
    out = ["sentinel"]
    for cid in checkpoints:
        for label in (f"sentinel-surface-{checkpoint_parts(cid)['surface']}",
                      f"sentinel-cp-{hashlib.sha256(cid.encode()).hexdigest()[:12]}"):
            if label not in out:
                out.append(label)
    return out


def _bug(priority: str, key: str, summary: str, rows: list[CanaryResult], state: str, run_id: str,
         report_link: str | None, lines: list[str]) -> dict[str, Any]:
    block = ["sentinel:", f"  canary: {key}", f"  state: {state}", f"  run: {run_id}"]
    block += [f"  checkpoint: {cid}" for cid in dict.fromkeys(r.checkpoint_id for r in rows)]
    if report_link:
        block.append(f"  report: {report_link}")
    return {"project": PROJECT, "issuetype": "Bug", "priority": priority, "dedup_key": key,
            "summary": f"[Sentinel] canary {state}: {summary}",
            "labels": _labels(list(dict.fromkeys(r.checkpoint_id for r in rows))),
            "description": "\n".join(lines + [""] + block)}


def sent_bug_payload(report: CanaryReport, *, run_id: str, report_link: str | None = None) -> list[dict[str, Any]]:
    """The SENT Bugs the night's canary result files (9.9, 10.3): none when the state is ok."""
    if report.state == "ok":
        return []
    bugs = []
    blind = [r for r in report.results if r.kind == "mutant" and (
        r.state == "detector_blind" or (r.defect_class == "redaction" and r.state not in ("detected", "generation_failed")))]
    for r in blind:
        produced = (r.produced_outcome or "nothing") + (f" ({r.produced_reason})" if r.produced_reason else "")
        bugs.append(_bug("Highest", r.mutant_id, f"{r.mutant_id} {r.defect_class} missed on {r.checkpoint_id}", [r],
                         "detector_blind", run_id, report_link,
                         [f"Mutant {r.mutant_id} ({r.defect_class}) produced {produced}.",
                          f"Absorbed by: {r.absorbed_by or 'not recorded'}."]))
    for cls in report.degraded_classes:
        rows = [r for r in report.results if r.kind == "mutant" and r.defect_class == cls and r.state == "detector_degraded"
                and r not in blind]
        if not rows:                                   # a blind redaction canary already has its Highest Bug
            continue
        bugs.append(_bug("High", cls, f"class {cls} below its rolling floor", rows, "detector_degraded", run_id,
                         report_link, [f"Class {cls}: rate {report.class_rates[cls]:.0%}, floor "
                                       f"{report.class_floors[cls]:.0%}."]
                         + [f"Missed {r.mutant_id} on {r.checkpoint_id}; absorbed by {r.absorbed_by or 'not recorded'}."
                            for r in rows]))
    if report.state == "generation_failed":
        failed = [r for r in report.results if r.state == "generation_failed"]
        bugs.append(_bug("Highest", GENERATION_STAGE, f"canaries did not run ({report.generation_reason})", failed,
                         "generation_failed", run_id, report_link,
                         [f"Canaries did not run: {report.generation_reason}."]
                         + [f"{r.mutant_id} {r.defect_class} on {r.checkpoint_id}: "
                            f"{report.generation_errors.get(f'{r.mutant_id}@{r.checkpoint_id}') or 'not generated'}."
                            for r in failed]))
    return bugs
