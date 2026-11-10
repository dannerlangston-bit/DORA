"""One simulated Sentinel run, end to end: `run_pipeline` (a PR run, 1.7) and `run_nightly` (10.3).

`run_pipeline(config_dir, scenario, out_dir, today=...)` follows the PR run of 1.7 and the
checkpoint sequence of 5.17 with the rig replaced by `sim`:

1. T+0:00. The config the run pins (the given directory plus the PR's manifest and build records,
   4.3) is linted; any error ends the run `aborted_harness (reason: config_invalid)` with the abort
   block in the PR comment. The build pair is read from the candidate's build record.
2. T+0:01 to 0:02. The manifest for the candidate is loaded (or not: the two no-manifest cases),
   the changed paths go through the static impact step and `scope.compute_scope` plans the run.
3. Before any threshold is used (3.9, 1.6 Calibration run) an A/A calibration of the base build is
   run with `diff.calibration` over every planned checkpoint: five captures in one session and one
   in a fresh session; the same A/A pairs drive the invariant enablement test of 2.7.
4. Per planned checkpoint (5.17): both builds are captured, `diff.settle` gates each capture, the
   Diff Engine compares them against the calibrated floors, the Invariant Checker runs on both
   builds, targeted re-runs decide stability (3.10, re-run budget max(30, 10 percent)), decisions
   are looked up by signature in the store (9.7), and `classify.classify` gives the outcome.
5. Reviewer pass (3.12, 3.15). The checkpoints are clustered exactly as the report will show them
   (`report.cluster.cluster_members`, 9.7); one simulated Reviewer call per cluster that needs one
   (task A for declared surfaces, task B descriptions for `collateral`; never for invariants or
   assertions, never for a member that inherited a decision), up to the PR cap of 60 calls.
6. Everything goes through `store.Store` (single writer) and `store.ArtifactStore` (content
   addressed, redaction header set: synthetic screens carry no account data). The coverage matrix
   (`coverage.build_matrix`) and the Reporter's PR comment, commit status and static HTML report
   are rendered from what the store returns.

`run_nightly` is the canary pass of a Nightly run (10.3): `canary.generate` mutates approved
captures of the base build, every mutant goes through the same `evaluate` path as a PR checkpoint
with `synthetic: true`, behavioral canaries run their canary flow on the simulated app, and
`canary.score` gives the result rows, the detector state, the synthetic `harness_error` rows and
the morning banner.

Choices and workarounds (reported):

- WORKAROUND(scope): `scope.compute_scope` selects flows from the change-derived scope only (its
  choice 1, which follows 1.6 and 1.11); 2.11's pseudocode selects tier-1 flows that touch the
  census surfaces, smoke included. In the no-manifest, non-UI case (b) the change-derived scope is
  empty, so no flow would run and the Order Confirmation money cells, where F41 says a backend side
  effect shows, would never be captured. In case (b) only, this run adds the tier-1 flows of class
  `standard` whose surfaces meet the smoke census, as the pseudocode does.
- WORKAROUND(classify): `classify` builds a rule classification with the Reviewer's description
  but drops the Reviewer's model id and prompt hash, so the report cannot tell a described
  `collateral` from an undescribed one (9.2 Reviewer line, 9.3 item 2). The run copies them onto a
  rule classification that carries a Reviewer description.
- The Nightly canary pass writes one synthetic outcome row per checkpoint at most: 8.3 keys
  `outcomes` by (run, checkpoint), and the store has no API for `canary_results`, so the result rows
  go to `canary-results.json` (and the artifact store) and only the synthetic `harness_error` rows
  `canary.score` returns are written to `outcomes`.
- No Oracle exists in the core, so no `assertion_failed` is produced; money cells are covered by
  their zero-floor pixel diff, which 10.3 counts as detection when it lands on the mutated region.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import cv2
import numpy as np

from .. import canary as cn
from .. import invariants as inv
from ..classify import (REVIEWER_ROWS, Classified, Condition, DecisionMatch, Facts, HarnessFailure, Review, classify)
from ..coverage import Profile, build_matrix
from ..diff import load_params
from ..diff.calibration import CalibrationFile, Capture as CalCapture, calibrate_checkpoint
from ..diff.engine import DiffContext, DiffResult, compare_fingerprints, diff_pair, fingerprint_id
from ..diff.phash import phash
from ..diff.regions import RegionsFile, effective_registry, park_rect
from ..diff.settle import Recapture, RerunBudget, SettleResult, diff_stability, settle
from ..lint import lint
from ..records import PERSONAS, CheckpointOutcome, Decision, Finding, RunOutcome, Signature, StepOutcome
from ..report import ReportContext, build_report, render_pr_comment, render_run_report
from ..report.cluster import Member, cluster_members
from ..scope import (DependencyIndex, Manifest, PlannedCheckpoint, Rig, ScopeConfigError, ScopePlan, classes_in,
                     compute_scope)
from ..store import ArtifactStore, Store
from ..store.artifacts import ManifestHeader, Redaction, StoredArtifact
from ..store.store import RunPlan, ScopeEntry, ShardPlan
from ..taxonomy import checkpoint_parts, load
from .config import SentinelConfig, as_dict, materialize, parse_time, tree_sha256
from .reviewer import ReviewerAdapter, ReviewRequest, SimulatedReviewer, change_types, to_review, validate
from .sim import Capture, SimApp, SimBuild, observe

DEFAULT_TODAY = date(2026, 11, 10)
RIGS = ("rig-01", "rig-02", "rig-03", "rig-04")
REDACTION = Redaction("redaction-v4-sim")
PR_REVIEWER_CAP = 60                     # 3.15 per-run call cap of a PR run
GATES_S, TEARDOWN_S = 9 * 60, 6 * 60     # 1.7: gates T+0:03 to 0:09; re-runs, teardown and report
LISTED = ("invariant_violation", "assertion_failed", "collateral", "needs_human", "intended", "landmark_missing",
          "needs_baseline", "harness_error")
FINGERPRINT = {"os_build": "10.0.22631.4460", "display": "1920x1080@100", "theme": "dark",
               "font_set": "sentinel-pinned-2", "jre": "17.0.11+9", "gpu_driver": "basic-display-10.0",
               "render_probe_hash": "8f3c2a1b4d5e6f70", "bounds_source": "B"}


@dataclass(frozen=True)
class Scenario:
    """What the build pipeline's webhook delivers for one run (1.7 T+0:00), plus the simulated builds."""

    name: str
    run_id: str
    candidate: SimBuild
    base: SimBuild
    run_type: str = "pr"
    pr: int | None = None
    author: str = "jdoe"
    overlay: Mapping[str, str] = field(default_factory=dict)       # config files the PR or an edit adds
    changed_paths: tuple[str, ...] = ()
    depends_on: Mapping[str, tuple[str, ...]] | None = None        # the candidate's dependency index
    clock: str = "10:31:00"                                         # run start, America/New_York
    reviewer_cap: int = PR_REVIEWER_CAP
    canary: Mapping[str, Any] | None = None                         # last Nightly's state for run health
    detector_banner: str | None = None
    history: tuple[dict, ...] = ()


@dataclass
class Evaluated:
    """One checkpoint after capture, diff, invariants, re-runs and the decision lookup."""

    cp: PlannedCheckpoint
    base: Capture | None
    cand: Capture
    registry: RegionsFile
    facts: Facts
    classified: Classified
    diff: DiffResult | None = None
    comparison: inv.Comparison | None = None
    region: str | None = None
    phash: str | None = None
    crop: tuple[int, int, int, int] | None = None
    recaptures: int = 0
    related: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    answer: dict | None = None
    review: Review | None = None
    settle_ms: int | None = None

    @property
    def category(self) -> str:
        """The computed category before any decision changed the outcome (9.7 signature)."""
        return self.facts_category or self.classified.outcome

    facts_category: str | None = None


@dataclass
class RunResult:
    """What one run produced, for the demo, the tests and summary.txt."""

    run_id: str
    run_type: str
    status: str
    reason: str | None
    out_dir: Path
    outcomes: list[CheckpointOutcome] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    comment: str = ""
    commit_status: dict = field(default_factory=dict)
    plan: ScopePlan | None = None
    manifest: dict | None = None
    reviewer_calls: list[dict] = field(default_factory=list)
    disabled_invariants: dict[str, list[str]] = field(default_factory=dict)
    lint_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    canary: Any = None
    extra_flows: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            if not o.synthetic:
                out[o.outcome] = out.get(o.outcome, 0) + 1
        return out


# ------------------------------------------------------------------------------------------------
# Small helpers


def _ts(today: date, clock: str, plus_s: int = 0) -> str:
    """An ISO time in America/New_York (EST in November) for this run's clock."""
    d = datetime.fromisoformat(f"{today.isoformat()}T{clock}-05:00") + timedelta(seconds=plus_s)
    return d.isoformat()


def _png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buf.tobytes()


