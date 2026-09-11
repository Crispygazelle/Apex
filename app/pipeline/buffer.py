"""In-memory ring buffer holding the most recent telemetry.

This is the node's short-term memory and it serves three readers:

  - crash detection, which needs the speed from a couple of seconds *before*
    an impact to decide whether the impact was real
  - the dashboard, which wants recent history to draw a trace on first load
  - the batch writer, which needs a survivable window if InfluxDB is down

Eviction is by age rather than count, so the 60-second guarantee holds even if
the output rate changes. A count cap is kept as a hard memory bound: 60 s at
50 Hz is 3000 samples, a few megabytes, which is affordable inside the Pi Zero
2 W's 512 MB but only if it cannot grow without limit.
"""

from __future__ import annotations

from collections import deque

from app.models import RideSample

# Headroom over the nominal count so a burst does not evict live data early.
COUNT_SAFETY_FACTOR = 1.5


class TelemetryBuffer:
    """Age-bounded deque of `RideSample`, newest last."""

    def __init__(self, seconds: float = 60.0, expected_rate_hz: float = 50.0) -> None:
        self.seconds = max(1.0, seconds)
        max_count = int(self.seconds * max(expected_rate_hz, 1.0) * COUNT_SAFETY_FACTOR)
        self._samples: deque[RideSample] = deque(maxlen=max(max_count, 64))

    def __len__(self) -> int:
        return len(self._samples)

    @property
    def capacity(self) -> int:
        return self._samples.maxlen or 0

    def append(self, sample: RideSample) -> None:
        self._samples.append(sample)
        self._evict_old(sample.timestamp)

    def _evict_old(self, now: float) -> None:
        cutoff = now - self.seconds
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()

    def latest(self) -> RideSample | None:
        return self._samples[-1] if self._samples else None

    def oldest(self) -> RideSample | None:
        return self._samples[0] if self._samples else None

    def span_seconds(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        return self._samples[-1].timestamp - self._samples[0].timestamp

    def snapshot(self) -> list[RideSample]:
        """Copy of everything held, safe to iterate while the buffer mutates."""
        return list(self._samples)

    def window(self, seconds: float) -> list[RideSample]:
        """Samples from the last `seconds`, oldest first."""
        if not self._samples:
            return []
        cutoff = self._samples[-1].timestamp - max(0.0, seconds)
        return [s for s in self._samples if s.timestamp >= cutoff]

    def sample_at(self, seconds_ago: float) -> RideSample | None:
        """Nearest sample to `seconds_ago`, or None if history is too short.

        Crash detection uses this to ask "how fast were we two seconds ago?"
        """
        if not self._samples:
            return None

        target = self._samples[-1].timestamp - max(0.0, seconds_ago)
        if self._samples[0].timestamp > target:
            return None

        best: RideSample | None = None
        best_delta = float("inf")
        for sample in self._samples:
            delta = abs(sample.timestamp - target)
            if delta < best_delta:
                best, best_delta = sample, delta
            elif sample.timestamp > target:
                break  # sorted by time, so we have passed the target
        return best

    def peak_g_force(self, seconds: float) -> float:
        window = self.window(seconds)
        return max((s.metrics.g_force for s in window), default=0.0)

    def clear(self) -> None:
        self._samples.clear()
