"""The "Not detectable by design" table of every merge, RC and Nightly report (2.13, 9.4 item 6).

What the model does not look at is printed, not implied: each class B01 to B16 of 2.13 has a stable id, the mechanism
that labels it, and the counts this run gives it. `compute(cfg, outcomes)` reads the run's config (the registry,
flows, regions files, entitlement profiles, corpus, display configurations, send policy and `phase0/branches.yaml`)
and its outcome records, and returns `BlindSpots`, one `BlindSpot` per id in 2.13 order; html.py prints the table.
`BlindSpots.header_lines()` are the B06 and B07 lines 9.2 also puts in the report header.

Choices where 2.13 is ambiguous (reported):

1. B02 "value regions": the registry has no field that marks a region as showing a value, so every declared `diff`,
   `freeze` or `rule` region of a tier-1 surface's checkpoint is a value region, counted per (checkpoint, region). It
   carries a rule when it is a `rule` region (the Oracle asserts it, 3.8) or when the checkpoint's flow assertions
   (`expect` of the checkpoint step, 4.7) name it; census and sweep checkpoints have no assertions (2.8.1).
2. B03 "masked share": for each checkpoint, the share of each panel's declared bounds (the bounding box of the
   regions of one panel under one landmark) that its `mask` regions cover, the 3.8 budget share without the run-time
   redaction boxes; the surface prints its largest. Every mask is listed: masks are made only for content that moves
   between captures (3.8), and the surface's `streaming` flag is printed beside it.
3. B06 "configurations covered: N of M": M is the display configurations of `configs/allowed.yaml` (the planned
   set), N those a workspace of this run's outcome records was saved under (4.6 `display_config`).
4. B08: the corpus entries list no studies (4.6 has no field for them), so the line prints the corpus size and says
   the corpus-workspace studies without expectations are not enumerated, instead of a count it cannot know.
5. B10: the browser stub's log comes from the run (`browser_log`, RC runs' Explorer); none prints "no launch
   recorded".
6. B16: a step is any entry of a flow's `steps` other than a `checkpoint` or `assert` step. It is observed when it
   carries its own `verify` assertion, or when one of the next three entries is a checkpoint, an `assert` step or a
   step with `verify`. Steps count toward tier 1 by their flow's tier.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .. import branches as branches_mod
from ..coverage import Profile, order_lifecycle_not_run
from ..diff.regions import Rect
from ..lint.repo import Repo
from ..records import CheckpointOutcome, Finding
from ..scope import Flow, Registry
from ..taxonomy import checkpoint_parts
from .inline import plural

PLATFORM_LINE = "platform: Windows 11 desktop only"                         # B07 (1.3 non-goals)
KNOWN_GAPS_LINE = "paperMoney versus live: delayed data, simulated fills, no real rejections"   # B04
OBSERVATION_STEPS = ("checkpoint", "assert")
OBSERVE_WINDOW = 3                                                           # B16: "within 3 steps"
VALUE_KINDS = ("diff", "freeze", "rule")
TITLES = {
    "B01": "Anything after Send", "B02": "A value wrong in both builds with no Oracle rule or assertion",
    "B03": "Temporal behavior", "B04": "Entitlement states paperMoney cannot produce",
    "B05": "Live-feed data-format defects under frozen data", "B06": "Rendering at non-canonical configurations",
    "B07": "macOS and Linux builds", "B08": "Study mathematics beyond the verification set and corpus",
    "B09": "Accessibility conformance", "B10": "Side effects outside the window", "B11": "Cross-client state",
    "B12": "Interactions outside the step vocabulary", "B13": "Surfaces unreachable because a gate failed",
    "B14": "Defects seen only in the app log or a crash file", "B15": "A dialog the registry has but no flow reaches",
    "B16": "Behavior with no visible symptom at any checkpoint"}
MECHANISMS = {
    "B01": "flows carry requires: [paper_send]; every cell not_run (entitlement)",
    "B02": "ends as identical or noise; Oracle coverage per tier-1 surface",
    "B03": "captures are stills; streaming regions are rule or mask",
    "B04": "not_run (entitlement) on every cell of a missing profile",
    "B05": "every checkpoint records its data mode",
    "B06": "configuration is in the fingerprint; the matrix is per configuration",
    "B07": "non-goal; no registry entries",
    "B08": "only corpus studies have expected values",
    "B09": "branch A diffs the accessible tree; nothing absolute is asserted",
    "B10": "Explorer login has no verified channels; the browser is a logging stub",
    "B11": "not observed; not an Oracle source (DR-06, 6.12)",
    "B12": "flows cannot express them; the vocabulary is the closed list of 2.6.1",
    "B13": "env_error with gate name and screenshot; counted in run health, never in coverage",
    "B14": "Watchdog attaches logs and crash files; app_alive, app_crash and new_log_signature",
    "B15": "the popup catalog catches dialogs that appear on their own as unknown_popup",
    "B16": "checkpoints and assertions are the only observation points"}


class BlindSpotConfig(Protocol):
    """What `compute` reads of the run's config: `pipeline.config.SentinelConfig` satisfies it."""

    repo: Repo
    registry: Registry
    flows: list[Flow]
    flow_docs: dict[str, dict]
    regions: dict[str, dict]
    profiles: tuple[Profile, ...]
    send_policy: str


