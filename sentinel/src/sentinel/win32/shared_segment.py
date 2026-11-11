"""`ports.SegmentPort` over the named shared-memory segment of 5.4.2 (a named file mapping on Windows,
`multiprocessing.shared_memory` underneath, so it also runs elsewhere).

The Orchestrator creates the segment before it launches the hook host (`SharedSegment.create`), the hook host attaches
by name (`gateway.hookhost_loop.main`). The codec (`gateway.segment`: layout, seqlock, tokens, ring, the hook's
decision) works on the segment's buffer; this port is the byte-level boundary the protocol names:

- `read()`: a copy of the whole segment, taken while `seq` is even and unchanged across the copy, so the slow half's
  fields in it are one consistent picture (the hook's fields have one writer each and need no lock, 5.4.2).
- `write(offset, data)`: bytes at an offset, inside the segment; the seqlock around a write is the codec's
  (`segment.SegmentWriter.transaction`), not the port's.

Choice (reported): a seqlock that never settles within `segment.MAX_SPINS` copies raises `segment.SegmentError`
rather than returning a torn copy (the writer died mid-write; the hook fails closed on the same condition,
`segment` choice 4).

Verified here, on real shared memory: create, attach, read and write, the bounds and the torn-copy refusal. Only
Phase 0 on a rig verifies the mapping between two processes of different integrity on Windows (H2).
"""
from __future__ import annotations

from typing import Any

from ..gateway import segment as sg


class SharedSegment:
    """`ports.SegmentPort` over a `gateway.segment.Segment` (module docstring)."""

    def __init__(self, segment: sg.Segment):
        self.segment = segment

    @classmethod
    def create(cls, name: str | None = None, *, orchestrator_pid: int, hook_host_pid: int = 0) -> SharedSegment:
        """Create and initialise the named segment (the Orchestrator, before it launches the hook host)."""
        return cls(sg.Segment.create(name, orchestrator_pid=orchestrator_pid, hook_host_pid=hook_host_pid))

    @classmethod
    def attach(cls, name: str) -> SharedSegment:
        return cls(sg.Segment.attach(name))

    @property
    def name(self) -> str | None:
        return self.segment.name

    @property
    def buf(self) -> Any:
        """The live buffer, for the codec's functions."""
        return self.segment.buf

    def read(self) -> bytes:
        buf = self.segment.buf
        for _ in range(sg.MAX_SPINS):
            seq = int.from_bytes(buf[sg.OFF_SEQ:sg.OFF_SEQ + 8], "little")
            if not seq & 1:
                body = bytes(buf)
                if int.from_bytes(buf[sg.OFF_SEQ:sg.OFF_SEQ + 8], "little") == seq:
                    return body
        raise sg.SegmentError("the segment's seqlock never settled: a writer stopped mid-write")

    def write(self, offset: int, data: bytes) -> None:
        if offset < 0 or offset + len(data) > sg.SEGMENT_SIZE:
            raise ValueError(f"write of {len(data)} bytes at {offset} outside the {sg.SEGMENT_SIZE}-byte segment")
        self.segment.buf[offset:offset + len(data)] = data

    def close(self) -> None:
        self.segment.close()

    def unlink(self) -> None:
        self.segment.unlink()
