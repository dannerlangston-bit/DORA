"""Quarantine: the 10.5 triggers, record proposals, the `quarantine.yaml` patch, the run-start mirror and the tier-1
quarantine share (10.5, 3.10 step 6, 3.9 step 6, 11.5, 9.11 Quarantine share, SD-96).

A checkpoint enters quarantine on any of (10.5): flake score above 0.10 over the last 5 or more observations of one
build, 3 unstable outcomes in 7 days or 2 verdict flips on one build (reason `flake`); `not_settled (reason:
aa_drift)` on 3 consecutive Nightly runs (`aa_drift`); a fix-list item on a frozen checkpoint open past 5 business
days (`aa_floor`). A flow is quarantined whole when its 30-day agent-fallback rate exceeds 5 percent
(`fallback_rate`, 11.5), and by the safety controls that already took it out of service (`gateway_drops`,
`kill_switch`, `fire_drill`, `safety_canary`, `unknown_popup`: the open `safety_quarantines` rows of W1, decided by
the gateway, the kill switch, the drill, the safety canaries and the popup sentinel; this module does not re-count
their thresholds). Drill and
safety-canary flows (class `canary`, 6.5) are exempt: their triggers are returned as `paged` for the safety routing
of 9.10, never proposed. The record is opened by the Orchestrator as a PR to `sentinel-config` and merged by the
system owner; nothing here un-quarantines anything.

- `evaluate(stats, step_events, fixes, aa_history, today)`: the proposals (one per target, its reason the first of
  `taxonomy.quarantine_reasons` that fired, every trigger in `detail`) and the exempt triggers to page.
- `inputs(store, today, exceptions=)`: the store-side inputs of `evaluate` (step events, open fixes less those a live
  floor exception closes, A/A history, open safety quarantines); the statistics come from `flake.refresh`.
- `render_patch(proposals, current_text)`: the new `quarantine.yaml` content with the proposals added, and the PR
  payload for the outbox (with its `sha256:` id). A proposal without an owner or ticket waits in `pending`.
- `mirror(store, doc, commit=...)`: at run start, the file at the pinned commit mirrored into `flake_stats`
  (`quarantined`, commit, owner, ticket, reason; a flow record covers each of its checkpoints). A record removed
  without exit evidence is listed in `unproven_exits`.
- `exit_evidence(store, checkpoint_id, since)`: the Calibration run that shows the exit rule (5 consecutive clean
  captures with a zero floor) for a checkpoint after `since`, for the PR that removes a record.
- `tier1_quarantine_share(store)`: tier-1 checkpoints quarantined over registered tier-1 checkpoints
  (`coverage.QuarantineShare`, red above 2 percent: run health red and the RC gate blocks).

Choices where the spec is ambiguous (reported to the coordinator):

1. "Last 5 or more observations of the same build" is every observation of that build inside the 30-day window
   (`flake.FlakeStats.by_build`), evaluated per build; the trigger fires above 0.10 with at least 5 of them.
2. "3 consecutive Nightly runs" are the checkpoint's last three non-synthetic Nightly rows (a rotating checkpoint is
   not selected every night); any of the three that is not `not_settled (reason: aa_drift)` breaks the streak.
3. The fix-list trigger follows 3.9 step 6: any open item (no `closed_at`) on a frozen checkpoint, a failed-capture
   item included, quarantines it on the 6th business day after it opened (Monday to Friday, minus the caller's
   holidays); the item's owner is the record's owner. A floor exception closes a region item only while it is in
   force (`inputs(exceptions=)`, `diff.calibration.live_exceptions`): the ticket stored on the row
   (`floor_exception_ref`) does not, so an expired or removed exception lets the item quarantine its checkpoint.
4. The fallback rate of a flow is `passed_via_agent_fallback` step rows over its step outcome rows in the 30 days
   ending `today` (fire-drill rows left out), with no minimum count.
5. Owners: `owners` maps a checkpoint or flow id to its owner (a checkpoint falls back to its flow's owner); with
   none the proposal waits in `pending`. Tickets come from the SENT task the Orchestrator files for the owner; a
   proposal without one waits in `pending` too, so no placeholder ever reaches the file.
6. Comments: the patch keeps the file's leading comment lines; other comments are not kept (the content is dumped).
7. Exit evidence (10.5): a Calibration run started at or after `since` with current floors for the checkpoint, every
   one zero (`diff_pixels_max` 0, no components), at least 10 pairs (the pairs of 5 captures) and no fix item.
8. The tier-1 share counts the mirror (`flake_stats.quarantined`) and the checkpoints of flows with an open safety
   quarantine (class `canary` exempt), over registered tier-1 checkpoints that are neither retired nor renamed away.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import yaml

from .canonical import sha256_id
from .coverage import QuarantineShare
from .flake import WINDOW_DAYS, FlakeStats, window, window_events
from .lint.repo import load_yaml_text
from .records import StepEvent, now
from .store.lifecycle import add_business_days
from .store.safety import SafetyQuarantine, open_quarantines
from .store.store import Store, utc
from .taxonomy import load

FLAKE_SCORE = 0.10                    # 10.5: flake score above 0.10 ...
FLAKE_MIN_OBSERVATIONS = 5            # ... over the last 5 or more observations of the same build
UNSTABLE_7D = 3                       # 3 unstable outcomes in 7 days
FLIPS_ONE_BUILD = 2                   # 2 verdict flips on one build
AA_DRIFT_NIGHTS = 3                   # aa_drift on 3 consecutive Nightly runs (10.6)
FIX_BUSINESS_DAYS = 5                 # a frozen floor not fixed within 5 business days (3.9 step 6)
FALLBACK_RATE = 0.05                  # 30-day agent-fallback rate above 5 percent (11.5)
EXIT_CAPTURES = 5                     # exit: a fix plus 5 consecutive clean captures in a Calibration run (10.5)
EXEMPT_CLASS = "canary"               # drill and safety-canary flows (6.5, store.safety.quarantined_flow)
CONFIG_REPOSITORY = "sentinel-config"
QUARANTINE_FILE = "quarantine.yaml"
TARGET_KINDS = ("checkpoint", "flow")


@dataclass(frozen=True)
class OpenFix:
    """An open fix-list item of 3.9 (8.3 `calibration_fixes`) with its checkpoint's data mode."""

    fix_id: int
    checkpoint_id: str
    data_mode: str
    opened_at: str
    owner: str
    ticket_ref: str | None = None