@dataclass(frozen=True)
class BlindSpot:
    """One 2.13 row: id, what is not detectable, the labeling mechanism, this run's lines and an optional table."""

    id: str
    title: str
    mechanism: str
    lines: tuple[str, ...]
    head: tuple[str, ...] = ()
    table: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class BlindSpots:
    """The "Not detectable by design" table, B01 to B16 in order."""

    rows: tuple[BlindSpot, ...]

    def row(self, bid: str) -> BlindSpot:
        return next(r for r in self.rows if r.id == bid)

    def header_lines(self) -> tuple[str, ...]:
        """The report header's B06 and B07 lines (9.2)."""
        return (self.row("B06").lines[0], self.row("B07").lines[0])


# -- B02, B03: the regions files ------------------------------------------------------------------------------------
def _strings(x: Any) -> Iterable[str]:
    if isinstance(x, str):
        yield x
    elif isinstance(x, Mapping):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _strings(v)


def asserted_regions(flow_docs: Mapping[str, Mapping[str, Any]]) -> dict[str, set[str]]:
    """Per flow checkpoint id, every string its `expect` assertions name (the region ids among them, choice 1)."""
    out: dict[str, set[str]] = {}
    for doc in flow_docs.values():
        for step in doc.get("steps") or ():
            if isinstance(step, Mapping) and step.get("type") == "checkpoint" and step.get("checkpoint_id"):
                out.setdefault(str(step["checkpoint_id"]), set()).update(_strings(step.get("expect") or ()))
    return out


def oracle_coverage(registry: Registry, regions: Mapping[str, Mapping[str, Any]],
                    flow_docs: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, int, int]]:
    """B02: per tier-1 surface, (surface, value regions, value regions with a rule or assertion) over its checkpoints'
    regions files (choice 1); a tier-1 surface with no regions file prints 0 of 0."""
    named = asserted_regions(flow_docs)
    per: dict[str, list[int]] = {s.id: [0, 0] for s in registry if s.tier == 1}
    for cid, doc in regions.items():
        sid = checkpoint_parts(cid)["surface"]
        if sid not in per:
            continue
        for r in doc.get("regions") or ():
            if r.get("type") not in VALUE_KINDS:
                continue
            per[sid][0] += 1
            per[sid][1] += r.get("type") == "rule" or r.get("id") in named.get(cid, ())
    return [(sid, n, ruled) for sid, (n, ruled) in sorted(per.items())]


def _rect(r: Mapping[str, Any]) -> Rect:
    d = r.get("rect") or {}
    return Rect(int(d.get("dx", 0)), int(d.get("dy", 0)), int(d.get("w", 0)), int(d.get("h", 0)))


