"""The run's effective configuration at run start (4.5, 4.7, 4.8, 2.7, 5.9; SD-08; L-SRF-11, L-MSK-02, L-RGN-05).

What the Surface Registry, the flows and the Region Registry declare is not all in force for a run: an expired
invariant exception, mask or floor exception is dropped at run start and listed in run health, never `config_invalid`
(1.10, 3.8, L-SRF-11), and a checkpoint step can override its surface's tier, account-state flag and required labels
(4.7). This module turns a linted config into what the run applies:

- `effective_registry(cfg, run_start)` -> `EffectiveRegistry`: the invariant exceptions in force per surface, the
  masks in force, and `dropped`, every expired record as a `lint.rules.ExpiredRecord` (finding kind
  `invariant_exception_expired`, `mask_expired` or `floor_exception_expired`, `run_health_line()`).
- `EffectiveRegistry.surface_rules(checkpoint)` -> `invariants.SurfaceRules`: the 2.7 per-surface inputs of the
  registry entry (labels, required controls, the `invariants` block of SD-08) with the checkpoint's overrides and the
  exceptions in force. `overrides`, `tier` and `account_state` give the 4.7 overrides to the planner and the gates.
- `checker_config(cfg, theme, error_title_patterns)` -> `invariants.CheckerConfig`: the INV-15 palette of
  `palettes/<theme>.yaml` and the INV-14 error title patterns (the popup catalog's `app_failure` entries, 4.10).
- `workspace_theme(cfg, workspace_id)`: the theme of a workspace's `display_config`, which picks the palette.

`cfg` is anything with the loaded `repo` (`pipeline.config.SentinelConfig`); a checkpoint is anything with
`checkpoint_id`, `surface_id` and `data_mode` (`scope.PlannedCheckpoint`).

Choices where the spec is ambiguous or silent (reported):

1. Expiry instant. A record expires at its `expires_at` (as `MaskRecord`, `FloorException` and
   `InvariantException` decide it), compared with the run start as an instant; a naive timestamp is UTC. The CI and
   run-start lint decide at the start of their `today`, so a record that lint left in force can still drop here,
   never the reverse.
2. Dropped records are `ExpiredRecord`s with the fields the run-start lint gives them (`lint_at_run_start`), so the
   two lists union without duplicates; the regions file's checkpoint id is read from its path, as the lint does.
3. Overrides (4.7) come from the flow's checkpoint step with the same `checkpoint_id`; census, sweep and split
   checkpoints have none. `required_labels` add to the surface's labels without repeating one.
4. INV-07 minimum. The registry's `min_font_px` replaces the pinned 9 px; the Branch B cap-height minimum scales with
   it (about 7 px at 9 px, 2.7 INV-07).
5. Required controls carry their JAB name and role when the registry gives them (present only under DR-01 branch A,
   L-SRF-09) and their OCR anchor.
6. Missing inputs fail loudly (2.7): `EffectiveConfigError` names the lint rule whose check would have caught it,
   and the caller fails the run closed (`aborted_harness (reason: config_invalid)`): an unknown surface (L-XREF-11),
   an unreadable exception or expiry (L-SRF-11, L-MSK-02, L-RGN-05), an overlap allowlist entry that is not a pair
   or a missing palette (L-XREF-30), no INV-14 title patterns (L-XREF-12: the starter catalog has two `app_failure`
   entries) or one that does not compile (L-POP-01).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from . import invariants as inv
from .lint.repo import Repo
from .lint.rules import ExpiredRecord

PALETTE_ENTRIES = ("positive", "negative", "neutral")      # 4.16 palette.colors
_HEX = re.compile(r"^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$")


class EffectiveConfigError(ValueError):
    """A run input is missing or unusable; `rule` is the lint rule whose check covers it (choice 6)."""

    def __init__(self, message: str, *, rule: str):
        super().__init__(message)
        self.rule = rule


class ConfigView(Protocol):
    """What this module reads of the run's config: the loaded checkout (`SentinelConfig.repo`)."""

    @property
    def repo(self) -> Repo: ...


