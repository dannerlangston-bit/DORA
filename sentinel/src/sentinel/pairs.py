"""Build pairs at run time: the PR base, a missing base, and frozen base-capture reuse.

Spec: 1.6 (PR row), 1.7 T+0:00 and T+0:15 to 0:35, 1.10 (`aborted_harness`), 3.1 (build pair rules), 3.11 input B
(`reuse_missing`), 4.2 (last paragraph, L-BLD-04), 4.9 and 7.9 (the reuse key), 7.4 (pairing by data mode,
`base_reused`), 9.2 (abort block, the run-health base row), 11.2 (scheduling rule), SD-02, SD-13, SD-68.

The PR base (SD-02, 3.1, 4.2):

- `resolve_base(candidate, main_history, builds)`: the main build at the candidate's merge base, or the nearest main
  ancestor of it with a build record, at most `MAX_BASE_DISTANCE` (20) commits back. The result names the base
  build, its commit, `base_commit_distance` (for `RunPlan`/`RunOutcome.base_commit_distance` and the 9.2 base row,
  `BaseResolution.health_row`) and the manifests of the PRs merged between that build and the merge base, which the
  run treats as declared (`declared_manifest`). None found raises `BaseBuildMissing`.
- `base_missing_result(cause)`: the run-level result of a missing base, `aborted_harness (reason:
  base_build_missing)` at `provisioned` before any capture, every scoped checkpoint `harness_error
  (base_build_missing)` (1.10), the P1 harness page, the abort block of 9.2, the rebuild request for the merge base
  and the single re-queue (3.1). The three causes are `BaseBuildMissing.not_found` (from `resolve_base`),
  `.install_failed` and `.capture_set_absent` (4.2: "the base build cannot be installed or its capture set is
  absent").

Frozen base-capture reuse (3.1, 7.4, 7.9, SD-68):

- `ReuseKey`: `fingerprint_id`, `epoch_id`, the fixture file's sha256 and the golden account's latest `seeded_at`
  (`reuse_key` builds the live one from the fingerprint id and the fixture file's bytes).
- `stored_bases(store, checkpoint_ids, base_build_id=...)`: the stored frozen captures of the base build per
  checkpoint, latest first, read through `Store.query` (no store change). A run records the fixture half of its key
  per shard in its summary (`reuse_fixture_patch`, summary key `reuse_fixture`), since no 8.3 column holds it.
- `reuse_decision(checkpoint_id, live, stored, ...)`: reuse the latest stored capture whose key, region registry
  version and redaction version equal the live ones (`live_versions` reads the live pair from the fingerprint's
  `harness`); else re-capture (3.1: a stored capture whose fingerprint differs is re-captured, never scored);
  `missing` when the plan expected reuse and nothing is stored (3.11 B `reuse_missing`).
- `reused_base(arts, capture)`: the stored bytes, or the `reuse_missing` evidence when they are gone.
- `scope_base_reuse(live, stored, ...)`: the same decision as a `scope.BaseReuse` for `compute_scope`, whose per-rig
  fingerprint check (L-09) then applies.
- `ReuseLedger`: the run's reuse decisions, for the outcome records (`base_source_run_id`), the report context
  (`ReportContext.base_reused_from`) and the run-health row (`base_reused`, 7.4).

Choices where the spec is ambiguous or silent (reported to the coordinator):

1. Main history (3.1) is the first-parent list of main commits, newest first (`git rev-list --first-parent main`),
   each a `MainCommit` naming the manifests of the PRs merged at that commit (by the PR's candidate build id, the
   `manifests/<build_id>.yaml` name). A merge base absent from the history can still resolve at distance 0; with
   no build there either, the walk cannot go further and the run is `base_build_missing`.
2. "Merged between that ancestor and the merge base" (3.1, 4.2) is the commits after the base build's commit up to
   and including the merge base: at distance d the commits at distances 0 to d-1, whose changes the candidate has and
   the base lacks. (The gap note's "the parent's manifest" for distance 1 would declare a change the base already
   contains.) Their entries join the PR manifest with `must_be_observed: false`: they are not attributed to this PR
   and are not its obligations. With no PR manifest the 1.7 T+0:01 no-manifest cases stand and nothing is merged.
3. Several main builds of one commit: the latest `built_at`, then the greater build id.
4. A base build that cannot be installed and an absent capture set are re-queued once like a missing build record
   (9.2 prints "re-queued once" unless a re-run cannot help); only the missing record asks the pipeline to build the
   merge base (3.1). Last gate reached: `provisioned`, which the run never passes.
5. Stored captures (`stored_bases`) come from finished `merge` and `pr` runs that ended `complete` or
   `complete_with_env_errors`: the candidate side of a run whose candidate is the base build (a merge run's capture of
   the new main) and the base side of a run whose base is the base build, when that side was captured there (no
   `base_source_run_id`). Only frozen, non-synthetic, non-rescued records whose capture artifact is indexed and not
   deleted or failed audit, with an outcome reached after a settled capture (`REUSABLE_OUTCOMES`; an
   `assertion_failed (assertion: app_alive)` capture is excluded). The fingerprint is the record's, else the shard's
   stored one for that side; the region registry version is the stored fingerprint's
   `harness.region_registry_version`; the redaction version is the artifact's.
6. The fixture file sha256 is `sha256:` plus the hex digest of the file's bytes; the golden `seeded_at` is the latest
   `seeded_at` of the positions of the `golden` accounts, as a UTC ISO time, or "" when the golden account holds no
   position. With one fixture file per rig (4.9), the key is per shard; the plan's reuse (`scope_base_reuse`) uses
   the run's key and is an estimate, and `reuse_decision` with the shard's live key decides at execution.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any

import yaml

from .evaluation import BaseEvidence
from .orchestrator import alerts
from .orchestrator.results import ControlResult, Outcome, RunVerdict
from .records import Fingerprint
from .scope import BaseReuse, Manifest, StoredBase
from .store.artifacts import ArtifactError, ArtifactStore
from .store.store import Store

MAX_BASE_DISTANCE = 20                         # SD-02, 3.1, 4.2 L-BLD-04
ABORT_REASON = "base_build_missing"            # 1.10, 4.13 run aborted_harness
ABORT_GATE = "provisioned"                     # choice 4: the gate the run never passes
REUSE_RUN_TYPES = ("merge", "pr")              # choice 5
FINISHED_OUTCOMES = ("complete", "complete_with_env_errors")
# choice 5: the checkpoint outcomes reached after a settled capture of the side (1.10, 3.11)
REUSABLE_OUTCOMES = ("identical", "noise", "intended", "collateral", "needs_human", "invariant_violation",
                     "assertion_failed", "needs_baseline")
SUMMARY_KEY = "reuse_fixture"                  # runs.summary_json: {shard_id: {fixture_sha256, golden_seeded_at}}
KEY_FIELDS = ("fingerprint_id", "epoch_id", "fixture_sha256", "golden_seeded_at")


# -- the PR base (SD-02, 3.1, 4.2) -------------------------------------------------------------------------------

@dataclass(frozen=True)
class MainCommit:
    """One first-parent commit of main and the manifests of the PRs merged at it (choice 1)."""
    sha: str
    manifests: tuple[str, ...] = ()


class BaseBuildMissing(Exception):
    """No base for the run: `aborted_harness (reason: base_build_missing)` (1.10, 3.1, 4.2). `cause` is
    `not_found`, `install_failed` or `capture_set_absent`; `merge_base_sha` is set for `not_found`."""

    def __init__(self, cause: str, detail: str, *, merge_base_sha: str | None = None,
                 searched: tuple[str, ...] = ()):
        super().__init__(detail)
        self.cause = cause
        self.detail = detail
        self.merge_base_sha = merge_base_sha
        self.searched = searched

    @classmethod
    def not_found(cls, merge_base_sha: str, searched: Sequence[str], max_distance: int) -> BaseBuildMissing:
        """No main build record at the merge base or on its nearest `max_distance` ancestors (3.1)."""
        return cls("not_found", f"no main build record at merge base {merge_base_sha[:7]} or on its "
                                f"{max_distance} nearest main ancestors ({len(searched)} commits searched)",
                   merge_base_sha=merge_base_sha, searched=tuple(searched))

    @classmethod
    def install_failed(cls, build_id: str, why: str) -> BaseBuildMissing:
        """The base build cannot be installed (4.2)."""
        return cls("install_failed", f"base build {build_id} could not be installed: {why}")

    @classmethod
    def capture_set_absent(cls, build_id: str) -> BaseBuildMissing:
        """The base build's capture set is absent (4.2)."""
        return cls("capture_set_absent", f"the capture set of base build {build_id} is absent")


