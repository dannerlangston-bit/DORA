"""The shared-memory segment between the gateway's two halves (5.4.2) and the hook's decision over it (5.4.4).

The slow half (the Orchestrator) and the fast half (the hook host) share one memory-mapped segment. This module
is its codec: byte layout, the seqlock, the token slots, the deny rectangles, the blocked key map and chord
table, the drop ring, and `decide_from_segment`, the hook callback's table of 5.4.4 read straight from the bytes.
It imports the standard library only (5.4.2: the hook host imports only ctypes, mmap and the standard library),
so it cannot import `sentinel.orchestrator.gateway`; its rules mirror `orchestrator.gateway.hook_decide`, and
tests/test_gateway_segment.py proves the two decide alike on the same states and events (parity).

Layout (little-endian, offsets in bytes; a native rewrite of the hook host keeps exactly this contract, 5.4.4):

    header        0..128   magic u32 @0, layout_version u32 @4, seq u64 @8 (seqlock)
                           slow half: armed u8 @16, deny_all u8 @17, flag_ticket u8 @18 (0 clear, 1 set,
                             2 unknown), deny_count u8 @19, chord_count u8 @20, orchestrator_pid u32 @24,
                             hook_host_pid u32 @28, watcher_mono_ts i64 @32, reinstall_request u32 @40
                           hook: last_probe_mono_ts i64 @64, hook_alive_mono_ts i64 @72, ring_head u64 @80,
                             reinstall_done u32 @88, reinstall_ok u8 @92, send_rect_passed u8 @93
    tokens[16]    128..768  40 bytes each; slow half: id u32 @0, kind u8 @4, flags u8 @5 (dialog_ok 1,
                           secret 2, release 4, send_ok 8), button_or_vk u16 @6, x i32 @8, y i32 @12,
                           tol_px u16 @16, expires_mono_ts i64 @24; hook: state_id u32 @32, state u8 @36,
                           drop_reason u8 @37
    move_allow    768..800  active u8 @0, until_mono_ts i64 @8, bbox x, y, w, h i32 @16
    deny_rects[8] 800..960  20 bytes each: x, y, w, h i32, is_send u8
    blocked_vk    960..992  256-bit map, bit (vk & 7) of byte (vk >> 3)
    chords[16]    992..1056 4 bytes each: mods u8 (shift 1, ctrl 2, alt 4, win 8), vk u16 @2
    drop_ring     1088..    4096 x 32-byte records: seq u32, event_type u8, origin u8, reason_code u8,
                           flags u8 (mods bits as above, injected 16, secret 32), mono_ts i64, x i32, y i32,
                           vk_or_button u16, aux i16 (wheel delta; the delay in ms of a late_up), token_id u32

Times are `time.monotonic_ns()` nanoseconds, one system-wide clock for both processes; the slow half's
seconds (`GatewayState`) convert with `to_ns`. The slow half writes its fields inside the seqlock (seq to odd,
write, seq to even); the hook writes token `state`, `last_probe_mono_ts`, `hook_alive_mono_ts` and the ring,
never under the seqlock, each field with a single writer (5.4.2).

Choices where the spec is silent or ambiguous (reported):

1. The 5.4.2 table names the fields, not their encoding; the layout above is the encoding. Five fields are
   added because 5.4.4 needs them and the table has none: `reinstall_request`/`reinstall_done`/`reinstall_ok`
   (the slow half asks the hook host to reinstall both hooks after a missed probe, the hook host answers),
   `send_rect_passed` (the gated branch's one pass of the Send rectangle, 6.7, as `GatewayState` keeps it),
   and `ring_head` (the count of ring records written, so the drainer knows where the hook is).
2. The hook alone writes a token's `state`, so a token issued into a slot cannot reset it. The hook writes
   `state_id` (the id the state belongs to) next to it, and a slot's state is `armed` until `state_id` equals
   its `id`. `release` is a token flag (5.4.2), so a release token has kind `button` or `key`; a wheel token
   keeps its delta sign in `button_or_vk` (1 positive, 2 negative); kind `probe` is the tokened probe move of
   5.4.4, which the hook answers by stamping `last_probe_mono_ts` (in every state, deny_all included, since the
   probe asks whether the hook runs, not whether input may pass) and which never counts as a pending token for
   the move rule.
3. Token order: `hook_decide` takes the first matching token in issue order; slots are reused, so the hook
   orders candidates by token id, which the slow half issues in increasing order.
4. A seqlock that stays odd after `MAX_SPINS` rereads (a writer died mid-write) makes the hook drop the event
   `deny_all`: there is no consistent picture to act on, and 5.4.2 fails closed. A writer whose transaction
   raises leaves `seq` odd for the same reason.
5. The ring keeps a record for every drop and for every event that matched a token (5.4.4: "every consumed
   token writes a compact record"), the probe included; allowed moves with no token write nothing. A record's
   `origin` is what the hook can tell from the injected flag and the emitter's tag (`emitter`,
   `foreign_process`, `drill`, `console`), so the drainer can name `untokened_injected` and the drill tag. A
   control record (event type `control`: the hook host's own `hook_reinstalled` log, aux 1 when the reinstall
   worked) has origin 0 and no injected flag.
"""
from __future__ import annotations

import struct
import sys
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from typing import Any, NamedTuple, Protocol