class CheckpointRef(Protocol):
    """What this module reads of a planned checkpoint (`scope.PlannedCheckpoint`)."""

    @property
    def checkpoint_id(self) -> str: ...

    @property
    def surface_id(self) -> str: ...

    @property
    def data_mode(self) -> str: ...


@dataclass(frozen=True)
class CheckpointOverrides:
    """The 4.7 checkpoint fields that override the surface (None: the surface's value applies)."""

    tier: int | None = None
    account_state: bool | None = None
    required_labels: tuple[str, ...] = ()


def _s(x: Any) -> str:
    return x if isinstance(x, str) else ""


def _expired(expires_at: Any, now: datetime, rule: str, where: str) -> bool:
    """Choice 1: expired from `expires_at` on."""
    try:
        t = datetime.fromisoformat(expires_at) if isinstance(expires_at, str) else None
    except ValueError:
        t = None
    if t is None:
        raise EffectiveConfigError(f"{where}: expires_at {expires_at!r} is not an RFC 3339 timestamp", rule=rule)
    return now >= (t if t.tzinfo else t.replace(tzinfo=timezone.utc))


def _exception(x: Any, where: str) -> inv.InvariantException:
    d = x if isinstance(x, dict) else {}
    if not (d.get("owner") and d.get("reason")) or d.get("invariant_id") not in inv.SEVERITY:
        raise EffectiveConfigError(f"{where}: an invariant exception names a catalog invariant (2.7) with owner and "
                                   "reason", rule="L-SRF-11")
    return inv.InvariantException(str(d["invariant_id"]), str(d["owner"]), str(d["reason"]), _s(d.get("expires_at")))


@dataclass(frozen=True)
class EffectiveRegistry:
    """The registry records in force at `run_start` and the ones the run dropped (module docstring)."""

    run_start: datetime
    surfaces: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)           # surface id -> entry
    steps: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)              # checkpoint id -> flow step
    exceptions: Mapping[str, tuple[inv.InvariantException, ...]] = field(default_factory=dict)
    masks_in_force: frozenset[str] = frozenset()
    dropped: tuple[ExpiredRecord, ...] = ()

    def run_health_lines(self) -> tuple[str, ...]:
        """`invariant exception expired: INV-06`, `mask expired: msk.x`, ... one per dropped record (4.5, 4.8)."""
        return tuple(r.run_health_line() for r in self.dropped)

    def _surface(self, surface_id: str) -> Mapping[str, Any]:
        s = self.surfaces.get(surface_id)
        if s is None:
            raise EffectiveConfigError(f"surface {surface_id} is not in the registry", rule="L-XREF-11")
        return s

    def overrides(self, checkpoint: CheckpointRef) -> CheckpointOverrides:
        """The checkpoint step's tier, account_state and required_labels (4.7; choice 3)."""
        st = self.steps.get(checkpoint.checkpoint_id, {})
        tier, acct = st.get("tier"), st.get("account_state")
        return CheckpointOverrides(int(tier) if tier is not None else None, bool(acct) if acct is not None else None,
                                   tuple(str(x) for x in st.get("required_labels") or ()))

    def tier(self, checkpoint: CheckpointRef) -> int:
        """The checkpoint's tier: its own `tier`, else the surface's (4.7 "overrides the surface tier")."""
        o = self.overrides(checkpoint).tier
        return o if o is not None else int(self._surface(checkpoint.surface_id).get("tier", 3))

    def account_state(self, checkpoint: CheckpointRef) -> bool:
        """The effective account-state flag (4.7 "overrides the surface flag"): fixture drift makes such checkpoints
        `env_error (gate: fixture_verified, reason: fixture_drift)` (4.5)."""
        o = self.overrides(checkpoint).account_state
        return o if o is not None else bool(self._surface(checkpoint.surface_id).get("account_state", False))

    def surface_rules(self, checkpoint: CheckpointRef) -> inv.SurfaceRules:
        """The invariant inputs of the checkpoint (2.7 per-surface inputs, 5.9 inputs; choices 3 to 5)."""
        s = self._surface(checkpoint.surface_id)
        block = s.get("invariants") if isinstance(s.get("invariants"), dict) else {}
        labels = dict.fromkeys([str(t) for t in s.get("labels") or ()] + list(self.overrides(checkpoint).required_labels))
        controls = []
        for c in s.get("controls") or ():
            if isinstance(c, dict) and c.get("required"):
                jab = c.get("jab") if isinstance(c.get("jab"), dict) else {}
                controls.append(inv.RequiredControl(str(c["id"]), role=jab.get("role"), name=jab.get("name"),
                                                    ocr_text=c.get("ocr_text")))
        pairs = []
        for i, pair in enumerate(block.get("overlap_allowlist") or ()):
            if not (isinstance(pair, list) and len(pair) == 2):
                raise EffectiveConfigError(f"surface {checkpoint.surface_id}: invariants.overlap_allowlist[{i}] is "
                                           "not a pair of controls (4.5)", rule="L-XREF-30")
            pairs.append((str(pair[0]), str(pair[1])))
        min_font = float(block.get("min_font_px", inv.MIN_FONT_PX))
        focus = block.get("focus_order")
        return inv.SurfaceRules(
            checkpoint.surface_id, tier=self.tier(checkpoint), data_mode=checkpoint.data_mode,
            labels=tuple(inv.RequiredLabel(t) for t in labels), controls=tuple(controls), allowlist=tuple(pairs),
            allowed_scrollbars={str(k): (int(v["w"]), int(v["h"]))
                                for k, v in (block.get("allowed_scrollbars") or {}).items()},
            min_font_px=min_font, min_cap_height_px=inv.MIN_CAP_HEIGHT_PX * min_font / inv.MIN_FONT_PX,
            signed_columns=frozenset(str(x) for x in block.get("signed_columns") or ()),
            focus_order=tuple(str(x) for x in focus) if focus else None,
            exceptions=self.exceptions.get(checkpoint.surface_id, ()))


