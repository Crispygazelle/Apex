"""Timestamp-ordered reassembly of readings from independent sensor threads.

Three threads read at 100 Hz, ~10 Hz, and audio rate, and hand results to one
queue. Thread scheduling means a GPS fix stamped at t=1.000 can arrive after an
IMU sample stamped t=1.004. Feeding that to the filter out of order produces a
negative dt and a corrupted covariance.

So readings are held briefly in a heap and released only once they are older
than a watermark trailing the newest arrival. This is the reorder logic from
the original `daemon.py`, lifted out of the threading code into something with
no I/O, which makes it directly testable.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from dataclasses import dataclass

from app.models import SensorReading


@dataclass
class SyncStats:
    """Counters worth watching: steady growth in either is a real problem."""

    accepted: int = 0
    dropped_late: int = 0
    emitted: int = 0
    max_depth: int = 0


class FrameSynchronizer:
    """Buffers readings for `reorder_window_s`, then emits them in order."""

    def __init__(self, reorder_window_s: float = 0.3) -> None:
        self.reorder_window_s = max(0.0, reorder_window_s)
        self._heap: list[tuple[float, int, SensorReading]] = []
        self._sequence = 0
        self._newest_timestamp = 0.0
        self._last_emitted = 0.0
        self.stats = SyncStats()

    def __len__(self) -> int:
        return len(self._heap)

    @property
    def watermark(self) -> float:
        """Readings at or below this timestamp are safe to release."""
        return self._newest_timestamp - self.reorder_window_s

    def push(self, reading: SensorReading) -> bool:
        """Accept a reading. Returns False if it arrived too late to be useful."""
        if reading.timestamp < self._last_emitted:
            # We already released a later reading; admitting this one would
            # push a negative dt into the filter.
            self.stats.dropped_late += 1
            return False

        self._sequence += 1
        heapq.heappush(self._heap, (reading.timestamp, self._sequence, reading))
        self._newest_timestamp = max(self._newest_timestamp, reading.timestamp)
        self.stats.accepted += 1
        self.stats.max_depth = max(self.stats.max_depth, len(self._heap))
        return True

    def drain(self, *, flush: bool = False) -> Iterator[SensorReading]:
        """Yield every reading that has cleared the watermark, oldest first.

        With `flush=True` the window is ignored and everything is released,
        which is what shutdown wants so no ride data is lost.
        """
        watermark = self.watermark
        while self._heap:
            timestamp = self._heap[0][0]
            if not flush and timestamp > watermark:
                break
            _, _, reading = heapq.heappop(self._heap)
            self._last_emitted = reading.timestamp
            self.stats.emitted += 1
            yield reading

    def reset(self) -> None:
        self._heap.clear()
        self._sequence = 0
        self._newest_timestamp = 0.0
        self._last_emitted = 0.0
