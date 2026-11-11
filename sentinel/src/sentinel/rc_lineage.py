"""The RC run's base: the lineage baseline set and the lineage soak (1.9, 3.1, 9.8, 10.11).

Base of an RC run is the last released build: its approved lineage baseline set when that set's stored fingerprint
equals the current one, otherwise the Rig Manager installs the released build and re-captures it on the current
fingerprint, and approved decisions carry over by signature (3.1, 3.11). A stored capture is compared against a
fresh one only when its stored fingerprint, data epoch included, equals the live fingerprint. `resolve` reads the
Central Store and returns the plan:

- mode `stored`: every approved baseline of the lineage was captured on the current fingerprint; the base of each
  scoped checkpoint is its approved image, and a scoped checkpoint the set lacks is `needs_baseline (reason:
  lineage_missing)` (`LineageBase.outcomes`);
- mode `recapture`: the released build is installed and every scoped checkpoint's base is captured on it (the
  11.2 worst case: 1,236 rig-minutes instead of 803).

`lineage_soak` finds the lineage soak: the soak of the last released build's own RC run, its samples, its cycle
captures, the log signatures it saw, and its fingerprint; `comparable` is False unless that fingerprint equals the
current one (10.11), in which case every soak comparison is `needs_baseline` and the candidate curves are shown alone.

Choices (reported):
1. The approved set is every `approved` row of the lineage (one per checkpoint, `ux_baseline_approved`), whatever
   build captured it: an approval is a pointer change and an unchanged checkpoint keeps an older release's image.
2. A row's stored fingerprint is the `env_fingerprints` row of its source run for the build that captured it; a row
   with none, or with several different ones, cannot be compared. The set is reused only when every row matches
   (the set is all or nothing, as 1.9 and 11.2 count it); a set with no rows is re-captured, never `lineage_missing`
   for every checkpoint.
3. The lineage soak run is the latest finished RC run whose candidate was the released build and that recorded soak
   samples; its fingerprint is that of the shard that captured its soak cycles; the soak start is one sampler
   interval (60 s) before its first sample.
4. The lineage run's log signature set is `runs.summary_json` at `rc.soak.log_signatures` (what `pipeline.rc` writes
   for every RC run); a run that recorded none has no set.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .orchestrator.results import Outcome
from .soak import Sample, samples_from_rows

LINEAGE = "released"                               # 8.3 baselines.lineage: the release channel, 'released' in phases 1-3
SAMPLE_INTERVAL_S = 60.0                           # 10.11 resource sampler
_CYCLE = re.compile(r"/soak/c(\d{2})$")


@dataclass(frozen=True)
class LineageBaseline:
    """One approved baseline of the lineage and the fingerprint it was captured on (choice 2)."""

    checkpoint_id: str
    baseline_id: int
    build_id: str
    image_sha256: str
    source_run_id: str | None
    fingerprint_id: str | None
    sidecar_sha256: str | None = None


@dataclass(frozen=True)
class LineageBase:
    """The RC run's base plan (module docstring)."""

    lineage: str
    mode: str                                      # stored | recapture
    released_build_id: str | None
    fingerprint_id: str
    stored: Mapping[str, LineageBaseline] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    recapture: tuple[str, ...] = ()
    detail: str = ""

    def outcomes(self) -> dict[str, Outcome]:
        """`needs_baseline (reason: lineage_missing)` for every scoped checkpoint the stored set lacks."""
        return {cid: Outcome("needs_baseline", "lineage_missing",
                             detail=f"the {self.lineage} lineage has no approved baseline for this checkpoint")
                for cid in self.missing}

    def base_image(self, checkpoint_id: str) -> str | None:
        """The approved image a stored-mode checkpoint is compared against."""
        b = self.stored.get(checkpoint_id)
        return b.image_sha256 if b else None

    def health_line(self) -> str:
        """The run-health line that names the base the RC run used."""
        if self.mode == "stored":
            return (f"RC base: {self.lineage} lineage baseline set on the current fingerprint ({len(self.stored)} "
                    f"approved, {len(self.missing)} lineage_missing)")
        return f"RC base: {self.released_build_id or 'the released build'} re-captured on the current fingerprint " \
               f"({self.detail})"

    def to_summary(self) -> dict[str, Any]:
        return {"lineage": self.lineage, "mode": self.mode, "released_build_id": self.released_build_id,
                "stored": len(self.stored), "lineage_missing": list(self.missing),
                "recaptured": len(self.recapture), "detail": self.detail}


def released_build(store: Any) -> str | None:
    """The last released build (8.3 `builds.released_at` set at GA), or None."""
    rows = store.query("SELECT build_id FROM builds WHERE released_at IS NOT NULL "
                       "ORDER BY released_at DESC, build_id DESC LIMIT 1")
    return rows[0]["build_id"] if rows else None


def _fingerprint_of(store: Any, run_id: str | None, build_id: str) -> str | None:
    if run_id is None:
        return None
    rows = store.query("SELECT DISTINCT fingerprint_sha256 FROM env_fingerprints WHERE run_id = ? AND build_id = ?",
                       (run_id, build_id))
    return rows[0]["fingerprint_sha256"] if len(rows) == 1 else None