def _clip(box: tuple[int, int, int, int], w: int, h: int, pad: int = 0) -> tuple[int, int, int, int]:
    x, y, bw, bh = box
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(w, x + bw + pad), min(h, y + bh + pad)
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def _crop(img: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = box
    return img[y:y + h, x:x + w]


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _fingerprint(epoch: str) -> tuple[dict, str]:
    match = dict(FINGERPRINT, data_epoch_id=epoch)
    return match, fingerprint_id(match)


def _yaml_dump(obj: Any) -> str:
    import yaml
    return yaml.safe_dump(obj, sort_keys=False)


def _flow_class(cfg: SentinelConfig, cp: PlannedCheckpoint) -> str | None:
    if cp.test_kind != "flow":
        return None
    return str(cfg.flow_docs.get(cp.flow_id or "", {}).get("class", "standard"))


def _recapturable(cfg: SentinelConfig, cp: PlannedCheckpoint) -> bool:
    if cp.test_kind != "flow":
        return True
    return bool(cfg.flow_step(cp.flow_id or "", cp.step_id or "").get("recapturable", True))


def _host(cp: PlannedCheckpoint) -> str | None:
    step = dict(cp.size_step or {})
    return step.get("host") or step.get("splitter")


# ------------------------------------------------------------------------------------------------
# The context of one run

# Calibrations already measured in this process, by fingerprint, base build, checkpoint and registry:
# a fingerprint is calibrated once and reused until it changes (3.9, 7.13), so a second run of the same
# base build in one process reads the floors instead of re-measuring them.
_CALIBRATIONS: dict[tuple, Any] = {}


def _registry_key(reg: RegionsFile) -> str:
    parts = [f"{lm.id}:{hashlib.sha256(lm.template.tobytes()).hexdigest()[:16]}:{lm.expected}" for lm in reg.landmarks]
    parts += [f"{r.id}:{r.type}:{r.rect}:{r.money_cell}:{r.mask_ref}" for r in reg.regions]
    return "|".join(parts)


class _Run:
    """Shared state of one run: config, apps, calibration, rules, budget, store handles."""

    def __init__(self, cfg: SentinelConfig, run_id: str, run_type: str, base: SimBuild, cand: SimBuild,
                 now: datetime, manifest: Manifest | None, plan: ScopePlan | None, x_input: Callable[[str], str]):
        self.cfg, self.run_id, self.run_type = cfg, run_id, run_type
        self.base_app, self.cand_app = SimApp(base), SimApp(cand)
        self.now, self.manifest, self.plan, self.x_input = now, manifest, plan, x_input
        self.params = load_params()
        epoch = str(((cfg.fixture.get("epoch") or {}).get("epoch_id")) or "epoch-unknown")
        self.fp_match, self.fp_id = _fingerprint(epoch)
        self.epoch = epoch
        self.calibration: CalibrationFile | None = None
        self.rules: dict[str, inv.SurfaceRules] = {}
        self.disabled: dict[str, list[str]] = {}
        self.registries: dict[str, RegionsFile] = {}
        self.expiry_notices: list = []
        self.budget: RerunBudget | None = None
        self.entries = frozenset(e.entry_id for e in manifest.entries) if manifest else frozenset()
        self.tickets = frozenset(t for e in manifest.entries for t in e.tickets) if manifest else frozenset()

    # -- registry and rules ----------------------------------------------------------------
    def registry(self, cp: PlannedCheckpoint, base: Capture) -> RegionsFile:
        if cp.checkpoint_id not in self.registries:
            doc = self.cfg.regions_doc(cp.checkpoint_id, cp.surface_id, cp.test_kind, _host(cp))
            reg = SimApp.registry(cp, base.screen, base.info, doc)
            reg, notices = effective_registry(reg, self.cfg.masks, self.now)
            self.expiry_notices += notices
            self.registries[cp.checkpoint_id] = reg
        return self.registries[cp.checkpoint_id]

    def surface_rules(self, cp: PlannedCheckpoint) -> inv.SurfaceRules:
        key = f"{cp.surface_id}|{cp.data_mode}"
        if key not in self.rules:
            self.rules[key] = self.cfg.rules(cp.surface_id, cp.data_mode)
        return self.rules[key]

    def capture(self, cp: PlannedCheckpoint, side: str, tick: int = 0) -> Capture:
        app = self.base_app if side == "base" else self.cand_app
        return app.capture(cp, side, controls=self.cfg.control_ids(cp.surface_id), tick=tick)

    # -- 3.9 A/A calibration and the 2.7 enablement test -------------------------------------
    def calibrate(self, planned: Sequence[PlannedCheckpoint], out_dir: Path) -> None:
        """The Calibration run of 1.6 on the base build, before any threshold is used."""
        cal = CalibrationFile.load(out_dir / "calibration", self.fp_id) or CalibrationFile(self.fp_id, f"cal-{self.run_id}")
        pairs: dict[str, list[inv.AAPair]] = {}
        for cp in planned:
            caps = [self.capture(cp, "base", tick=10 + k) for k in range(6)]
            reg = self.registry(cp, caps[0])
            if cp.checkpoint_id not in cal.checkpoints:     # a fingerprint is calibrated once (3.9, 7.13)
                key = (self.fp_id, self.base_app.build, cp.checkpoint_id, cp.data_mode, _registry_key(reg))
                if key not in _CALIBRATIONS:
                    _CALIBRATIONS[key] = calibrate_checkpoint(
                        [CalCapture(c.screen.image, time_to_settle_ms=3 * 250) for c in caps], reg,
                        data_mode=cp.data_mode, system_masks=[park_rect(_park(caps[0]))], now=self.now,
                        params=self.params)
                cal.checkpoints[cp.checkpoint_id] = _CALIBRATIONS[key]
            pairs.setdefault(f"{cp.surface_id}|{cp.data_mode}", []).append(
                inv.AAPair(cp.data_mode, caps[0].models, caps[5].models))
        for key, ps in sorted(pairs.items()):
            surface, mode = key.split("|")
            rules = self.cfg.rules(surface, mode)
            statuses = inv.enablement(ps, rules)
            self.rules[key] = inv.with_enablement(rules, statuses)
            off = [i for i, s in statuses.items() if s != inv.ENABLED]
            if off:
                self.disabled[surface] = off
        self.calibration = cal
        cal.save(out_dir / "calibration")


# ------------------------------------------------------------------------------------------------
# One checkpoint (5.17): settle, diff, invariants, re-runs, decisions, classification


def _park(cap: Capture) -> tuple[int, int]:
    """The declared parking coordinate (3.5): a dead area 8 px inside the capture's bottom-right corner."""
    return cap.screen.image.shape[1] - 8, cap.screen.image.shape[0] - 8


def _region_choice(ev_reg: RegionsFile, res: DiffResult | None, comparison: inv.Comparison | None,
                   cand_lm: Mapping[str, tuple[int, int]], size: tuple[int, int]) -> tuple[str | None, tuple | None]:
    """The signature region (9.7) and its candidate bounds: the declared region the finding lands in,
    else the 8x8 grid cell (relative to the landmark) holding the largest component."""
    W, H = size
    located = {}
    for r in ev_reg.regions:
        if r.pixel_diffed and r.landmark in cand_lm:
            lx, ly = cand_lm[r.landmark]
            located[r.id] = (r.rect.x + lx, r.rect.y + ly, r.rect.w, r.rect.h)
    if comparison is not None and comparison.fired:
        crop = comparison.reported[0].violation.crop
        best, area = None, 0
        for rid, (x, y, w, h) in located.items():
            ox = min(x + w, crop.right) - max(x, crop.x)
            oy = min(y + h, crop.bottom) - max(y, crop.y)
            if ox > 0 and oy > 0 and ox * oy > area:
                best, area = rid, ox * oy
        if best:
            return best, located[best]
    if res is not None and res.regions:
        above = [m for m in res.regions if m.above_threshold and m.region_id != "_rest"]
        if above:
            m = max(above, key=lambda m: (m.changed_px + 100 * m.shift_px, m.region_id))
            return m.region_id, located.get(m.region_id, m.candidate_rect.as_tuple())
        comps = sorted(res.components, key=lambda c: (-c.area, c.bbox))
        if comps:
            ox, oy = next(iter(cand_lm.values()), (0, 0))
            cx, cy = comps[0].centroid
            col, row = min(7, max(0, int((cx - ox) * 8 // W))), min(7, max(0, int((cy - oy) * 8 // H)))
            cw, ch = math.ceil(W / 8), math.ceil(H / 8)
            box = (ox + col * cw, oy + row * ch, cw, ch)
            return f"g-{row}-{col}", box
    return None, None


def evaluate(run: _Run, cp: PlannedCheckpoint, base: Capture | None, cand: Capture, *, x: str,
             primitive: str | None, entry_ref: str | None, recapture: Callable[[], Capture],
             recapturable: bool = True, store: Store | None = None, synthetic: bool = False,
             absent_reason: str | None = None, injected: bool = False) -> Evaluated:
    """5.17 from capture to classification for one checkpoint; PR checkpoints and canary mutants alike.
    An `injected` candidate (a canary mutant, 10.3 Injection) is already a settled capture: the settle
    loop, a capture-time gate, does not run on it."""
    reg = run.registry(cp, base if base is not None else cand)
    rules = run.surface_rules(cp)
    park = _park(cand)
    spec = SimApp.settle_spec(reg, park)
    fkw: dict[str, Any] = dict(manifest=x, primitive=primitive, manifest_entry_ref=entry_ref,
                               manifest_entries=run.entries, manifest_tickets=run.tickets)
    s_c = settle(cand.frames, spec, run.params) if not injected else _as_settled(cand)
    s_b = settle(base.frames, spec, run.params) if base is not None else None
    res, comparison = None, None
    recaps = 0
    if not s_c.settled:
        facts = Facts(not_settled=Condition(s_c.reason, side="candidate", detail=s_c.detail), **fkw)
    elif base is None or (s_b is not None and not s_b.settled):
        facts = Facts(base="absent", base_reason=absent_reason or "base_not_settled", **fkw)
        comparison = inv.compare(None, cand.models, rules)
    else:
        res = diff_pair(s_b.capture, s_c.capture, reg, calibration=run.calibration, system_masks=[park_rect(park)],
                        context=DiffContext(run.run_id, cp.checkpoint_id, cp.tier, synthetic=synthetic),
                        params=run.params)
        comparison = inv.compare(base.models, cand.models, rules)
        if res.terminal is not None:
            t = res.terminal
            if t.outcome == "landmark_missing":
                facts = Facts(landmark=Condition(t.reason, side=t.side, detail=t.detail), **fkw)
            else:
                event = "wd-" + hashlib.sha256(f"{run.run_id}|{cp.checkpoint_id}".encode()).hexdigest()[:8]
                facts = Facts(harness=HarnessFailure(t.reason, event), **fkw)
        else:
            inv_ids, inv_stab, inv_cause = (), None, None
            stab, cause = None, None
            budget_ok = run.budget.take() if (run.budget and (comparison.fired or res.level == "above")
                                              and recapturable) else True
            if comparison.fired:
                if not recapturable or not budget_ok:
                    p = inv.confirm(comparison, None, cause="not_recapturable" if not recapturable else "budget")
                else:
                    again = recapture()
                    recaps = 1
                    p = inv.confirm(comparison, inv.check_capture(again.models, rules, reference=base.models[0]))
                inv_ids = tuple(r.violation.invariant_id for r in (p.confirmed or p.unconfirmed))
                inv_stab, inv_cause = p.stability, p.stability_cause
            if res.level == "above":
                if comparison.fired:
                    stab, cause = inv_stab, inv_cause
                else:
                    def above(a: np.ndarray, b: np.ndarray) -> bool:
                        return diff_pair(a, b, reg, calibration=run.calibration, system_masks=[park_rect(park)],
                                         context=DiffContext(run.run_id, cp.checkpoint_id, cp.tier, synthetic=synthetic),
                                         params=run.params).level == "above"
                    if recapturable and budget_ok:
                        again = [recapture(), recapture()]
                        recaps = 2
                        st = diff_stability(s_c.capture, [Recapture(True, a.screen.image) for a in again], above,
                                            params=run.params)
                    else:
                        st = diff_stability(s_c.capture, [], above, recapturable=recapturable, budget_ok=budget_ok,
                                            params=run.params)
                    stab, cause = st.stability, st.cause
            fix = None
            if res.threshold_source == "uncalibrated":
                fix = "no calibrated floor for this fingerprint (3.9); threshold 0"
            facts = Facts(diff=res.level, stability=stab, stability_cause=cause, threshold_source=res.threshold_source,
                          fix_list_item=fix, invariant_ids=inv_ids, invariant_stability=inv_stab,
                          invariant_stability_cause=inv_cause, bounds_unreadable=comparison.unreadable, **fkw)
    classified = classify(facts)
    ev = Evaluated(cp, base, cand, reg, facts, classified, res, comparison, recaptures=recaps,
                   settle_ms=s_c.time_to_settle_ms)
    ev.facts_category = classified.outcome
    cand_lm = {k: m[1].pos for k, m in (res.landmarks.items() if res else []) if m[1].found}
    h, w = cand.screen.image.shape[:2]
    if classified.outcome in LISTED and classified.outcome != "harness_error":
        region, box = _region_choice(reg, res, comparison, cand_lm, (w, h))
        if region is None and comparison is not None and comparison.fired:
            region, box = "_window", (0, 0, w, h)
        if region is not None and box is not None:
            ev.region = region
            ev.phash = phash(_crop(cand.screen.image, _clip(box, w, h)))
            ev.crop = _clip(box, w, h, 24)
    if comparison is not None and comparison.fired:
        u = inv.Rect.union(r.violation.crop for r in comparison.reported)
        ev.crop = _clip(u.as_tuple(), w, h, 24)
    if ev.crop is None:
        ev.crop = (0, 0, w, h)
    if store is not None and not synthetic and ev.phash and ev.region:
        look = store.lookup_decisions(Signature(cp.checkpoint_id, ev.region, ev.phash, ev.category),
                                      at=run.now.isoformat(), run_id=run.run_id, manifest_entries=run.entries,
                                      manifest_tickets=run.tickets)
        ev.related = [{"decision_id": m.decision.decision_id, "hamming": m.distance} for m in look.related]
        if look.applicable and not look.frozen:      # a frozen signature inherits nothing (9.7)
            facts = dataclasses.replace(facts, decisions=tuple(DecisionMatch(m.decision, m.distance)
                                                               for m in look.applicable))
            ev.facts, ev.classified = facts, classify(facts)
            if look.conflict:
                ev.conflicts = [{"decision_id": m.decision.decision_id, "verdict": m.decision.verdict}
                                for m in look.matches]
    return ev


def _as_settled(cap: Capture) -> SettleResult:
    """An injected capture is a settled capture already (10.3 Injection)."""
    return SettleResult(True, cap.screen.image, 0, 750, None, None, None, 750)


def _with_review(ev: Evaluated, review: Review) -> None:
    ev.review = review
    ev.facts = dataclasses.replace(ev.facts, review=review)
    c = classify(ev.facts)
    cls = c.classification
    if cls is not None and cls.by == "rule" and cls.description and not cls.model_id:
        # WORKAROUND(classify): keep the describing Reviewer call's model id and prompt hash (module docstring)
        c = dataclasses.replace(c, classification=dataclasses.replace(cls, model_id=review.model_id,
                                                                       prompt_hash=review.prompt_hash))
    ev.classified = c
    if not (c.reason or "").startswith("decision:"):
        ev.facts_category = c.outcome     # the verdict decided the category; a decision would not have


# ------------------------------------------------------------------------------------------------
# Records


_NOUNS = {"ladder.viewport": "the ladder viewport", "ladder.strip": "the order-control strip",
          "grid.cell00.header": "the cell header"}
# 1.10's short names for the fifteen invariants (the `invariant_violation` definition)
SHORT = {"INV-01": "sibling bounds intersect", "INV-02": "component outside parent", "INV-03": "text clipped",
         "INV-04": "ellipsis", "INV-05": "required label missing", "INV-06": "unexpected scrollbar",
         "INV-07": "font below minimum", "INV-08": "placeholder or error text", "INV-09": "window off screen",
         "INV-10": "control count wrong", "INV-11": "text runs overlap", "INV-12": "columns misaligned",
         "INV-13": "focus order wrong", "INV-14": "error dialog present", "INV-15": "sign and color disagree"}


def _noun(name: str, screen) -> str:
    try:
        x, y, w, h = screen.get(name).bbox
    except KeyError:
        return name
    return f"{_NOUNS.get(name, name)} (y {y} to {y + h})"


def _violation_text(ev: Evaluated) -> tuple[str, dict]:
    """The invariant finding's description and the evidence notes 9.2 prints (overlay, violation)."""
    parts, ev_notes = [], {}
    cand, base = ev.cand.screen, ev.base.screen if ev.base else None
    by_id: dict[str, list] = {}
    for r in ev.comparison.reported if ev.comparison else ():
        by_id.setdefault(r.violation.invariant_id, []).append(r)
    for inv_id, reps in by_id.items():
        pre = " (pre-existing on the base, reported one severity lower)" if all(r.pre_existing for r in reps) else ""
        v = reps[0].violation
        if inv_id == "INV-01" and "|" in v.subject:
            a, b = v.subject.split("|")
            oy = inv.Rect(*cand.get(a).bbox).overlap(inv.Rect(*cand.get(b).bbox))[1]
            parts.append(f"{inv_id} {SHORT[inv_id]}: {_noun(b, cand)} overlaps {_noun(a, cand)} by {oy} px "
                         f"across its width{pre}.")
            ev_notes.setdefault("violation", f"{oy} px intersection")
            if base is not None:
                try:
                    ba, bb = inv.Rect(*base.get(a).bbox), inv.Rect(*base.get(b).bbox)
                    gap = max(ba.y, bb.y) - min(ba.bottom, bb.bottom)
                    touch = f"touch at y={max(ba.y, bb.y)}, 0 px gap" if gap == 0 else f"are {gap} px apart"
                    ev_notes.setdefault("overlay", f"bounds overlaid, intersection hatched; on the base the "
                                                   f"{_NOUNS.get(a, a).replace('the ', '')} and "
                                                   f"{_NOUNS.get(b, b).replace('the ', '')} {touch}")
                except KeyError:
                    pass
        elif inv_id == "INV-10":
            ids = [r.violation.subject for r in reps]
            parts.append(f"{inv_id} {SHORT[inv_id]}: required control{'s' if len(ids) > 1 else ''} "
                         f"{' and '.join(ids)} not found by template (covered by a later-drawn component){pre}.")
        else:
            details = "; ".join(r.violation.detail for r in reps)
            for k, noun in _NOUNS.items():
                details = details.replace(k, noun.replace("the ", ""))
            parts.append(f"{inv_id} {SHORT[inv_id]}: {details}{pre}.")
    return " ".join(parts), ev_notes


def _ocr_in(model: inv.ScreenModel, box: tuple[int, int, int, int]) -> str:
    x, y, w, h = box
    words = [wd for wd in model.words if x <= wd.bbox.center[0] < x + w and y <= wd.bbox.center[1] < y + h]
    return " ".join(wd.text for wd in sorted(words, key=lambda wd: (wd.bbox.y, wd.bbox.x)))


def _metrics_text(ev: Evaluated) -> str:
    """A finding description from what the run measured: the Diff Engine's regions and, inside them,
    the OCR words of the bounds source on both builds (there is no Reviewer description)."""
    if ev.diff is None:
        return ""
    money = {r.id for r in ev.registry.regions if r.money_cell}
    parts = []
    for m in ev.diff.regions:
        if not m.above_threshold:
            continue
        text = f"{m.region_id}{' (money cell, zero floor)' if m.region_id in money else ''}: {m.changed_px} px changed"
        if any(m.shift):
            text += f", shifted {m.shift}"
        if ev.base is not None:
            was, now = _ocr_in(ev.base.model, m.base_rect.as_tuple()), _ocr_in(ev.cand.model, m.candidate_rect.as_tuple())
            if was != now:
                text += f"; OCR reads '{now}' where the base read '{was}'"
        parts.append(text)
    return "; ".join(parts) + "." if parts else ""


def _overlay(img: np.ndarray, rects: Iterable[inv.Rect]) -> np.ndarray:
    """Violation bounds drawn over a capture, the intersection hatched (1.11 evidence)."""
    out = img.copy()
    rs = list(rects)
    for r in rs:
        cv2.rectangle(out, (r.x, r.y), (r.right - 1, r.bottom - 1), (0, 170, 255), 1)
    for i, a in enumerate(rs):
        for b in rs[i + 1:]:
            x0, y0 = max(a.x, b.x), max(a.y, b.y)
            x1, y1 = min(a.right, b.right), min(a.bottom, b.bottom)
            for k in range(x0 - (y1 - y0), x1, 6):
                cv2.line(out, (max(k, x0), y0 + max(0, x0 - k)), (min(k + (y1 - y0), x1 - 1), min(y1 - 1, y0 + x1 - 1 - k)),
                         (255, 64, 64), 1)
    return out


class _Writer:
    """Turns evaluated checkpoints into the records of 4.13 and their artifacts (8.4)."""

    def __init__(self, run: _Run, arts: ArtifactStore, today: date, clock: str):
        self.run, self.arts, self.today, self.clock = run, arts, today, clock
        tag = run.run_id.split("-pr-")[-1].replace("-", "") if "-pr-" in run.run_id else \
            hashlib.sha256(run.run_id.encode()).hexdigest()[:6]
        self.tag, self.n = tag, 0

    def _put(self, img: np.ndarray, name: str, kind: str, cid: str, side: str | None, tag: str = "") -> StoredArtifact:
        path = f"{self.run.run_id}/{cid}/{tag + '/' if tag else ''}{name}.png"
        return self.arts.put(_png(img), name=path, kind=kind, media_type="image/png",
                             redaction=REDACTION, checkpoint_id=cid, side=side,
                             captured_at=_ts(self.today, self.clock))

    def artifacts(self, ev: Evaluated, tag: str = "") -> tuple[dict, list[StoredArtifact]]:
        cid = ev.cp.checkpoint_id
        stored: list[StoredArtifact] = []
        cimg = ev.cand.screen.image
        bimg = ev.base.screen.image if ev.base else None
        listed = ev.classified.outcome in LISTED or ev.classified.decision_ref is not None
        box = ev.crop if listed else (0, 0, cimg.shape[1], cimg.shape[0])
        if ev.comparison is not None and ev.comparison.fired:
            rects = [r for rep in ev.comparison.reported[:1] for r in rep.violation.rects]
            cimg = _overlay(cimg, rects)
            if bimg is not None:
                names = ev.comparison.reported[0].violation.subject.split("|")
                brects = []
                for nm in names:
                    try:
                        brects.append(inv.Rect(*ev.base.screen.get(nm).bbox))
                    except KeyError:
                        pass
                bimg = _overlay(bimg, brects or rects)
        out: dict[str, Any] = {}
        a = self._put(_crop(cimg, _clip(box, cimg.shape[1], cimg.shape[0])), "candidate", "crop", cid, "candidate", tag)
        stored.append(a)
        out["candidate"] = a.sha256
        if bimg is not None:
            b = self._put(_crop(bimg, _clip(box, bimg.shape[1], bimg.shape[0])), "base", "crop", cid, "base", tag)
            stored.append(b)
            out["base"] = b.sha256
        if listed and ev.diff is not None and ev.diff.terminal is None and ev.diff.level != "zero":
            hm = ev.diff.heatmap
            hmap = self._put(_crop(hm, _clip(box, hm.shape[1], hm.shape[0])), "heatmap", "heatmap", cid, None, tag)
            stored.append(hmap)
            out["heatmap"] = hmap.sha256
        return out, stored

    def records(self, ev: Evaluated, *, synthetic: bool = False, data_mode: str | None = None, tag: str = ""
                ) -> tuple[CheckpointOutcome, list[Finding], list[StoredArtifact]]:
        cp, c = ev.cp, ev.classified
        arts, stored = self.artifacts(ev, tag)
        diff = None
        if ev.diff is not None and ev.diff.terminal is None:
            diff = ev.diff.summary
        rec_kw = c.fields()
        o = CheckpointOutcome(
            run_id=self.run.run_id, checkpoint_id=cp.checkpoint_id, workspace_id=cp.workspace_id,
            persona=cp.persona, data_mode=data_mode or cp.data_mode, surface_id=cp.surface_id,
            test_kind=cp.test_kind, tier=cp.tier, flow_id=cp.flow_id, step_id=cp.step_id,
            size_step=dict(cp.size_step) if cp.size_step else None, diff=diff, artifacts=arts,
            recaptures=ev.recaptures, synthetic=synthetic, flow_class=_flow_class(self.run.cfg, cp),
            fingerprint_id=self.run.fp_id, data_epoch_id=self.run.epoch,
            captured_at=_ts(self.today, self.clock, 600), recorded_at=_ts(self.today, self.clock, 900), **rec_kw)
        findings: list[Finding] = []
        if synthetic:
            return o, findings, stored
        if c.outcome in LISTED or c.decision_ref:
            self.n += 1
            fid = f"F-{self.tag}-{self.n:03d}"
            evidence: dict[str, Any] = {"target_window": ev.cand.info.target_window}
            if cp.test_kind in ("sweep", "split") and ev.cand.info.layout:
                evidence["layout"] = ev.cand.info.layout
            sev = None
            if ev.comparison is not None and ev.comparison.fired and c.outcome == "invariant_violation":
                desc, notes = _violation_text(ev)
                evidence.update(notes)
                sev = ev.comparison.reported[0].severity
            elif c.outcome == "landmark_missing":
                desc = f"Landmark not found ({c.reason}, side {c.side}): {c.detail}"
                evidence["side"] = c.side
            elif c.classification is not None and c.classification.description:
                desc = c.classification.description
            else:
                desc = _metrics_text(ev) or (c.detail or c.outcome)
            if ev.answer is not None:
                evidence["reviewer"] = {"verdict": ev.answer["verdict"], "change_types": change_types(ev.answer)}
            if ev.related:
                evidence["near_decisions"] = ev.related
            if ev.conflicts:
                evidence["conflicting_decisions"] = ev.conflicts
            kind = ev.category if ev.category in load().finding_kinds else c.outcome
            if kind not in load().finding_kinds:
                kind = "collateral"
            findings.append(Finding(
                finding_id=fid, run_id=self.run.run_id, kind=kind, description=desc, first_seen_run_id=self.run.run_id,
                checkpoint_id=cp.checkpoint_id, tier=cp.tier, region_id=ev.region, primitive=c.primitive,
                manifest_entry_ref=c.manifest_entry_ref, phash=ev.phash, category=ev.category, evidence=evidence,
                severity=sev, side=c.side, step_id=cp.step_id, decision_ref=c.decision_ref,
                created_at=_ts(self.today, self.clock, 900)))
        if ev.diff is not None:
            for f in ev.diff.findings:
                findings.append(dataclasses.replace(f, created_at=_ts(self.today, self.clock, 900), tier=cp.tier))
        return o, findings, stored


# ------------------------------------------------------------------------------------------------
# Scope workaround, coverage and run-health inputs


def _case_b_flows(cfg: SentinelConfig, plan: ScopePlan) -> list[PlannedCheckpoint]:
    """WORKAROUND(scope): in case (b), 2.11's flow rule over the smoke census (module docstring)."""
    if plan.manifest_state != "no_manifest_nonui" or plan.scope_surfaces:
        return []
    have = set(plan.planned_ids)
    out = []
    for f in sorted(cfg.flows, key=lambda f: f.flow_id):
        if f.tier != 1 or f.flow_class != "standard" or not set(f.surfaces) & plan.census_surfaces:
            continue
        if not f.requires <= cfg.entitlements:
            continue
        persona = f.persona
        for fc in f.checkpoints:
            if fc.checkpoint_id in have:
                continue
            tier = cfg.registry[fc.surface_id].tier if fc.surface_id in cfg.registry else f.tier
            out.append(PlannedCheckpoint(fc.checkpoint_id, fc.surface_id, "flow", tier, f.workspace, persona,
                                         fc.data_mode, flow_id=f.flow_id, step_id=fc.step_id))
    return out


def coverage_summary(cfg: SentinelConfig, plan: ScopePlan, outcomes: Sequence[CheckpointOutcome], run_id: str,
                     manifest: Manifest | None) -> dict:
    """The PR run's coverage summary the Reporter reads (`report.model` docstring, 2.12)."""
    matrix = build_matrix(run_id, cfg.registry, [Profile("margin", cfg.entitlements)], {"margin": list(outcomes)},
                          plan.cuts, PERSONAS)
    tiers: dict[int, dict] = {}
    for s in cfg.registry:
        t = tiers.setdefault(s.tier, {"registered": 0, "in_scope": 0, "covered": 0, "not_compared": {}, "not_run": {}})
        t["registered"] += 1
        cells = [matrix.cell(s.id, p, "margin") for p in PERSONAS]
        scoped = any(o.surface_id == s.id for o in outcomes) or any(
            c.surface_id == s.id and c.reason != "scope" for c in plan.cuts)
        if not scoped:
            t["not_run"]["scope"] = t["not_run"].get("scope", 0) + 1
            continue
        t["in_scope"] += 1
        if any(c.state == "covered" for c in cells):
            t["covered"] += 1
            continue
        reasons = [c.reason for c in cells if c.state == "not_run" and c.reason and c.reason != "scope"]
        blocked = [r for r in reasons if r.startswith("blocked:")]
        if blocked:
            k = blocked[0].split(":", 1)[1]
            t["not_compared"][k] = t["not_compared"].get(k, 0) + 1
        elif reasons:
            k = reasons[0].split(":", 1)[0]
            t["not_run"][k] = t["not_run"].get(k, 0) + 1
    impact = plan.impact
    touched = sorted(impact.touched_primitives) if impact else []
    edges = sum(1 for p in touched for _ in cfg.dep_map.surfaces_of(p, cfg.registry))
    static_added = len(impact.static_surfaces - impact.map_expanded - plan.declared) if impact else 0
    smoke = cfg.registry.smoke_subset()
    smoke_cps = sum(1 for cp in plan.planned if cp.surface_id in smoke and cp.test_kind == "census")
    cells = [{"surface": c.surface_id, "persona": c.persona, "state": c.state, "worst": c.worst, "n": c.count,
              "reason": c.reason} for c in matrix.cells.values()
             if not (c.state == "not_run" and c.reason == "scope" and c.persona not in
                     {o.persona for o in outcomes})]
    return {"tiers": tiers,
            "scope": {"declared_surfaces": len(plan.declared), "edges": edges, "primitives": touched,
                      "static_impact_added": static_added, "smoke_census": smoke_cps,
                      "send_policy": cfg.send_policy, "order_surfaces_entitlement": 0},
            "cells": cells}


# ------------------------------------------------------------------------------------------------
# The store: identity rows


def _register(store: Store, cfg: SentinelConfig, run_ids: Iterable[str], builds: Iterable[str], today: date) -> None:
    for i, rig in enumerate(RIGS, 1):
        store.register_rig(rig, login_alias=f"vault://sentinel/rigs/{rig}/tos-login", owner="asmith",
                           snapshot_id=f"snap-{rig}-2026-11-01", allowlist_entry=i)
    for bid in builds:
        doc = cfg.builds.get(bid)
        if doc is None:
            continue
        b = doc["build"]
        store.register_build(bid, channel=b["channel"], source_commit=b["commit_sha"],
                             pipeline_artifact_url=(b.get("artifact") or {}).get("uri", "sim://"),
                             installer_sha256=str((b.get("artifact") or {}).get("sha256", "sha256:sim")).split(":")[-1],
                             branch=b.get("branch"), first_seen_at=str(b.get("built_at")) if b.get("built_at") else None)


def _run_plan(run_id: str, run_type: str, cfg_root: Path, scope: Sequence[ScopeEntry], shards: Sequence[ShardPlan],
              started: str, manifest_text: str | None, pr: int | None, author: str | None) -> RunPlan:
    sha = tree_sha256(cfg_root)
    return RunPlan(run_id=run_id, run_type=run_type, config_commit=sha[:12], config_tarball_sha256=sha,
                   snapshot_set_sha256=hashlib.sha256(",".join(RIGS).encode()).hexdigest(),
                   harness_sha="sentinel-core-0.1.0-sim", authorization_ref="AUTH-SIM-2026-11",
                   budget_sec=2700 if run_type == "pr" else 4 * 3600, scope=tuple(scope), shards=tuple(shards),
                   manifest_sha256=hashlib.sha256(manifest_text.encode()).hexdigest() if manifest_text else None,
                   pr_ref=f"PR {pr}" if pr else None, pr_author=author, started_at=started)


def _status_json(status) -> dict:
    return {"context": status.context, "state": status.state, "description": status.description,
            "target_url": status.target_url, "mode": status.mode}


# ------------------------------------------------------------------------------------------------
# run_pipeline


def _abort_config(store: Store, scenario: Scenario, cfg_root: Path, errors: list[str], rule_ids: list[str],
                  out_dir: Path, today: date, started: str) -> RunResult:
    """1.7 T+0:00: a lint failure ends the run aborted_harness (reason: config_invalid)."""
    store.begin_run(_run_plan(scenario.run_id, scenario.run_type, cfg_root, (), (), started, None, scenario.pr,
                              scenario.author))
    store.fill_unreached(scenario.run_id, "harness_error", "config_invalid")   # 1.10: none was scoped yet
    run = store.end_run(RunOutcome(scenario.run_id, scenario.run_type, "aborted_harness", "provisioned",
                                   reason="config_invalid", started_at=started, ended_at=_ts(today, scenario.clock, 60)))
    ctx = ReportContext(pr=scenario.pr or 0, candidate_build=scenario.candidate.build_id,
                        base_build=scenario.base.build_id,
                        abort={"rule_ids": rule_ids, "requeued": "not re-queued: a config fix is needed first"})
    comment = render_pr_comment(run, [], [], [], {}, {}, None, ctx)
    report = build_report(run, [], [], [], {}, {}, None, ctx)
    (out_dir / "pr-comment.md").write_text(comment, encoding="utf-8")
    (out_dir / "commit-status.json").write_text(json.dumps(_status_json(report.status), indent=2) + "\n")
    render_run_report(run, [], [], [], {}, {}, None, ctx, out_dir / "report.html")
    return RunResult(scenario.run_id, scenario.run_type, run.status, run.reason, out_dir, comment=comment,
                     commit_status=_status_json(report.status), lint_errors=errors)


def run_pipeline(config_dir: str | Path, scenario: Scenario, out_dir: str | Path, today: date = DEFAULT_TODAY,
                 reviewer: ReviewerAdapter | None = None) -> RunResult:
    """One simulated PR run from webhook to PR comment (module docstring). Writes into `out_dir`:
    `config/<run_id>/`, `sentinel.db` (shared by every run of the directory), `artifacts/`,
    `calibration/`, `pr-comment.md`, `commit-status.json`, `report.html`, `reviewer-calls.json`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = _ts(today, scenario.clock)
    now = parse_time(started)
    cfg_root = materialize(config_dir, scenario.overlay, out / "config" / scenario.run_id)
    report_lint = lint(cfg_root, phase=1, today=today)
    with Store(out / "sentinel.db") as store:
        if report_lint.errors:
            errs = [v.line() for v in report_lint.errors]
            rules = sorted({v.rule for v in report_lint.errors})
            return _abort_config(store, scenario, cfg_root, errs, rules, out, today, started)
        cfg = SentinelConfig.load(cfg_root)
        cand_doc = cfg.builds.get(scenario.candidate.build_id, {})
        pair = cand_doc.get("build_pair") or {}
        base_id = pair.get("base", scenario.base.build_id)
        mdoc = cfg.manifests.get(scenario.candidate.build_id)
        manifest = Manifest.from_dict(mdoc) if mdoc else None
        dep_index = DependencyIndex.from_depends_on(scenario.depends_on) if scenario.depends_on is not None else None
        try:
            plan = compute_scope("pr", manifest, cfg.dep_map, cfg.registry, dep_index, classes_in(scenario.changed_paths),
                                 run_id=scenario.run_id, workspaces=cfg.workspaces, flows=cfg.flows,
                                 entitlements=cfg.entitlements, quarantine=cfg.quarantine,
                                 rigs=[Rig(r, "fp") for r in RIGS], candidate_build_id=scenario.candidate.build_id)
        except ScopeConfigError as exc:
            return _abort_config(store, scenario, cfg_root, [str(exc)], ["L-SRF-10"], out, today, started)
        extra = _case_b_flows(cfg, plan)
        planned = list(plan.planned) + extra
        rig_of = {cp.checkpoint_id: it.rigs[0] for it in plan.items for cp in it.checkpoints}
        for cp in extra:
            rig_of[cp.checkpoint_id] = RIGS[0]
        run = _Run(cfg, scenario.run_id, "pr", scenario.base, scenario.candidate, now, manifest, plan, plan.x_input)
        run.budget = RerunBudget(len(planned), run.params)
        run.calibrate(planned, out)
        # 3.3: fingerprint equality on every field but build id, before any capture
        mismatch = compare_fingerprints({"match": dict(run.fp_match, build_id=base_id)},
                                        {"match": dict(run.fp_match, build_id=scenario.candidate.build_id)})
        assert mismatch is None
        # -- T+0:00 to 0:02: the run row, its scope and shards (1.7)
        arts = ArtifactStore(out / "artifacts", f"runs/{scenario.run_id}")
        writer = _Writer(run, arts, today, scenario.clock)
        _register(store, cfg, [base_id, scenario.candidate.build_id], [base_id, scenario.candidate.build_id], today)
        used = sorted({rig_of[cp.checkpoint_id] for cp in planned})
        shard_of = {r: f"{scenario.run_id}/{i}" for i, r in enumerate(used, 1)}
        scope_entries = [ScopeEntry(cp.checkpoint_id, cp.workspace_id, cp.persona, cp.tier, cp.data_mode,
                                    flow_class=_flow_class(cfg, cp), recapturable=_recapturable(cfg, cp))
                         for cp in planned]
        scope_entries += [ScopeEntry(c.checkpoint.checkpoint_id, c.checkpoint.workspace_id, c.checkpoint.persona,
                                     c.checkpoint.tier, c.checkpoint.data_mode,
                                     flow_class="standard" if c.checkpoint.test_kind == "flow" else None)
                          for c in plan.cuts if c.checkpoint is not None]
        shards = [ShardPlan(shard_of[r], r, scope_count=sum(1 for cp in planned if rig_of[cp.checkpoint_id] == r))
                  for r in used]
        mtext = (cfg.root / "manifests" / f"{scenario.candidate.build_id}.yaml")
        store.begin_run(_run_plan(scenario.run_id, "pr", cfg.root, scope_entries, shards, started,
                                  mtext.read_text() if mtext.exists() else None, scenario.pr, scenario.author))
        rev = reviewer or SimulatedReviewer()
        # -- 5.17 per checkpoint
        evals: list[Evaluated] = []
        for k, cp in enumerate(planned):
            live = cp.data_mode == "masked_live"
            base_cap = run.capture(cp, "base", tick=1 if live else 0)
            cand_cap = run.capture(cp, "candidate", tick=2 if live else 0)
            att = plan.attribution.get(cp.surface_id)
            ticks = iter(range(3, 9))
            ev = evaluate(run, cp, base_cap, cand_cap, x=plan.x_input(cp.surface_id),
                          primitive=att.primitive if att else None, entry_ref=att.entry_id if att else None,
                          recapture=lambda cp=cp: run.capture(cp, "candidate", tick=next(ticks)),
                          recapturable=_recapturable(cfg, cp), store=store)
            evals.append(ev)
        calls = _review_pass(run, evals, rev, scenario.reviewer_cap)
        for f in plan.findings:
            store.write_finding(dataclasses.replace(f, created_at=started))
        for n in run.expiry_notices:
            store.write_finding(Finding(f"fnd-{scenario.run_id}-expiry-{n.record_id}", scenario.run_id, n.kind,
                                        f"{n.kind}: {n.record_id} on {n.region_id} stopped applying at run start",
                                        scenario.run_id, created_at=started))
        for ev in evals:
            o, fs, stored = writer.records(ev)
            steps = []
            if ev.cp.test_kind == "flow":
                steps = [StepOutcome(scenario.run_id, ev.cp.flow_id or "", ev.cp.step_id or "", "passed",
                                     locator_rung="image", duration_ms=ev.settle_ms or 750,
                                     recorded_at=_ts(today, scenario.clock, 600))]
            store.write_checkpoint(o, findings=fs, artifacts=stored, shard_id=shard_of[rig_of[ev.cp.checkpoint_id]],
                                   steps=steps)
        for c in plan.cuts:
            if c.checkpoint is not None:
                store.write_checkpoint(dataclasses.replace(c.outcome(scenario.run_id), recorded_at=started))
        for sid in shards:
            store.end_shard(sid.shard_id, "complete")
            store.mark_shard_imported(sid.shard_id)
        arts.write_manifest(ManifestHeader(config_commit=tree_sha256(cfg.root)[:12], harness_commit="sentinel-core-0.1.0",
                                           run_id=scenario.run_id, created_at=started))
        wall = GATES_S + max(sum(it.rig_cost_s for it in plan.items if r in it.rigs) for r in used) + \
            8 * sum(ev.recaptures for ev in evals) + TEARDOWN_S
        blockers = store.complete_blockers(scenario.run_id)
        status = "complete" if not blockers else "complete_with_env_errors"
        runrec = store.end_run(RunOutcome(scenario.run_id, "pr", status, "reported", started_at=started,
                                          ended_at=_ts(today, scenario.clock, int(wall))))
        result = _report(store, cfg, plan, run, scenario, runrec, manifest, mdoc, out, today, wall, len(used))
        result.reviewer_calls = calls
        result.disabled_invariants = run.disabled
        result.extra_flows = sorted({cp.flow_id for cp in extra if cp.flow_id})
        (out / "reviewer-calls.json").write_text(json.dumps(calls, indent=1, sort_keys=True, default=str) + "\n")
        return result


def _review_pass(run: _Run, evals: list[Evaluated], rev: ReviewerAdapter, cap: int) -> list[dict]:
    """5.11, 9.7: one Reviewer call per report cluster that needs one, in cluster order, up to `cap`."""
    members = []
    by_id: dict[str, Evaluated] = {}
    for ev in evals:
        c = ev.classified
        if c.outcome not in LISTED and c.decision_ref is None:
            continue
        rec = CheckpointOutcome(run_id=run.run_id, checkpoint_id=ev.cp.checkpoint_id, workspace_id=ev.cp.workspace_id,
                                persona=ev.cp.persona, data_mode=ev.cp.data_mode, tier=ev.cp.tier,
                                diff=ev.diff.summary if ev.diff and ev.diff.terminal is None else None, **c.fields())
        f = Finding("pre", run.run_id, "collateral", "", run.run_id, checkpoint_id=ev.cp.checkpoint_id,
                    region_id=ev.region, phash=ev.phash, category=ev.category)
        members.append(Member(rec, f, ev.category, 0, ev.region, c.decision_ref))
        by_id[ev.cp.checkpoint_id] = ev
    calls: list[dict] = []
    for cl in cluster_members(members):
        evs = [by_id[m.checkpoint_id] for m in cl.unsuppressed]
        if not evs:
            continue
        rows = {ev.classified.row for ev in evs}
        if rows & (REVIEWER_ROWS - {"24", "25", "25a"}):
            task = "manifest_match"
        elif all(ev.classified.outcome == "collateral" for ev in evs):
            task = "describe_collateral"
        else:
            continue
        if len(calls) >= cap:
            if task == "manifest_match":
                for ev in evs:
                    _with_review(ev, Review("capped", cause="call_cap"))
            continue
        ordered = sorted(evs, key=lambda e: (_area(e), e.cp.checkpoint_id))
        rep = ordered[0]
        sizes = tuple(f"{dict(e.cp.size_step)['w']}x{dict(e.cp.size_step)['h']}" for e in (ordered[0], ordered[-1])
                      ) if rep.cp.test_kind == "sweep" and len(ordered) > 1 else ()
        tickets = run.cand_app.build.tickets_for(rep.cp.surface_id) - run.base_app.build.tickets_for(rep.cp.surface_id)
        obs = observe(rep.base.screen, rep.cand.screen, tickets) if rep.base else []
        entry = None
        if run.manifest is not None and task == "manifest_match":
            e = run.manifest.entry_declaring(rep.cp.surface_id)
            entry = {"entry_id": e.entry_id, "tickets": list(e.tickets), "summary": e.summary,
                     "expected_visual": e.expected_visual} if e else None
        att = run.plan.attribution.get(rep.cp.surface_id) if run.plan else None
        res = rep.diff
        req = ReviewRequest(
            task=task, checkpoint_ids=tuple(e.cp.checkpoint_id for e in ordered), surface_id=rep.cp.surface_id,
            region_id=rep.region, tier=rep.cp.tier, data_mode=rep.cp.data_mode, workspace_id=rep.cp.workspace_id,
            diff={"changed_px": sum(m.changed_px for m in res.regions) if res else 0,
                  "shift": list(res.summary.shift) if res else [0, 0],
                  "components": [list(c.bbox) for c in (res.summary.components if res else [])][:20]},
            observations=tuple(obs), crop=rep.crop or (0, 0, 1, 1), entry=entry,
            edge={"primitive": att.primitive, "entry_id": att.entry_id} if att and task == "describe_collateral" else None,
            sizes=sizes, ladder=any(d.kind == "order_strip" for d in rep.cand.screen.components))
        answer = rev.review(req)
        problems = validate(answer, req)
        if problems:                      # 3.13: re-sent once, then unavailable (row 23)
            answer = rev.review(req)
            problems = validate(answer, req)
        if problems:
            for ev in evs:
                _with_review(ev, Review("unavailable", cause="reviewer_schema_violation"))
            continue
        review = to_review(answer, rev, rep.region)
        for ev in evs:
            ev.answer = answer
            _with_review(ev, review)
        calls.append({"task": task, "checkpoints": list(req.checkpoint_ids), "request_sha256": req.sha256,
                      "model_id": rev.model_id, "prompt_hash": rev.prompt_hash, "answer": answer})
    return calls


def _area(ev: Evaluated) -> tuple:
    step = dict(ev.cp.size_step or {})
    return (int(step.get("w", 0)) * int(step.get("h", 0)), int(step.get("w", 0)))


def _report(store: Store, cfg: SentinelConfig, plan: ScopePlan, run: _Run, scenario: Scenario, runrec: RunOutcome,
            manifest: Manifest | None, mdoc: dict | None, out: Path, today: date, wall: float, shards: int) -> RunResult:
    """Read the run back from the store and render the PR comment, commit status and report (9.2, 9.5)."""
    outcomes = store.outcomes(scenario.run_id)
    findings = store.findings(scenario.run_id)
    steps = store.step_outcomes(scenario.run_id)
    ids = sorted({o.decision_ref for o in outcomes if o.decision_ref} | {f.decision_ref for f in findings if f.decision_ref})
    decisions = [d for d in (store.get_decision(i) for i in ids) if d is not None]
    coverage = coverage_summary(cfg, plan, outcomes, scenario.run_id, manifest)
    n_calls = 0
    health = {
        "wall_clock": _mmss(wall), "budget": "45:00", "shards": shards,
        "fingerprint": {"candidate": run.fp_id[7:13], "base": run.fp_id[7:13]},
        "reviewer": {"cost_usd": 0.0 * n_calls},
        "safety": {"attestation": "passed (simulated rig)", "paper_mode_asserted": shards, "gateway_drops": 0,
                   "cancel_all": "not needed", "proof": "PROVEN: no orders sent (simulated rig, nothing to send)"},
        "integrations": {"pr_comment": {"state": "posted", "time": "(simulated: written to pr-comment.md)"},
                         "jira_queued": 0, "slack": {"queued": 0}},
    }
    if scenario.canary:
        health["canary"] = dict(scenario.canary)
    ctx = ReportContext(pr=scenario.pr or 0, candidate_build=scenario.candidate.build_id,
                        base_build=scenario.base.build_id, detector_banner=scenario.detector_banner,
                        history=scenario.history,
                        provenance={"harness_commit": "sentinel-core-0.1.0 (simulated run)",
                                    "config_commit": tree_sha256(cfg.root)[:12],
                                    "vm_image_id": "none: synthetic screens from sentinel.synth"})
    mdict = as_dict(mdoc) if mdoc else None
    comment = render_pr_comment(runrec, outcomes, findings, decisions, coverage, health, mdict, ctx, steps=steps)
    (out / "pr-comment.md").write_text(comment, encoding="utf-8")
    rep = build_report(runrec, outcomes, findings, decisions, coverage, health, mdict, ctx, steps)
    status = _status_json(rep.status)
    (out / "commit-status.json").write_text(json.dumps(status, indent=2) + "\n")
    render_run_report(runrec, outcomes, findings, decisions, coverage, health, mdict, ctx, out / "report.html",
                      steps=steps, objects_dir=out / "artifacts" / "objects", embed=True)
    return RunResult(scenario.run_id, "pr", runrec.status, runrec.reason, out, outcomes, findings, decisions, comment,
                     status, plan, mdict)


# ------------------------------------------------------------------------------------------------
# Decisions (4.14, 9.7): what a human clicks in the report UI


def record_decisions(store: Store, result: RunResult, select: Callable[[CheckpointOutcome], bool], *, action: str,
                     decided_by: str, reason: str, ticket: str | None = None, confirmed_by: str | None = None,
                     proposed_by: str | None = None,
                     entry: str | None = None, cluster_id: str | None = None, at: str | None = None) -> list[Decision]:
    """Write one decision per member signature of the selected checkpoints (9.7: a cluster decision is
    written per member signature with the cluster id). Returns the decisions as stored."""
    from ..records import ACTION_VERDICT
    primary = {f.checkpoint_id: f for f in result.findings if f.finding_id.startswith("F-")}
    out = []
    for o in sorted(result.outcomes, key=lambda o: o.checkpoint_id):
        f = primary.get(o.checkpoint_id)
        if not select(o) or f is None or not f.phash or not f.region_id:
            continue
        nxt = store.conn.execute("SELECT COALESCE(MAX(decision_id), 0) + 1 FROM decisions").fetchone()[0]
        d = Decision(f"dec-{max(nxt, 900):06d}", action, ACTION_VERDICT[action],
                     Signature(o.checkpoint_id, f.region_id, f.phash, f.category or f.kind), decided_by=decided_by,
                     ticket=ticket, manifest_entry_ref=entry, reason=reason, run_id=result.run_id,
                     source="pr_comment", decided_at=at or o.recorded_at)
        out.append(store.record_decision(d, run_id=result.run_id, finding_id=f.finding_id, confirmed_by=confirmed_by,
                                         proposed_by=proposed_by, cluster_id=cluster_id))
    return out


# ------------------------------------------------------------------------------------------------
# The Nightly canary pass (10.3)


@dataclass(frozen=True)
class NightlyScenario:
    """One Nightly run's canary pass on the main build."""

    name: str
    run_id: str
    build: SimBuild
    overlay: Mapping[str, str] = field(default_factory=dict)
    mutants: tuple[str, ...] = ()                     # catalog ids simulated (the rest listed as not simulated)
    clock: str = "03:12:00"
    history_nights: int = 14


SIMULATED_MUTANTS = ("M01", "M02", "M03", "M04", "M05", "M06", "M08", "M09", "M12", "M14", "M18", "M20", "M22",
                     "M24", "M25", "M26", "M27", "M28", "M29", "M30")
NOT_SIMULATED = {
    "M07": "color_swap expects INV-15; the registry declares no signed P/L column and palette for the synthetic app",
    "M10": "clipped_label needs OCR to read the cropped header word; the synthetic OCR reads drawn text, not pixels",
    "M11": "clipped_label (ellipsis) needs OCR of the pixels, as M10",
    "M13": "missing_row expects an Oracle assertion (R08); the core has no Oracle",
    "M15": "placeholder lands on a rule region, asserted by the Oracle, which the core lacks",
    "M16": "font_height needs OCR cap heights from the pixels",
    "M17": "scrollbar needs a template match of the drawn thumb",
    "M19": "redaction is proven by the redaction stage and the egress log, which the simulation has no rig for",
    "M21": "global_shift is judged by the run-level pre-pass over at least 20 pairs of one run",
    "M23": "global_shift, as M21",
    "M31": "misaligned_column expects INV-12; the registry declares no grid columns for the synthetic app",
    "M32": "focus_order needs the Tab focus trace of a keyboard-only flow",
    "M33": "error_dialog needs the popup catalog's error title patterns and the dialog as a window",
}


def planned_for(cfg: SentinelConfig, cid: str) -> PlannedCheckpoint:
    """A PlannedCheckpoint for a checkpoint id of the config (the canary catalog binds ids, not plans)."""
    p = checkpoint_parts(cid)
    kind, surface, q = p["kind"], p["surface"], p["qualifier"].split("/")
    s = cfg.surfaces[surface]
    tier, mode = int(s["tier"]), str(s.get("data_mode", "frozen"))
    if kind == "flow":
        flow = cfg.flow_docs[q[0]]
        step = cfg.flow_step(q[0], q[1])
        return PlannedCheckpoint(cid, surface, "flow", int(step.get("tier", tier)), flow["workspace"], flow["persona"],
                                 str(step.get("data_mode", flow.get("data_mode", mode))), flow_id=q[0], step_id=q[1])
    if kind == "sweep":
        w, h = q[1].split("x")
        return PlannedCheckpoint(cid, surface, "sweep", tier, "ws.tier-c.sweep-harness", s["personas"][0], mode,
                                 size_step=(("host", q[0]), ("w", int(w)), ("h", int(h))))
    ws = next(w for w in cfg.workspaces if w.id == q[0])
    return PlannedCheckpoint(cid, surface, "census", tier, ws.id, ws.persona or s["personas"][0], mode)


def _mutant_model(model: inv.ScreenModel, m: cn.Mutant, boxes: Mapping[str, Sequence[int]]) -> inv.ScreenModel:
    """What the bounds source reads on a mutant (best effort per op, module `sim` docstring): erased
    boxes lose their controls and words; translated boxes move them; a moved window moves."""
    e = m.entry
    if m.defect_class == "window_offscreen":
        x = int(e.params.get("x", -200))
        w = model.windows[0]
        return dataclasses.replace(model, windows=(dataclasses.replace(w, rect=inv.Rect(x, w.rect.y, w.rect.w, w.rect.h)),))
    box = boxes.get(e.region_ref or "")
    if box is None:
        return model
    bx, by, bw, bh = box
    inside = lambda r: bx <= r.center[0] < bx + bw and by <= r.center[1] < by + bh  # noqa: E731
    if e.op == "erase" and m.defect_class in ("erased_control", "landmark_loss"):
        return dataclasses.replace(model, components=tuple(c for c in model.components if c.kind != "control"
                                                           or not inside(c.bbox)),
                                   words=tuple(w for w in model.words if not inside(w.bbox)))
    if e.op == "translate" and m.defect_class in ("shifted_panel", "overlapped_panels", "outside_parent"):
        dx, dy = int(e.params.get("dx", 0)), int(e.params.get("dy", 0))
        if m.defect_class == "outside_parent" and not (dx or dy):
            dy = int(e.params.get("distance", 6))
        src = boxes.get(str(e.params.get("layer"))) if e.params.get("layer") else box
        sx, sy, sw, sh = src
        def moved(r: inv.Rect) -> inv.Rect:
            if sx <= r.center[0] < sx + sw and sy <= r.center[1] < sy + sh:
                return inv.Rect(r.x + dx, r.y + dy, r.w, r.h)
            return r
        comps = []
        for c in model.components:
            r = moved(c.bbox)
            if r == c.bbox and c.kind == "panel" and (c.bbox.x, c.bbox.y, c.bbox.w, c.bbox.h) == tuple(src):
                r = inv.Rect(r.x + dx, r.y + dy, r.w, r.h)
            comps.append(dataclasses.replace(c, bbox=r))
        return dataclasses.replace(model, components=tuple(comps),
                                   words=tuple(dataclasses.replace(w, bbox=moved(w.bbox)) for w in model.words))
    return model


def _static_capture(cid: str, img: np.ndarray, model: inv.ScreenModel, template: Capture) -> Capture:
    from ..diff.settle import Frame
    import copy
    screen = copy.copy(template.screen)
    screen.image = img
    frames = [Frame(k * 250, img, texts=template.frames[0].texts) for k in range(3)]
    return Capture(cid, "candidate", frames, screen, model, template.info)


def run_nightly(config_dir: str | Path, scenario: NightlyScenario, out_dir: str | Path, today: date = DEFAULT_TODAY,
                history: Mapping[str, Sequence[float]] | None = None) -> RunResult:
    """The canary pass of one Nightly run (module docstring). Writes `canary-results.json`,
    `morning-report.txt`, the run's rows in `sentinel.db` and its artifacts."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = _ts(today, scenario.clock)
    now = parse_time(started)
    cfg_root = materialize(config_dir, scenario.overlay, out / "config" / scenario.run_id)
    lrep = lint(cfg_root, phase=1, today=today)
    with Store(out / "sentinel.db") as store:
        if lrep.errors:
            sc = Scenario(scenario.name, scenario.run_id, scenario.build, scenario.build, run_type="nightly",
                          clock=scenario.clock)
            return _abort_config(store, sc, cfg_root, [v.line() for v in lrep.errors],
                                 sorted({v.rule for v in lrep.errors}), out, today, started)
        cfg = SentinelConfig.load(cfg_root)
        assert cfg.catalog is not None
        wanted = set(scenario.mutants or SIMULATED_MUTANTS)
        entries = [e for e in cfg.catalog.entries if e.mutant_id in wanted]
        run = _Run(cfg, scenario.run_id, "nightly", scenario.build, scenario.build, now, None, None,
                   lambda s: "undeclared")
        bound = sorted({e.checkpoint_id for e in entries} | {str(e.params.get("donor_checkpoint")) for e in entries
                                                              if e.params.get("donor_checkpoint")})
        cps = {cid: planned_for(cfg, cid) for cid in bound}
        run.budget = RerunBudget(len(cps), run.params)
        run.calibrate(list(cps.values()), out)
        approved: dict[str, Capture] = {cid: run.capture(cp, "base") for cid, cp in cps.items()}
        boxes: dict[str, dict[str, tuple[int, int, int, int]]] = {}
        for cid, cp in cps.items():
            reg = run.registry(cp, approved[cid])
            lm = {l.id: l.expected for l in reg.landmarks}
            boxes[cid] = {r.id: (r.rect.x + lm[r.landmark][0], r.rect.y + lm[r.landmark][1], r.rect.w, r.rect.h)
                          for r in reg.regions if lm.get(r.landmark) is not None}
        gen = cn.generate(entries, {cid: c.screen.image for cid, c in approved.items()}, boxes)
        results: list[cn.MutantResult] = []
        produced: dict[str, CheckpointOutcome] = {}
        writer = _Writer(run, ArtifactStore(out / "artifacts", f"runs/{scenario.run_id}"), today, scenario.clock)
        rows_meta = []
        arts_by_sha: dict[str, StoredArtifact] = {}
        for m in gen.mutants:
            cp = cps[m.checkpoint_id]
            if m.failed or m.image is None:
                results.append(cn.MutantResult(m))
                continue
            base_cap = approved[m.checkpoint_id]
            model = _mutant_model(base_cap.model, m, boxes[m.checkpoint_id])
            image = m.image
            if m.defect_class == "window_offscreen":
                image = base_cap.screen.image    # Capture grabs the window by its DWM bounds (5.7): same pixels
            mutant_cap = _static_capture(m.checkpoint_id, image, model, base_cap)
            ev = evaluate(run, cp, base_cap, mutant_cap, x="undeclared", primitive=None, entry_ref=None,
                          recapture=lambda c=mutant_cap: c, recapturable=_recapturable(cfg, cp), synthetic=True,
                          injected=True)
            rec, _, stored = writer.records(ev, synthetic=True, tag=m.mutant_id)
            arts_by_sha.update({a.sha256: a for a in stored})
            step_status = None
            if m.defect_class == "gateway":
                step_status = "blocked_by_gateway"       # the simulated gateway refuses the unmatched Send template
            absorbed = None
            if ev.classified.outcome in ("identical", "noise"):
                masks = [f"mask {r.mask_ref} (region {r.id})" for r in ev.registry.regions if r.type == "mask"]
                absorbed = ", ".join(masks) if masks else None
            results.append(cn.MutantResult(m, rec, absorbed_by=absorbed, step_status=step_status))
            produced[m.key] = rec
            rows_meta.append((m, ev))
        report = cn.score(results, history=history or {}, since=f"{today.isoformat()} {scenario.clock[:5]} ET",
                          context=cn.RowContext(scenario.run_id, "ws.tier-a.swing-trader", "swing_trader", "frozen"))
        # -- store: identity, the run, synthetic rows (one per checkpoint at most, run docstring)
        _register(store, cfg, [], [scenario.build.build_id], today)
        store.register_checkpoints([ScopeEntry(cp.checkpoint_id, cp.workspace_id, cp.persona, cp.tier, cp.data_mode,
                                               flow_class=_flow_class(cfg, cp)) for cp in cps.values()],
                                   config_commit=tree_sha256(cfg.root)[:12])
        store.begin_run(_run_plan(scenario.run_id, "nightly", cfg.root, (), (), started, None, None, None))
        written: set[str] = set()
        for row in report.outcome_rows:
            if row.checkpoint_id in written:
                continue
            written.add(row.checkpoint_id)
            named = [arts_by_sha[h] for h in (row.artifacts or {}).values() if isinstance(h, str) and h in arts_by_sha]
            store.write_checkpoint(dataclasses.replace(row, recorded_at=_ts(today, scenario.clock, 1800)), artifacts=named)
        for r in report.results:
            if r.state in ("detector_blind", "detector_degraded", "generation_failed"):
                store.write_finding(Finding(
                    f"fnd-{scenario.run_id}-canary-{r.mutant_id}", scenario.run_id, "canary_miss",
                    f"{r.mutant_id} {r.defect_class} {r.state}: produced {r.produced_outcome or 'nothing'}"
                    f"{' (' + r.produced_reason + ')' if r.produced_reason else ''}; absorbed by "
                    f"{r.absorbed_by or 'not recorded'}", scenario.run_id, checkpoint_id=r.checkpoint_id, tier=1,
                    evidence={"raw_values": {"state": r.state, "money_cell": str(r.money_cell)}},
                    created_at=_ts(today, scenario.clock, 1800)))
        runrec = store.end_run(RunOutcome(scenario.run_id, "nightly", "complete", "reported", started_at=started,
                                          ended_at=_ts(today, scenario.clock, 2 * 3600)))
        money = [r for r in report.results if r.money_cell and r.state != "generation_failed"]
        other = [r for r in report.results if not r.money_cell and r.state != "generation_failed"]
        canary_health = {"money_detected": sum(r.state == "detected" for r in money), "money_total": len(money),
                         "other_detected": sum(r.state == "detected" for r in other), "other_total": len(other),
                         "detector_state": report.state, "date": today.isoformat(),
                         "since": f"{today.isoformat()} {scenario.clock[:5]} ET"}
        body = {"run_id": scenario.run_id, "detector_state": report.state, "banner": report.banner,
                "catalog_version": cfg.catalog.version, "simulated": sorted(wanted),
                "not_simulated": {k: v for k, v in NOT_SIMULATED.items() if k not in wanted},
                "results": [r.to_dict() for r in report.results],
                "class_rates": report.class_rates, "class_floors": report.class_floors}
        (out / f"canary-results-{scenario.run_id}.json").write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
        writer.arts.put(json.dumps(body, sort_keys=True).encode(), name=f"{scenario.run_id}/canary-results.json",
                        kind="canary_results", media_type="application/json")
        writer.arts.write_manifest(ManifestHeader(config_commit=tree_sha256(cfg.root)[:12],
                                                  harness_commit="sentinel-core-0.1.0", run_id=scenario.run_id,
                                                  created_at=started))
        ctx = ReportContext(pr=0, candidate_build=scenario.build.build_id, base_build=scenario.build.build_id,
                            detector_banner=report.banner)
        health = {"wall_clock": "2:41:00", "budget": "4:00:00", "shards": 1, "canary": canary_health,
                  "fingerprint": {"candidate": run.fp_id[7:13], "base": run.fp_id[7:13]},
                  "integrations": {"pr_comment": {"state": "unposted", "time": "n/a (Nightly run)"}, "jira_queued":
                                   1 if report.state != "ok" else 0, "slack": {"queued": 0}}}
        outcomes = store.outcomes(scenario.run_id)
        render_run_report(runrec, outcomes, store.findings(scenario.run_id), [], {}, health, None, ctx,
                          out / f"nightly-report-{scenario.run_id}.html")
        res = RunResult(scenario.run_id, "nightly", runrec.status, runrec.reason, out, outcomes,
                        store.findings(scenario.run_id))
        res.canary = {"report": report, "health": canary_health, "produced": produced, "evals": rows_meta,
                      "simulated": sorted(wanted)}
        return res