MAGIC = 0x47544E53                      # b"SNTG" as a little-endian u32
LAYOUT_VERSION = 1

TOKEN_SLOTS = 16
DENY_RECT_SLOTS = 8
CHORD_SLOTS = 16
RING_SLOTS = 4096
RING_RECORD_SIZE = 32
WATCHER_STALE_NS = 500_000_000         # 5.4.2: a Watcher stamp older than 500 ms is flag_ticket = unknown
MAX_SPINS = 256                        # choice 4
INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1
PROBE_ID_BIT = 0x8000_0000             # probe token ids never collide with the slow half's own ids

# -- header ---------------------------------------------------------------------------------------------------
OFF_MAGIC, OFF_VERSION, OFF_SEQ = 0, 4, 8
OFF_ARMED, OFF_DENY_ALL, OFF_FLAG, OFF_DENY_COUNT, OFF_CHORD_COUNT = 16, 17, 18, 19, 20
OFF_ORCH_PID, OFF_HOOK_PID, OFF_WATCHER_TS, OFF_REINSTALL_REQUEST = 24, 28, 32, 40
OFF_LAST_PROBE, OFF_HOOK_ALIVE, OFF_RING_HEAD = 64, 72, 80
OFF_REINSTALL_DONE, OFF_REINSTALL_OK, OFF_SEND_PASSED = 88, 92, 93
HEADER_SIZE = 128
_HEADER_SLOW = struct.Struct("<BBBBBxxxIIqI")          # @16: armed .. reinstall_request

# -- token slots ----------------------------------------------------------------------------------------------
OFF_TOKENS = HEADER_SIZE
TOKEN_SIZE = 40
_TOKEN_SLOW = struct.Struct("<IBBHiiHxxxxxxq")         # id kind flags bov x y tol .. expires (32 bytes)
_TOKEN_HOOK = struct.Struct("<IBB")                    # @32: state_id state drop_reason
TOKEN_SLOW_SIZE = _TOKEN_SLOW.size
KIND_EMPTY, KIND_BUTTON, KIND_KEY, KIND_WHEEL, KIND_PROBE = 0, 1, 2, 3, 4
TF_DIALOG_OK, TF_SECRET, TF_RELEASE, TF_SEND_OK = 1, 2, 4, 8
STATES = ("armed", "half_consumed", "consumed", "dropped", "expired")
ARMED, HALF_CONSUMED, CONSUMED, DROPPED, EXPIRED = range(5)
LIVE = (ARMED, HALF_CONSUMED)

# -- the rest of the slow half's picture ----------------------------------------------------------------------
OFF_MOVE_ALLOW = OFF_TOKENS + TOKEN_SLOTS * TOKEN_SIZE  # 768
_MOVE_ALLOW = struct.Struct("<Bxxxxxxxqiiii")
OFF_DENY_RECTS = OFF_MOVE_ALLOW + _MOVE_ALLOW.size      # 800
_DENY_RECT = struct.Struct("<iiiiBxxx")
OFF_BLOCKED_VK = OFF_DENY_RECTS + DENY_RECT_SLOTS * _DENY_RECT.size   # 960
BLOCKED_VK_SIZE = 32
OFF_CHORDS = OFF_BLOCKED_VK + BLOCKED_VK_SIZE           # 992
_CHORD = struct.Struct("<BxH")
SLOW_END = OFF_CHORDS + CHORD_SLOTS * _CHORD.size       # 1056
OFF_RING = 1088
_RECORD = struct.Struct("<IBBBBqiiHhI")
SEGMENT_SIZE = OFF_RING + RING_SLOTS * RING_RECORD_SIZE
_U64 = struct.Struct("<Q")
_U32 = struct.Struct("<I")
_I64 = struct.Struct("<q")
_INVALID_SEQ = 0xFFFF_FFFF

# -- codes ----------------------------------------------------------------------------------------------------
EVENT_TYPES = ("control", "move", "button_down", "button_up", "wheel", "key_down", "key_up")
EVENT_CODE = {name: i for i, name in enumerate(EVENT_TYPES)}
ORIGINS = ("console", "emitter", "foreign_process", "drill")
ORIGIN_CODE = {name: i for i, name in enumerate(ORIGINS)}
# 0 and 1 are the allowed decisions; the rest are the hook's 5.4.4 codes, plus the hook host's reinstall log
REASONS = ("emitted", "late_up", "no_token", "token_mismatch_coords", "token_expired", "deny_rect",
           "shift_click_dialog", "dialog_not_ok", "blocked_key_dialog", "blocked_chord_dialog", "move_outside_allow",
           "deny_all", "not_armed", "foreign_input", "hook_reinstalled")
REASON_CODE = {name: i for i, name in enumerate(REASONS)}
BUTTONS = (None, "left", "right", "middle", "x1", "x2")
BUTTON_CODE = {name: i for i, name in enumerate(BUTTONS) if name}
MOD_BITS = {"shift": 1, "ctrl": 2, "alt": 4, "win": 8}
RF_INJECTED, RF_SECRET = 16, 32
FLAG_CLEAR, FLAG_SET, FLAG_UNKNOWN = 0, 1, 2

