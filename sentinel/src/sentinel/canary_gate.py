"""The CI canary gate: the full mutant catalog as a merge gate (10.3 Gate and governance, 10.4, 12.5, 10.10 Part C).

"The full catalog also runs in CI on any change to masks, thresholds, the Region Registry, the Reviewer prompt or
few-shot set, the pinned model id, the redaction stage or the downscale path, and on every Calibration run; the
change merges only with a passing result attached" (10.3).

- `canary_gate_needed(changed_paths, ...)` maps a change to the trigger inputs it touches (`GateNeed`, false when
  none): changed paths of `sentinel-config` or of the `sentinel` code, the reviewer version tuples before and after
  (model id, prompt, few-shot set, crop policy as the downscale path, redaction version) and the redaction stage
  versions before and after, and `calibration_run` for the gate run after every Calibration run.
- `run_gate(config_dir, out, ...)` runs the catalog through the Nightly canary path against the branch config
  (`pipeline.run.run_nightly`: `canary.generate`, the same `evaluate` path as a product checkpoint, `canary.score`)
  and returns a `GateResult`: passed only when the canary state is `ok`; otherwise its message is the 10.3 morning
  banner followed by one line per mutant not detected, naming what absorbed it (`absorbed_by`: a mask, a decision, a
  threshold or a landmark), and the branch config's lint errors when the pass could not run at all.
- `main(argv)` is `sentinel canary-gate`: writes `canary-gate.json` (the result to attach to the pull request; the
  `--canary-result` input of `sentinel eval-gate`) and exits 1 when the gate fails.

Choices where the spec is ambiguous (reported):

1. Trigger paths. Masks: `masks/`. The Region Registry: `regions/` (regions files, their `mask` regions and landmark
   templates, 4.8); the Surface Registry (`registry/`) is not a 10.3 trigger. Thresholds: `calibration/` and
   `diff_engine.yaml` (3.7, 3.9). The redaction stage: `redaction/` and `capture/redaction.py`. The Reviewer prompt:
   `reviewer/prompt.py` and `reviewer/schema.py` (the schema is part of the prompt, 3.12). The downscale path:
   `reviewer/images.py` and `reviewer/config.py` (the crop policy, 3.12). The pinned model id: `phase0/branches.yaml`
   (4.16 `reviewer.model_id`), but only by path when the tuples are not given; with both tuples the model id, prompt,
   few-shot ids and crop policy are compared instead, so an unrelated edit of the branches file needs no gate. Paths
   match anywhere in a repository-relative path, so `sentinel/config/example/masks/...` counts like `masks/...`.
2. The gate's build is the newest `main` channel build of the config's `builds/` (by `built_at`) unless `--build`
   names one; its approved captures are the base the mutants are made from (10.3 Determinism). The run is a Nightly
   canary pass without the A/A pass (`aa_pass: False`), in a fresh store under `<out>/run`.
3. A passing result is canary state `ok` (10.3 Result states): `detector_blind`, `detector_degraded` and
   `generation_failed` fail the gate, and so does a branch config whose canary pass did not run (its lint errors,
   12.7 G1, are listed). The rolling floor has no history in CI, so a class's floor is the 90 percent of 10.3.
   `--mutants` narrows the catalog for a local check; CI runs the whole catalog.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .change_control import ReviewerTuple, stage_redaction_version, tuple_in_force
from .lint.repo import load_yaml_text

TRIGGERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("masks", re.compile(r"(^|/)masks/")),
    ("region_registry", re.compile(r"(^|/)regions/")),
    ("thresholds", re.compile(r"(^|/)calibration/")),
    ("thresholds", re.compile(r"(^|/)diff_engine\.yaml$")),
    ("redaction", re.compile(r"(^|/)redaction/")),
    ("redaction", re.compile(r"(^|/)capture/redaction\.py$")),
    ("reviewer_prompt", re.compile(r"(^|/)reviewer/(prompt|schema)\.py$")),
    ("downscale", re.compile(r"(^|/)reviewer/(images|config)\.py$")),
    ("model_id", re.compile(r"(^|/)phase0/branches\.yaml$")),
)
TUPLE_TRIGGERS = {"model_id": "model_id", "prompt_sha256": "reviewer_prompt", "schema_version": "reviewer_prompt",
                  "few_shot_ids": "few_shot_set", "effort": "reviewer_prompt", "request_settings": "reviewer_prompt",
                  "crop_policy_version": "downscale", "redaction_version": "redaction"}
RESULT_FILE = "canary-gate.json"
FAIL_TAIL = "This change merges only with a passing canary gate result attached (10.3 Gate and governance)."


@dataclass(frozen=True)
class GateNeed:
    """The trigger inputs a change touches, each with the paths or tuple elements that fired it; false when none."""

    triggers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.triggers)

    @property
    def needed(self) -> bool:
        return bool(self.triggers)

    def text(self) -> str:
        return "; ".join(f"{k}: {', '.join(v)}" for k, v in self.triggers.items()) or "no trigger input changed"


def canary_gate_needed(changed_paths: Iterable[str] = (), *,
                       reviewer: tuple[ReviewerTuple | None, ReviewerTuple | None] | None = None,
                       redaction: tuple[str, str] | None = None, calibration_run: bool = False) -> GateNeed:
    """Whether the catalog gate must run for a change (module docstring, choice 1). `reviewer` is the tuple in force
    before and after (None for no tuple in force), `redaction` the redaction stage version before and after."""
    found: dict[str, list[str]] = {}
    for raw in changed_paths:
        path = raw.replace("\\", "/").removeprefix("./")
        for name, rx in TRIGGERS:
            if rx.search(path) and not (name == "model_id" and reviewer is not None):
                found.setdefault(name, []).append(path)
    if reviewer is not None:
        old, new = reviewer
        if (old is None) != (new is None):
            found.setdefault("model_id", []).append("reviewer tuple in force " + ("added" if new else "removed"))
        elif old is not None and new is not None:
            for element in old.differences(new):
                found.setdefault(TUPLE_TRIGGERS[element], []).append(element)
    if redaction is not None and redaction[0] != redaction[1]:
        found.setdefault("redaction", []).append(f"redaction version {redaction[0]} -> {redaction[1]}")
    if calibration_run:
        found.setdefault("calibration_run", []).append("every Calibration run")
    return GateNeed({k: tuple(dict.fromkeys(v)) for k, v in found.items()})


class GateError(ValueError):
    """The gate cannot run: no config directory or no build to make the mutants from."""


def main_build(config_dir: str | Path) -> str:
    """The newest `main` channel build of the config's `builds/` (choice 2)."""
    best: tuple[str, str] | None = None
    for p in sorted(Path(config_dir).glob("builds/*.yaml")):
        b = (load_yaml_text(p.read_text(encoding="utf-8")) or {}).get("build") or {}
        if b.get("channel") == "main" and b.get("build_id"):
            key = (str(b.get("built_at", "")), str(b["build_id"]))
            best = key if best is None or key > best else best
    if best is None:
        raise GateError(f"{config_dir}: no main channel build in builds/ to make the mutants from")
    return best[1]


