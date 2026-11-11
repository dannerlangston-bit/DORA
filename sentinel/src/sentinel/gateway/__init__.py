"""The Input Gateway's process-boundary code (5.4.1, 5.4.2, 5.4.4, 5.4.8).

- `segment`: the shared-memory segment of 5.4.2 (layout, seqlock, token slots, deny rectangles, blocked keys and
  chords, the drop ring) and `decide_from_segment`, the fast half's table of 5.4.4 read from those bytes.
- `hookhost_loop`: the hook host's control loop (5.4.2 fail-closed rules) behind an injectable OS facade.
- `liveness`: the hook liveness probe and the reinstall rule of 5.4.4.
- `drainer`: the drop-log drainer of 5.4.8 and 6.2 (`step_events` rows and `gateway_drops.jsonl`).

`segment` and `hookhost_loop` import the standard library only, because the hook host is "a separate Python
process importing only ctypes, mmap and the standard library" (5.4.2); this package therefore imports nothing
on import, so `import sentinel.gateway.segment` stays that light.
"""