# The same tables as orchestrator.gateway (5.4.7), restated because this module imports the stdlib only;
# tests/test_gateway_segment.py checks they are equal.
MODIFIER_VK = {0x10: "shift", 0xA0: "shift", 0xA1: "shift", 0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
               0x12: "alt", 0xA4: "alt", 0xA5: "alt", 0x5B: "win", 0x5C: "win"}
CHORD_MODS = frozenset({"ctrl", "alt", "win"})


class SegmentError(RuntimeError):
    """The segment cannot hold what the slow half asked for (no free slot, a value outside the layout)."""


class LayoutMismatch(SegmentError):
    """`magic` or `layout_version` differ: the two halves were built from different harness commits (5.4.2)."""


# -- duck types: what the slow half's picture looks like (orchestrator.gateway.GatewayState) -----------------
class RectLike(Protocol):
    x: int
    y: int
    w: int
    h: int


class TokenLike(Protocol):
    id: int
    kind: str                     # click | key | wheel | release
    x: int
    y: int
    button: str | None
    vk: int | None
    sign: int
    tol_px: int
    expires: float
    dialog_ok: bool
    secret: bool
    send_ok: bool
    state: str
    drop_reason: str | None


class StateLike(Protocol):
    """The parts of `orchestrator.gateway.GatewayState` the segment carries (times in seconds)."""

    armed: bool
    deny_all: bool
    flag: int
    watcher_mono_ts: float
    tokens: Mapping[int, TokenLike]
    move_allow: Any               # None or an object with `until` (seconds) and `bbox` (RectLike)
    deny_rects: Any               # a sequence of objects with `rect` (RectLike) and `label`
    blocked_vk: Iterable[int]
    chords: Any                   # a sequence of objects with `mods` (names) and `vk`


class EventLike(Protocol):
    """One input event as the hook sees it (the fields of `orchestrator.gateway.InputEvent`)."""

    type: str
    x: int
    y: int
    button: str | None
    vk: int | None
    delta: int
    mods: frozenset[str]
    origin: str


class HookEvent(NamedTuple):
    """The hook host's own event value: the fields of `EventLike`, built by the Win32 callback."""

    type: str
    x: int = 0
    y: int = 0
    button: str | None = None
    vk: int | None = None
    delta: int = 0
    mods: frozenset[str] = frozenset()
    origin: str = "emitter"


class Decision(NamedTuple):
    """The callback's answer (as `orchestrator.gateway.HookDecision`): allow, or drop with a 5.4.4 code."""

    allowed: bool
    reason: str | None = None
    token_id: int | None = None
    late_up: bool = False

    @property
    def decision(self) -> str:
        """The 8.3 `gate_decision`: `emitted`, `late_up` or `dropped`."""
        if not self.allowed:
            return "dropped"
        return "late_up" if self.late_up else "emitted"


class TokenSlot(NamedTuple):
    """One token slot as read from the segment; `state` is the effective state (choice 2)."""

    slot: int
    id: int
    kind: int
    flags: int
    button_or_vk: int
    x: int
    y: int
    tol_px: int
    expires_ns: int
    state: int
    drop_reason: int

    @property
    def release(self) -> bool:
        return bool(self.flags & TF_RELEASE)

    @property
    def sign(self) -> int:
        return 1 if self.button_or_vk == 1 else -1


class Header(NamedTuple):
    """The header fields (times in monotonic nanoseconds; INT64_MIN means never stamped)."""

    magic: int
    layout_version: int
    seq: int
    armed: bool
    deny_all: bool
    flag_ticket: int
    orchestrator_pid: int
    hook_host_pid: int
    watcher_mono_ts: int
    reinstall_request: int
    last_probe_mono_ts: int
    hook_alive_mono_ts: int
    ring_head: int
    reinstall_done: int
    reinstall_ok: bool
    send_rect_passed: bool


class RingRecord(NamedTuple):
    """One drop-ring record (5.4.2), decoded."""

    seq: int
    event_type: str
    origin: str
    reason: str
    mods: frozenset[str]
    injected: bool
    secret: bool
    mono_ns: int
    x: int
    y: int
    vk_or_button: int
    aux: int
    token_id: int | None

    @property
    def decision(self) -> str:
        """`emitted`, `late_up`, `dropped`, or `none` for the hook host's own log records (`hook_reinstalled`)."""
        if self.reason in ("emitted", "late_up"):
            return self.reason
        return "none" if self.event_type == "control" else "dropped"

    @property
    def button(self) -> str | None:
        if self.event_type in ("button_down", "button_up") and 0 < self.vk_or_button < len(BUTTONS):
            return BUTTONS[self.vk_or_button]
        return None

    @property
    def vk(self) -> int | None:
        return self.vk_or_button if self.event_type in ("key_down", "key_up") else None


# -- conversions ----------------------------------------------------------------------------------------------
def to_ns(seconds: float) -> int:
    """Seconds on the monotonic clock to the segment's nanoseconds; -inf (never stamped) is INT64_MIN."""
    if seconds == float("-inf"):
        return INT64_MIN
    if seconds == float("inf"):
        return INT64_MAX
    return max(INT64_MIN, min(INT64_MAX, round(seconds * 1e9)))


def to_seconds(ns: int) -> float:
    """The inverse of `to_ns`: INT64_MIN reads back as -inf."""
    return float("-inf") if ns == INT64_MIN else ns / 1e9


