"""Sensor threads feeding the event loop.

I2C, UART, and I2S reads all block. Running them on the event loop would stall
the dashboard every time the GPS goes quiet for a second. Running the whole
pipeline in threads instead would mean locking shared state everywhere.

So: one thread per sensor, and the only thing crossing the boundary is a frozen
`SensorReading` handed over with `loop.call_soon_threadsafe`. Everything
downstream of the queue runs single-threaded on the loop and needs no locks.

A sensor that fails is reopened with exponential backoff rather than taking the
process down. Losing GPS should degrade the ride, not end it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from app import clock
from app.models import SensorReading, SensorSource
from app.sensors.base import Sensor, SensorError

logger = logging.getLogger(__name__)

INITIAL_RETRY_S = 0.5
MAX_RETRY_S = 8.0


@dataclass
class SensorStats:
    """Per-sensor health. Rising `errors` or `dropped` means trouble."""

    reads: int = 0
    empty_reads: int = 0
    errors: int = 0
    dropped: int = 0
    reopens: int = 0
    last_reading_at: float = 0.0
    last_error: str = ""
    open: bool = False


@dataclass
class AcquisitionStats:
    per_sensor: dict[str, SensorStats] = field(default_factory=dict)

    def for_sensor(self, name: str) -> SensorStats:
        return self.per_sensor.setdefault(name, SensorStats())

    @property
    def total_dropped(self) -> int:
        return sum(s.dropped for s in self.per_sensor.values())


class Acquisition:
    """Runs each sensor on its own thread and marshals readings onto the loop."""

    def __init__(
        self,
        sensors: list[Sensor],
        queue: asyncio.Queue[SensorReading],
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        on_sensor_failed: Callable[[SensorSource, str], None] | None = None,
    ) -> None:
        self.sensors = sensors
        self.queue = queue
        self.loop = loop or asyncio.get_event_loop()
        self.on_sensor_failed = on_sensor_failed

        self.stats = AcquisitionStats()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("Acquisition already started")

        self._stop.clear()
        for sensor in self.sensors:
            thread = threading.Thread(
                target=self._run_sensor,
                args=(sensor,),
                name=f"apex-{sensor.name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    # --- thread body ------------------------------------------------------

    def _run_sensor(self, sensor: Sensor) -> None:
        stats = self.stats.for_sensor(sensor.name)
        retry_delay = INITIAL_RETRY_S
        opened = False
        period = 0.0 if sensor.self_paced else 1.0 / sensor.rate_hz
        next_deadline = clock.monotonic()

        while not self._stop.is_set():
            if not opened:
                try:
                    sensor.open()
                    opened = True
                    stats.open = True
                    retry_delay = INITIAL_RETRY_S
                    logger.info("%s sensor opened", sensor.name)
                except SensorError as exc:
                    stats.errors += 1
                    stats.last_error = str(exc)
                    stats.open = False
                    logger.warning("%s open failed: %s", sensor.name, exc)
                    self._notify_failure(sensor, str(exc))
                    if self._stop.wait(retry_delay):
                        break
                    retry_delay = min(retry_delay * 2.0, MAX_RETRY_S)
                    continue

            try:
                reading = sensor.read()
            except SensorError as exc:
                stats.errors += 1
                stats.last_error = str(exc)
                logger.warning("%s read failed, reopening: %s", sensor.name, exc)
                self._notify_failure(sensor, str(exc))
                self._safe_close(sensor)
                opened = False
                stats.open = False
                stats.reopens += 1
                if self._stop.wait(retry_delay):
                    break
                retry_delay = min(retry_delay * 2.0, MAX_RETRY_S)
                continue
            except Exception as exc:  # noqa: BLE001 - a driver bug must not kill the ride
                stats.errors += 1
                stats.last_error = repr(exc)
                logger.exception("%s raised unexpectedly", sensor.name)
                self._notify_failure(sensor, repr(exc))
                if self._stop.wait(retry_delay):
                    break
                continue

            if reading is None:
                stats.empty_reads += 1
            else:
                stats.reads += 1
                stats.last_reading_at = reading.timestamp
                self._submit(reading, stats)

            if period > 0.0:
                # Absolute deadlines, not "sleep period minus elapsed".
                # Event.wait resolution is a millisecond or two, and at a 10 ms
                # period that overshoot silently costs ~15% of the sample rate
                # if it is never recovered. Advancing a fixed deadline absorbs
                # it instead.
                next_deadline += period
                now = clock.monotonic()
                if next_deadline < now - period:
                    # Fell far behind (a long GC pause, or CPU starvation).
                    # Resynchronise rather than sprinting to catch up.
                    next_deadline = now + period
                if self._stop.wait(max(0.0, next_deadline - clock.monotonic())):
                    break

        self._safe_close(sensor)
        stats.open = False
        logger.info("%s sensor thread stopped", sensor.name)

    # --- crossing into the loop -------------------------------------------

    def _submit(self, reading: SensorReading, stats: SensorStats) -> None:
        # A closed loop means we are shutting down and this reading is moot.
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self._enqueue, reading, stats)

    def _enqueue(self, reading: SensorReading, stats: SensorStats) -> None:
        """Runs on the event loop, so no locking is needed here."""
        try:
            self.queue.put_nowait(reading)
        except asyncio.QueueFull:
            # Shed the oldest reading rather than the newest: stale telemetry
            # is worth less than current telemetry.
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except asyncio.QueueEmpty:
                pass
            stats.dropped += 1
            try:
                self.queue.put_nowait(reading)
            except asyncio.QueueFull:
                stats.dropped += 1

    def _notify_failure(self, sensor: Sensor, message: str) -> None:
        if self.on_sensor_failed is None:
            return
        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(self.on_sensor_failed, sensor.source, message)

    @staticmethod
    def _safe_close(sensor: Sensor) -> None:
        try:
            sensor.close()
        except Exception:  # noqa: BLE001 - close must never raise on shutdown
            logger.debug("%s close raised, ignoring", sensor.name, exc_info=True)
