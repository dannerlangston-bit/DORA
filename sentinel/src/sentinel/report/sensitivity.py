"""The effective-sensitivity report, the seven-day mask warning and the mask budget notice (3.8, 3.9).

3.8: "The Reporter prints effective sensitivity per checkpoint: percent `diff` plus `freeze`, percent `rule`, percent
`mask` plus redaction, budget use per panel, and the week-over-week change"; "a mask stops applying at `expires_at`;
the Reporter warns the owner seven days before"; a run-time budget breach is "`harness_error (reason:
mask_budget_exceeded)` with the panel named in `detail`, posted to on-call and the mask owner". 3.9: "A region with a
`floor_exception` ... appears in the sensitivity report".

- `measure(regfile, located, redactions, width, height, budget)` is one checkpoint's row, taken at diff time from the
  located candidate regions, the redaction boxes and the per-capture budget result (`diff.regions.sensitivity`,
  `diff.regions.mask_budget`); the run stores the rows in its summary as `summary["sensitivity"]` {checkpoint id: row}.
- `compute(current, previous, outcomes=, regions=)` is the report: one `CheckpointSensitivity` per checkpoint with
  the change against the run a week earlier (`previous_rows(store, run_id)` reads that run's rows), and every region
  that carries a `floor_exception`.
- `mask_expiry_notices(warnings)` groups `lint.runstart.ExpiryWarning`s (`expiring_soon`) into one notice per mask
  owner; `mask_budget_notices(outcomes, masks)` gives one notice per `mask_budget_exceeded` checkpoint to on-call and
  the owners of that checkpoint's masks. Each `OwnerNotice` names its 9.10 route; sending it is the caller's.

Choices where the spec is ambiguous (reported):

1. Shares are percent of the candidate capture's window, as `diff.regions.sensitivity` computes them (`_rest`
   counts as `diff`). Budget use per panel is the larger of the two sides' shares against the panel's budget.
2. A checkpoint with no diff-time row (the run stored none for it) still prints, from its outcome record's
   `masked_pct` (8.3, the masked plus redaction share), with the other two shares "not measured".
3. "Week-over-week": the newest finished run of the same run type that started at least seven days before this one
   (`previous_rows`); the change is printed in percentage points per share, and a checkpoint that run did not
   measure prints "new".
4. The seven-day warning is a P3 notice per owner (the daily digest of 9.10), listing every mask of the owner that
   expires within the window; the budget breach takes the `harness_error` row of 9.10 (P2 to the on-call channel)
   with the masks' owners added by name, since 9.10 has no mask-owner role. A breach on a checkpoint with no mask
   (a redaction box or the parking rectangle) goes to on-call alone and says so.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..diff.regions import BudgetResult, MaskRecord, Rect, RedactionBox, RegionsFile
from ..diff.regions import sensitivity as region_shares
from ..lint.runstart import WARN_DAYS, ExpiryWarning
from ..orchestrator import alerts
from ..records import CheckpointOutcome

if TYPE_CHECKING:
    from ..store.store import Store

SHARES = ("diff_freeze", "rule", "mask_redaction")
SHARE_SAID = {"diff_freeze": "diff+freeze", "rule": "rule", "mask_redaction": "mask+redaction"}


def measure(regfile: RegionsFile, located: Mapping[str, Rect], redactions: Sequence[RedactionBox], width: int,
            height: int, budget: BudgetResult | None = None) -> dict[str, Any]:
    """One checkpoint's sensitivity row at diff time (module docstring): the three shares in percent and the budget
    use of each panel, for `summary["sensitivity"][checkpoint_id]`."""
    shares = region_shares(regfile, located, redactions, width, height)
    row: dict[str, Any] = {k: round(100.0 * shares[k], 3) for k in SHARES}
    row["panels"] = [{"panel": p.panel, "side": p.side, "share": round(100.0 * p.share, 3),
                      "budget": round(100.0 * p.budget, 3)} for p in (budget.panels if budget else ())]
    return row


@dataclass(frozen=True)
class PanelUse:
    """Budget use of one critical panel: the larger side's masked plus non-identity redacted share (choice 1)."""

    panel: str
    share: float
    budget: float

    @property
    def exceeded(self) -> bool:
        return self.share > self.budget

    def text(self) -> str:
        return f"{self.panel} {self.share:.1f} of {self.budget:.0f} percent" + (" (over budget)" if self.exceeded else "")