def approved_set(store: Any, lineage: str = LINEAGE) -> dict[str, LineageBaseline]:
    """Every approved baseline of the lineage with its stored fingerprint (choices 1 and 2)."""
    rows = store.query("SELECT baseline_id, checkpoint_id, build_id, image_sha256, sidecar_sha256, source_run_id "
                       "FROM baselines WHERE lineage = ? AND state = 'approved' ORDER BY checkpoint_id", (lineage,))
    return {r["checkpoint_id"]: LineageBaseline(r["checkpoint_id"], int(r["baseline_id"]), r["build_id"],
                                                r["image_sha256"], r["source_run_id"],
                                                _fingerprint_of(store, r["source_run_id"], r["build_id"]),
                                                r["sidecar_sha256"])
            for r in rows}


def resolve(store: Any, lineage: str = LINEAGE, *, fingerprint_id: str, scope: Iterable[str],
            released_build_id: str | None = None) -> LineageBase:
    """The RC base plan for the scoped checkpoints (module docstring). `fingerprint_id` is the current live
    fingerprint (4.12, data epoch included)."""
    cids = tuple(dict.fromkeys(scope))
    released = released_build_id or released_build(store)
    approved = approved_set(store, lineage)
    if not approved:
        return LineageBase(lineage, "recapture", released, fingerprint_id, recapture=cids,
                           detail=f"the {lineage} lineage has no approved baseline set")
    stale = sorted(cid for cid, b in approved.items() if b.fingerprint_id != fingerprint_id)
    if stale:
        unknown = [cid for cid in stale if approved[cid].fingerprint_id is None]
        why = (f"{len(stale)} of {len(approved)} approved baselines were captured on another fingerprint"
               + (f" ({len(unknown)} with none recorded)" if unknown else ""))
        return LineageBase(lineage, "recapture", released, fingerprint_id, recapture=cids, detail=why)
    stored = {cid: approved[cid] for cid in cids if cid in approved}
    missing = tuple(cid for cid in cids if cid not in approved)
    return LineageBase(lineage, "stored", released, fingerprint_id, stored, missing,
                       detail="the approved set's fingerprint equals the current one")


# -- the lineage soak (10.11) ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LineageSoak:
    """The lineage soak of the last released build (module docstring)."""

    run_id: str | None
    fingerprint_id: str | None
    comparable: bool
    samples: tuple[Sample, ...] = ()
    cycle_captures: Mapping[int, str] = field(default_factory=dict)
    log_signatures: frozenset[str] | None = None

    def capture(self, cycle: int) -> str | None:
        """The lineage capture at the same cycle index, when comparable."""
        return self.cycle_captures.get(cycle) if self.comparable else None


def _parse(at: str) -> datetime:
    return datetime.fromisoformat(at.replace("Z", "+00:00"))


def lineage_soak(store: Any, released_build_id: str | None, *, fingerprint_id: str) -> LineageSoak:
    """The lineage soak and whether it is comparable with the current fingerprint (10.11; choices 3 and 4)."""
    if released_build_id is None:
        return LineageSoak(None, None, False)
    runs = store.query(
        "SELECT r.run_id FROM runs r JOIN build_pairs p ON p.pair_id = r.pair_id WHERE r.run_type = 'rc' "
        "AND p.candidate_build_id = ? AND r.lifecycle = 'finished' "
        "AND EXISTS (SELECT 1 FROM soak_samples s WHERE s.run_id = r.run_id) "
        "ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1", (released_build_id,))
    if not runs:
        return LineageSoak(None, None, False)
    run_id = runs[0]["run_id"]
    fps = store.query(
        "SELECT DISTINCT f.fingerprint_sha256 FROM env_fingerprints f JOIN outcomes o ON o.shard_id = f.shard_id "
        "WHERE o.run_id = ? AND o.checkpoint_id LIKE 'flow/%/soak/c%' AND f.side = 'candidate'", (run_id,))
    fp = fps[0]["fingerprint_sha256"] if len(fps) == 1 else None
    rows = [dict(r) for r in store.query("SELECT sampled_at, pid, metric, value FROM soak_samples WHERE run_id = ? "
                                         "ORDER BY sampled_at", (run_id,))]
    start = _parse(rows[0]["sampled_at"]) - timedelta(seconds=SAMPLE_INTERVAL_S)
    caps: dict[int, str] = {}
    for r in store.query("SELECT checkpoint_id, cand_capture_sha256 FROM outcomes WHERE run_id = ? "
                         "AND checkpoint_id LIKE 'flow/%/soak/c%' AND cand_capture_sha256 IS NOT NULL", (run_id,)):
        m = _CYCLE.search(r["checkpoint_id"])
        if m:
            caps[int(m.group(1))] = r["cand_capture_sha256"]
    sigs = (store.run_summary(run_id).get("rc") or {}).get("soak", {}).get("log_signatures")
    return LineageSoak(run_id, fp, fp is not None and fp == fingerprint_id, tuple(samples_from_rows(rows, start)),
                       caps, frozenset(sigs) if isinstance(sigs, Sequence) and not isinstance(sigs, str) else None)
