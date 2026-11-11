"""The Nightly A/A drift pass (3.9 step 7, 10.6, 2.7 `invariant_on_aa`, 7.13, SD-25).

Every Nightly run captures the selected checkpoints (all tier 1 plus a rotating 20 percent of the rest, chosen by
`scope.plan_nightly`) twice in one session on one rig. Each checkpoint gives one pair, pass 1 against pass 2, diffed
per region against the Calibration floors (10.6 is authoritative for the shape, SD-25). This module runs that pass
over checkpoints the caller has already selected and returns the outcome rows; it writes nothing itself except
through `invalidate_floors`.

- `run(items, capture_pair, run_id=..., calibration=..., watchdog_event=...)`: one `PassPair` per checkpoint from
  `capture_pair` (two captures in one session), diffed by `engine` (default `diff.engine.diff_pair`), judged by
  `review_queue.nightly_global_shift` (`diff.calibration.aa_drift` plus the Nightly global-shift pre-pass of 3.7),
  with `invariants.invariants_on_aa` over the pair's frames. Returns an `AAPassResult`: the rows, the A/A
  false-positive rate (3.9 step 7), whether thresholds are now invalid, the Calibration request, whether floors
  carry over to a new rendering key, and the morning-report lines.
- `rendering_changes(current, calibrated)`: the rendering-key fields (4.12 Calibration key, SD-69) that differ
  between the current fingerprint's `match` block and the Calibration's; `run` takes them as `rendering_change`.
- `invalidate_floors(store, rendering_key, at=...)`: supersedes the current `calibration_floors` rows of a rendering
  key after `aa_drift_global`, so a later run finds no floor and runs `uncalibrated` until a Calibration run writes
  new ones (10.6 "thresholds are invalid until a Calibration run passes").

The fingerprint is checked first (10.6, 7.13). With no change the pass runs against `calibration`. When the rendering
key differs only in `jre_release` or `render_probe_hash` the pass runs against the previous floors and, if it passes,
`carry_over` is set (the floors carry over to the new key; persisting that is the calibration store's job). Any other
change, or no calibration at all, means no floor exists: every pair is diffed at threshold 0, a pair with any changed
pixel is `needs_human (reason: uncalibrated)` naming the changed fields, and a Calibration run is requested.

Choices where the spec is ambiguous (reported to the coordinator):

1. Drift on masked_live checkpoints (SD-25, 10.6): zero-floor calibrated regions of a masked_live checkpoint are left
   out of the drift test, the global fraction and the false-positive rate; its remaining regions (non-zero floors,
   held under a floor_exception) and an accepted whole-window shift drift exactly as on a frozen checkpoint, so a
   masked_live checkpoint can be `not_settled (reason: aa_drift)` too. SD-25's exclusion only has a meaning if the
   other regions of a masked_live checkpoint are subject to drift. An uncalibrated region has no floor and is never
   excluded.
2. Rows for pairs that do not drift: `identical` when nothing changed and `noise (reason: below_floor)` (classified by
   rule) when pixels changed below the floor or only in excluded regions, the outcomes the A/A control mutant expects
   (12.5 M25). The pair's side is `both` (pass 1 against pass 2).
3. `aa_drift_global` (more than 2 percent of checkpoints above floor, or the Nightly pre-pass) is the outcome of every
   compared pair of the pass, as `review_queue.nightly_global_shift` decides (its choice 3): the thresholds are
   invalid, so no pair is judged against them, and no `aa_drift` row is written that night, so a rendering change
   never feeds the per-checkpoint quarantine streak. The request asks for a full Calibration run (7.13).
4. Precedence on one checkpoint (1.10, 3.11 row order): a capture failure or an engine terminal first, then
   `harness_error (reason: invariant_on_aa)`, then `aa_drift_global`, then `not_settled (reason: aa_drift)`, then
   `needs_human (reason: uncalibrated)`, then `identical` or `noise`.
5. The carry-over "passes" (7.13) when no checkpoint drifted, the global trip did not fire and at least one pair was
   compared against calibrated floors; otherwise the new key stays uncalibrated and a Calibration run is requested.
6. Uncalibrated pairs: a pair with no changed pixel is `identical` even with no floor (3.11 row 10 needs no
   threshold); only a pair with changed pixels is `needs_human (reason: uncalibrated)` (3.9 step 6).
7. `harness_error` rows carry the Watchdog event id the caller's `watchdog_event(checkpoint_id)` returns, as
   `watchdog_event: <id>` in `detail` (4.13 L-OUT-02).
8. The Oracle's rule checks on the pair ("their rule checks still run", 10.6) are the evaluation pipeline's, not this
   pass's; a capture failure the caller hit is passed in as `PassPair.failure`.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from .canonical import CALIBRATION_KEY_FIELDS, CALIBRATION_KEY_HARNESS
from .diff.calibration import CalibrationFile, aa_false_positive_rate
from .diff.engine import DiffContext, DiffResult, diff_pair
from .diff.regions import Rect, RedactionBox, RegionsFile
from .invariants import DEFAULT_CONFIG, AAPair, CheckerConfig, ScreenModel, SurfaceRules, invariants_on_aa
from .records import CheckpointOutcome, Classification, now
from .review_queue import nightly_global_shift
from .store.store import Store, utc
from .taxonomy import load

CARRY_OVER_FIELDS = ("jre_release", "render_probe_hash")     # 7.13: floors carry over across these alone
RENDERING_FIELDS = CALIBRATION_KEY_FIELDS + tuple(f"harness.{k}" for k in CALIBRATION_KEY_HARNESS)
MORNING_INVALID = "thresholds are invalid until a Calibration run passes"   # 10.6 morning-report wording


class AACheckpoint(Protocol):
    """What the pass reads from one selected checkpoint (`scope.PlannedCheckpoint` has these fields)."""

    checkpoint_id: str
    surface_id: str
    test_kind: str
    tier: int
    workspace_id: str
    persona: str
    data_mode: str
    flow_id: str | None
    step_id: str | None


@dataclass(frozen=True)
class PassFailure:
    """A pass that produced no comparable capture: its outcome, validated against the taxonomy (4.13)."""

    outcome: str
    reason: str
    side: str | None = None
    gate: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        load().check_checkpoint(self.outcome, self.reason, self.gate)


@dataclass(frozen=True)
class PassPair:
    """One checkpoint's two captures in one session (10.6), as `capture_pair` returns them.

    `first` and `second` are the redacted RGB images of pass 1 and pass 2 (None with `failure`); `registry` is the
    checkpoint's effective Region Registry entry; `redactions` the union of both captures' boxes and `system_masks`
    the other system exclusions (both as `diff_pair` takes them). `frames` are each pass's settled `ScreenModel`s and
    `rules` the surface's invariant rules, for `invariant_on_aa`; without them the invariant check is skipped.
    `artifacts` are the stored captures (`{"base": pass 1 sha256, "candidate": pass 2 sha256}`), and
    `fingerprint_id`, `data_epoch_id`, `captured_at` and `rig_id` describe the session (SD-48)."""

    first: np.ndarray | None
    second: np.ndarray | None
    registry: RegionsFile | None = None
    redactions: Sequence[RedactionBox] = ()
    system_masks: Sequence[Rect] = ()
    frames: tuple[Sequence[ScreenModel], Sequence[ScreenModel]] | None = None
    rules: SurfaceRules | None = None
    failure: PassFailure | None = None
    artifacts: Mapping[str, str] | None = None
    fingerprint_id: str | None = None
    data_epoch_id: str | None = None
    captured_at: str | None = None
    rig_id: str | None = None

    def __post_init__(self) -> None:
        if self.failure is None and (self.first is None or self.second is None):
            raise ValueError("a pass pair has both captures, or a failure")


@dataclass(frozen=True)
class CalibrationRequest:
    """A Calibration run the pass asks for (10.6, 7.13): why, and the rendering-key fields that changed."""

    reason: str                      # rendering_change | no_calibration | aa_drift_global | carry_over_failed
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class AAPassResult:
    """What one Nightly A/A pass produced (10.6, 3.9 step 7)."""

    rows: tuple[CheckpointOutcome, ...]
    fp_rate: float | None                    # region pairs over floor / region pairs compared; None: none calibrated
    above_floor_fraction: float
    invalidate: bool                         # aa_drift_global: thresholds invalid until a Calibration run passes
    queue_calibration: CalibrationRequest | None
    carry_over: bool                         # floors carry over to the new rendering key (7.13)
    drifted: tuple[str, ...]                 # not_settled (aa_drift): flake stats, owner notified (10.6)
    invariant_on_aa: tuple[str, ...]
    morning: tuple[str, ...]
    diffs: Mapping[str, DiffResult] = field(default_factory=dict)

    def health(self) -> dict[str, Any]:
        """The run-health block of the pass (10.6, 3.9 step 7)."""
        return {"pairs": len(self.rows), "fp_rate": self.fp_rate,
                "above_floor_fraction": round(self.above_floor_fraction, 4), "drifted": list(self.drifted),
                "invariant_on_aa": list(self.invariant_on_aa), "global": self.invalidate,
                "carry_over": self.carry_over,
                "queue_calibration": None if self.queue_calibration is None else
                {"reason": self.queue_calibration.reason, "fields": list(self.queue_calibration.fields)}}


Engine = Callable[..., DiffResult]
_MISSING = object()


def rendering_changes(current: Mapping[str, Any], calibrated: Mapping[str, Any]) -> tuple[str, ...]:
    """The rendering-key fields (4.12, SD-69) whose values differ between two `match` blocks, in key order; a
    harness subfield is named `harness.<subfield>`. A field missing on either side counts as changed."""
    out = []
    for name in RENDERING_FIELDS:
        if name.startswith("harness."):
            sub = name.split(".", 1)[1]
            a, b = (dict(m.get("harness") or {}).get(sub, _MISSING) for m in (current, calibrated))
        else:
            a, b = current.get(name, _MISSING), calibrated.get(name, _MISSING)
        if a is _MISSING or b is _MISSING or a != b:
            out.append(name)
    return tuple(out)


def _checkpoints(items: Iterable[Any]) -> list[AACheckpoint]:
    """Plan items (`scope.PlanItem`, with `.checkpoints`) or checkpoints, flattened in order."""
    out: list[AACheckpoint] = []
    for it in items:
        out.extend(getattr(it, "checkpoints", None) or [it])
    return out


def _zero_floor(m: Any) -> bool:
    return m.threshold_source == "calibrated" and not m.threshold.nonzero_floor


def _drift_view(res: DiffResult, data_mode: str) -> DiffResult:
    """The result the drift rules read: on a masked_live checkpoint, calibrated zero-floor regions left out (SD-25)."""
    if data_mode != "masked_live" or res.terminal is not None or res.signal is None:
        return res
    kept = [m for m in res.regions if not _zero_floor(m)]
    ws = res.window_shift
    above = any(m.above_floor or m.above_threshold for m in kept) or (ws is not None and ws.accepted)
    return dataclasses.replace(res, regions=kept, signal=dataclasses.replace(res.signal, above_floor=above))


def _changed(res: DiffResult) -> bool:
    return res.level not in (None, "zero")


def _region_change(view: DiffResult) -> bool:
    """Any changed pixel or shift left in the drift view (False: every change sat in an excluded region)."""
    ws = view.window_shift
    return any(m.changed_px or m.shift_px for m in view.regions) or (ws is not None and ws.accepted)


def _fp_rate(views: Iterable[DiffResult]) -> float | None:
    """3.9 step 7 over calibrated regions only: an uncalibrated region has no floor to be over."""
    cal = [dataclasses.replace(r, regions=[m for m in r.regions if m.threshold_source == "calibrated"])
           for r in views if r.terminal is None]
    cal = [r for r in cal if r.regions]
    return aa_false_positive_rate(cal) if cal else None


class _Rows:
    """Builds the outcome record of one checkpoint of the pass."""

    def __init__(self, run_id: str, watchdog_event: Callable[[str], str]):
        self.run_id = run_id
        self.watchdog_event = watchdog_event

    def make(self, cp: AACheckpoint, pair: PassPair, outcome: str, reason: str | None, *, side: str | None = None,
             gate: str | None = None, detail: str | None = None, res: DiffResult | None = None) -> CheckpointOutcome:
        if outcome == "harness_error":
            event = f"watchdog_event: {self.watchdog_event(cp.checkpoint_id)}"
            detail = f"{event}; {detail}" if detail else event
        cls = Classification("noise", "rule") if outcome == "noise" else None
        size = getattr(cp, "size_step", None)
        return CheckpointOutcome(
            run_id=self.run_id, checkpoint_id=cp.checkpoint_id, outcome=outcome, reason=reason, detail=detail,
            gate=gate, side=side, workspace_id=cp.workspace_id, persona=cp.persona, data_mode=cp.data_mode,
            surface_id=cp.surface_id, test_kind=cp.test_kind, tier=cp.tier, flow_id=cp.flow_id, step_id=cp.step_id,
            size_step=dict(size) if size else None, diff=res.summary if res is not None and res.terminal is None
            else None, threshold_source=res.threshold_source if res is not None and res.terminal is None else None,
            classification=cls, artifacts=dict(pair.artifacts) if pair.artifacts else None, fingerprint_id=pair.fingerprint_id,
            data_epoch_id=pair.data_epoch_id, captured_at=pair.captured_at, cand_rig_id=pair.rig_id,
            base_rig_id=pair.rig_id, recorded_at=now())


def run(items: Iterable[Any], capture_pair: Callable[[AACheckpoint], PassPair], *, run_id: str,
        calibration: CalibrationFile | None, watchdog_event: Callable[[str], str], engine: Engine | None = None,
        rendering_change: Sequence[str] = (), checker_config: CheckerConfig = DEFAULT_CONFIG,
        params: Mapping[str, Any] | None = None) -> AAPassResult:
    """Run the Nightly A/A pass over the selected checkpoints (module docstring).

    `items` are the A/A selection of `scope.plan_nightly` (plan items or checkpoints); `capture_pair(cp)` captures
    `cp` twice in one session; `calibration` is the file of the Calibration's rendering key (None: none exists);
    `rendering_change` the fields `rendering_changes` found between the current fingerprint and the Calibration's;
    `watchdog_event(checkpoint_id)` the Watchdog event id a harness_error row carries; `engine` diffs one pair with
    `diff_pair`'s signature."""
    engine = engine or diff_pair
    changed = tuple(rendering_change)
    carry = bool(changed) and set(changed) <= set(CARRY_OVER_FIELDS)
    floors = calibration if (not changed or carry) else None
    cps = _checkpoints(items)
    rows = _Rows(run_id, watchdog_event)
    pairs: dict[str, PassPair] = {}
    results: dict[str, DiffResult] = {}
    views: dict[str, DiffResult] = {}
    fired: dict[str, tuple[str, ...]] = {}
    for cp in cps:
        pair = capture_pair(cp)
        pairs[cp.checkpoint_id] = pair
        if pair.failure is not None:
            continue
        res = engine(pair.first, pair.second, pair.registry, calibration=floors, redactions=pair.redactions,
                     system_masks=pair.system_masks,
                     context=DiffContext(run_id=run_id, checkpoint_id=cp.checkpoint_id, tier=cp.tier))
        results[cp.checkpoint_id] = res
        views[cp.checkpoint_id] = _drift_view(res, cp.data_mode)
        if pair.frames is not None and pair.rules is not None:
            fired[cp.checkpoint_id] = invariants_on_aa(
                AAPair(cp.data_mode, tuple(pair.frames[0]), tuple(pair.frames[1])), pair.rules, config=checker_config)
    shift = nightly_global_shift(views, frozen=views.keys(), params=params)
    verdict, glob, under_glob = shift.drift, shift.terminal, set(shift.applies_to)
    out: list[CheckpointOutcome] = []
    for cp in cps:
        cid, pair = cp.checkpoint_id, pairs[cp.checkpoint_id]
        res, view = results.get(cid), views.get(cid)
        if pair.failure is not None:
            f = pair.failure
            out.append(rows.make(cp, pair, f.outcome, f.reason, side=f.side, gate=f.gate, detail=f.detail))
        elif res.terminal is not None:
            t = res.terminal
            out.append(rows.make(cp, pair, t.outcome, t.reason, side=t.side, detail=t.detail, res=res))
        elif fired.get(cid):
            ids = fired[cid]
            out.append(rows.make(cp, pair, "harness_error", "invariant_on_aa", res=res,
                                 detail=f"enabled invariants fired on the A/A pair: {', '.join(ids)}"))
        elif glob is not None and cid in under_glob:
            above = view.signal is not None and view.signal.above_floor and view.threshold_source == "calibrated"
            out.append(rows.make(cp, pair, "harness_error", "aa_drift_global", res=res,
                                 detail=f"{glob.detail}; this checkpoint {'above' if above else 'not above'} floor"))
        elif cid in verdict.drifted:
            t = verdict.drifted[cid]
            out.append(rows.make(cp, pair, "not_settled", "aa_drift", side="both", detail=t.detail, res=res))
        elif _changed(res) and res.threshold_source == "uncalibrated":
            why = (f"rendering key changed: {', '.join(changed)}; Calibration queued" if changed and not carry
                   else "no Calibration floors for this rendering key" if floors is None
                   else "a region with no floor changed")
            out.append(rows.make(cp, pair, "needs_human", "uncalibrated", detail=why, res=res))
        elif not _changed(res):
            out.append(rows.make(cp, pair, "identical", None, res=res))
        else:
            excluded = not _region_change(view)
            out.append(rows.make(cp, pair, "noise", "below_floor", res=res,
                                 detail="changes only in zero-floor regions of a masked_live checkpoint, excluded "
                                        "from drift (SD-25)" if excluded else "A/A change below the floor"))
    drifted = tuple(r.checkpoint_id for r in out if r.reason == "aa_drift")
    invalid = tuple(r.checkpoint_id for r in out if r.reason == "invariant_on_aa")
    compared_calibrated = any(v.threshold_source == "calibrated" for v in views.values() if v.terminal is None)
    carried = carry and floors is not None and glob is None and not drifted and compared_calibrated
    request: CalibrationRequest | None = None
    morning: list[str] = []
    if changed and not carry:
        request = CalibrationRequest("rendering_change", changed)
        morning.append(f"rendering key changed ({', '.join(changed)}): A/A pairs at threshold 0, Calibration queued")
    elif floors is None:
        request = CalibrationRequest("no_calibration")
        morning.append("no Calibration for this rendering key: A/A pairs at threshold 0, Calibration queued")
    if glob is not None:
        request = CalibrationRequest("aa_drift_global", changed)
        morning.append(f"A/A drift global ({glob.detail}): {MORNING_INVALID}")
    elif carry and floors is not None and not carried:
        request = CalibrationRequest("carry_over_failed", changed)
        morning.append(f"floors did not carry over to the new rendering key ({', '.join(changed)}): Calibration "
                       "queued")
    if carried:
        morning.append(f"floors carried over to the new rendering key ({', '.join(changed)})")
    if drifted:
        morning.append(f"A/A drift on {len(drifted)} checkpoint(s): {', '.join(drifted)}")
    fp = _fp_rate(views.values())
    if fp is not None:
        morning.append(f"A/A false-positive rate {100 * fp:.2f}%")
    return AAPassResult(tuple(out), fp, verdict.above_floor_fraction, glob is not None, request, carried, drifted,
                        invalid, tuple(morning), results)


def invalidate_floors(store: Store, rendering_key: str, *, at: str | None = None) -> int:
    """Supersede every current `calibration_floors` row of `rendering_key` (10.6: after `aa_drift_global` the
    thresholds are invalid until a Calibration run passes); returns how many rows it superseded."""
    ids = [r[0] for r in store.query("SELECT floor_id FROM calibration_floors WHERE rendering_key_sha256 = ? "
                                     "AND superseded_at IS NULL", (rendering_key,))]
    ts = utc(at) or now()
    with store.batch("invalidate_floors") as b:
        for floor_id in ids:
            b.update("calibration_floors", {"floor_id": floor_id}, {"superseded_at": ts})
    return len(ids)