def mod_bits(mods: Iterable[str]) -> int:
    """The modifier names as the segment's bit mask (names outside shift, ctrl, alt, win are ignored)."""
    return sum(bit for name, bit in MOD_BITS.items() if name in mods)


def mod_names(bits: int) -> frozenset[str]:
    return frozenset(name for name, bit in MOD_BITS.items() if bits & bit)


# -- the buffer -----------------------------------------------------------------------------------------------
def init_segment(buf: Any, *, orchestrator_pid: int, hook_host_pid: int = 0) -> None:
    """Zero a fresh segment and write its header: not armed, flag unknown, every stamp unset (fails closed)."""
    if len(buf) < SEGMENT_SIZE:
        raise SegmentError(f"segment of {len(buf)} bytes; the layout needs {SEGMENT_SIZE}")
    buf[0:SEGMENT_SIZE] = bytes(SEGMENT_SIZE)
    struct.pack_into("<II", buf, OFF_MAGIC, MAGIC, LAYOUT_VERSION)
    buf[OFF_FLAG] = FLAG_UNKNOWN
    struct.pack_into("<II", buf, OFF_ORCH_PID, orchestrator_pid, hook_host_pid)
    for off in (OFF_WATCHER_TS, OFF_LAST_PROBE, OFF_HOOK_ALIVE):
        _I64.pack_into(buf, off, INT64_MIN)


def layout_ok(buf: Any) -> bool:
    """True when the segment carries this build's `magic` and `layout_version` (5.4.2)."""
    if len(buf) < SEGMENT_SIZE:
        return False
    return struct.unpack_from("<II", buf, OFF_MAGIC) == (MAGIC, LAYOUT_VERSION)


def check_layout(buf: Any) -> None:
    """Raise `LayoutMismatch` unless `layout_ok`."""
    if not layout_ok(buf):
        raise LayoutMismatch("shared segment magic or layout_version differs from this build (5.4.2)")


def read_header(buf: Any) -> Header:
    """The header as it stands (no seqlock: use it for the hook-written fields and the writer's own)."""
    magic, version, seq = struct.unpack_from("<IIQ", buf, OFF_MAGIC)
    armed, deny_all, flag, _dc, _cc, opid, hpid, watcher, req = _HEADER_SLOW.unpack_from(buf, OFF_ARMED)
    last_probe, alive, head = struct.unpack_from("<qqQ", buf, OFF_LAST_PROBE)
    done, ok, passed = struct.unpack_from("<IBB", buf, OFF_REINSTALL_DONE)
    return Header(magic, version, seq, bool(armed), bool(deny_all), flag, opid, hpid, watcher, req, last_probe,
                  alive, head, done, bool(ok), bool(passed))


def _seq(buf: Any) -> int:
    return int.from_bytes(buf[OFF_SEQ:OFF_SEQ + 8], "little")


def read_snapshot(buf: Any, *, max_spins: int = MAX_SPINS,
                  spin: Callable[[], None] | None = None) -> bytes | None:
    """A consistent copy of bytes [0, SLOW_END) under the seqlock, or None after `max_spins` rereads.

    Reread while `seq` is odd (a write is in progress) or changed across the copy (5.4.2: "the hook rereads on
    mismatch"). `spin` runs before every reread. Only slicing is used on `buf`, so any sliceable buffer works."""
    for _ in range(max_spins):
        s1 = _seq(buf)
        if not s1 & 1:
            body = bytes(buf[0:SLOW_END])
            if _seq(buf) == s1:
                return body
        if spin is not None:
            spin()
    return None


def _tokens(snap: bytes) -> list[TokenSlot]:
    """The occupied token slots of a snapshot, in issue (id) order (choice 3)."""
    out = []
    for i in range(TOKEN_SLOTS):
        off = OFF_TOKENS + i * TOKEN_SIZE
        tid, kind, flags, bov, x, y, tol, expires = _TOKEN_SLOW.unpack_from(snap, off)
        if tid == 0 or kind == KIND_EMPTY:
            continue
        state_id, state, reason = _TOKEN_HOOK.unpack_from(snap, off + TOKEN_SLOW_SIZE)
        if state_id != tid:
            state, reason = ARMED, 0
        out.append(TokenSlot(i, tid, kind, flags, bov, x, y, tol, expires, state, reason))
    out.sort(key=lambda t: t.id)
    return out


def read_tokens(buf: Any) -> list[TokenSlot]:
    """The occupied token slots with their effective states, read under the seqlock (empty if it never settles)."""
    snap = read_snapshot(buf)
    return [] if snap is None else _tokens(snap)


def token_states(buf: Any) -> dict[int, tuple[str, str | None]]:
    """`{token id: (state, drop reason or None)}` as the hook left them, for stage 7 of 5.4.3."""
    return {t.id: (STATES[t.state], REASONS[t.drop_reason] if t.drop_reason else None) for t in read_tokens(buf)}


def apply_token_states(buf: Any, state: StateLike) -> None:
    """Copy the hook's token states into the slow half's picture (`GatewayState.tokens`), for stage 7."""
    for tid, (st, reason) in token_states(buf).items():
        tok = state.tokens.get(tid)
        if tok is not None:
            tok.state, tok.drop_reason = st, reason


