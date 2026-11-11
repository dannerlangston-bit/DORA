"""Change control: the reviewer version tuple, the hashes a run is pinned to and the canary re-run rule
(3.7, 3.15, 4.16 eval report, 8.3 `runs`).

Two inputs change what the detector path sees and therefore need a fresh canary pass before a product run uses
them: the Diff Engine parameters (3.7: "All parameters live in `diff_engine.yaml` in git; any change re-runs the
canary catalog before the next product run") and the reviewer version tuple (3.15: "the Canary Service re-runs the
mutant catalog through the full pipeline before the first product run on it").

- `ReviewerTuple` is the tuple of 3.15 in the `reviewer_version` shape of the 4.16 eval report (model id, prompt
  sha256, schema version, few-shot ids, effort and request settings, crop policy version, redaction version);
  `sha256` is over its canonical JSON and names the eval report file `eval/reports/<hex>.yaml`.
- `tuple_in_force(root)` is the tuple a config checkout pins: the model of `phase0/branches.yaml` (the 3.15 pin,
  4.16 `reviewer.model_id`) with the prompt the code builds from the reviewer settings; None while the Reviewer is
  not enabled (DR-07 undecided or no pinned model, `branches.ModelBranches.reviewer_enabled`), since no tuple is in
  force then, and None for the offline stand-in model of the simulated run, which is not a model and has no eval.
- `hashes(cfg)` gives the `ChangeHashes` a run records on its `runs` row (`diff_params_sha256`,
  `reviewer_version_sha256`; `ChangeHashes.plan_fields` for `store.RunPlan`). The Nightly run records them too, so
  its canary pass says which configuration it proved.
- `refuse_if_stale(store, hashes, run_type)` raises `CanaryStale` before a product run (PR, merge, RC) when the last
  Nightly canary pass in the store ran on other hashes, or none ran yet; `staleness` returns the same finding
  without raising, for a caller that only marks the run.

Choices where the spec is ambiguous (reported):

1. The diff hash covers the whole `diff_engine.yaml`, the `settle` section included (3.7: "All parameters ...
   any change"); `diff.params_sha256` leaves `settle` out because the fingerprint hashes it on its own (4.12).
2. The tuple's redaction version is `capture.redaction_version` over the config's `redaction/anchors.yaml` and the
   default sweep patterns: the version every stored image and verdict carries (`run.red_header`), since the sweep's
   digest covers its patterns, never the login's literal ids or display name (which change with a credential
   rotation, 7.7, not by a pull request).
3. The reviewer settings in force are `reviewer.config` defaults unless the caller passes the `reviewer:` block the
   run uses (`pipeline.live.load_reviewer_config`); the few-shot set is the caller's (none ships yet, 3.12 item 6).
   `schema_version` is the integer of `reviewer.schema.SCHEMA_VERSION` (`reviewer-1` -> 1), as 4.16 types it.
4. "A canary pass" is the latest Nightly run whose summary carries a canary section where the catalog ran
   (`canary.persist`, `no_canary_ran` false), whatever its detector state: a blind or degraded result already
   blocks RC sign-off and banners every report (10.3: "PR and merge runs continue under the banner"); what this rule
   adds is that the catalog ran on the configuration in force. Only the latest canary run counts, so a revert is a
   change too. An element the current run does not pin (no reviewer tuple in force) is not compared.
5. A stale product run is refused as `aborted_harness (reason: config_invalid)`: the configuration in force is not
   yet proven by the canary catalog, which is a property of the configuration (12.7 G1 is the nearest gate), and a
   re-queue cannot help until a Nightly canary pass runs on it.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import branches as branches_mod
from .capture import RedactionList, Sweep, redaction_version
from .diff import load_params
from .pipeline.reviewer import SIMULATED_MODEL_ID
from .reviewer.config import ReviewerConfig
from .reviewer.prompt import FewShot, Prompt
from .reviewer.schema import SCHEMA_VERSION, canonical_json
from .store import Store

PREFIX = "sha256:"
PRODUCT_RUN_TYPES = ("pr", "merge", "rc")           # the runs a stale canary pass refuses (3.7, 3.15)
CANARY_RUN_TYPE = "nightly"                         # the Canary Service's pass runs on the Nightly run (10.3)
REFUSAL = ("aborted_harness", "config_invalid")     # choice 5
ANCHORS = "redaction/anchors.yaml"
TUPLE_FIELDS = ("model_id", "prompt_sha256", "schema_version", "few_shot_ids", "effort", "request_settings",
                "crop_policy_version", "redaction_version")


def sha256_of(obj: Any) -> str:
    """`sha256:` plus the hex digest of the canonical JSON of `obj`."""
    return PREFIX + hashlib.sha256(canonical_json(obj).encode()).hexdigest()


@dataclass(frozen=True)
class ReviewerTuple:
    """The reviewer version of 3.15, in the `reviewer_version` shape of the 4.16 eval report."""

    model_id: str
    prompt_sha256: str
    schema_version: int
    few_shot_ids: tuple[str, ...]
    effort: str
    request_settings: Mapping[str, Any]
    crop_policy_version: str
    redaction_version: str

    def doc(self) -> dict[str, Any]:
        """The `reviewer_version` mapping of the eval report (4.16)."""
        return {"model_id": self.model_id, "prompt_sha256": self.prompt_sha256, "schema_version": self.schema_version,
                "few_shot_ids": list(self.few_shot_ids), "effort": self.effort,
                "request_settings": dict(self.request_settings), "crop_policy_version": self.crop_policy_version,
                "redaction_version": self.redaction_version}

    @property
    def sha256(self) -> str:
        """`reviewer_version_sha256` of 4.16: over the canonical JSON of `doc()`."""
        return sha256_of(self.doc())

    @property
    def hex(self) -> str:
        """The hash without its prefix: the `<hex>` of `eval/reports/<hex>.yaml`."""
        return self.sha256[len(PREFIX):]

    @classmethod
    def from_doc(cls, d: Mapping[str, Any]) -> "ReviewerTuple":
        """Read a `reviewer_version` mapping back; a missing element raises KeyError."""
        return cls(str(d["model_id"]), str(d["prompt_sha256"]), int(d["schema_version"]),
                   tuple(str(x) for x in d["few_shot_ids"]), str(d["effort"]), dict(d["request_settings"]),
                   str(d["crop_policy_version"]), str(d["redaction_version"]))

    def differences(self, other: "ReviewerTuple") -> tuple[str, ...]:
        """The elements of the tuple that differ from `other`, in 3.15 order."""
        a, b = self.doc(), other.doc()
        return tuple(k for k in TUPLE_FIELDS if canonical_json(a[k]) != canonical_json(b[k]))


def schema_version_number(version: str = SCHEMA_VERSION) -> int:
    """The integer schema version of 4.16 from the 3.13 `schema_version` string (`reviewer-1` -> 1)."""
    return int(version.rsplit("-", 1)[1])


def reviewer_tuple(config: ReviewerConfig, *, redaction_version: str, few_shots: Sequence[FewShot] = (),
                   model_id: str | None = None) -> ReviewerTuple:
    """The tuple of a reviewer configuration: the prompt hash the code builds for `config` and `few_shots` (3.12),
    and `model_id` (the pin) or else the config's model."""
    prompt = Prompt(config, few_shots)
    return ReviewerTuple(model_id or config.model_id, PREFIX + prompt.prompt_hash, schema_version_number(),
                         tuple(s.shot_id for s in few_shots), config.effort, config.request_settings(),
                         config.crop.version, redaction_version)