def effective_registry(cfg: ConfigView, run_start: datetime) -> EffectiveRegistry:
    """The registry as it applies at `run_start` (module docstring; choices 1 and 2)."""
    now = run_start if run_start.tzinfo else run_start.replace(tzinfo=timezone.utc)
    repo = cfg.repo
    surfaces: dict[str, Mapping[str, Any]] = {}
    exceptions: dict[str, tuple[inv.InvariantException, ...]] = {}
    dropped: list[ExpiredRecord] = []
    for doc in repo.of("surface"):
        data = doc.data if isinstance(doc.data, dict) else {}
        sid = _s(data.get("id"))
        if not sid:
            continue
        surfaces[sid] = data
        live = []
        for i, x in enumerate(data.get("invariant_exceptions") or ()):
            at = f"invariant_exceptions[{i}]"
            exc = _exception(x, f"{doc.rel} {at}")
            if _expired(exc.expires_at, now, "L-SRF-11", f"{doc.rel} {at}"):
                dropped.append(ExpiredRecord("invariant_exception_expired", exc.invariant_id, sid, doc.rel, at,
                                             exc.expires_at, "L-SRF-11", exc.owner))
            else:
                live.append(exc)
        if live:
            exceptions[sid] = tuple(live)
    masks = set()
    for doc in repo.of("mask"):
        m = doc.data if isinstance(doc.data, dict) else {}
        if _expired(m.get("expires_at"), now, "L-MSK-02", doc.rel):
            dropped.append(ExpiredRecord("mask_expired", _s(m.get("mask_id")), _s(m.get("checkpoint_id")), doc.rel,
                                         "expires_at", _s(m.get("expires_at")), "L-MSK-02", _s(m.get("owner"))))
        else:
            masks.add(_s(m.get("mask_id")))
    for doc in repo.of("regions"):
        cid = doc.rel[len("regions/"):-len(".yaml")]
        regions = (doc.data if isinstance(doc.data, dict) else {}).get("regions") or ()
        for i, r in enumerate(regions):
            fe = r.get("floor_exception") if isinstance(r, dict) else None
            at = f"regions[{i}].floor_exception"
            if isinstance(fe, dict) and _expired(fe.get("expires_at"), now, "L-RGN-05", f"{doc.rel} {at}"):
                dropped.append(ExpiredRecord("floor_exception_expired", _s(r.get("id")), cid, doc.rel, at,
                                             _s(fe.get("expires_at")), "L-RGN-05", _s(fe.get("owner"))))
    steps = {str(st["checkpoint_id"]): st for doc in repo.of("flow") if isinstance(doc.data, dict)
             for st in doc.data.get("steps") or ()
             if isinstance(st, dict) and st.get("type") == "checkpoint" and st.get("checkpoint_id")}
    return EffectiveRegistry(now, surfaces, steps, exceptions, frozenset(masks), tuple(sorted(dropped)))