@dataclass(frozen=True)
class CheckpointSensitivity:
    """One checkpoint's effective sensitivity (3.8) and its change since the week-earlier run (choice 3)."""

    checkpoint_id: str
    tier: int | None
    shares: Mapping[str, float | None]                 # diff_freeze, rule, mask_redaction in percent; None unmeasured
    panels: tuple[PanelUse, ...] = ()
    change: Mapping[str, float] | None = None          # percentage points per share; None: not measured a week ago

    def shares_text(self) -> tuple[str, str, str]:
        return tuple("not measured" if self.shares.get(k) is None else f"{self.shares[k]:.1f}" for k in SHARES)

    def change_text(self, has_previous: bool) -> str:
        if not has_previous:
            return "no run a week earlier"
        if self.change is None:
            return "new"
        return ", ".join(f"{SHARE_SAID[k]} {v:+.1f}" for k, v in self.change.items())


@dataclass(frozen=True)
class FloorExceptionRow:
    """A region with a `floor_exception` (3.9): it has a derived non-zero threshold and is listed in the report."""

    checkpoint_id: str
    region_id: str
    approved_by: str
    reason: str
    ticket: str
    expires_at: str


@dataclass(frozen=True)
class SensitivityReport:
    """The effective-sensitivity section (module docstring)."""

    rows: tuple[CheckpointSensitivity, ...]
    floor_exceptions: tuple[FloorExceptionRow, ...] = ()
    previous_run_id: str | None = None

    @property
    def over_budget(self) -> tuple[str, ...]:
        return tuple(r.checkpoint_id for r in self.rows if any(p.exceeded for p in r.panels))


def _panels(row: Mapping[str, Any]) -> tuple[PanelUse, ...]:
    best: dict[str, PanelUse] = {}
    for p in row.get("panels") or ():
        use = PanelUse(str(p["panel"]), float(p["share"]), float(p["budget"]))
        if use.panel not in best or use.share > best[use.panel].share:
            best[use.panel] = use
    return tuple(best[k] for k in sorted(best))


def floor_exceptions(regions: Mapping[str, Mapping[str, Any]]) -> tuple[FloorExceptionRow, ...]:
    """Every region of the regions files that carries a `floor_exception` (4.8, 3.9), by checkpoint and region."""
    out = []
    for cid, doc in sorted(regions.items()):
        for r in doc.get("regions") or ():
            fe = r.get("floor_exception")
            if fe:
                out.append(FloorExceptionRow(cid, str(r.get("id")), str(fe.get("approved_by", "")),
                                             str(fe.get("reason", "")), str(fe.get("ticket", "")),
                                             str(fe.get("expires_at", ""))))
    return tuple(out)


def compute(current: Mapping[str, Mapping[str, Any]], previous: Mapping[str, Mapping[str, Any]] | None = None, *,
            outcomes: Sequence[CheckpointOutcome] = (), regions: Mapping[str, Mapping[str, Any]] | None = None,
            previous_run_id: str | None = None) -> SensitivityReport:
    """The report (module docstring): `current` the run's `summary["sensitivity"]`, `previous` the week-earlier run's
    (None when there is none), `outcomes` the run's records (tiers, and `masked_pct` for checkpoints without a row,
    choice 2), `regions` the config's regions files by checkpoint (floor exceptions)."""
    tiers = {o.checkpoint_id: o.tier for o in outcomes if not o.synthetic}
    masked = {o.checkpoint_id: o.masked_pct for o in outcomes if not o.synthetic and o.masked_pct is not None}
    rows = []
    for cid in sorted(set(current) | set(masked)):
        row = current.get(cid)
        shares = {k: (float(row[k]) if row.get(k) is not None else None) for k in SHARES} if row is not None else \
            {"diff_freeze": None, "rule": None, "mask_redaction": float(masked[cid])}
        before = (previous or {}).get(cid)
        change = None
        if before is not None:
            change = {k: round(shares[k] - float(before[k]), 3) for k in SHARES
                      if shares.get(k) is not None and before.get(k) is not None}
        rows.append(CheckpointSensitivity(cid, tiers.get(cid), shares, _panels(row or {}), change))
    rows.sort(key=lambda r: (r.tier if r.tier is not None else 9, r.checkpoint_id))
    return SensitivityReport(tuple(rows), floor_exceptions(regions or {}),
                             previous_run_id if previous is not None else None)