@dataclass(frozen=True)
class GateResult:
    """One canary gate run (choice 3)."""

    passed: bool
    state: str | None                        # the canary state; None when no canary ran
    run_id: str
    message: str
    rows: tuple[Mapping[str, Any], ...] = ()
    catalog_version: str | None = None
    need: GateNeed = GateNeed()

    def doc(self) -> dict[str, Any]:
        """The result file to attach to the pull request."""
        return {"needed": True, "passed": self.passed, "state": self.state, "run_id": self.run_id,
                "catalog_version": self.catalog_version, "triggers": {k: list(v) for k, v in self.need.triggers.items()},
                "message": self.message, "results": [dict(r) for r in self.rows]}


def _row(r: Any) -> dict[str, Any]:
    return {"mutant_id": r.mutant_id, "class": r.defect_class, "checkpoint_id": r.checkpoint_id, "state": r.state,
            "money_cell": r.money_cell, "produced_outcome": r.produced_outcome, "produced_reason": r.produced_reason,
            "absorbed_by": r.absorbed_by}


def _miss_line(r: Mapping[str, Any]) -> str:
    produced = r["produced_outcome"] or "nothing"
    if r["produced_reason"]:
        produced += f" ({r['produced_reason']})"
    return (f"{r['mutant_id']} {r['class']} on {r['checkpoint_id']}: {r['state']}, produced {produced}; absorbed by "
            f"{r['absorbed_by'] or 'nothing named'}")


