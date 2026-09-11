"""The safety layer end to end: detection, state, LED, alert, and cancel."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.config import AppConfig
from app.models import SensorSource, SystemState
from app.pipeline.coordinator import Coordinator
from app.safety.monitor import SafetyMonitor
from app.safety.sos import SosState
from app.safety.status_led import RecordingLedBackend
from app.streaming.websocket import TelemetryHub
from tests.test_crash import RATE_HZ, make_sample


class StubNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send(self, payload: dict[str, Any]) -> None:
        self.calls.append(payload)


class RecordingHub(TelemetryHub):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[dict[str, Any]] = []

    async def publish(self, message: dict[str, Any]) -> None:
        self.published.append(message)
        await super().publish(message)


@pytest.fixture
def monitor(config: AppConfig) -> tuple[SafetyMonitor, Coordinator, RecordingHub, StubNotifier]:
    config.safety.enabled = True
    config.safety.led.enabled = True
    config.safety.sos_countdown_s = 0.05

    coordinator = Coordinator(config, ride_id="ride-test")
    hub = RecordingHub()
    notifier = StubNotifier()
    monitor = SafetyMonitor(
        config,
        coordinator,
        hub=hub,
        notifier=notifier,
        led_backend=RecordingLedBackend(),
        retry_backoff_s=0.0,
    )
    return monitor, coordinator, hub, notifier


async def feed(monitor: SafetyMonitor, coordinator: Coordinator, samples: list) -> None:
    for sample in samples:
        coordinator.buffer.append(sample)
        await monitor.on_sample(sample)


def ride(start_t: float, seconds: float, speed_kmh: float, g: float = 1.0) -> list:
    return [
        make_sample(start_t + i / RATE_HZ, speed_kmh, g)
        for i in range(1, int(seconds * RATE_HZ) + 1)
    ]


async def test_a_crash_escalates_all_the_way_to_dispatch(monitor) -> None:
    safety, coordinator, hub, notifier = monitor
    await safety.start()

    await feed(safety, coordinator, ride(0.0, 10.0, 60.0))
    assert coordinator.system_state is not SystemState.CRASH_SUSPECTED

    await feed(safety, coordinator, ride(10.0, 0.1, 55.0, g=8.0))
    assert coordinator.system_state is SystemState.CRASH_SUSPECTED

    await feed(safety, coordinator, ride(10.1, 6.0, 0.0))
    assert coordinator.system_state is SystemState.SOS_ACTIVE
    assert len(safety.crashes) == 1

    await safety.sos.wait()
    assert safety.sos.state is SosState.SENT
    assert len(notifier.calls) == 1
    await safety.stop()


async def test_cancelling_restores_the_previous_state(monitor) -> None:
    safety, coordinator, hub, notifier = monitor
    safety.config.safety.sos_countdown_s = 5.0
    await safety.start()

    await feed(safety, coordinator, ride(0.0, 10.0, 60.0))
    await feed(safety, coordinator, ride(10.0, 0.1, 55.0, g=8.0))
    await feed(safety, coordinator, ride(10.1, 6.0, 0.0))
    assert coordinator.system_state is SystemState.SOS_ACTIVE

    assert safety.cancel_sos("rider is fine")
    await safety.sos.wait()

    assert notifier.calls == [], "a cancel must reach the notifier as silence"
    assert coordinator.processor.state_override is None
    assert coordinator.system_state is not SystemState.SOS_ACTIVE
    await safety.stop()


async def test_a_pothole_clears_the_suspicion_without_an_sos(monitor) -> None:
    safety, coordinator, hub, notifier = monitor
    await safety.start()

    await feed(safety, coordinator, ride(0.0, 10.0, 60.0))
    await feed(safety, coordinator, ride(10.0, 0.1, 60.0, g=6.0))
    assert coordinator.system_state is SystemState.CRASH_SUSPECTED

    await feed(safety, coordinator, ride(10.1, 6.0, 59.0))

    assert safety.sos.state is SosState.IDLE
    assert notifier.calls == []
    assert coordinator.processor.state_override is None
    assert safety.detector.stats.rejected == 1
    await safety.stop()


async def test_a_degraded_sensor_is_not_forgotten_after_a_false_alarm(monitor) -> None:
    """CRASH_SUSPECTED sits on top of DEGRADED; clearing it must not erase it."""
    safety, coordinator, hub, notifier = monitor
    await safety.start()

    coordinator._on_sensor_failed(SensorSource.IMU, "i2c read failed")
    assert coordinator.processor.state_override is SystemState.DEGRADED

    await feed(safety, coordinator, ride(0.0, 10.0, 60.0))
    await feed(safety, coordinator, ride(10.0, 0.1, 60.0, g=6.0))
    assert coordinator.processor.state_override is SystemState.CRASH_SUSPECTED

    await feed(safety, coordinator, ride(10.1, 6.0, 59.0))
    assert coordinator.processor.state_override is SystemState.DEGRADED
    await safety.stop()


async def test_the_alert_is_published_not_throttled(monitor) -> None:
    safety, coordinator, hub, notifier = monitor
    await safety.start()

    await feed(safety, coordinator, ride(0.0, 10.0, 60.0))
    await feed(safety, coordinator, ride(10.0, 0.1, 55.0, g=8.0))
    await feed(safety, coordinator, ride(10.1, 6.0, 0.0))
    await safety.sos.wait()

    kinds = [message["type"] for message in hub.published]
    assert "crash" in kinds
    assert "sos" in kinds

    crash_alert = next(m for m in hub.published if m["type"] == "crash")
    assert crash_alert["event"]["peak_g"] == 8.0
    assert crash_alert["sos"]["state"] in ("idle", "countdown")
    await safety.stop()


async def test_the_led_tracks_the_escalation(monitor) -> None:
    safety, coordinator, hub, notifier = monitor
    await safety.start()
    backend = safety.led.backend

    await feed(safety, coordinator, ride(0.0, 10.0, 60.0))
    await feed(safety, coordinator, ride(10.0, 0.1, 55.0, g=8.0))
    await feed(safety, coordinator, ride(10.1, 6.0, 0.0))
    await safety.sos.wait()

    # Red is lit by the time the SOS is out, and green is not.
    assert backend.transitions[-1] == (True, False, False)
    await safety.stop()


async def test_a_disabled_safety_layer_never_subscribes(config: AppConfig) -> None:
    config.safety.enabled = False
    coordinator = Coordinator(config, ride_id="ride-test")
    safety = SafetyMonitor(config, coordinator, led_backend=RecordingLedBackend())

    await safety.start()
    assert coordinator._sample_subscribers == []
    await safety.stop()


async def test_stats_report_what_happened(monitor) -> None:
    safety, coordinator, hub, notifier = monitor
    await safety.start()

    await feed(safety, coordinator, ride(0.0, 10.0, 60.0))
    await feed(safety, coordinator, ride(10.0, 0.1, 60.0, g=6.0))
    await feed(safety, coordinator, ride(10.1, 6.0, 59.0))

    stats = safety.stats()
    assert stats["enabled"] is True
    assert stats["triggers"] == 1
    assert stats["rejected"] == 1
    assert stats["confirmed"] == 0
    assert stats["sos"]["state"] == "idle"
    await safety.stop()


async def test_a_crash_is_written_to_disk_even_without_influx(
    config: AppConfig, tmp_path
) -> None:
    """The card pulled from a wreck must carry the record."""
    from app.storage.batch_writer import CRASH_LOG_NAME, BatchWriter
    from app.storage.influxdb import InfluxWriter

    config.safety.enabled = True
    config.safety.sos_countdown_s = 0.01
    config.storage.spool_dir = str(tmp_path / "spool")

    coordinator = Coordinator(config, ride_id="ride-test")
    writer = InfluxWriter(config.storage)
    batch_writer = BatchWriter(config.storage, writer)
    safety = SafetyMonitor(
        config,
        coordinator,
        batch_writer=batch_writer,
        notifier=StubNotifier(),
        led_backend=RecordingLedBackend(),
        retry_backoff_s=0.0,
    )
    await safety.start()

    await feed(safety, coordinator, ride(0.0, 10.0, 60.0))
    await feed(safety, coordinator, ride(10.0, 0.1, 55.0, g=8.0))
    await feed(safety, coordinator, ride(10.1, 6.0, 0.0))
    await safety.sos.wait()

    log = tmp_path / "spool" / CRASH_LOG_NAME
    assert log.exists(), "crash record must survive an unreachable database"
    assert "8.0" in log.read_text()
    await safety.stop()


async def test_a_second_impact_during_an_sos_does_not_restart_it(monitor) -> None:
    safety, coordinator, hub, notifier = monitor
    safety.config.safety.sos_countdown_s = 5.0
    await safety.start()

    await feed(safety, coordinator, ride(0.0, 10.0, 60.0))
    await feed(safety, coordinator, ride(10.0, 0.1, 55.0, g=8.0))
    await feed(safety, coordinator, ride(10.1, 6.0, 0.0))
    first_remaining = safety.sos.remaining_s

    await asyncio.sleep(0.05)
    safety.detector.reset()
    await feed(safety, coordinator, ride(16.1, 0.1, 55.0, g=9.0))
    await feed(safety, coordinator, ride(16.2, 6.0, 0.0))

    assert safety.sos.remaining_s < first_remaining, "countdown must not restart"
    safety.cancel_sos()
    await safety.sos.wait()
    await safety.stop()