def _ts(text: str) -> datetime:
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def previous_rows(store: "Store", run_id: str, *, days: int = 7) -> tuple[str | None, dict[str, Any] | None]:
    """The week-earlier run's id and its `summary["sensitivity"]` rows (choice 3), or (None, None)."""
    me = store.query("SELECT run_type, started_at FROM runs WHERE run_id = ?", (run_id,))
    if not me:
        return None, None
    cutoff = _ts(me[0]["started_at"]) - timedelta(days=days)
    for r in store.query("SELECT run_id, started_at FROM runs WHERE run_type = ? AND lifecycle = 'finished' "
                         "AND run_id <> ? ORDER BY started_at DESC", (me[0]["run_type"], run_id)):
        if _ts(r["started_at"]) <= cutoff:
            return r["run_id"], dict(store.run_summary(r["run_id"]).get("sensitivity") or {})
    return None, None


# -- the notices of 3.8 ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class OwnerNotice:
    """A notice the Reporter sends (3.8): its kind, the mask owners it names, the 9.10 route it takes, the checkpoints
    it is about and its lines."""

    kind: str                          # mask_expiring | mask_budget_exceeded
    owners: tuple[str, ...]
    route: alerts.AlertRoute
    checkpoint_ids: tuple[str, ...]
    lines: tuple[str, ...]


MASK_EXPIRING_ROUTE = alerts.AlertRoute("mask_expiring", alerts.P3)      # the daily digest (choice 4)


def mask_expiry_notices(warnings: Iterable[ExpiryWarning], *, days: int = WARN_DAYS) -> list[OwnerNotice]:
    """One notice per mask owner for the masks that expire within `days` (3.8: "the Reporter warns the owner seven
    days before"), from `lint.runstart.expiring_soon`; a warning past the window is left out."""
    by_owner: dict[str, list[ExpiryWarning]] = {}
    for w in warnings:
        if 0 <= w.days_left <= days:
            by_owner.setdefault(w.owner or "(no owner)", []).append(w)
    return [OwnerNotice("mask_expiring", (owner,), MASK_EXPIRING_ROUTE,
                        tuple(dict.fromkeys(w.checkpoint_id for w in ws)),
                        tuple(w.line() + "; renewal quotes the latest nightly A/A result for the region (3.8)"
                              for w in sorted(ws)))
            for owner, ws in sorted(by_owner.items())]


def mask_budget_notices(outcomes: Iterable[CheckpointOutcome],
                        masks: Mapping[str, MaskRecord] | Iterable[MaskRecord]) -> list[OwnerNotice]:
    """One notice per checkpoint whose capture broke the mask budget (`harness_error (reason: mask_budget_exceeded)`,
    3.8), to on-call and the owners of the checkpoint's masks (choice 4); `masks` are the config's mask records."""
    records = list(masks.values()) if isinstance(masks, Mapping) else list(masks)
    out = []
    for o in outcomes:
        if o.synthetic or o.outcome != "harness_error" or o.reason != "mask_budget_exceeded":
            continue
        owners = tuple(sorted({m.owner for m in records if m.checkpoint_id == o.checkpoint_id}))
        line = f"{o.checkpoint_id}: harness_error (reason: mask_budget_exceeded): {o.detail or 'panel not named'}"
        tail = "; a broken mask configuration is never diffed around (3.8)"
        if not owners:
            tail += "; no mask on this checkpoint, so on-call alone is told"
        out.append(OwnerNotice("mask_budget_exceeded", owners, alerts.harness_error(), (o.checkpoint_id,),
                               (line + tail,)))
    return out