# -- the slow half's writer -----------------------------------------------------------------------------------
class SegmentWriter:
    """The slow half's side of the segment: every write goes through the seqlock (5.4.2).

    `publish(state)` writes a whole `GatewayState`-like picture; `issue_probe` writes the probe token of 5.4.4;
    `request_reinstall` asks the hook host to reinstall both hooks (choice 1)."""

    def __init__(self, buf: Any):
        check_layout(buf)
        self.buf = buf
        self._probe_id = 0
        self._probes = 0
        self._published: set[int] = set()

    @contextmanager
    def transaction(self):
        """seq to odd, the body writes, seq to even; an exception leaves seq odd so the hook fails closed."""
        seq = _U64.unpack_from(self.buf, OFF_SEQ)[0]
        if not seq & 1:
            seq += 1
            _U64.pack_into(self.buf, OFF_SEQ, seq)
        yield self.buf
        _U64.pack_into(self.buf, OFF_SEQ, seq + 1)

    def set_pids(self, *, orchestrator_pid: int, hook_host_pid: int) -> None:
        with self.transaction() as b:
            struct.pack_into("<II", b, OFF_ORCH_PID, orchestrator_pid, hook_host_pid)

    def request_reinstall(self) -> int:
        """Ask the hook host to reinstall both hooks (5.4.4); returns the request number it will answer."""
        req = _U32.unpack_from(self.buf, OFF_REINSTALL_REQUEST)[0] + 1
        with self.transaction() as b:
            _U32.pack_into(b, OFF_REINSTALL_REQUEST, req)
        return req

    def _slot_ids(self) -> list[int]:
        return [_U32.unpack_from(self.buf, OFF_TOKENS + i * TOKEN_SIZE)[0] for i in range(TOKEN_SLOTS)]

    def _free_slot(self, ids: list[int], keep: set[int]) -> int:
        for i, tid in enumerate(ids):
            if tid == 0 or tid not in keep:
                return i
        raise SegmentError(f"all {TOKEN_SLOTS} token slots hold live tokens")

    def publish(self, state: StateLike) -> None:
        """Write the slow half's picture: armed, deny_all, flag and Watcher stamp, the tokens (never their
        states), move_allow, deny rectangles, blocked keys and chords. Times in `state` are seconds."""
        rects = list(state.deny_rects)
        chords = list(dict.fromkeys((mod_bits(c.mods), c.vk) for c in state.chords))
        if len(rects) > DENY_RECT_SLOTS:
            raise SegmentError(f"{len(rects)} deny rectangles; the segment holds {DENY_RECT_SLOTS}")
        if len(chords) > CHORD_SLOTS:
            raise SegmentError(f"{len(chords)} chords; the segment holds {CHORD_SLOTS}")
        tokens = dict(state.tokens)
        ids = self._slot_ids()
        keep = set(tokens) | ({self._probe_id} if self._probe_id else set())
        for tid in tokens:
            if tid & PROBE_ID_BIT or tid <= 0:
                raise SegmentError(f"token id {tid} is outside the slow half's id space")
            if tid not in ids:
                i = self._free_slot(ids, keep)
                ids[i] = tid
                keep.add(tid)
        slots = [_TOKEN_SLOW.pack(*_encode_token(tokens[tid])) if tid in tokens else None for tid in ids]
        ma = state.move_allow
        tail = bytearray(SLOW_END - OFF_MOVE_ALLOW)
        if ma is not None:
            _MOVE_ALLOW.pack_into(tail, 0, 1, to_ns(ma.until), ma.bbox.x, ma.bbox.y, ma.bbox.w, ma.bbox.h)
        for i, d in enumerate(rects):
            r = d.rect
            _DENY_RECT.pack_into(tail, OFF_DENY_RECTS - OFF_MOVE_ALLOW + i * _DENY_RECT.size, r.x, r.y, r.w, r.h,
                                 int(str(d.label).strip().lower() == "send"))
        for vk in state.blocked_vk:
            if not 0 <= vk < 256:
                raise SegmentError(f"blocked vk {vk} is outside the 256-bit map")
            tail[OFF_BLOCKED_VK - OFF_MOVE_ALLOW + (vk >> 3)] |= 1 << (vk & 7)
        for i, (bits, vk) in enumerate(chords):
            _CHORD.pack_into(tail, OFF_CHORDS - OFF_MOVE_ALLOW + i * _CHORD.size, bits, vk)
        with self.transaction() as b:
            struct.pack_into("<BBB", b, OFF_ARMED, int(state.armed), int(state.deny_all), int(state.flag))
            struct.pack_into("<BB", b, OFF_DENY_COUNT, len(rects), len(chords))
            _I64.pack_into(b, OFF_WATCHER_TS, to_ns(state.watcher_mono_ts))
            for i, packed in enumerate(slots):
                off = OFF_TOKENS + i * TOKEN_SIZE
                if packed is not None:
                    b[off:off + TOKEN_SLOW_SIZE] = packed
                elif ids[i] != self._probe_id:
                    b[off:off + TOKEN_SLOW_SIZE] = bytes(TOKEN_SLOW_SIZE)
            b[OFF_MOVE_ALLOW:SLOW_END] = tail
        self._published = set(tokens)

    def issue_probe(self, x: int, y: int, now_ns: int, *, expiry_ns: int = 500_000_000) -> int:
        """Write the tokened probe move of 5.4.4 at the parking coordinate; the emitter then sends the move.
        One probe slot at a time: a new probe replaces the last one. Returns the probe's token id."""
        self._probes += 1
        pid = PROBE_ID_BIT | self._probes
        ids = self._slot_ids()
        if self._probe_id and self._probe_id in ids:
            slot = ids.index(self._probe_id)
        else:                    # never a slot of a published token: re-placing one would re-arm it (choice 2)
            slot = self._free_slot(ids, self._published)
        with self.transaction() as b:
            _TOKEN_SLOW.pack_into(b, OFF_TOKENS + slot * TOKEN_SIZE, pid, KIND_PROBE, 0, 0, x, y, 2, now_ns + expiry_ns)
        self._probe_id = pid
        return pid