def stage_redaction_version(root: str | Path) -> str:
    """The redaction version over the checkout's redaction list and the default sweep patterns (choice 2)."""
    path = Path(root) / ANCHORS
    anchors = RedactionList.load(path, Path(root)) if path.is_file() else None
    return redaction_version(anchors, Sweep())


def tuple_in_force(root: str | Path, *, config: ReviewerConfig | None = None,
                   few_shots: Sequence[FewShot] = ()) -> ReviewerTuple | None:
    """The tuple a config checkout pins (module docstring); raises `branches.BranchesError` when
    `phase0/branches.yaml` is missing or invalid."""
    models = branches_mod.load(Path(root) / branches_mod.BRANCHES_PATH).models()
    if not models.reviewer_enabled or models.model_id == SIMULATED_MODEL_ID:
        return None
    return reviewer_tuple(config or ReviewerConfig(), redaction_version=stage_redaction_version(root),
                          few_shots=few_shots, model_id=models.model_id)


def diff_params_sha256(params: Mapping[str, Any] | None = None) -> str:
    """sha256 over the whole `diff_engine.yaml` (choice 1)."""
    return sha256_of(dict(params if params is not None else load_params()))


@dataclass(frozen=True)
class ChangeHashes:
    """What one run is pinned to (8.3 `runs.diff_params_sha256`, `runs.reviewer_version_sha256`)."""

    diff_params_sha256: str
    reviewer_version_sha256: str | None = None
    reviewer: ReviewerTuple | None = None

    def plan_fields(self) -> dict[str, str | None]:
        """The `store.RunPlan` fields: `dataclasses.replace(plan, **hashes.plan_fields())`."""
        return {"diff_params_sha256": self.diff_params_sha256,
                "reviewer_version_sha256": self.reviewer_version_sha256}


