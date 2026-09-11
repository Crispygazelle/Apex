"""Coordinator: real threads, real event loop, real sensors (simulated)."""

from __future__ import annotations

import asyncio

import pytest

from app.config import AppConfig
from app.models import RideSample, SensorSource, SystemState
from app.pipeline.coordinator import Coordinator, new_ride_id


@pytest.fixture
def fast_config(config: AppConfig) -> AppConfig:
    """Short run, high rates, so the test finishes quickly."""
    config.sensors.imu.rate_hz = 200.0
    config.sensors.gps.rate_hz = 20.0
    config.pipeline.output_rate_hz = 100.0
    config.calibration.stationary_samples = 40
    return config


async def test_runs_and_emits_samples(fast_config: AppConfig) -> None:
    coordinator = Coordinator(fast_config)
    await coordinator.run(duration_s=1.5)

    assert len(coordinator.buffer) > 50
    assert coordinator.latest is not None
    assert coordinator.stats()["samples_emitted"] == len(coordinator.buffer)


async def test_subscribers_receive_every_sample_in_order(fast_config: AppConfig) -> None:
    received: list[RideSample] = []
    coordinator = Coordinator(fast_config)
    coordinator.subscribe_sample(received.append)

    await coordinator.run(duration_s=1.0)

    assert len(received) == coordinator.stats()["samples_emitted"]
    timestamps = [s.timestamp for s in received]
    assert timestamps == sorted(timestamps)


async def test_async_subscribers_are_awaited(fast_config: AppConfig) -> None:
    received: list[RideSample] = []

    async def consume(sample: RideSample) -> None:
        await asyncio.sleep(0)
        received.append(sample)

    coordinator = Coordinator(fast_config)
    coordinator.subscribe_sample(consume)
    await coordinator.run(duration_s=1.0)

    assert received


async def test_a_broken_subscriber_does_not_stop_the_ride(fast_config: AppConfig) -> None:
    good: list[RideSample] = []

    def explode(_: RideSample) -> None:
        raise ValueError("consumer bug")

    coordinator = Coordinator(fast_config)
    coordinator.subscribe_sample(explode)
    coordinator.subscribe_sample(good.append)

    await coordinator.run(duration_s=1.0)
    assert good, "a failing subscriber must not starve the others"


async def test_raw_audio_is_fanned_out_to_the_voice_layer(config: AppConfig) -> None:
    config.sensors.imu.rate_hz = 200.0
    config.sensors.mic.chunk = 256
    chunks: list[bytes] = []

    coordinator = Coordinator(config, include_mic=True)
    coordinator.subscribe_reading(
        SensorSource.MIC, lambda reading: chunks.append(reading.payload["pcm"])
    )

    await coordinator.run(duration_s=1.0)
    assert chunks
    assert all(isinstance(chunk, bytes) for chunk in chunks)


async def test_stop_ends_the_run_promptly(fast_config: AppConfig) -> None:
    coordinator = Coordinator(fast_config)

    async def stop_soon() -> None:
        await asyncio.sleep(0.4)
        coordinator.stop()

    async with asyncio.timeout(5.0):
        await asyncio.gather(coordinator.run(), stop_soon())

    assert len(coordinator.buffer) > 0


async def test_shutdown_flushes_readings_still_in_flight(fast_config: AppConfig) -> None:
    """Nothing held inside the reorder window may be dropped on exit."""
    coordinator = Coordinator(fast_config)
    await coordinator.run(duration_s=1.0)

    assert len(coordinator.synchronizer) == 0
    stats = coordinator.synchronizer.stats
    assert stats.emitted == stats.accepted


async def test_a_failed_imu_marks_the_node_degraded(fast_config: AppConfig) -> None:
    coordinator = Coordinator(fast_config)
    coordinator._on_sensor_failed(SensorSource.IMU, "I2C bus error")  # noqa: SLF001

    assert coordinator.system_state is SystemState.DEGRADED

    coordinator.clear_failure(SensorSource.IMU)
    assert coordinator.system_state is not SystemState.DEGRADED


async def test_refuses_to_run_with_no_sensors(config: AppConfig) -> None:
    config.sensors.imu.enabled = False
    config.sensors.gps.enabled = False

    coordinator = Coordinator(config)
    with pytest.raises(RuntimeError, match="No sensors enabled"):
        await coordinator.run(duration_s=0.1)


def test_ride_ids_are_human_sortable() -> None:
    ride_id = new_ride_id()
    assert ride_id.startswith("ride-")
    assert len(ride_id) == len("ride-20260912-0214-31")