def workspace_theme(cfg: ConfigView, workspace_id: str) -> str:
    """The `display_config.theme` of a Workspace Corpus entry (4.6), which selects the INV-15 palette (2.7)."""
    for doc in cfg.repo.of("workspace"):
        data = doc.data if isinstance(doc.data, dict) else {}
        if data.get("id") == workspace_id:
            theme = (data.get("display_config") or {}).get("theme")
            if isinstance(theme, str) and theme:
                return theme
            break
    raise EffectiveConfigError(f"workspace {workspace_id} declares no display_config.theme", rule="L-WSP-00")


def _rgb(value: Any) -> tuple[int, int, int] | None:
    m = _HEX.match(value) if isinstance(value, str) else None
    return (int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)) if m else None


def checker_config(cfg: ConfigView, theme: str, error_title_patterns: Iterable[str]) -> inv.CheckerConfig:
    """The Invariant Checker's run-wide settings for one theme (5.9 inputs): the palette pinned in
    `palettes/<theme>.yaml` (INV-15) and `error_title_patterns`, the `title_regex` of the popup catalog's
    `app_failure` entries (INV-14, 4.10). Raises `EffectiveConfigError` when either is missing (choice 6)."""
    rel = f"palettes/{theme}.yaml"
    doc = cfg.repo.docs.get(rel)
    data: Mapping[str, Any] = doc.data if doc is not None and not doc.error and isinstance(doc.data, dict) else {}
    if not data:
        raise EffectiveConfigError(f"{rel} is missing or unreadable; INV-15 needs the palette pinned for theme "
                                   f"{theme} (2.7, 5.9)", rule="L-XREF-30")
    if data.get("theme") != theme:
        raise EffectiveConfigError(f"{rel} declares theme {data.get('theme')!r}, not {theme}", rule="L-XREF-30")
    colors = data.get("colors") if isinstance(data.get("colors"), dict) else {}
    palette: dict[str, tuple[int, int, int]] = {}
    for name in PALETTE_ENTRIES:
        rgb = _rgb(colors.get(name))
        if rgb is None:
            raise EffectiveConfigError(f"{rel} colors.{name} is not a #rrggbb color", rule="L-XREF-30")
        palette[name] = rgb
    patterns = tuple(error_title_patterns)
    if not patterns:
        raise EffectiveConfigError("INV-14 needs the title_regex of the popup catalog's app_failure entries (2.7, "
                                   "4.10)", rule="L-XREF-12")
    for p in patterns:
        try:
            re.compile(p)
        except re.error as exc:
            raise EffectiveConfigError(f"app_failure title_regex {p!r} does not compile: {exc}", rule="L-POP-01") \
                from exc
    return inv.CheckerConfig(error_title_patterns=patterns, palette=palette,
                             palette_tolerance=float(data.get("tolerance", inv.DEFAULT_CONFIG.palette_tolerance)))
