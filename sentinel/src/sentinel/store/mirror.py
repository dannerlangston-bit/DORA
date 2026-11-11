"""The config mirror of the Central Store (8.3, 8.5): regions, masks and renames at the run's config commit, and the
request_mask path that reads them (4.14 L-DEC-04, 8.3, 8.7, 9.6).

8.5: "The `regions` and `masks` tables are loaded from the same commit" as the run. They are what
`trg_decision_money_cell` (L-DEC-04) and the calibrated tolerance clamp of `Store.record_decision` read, and how
the Reporter shows mask growth by commit.

- `load_config_mirror(store, commit, registry, masks, redaction)` writes one `regions` row per region of every
  regions file (`registry`: checkpoint id -> 4.8 regions document), the redaction regions of
  redaction/anchors.yaml (`redaction`) under every checkpoint that declares their landmark, and one `masks` row per
  mask record (`masks`: mask id -> `diff.regions.MaskRecord` or the 4.8 record mapping), placed by its mask region.
  A mask record whose `decision_ref` names a request_mask decision links it (`link_mask`). Loading a commit again
  writes only rows it lacks.
- `link_mask(store, decision_id, mask_id)` is the merge of a request_mask's mask PR (9.6 Approve mask): `mask_ref`
  set once, which makes the decision act (8.3 trg_decision_mask_ref), and its finding becomes noise.
- `request_mask_check(store, finding_id, registry=, redaction=)` is the Reporter API's refusal before any row or
  mask PR is written (L-DEC-04): a money cell (the finding's region, or any money cell the prefilled mask touches),
  or a prefilled mask that takes its panel's masked plus non-identity redacted share over the budget, computed
  with the lint's `panel_share` (one geometry for lint and API). `MaskBudgetExceeded` carries the current share,
  the budget and the mask's share for the UI. `record_request_mask` checks, then writes the decision.
- `apply_renames(store, renames, commit)` applies flows/RENAMES.yaml (`Store.rename_checkpoint`,
  `Store.retire_checkpoint`); `approved_baseline` and `flake_stats_for` follow the rename chain, as the decision
  lookup does (8.3: a rename carries baselines, decisions and flake statistics).

Choices where the spec is ambiguous (reported to the coordinator):
- A region's `regions.hamming_tolerance` is the smallest tolerance of its current calibration floors (any
  rendering key; lowered, never raised, 9.7) and `exact_fallback` is set when any current floor falls back; NULL
  without a floor. Rows already loaded for a commit are kept as first loaded (a later calibration applies from the
  next commit).
- The redaction list is global (SD-31): its regions are mirrored under each checkpoint whose regions file declares
  the landmark, so the per-checkpoint mask budget sees them. A checkpoint region with the same id wins.
- A `money_cell: true` region without a `money_role` cannot be held by `regions` (8.3 CHECK, SD-28) and is refused
  with L-RGN-08 rather than mirrored as an ordinary region; a `money_role` alone makes the region a money cell.
- `masks.created_by` is the requester of the linked request_mask decision when the store has it, else the record's
  `created_by`; `critical_panel` is the mask region's panel; `decision_id` is set when the decision exists.
- Linking the same mask again is a no-op (a reloaded commit); `link_mask` refuses a different mask for a linked
  decision, and the mirror load lists such a mask, or one naming a revoked or non-request_mask decision, in
  `unlinked` instead of failing the load. The finding moves to noise at the merge (8.3 action table "open until
  merge, then noise").
- The prefilled mask of a request is the finding region's landmark and rect unless `rect` (`{landmark, rect}`)
  says otherwise; a finding on a grid cell needs `rect`. The request is refused when the mask raises any panel of
  its landmark over budget; `mask_share` is the share the mask adds to that panel.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from ..lint.rules import money_cells_covered, panel_share
from ..records import Decision
from .store import Store, StoreRefused, WriteBatch, decision_int, decision_ref, rename_chain, utc


class MirrorRefused(StoreRefused):
    """A config record the 8.3 mirror tables cannot hold as given; nothing was written."""


class MaskBudgetExceeded(StoreRefused):
    """L-DEC-04: the prefilled mask would take its panel over the mask budget (4.8). The UI shows the three
    numbers; no decision row and no mask PR are written and the finding stays open in Unexpected."""

    def __init__(self, panel: str, landmark: str, current: float, budget: float, mask_share: float):
        self.panel, self.landmark = panel, landmark
        self.current, self.budget, self.mask_share = current, budget, mask_share
        super().__init__(f"L-DEC-04: the mask would take panel {panel} (landmark {landmark}) over its mask budget: "
                         f"current share {current:.3f}, budget {budget:.3f}, the mask adds {mask_share:.3f}; declare "
                         "a rule region instead, or decide by confirm_bug or mark_intended (raising a budget is a "
                         "system-owner pull request)")


@dataclass(frozen=True)
class MirrorResult:
    """What one `load_config_mirror` call wrote (rows it lacked) and how the mask records linked."""
    regions: int = 0
    masks: int = 0
    linked: tuple[str, ...] = ()               # request_mask decisions whose mask_ref this load set
    unlinked: tuple[str, ...] = ()             # mask ids whose decision_ref names no active request_mask free to link


@dataclass(frozen=True)
class MaskCheck:
    """A request_mask that passed L-DEC-04: the panel its mask touches most, its share before, the budget and what
    the mask adds (budget None: a panel class without a budget)."""
    panel: str | None
    landmark: str
    current: float
    budget: float | None
    mask_share: float


@dataclass(frozen=True)
class RenameResult:
    renamed: tuple[str, ...] = ()              # new ids that now carry an old one
    retired: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()              # entries naming ids this store has never seen


# ---------------------------------------------------------------------------------------------------------------
# helpers

def _ts(v: Any) -> str:
    return utc(v.isoformat() if isinstance(v, datetime) else str(v))


def _field(record: Any, name: str) -> Any:
    return record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)


def _region_row(checkpoint_id: str, commit: str, r: Mapping[str, Any], tolerance: tuple[Any, Any]) -> dict:
    role = r.get("money_role")
    if r.get("money_cell") is True and role is None:
        raise MirrorRefused(f"L-RGN-08: region {r.get('id')} of {checkpoint_id} is a money cell without a money_role "
                            "(SD-28); the regions mirror holds money cells by role (8.3)")
    rect = r["rect"]
    return {"checkpoint_id": checkpoint_id, "region_id": str(r["id"]), "config_commit": commit,
            "kind": str(r["type"]), "money_cell": int(role is not None), "money_role": role,
            "identity_field": int(r.get("identity_field") is True), "landmark_id": str(r["landmark"]),
            "dx": int(rect["dx"]), "dy": int(rect["dy"]), "w": int(rect["w"]), "h": int(rect["h"]),
            "hamming_tolerance": tolerance[0], "exact_fallback": int(bool(tolerance[1]))}


def _calibrated(store: Store, checkpoint_id: str, region_id: str) -> tuple[Any, Any]:
    r = store.query("SELECT min(hamming_tolerance), max(exact_fallback) FROM calibration_floors "
                    "WHERE checkpoint_id = ? AND region_id = ? AND superseded_at IS NULL "
                    "AND hamming_tolerance IS NOT NULL", (checkpoint_id, region_id))
    return (r[0][0], r[0][1]) if r else (None, None)


def _exists(store: Store, table: str, key: Mapping[str, Any]) -> bool:
    where = " AND ".join(f"{k} = ?" for k in key)
    return bool(store.query(f"SELECT 1 FROM {table} WHERE {where}", tuple(key.values())))


def _decision_row(store: Store, ref: str | None) -> sqlite3.Row | None:
    if not ref:
        return None
    try:
        did = decision_int(str(ref))
    except StoreRefused:
        return None
    rows = store.query("SELECT * FROM decisions WHERE decision_id = ?", (did,))
    return rows[0] if rows else None


def _link(b: WriteBatch, d: sqlite3.Row, mask_id: str) -> bool:
    """Set `mask_ref` once on an active request_mask decision and move its finding to noise; False when this mask
    is already linked (a reload)."""
    if d["action"] != "request_mask" or d["status"] != "active":
        raise StoreRefused(f"{decision_ref(d['decision_id'])} is a {d['status']} {d['action']}; a mask links to an "
                           "active request_mask decision (4.14, 9.6)")
    if d["mask_ref"] == mask_id:
        return False
    if d["mask_ref"] is not None:
        raise StoreRefused(f"{decision_ref(d['decision_id'])} already acts through mask {d['mask_ref']}; mask_ref is "
                           "set once (8.3)")
    b.update("decisions", {"decision_id": d["decision_id"]}, {"mask_ref": mask_id})
    if d["source_finding_id"] is not None:
        b.update("findings", {"finding_id": d["source_finding_id"]}, {"triage_state": "noise"})
    return True


# ---------------------------------------------------------------------------------------------------------------
# the mirror (8.5)

def load_config_mirror(store: Store, commit: str, registry: Mapping[str, Mapping[str, Any]],
                       masks: Mapping[str, Any] | Iterable[Any] = (), redaction: Mapping[str, Any] | None = None
                       ) -> MirrorResult:
    """Mirror the Region Registry, the redaction list and the mask records of config commit `commit` (8.5), in one
    batch, and link every mask record to its request_mask decision. Rows the commit already has are kept."""
    records = list(masks.values()) if isinstance(masks, Mapping) else list(masks)
    redactions = [r for r in (redaction or {}).get("regions") or () if r.get("type") == "redaction"]
    rows: list[tuple[str, dict]] = []
    placed: dict[tuple[str, str], Mapping[str, Any]] = {}
    for cp, doc in sorted(registry.items()):
        regions = list(doc.get("regions") or ())
        own = {str(r["id"]) for r in regions}
        landmarks = {str(lm["id"]) for lm in doc.get("landmarks") or ()} | {str(r["landmark"]) for r in regions}
        for r in regions + [x for x in redactions if x.get("landmark") in landmarks and str(x["id"]) not in own]:
            placed[(cp, str(r["id"]))] = r
            rows.append(("regions", _region_row(cp, commit, r, _calibrated(store, cp, str(r["id"])))))
    links: list[tuple[sqlite3.Row, str]] = []
    unlinked: list[str] = []
    for m in records:
        mask_id, cp, rid = str(_field(m, "mask_id")), str(_field(m, "checkpoint_id")), str(_field(m, "region_id"))
        region = placed.get((cp, rid))
        if region is None or region.get("type") != "mask":
            raise MirrorRefused(f"mask {mask_id}: {cp} declares no mask region {rid} to place it (4.8, 8.3)")
        d = _decision_row(store, _field(m, "decision_ref"))
        rect = region["rect"]
        rows.append(("masks", {
            "mask_id": mask_id, "config_commit": commit, "checkpoint_id": cp, "landmark_id": str(region["landmark"]),
            "dx": int(rect["dx"]), "dy": int(rect["dy"]), "w": int(rect["w"]), "h": int(rect["h"]),
            "owner": str(_field(m, "owner")), "reason": str(_field(m, "reason")),
            "created_by": d["decided_by"] if d is not None else str(_field(m, "created_by")),
            "created_by_kind": "human", "created_at": _ts(_field(m, "created_at")),
            "expires_at": _ts(_field(m, "expires_at")), "critical_panel": region.get("panel"),
            "decision_id": None if d is None else d["decision_id"]}))
        if (d is not None and d["action"] == "request_mask" and d["status"] == "active"
                and d["mask_ref"] in (None, mask_id)):
            links.append((d, mask_id))
        else:
            unlinked.append(mask_id)
    keys = {"regions": ("checkpoint_id", "region_id", "config_commit"), "masks": ("mask_id", "config_commit")}
    fresh = [(t, v) for t, v in rows if not _exists(store, t, {k: v[k] for k in keys[t]})]
    linked: list[str] = []
    with store.batch("config_mirror") as b:
        for table, values in fresh:
            b.insert(table, values)
        for d, mask_id in links:
            if _link(b, d, mask_id):
                linked.append(decision_ref(d["decision_id"]))
    return MirrorResult(sum(t == "regions" for t, _ in fresh), sum(t == "masks" for t, _ in fresh), tuple(linked),
                        tuple(unlinked))


def link_mask(store: Store, decision_id: str, mask_id: str) -> Decision:
    """The request_mask's mask record merged (9.6 Approve mask): `mask_ref` is set once and the decision acts; its
    finding becomes noise (8.3). The same mask again is a no-op; another mask is refused."""
    d = _decision_row(store, decision_id)
    if d is None:
        raise StoreRefused(f"unknown decision {decision_id}")
    if not mask_id:
        raise StoreRefused("a mask id is non-empty")
    with store.batch("decision_mask_link") as b:
        _link(b, d, mask_id)
    return store.get_decision(decision_id)


# ---------------------------------------------------------------------------------------------------------------
# the request_mask refusal (L-DEC-04)

def request_mask_check(store: Store, finding_id: str, *, registry: Mapping[str, Mapping[str, Any]],
                       redaction: Mapping[str, Any] | None = None, rect: Mapping[str, Any] | None = None,
                       budgets: Mapping[str, float] | None = None) -> MaskCheck:
    """Refuse, before any row is written, a request_mask on a money cell or whose prefilled mask would take its
    panel over the mask budget (L-DEC-04, 4.8, 9.6). `registry` maps checkpoint ids to regions documents (the run's
    config), `redaction` is redaction/anchors.yaml, `rect` an explicit `{landmark, rect}` mask and `budgets` the
    panel budgets (default: the Diff Engine parameters, as lint)."""
    f = store.get_finding(finding_id)
    if f is None:
        raise StoreRefused(f"unknown finding {finding_id}")
    if not f.checkpoint_id or not f.region_id:
        raise StoreRefused(f"finding {finding_id} has no signature region to mask (9.7)")
    money = store.query("SELECT 1 FROM regions g JOIN runs r ON r.config_commit = g.config_commit WHERE r.run_id = ? "
                        "AND g.checkpoint_id = ? AND g.region_id = ? AND g.money_cell = 1",
                        (f.run_id, f.checkpoint_id, f.region_id))
    doc = registry.get(f.checkpoint_id)
    if doc is None:
        raise StoreRefused(f"{f.checkpoint_id} has no regions file to place a mask in (4.8)")
    regions = [dict(r) for r in doc.get("regions") or ()]
    own = next((r for r in regions if r.get("id") == f.region_id), None)
    if money or (own is not None and (own.get("money_cell") is True or own.get("money_role") is not None)):
        raise StoreRefused(f"L-DEC-04: {f.region_id} of {f.checkpoint_id} is a money cell; no request_mask on a money "
                           "cell (decide it by confirm_bug or mark_intended)")
    if rect is None:
        if own is None:
            raise StoreRefused(f"{f.region_id} is not a registered region of {f.checkpoint_id}; a request_mask on a "
                               "grid cell names its mask rect")
        rect = {"landmark": own["landmark"], "rect": own["rect"]}
    proposed = {"landmark": str(rect["landmark"]), "rect": dict(rect["rect"])}
    landmarks = {str(r.get("landmark")) for r in regions}
    cover = [dict(r) for r in (redaction or {}).get("regions") or ()
             if r.get("type") == "redaction" and str(r.get("landmark")) in landmarks]
    touched = sorted({cell for cell, by in money_cells_covered(regions, cover, proposed) if by == "proposed mask"})
    if touched:
        raise StoreRefused(f"L-DEC-04: the mask would cover money cell {', '.join(touched)} of {f.checkpoint_id}; a "
                           "money cell's mask budget is 0 percent")
    before = {(p.panel, p.landmark): p for p in panel_share(regions, cover, None, budgets)}
    after = [p for p in panel_share(regions, cover, proposed, budgets) if p.landmark == proposed["landmark"]]
    best: MaskCheck | None = None
    for p in after:
        current = before[(p.panel, p.landmark)].share
        added = p.share - current
        if p.exceeded and added > 0:
            raise MaskBudgetExceeded(p.panel, p.landmark, current, p.budget, added)
        if best is None or added > best.mask_share:
            best = MaskCheck(p.panel, p.landmark, current, p.budget, added)
    return best or MaskCheck(None, proposed["landmark"], 0.0, None, 0.0)


def record_request_mask(store: Store, decision: Decision, *, run_id: str, finding_id: str,
                        registry: Mapping[str, Mapping[str, Any]], redaction: Mapping[str, Any] | None = None,
                        rect: Mapping[str, Any] | None = None, budgets: Mapping[str, float] | None = None,
                        **kwargs: Any) -> Decision:
    """`request_mask_check`, then `Store.record_decision` (the Reporter API order of 9.6: the refusal comes before
    any row or mask PR)."""
    if decision.action != "request_mask":
        raise StoreRefused(f"record_request_mask writes request_mask decisions, not {decision.action}")
    request_mask_check(store, finding_id, registry=registry, redaction=redaction, rect=rect, budgets=budgets)
    return store.record_decision(decision, run_id=run_id, finding_id=finding_id, **kwargs)


# ---------------------------------------------------------------------------------------------------------------
# renames (8.3, gap G33)

def apply_renames(store: Store, renames: Mapping[str, Any] | None, config_commit: str) -> RenameResult:
    """Apply flows/RENAMES.yaml (`{renames: [{old, new, build}]}`, `new: retired` for a retirement)."""
    renamed, retired, unknown = [], [], []
    for e in (renames or {}).get("renames") or ():
        old, new, build = str(e["old"]), str(e["new"]), str(e["build"])
        if new == "retired":
            (retired if store.retire_checkpoint(old, build_id=build, config_commit=config_commit) else unknown
             ).append(old)
        elif store.rename_checkpoint(old, new, build_id=build, config_commit=config_commit):
            renamed.append(new)
        else:
            unknown.append(old)
    return RenameResult(tuple(renamed), tuple(retired), tuple(unknown))


def approved_baseline(store: Store, checkpoint_id: str, lineage: str = "released") -> sqlite3.Row | None:
    """The approved `baselines` row of a checkpoint and lineage, the checkpoint's own first, then along its rename
    chain (8.3: a rename carries baselines to the new id)."""
    for cid in rename_chain(store.conn, checkpoint_id):
        rows = store.query("SELECT * FROM baselines WHERE checkpoint_id = ? AND lineage = ? AND state = 'approved'",
                           (cid, lineage))
        if rows:
            return rows[0]
    return None


def flake_stats_for(store: Store, checkpoint_id: str) -> sqlite3.Row | None:
    """The `flake_stats` row of a checkpoint, the checkpoint's own first, then along its rename chain (8.3)."""
    for cid in rename_chain(store.conn, checkpoint_id):
        rows = store.query("SELECT * FROM flake_stats WHERE checkpoint_id = ?", (cid,))
        if rows:
            return rows[0]
    return None
