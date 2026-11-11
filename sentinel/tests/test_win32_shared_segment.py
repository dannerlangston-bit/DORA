"""Tests for sentinel.win32.shared_segment: the SegmentPort over real shared memory."""
import struct

import pytest

from sentinel.gateway import segment as sg
from sentinel.orchestrator.gateway import Flag, GatewayState
from sentinel.orchestrator.ports import SegmentPort
from sentinel.win32.shared_segment import SharedSegment


@pytest.fixture
def seg():
    s = SharedSegment.create(orchestrator_pid=4242)
    yield s
    s.close()
    s.unlink()


def test_it_is_a_segment_port_shared_by_name(seg):
    assert isinstance(seg, SegmentPort)
    g = GatewayState(armed=True)
    g.stamp_watcher(Flag.CLEAR, 100.0)
    g.issue_key(0x41, 100.0)
    sg.SegmentWriter(seg.buf).publish(g)
    other = SharedSegment.attach(seg.name)
    try:
        snap = other.read()
        assert sg.layout_ok(snap) and sg.read_header(snap).orchestrator_pid == 4242
        assert [t.button_or_vk for t in sg.read_tokens(snap)] == [0x41]
        other.write(sg.OFF_HOOK_ALIVE, struct.pack("<q", 123))
        assert sg.read_header(seg.buf).hook_alive_mono_ts == 123
    finally:
        other.close()


def test_writes_stay_inside_the_segment(seg):
    seg.write(sg.SEGMENT_SIZE - 4, b"\x01\x02\x03\x04")
    assert bytes(seg.read()[sg.SEGMENT_SIZE - 4:sg.SEGMENT_SIZE]) == b"\x01\x02\x03\x04"
    with pytest.raises(ValueError, match="outside"):
        seg.write(sg.SEGMENT_SIZE - 2, b"\x00\x00\x00")
    with pytest.raises(ValueError, match="outside"):
        seg.write(-1, b"\x00")


def test_a_seqlock_left_odd_is_never_read_as_a_picture(seg):
    struct.pack_into("<Q", seg.buf, sg.OFF_SEQ, 7)                  # a writer died mid-write
    with pytest.raises(sg.SegmentError, match="never settled"):
        seg.read()
    struct.pack_into("<Q", seg.buf, sg.OFF_SEQ, 8)
    assert len(seg.read()) >= sg.SEGMENT_SIZE