@dataclass(frozen=True)
class BaseResolution:
    """The resolved PR base (3.1): the build, its commit, the distance from the merge base and the manifests of the
    PRs merged in between, oldest first (choice 2)."""
    base_build_id: str
    base_commit: str
    merge_base_sha: str
    distance: int
    declared_manifests: tuple[str, ...] = ()

    @property
    def placeholder(self) -> str:
        """The 1.7 T+0:00 PR comment placeholder, naming the base."""
        return f"Sentinel run started, base = main@{self.base_commit[:7]}"

    def health_row(self) -> tuple[str, str] | None:
        """The 9.2 run-health base row, for `summary["health_rows"]`; None when the base is the merge base."""
        if self.distance == 0:
            return None
        return ("Base", (f"{self.distance} commits before the merge base, the nearest main ancestor with a build "
                         "record (4.2); the manifests of the PRs merged in between are treated as declared"))


def _block(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    """The 4.2 `build` block of a build record, or the block itself."""
    inner = doc.get("build")
    return inner if isinstance(inner, Mapping) else doc


def _built_at(b: Mapping[str, Any]) -> str:
    v = b.get("built_at")
    return v.isoformat() if isinstance(v, (date, datetime)) else str(v or "")


def _main_builds_by_commit(builds: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Commit sha -> the main build id chosen for it (choice 3)."""
    best: dict[str, tuple[str, str]] = {}
    for build_id, doc in builds.items():
        b = _block(doc)
        if b.get("channel") != "main" or not b.get("commit_sha"):
            continue
        sha = str(b["commit_sha"])
        rank = (_built_at(b), str(build_id))
        if sha not in best or rank > best[sha]:
            best[sha] = rank
    return {sha: rank[1] for sha, rank in best.items()}


def resolve_base(candidate: Mapping[str, Any], main_history: Sequence[MainCommit | str],
                 builds: Mapping[str, Mapping[str, Any]], max_distance: int = MAX_BASE_DISTANCE) -> BaseResolution:
    """The PR base (SD-02, 3.1, 4.2): the main build at `candidate`'s merge base, else the nearest main ancestor of
    it with a build record, at most `max_distance` commits back. `candidate` is the branch build record (4.2, or its
    `build` block); `builds` maps build id to record. Raises `BaseBuildMissing` when none is found, and ValueError
    for a candidate without `merge_base_sha` (L-BLD-03 refuses it before a run exists)."""
    merge_base = _block(candidate).get("merge_base_sha")
    if not merge_base:
        raise ValueError("the candidate build carries no merge_base_sha (4.2 L-BLD-03)")
    merge_base = str(merge_base)
    history = [c if isinstance(c, MainCommit) else MainCommit(str(c)) for c in main_history]
    shas = [c.sha for c in history]
    at = _main_builds_by_commit(builds)
    walk = history[shas.index(merge_base):] if merge_base in shas else [MainCommit(merge_base)]
    for distance, commit in enumerate(walk[:max_distance + 1]):
        if commit.sha in at:
            between = [m for c in reversed(walk[:distance]) for m in c.manifests]
            return BaseResolution(at[commit.sha], commit.sha, merge_base, distance, tuple(dict.fromkeys(between)))
    raise BaseBuildMissing.not_found(merge_base, [c.sha for c in walk[:max_distance + 1]], max_distance)


def declared_manifest(pr_manifest: Manifest | None, intervening: Sequence[Manifest]) -> Manifest | None:
    """The PR manifest with the intervening merged manifests' entries added as declared (3.1, choice 2): each added
    entry carries `must_be_observed: false`. None stays None (the no-manifest cases of 1.7 T+0:01). An entry id
    that two manifests share is a ValueError: the classification names entries by id (3.11)."""
    if pr_manifest is None:
        return None
    added = tuple(replace(e, must_be_observed=False) for m in intervening for e in m.entries)
    entries = pr_manifest.entries + added
    ids = [e.entry_id for e in entries]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"manifest entry ids {dupes} appear in more than one declared manifest (3.1, 3.11)")
    return replace(pr_manifest, entries=entries, empty_reason=None if entries else pr_manifest.empty_reason)


def base_missing_result(cause: BaseBuildMissing, *, requeue_of: str | None = None,
                        requeued_as: str | None = None) -> ControlResult:
    """The run-level result of a missing base (1.7 T+0:00, 1.10, 3.1, 4.2): `aborted_harness (reason:
    base_build_missing)` at `provisioned` before any capture; every scoped checkpoint `harness_error
    (base_build_missing)` (`ControlResult.unreached`, for `Store.fill_unreached`); the P1 harness page (9.10).
    `evidence["abort"]` is the 9.2 abort block for `summary["abort"]`; `evidence["rebuild"]` the merge base the
    pipeline is asked to build (`not_found` only, choice 4); `evidence["requeue"]` whether to re-queue the run.
    A run that is itself the re-queue (`requeue_of`) is not re-queued again ("already re-queued"); otherwise
    `requeued_as`, the id of the re-queued run, is required."""
    if requeue_of is None and not requeued_as:
        raise ValueError("a first base_build_missing abort re-queues the run once: name the re-queued run (3.1)")
    abort: dict[str, Any] = {"trigger": cause.detail, "gate": ABORT_GATE}
    if requeue_of is None:
        abort["requeued"] = requeued_as
    return ControlResult("base_build", False, ABORT_GATE, run=RunVerdict("aborted_harness", ABORT_REASON),
                         unreached=Outcome("harness_error", ABORT_REASON),
                         alert=alerts.aborted_harness(), detail=cause.detail,
                         evidence={"abort": abort, "cause": cause.cause,
                                   "rebuild": cause.merge_base_sha if cause.cause == "not_found" else None,
                                   "requeue": requeue_of is None})


# -- frozen base-capture reuse (3.1, 7.4, 7.9, SD-68) ---------------------------------------------------------

@dataclass(frozen=True)
class ReuseKey:
    """The base-capture reuse key of 7.9 (SD-68): reuse requires all four equal."""
    fingerprint_id: str
    epoch_id: str
    fixture_sha256: str
    golden_seeded_at: str

    def differing(self, other: ReuseKey) -> list[str]:
        """The key fields whose values differ, in key order."""
        return [f for f in KEY_FIELDS if getattr(self, f) != getattr(other, f)]

    def fixture_part(self) -> dict[str, str]:
        """The half of the key no 8.3 column holds, as `reuse_fixture_patch` stores it."""
        return {"fixture_sha256": self.fixture_sha256, "golden_seeded_at": self.golden_seeded_at}


def _utc(v: Any) -> datetime:
    t = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    if t.tzinfo is None:
        raise ValueError(f"seeded_at {v!r} carries no UTC offset (4.9 ts)")
    return t.astimezone(timezone.utc)


def golden_seeded_at(fixture: Mapping[str, Any]) -> str:
    """The golden account's latest `seeded_at` (7.9, choice 6), as a UTC ISO time; "" without a golden position."""
    golden = {a.get("account_id") for a in fixture.get("accounts") or () if a.get("role") == "golden"}
    times = [_utc(p["seeded_at"]) for p in fixture.get("positions") or ()
             if p.get("account_ref") in golden and p.get("seeded_at") is not None]
    return max(times).isoformat() if times else ""


def reuse_key(fingerprint_id: str, fixture_bytes: bytes) -> ReuseKey:
    """The live reuse key (7.9): the fingerprint id, and from the fixture file's bytes its `epoch.epoch_id`, its
    sha256 and the golden account's latest `seeded_at` (choice 6). ValueError for a fixture without an epoch id."""
    doc = yaml.safe_load(fixture_bytes) or {}
    epoch = (doc.get("epoch") or {}).get("epoch_id") if isinstance(doc, Mapping) else None
    if not epoch:
        raise ValueError("the fixture file has no epoch.epoch_id (4.9 L-FIX-06)")
    return ReuseKey(fingerprint_id, str(epoch), "sha256:" + hashlib.sha256(fixture_bytes).hexdigest(),
                    golden_seeded_at(doc))


def live_versions(fingerprint: Fingerprint | Mapping[str, Any]) -> tuple[str, str]:
    """(redaction version, region registry version) of a live fingerprint's `harness` block (4.12), the values
    `reuse_decision` compares with a stored capture's artifact redaction version and stored fingerprint's region
    registry version (3.1, choice 5). ValueError when the block lacks either."""
    match = fingerprint.match if isinstance(fingerprint, Fingerprint) else fingerprint.get("match", fingerprint)
    harness = match.get("harness") if isinstance(match, Mapping) else None
    if not isinstance(harness, Mapping) or not harness.get("redaction_version") or not harness.get(
            "region_registry_version"):
        raise ValueError("the fingerprint's harness block names no redaction_version or region_registry_version "
                         "(4.12)")
    return str(harness["redaction_version"]), str(harness["region_registry_version"])


def reuse_fixture_patch(keys: Mapping[str, ReuseKey]) -> dict[str, Any]:
    """The run-summary patch (`Store.end_run(summary=)` or `update_summary`) that records, per shard id, the fixture
    half of the key its captures were taken under, which `stored_bases` reads back (choice 6)."""
    return {SUMMARY_KEY: {shard: k.fixture_part() for shard, k in sorted(keys.items())}}


@dataclass(frozen=True)
class StoredCapture:
    """A stored frozen capture of the base build and the identity it was taken under (3.1, choice 5). The fixture
    half of the key is None when its run recorded none; such a capture is never reused."""
    checkpoint_id: str
    source_run_id: str
    run_type: str
    side: str
    shard_id: str | None
    capture_sha256: str
    captured_at: str | None
    fingerprint_id: str | None
    epoch_id: str | None
    fixture_sha256: str | None
    golden_seeded_at: str | None
    redaction_version: str | None
    region_version: str | None

    @property
    def key(self) -> ReuseKey | None:
        """The stored reuse key, or None when a part of it was not recorded."""
        parts = [getattr(self, f) for f in KEY_FIELDS]
        return None if any(p is None for p in parts) else ReuseKey(*parts)


_STORED_SQL = """
SELECT o.run_id, o.checkpoint_id, o.shard_id, o.outcome, o.reason_detail, o.data_epoch_id, o.captured_at,
       o.{col} AS sha, r.run_type, r.summary_json, r.finished_at, a.redaction_version,
       json_extract(rec.record_json, '$.fingerprint_id') AS record_fp, f.fingerprint_sha256 AS shard_fp,
       f.fields_json
FROM outcomes o
JOIN runs r ON r.run_id = o.run_id
JOIN build_pairs p ON p.pair_id = r.pair_id
JOIN checkpoints c ON c.checkpoint_id = o.checkpoint_id
JOIN artifacts a ON a.sha256 = o.{col}
LEFT JOIN outcome_records rec ON rec.run_id = o.run_id AND rec.checkpoint_id = o.checkpoint_id
LEFT JOIN env_fingerprints f ON f.shard_id = o.shard_id AND f.side = '{side}'
WHERE p.{build_col} = ? {ids}
  AND COALESCE(json_extract(rec.record_json, '$.data_mode'), c.data_mode) = 'frozen'
  AND o.synthetic = 0 AND o.rescued = 0 AND r.lifecycle = 'finished'
  AND r.outcome IN ({finished}) AND r.run_type IN ({types})
  AND a.deleted_at IS NULL AND a.audit_state = 'ok' {extra}
"""


def _quoted(values: Iterable[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _faithful(row: Any) -> bool:
    """Choice 5: an outcome reached after a settled capture, an app failure excluded."""
    if row["outcome"] not in REUSABLE_OUTCOMES:
        return False
    return not (row["outcome"] == "assertion_failed" and (row["reason_detail"] or "").split("\n")[0] == "app_alive")


def _region_version(fields_json: str | None) -> str | None:
    if not fields_json:
        return None
    harness = (json.loads(fields_json).get("match") or {}).get("harness")
    v = harness.get("region_registry_version") if isinstance(harness, Mapping) else None
    return None if v is None else str(v)


def stored_bases(store: Store, checkpoint_ids: Iterable[str] | None, *, base_build_id: str
                 ) -> dict[str, tuple[StoredCapture, ...]]:
    """The stored frozen captures of `base_build_id` per checkpoint id, latest first (3.1, 7.4, 11.2, choice 5),
    read through `Store.query`. A checkpoint with none is absent from the result. `checkpoint_ids` None reads every
    checkpoint, so the result can feed `compute_scope` before the run's scope exists."""
    ids = None if checkpoint_ids is None else list(dict.fromkeys(checkpoint_ids))
    if ids == []:
        return {}
    where = "" if ids is None else "AND o.checkpoint_id IN (" + ", ".join("?" * len(ids)) + ")"
    found: dict[str, list[tuple[tuple[str, str, str], StoredCapture]]] = {}
    for side, col, build_col, extra in (("candidate", "cand_capture_sha256", "candidate_build_id", ""),
                                        ("base", "base_capture_sha256", "base_build_id",
                                         "AND o.base_source_run_id IS NULL")):
        sql = _STORED_SQL.format(col=col, side=side, build_col=build_col, ids=where,
                                 finished=_quoted(FINISHED_OUTCOMES), types=_quoted(REUSE_RUN_TYPES), extra=extra)
        for row in store.query(sql, [base_build_id, *(ids or ())]):
            if not _faithful(row):
                continue
            fixture = (json.loads(row["summary_json"] or "{}").get(SUMMARY_KEY) or {}).get(row["shard_id"]) or {}
            cap = StoredCapture(
                checkpoint_id=row["checkpoint_id"], source_run_id=row["run_id"], run_type=row["run_type"],
                side=side, shard_id=row["shard_id"], capture_sha256=row["sha"], captured_at=row["captured_at"],
                fingerprint_id=row["record_fp"] or row["shard_fp"], epoch_id=row["data_epoch_id"],
                fixture_sha256=fixture.get("fixture_sha256"), golden_seeded_at=fixture.get("golden_seeded_at"),
                redaction_version=row["redaction_version"], region_version=_region_version(row["fields_json"]))
            order = (row["captured_at"] or "", row["finished_at"] or "", row["run_id"])
            found.setdefault(cap.checkpoint_id, []).append((order, cap))
    return {cid: tuple(c for _, c in sorted(found[cid], key=lambda t: t[0], reverse=True))
            for cid in (sorted(found) if ids is None else ids) if cid in found}


@dataclass(frozen=True)
class ReuseDecision:
    """What the base side of one frozen checkpoint does (3.1): `reuse` the stored capture `reused`, `recapture`
    (with `why`), or `missing`, the 3.11 B `reuse_missing` of a reuse the plan expected and no stored capture."""
    checkpoint_id: str
    action: str                                   # reuse | recapture | missing
    reused: StoredCapture | None = None
    why: str | None = None

    @property
    def source_run_id(self) -> str | None:
        """The outcome record's `base_source_run_id` (3.1: the outcome names the source run)."""
        return self.reused.source_run_id if self.reused else None


def _mismatch(live: ReuseKey, capture: StoredCapture, redaction_version: str, region_version: str) -> list[str]:
    key = capture.key
    if key is None:
        return [f for f in KEY_FIELDS if getattr(capture, f) is None]
    out = key.differing(live)
    if capture.redaction_version != redaction_version:
        out.append("redaction_version")
    if capture.region_version != region_version:
        out.append("region_registry_version")
    return out


def reuse_decision(checkpoint_id: str, live: ReuseKey, stored: Sequence[StoredCapture] | StoredCapture | None, *,
                   redaction_version: str, region_version: str, data_mode: str = "frozen",
                   expected: bool = False) -> ReuseDecision:
    """The base side of one checkpoint (3.1, 7.4, 7.9): reuse the latest stored capture whose reuse key, region
    registry version and redaction version equal the live ones; a `masked_live` checkpoint never reuses; with no
    stored capture, `missing` when the plan expected reuse (`expected`, the planned `base_source_run_id`), else
    `recapture`. A stored capture whose key differs is re-captured, never scored."""
    if data_mode != "frozen":
        return ReuseDecision(checkpoint_id, "recapture", why="masked_live base captures are never reused (3.1)")
    caps = (stored,) if isinstance(stored, StoredCapture) else tuple(stored or ())
    if any(c.checkpoint_id != checkpoint_id for c in caps):
        raise ValueError(f"stored captures of another checkpoint passed for {checkpoint_id}")
    if not caps:
        if expected:
            return ReuseDecision(checkpoint_id, "missing", why="frozen reuse expected, no stored capture found")
        return ReuseDecision(checkpoint_id, "recapture", why="no stored capture of the base build")
    for c in caps:
        if not _mismatch(live, c, redaction_version, region_version):
            return ReuseDecision(checkpoint_id, "reuse", reused=c)
    latest = caps[0]
    return ReuseDecision(checkpoint_id, "recapture",
                         why=f"stored capture of run {latest.source_run_id} differs in "
                             + ", ".join(_mismatch(live, latest, redaction_version, region_version)))


@dataclass(frozen=True)
class ReusedBase:
    """The base side of a reused checkpoint at execution: the stored bytes, or why they could not be read."""
    capture: StoredCapture
    data: bytes | None
    missing: str | None = None

    def evidence(self) -> BaseEvidence:
        """Input B of 3.11 (`evaluation.b_input_reason`): captured, or absent `reuse_missing`."""
        return BaseEvidence(captured=True) if self.data is not None else BaseEvidence(reuse_missing=True)


def reused_base(arts: ArtifactStore, capture: StoredCapture) -> ReusedBase:
    """Read the reused capture's bytes, verified against its hash (8.4); a missing, corrupt or quarantined object is
    the 3.11 B `reuse_missing` (frozen reuse expected, capture not found)."""
    try:
        return ReusedBase(capture, arts.get(capture.capture_sha256))
    except ArtifactError as e:
        return ReusedBase(capture, None, missing=f"{type(e).__name__}: {e}")


def scope_base_reuse(live: ReuseKey, stored: Mapping[str, Sequence[StoredCapture]], *, redaction_version: str,
                     region_version: str) -> BaseReuse:
    """`reuse_decision` per checkpoint as the `scope.BaseReuse` that `compute_scope(base_reuse=)` takes: the chosen
    capture's identity, so scope's per-rig fingerprint check (L-09) still applies (choice 6)."""
    chosen: dict[str, StoredBase] = {}
    for cid, caps in stored.items():
        d = reuse_decision(cid, live, caps, redaction_version=redaction_version, region_version=region_version)
        if d.reused is not None:
            c = d.reused
            chosen[cid] = StoredBase(c.source_run_id, str(c.fingerprint_id), str(c.epoch_id), region_version,
                                     redaction_version)
    return BaseReuse(live.fingerprint_id, live.epoch_id, chosen, redaction_version=redaction_version,
                     region_versions={cid: region_version for cid in chosen})


@dataclass
class ReuseLedger:
    """The run's base-side decisions for frozen checkpoints (7.4: reuse is recorded per checkpoint in run health as
    `base_reused`)."""
    decisions: dict[str, ReuseDecision] = field(default_factory=dict)
    unreadable: dict[str, str] = field(default_factory=dict)

    def add(self, decision: ReuseDecision, loaded: ReusedBase | None = None) -> None:
        """Record one checkpoint's decision and, for a reuse, what reading the bytes gave."""
        self.decisions[decision.checkpoint_id] = decision
        if loaded is not None and loaded.missing is not None:
            self.unreadable[decision.checkpoint_id] = loaded.missing

    @property
    def base_reused(self) -> dict[str, str]:
        """Checkpoint id -> source run, for every base actually reused (`base_source_run_id`)."""
        return {cid: d.source_run_id for cid, d in sorted(self.decisions.items())
                if d.action == "reuse" and d.source_run_id and cid not in self.unreadable}

    @property
    def reuse_missing(self) -> list[str]:
        """The checkpoints whose base is absent `reuse_missing` (3.11 B)."""
        return sorted(cid for cid, d in self.decisions.items() if d.action == "missing" or cid in self.unreadable)

    @property
    def reused_from(self) -> str | None:
        """`ReportContext.base_reused_from`: the run(s) whose captures were reused, or None."""
        runs = sorted(set(self.base_reused.values()))
        return ", ".join(runs) if runs else None

    def health_row(self) -> tuple[str, str] | None:
        """The run-health row for `summary["health_rows"]`; None when no frozen base was decided."""
        if not self.decisions:
            return None
        recaptured = sum(1 for d in self.decisions.values() if d.action == "recapture")
        reused = self.base_reused
        text = f"base_reused {len(reused)}"
        if reused:
            text += f" (from {self.reused_from})"
        return ("Base reuse", f"{text}; re-captured {recaptured}; reuse_missing {len(self.reuse_missing)}")
