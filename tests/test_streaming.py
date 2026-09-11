"""Broadcast hub: throttling, capacity, and shedding clients that stall."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.config import StreamingConfig
from app.models import MotionState, RideMetrics, RideSample
from app.streaming.websocket import TelemetryHub


class FakeClient:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def send_json(self, data: Any) -> None:
        self.received.append(data)


class BrokenClient(FakeClient):
    async def send_json(self, data: Any) -> None:
        raise ConnectionResetError("browser went away")


class StalledClient(FakeClient):
    """A client on a flaky link that never drains its socket."""

    async def send_json(self, data: Any) -> None:
        await asyncio.sleep(30.0)


def sample(timestamp: float, speed_kmh: float = 50.0) -> RideSample:
    return RideSample(
        timestamp=timestamp,
        ride_id="ride-1",
        state=MotionState(timestamp=timestamp, speed_mps=speed_kmh / 3.6),
        metrics=RideMetrics(speed_kmh=speed_kmh),
    )


async def test_broadcast_reaches_registered_clients() -> None:
    hub = TelemetryHub(StreamingConfig(rate_hz=100.0))
    client = FakeClient()
    assert hub.register(client)

    await hub.broadcast(sample(1000.0))

    assert len(client.received) == 1
    assert client.received[0]["speed_kmh"] == pytest.approx(50.0)


async def test_broadcast_with_no_clients_is_harmless() -> None:
    hub = TelemetryHub()
    await hub.broadcast(sample(1000.0))
    assert hub.frames_sent == 0


async def test_feed_is_downsampled_to_the_configured_rate() -> None:
    """50 Hz to a browser is wasted CPU on a Pi Zero."""
    hub = TelemetryHub(StreamingConfig(rate_hz=10.0))
    client = FakeClient()
    hub.register(client)

    # One second of 50 Hz telemetry.
    for i in range(50):
        await hub.broadcast(sample(1000.0 + i * 0.02))

    assert 9 <= len(client.received) <= 11, f"sent {len(client.received)} frames for 10 Hz"
    assert hub.frames_skipped > 30


async def test_capacity_is_enforced() -> None:
    hub = TelemetryHub(StreamingConfig(max_clients=2))
    assert hub.register(FakeClient())
    assert hub.register(FakeClient())
    assert hub.full
    assert hub.register(FakeClient()) is False
    assert hub.client_count == 2


async def test_a_client_that_errors_is_dropped() -> None:
    hub = TelemetryHub(StreamingConfig(rate_hz=100.0))
    good, broken = FakeClient(), BrokenClient()
    hub.register(good)
    hub.register(broken)

    await hub.broadcast(sample(1000.0))

    assert hub.client_count == 1
    assert hub.clients_dropped == 1
    assert len(good.received) == 1, "one bad client must not starve the others"


async def test_a_stalled_client_cannot_block_the_pipeline() -> None:
    """Without a send timeout this broadcast would hold up the fusion loop."""
    hub = TelemetryHub(StreamingConfig(rate_hz=100.0))
    good, stalled = FakeClient(), StalledClient()
    hub.register(good)
    hub.register(stalled)

    async with asyncio.timeout(5.0):
        await hub.broadcast(sample(1000.0))

    assert len(good.received) == 1
    assert hub.client_count == 1  # the stalled one was shed
    assert hub.clients_dropped == 1


async def test_publish_bypasses_the_throttle_for_urgent_events() -> None:
    """A crash alert must not be dropped by downsampling."""
    hub = TelemetryHub(StreamingConfig(rate_hz=1.0))
    client = FakeClient()
    hub.register(client)

    await hub.broadcast(sample(1000.0))
    await hub.broadcast(sample(1000.1))  # throttled away
    await hub.publish({"event": "crash", "peak_g": 6.4})

    assert len(client.received) == 2
    assert client.received[-1]["event"] == "crash"


async def test_unregister_is_idempotent() -> None:
    hub = TelemetryHub()
    client = FakeClient()
    hub.register(client)
    hub.unregister(client)
    hub.unregister(client)
    assert hub.client_count == 0


async def test_stats_report_the_feed_state() -> None:
    hub = TelemetryHub(StreamingConfig(rate_hz=10.0, max_clients=4))
    hub.register(FakeClient())
    await hub.broadcast(sample(1000.0))

    stats = hub.stats()
    assert stats["clients"] == 1
    assert stats["max_clients"] == 4
    assert stats["frames_sent"] == 1