def _encode_token(t: TokenLike) -> tuple:
    flags = (TF_DIALOG_OK if t.dialog_ok else 0) | (TF_SECRET if t.secret else 0) | (TF_SEND_OK if t.send_ok else 0)
    if t.kind in ("click", "release") and t.button is not None and t.button not in BUTTON_CODE:
        raise SegmentError(f"button {t.button!r}; the segment knows {sorted(BUTTON_CODE)}")
    if t.kind == "click":
        kind, bov = KIND_BUTTON, BUTTON_CODE.get(t.button or "", 0)
    elif t.kind == "key":
        kind, bov = KIND_KEY, t.vk or 0
    elif t.kind == "wheel":
        kind, bov = KIND_WHEEL, 1 if t.sign > 0 else 2
    elif t.kind == "release":
        flags |= TF_RELEASE
        kind, bov = (KIND_BUTTON, BUTTON_CODE[t.button]) if t.button else (KIND_KEY, t.vk or 0)
    else:
        raise SegmentError(f"token kind {t.kind!r}")
    return (t.id, kind, flags, bov, t.x, t.y, t.tol_px, to_ns(t.expires))


# -- the hook's writes ----------------------------------------------------------------------------------------
def stamp_alive(buf: Any, now_ns: int) -> None:
    """`hook_alive_mono_ts`, stamped every 250 ms from the hook host's message loop (5.4.2)."""
    _I64.pack_into(buf, OFF_HOOK_ALIVE, now_ns)


def answer_reinstall(buf: Any, request: int, ok: bool) -> None:
    """The hook host's answer to a reinstall request (choice 1): `ok` first, then the request number."""
    buf[OFF_REINSTALL_OK] = int(ok)
    _U32.pack_into(buf, OFF_REINSTALL_DONE, request)


def append_record(buf: Any, *, event_type: str, origin: str, reason: str, mono_ns: int, mods: Iterable[str] = (),
                  secret: bool = False, x: int = 0, y: int = 0, vk_or_button: int = 0, aux: int = 0,
                  token_id: int | None = None) -> int:
    """Write one ring record at `ring_head` and advance it (the hook only). The record's seq is invalidated
    first and written last, so a reader the hook laps sees the change. Returns the record's sequence number."""
    head = _U64.unpack_from(buf, OFF_RING_HEAD)[0]
    off = OFF_RING + (head % RING_SLOTS) * RING_RECORD_SIZE
    flags = mod_bits(mods) | (RF_INJECTED if origin != "console" else 0) | (RF_SECRET if secret else 0)
    _U32.pack_into(buf, off, _INVALID_SEQ)
    struct.pack_into("<BBBBqiiHhI", buf, off + 4, EVENT_CODE[event_type], ORIGIN_CODE[origin], REASON_CODE[reason],
                     flags, mono_ns, _i32(x), _i32(y), vk_or_button & 0xFFFF, max(-32768, min(32767, aux)),
                     token_id or 0)
    _U32.pack_into(buf, off, head & 0xFFFF_FFFF)
    _U64.pack_into(buf, OFF_RING_HEAD, head + 1)
    return head


def _i32(v: int) -> int:
    return max(-(2 ** 31), min(2 ** 31 - 1, v))


class RingReader:
    """The drainer's cursor over the drop ring: records in seq order, counting those the hook overwrote."""

    def __init__(self, cursor: int = 0):
        self.cursor = cursor
        self.lost = 0

    def read(self, buf: Any) -> list[RingRecord]:
        head = _U64.unpack_from(buf, OFF_RING_HEAD)[0]
        if head - self.cursor > RING_SLOTS:
            self.lost += head - self.cursor - RING_SLOTS
            self.cursor = head - RING_SLOTS
        out = []
        while self.cursor < head:
            off = OFF_RING + (self.cursor % RING_SLOTS) * RING_RECORD_SIZE
            raw = bytes(buf[off:off + RING_RECORD_SIZE])
            want = self.cursor & 0xFFFF_FFFF
            if _U32.unpack_from(raw, 0)[0] == want and _U32.unpack_from(buf, off)[0] == want:
                out.append(decode_record(raw))
            else:
                self.lost += 1
            self.cursor += 1
        return out


def decode_record(raw: bytes) -> RingRecord:
    seq, ev, origin, reason, flags, mono, x, y, bov, aux, tid = _RECORD.unpack(raw)
    return RingRecord(seq, EVENT_TYPES[ev], ORIGINS[origin], REASONS[reason], mod_names(flags),
                      bool(flags & RF_INJECTED), bool(flags & RF_SECRET), mono, x, y, bov, aux, tid or None)


