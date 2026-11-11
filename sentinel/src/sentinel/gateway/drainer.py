"""The drop-log drainer: every gateway decision as an 8.3 `step_events` row (5.4.8, 5.4.4, 6.2 Audit trail, 8.2).

The hook writes a compact ring record for every drop and every token it consumed (5.4.4); the slow half refuses
in stage 4 before anything reaches the hook (5.4.3). The drainer, a thread of the slow half (5.4.2), turns both
into `records.StepEvent` rows with every field 5.4.8 and 6.2 name, each in its own column (SD-16): run, flow,
step (`run_id`, `flow_id`, `step_id`), mode, `mono_ms` and `wall_at`, the event type with its vk or button
(`keys`), the screen point (`screen_x`, `screen_y`), `injected`, the reason code and its class (`gate_reason`,
`gate_class`), `token_id`, the resolved target (`target_text`), the 96x96 click crop (`click_crop_sha256`), the
latest capture (`pre_hash`) and the drill tag (`drill`). Allowed events are rows too (`emitted`, and `late_up`
with the delay), so the trail answers what the harness pressed as well as what it refused. Every dropped or
refused row is also appended to `gateway_drops.jsonl` before it is returned, so an Orchestrator that dies
before the store writes the rows loses none: on resume `ingest_journal` reads them back for
`Store.write_events`, which takes the same row again as a no-op (8.2).

Usage (the slow half): `Drainer(ctx, journal=..., next_seq=..., wall_at=wall_from(clock))`; the emitter calls
`note_token` for every token it writes (stage 5) and `set_context` when the step, mode, latest capture or drill
phase changes; the drainer thread calls `drain_ring(segment.buf)`; a stage 4 refusal goes through `refused`;
the rows go to `Store.write_events`. A caller with no segment (the simulated rig, whose `Rig.send_input` returns
each event with its `HookDecision`) builds the records with `segment.record_for` and calls `drain`.

Choices where the spec is silent or ambiguous (reported):

1. The drop classes are `orchestrator.gateway.drain`'s (`no_token` on another process's injected event is
   `untokened_injected`; safety and integrity by the 5.4.4 split), applied to the record rebuilt as a
   `HookDecision` and an `InputEvent`. An event tagged by the drill injector counts as the drill's only inside
   the declared raw-injection phase (`ctx.drill_phase`); outside it the tag has no standing and the event is
   another process's injection (`untokened_injected`).
2. Every ring decision is action `gateway_decision` (5.4.4, 8.3); the hook host's reinstall log is action
   `hook_reinstalled` with `gate_decision` `none`, `gate_reason` `hook_reinstalled`, class `integrity`, mode
   `system`. Allowed rows have class `allowed`. A probe move's row has mode `system` and `reason`
   `liveness_probe`.
3. `keys` is `<event type>[:<detail>]`: `move`, `button_down:shift+left`, `wheel:-120`, `key_down:ctrl+0x42`.
   A key event of a secret token, or any key event while `ctx.secret_step` is set, shows `<redacted>` for the vk
   (6.9: a secret never becomes a record). The screen point is set for pointer events only (the low-level
   keyboard hook has none).
4. The delay of a `late_up` (5.4.5 "the step event carries the delay") goes in `duration_ms`; a refusal's
   detail goes in `reason`.
5. A row a token explains takes the token's mode, flow, step, attempt, target, rect and click crop from its
   `TokenNote`; a row no token explains (console input, another process's injection, `no_token`) takes the
   context's, and its click crop from `crop_at` when one is given (the drainer runs within a poll interval of
   the event, which is as close to action time as the hook allows).
6. The journal holds the dropped and refused rows only, as its name says; emitted rows go to the store alone.
   The journal is flushed per row, not fsynced: it covers an Orchestrator death, and the rig's own crash
   handling covers the OS.
7. `step_events.seq` orders a whole shard, so it comes from the caller's shard-wide allocator (`next_seq`),
   shared with every other writer of the shard's step events.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..orchestrator.clock import Clock
from ..orchestrator.gateway import HookDecision, InputEvent, SlowDecision
from ..orchestrator.gateway import drain as classify_drop
from ..records import StepEvent, from_dict
from .segment import PROBE_ID_BIT, RingReader, RingRecord

JOURNAL_NAME = "gateway_drops.jsonl"
MAX_NOTES = 4096                       # token notes kept; a token's records drain within a poll interval
POINTER_EVENTS = ("move", "button_down", "button_up", "wheel")
KEY_EVENTS = ("key_down", "key_up")


@dataclass(frozen=True)
class TokenNote:
    """What the slow half knew when it wrote a token (5.4.3 stages 3 and 5), for the rows its records become."""

    token_id: int
    mode: str
    flow_id: str | None = None
    step_id: str | None = None
    checkpoint_id: str | None = None
    attempt: int = 1
    target_text: str | None = None            # the resolved control name, or '<redacted:...>' on a secret step
    click_crop_sha256: str | None = None      # the 96x96 crop taken at action time
    rect_w: int | None = None
    rect_h: int | None = None


@dataclass(frozen=True)
class DrainContext:
    """Where the slow half is: the run, shard and side, the work in progress and the latest capture."""

    run_id: str
    shard_id: str
    side: str
    mode: str = "system"                      # the mode of the work in progress
    flow_id: str | None = None
    step_id: str | None = None
    checkpoint_id: str | None = None
    pre_hash: str | None = None               # sha256 of the latest capture (5.4.8)
    drill_phase: bool = False                 # inside the fire drill's declared raw-injection phase (6.5)
    secret_step: bool = False                 # a secret step is typing (6.9)


def wall_from(clock: Clock) -> Callable[[int], str]:
    """`wall_at` for the drainer: an event's monotonic nanoseconds as UTC ISO text, through `clock`'s pair of
    readings (the event's own time, never the drainer's)."""
    def at(mono_ns: int) -> str:
        return (clock.now() - timedelta(seconds=clock.monotonic() - mono_ns / 1e9)).isoformat()
    return at


class Drainer:
    """The drop-log drainer of one shard (module docstring)."""

    def __init__(self, ctx: DrainContext, *, journal: str | Path, next_seq: Callable[[], int],
                 wall_at: Callable[[int], str], crop_at: Callable[[int, int], str | None] | None = None):
        self.ctx = ctx
        self.journal = Path(journal)
        self.next_seq = next_seq
        self.wall_at = wall_at
        self.crop_at = crop_at
        self.reader = RingReader()
        self.notes: dict[int, TokenNote] = {}

    @property
    def lost(self) -> int:
        """Ring records the hook overwrote before they were drained."""
        return self.reader.lost

    def set_context(self, **changes: Any) -> None:
        """Change the context (step, mode, latest capture, drill phase, secret step)."""
        self.ctx = replace(self.ctx, **changes)

    def note_token(self, note: TokenNote) -> None:
        """Register a token the emitter wrote (stage 5), so its records carry the step that asked for it."""
        self.notes[note.token_id] = note
        while len(self.notes) > MAX_NOTES:
            del self.notes[next(iter(self.notes))]

    def drain_ring(self, buf: Any) -> list[StepEvent]:
        """Read the ring records written since the last call and drain them."""
        return self.drain(self.reader.read(buf))

    def drain(self, records: Iterable[RingRecord]) -> list[StepEvent]:
        """One `step_events` row per ring record, in ring order; the dropped rows are journaled first."""
        rows = [self._row(r) for r in records]
        self._journal([r for r in rows if r.gate_decision == "dropped"])
        return rows

    def refused(self, decision: SlowDecision, *, mono_ns: int, keys: str | None = None,
                point: tuple[int, int] | None = None, target_text: str | None = None,
                click_crop_sha256: str | None = None, attempt: int = 1,
                rect: tuple[int, int] | None = None) -> StepEvent:
        """A stage 4 refusal (5.4.3) as its row, journaled: nothing reached the hook, so no token and not
        injected; the mode, flow and step are the context's."""
        if not decision.refused or decision.reason is None:
            raise ValueError("not a refusal")
        c = self.ctx
        row = StepEvent(
            run_id=c.run_id, shard_id=c.shard_id, side=c.side, seq=self.next_seq(), source="orchestrator",
            wall_at=self.wall_at(mono_ns), mono_ms=mono_ns // 1_000_000, mode=c.mode, action="gateway_decision",
            flow_id=c.flow_id, step_id=c.step_id, checkpoint_id=c.checkpoint_id, attempt=attempt,
            target_text=target_text, screen_x=point[0] if point else None, screen_y=point[1] if point else None,
            keys=keys, gate_decision="refused", gate_reason=decision.reason, gate_class=decision.gate_class,
            pre_hash=c.pre_hash, click_crop_sha256=click_crop_sha256, reason=decision.detail,
            rect_w=rect[0] if rect else None, rect_h=rect[1] if rect else None)
        self._journal([row])
        return row

    # -- rows -------------------------------------------------------------------------------------------------
    def _row(self, r: RingRecord) -> StepEvent:
        c = self.ctx
        common = dict(run_id=c.run_id, shard_id=c.shard_id, side=c.side, seq=self.next_seq(), source="gateway_hook",
                      wall_at=self.wall_at(r.mono_ns), mono_ms=r.mono_ns // 1_000_000, pre_hash=c.pre_hash)
        if r.event_type == "control":                                                   # choice 2
            return StepEvent(**common, mode="system", action="hook_reinstalled", flow_id=c.flow_id,
                             step_id=c.step_id, checkpoint_id=c.checkpoint_id, gate_decision="none",
                             gate_reason="hook_reinstalled", gate_class="integrity",
                             reason="reinstall ok" if r.aux else "reinstall failed")
        note = self.notes.get(r.token_id) if r.token_id is not None else None
        probe = r.token_id is not None and bool(r.token_id & PROBE_ID_BIT)
        mode = note.mode if note else ("system" if probe else c.mode)
        reason, cls, drill = None, "allowed", False
        if r.decision == "dropped":                                                      # choice 1
            origin = r.origin if r.origin != "drill" or c.drill_phase else "foreign_process"
            e = InputEvent(r.event_type, r.x, r.y, button=r.button, vk=r.vk,
                           delta=r.aux if r.event_type == "wheel" else 0, mods=r.mods, origin=origin)
            d = classify_drop(HookDecision(False, r.reason, r.token_id), e, mode=mode, drill_phase=c.drill_phase)
            reason, cls, drill = d.reason, d.gate_class, d.tag == "drill"
        secret = r.secret or (c.secret_step and r.event_type in KEY_EVENTS)
        pointer = r.event_type in POINTER_EVENTS
        crop = note.click_crop_sha256 if note else None
        if crop is None and self.crop_at is not None and r.event_type == "button_down":
            crop = self.crop_at(r.x, r.y)
        return StepEvent(
            **common, mode=mode, action="gateway_decision",
            flow_id=note.flow_id if note else c.flow_id, step_id=note.step_id if note else c.step_id,
            checkpoint_id=note.checkpoint_id if note else c.checkpoint_id, attempt=note.attempt if note else 1,
            target_text=note.target_text if note else None, screen_x=r.x if pointer else None,
            screen_y=r.y if pointer else None, keys=_keys(r, secret), gate_decision=r.decision, gate_reason=reason,
            gate_class=cls, drill=drill, click_crop_sha256=crop, reason="liveness_probe" if probe else None,
            duration_ms=r.aux if r.decision == "late_up" else None, rect_w=note.rect_w if note else None,
            rect_h=note.rect_h if note else None, injected=r.injected, token_id=r.token_id)

    def _journal(self, rows: list[StepEvent]) -> None:
        if not rows:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row.to_dict(), sort_keys=True) + "\n")
            f.flush()


def _keys(r: RingRecord, secret: bool) -> str:
    """The 8.3 `keys` of a ring record (choice 3)."""
    mods = "".join(f"{m}+" for m in ("shift", "ctrl", "alt", "win") if m in r.mods)
    if r.event_type == "move":
        return "move"
    if r.event_type == "wheel":
        return f"wheel:{r.aux:+d}"
    if r.event_type in KEY_EVENTS:
        return f"{r.event_type}:<redacted>" if secret else f"{r.event_type}:{mods}0x{r.vk_or_button:02X}"
    return f"{r.event_type}:{mods}{r.button or r.vk_or_button}"


def ingest_journal(path: str | Path) -> list[StepEvent]:
    """The journal's rows for `Store.write_events` on resume (8.2); a torn last line (the Orchestrator died
    mid-write) is skipped, a torn line anywhere else raises."""
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            if i == len(lines) - 1 and not text.endswith("\n"):
                break
            raise
        out.append(from_dict(StepEvent, data))
    return out
