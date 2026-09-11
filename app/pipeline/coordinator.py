"""Orchestration: owns the sensors, the pipeline, and the fan-out to consumers.

Everything downstream (storage, streaming, safety, voice) attaches here as a
subscriber rather than being wired into the pipeline, so each phase can be
added without reopening the fusion code.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app import clock
from app.config import AppConfig
from app.models import RideSample, SensorReading, SensorSource, SystemState
from app.pipeline.acquisition import Acquisition, SensorStats
from app.pipeline.buffer import TelemetryBuffer
from app.pipeline.processor import Processor
from app.processing.synchronization import FrameSynchronizer
from app.sensors import RideSimulator, SensorSet, build_sensors

logger = logging.getLogger(__name__)

SampleCallback = Callable[[RideSample], Awaitable[None] | None]
ReadingCallback = Callable[[SensorReading], Awaitable[None] | None]

# How long to wait on the queue before checking for shutdown and idle flushes.
POLL_TIMEOUT_S = 0.1


def new_ride_id() -> str:
    """Human-sortable ride identifier, e.g. ride-20260912-0214-31."""
    return time.strftime("ride-%Y%m%d-%H%M-%S", time.localtime())


class Coordinator:
    """Runs the acquisition threads and the fusion loop, and fans out samples."""

    def __init__(
        self,
        config: AppConfig,
        *,
        ride_id: str | None = None,
        simulator: RideSimulator | None = None,
        include_mic: bool | None = None,
    ) -> None:
        self.config = config
        self.ride_id = ride_id or new_ride_id()

        self.buffer = TelemetryBuffer(
            seconds=config.pipeline.buffer_seconds,
            expected_rate_hz=config.pipeline.output_rate_hz,
        )
        self.processor = Processor(config, self.ride_id)
        self.synchronizer = FrameSynchronizer(config.fusion.reorder_window_s)

        self.sensor_set: SensorSet = build_sensors(
            config, simulator=simulator, include_mic=include_mic
        )
        self.queue: asyncio.Queue[SensorReading] = asyncio.Queue(
            maxsize=max(64, config.pipeline.queue_size)
        )
        self.acquisition: Acquisition | None = None

        self._sample_subscribers: list[SampleCallback] = []
        self._reading_subscribers: dict[SensorSource, list[ReadingCallback]] = {}
        self._stopping = asyncio.Event()
        self._failed_sensors: dict[SensorSource, str] = {}
        self._samples_emitted = 0
        self._last_arrival = 0.0

    # --- subscriptions ----------------------------------------------------

    def subscribe_sample(self, callback: SampleCallback) -> None:
        """Receive every fused `RideSample`, in order."""
        self._sample_subscribers.append(callback)

    def subscribe_reading(self, source: SensorSource, callback: ReadingCallback) -> None:
        """Receive raw readings from one sensor, e.g. audio for the voice layer."""
        self._reading_subscribers.setdefault(source, []).append(callback)

    # --- state ------------------------------------------------------------

    @property
    def latest(self) -> RideSample | None:
        return self.buffer.latest()

    @property
    def system_state(self) -> SystemState:
        return self.processor.system_state

    @property
    def simulator(self) -> RideSimulator | None:
        return self.sensor_set.simulator

    def stats(self) -> dict[str, Any]:
        """Health snapshot, surfaced by the dashboard and the CLI summary."""
        sync = self.synchronizer.stats
        return {
            "ride_id": self.ride_id,
            "system_state": self.system_state.value,
            "samples_emitted": self._samples_emitted,
            "buffer_samples": len(self.buffer),
            "buffer_span_s": round(self.buffer.span_seconds(), 2),
            "queue_depth": self.queue.qsize(),
            "imu_readings": self.processor.imu_count,
            "gps_readings": self.processor.gps_count,
            "rejected_fixes": self.processor.kalman.rejected_fixes,
            "fusion_mode": self.processor.kalman.mode.value,
            "sync_accepted": sync.accepted,
            "sync_dropped_late": sync.dropped_late,
            "sync_max_depth": sync.max_depth,
            "sensors": {
                name: {
                    "open": s.open,
                    "reads": s.reads,
                    "errors": s.errors,
                    "dropped": s.dropped,
                    "reopens": s.reopens,
                    "last_error": s.last_error,
                }
                for name, s in _sensor_stats(self.acquisition)
            },
            "failed_sensors": {k.value: v for k, v in self._failed_sensors.items()},
        }

    # --- run loop ---------------------------------------------------------

    async def run(self, duration_s: float | None = None) -> None:
        """Start acquisition and consume until stopped or `duration_s` elapses."""
        if not self.sensor_set.sensors:
            raise RuntimeError("No sensors enabled; check config.sensors.*.enabled")

        loop = asyncio.get_running_loop()
        self.acquisition = Acquisition(
            self.sensor_set.sensors,
            self.queue,
            loop,
            on_sensor_failed=self._on_sensor_failed,
        )

        logger.info(
            "Starting ride %s with %s backend and sensors: %s",
            self.ride_id,
            self.config.node.sensor_backend,
            ", ".join(s.name for s in self.sensor_set.sensors),
        )
        self.acquisition.start()
        self._last_arrival = clock.monotonic()

        deadline = clock.monotonic() + duration_s if duration_s else None
        try:
            while not self._stopping.is_set():
                if deadline is not None and clock.monotonic() >= deadline:
                    break
                await self._consume_once()
        finally:
            await self._shutdown()

    async def _consume_once(self) -> None:
        """One iteration: take what has arrived, then release what is ordered."""
        try:
            reading = await asyncio.wait_for(self.queue.get(), timeout=POLL_TIMEOUT_S)
        except TimeoutError:
            reading = None
        else:
            self.queue.task_done()
            self._last_arrival = clock.monotonic()
            self.synchronizer.push(reading)
            await self._fan_out_reading(reading)

        # The watermark only advances when readings arrive. If the sensors have
        # gone quiet, nothing newer is coming, so release what is held rather
        # than stalling the pipeline behind a window that will never clear.
        idle = clock.monotonic() - self._last_arrival
        flush = idle > self.synchronizer.reorder_window_s

        for ordered in self.synchronizer.drain(flush=flush):
            await self._process(ordered)

    async def _process(self, reading: SensorReading) -> None:
        sample = self.processor.process(reading)
        if sample is None:
            return

        self.buffer.append(sample)
        self._samples_emitted += 1

        for callback in self._sample_subscribers:
            try:
                await _invoke(callback, sample)
            except Exception:  # noqa: BLE001 - a bad consumer must not stop the ride
                logger.exception("Sample subscriber %r failed", callback)

    async def _fan_out_reading(self, reading: SensorReading) -> None:
        for callback in self._reading_subscribers.get(reading.source, ()):
            try:
                await _invoke(callback, reading)
            except Exception:  # noqa: BLE001
                logger.exception("Reading subscriber %r failed", callback)

    # --- shutdown ---------------------------------------------------------

    def stop(self) -> None:
        """Ask the run loop to finish. Safe to call from a signal handler."""
        self._stopping.set()

    async def _shutdown(self) -> None:
        logger.info("Stopping ride %s", self.ride_id)
        if self.acquisition is not None:
            self.acquisition.stop()

        # Drain anything the sensor threads handed over just before stopping.
        while not self.queue.empty():
            reading = self.queue.get_nowait()
            self.queue.task_done()
            self.synchronizer.push(reading)

        for ordered in self.synchronizer.drain(flush=True):
            await self._process(ordered)

        logger.info(
            "Ride %s complete: %d samples, %d IMU, %d GPS readings",
            self.ride_id,
            self._samples_emitted,
            self.processor.imu_count,
            self.processor.gps_count,
        )

    # --- callbacks --------------------------------------------------------

    def _on_sensor_failed(self, source: SensorSource, message: str) -> None:
        self._failed_sensors[source] = message
        # The IMU is load-bearing: without it there is no prediction step and
        # no crash detection, so report the node as degraded.
        if source is SensorSource.IMU:
            self.processor.state_override = SystemState.DEGRADED

    def clear_failure(self, source: SensorSource) -> None:
        self._failed_sensors.pop(source, None)
        if not self._failed_sensors and self.processor.state_override is SystemState.DEGRADED:
            self.processor.state_override = None


def _sensor_stats(acquisition: Acquisition | None) -> list[tuple[str, SensorStats]]:
    if acquisition is None:
        return []
    return list(acquisition.stats.per_sensor.items())


async def _invoke(callback: Callable[..., Any], *args: Any) -> None:
    """Call a subscriber that may be sync or async."""
    result = callback(*args)
    if inspect.isawaitable(result):
        await result