# -- the fast half --------------------------------------------------------------------------------------------
def _near(t: TokenSlot, e: EventLike) -> bool:
    return abs(t.x - e.x) <= t.tol_px and abs(t.y - e.y) <= t.tol_px


def _inside(x: int, y: int, rx: int, ry: int, rw: int, rh: int) -> bool:
    return rx <= x < rx + rw and ry <= y < ry + rh


def decide_from_segment(buf: Any, e: EventLike, now_ns: int, *, deny_all: bool = False) -> Decision:
    """The hook callback (5.4.4; 6.2 rules 1 to 4) over the segment: read the slow half's picture under the
    seqlock, decide as `orchestrator.gateway.hook_decide` does, write the token's new state and at most one
    ring record. `deny_all` is the hook host's own latch (orchestrator gone, layout mismatch), ORed with the
    segment's flag. `buf` must be writable."""
    snap = read_snapshot(buf)
    if snap is None:                                                         # choice 4
        return _commit(buf, e, now_ns, Decision(False, "deny_all"), None, None)
    armed, seg_deny_all, flag, deny_count, chord_count, _o, _h, watcher_ns, _r = _HEADER_SLOW.unpack_from(snap, 16)
    toks = _tokens(snap)
    probe = None
    if e.type == "move" and e.origin == "emitter":
        probe = next((t for t in toks if t.kind == KIND_PROBE and t.state == ARMED and _near(t, e)
                      and now_ns <= t.expires_ns), None)
        if probe is not None:                                                # choice 2: answer the probe
            _I64.pack_into(buf, OFF_LAST_PROBE, now_ns)
    if deny_all or seg_deny_all:                                             # rule 1
        return _commit(buf, e, now_ns, Decision(False, "deny_all", probe.id if probe else None), probe, CONSUMED)
    if not armed:
        return _commit(buf, e, now_ns, Decision(False, "not_armed", probe.id if probe else None), probe, CONSUMED)
    if e.origin == "console":                                                # rule 4
        return _commit(buf, e, now_ns, Decision(False, "foreign_input"), None, None)
    if now_ns - watcher_ns > WATCHER_STALE_NS:
        flag = FLAG_UNKNOWN
    if flag != FLAG_CLEAR:                                                   # rule 2, regardless of token
        if e.type == "key_down":
            mods = set(e.mods)
            if e.vk in MODIFIER_VK:
                mods.add(MODIFIER_VK[e.vk])
            if mods & CHORD_MODS or (set(e.mods) <= MOD_BITS.keys()
                                     and (mod_bits(e.mods), e.vk) in _chords(snap, chord_count)):
                return _commit(buf, e, now_ns, Decision(False, "blocked_chord_dialog"), None, None)
            if e.vk is not None and 0 <= e.vk < 256 and snap[OFF_BLOCKED_VK + (e.vk >> 3)] & (1 << (e.vk & 7)):
                return _commit(buf, e, now_ns, Decision(False, "blocked_key_dialog"), None, None)
        if e.type == "button_down" and "shift" in e.mods:
            return _commit(buf, e, now_ns, Decision(False, "shift_click_dialog"), None, None)
    tokened = e.origin == "emitter"                                          # rule 3
    if e.type == "move":
        active, until, bx, by, bw, bh = _MOVE_ALLOW.unpack_from(snap, OFF_MOVE_ALLOW)
        if active and now_ns <= until and _inside(e.x, e.y, bx, by, bw, bh):
            return _commit(buf, e, now_ns, Decision(True, token_id=probe.id if probe else None), probe, CONSUMED)
        if not any(t.state in LIVE and not t.release and t.kind != KIND_PROBE for t in toks):
            return _commit(buf, e, now_ns, Decision(True, token_id=probe.id if probe else None), probe, CONSUMED)
        return _commit(buf, e, now_ns, Decision(False, "move_outside_allow", probe.id if probe else None), probe,
                       CONSUMED)
    if not tokened:
        toks = []
    if e.type == "button_down":
        code = BUTTON_CODE.get(e.button or "", -1)
        cands = [t for t in toks if t.kind == KIND_BUTTON and not t.release and t.button_or_vk == code
                 and t.state == ARMED]
        if not cands:
            return _commit(buf, e, now_ns, Decision(False, "no_token"), None, None)
        near = [t for t in cands if _near(t, e)]
        if not near:
            return _commit(buf, e, now_ns, Decision(False, "token_mismatch_coords"), None, None)
        t = near[0]
        if now_ns > t.expires_ns:
            return _commit(buf, e, now_ns, Decision(False, "token_expired", t.id), t, EXPIRED)
        for i in range(deny_count):
            rx, ry, rw, rh, is_send = _DENY_RECT.unpack_from(snap, OFF_DENY_RECTS + i * _DENY_RECT.size)
            if _inside(e.x, e.y, rx, ry, rw, rh):
                if t.flags & TF_SEND_OK and is_send and not buf[OFF_SEND_PASSED]:
                    buf[OFF_SEND_PASSED] = 1                                 # 6.7: once
                    continue
                return _commit(buf, e, now_ns, Decision(False, "deny_rect", t.id), t, DROPPED)
        if flag != FLAG_CLEAR and not t.flags & TF_DIALOG_OK:
            return _commit(buf, e, now_ns, Decision(False, "dialog_not_ok", t.id), t, DROPPED)
        return _commit(buf, e, now_ns, Decision(True, token_id=t.id), t, HALF_CONSUMED)
    if e.type in ("button_up", "key_up"):
        kind = KIND_BUTTON if e.type == "button_up" else KIND_KEY
        val = BUTTON_CODE.get(e.button or "", -1) if e.type == "button_up" else e.vk
        for t in toks:
            if t.kind == kind and not t.release and t.state == HALF_CONSUMED and t.button_or_vk == val:
                return _commit(buf, e, now_ns, Decision(True, token_id=t.id, late_up=now_ns > t.expires_ns), t,
                               CONSUMED)
        for t in toks:
            if t.kind == kind and t.release and t.state == ARMED and t.button_or_vk == val:
                return _commit(buf, e, now_ns, Decision(True, token_id=t.id), t, CONSUMED)
        return _commit(buf, e, now_ns, Decision(False, "no_token"), None, None)
    if e.type == "wheel":
        sign = 1 if e.delta > 0 else -1
        cands = [t for t in toks if t.kind == KIND_WHEEL and t.state == ARMED and t.sign == sign
                 and now_ns <= t.expires_ns]
        if not cands:
            return _commit(buf, e, now_ns, Decision(False, "no_token"), None, None)
        near = [t for t in cands if _near(t, e)]
        if not near:
            return _commit(buf, e, now_ns, Decision(False, "token_mismatch_coords"), None, None)
        return _commit(buf, e, now_ns, Decision(True, token_id=near[0].id), near[0], CONSUMED)
    # key_down
    cands = [t for t in toks if t.kind == KIND_KEY and not t.release and t.button_or_vk == e.vk and t.state == ARMED]
    if not cands:
        return _commit(buf, e, now_ns, Decision(False, "no_token"), None, None)
    t = cands[0]
    if now_ns > t.expires_ns:
        return _commit(buf, e, now_ns, Decision(False, "token_expired", t.id), t, EXPIRED)
    return _commit(buf, e, now_ns, Decision(True, token_id=t.id), t, HALF_CONSUMED)