def masked_share(doc: Mapping[str, Any]) -> float:
    """B03: the largest share of one panel's declared bounds covered by masks in one regions file (choice 2)."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for r in doc.get("regions") or ():
        groups.setdefault((str(r.get("landmark")), str(r.get("panel", "other"))), []).append(r)
    best = 0.0
    for members in groups.values():
        masks = [_rect(r) for r in members if r.get("type") == "mask"]
        if not masks:
            continue
        bounds = Rect.bounding([_rect(r) for r in members])
        if bounds.area == 0:
            continue
        canvas = np.zeros((bounds.h, bounds.w), dtype=bool)
        for m in masks:
            i = m.intersect(bounds)
            if i is not None:
                canvas[i.y - bounds.y:i.y - bounds.y + i.h, i.x - bounds.x:i.x - bounds.x + i.w] = True
        best = max(best, float(canvas.sum()) / bounds.area)
    return best


def masked_surfaces(registry: Registry, regions: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, int, float, bool]]:
    """B03: (surface, mask regions, largest masked share, streaming) of every surface with a mask region."""
    per: dict[str, list[float]] = {}
    for cid, doc in regions.items():
        n = sum(1 for r in doc.get("regions") or () if r.get("type") == "mask")
        if n:
            row = per.setdefault(checkpoint_parts(cid)["surface"], [0, 0.0])
            row[0] += n
            row[1] = max(row[1], masked_share(doc))
    return [(sid, int(n), share, sid in registry and registry[sid].streaming) for sid, (n, share) in sorted(per.items())]


# -- B16: the flow files --------------------------------------------------------------------------------------------
def _observes(step: Mapping[str, Any]) -> bool:
    return step.get("type") in OBSERVATION_STEPS or bool(step.get("verify"))


def unobserved_steps(doc: Mapping[str, Any]) -> tuple[int, list[str]]:
    """B16 for one flow: (its steps, the ids of those not followed by a checkpoint or assertion within 3 steps)."""
    steps = [s for s in doc.get("steps") or () if isinstance(s, Mapping)]
    counted, missing = 0, []
    for i, s in enumerate(steps):
        if s.get("type") in OBSERVATION_STEPS:
            continue
        counted += 1
        if not (s.get("verify") or any(_observes(n) for n in steps[i + 1:i + 1 + OBSERVE_WINDOW])):
            missing.append(str(s.get("id")))
    return counted, missing


# -- the rows -------------------------------------------------------------------------------------------------------
def _pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:.1f}" if b else "0.0"


def _b01(cfg: BlindSpotConfig) -> tuple[str, ...]:
    if cfg.send_policy == "gated":
        return ("send policy: gated (paper_send_ok); fills untested until a human verifies that paperMoney simulates "
                "them on the delayed feed",)
    n = len(order_lifecycle_not_run(cfg.registry, cfg.flows, cfg.profiles))
    return (f"send policy: default (no sends); {plural(n, 'order-lifecycle surface')} not run (entitlement)",)


def _configs(cfg: BlindSpotConfig, outcomes: Sequence[CheckpointOutcome]) -> str:
    doc = cfg.repo.one("configs_allowed")
    planned = [c for c in ((doc.data or {}).get("configs") if doc is not None else None) or () if isinstance(c, Mapping)]
    used = {o.workspace_id for o in outcomes}
    keys = ("width", "height", "scale_percent", "theme", "font_size")
    shown = [(w.data.get("display_config") or {}) for w in cfg.repo.of("workspace")
             if isinstance(w.data, Mapping) and w.data.get("id") in used]
    covered = [c for c in planned if any(all(d.get(k) == c.get(k) for k in keys) for d in shown)]
    return f"configurations covered: {len(covered)} of {len(planned)} planned"


def _accessible_tree(cfg: BlindSpotConfig) -> str:
    doc = cfg.repo.one("branches")
    jab = branches_mod.parse(doc.data).branch("jab") if doc is not None else "B"
    return ("accessible tree: diffed build to build (DR-01 branch A); lost names are collateral" if jab == "A"
            else "accessible tree: not available")


def compute(cfg: BlindSpotConfig, outcomes: Sequence[CheckpointOutcome], *, findings: Sequence[Finding] = (),
            browser_log: Sequence[str] = ()) -> BlindSpots:
    """The 2.13 table for one run (module docstring): `outcomes` are the run's outcome records (synthetic canary rows
    are left out), `findings` its findings (B14), `browser_log` the browser stub's log lines (B10)."""
    outs = [o for o in outcomes if not o.synthetic]
    reg = cfg.registry
    rows: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, ...], ...]]] = {}
    rows["B01"] = (_b01(cfg), (), ())

    cov = oracle_coverage(reg, cfg.regions, cfg.flow_docs)
    total, ruled = sum(n for _, n, _ in cov), sum(r for _, _, r in cov)
    rows["B02"] = ((f"Oracle coverage on {plural(len(cov), 'tier-1 surface')}: {ruled} of {total} value regions "
                    f"carry a rule or assertion, {total - ruled} carry none",),
                   ("Tier-1 surface", "Value regions", "With a rule", "Without"),
                   tuple((s, str(n), str(r), str(n - r)) for s, n, r in cov))

    masked = masked_surfaces(reg, cfg.regions)
    rows["B03"] = ((f"surfaces with masked streaming regions: {len(masked)}",),
                   ("Surface", "Mask regions", "Largest masked share of a panel", "Streaming"),
                   tuple((s, str(n), f"{100 * share:.1f} percent", "yes" if streaming else "no")
                         for s, n, share, streaming in masked))

    missing = [s.id for s in reg if not any(s.entitlements <= p.entitlements for p in cfg.profiles)]
    profiles = ", ".join(f"{p.id} ({', '.join(sorted(p.entitlements))})" for p in cfg.profiles) or "none"
    rows["B04"] = ((f"profiles: {profiles}", KNOWN_GAPS_LINE,
                    f"surfaces no profile is entitled to: {len(missing)}" + (f" ({', '.join(missing)})" if missing
                                                                             else "")), (), ())

    modes: Counter[tuple[int | None, str]] = Counter(
        (o.tier if o.tier is not None else (reg[o.surface_id].tier if o.surface_id in reg else None), o.data_mode)
        for o in outs)
    tiers = sorted({t for t, _ in modes}, key=lambda t: (t is None, t))
    rows["B05"] = (tuple(f"tier {t if t is not None else 'unknown'}: frozen {modes[(t, 'frozen')]}, masked_live "
                         f"{modes[(t, 'masked_live')]}" for t in tiers) or ("no checkpoint recorded a data mode",),
                   (), ())
    rows["B06"] = ((_configs(cfg, outs),), (), ())
    rows["B07"] = ((PLATFORM_LINE,), (), ())
    corpus = len(cfg.repo.of("workspace"))
    rows["B08"] = ((f"corpus size: {plural(corpus, 'workspace entry', 'workspace entries')}; corpus-workspace studies "
                    "without expectations: not enumerated (no corpus entry lists its studies)",), (), ())
    rows["B09"] = ((_accessible_tree(cfg),), (), ())
    rows["B10"] = ((f"browser stub log: {plural(len(browser_log), 'launch', 'launches')}" if browser_log
                    else "browser stub log: no launch recorded", *browser_log), (), ())
    rows["B11"] = (("cross-client sync: not observed",), (), ())
    rows["B12"] = (("step vocabulary: the closed list of 2.6.1; touch, pen, IME, screen readers and drags between "
                    "displays cannot be expressed",), (), ())
    gates = Counter(o.gate or "unknown" for o in outs if o.outcome == "env_error")
    rows["B13"] = ((f"env_error: {sum(gates.values())}" + (" (" + ", ".join(f"{g} {n}" for g, n in
                                                                          sorted(gates.items())) + ")" if gates else "")
                    + "; counted in run health, never in coverage",), (), ())
    kinds = Counter(f.kind for f in findings)
    alive = sum(1 for o in outs if o.outcome == "assertion_failed" and o.reason == "assertion:app_alive")
    rows["B14"] = ((f"assertion_failed (assertion: app_alive): {alive}; app_crash findings: {kinds['app_crash']}; "
                    f"new_log_signature findings: {kinds['new_log_signature']}",), (), ())
    dialogs = [s.id for s in reg if s.kind == "dialog"]
    reached = {c.surface_id for f in cfg.flows for c in f.checkpoints}
    zero = [d for d in dialogs if d not in reached]
    rows["B15"] = ((f"registered dialogs with zero flow checkpoints: {len(zero)} of {len(dialogs)}"
                    + (f" ({', '.join(zero)})" if zero else ""),), (), ())

    tier_of = {f.flow_id: f.tier for f in cfg.flows}
    per_flow, t1_steps, t1_missing = [], 0, 0
    for fid, doc in sorted(cfg.flow_docs.items()):
        n, gaps = unobserved_steps(doc)
        per_flow.append((fid, str(n), str(len(gaps)), ", ".join(gaps)))
        if tier_of.get(fid) == 1:
            t1_steps, t1_missing = t1_steps + n, t1_missing + len(gaps)
    rows["B16"] = ((f"tier-1 steps observed: {t1_steps - t1_missing} of {t1_steps} "
                    f"({_pct(t1_steps - t1_missing, t1_steps)} percent)",),
                   ("Flow", "Steps", "Unobserved steps", "Which"), tuple(per_flow))
    return BlindSpots(tuple(BlindSpot(bid, TITLES[bid], MECHANISMS[bid], *rows[bid]) for bid in TITLES))
