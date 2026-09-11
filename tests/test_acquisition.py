"""Threaded acquisition: pacing, failure recovery, and backpressure."""

from __future__ import annotations

import asyncio

import pytest

from app import clock
from app.models import SensorReading, SensorSource
from app.pipeline.acquisition import Acquisition
from app.sensors.base import Sensor, SensorError


class CountingSensor(Sensor):
    """Yields an incrementing reading at a fixed rate."""

    source = SensorSource.IMU

    def __init__(self, rate_hz: float = 200.0) -> None:
        self.rate_hz = rate_hz
        self.opens = 0
        self.closes = 0
        self.reads = 0

    def open(self) -> None:
        self.opens += 1

    def read(self) -> SensorReading | None:
        self.reads += 1
        return SensorReading(
            timestamp=clock.now(),
            source=self.source,
            payload={"n": self.reads},
        )

    def close(self) -> None:
        self.closes += 1


class FlakySensor(CountingSensor):
    """Fails to open a few times, then works. Models a slow-booting GPS."""

    source = SensorSource.GPS

    def __init__(self, failures: int) -> None:
        super().__init__(rate_hz=200.0)
        self.remaining_failures = failures

    def open(self) -> None:
        self.opens += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise SensorError("device not ready")


class DyingSensor(CountingSensor):
    """Works, then starts failing reads, then recovers."""

    def __init__(self) -> None:
        super().__init__(rate_hz=200.0)
        self.failed_reads = 0

    def read(self) -> SensorReading | None:
        self.reads += 1
        if 5 <= self.reads <= 7:
            self.failed_reads += 1
            raise SensorError("I2C read failed")
        return super().read()


async def test_readings_reach_the_event_loop() -> None:
    sensor = CountingSensor(rate_hz=200.0)
    queue: asyncio.Queue[SensorReading] = asyncio.Queue(maxsize=256)
    acquisition = Acquisition([sensor], queue, asyncio.get_running_loop())

    acquisition.start()
    await asyncio.sleep(0.3)
    acquisition.stop()

    assert queue.qsize() > 10
    reading = await queue.get()
    assert reading.source is SensorSource.IMU
    assert sensor.closes >= 1


async def test_rate_is_roughly_honoured() -> None:
    """Absolute-deadline pacing should not lose a large slice of the rate."""
    sensor = CountingSensor(rate_hz=100.0)
    queue: asyncio.Queue[SensorReading] = asyncio.Queue(maxsize=1024)
    acquisition = Acquisition([sensor], queue, asyncio.get_running_loop())

    acquisition.start()
    await asyncio.sleep(1.0)
    acquisition.stop()

    # Allow generous slack for CI scheduling, but catch systematic rate loss.
    assert 80 <= sensor.reads <= 115, f"read {sensor.reads} times in 1 s at 100 Hz"


async def test_a_sensor_that_fails_to_open_is_retried() -> None:
    sensor = FlakySensor(failures=2)
    queue: asyncio.Queue[SensorReading] = asyncio.Queue(maxsize=256)
    failures: list[tuple[SensorSource, str]] = []
    acquisition = Acquisition(
        [sensor],
        queue,
        asyncio.get_running_loop(),
        on_sensor_failed=lambda source, message: failures.append((source, message)),
    )

    acquisition.start()
    await asyncio.sleep(2.0)
    acquisition.stop()

    assert sensor.opens >= 3  # two failures then a success
    assert queue.qsize() > 0
    assert failures and failures[0][0] is SensorSource.GPS


async def test_a_read_failure_reopens_rather_than_killing_the_ride() -> None:
    sensor = DyingSensor()
    queue: asyncio.Queue[SensorReading] = asyncio.Queue(maxsize=256)
    acquisition = Acquisition([sensor], queue, asyncio.get_running_loop())

    acquisition.start()
    await asyncio.sleep(1.5)
    acquisition.stop()

    stats = acquisition.stats.for_sensor("imu")
    assert sensor.failed_reads > 0
    assert stats.reopens > 0
    assert stats.reads > 0  # kept going afterwards


async def test_a_full_queue_sheds_the_oldest_reading() -> None:
    """Current telemetry is worth more than stale telemetry."""
    sensor = CountingSensor(rate_hz=500.0)
    queue: asyncio.Queue[SensorReading] = asyncio.Queue(maxsize=8)
    acquisition = Acquisition([sensor], queue, asyncio.get_running_loop())

    acquisition.start()
    await asyncio.sleep(0.5)
    acquisition.stop()

    stats = acquisition.stats.for_sensor("imu")
    assert stats.dropped > 0
    assert queue.qsize() <= 8

    # What survived must be recent, not the first samples taken.
    remaining = [queue.get_nowait().payload["n"] for _ in range(queue.qsize())]
    assert min(remaining) > 1


async def test_starting_twice_is_refused() -> None:
    acquisition = Acquisition(
        [CountingSensor()], asyncio.Queue(maxsize=16), asyncio.get_running_loop()
    )
    acquisition.start()
    with pytest.raises(RuntimeError, match="already started"):
        acquisition.start()
    acquisition.stop()