def hashes(cfg: Any, *, params: Mapping[str, Any] | None = None, reviewer_config: ReviewerConfig | None = None,
           few_shots: Sequence[FewShot] = ()) -> ChangeHashes:
    """The hashes of a configuration: `cfg` is a config directory or anything with a `root` (`SentinelConfig`);
    `params` the Diff Engine parameters (default `diff.load_params()`)."""
    root = Path(cfg) if isinstance(cfg, (str, os.PathLike)) else Path(cfg.root)
    t = tuple_in_force(root, config=reviewer_config, few_shots=few_shots)
    return ChangeHashes(diff_params_sha256(params), t.sha256 if t else None, t)


@dataclass(frozen=True)
class CanaryPass:
    """The latest Nightly run whose canary catalog ran (choice 4)."""

    run_id: str
    started_at: str
    diff_params_sha256: str | None
    reviewer_version_sha256: str | None
    canary_state: str


def last_canary_run(store: Store) -> CanaryPass | None:
    """The latest Nightly run with a canary section whose catalog ran; None before any."""
    rows = store.query("SELECT run_id, started_at, diff_params_sha256, reviewer_version_sha256, summary_json "
                       "FROM runs WHERE run_type = ? ORDER BY started_at DESC, run_id DESC", (CANARY_RUN_TYPE,))
    for row in rows:
        section = json.loads(row["summary_json"] or "{}").get("canary")
        if section and not section.get("no_canary_ran"):
            return CanaryPass(row["run_id"], row["started_at"], row["diff_params_sha256"],
                              row["reviewer_version_sha256"], str(section.get("canary_state", "ok")))
    return None


def _short(h: str | None) -> str:
    return "none" if h is None else h[:len(PREFIX) + 12]


@dataclass(frozen=True)
class Staleness:
    """Why the configuration in force is not canary-proven: the changed elements and the last canary pass."""

    changed: tuple[str, ...]                 # diff_params | reviewer_version | no_canary_run
    last: CanaryPass | None
    current: ChangeHashes

    @property
    def message(self) -> str:
        """One line for the abort block and run health."""
        if self.last is None:
            return ("the mutant catalog has never run on this store; run the Nightly canary pass before the first "
                    "product run (3.7, 3.15)")
        parts = []
        if "diff_params" in self.changed:
            parts.append(f"diff_engine.yaml parameters {_short(self.current.diff_params_sha256)} (canary ran on "
                         f"{_short(self.last.diff_params_sha256)})")
        if "reviewer_version" in self.changed:
            parts.append(f"reviewer version {_short(self.current.reviewer_version_sha256)} (canary ran on "
                         f"{_short(self.last.reviewer_version_sha256)})")
        return (f"the mutant catalog has not run on this configuration since the last canary pass "
                f"{self.last.run_id}: {'; '.join(parts)}; run the Nightly canary pass before the next product run "
                f"(3.7, 3.15)")


def staleness(store: Store, current: ChangeHashes) -> Staleness | None:
    """None when the last canary pass ran on `current`, else what changed (choice 4)."""
    last = last_canary_run(store)
    if last is None:
        return Staleness(("no_canary_run",), None, current)
    changed = []
    if last.diff_params_sha256 != current.diff_params_sha256:
        changed.append("diff_params")
    if current.reviewer_version_sha256 is not None and last.reviewer_version_sha256 != current.reviewer_version_sha256:
        changed.append("reviewer_version")
    return Staleness(tuple(changed), last, current) if changed else None


class CanaryStale(RuntimeError):
    """A product run on a configuration the canary catalog has not run on (3.7, 3.15); `verdict` is the run's
    outcome and reason (choice 5)."""

    def __init__(self, found: Staleness):
        super().__init__(found.message)
        self.staleness = found
        self.verdict = REFUSAL


def refuse_if_stale(store: Store, current: ChangeHashes, run_type: str = "pr") -> None:
    """Raise `CanaryStale` before a product run whose configuration the last canary pass did not run on; a Nightly
    or Calibration run is never refused (the Nightly run is the canary pass)."""
    if run_type not in PRODUCT_RUN_TYPES:
        return
    found = staleness(store, current)
    if found is not None:
        raise CanaryStale(found)