@dataclass(frozen=True)
class AANight:
    """One Nightly row of a checkpoint (10.6): its run, when the run started and whether it drifted."""

    run_id: str
    started_at: str
    drifted: bool


@dataclass(frozen=True)
class Proposal:
    """One proposed `quarantine.yaml` record (4.16 quarantine_record, SD-96): exactly one target, its reason and
    every trigger that fired. `owner` and `ticket` are filled before the record can be written."""

    kind: str
    target: str
    reason: str
    entered: date
    detail: str
    triggers: tuple[str, ...] = ()
    owner: str | None = None
    ticket: str | None = None
    flow_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in TARGET_KINDS:
            raise ValueError(f"a quarantine record names a checkpoint or a flow, not {self.kind!r} (SD-96)")
        load().check_member("quarantine_reasons", self.reason)

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.target

    def record(self) -> dict[str, Any]:
        """The `quarantine.yaml` record (owner and ticket must be set)."""
        if not (self.owner and self.ticket):
            raise ValueError(f"{self.kind} {self.target}: a record needs its owner and ticket (10.5)")
        return {self.kind: self.target, "owner": self.owner, "ticket": self.ticket,
                "entered": self.entered.isoformat(), "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class Evaluation:
    """`evaluate`'s answer: records to propose, and the exempt triggers to page per 9.10 instead."""

    proposals: tuple[Proposal, ...]
    paged: tuple[Proposal, ...] = ()


@dataclass(frozen=True)
class Inputs:
    """The store-side inputs of `evaluate` (`inputs`)."""

    step_events: tuple[StepEvent, ...]
    fixes: tuple[OpenFix, ...]
    aa_history: Mapping[str, tuple[AANight, ...]]
    safety: tuple[SafetyQuarantine, ...]


@dataclass(frozen=True)
class QuarantinePatch:
    """The `quarantine.yaml` change one Nightly run proposes, and its PR payload for the outbox (9.1)."""

    content: str
    added: tuple[dict[str, Any], ...]
    pending: tuple[tuple[Proposal, tuple[str, ...]], ...]      # each with the fields it still lacks
    skipped: tuple[Proposal, ...]                               # its target already has a record
    payload: Mapping[str, Any] | None = None
    payload_sha256: str | None = None


@dataclass(frozen=True)
class MirrorResult:
    """What `mirror` did: checkpoints now quarantined and released, releases without exit evidence, and records
    naming nothing registered."""

    quarantined: tuple[str, ...]
    released: tuple[str, ...]
    unproven_exits: tuple[str, ...]
    unknown: tuple[str, ...]


# -- triggers ------------------------------------------------------------------------------------------------------
def fix_overdue(opened_at: str, today: date, holidays: Iterable[date] = ()) -> bool:
    """True from the 6th business day after `opened_at` (3.9 step 6: "older than 5 business days")."""
    return today > datetime.fromisoformat(add_business_days(opened_at, FIX_BUSINESS_DAYS, holidays)).date()


def _flake(s: FlakeStats) -> list[str]:
    out = [f"flake score {c.score:.3f} over {c.observations} observations of build {b}"
           for b, c in sorted(s.by_build.items())
           if c.observations >= FLAKE_MIN_OBSERVATIONS and c.score > FLAKE_SCORE]
    if s.unstable_7d >= UNSTABLE_7D:
        out.append(f"{s.unstable_7d} unstable outcomes in 7 days")
    if s.flips_same_build >= FLIPS_ONE_BUILD:
        out.append(f"{s.flips_same_build} verdict flips on one build")
    return out


def _fallback_rates(events: Iterable[StepEvent], today: date) -> dict[str, tuple[int, int]]:
    first, last = window(today, WINDOW_DAYS)
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in events:
        if e.flow_id is None or e.step_outcome is None or e.drill or not first <= e.wall_at[:10] <= last:
            continue
        c = counts[e.flow_id]
        c[0] += int(e.step_outcome == "passed_via_agent_fallback")
        c[1] += 1
    return {f: (n, total) for f, (n, total) in counts.items()}


def evaluate(stats: Sequence[FlakeStats], step_events: Iterable[StepEvent], fixes: Iterable[OpenFix],
             aa_history: Mapping[str, Sequence[AANight]], today: date, *, owners: Mapping[str, str] | None = None,
             flow_classes: Mapping[str, str] | None = None, safety: Iterable[SafetyQuarantine] = (),
             holidays: Iterable[date] = ()) -> Evaluation:
    """Every 10.5 trigger over the given histories (module docstring). `stats` are `flake.refresh`'s statistics;
    `step_events` the window's step outcome rows (`flake.window_events`); `fixes` the open fix-list items; `aa_history`
    each checkpoint's Nightly rows, oldest first; `safety` the open safety quarantines; `flow_classes` flow id to
    class, beside the classes `stats` carry. A checkpoint already mirrored as quarantined gets no proposal."""
    owners = owners or {}
    by_cp = {s.checkpoint_id: s for s in stats}
    classes = {s.flow_id: s.flow_class for s in stats if s.flow_id and s.flow_class} | dict(flow_classes or {})
    found: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    fix_owner: dict[str, str] = {}
    for s in stats:
        if not s.quarantined:
            found[("checkpoint", s.checkpoint_id)] += [("flake", why) for why in _flake(s)]
    for cid, nights in aa_history.items():
        last = list(nights)[-AA_DRIFT_NIGHTS:]
        if len(last) == AA_DRIFT_NIGHTS and all(n.drifted for n in last) and not _mirrored(by_cp, cid):
            found[("checkpoint", cid)].append(
                ("aa_drift", f"not_settled (aa_drift) on {AA_DRIFT_NIGHTS} consecutive Nightly runs: "
                             f"{', '.join(n.run_id for n in last)}"))
    for fx in sorted(fixes, key=lambda f: (f.opened_at, f.fix_id)):
        overdue = fx.data_mode == "frozen" and fix_overdue(fx.opened_at, today, holidays)
        if overdue and not _mirrored(by_cp, fx.checkpoint_id):
            found[("checkpoint", fx.checkpoint_id)].append(
                ("aa_floor", f"fix item {fx.fix_id} open since {fx.opened_at[:10]}, past {FIX_BUSINESS_DAYS} "
                             "business days"))
            fix_owner.setdefault(fx.checkpoint_id, fx.owner)
    for flow, (n, total) in sorted(_fallback_rates(step_events, today).items()):
        if total and n / total > FALLBACK_RATE:
            found[("flow", flow)].append(("fallback_rate", f"agent-fallback rate {n} of {total} steps "
                                                           f"({100 * n / total:.1f}%) in {WINDOW_DAYS} days"))
    for q in safety:
        if q.target_kind == "flow":
            why = q.cause + (f": {q.detail}" if q.detail else "")
            found[("flow", q.target)].append((q.reason, f"safety quarantine {q.quarantine_id} ({why})"))
    order = list(load().quarantine_reasons)
    proposals, paged = [], []
    for (kind, target), hits in sorted(found.items()):
        if not hits:
            continue
        hits.sort(key=lambda h: order.index(h[0]))
        s = by_cp.get(target) if kind == "checkpoint" else None
        flow = s.flow_id if s is not None else (target if kind == "flow" else None)
        owner = fix_owner.get(target) if hits[0][0] == "aa_floor" else None
        owner = owner or owners.get(target) or (owners.get(flow) if flow else None)
        p = Proposal(kind, target, hits[0][0], today, "; ".join(t for _, t in hits),
                     tuple(dict.fromkeys(r for r, _ in hits)), owner=owner, flow_id=flow)
        exempt = (s.flow_class if s is not None else None) == EXEMPT_CLASS or classes.get(flow) == EXEMPT_CLASS
        (paged if exempt else proposals).append(p)
    return Evaluation(tuple(proposals), tuple(paged))


def _mirrored(by_cp: Mapping[str, FlakeStats], cid: str) -> bool:
    s = by_cp.get(cid)
    return s is not None and s.quarantined


def inputs(store: Store, today: date, *, exceptions: Mapping[str, Mapping[str, str]] | None = None) -> Inputs:
    """The store-side inputs of `evaluate` for the 30 days ending `today`. `exceptions` (checkpoint -> region ->
    ticket, `diff.calibration.live_exceptions` at run start) are the floor exceptions in force: an open region item
    they name is closed (choice 3). Without them every open item counts."""
    live = exceptions or {}
    fixes = tuple(OpenFix(r["fix_id"], r["checkpoint_id"], r["data_mode"], r["opened_at"], r["owner"],
                          r["ticket_ref"]) for r in store.query(
        "SELECT x.fix_id, x.checkpoint_id, x.region_id, c.data_mode, x.opened_at, x.owner, x.ticket_ref "
        "FROM calibration_fixes x JOIN checkpoints c ON c.checkpoint_id = x.checkpoint_id "
        "WHERE x.closed_at IS NULL ORDER BY x.fix_id")
        if r["region_id"] is None or r["region_id"] not in live.get(r["checkpoint_id"], {}))
    first, last = window(today)
    history: dict[str, list[AANight]] = defaultdict(list)
    for r in store.query(
            "SELECT o.checkpoint_id, o.run_id, r.started_at, o.outcome, o.reason FROM outcomes o "
            "JOIN runs r ON r.run_id = o.run_id WHERE r.run_type = 'nightly' AND o.synthetic = 0 "
            "AND substr(r.started_at, 1, 10) BETWEEN ? AND ? ORDER BY o.checkpoint_id, r.started_at, o.run_id",
            (first, last)):
        history[r["checkpoint_id"]].append(AANight(r["run_id"], r["started_at"],
                                                   r["outcome"] == "not_settled" and r["reason"] == "aa_drift"))
    return Inputs(tuple(window_events(store, today)), fixes, {k: tuple(v) for k, v in history.items()},
                  open_quarantines(store))


# -- the quarantine.yaml patch -------------------------------------------------------------------------------------
def _key(record: Mapping[str, Any]) -> tuple[str, str] | None:
    for kind in TARGET_KINDS:
        if record.get(kind):
            return kind, str(record[kind])
    return None


def render_patch(proposals: Iterable[Proposal], current_text: str | None = None, *,
                 base_commit: str | None = None) -> QuarantinePatch:
    """The `quarantine.yaml` content with every complete proposal added after the current records, and the PR the
    Orchestrator opens for it (10.5). `current_text` is the file at the pinned commit, read as the lint reads it. A
    proposal whose target already has a record is skipped; one without an owner or ticket waits in `pending`.
    `payload` is None when nothing is added."""
    text = current_text or ""
    doc = load_yaml_text(text) if text.strip() else None
    doc = doc if isinstance(doc, Mapping) else {}
    records = [dict(r) for r in (doc.get("records") or [])]
    have = {k for k in map(_key, records) if k is not None}
    added: list[dict[str, Any]] = []
    pending: list[tuple[Proposal, tuple[str, ...]]] = []
    skipped: list[Proposal] = []
    for p in proposals:
        if p.key in have:
            skipped.append(p)
            continue
        missing = tuple(n for n in ("owner", "ticket") if not getattr(p, n))
        if missing:
            pending.append((p, missing))
            continue
        added.append(p.record())
        have.add(p.key)
    lead: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        lead.append(f"{line}\n")
    header = "".join(lead)
    body = {"schema_version": doc.get("schema_version", 1), "records": records + added}
    content = header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True)
    payload = sha = None
    if added:
        lines = [f"- {next(iter(r))} `{r[next(iter(r))]}`: {r['reason']} ({r['detail']}); owner {r['owner']}, "
                 f"ticket {r['ticket']}" for r in added]
        payload = {"repository": CONFIG_REPOSITORY, "path": QUARANTINE_FILE, "base_commit": base_commit,
                   "title": f"Quarantine {len(added)} item(s) (10.5)",
                   "body": "\n".join(["Opened by the Orchestrator (10.5); the system owner merges it. Exit needs a fix "
                                      "plus 5 consecutive clean captures in a Calibration run, cited in the PR that "
                                      "removes the record.", *lines]),
                   "content": content, "records": added}
        sha = sha256_id(payload)
    return QuarantinePatch(content, tuple(added), tuple(pending), tuple(skipped), payload, sha)