def _chords(snap: bytes, count: int) -> set[tuple[int, int]]:
    return {_CHORD.unpack_from(snap, OFF_CHORDS + i * _CHORD.size) for i in range(count)}


def _commit(buf: Any, e: EventLike, now_ns: int, d: Decision, t: TokenSlot | None, new_state: int | None) -> Decision:
    """Write the token's new state (state, then state_id; choice 2) and the event's ring record (choice 5)."""
    if t is not None and new_state is not None:
        off = OFF_TOKENS + t.slot * TOKEN_SIZE + TOKEN_SLOW_SIZE
        reason = REASON_CODE[d.reason] if new_state in (DROPPED, EXPIRED) and d.reason else 0
        buf[off + 4] = new_state
        buf[off + 5] = reason
        _U32.pack_into(buf, off, t.id)
    if d.allowed and d.token_id is None:
        return d
    if e.type in ("button_down", "button_up"):
        bov = BUTTON_CODE.get(e.button or "", 0)
    else:
        bov = e.vk or 0
    if e.type == "wheel":
        aux = e.delta
    elif d.late_up and t is not None:
        aux = (now_ns - t.expires_ns) // 1_000_000
    else:
        aux = 0
    append_record(buf, event_type=e.type, origin=e.origin, reason=d.decision if d.allowed else d.reason or "no_token",
                  mono_ns=now_ns, mods=e.mods, secret=bool(t is not None and t.flags & TF_SECRET), x=e.x, y=e.y,
                  vk_or_button=bov, aux=aux, token_id=d.token_id)
    return d


# -- shared memory --------------------------------------------------------------------------------------------
class Segment:
    """The segment over `multiprocessing.shared_memory` (a named file mapping on Windows), or over any buffer."""

    def __init__(self, buf: Any, shm: Any = None):
        self.buf = buf
        self._shm = shm

    @classmethod
    def create(cls, name: str | None = None, *, orchestrator_pid: int, hook_host_pid: int = 0) -> "Segment":
        """Create and initialise a named segment (the Orchestrator, before it launches the hook host)."""
        from multiprocessing import shared_memory

        shm = shared_memory.SharedMemory(name=name, create=True, size=SEGMENT_SIZE)
        init_segment(shm.buf, orchestrator_pid=orchestrator_pid, hook_host_pid=hook_host_pid)
        return cls(shm.buf, shm)

    @classmethod
    def attach(cls, name: str) -> "Segment":
        """Attach to an existing segment (the hook host). The layout is checked by the caller (`layout_ok`)."""
        from multiprocessing import shared_memory

        if sys.version_info >= (3, 13):
            shm = shared_memory.SharedMemory(name=name, track=False)
        else:                                                                # pragma: no cover - Python 3.12
            shm = shared_memory.SharedMemory(name=name)
            if sys.platform != "win32":
                from multiprocessing import resource_tracker
                resource_tracker.unregister(shm._name, "shared_memory")     # the creator owns the unlink
        return cls(shm.buf, shm)

    @property
    def name(self) -> str | None:
        return self._shm.name if self._shm is not None else None

    def close(self) -> None:
        self.buf = None
        if self._shm is not None:
            self._shm.close()

    def unlink(self) -> None:
        if self._shm is not None:
            self._shm.unlink()
