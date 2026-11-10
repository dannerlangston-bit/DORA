"""The injectable clock of the Orchestrator and its safety controls (5.2, 5.4.2, 10.2).

Two time bases, as the spec keeps them apart: monotonic seconds for every staleness, expiry and
budget rule (token expiry 5.4.5, heartbeat and screenshot staleness 10.2, the network pause of
10.7), so a wall-clock correction can neither fake nor hide staleness; and an aware UTC wall clock
for deadlines that are calendar times (the dead-man deadline of 10.2, the run record timestamps).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """What every control reads time from."""

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards (the `mono_ts` of 10.2)."""

    def now(self) -> datetime:
        """The aware UTC wall time."""

    def sleep(self, seconds: float) -> None:
        """Wait (the 1 s between paper-mode reads, 6.4; the heartbeat retry, 10.2)."""


class SystemClock:
    """The real clock: `time.monotonic`, UTC wall time and `time.sleep`."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ManualClock:
    """A clock that moves only when told to, for tests and the simulated rig. `sleep` advances it."""

    def __init__(self, start: datetime | None = None, mono: float = 1000.0):
        self._wall = start or datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc)
        self._mono = mono

    def monotonic(self) -> float:
        return self._mono

    def now(self) -> datetime:
        return self._wall

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock never runs backwards")
        self._mono += seconds
        self._wall += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)