def run_gate(config_dir: str | Path, out: str | Path, *, today: date, mutants: Sequence[str] = (),
             build_id: str | None = None, need: GateNeed = GateNeed()) -> GateResult:
    """Run the catalog through the Nightly canary path against `config_dir` (module docstring, choices 2 and 3)."""
    from .pipeline.run import NightlyScenario, run_nightly
    from .pipeline.sim import SimBuild

    if not Path(config_dir).is_dir():
        raise GateError(f"{config_dir} is not a directory")
    build = build_id or main_build(config_dir)
    run_id = f"run-{today.isoformat()}-canary-gate-01"
    work = Path(out) / "run"
    if work.exists():
        shutil.rmtree(work)
    r = run_nightly(config_dir, NightlyScenario("canary-gate", run_id, SimBuild(build), mutants=tuple(mutants),
                                                aa_pass=False), work, today)
    report = (r.canary or {}).get("report")
    if report is None or report.no_canary_ran:
        why = report.generation_reason if report is not None else f"run {r.status} ({r.reason or 'no reason'})"
        lines = [f"CANARY GATE FAILED: the mutant catalog did not run on the branch config: {why}."]
        lines += [f"  {e}" for e in r.lint_errors] + [FAIL_TAIL]
        return GateResult(False, None if report is None else report.state, run_id, "\n".join(lines), need=need)
    rows = tuple(_row(x) for x in report.results)
    version = report.results[0].catalog_version if report.results else None
    if report.state == "ok":
        return GateResult(True, "ok", run_id, f"canary gate passed: {len(rows)} mutant results, canary state ok",
                          rows, version, need)
    missed = [_miss_line(x) for x in rows if x["state"] != "detected"]
    message = "\n".join([report.banner or f"CANARY STATE {report.state}", *missed, FAIL_TAIL])
    return GateResult(False, report.state, run_id, message, rows, version, need)


def main(argv: list[str] | None = None) -> int:
    """`sentinel canary-gate CONFIG_DIR [--changed PATH ...] [--base-config DIR] [--calibration-run] [--out DIR]
    [--mutants M01,M02] [--build ID] [--today YYYY-MM-DD]`: the 10.3 CI gate. With `--changed` (and optionally the
    base checkout for the tuple comparison) it runs only when a trigger input changed; without, it always runs.
    Writes `<out>/canary-gate.json`. Exit 0 when the gate passed or was not needed, 1 when it failed, 2 on bad input."""
    from .branches import BranchesError
    from .capture import RedactionError

    p = argparse.ArgumentParser(prog="sentinel canary-gate", description=main.__doc__)
    p.add_argument("config_dir")
    p.add_argument("--changed", nargs="+", help="the change's paths (sentinel-config or sentinel)")
    p.add_argument("--base-config", help="the base checkout, to compare the reviewer tuple and redaction version")
    p.add_argument("--calibration-run", action="store_true", help="the gate run after a Calibration run")
    p.add_argument("--out", default="out/canary-gate")
    p.add_argument("--mutants", default="", help="comma-separated catalog ids (a local check; CI runs them all)")
    p.add_argument("--build")
    p.add_argument("--today", type=date.fromisoformat, default=date.today())
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        need = GateNeed({"always": ("no change list given",)})
        if a.changed is not None or a.calibration_run:
            reviewer = redaction = None
            if a.base_config:
                reviewer = (tuple_in_force(a.base_config), tuple_in_force(a.config_dir))
                redaction = (stage_redaction_version(a.base_config), stage_redaction_version(a.config_dir))
            need = canary_gate_needed(a.changed or (), reviewer=reviewer, redaction=redaction,
                                      calibration_run=a.calibration_run)
        if not need:
            (out / RESULT_FILE).write_text(json.dumps({"needed": False, "passed": True, "triggers": {},
                                                       "message": "canary gate not needed: " + need.text()},
                                                      indent=1) + "\n", encoding="utf-8")
            print(f"canary gate not needed: {need.text()}")
            return 0
        result = run_gate(a.config_dir, out, today=a.today, mutants=[m for m in a.mutants.split(",") if m],
                          build_id=a.build, need=need)
    except (GateError, BranchesError, RedactionError, OSError) as exc:
        print(f"sentinel canary-gate: {exc}", file=sys.stderr)
        return 2
    (out / RESULT_FILE).write_text(json.dumps(result.doc(), indent=1) + "\n", encoding="utf-8")
    print(f"canary gate run for {need.text()}")
    print(result.message)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
