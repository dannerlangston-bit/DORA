"""The merge, RC and Nightly sections of a run's page, built from the store and the run's config (9.4, 9.5; 2.13,
3.8, 5.13, 6.6): what every HTML render call passes as `html.PageSections`.

`page_sections(store, run_id, cfg, manifest=)`:
- `safety`: the 6.6 ORDER SAFETY PROOF blocks of the run's `post_run_proofs` rows with each shard's run-manifest
  facts (`summary["safety"]["shards"]`, `safety.ShardSafety`), the A1 to A11 table, the previous night's fire drill
  and safety canaries, the safety canary banner, the open incidents and, on an RC run, the 6.13 residuals
  (`safety.render`);
- `blindspots`: the 2.13 not-detectable table (`blindspots.compute`);
- `sensitivity`: the 3.8 sensitivity report against the week-earlier run of the same type (`sensitivity.compute`
  over `summary["sensitivity"]`, which the run writes at diff time);
- `breakdown`: the 5.13 run health breakdown, with the 5.5 rescue cap of `policy/budgets.yaml`
  (`breakdown.query`);
- `manifest_surfaces` (the declared surfaces of the run's manifest: red coverage rows without a checkpoint) and
  `surface_tiers` (the registry tier of every surface: the coverage tier filter).

Choice (reported): a run whose summary has no shard safety facts or no diff-time sensitivity rows (a run recorded
before the runtime wrote them) gets the blocks and the report from the store rows alone: the proof blocks without
the manifest facts and the sensitivity report from each outcome's `masked_pct` (sensitivity choice 2).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..executor.locator import RescueCap
from ..store.safety import open_quarantines
from ..store.store import Store
from . import blindspots, breakdown, safety, sensitivity
from .html import PageSections
from .model import ReportInputs


def _shards(summary: Mapping[str, Any]) -> list[safety.ShardSafety]:
    block = summary.get("safety") if isinstance(summary.get("safety"), Mapping) else {}
    return [safety.ShardSafety.from_dict(d) for d in block.get("shards") or ()]


def declared_surfaces(manifest: Mapping[str, Any] | None) -> frozenset[str]:
    """The surfaces a 4.3 manifest declares (every entry's `declared_surfaces`)."""
    if not isinstance(manifest, Mapping):
        return frozenset()
    return frozenset(str(s) for e in manifest.get("entries") or () if isinstance(e, Mapping)
                     for s in e.get("declared_surfaces") or ())


def page_sections(store: Store, run_id: str, cfg: Any, *, manifest: Mapping[str, Any] | None = None) -> PageSections:
    """The sections of `run_id`'s page (module docstring); `cfg` is the run's `pipeline.config.SentinelConfig`."""
    run = store.query("SELECT r.run_type, r.started_at, p.candidate_build_id FROM runs r LEFT JOIN build_pairs p "
                      "ON p.pair_id = r.pair_id WHERE r.run_id = ?", (run_id,))[0]
    summary = store.run_summary(run_id)
    scope = (summary.get("coverage") or {}).get("scope") or {}
    block = summary.get("safety") if isinstance(summary.get("safety"), Mapping) else {}
    section = safety.render(ReportInputs.from_store(store, run_id).safety, shards=_shards(summary),
                            header=safety.ProofHeader(run["started_at"], run["candidate_build_id"]),
                            send_policy=cfg.send_policy, order_surfaces_not_run=scope.get("order_surfaces_entitlement"),
                            quarantines=open_quarantines(store), banner=block.get("banner"),
                            run_type=run["run_type"])
    outcomes, findings = store.outcomes(run_id), store.findings(run_id)
    prev_id, prev = sensitivity.previous_rows(store, run_id)
    budgets = cfg.repo.one("budgets")
    cap = RescueCap.from_budgets(budgets.data if budgets is not None and isinstance(budgets.data, Mapping) else {})
    return PageSections(
        safety=section, blindspots=blindspots.compute(cfg, outcomes, findings=findings),
        sensitivity=sensitivity.compute(summary.get("sensitivity") or {}, prev, outcomes=outcomes, regions=cfg.regions,
                                        previous_run_id=prev_id),
        breakdown=breakdown.query(store, run_id, rescue_cap=cap.max_steps),
        manifest_surfaces=declared_surfaces(manifest),
        surface_tiers={sid: s.tier for sid, s in cfg.registry.surfaces.items()})


__all__ = ["declared_surfaces", "page_sections"]
