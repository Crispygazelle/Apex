"""Record raw sensor readings, and replay them back through the real pipeline.

This exists because a live demo should not depend on a simulator running in
real time, and because "it worked when I ran it" is not a claim worth making
about a twelve minute ride.

What is recorded is *raw sensor output*, not fused telemetry. Replay therefore
re-runs calibration, the Kalman filter, metrics, crash detection and the event
log for real; nothing is played back as a finished answer. The only way to get
a different result from a recording is to change the code that interprets it,
which is exactly the property a regression test wants.

Audio is never recorded. The microphone carries speech, a recording on disk is
a privacy liability, and the voice layer has its own scripted path for demos.

Speed multipliers scale the *waiting*, not the timestamps. A reading keeps the
gap it was recorded with, so dt reaching the filter is unchanged and the physics
replays exactly; only the wall-clock sleep between readings shrinks. The visible
consequence is that telemetry timestamps advance faster than `clock.now()`
during a fast replay, so anything driven by real time (an SOS countdown) stays
at its true duration while the ride rushes past.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import clock
from app.models import SensorReading, SensorSource
from app.sensors.base import Sensor, SensorError

logger = logging.getLogger(__name__)

RECORDING_VERSION = 1
# Sources worth persisting. MIC is deliberately absent.
RECORDED_SOURCES = (SensorSource.IMU, SensorSource.GPS)


@dataclass
class RecordingMeta:
    """Header line of a recording: everything replay needs but cannot derive."""

    version: int = RECORDING_VERSION
    ride_id: str = ""
    recorded_at: float = 0.0
    route_name: str = ""
    destination_name: str = ""
    destination_latitude: float = 0.0
    destination_longitude: float = 0.0
    route_length_m: float = 0.0
    # Distance-triggered demo dialogue, carried so a replay is self-contained
    # rather than depending on whichever profile happened to produce it.
    voice_script: list[tuple[float, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = {k: v for k, v in vars(self).items() if k != "version"}
        payload["voice_script"] = [[at_m, text] for at_m, text in self.voice_script]
        return {"apex_recording": self.version, **payload}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RecordingMeta:
        return cls(
            version=int(raw.get("apex_recording", RECORDING_VERSION)),
            ride_id=str(raw.get("ride_id", "")),
            recorded_at=float(raw.get("recorded_at", 0.0)),
            route_name=str(raw.get("route_name", "")),
            destination_name=str(raw.get("destination_name", "")),
            destination_latitude=float(raw.get("destination_latitude", 0.0)),
            destination_longitude=float(raw.get("destination_longitude", 0.0)),
            route_length_m=float(raw.get("route_length_m", 0.0)),
            voice_script=[
                (float(at_m), str(text)) for at_m, text in raw.get("voice_script", [])
            ],
        )


def _iter_lines(handle: io.TextIOBase, path: Path):
    """Yield (index, line), then (index, None) once if the stream is truncated.

    Iterating a gzip file raises rather than stopping when the end marker is
    missing, so the exception is turned into a sentinel the caller can act on.
    """
    index = 0
    while True:
        try:
            line = handle.readline()
        except (EOFError, OSError) as exc:
            logger.debug("Truncated recording %s: %s", path, exc)
            yield index, None
            return
        if not line:
            return
        yield index, line
        index += 1


def _open_text(path: Path, mode: str) -> io.TextIOBase:
    """Open a recording, transparently gzipped when the name says so.

    A full twelve minute ride is around 18 MB of JSON and about a tenth of that
    compressed, which is the difference between a file you can keep beside the
    code and one you cannot.
    """
    if path.suffix == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")  # type: ignore[return-value]
    return path.open(mode, encoding="utf-8")


class RideRecorder:
    """Appends IMU and GPS readings to an NDJSON file as they arrive.

    Writes go through the interpreter's own buffering rather than a thread: at
    ~110 readings per second a buffered text write costs far less than handing
    the work to an executor, and the fusion loop must not be made to wait on
    either.
    """

    def __init__(self, path: str | Path, meta: RecordingMeta) -> None:
        self.path = Path(path)
        self.meta = meta
        self.readings_written = 0
        self.skipped_audio = 0
        self._handle: Any = None
        self._lock = threading.Lock()

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = _open_text(self.path, "w")
        self.meta.recorded_at = self.meta.recorded_at or clock.now()
        self._handle.write(json.dumps(self.meta.to_dict()) + "\n")
        logger.info("Recording raw sensor readings to %s", self.path)

    def __call__(self, reading: SensorReading) -> None:
        """Subscriber entry point. Safe to call from the fusion loop."""
        if self._handle is None:
            return
        if reading.source not in RECORDED_SOURCES:
            self.skipped_audio += 1
            return
        line = json.dumps(
            {
                "t": reading.timestamp,
                "source": reading.source.value,
                "quality": reading.quality,
                "payload": reading.payload,
            },
            separators=(",", ":"),
        )
        with self._lock:
            self._handle.write(line + "\n")
            self.readings_written += 1

    def close(self) -> None:
        if self._handle is None:
            return
        with self._lock:
            handle, self._handle = self._handle, None
            handle.close()
        logger.info(
            "Recorded %d readings to %s (%.1f KB)",
            self.readings_written,
            self.path,
            self.path.stat().st_size / 1024.0 if self.path.exists() else 0.0,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "readings": self.readings_written,
            "audio_skipped": self.skipped_audio,
        }


@dataclass
class Recording:
    """A loaded recording, split per source and ready to be replayed."""

    meta: RecordingMeta
    per_source: dict[SensorSource, deque[SensorReading]] = field(default_factory=dict)
    # 1.0 is real time. 0 means unpaced: hand readings over as fast as the
    # pipeline accepts them, which is what offline analysis and tests want.
    speed: float = 1.0
    loop: bool = False
    # Shared across sensors so both threads pace against the same origin.
    _wall_start: float | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def paced(self) -> bool:
        return self.speed > 0.0

    @property
    def first_timestamp(self) -> float:
        starts = [q[0].timestamp for q in self.per_source.values() if q]
        return min(starts) if starts else 0.0

    @property
    def duration_s(self) -> float:
        ends = [q[-1].timestamp for q in self.per_source.values() if q]
        return (max(ends) - self.first_timestamp) if ends else 0.0

    @property
    def reading_count(self) -> int:
        return sum(len(q) for q in self.per_source.values())

    @property
    def cycle_span_s(self) -> float:
        """Timeline distance between the start of one pass and the next.

        `duration_s` is the span from the first reading to the last, which omits
        the gap that would have followed the last one. Looping on that alone
        makes the first reading of a pass share a timestamp with the last of the
        previous pass, so one mean sample interval is added.
        """
        count = self.reading_count
        mean_interval = self.duration_s / max(count - 1, 1) if count > 1 else 0.01
        return self.duration_s + max(mean_interval, 1e-6)

    @property
    def destination(self) -> tuple[float, float] | None:
        if not self.meta.destination_latitude and not self.meta.destination_longitude:
            return None
        return self.meta.destination_latitude, self.meta.destination_longitude

    @property
    def route_length_m(self) -> float:
        return self.meta.route_length_m

    @property
    def route_name(self) -> str:
        return self.meta.route_name

    @property
    def destination_name(self) -> str:
        return self.meta.destination_name

    @property
    def voice_script(self) -> list[tuple[float, str]]:
        return self.meta.voice_script

    def wall_start(self, now: float) -> float:
        """Wall-clock origin, set once by whichever sensor reads first."""
        with self._lock:
            if self._wall_start is None:
                self._wall_start = now
            return self._wall_start


def load_recording(path: str | Path, *, speed: float = 1.0, loop: bool = False) -> Recording:
    """Parse an NDJSON recording. Raises `SensorError` if it is unusable."""
    source_path = Path(path)
    if not source_path.exists():
        raise SensorError(f"Recording not found: {source_path}")

    meta = RecordingMeta()
    per_source: dict[SensorSource, deque[SensorReading]] = {
        source: deque() for source in RECORDED_SOURCES
    }
    malformed = 0

    truncated = False
    with _open_text(source_path, "r") as handle:
        # A ride that ended in a power cut, or one still being written, leaves a
        # gzip stream with no end marker. Whatever was flushed is still a valid
        # ride, and refusing to open it would be the least useful response.
        for index, line in _iter_lines(handle, source_path):
            if line is None:
                truncated = True
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                # The last line of an interrupted write is routinely a partial
                # object, which is not a reason to distrust the rest.
                malformed += 1
                continue

            if index == 0 and "apex_recording" in raw:
                meta = RecordingMeta.from_dict(raw)
                if meta.version != RECORDING_VERSION:
                    raise SensorError(
                        f"{source_path} is recording format v{meta.version}, "
                        f"this build reads v{RECORDING_VERSION}"
                    )
                continue

            try:
                source = SensorSource(raw["source"])
            except (KeyError, ValueError):
                malformed += 1
                continue
            if source not in per_source:
                continue
            per_source[source].append(
                SensorReading(
                    timestamp=float(raw["t"]),
                    source=source,
                    payload=dict(raw.get("payload", {})),
                    quality=float(raw.get("quality", 1.0)),
                )
            )

    recording = Recording(
        meta=meta, per_source=per_source, speed=max(speed, 0.0), loop=loop
    )
    if recording.reading_count == 0:
        raise SensorError(f"{source_path} contains no replayable readings")
    if malformed:
        logger.warning("Skipped %d malformed lines in %s", malformed, source_path)
    if truncated:
        logger.warning(
            "%s is truncated; replaying the %d readings that were flushed",
            source_path.name,
            recording.reading_count,
        )

    logger.info(
        "Loaded %s: %d readings over %.1fs, replaying %s",
        source_path.name,
        recording.reading_count,
        recording.duration_s,
        f"at {recording.speed:.2f}x" if recording.paced else "unpaced",
    )
    return recording


class _ReplaySensor(Sensor):
    """Hands back recorded readings, paced against the wall clock.

    Timestamps are rebased onto the current epoch so that everything downstream
    sees a ride happening now, while the gaps between readings are preserved so
    the filter sees the dt it was recorded with.
    """

    def __init__(
        self,
        recording: Recording,
        source: SensorSource,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        self.recording = recording
        self.source = source
        self.rate_hz = 0.0  # self-paced: read() sleeps until the reading is due
        self._time = time_source or clock.now
        self._queue: deque[SensorReading] = deque(recording.per_source.get(source, ()))
        self._pristine = tuple(self._queue)
        self._open = False
        self._epoch_out = 0.0
        self._rec_start = recording.first_timestamp
        # Which pass through the recording we are on. Timestamps must keep
        # increasing across a loop: restarting them would make every reading in
        # the second pass look older than the reorder window and be discarded.
        self._cycle = 0

    def open(self) -> None:
        if not self._pristine:
            raise SensorError(f"Recording has no {self.source.value} readings")
        self._queue = deque(self._pristine)
        self._epoch_out = self._time()
        self._cycle = 0
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def exhausted(self) -> bool:
        return not self._queue

    def read(self) -> SensorReading | None:
        if not self._open:
            return None
        if not self._queue:
            if not self.recording.loop:
                # Block briefly rather than spinning: acquisition polls a
                # self-paced sensor as fast as it returns.
                time.sleep(0.05)
                return None
            self._queue = deque(self._pristine)
            self._cycle += 1

        reading = self._queue.popleft()
        # Position on one continuous timeline, however many passes in we are.
        # Both sensors derive this from the same recorded clock, so they stay in
        # step with each other across a loop boundary without coordinating.
        offset = (
            self._cycle * self.recording.cycle_span_s + reading.timestamp - self._rec_start
        )

        if self.recording.paced:
            wall_origin = self.recording.wall_start(self._time())
            due_at = wall_origin + offset / self.recording.speed
            delay = due_at - self._time()
            if delay > 0:
                # Capped: a torn recording with a huge gap should not park the
                # acquisition thread for minutes.
                time.sleep(min(delay, 1.0))

        return SensorReading(
            timestamp=self._epoch_out + offset,
            source=reading.source,
            payload=dict(reading.payload),
            quality=reading.quality,
        )


class ReplayImu(_ReplaySensor):
    def __init__(
        self, recording: Recording, time_source: Callable[[], float] | None = None
    ) -> None:
        super().__init__(recording, SensorSource.IMU, time_source)


class ReplayGps(_ReplaySensor):
    def __init__(
        self, recording: Recording, time_source: Callable[[], float] | None = None
    ) -> None:
        super().__init__(recording, SensorSource.GPS, time_source)