# -- the run-start mirror, exit evidence and the tier-1 share --------------------------------------------------------
def exit_evidence(store: Store, checkpoint_id: str, since: str | None) -> str | None:
    """The latest Calibration run started at or after `since` whose floors for the checkpoint show the exit rule
    (choice 7), or None."""
    pairs = EXIT_CAPTURES * (EXIT_CAPTURES - 1) // 2
    for r in store.query("SELECT run_id FROM runs WHERE run_type = 'calibration' AND started_at >= ? "
                         "ORDER BY started_at DESC, run_id DESC", (utc(since) if since else "",)):
        floors = store.query("SELECT diff_pixels_max, components_max, samples FROM calibration_floors "
                             "WHERE calibration_run_id = ? AND checkpoint_id = ?", (r[0], checkpoint_id))
        fixed = not store.query("SELECT 1 FROM calibration_fixes WHERE calibration_run_id = ? AND checkpoint_id = ?",
                                (r[0], checkpoint_id))
        if floors and fixed and all(f["diff_pixels_max"] == 0 and not f["components_max"] and f["samples"] >= pairs
                                    for f in floors):
            return r[0]
    return None


def mirror(store: Store, doc: Mapping[str, Any] | None, *, commit: str, at: str | None = None) -> MirrorResult:
    """Mirror `quarantine.yaml` at the pinned `commit` into `flake_stats` (10.5): every checkpoint a record names
    (a flow record names each of its registered checkpoints; a checkpoint record wins over a flow record) is
    `quarantined` with the record's commit, owner, ticket, reason and entry date; a checkpoint no longer named is
    released. Git is the record and SQLite the mirror."""
    stamp = utc(at) or now()
    records = [r for r in ((doc or {}).get("records") or []) if isinstance(r, Mapping)]
    known = {r[0] for r in store.query("SELECT checkpoint_id FROM checkpoints")}
    targets: dict[str, Mapping[str, Any]] = {}
    unknown: list[str] = []
    for kind in TARGET_KINDS:
        for rec in records:
            if not rec.get(kind):
                continue
            ref = str(rec[kind])
            ids = ([ref] if ref in known else []) if kind == "checkpoint" else [
                r[0] for r in store.query("SELECT checkpoint_id FROM checkpoints WHERE flow_id = ?", (ref,))]
            if not ids:
                unknown.append(ref)
            for cid in ids:
                targets.setdefault(cid, rec)
    was = {r["checkpoint_id"]: r["quarantined_at"] for r in store.query(
        "SELECT checkpoint_id, quarantined_at FROM flake_stats WHERE quarantined = 1")}
    released = sorted(set(was) - set(targets))
    rows = [{"checkpoint_id": cid, "quarantined": 1, "quarantine_commit": commit,
             "quarantined_at": utc(str(rec["entered"])) if rec.get("entered") else stamp,
             "quarantine_owner": rec.get("owner"), "quarantine_ticket_ref": rec.get("ticket"),
             "quarantine_reason": rec.get("reason"), "updated_at": stamp} for cid, rec in sorted(targets.items())]
    rows += [{"checkpoint_id": cid, "quarantined": 0, "quarantine_commit": None, "quarantined_at": None,
              "quarantined_by": None, "quarantine_owner": None, "quarantine_ticket_ref": None,
              "quarantine_reason": None, "updated_at": stamp} for cid in released]
    store.replace_flake_stats(rows)
    unproven = tuple(cid for cid in released if exit_evidence(store, cid, was[cid]) is None)
    return MirrorResult(tuple(sorted(targets)), tuple(released), unproven, tuple(dict.fromkeys(unknown)))


def tier1_quarantine_share(store: Store) -> QuarantineShare:
    """Tier-1 checkpoints currently quarantined over registered tier-1 checkpoints (9.11 Quarantine share, 10.5;
    choice 8); `.red` above 2 percent."""
    rows = store.query(
        "SELECT c.checkpoint_id, c.flow_id, c.flow_class, COALESCE(s.quarantined, 0) AS q FROM checkpoints c "
        "LEFT JOIN flake_stats s ON s.checkpoint_id = c.checkpoint_id WHERE c.tier = 1 AND c.retired_in_build IS NULL "
        "AND c.checkpoint_id NOT IN (SELECT renamed_from FROM checkpoints WHERE renamed_from IS NOT NULL)")
    flows = {q.target for q in open_quarantines(store) if q.target_kind == "flow"}
    hit = {r["checkpoint_id"] for r in rows
           if r["q"] or (r["flow_id"] in flows and r["flow_class"] != EXEMPT_CLASS)}
    return QuarantineShare(len(hit), len(rows))
