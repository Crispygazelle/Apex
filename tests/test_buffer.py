"""Ring buffer eviction and lookback."""

from __future__ import annotations

import pytest

from app.models import MotionState, RideMetrics, RideSample
from app.pipeline.buffer import TelemetryBuffer


def sample(timestamp: float, speed_kmh: float = 0.0, g_force: float = 1.0) -> RideSample:
    return RideSample(
        timestamp=timestamp,
        ride_id="r",
        state=MotionState(timestamp=timestamp, speed_mps=speed_kmh / 3.6),
        metrics=RideMetrics(speed_kmh=speed_kmh, g_force=g_force),
    )


def test_evicts_by_age_not_by_count() -> None:
    buffer = TelemetryBuffer(seconds=10.0, expected_rate_hz=50.0)

    for i in range(1000):  # 20 s at 50 Hz
        buffer.append(sample(1000.0 + i * 0.02))

    assert buffer.span_seconds() <= 10.0
    oldest = buffer.oldest()
    latest = buffer.latest()
    assert oldest is not None and latest is not None
    assert latest.timestamp - oldest.timestamp <= 10.0


def test_count_cap_bounds_memory_even_at_an_unexpected_rate() -> None:
    buffer = TelemetryBuffer(seconds=60.0, expected_rate_hz=50.0)

    # Ten times the expected rate, all inside the age window.
    for i in range(100_000):
        buffer.append(sample(1000.0 + i * 0.001))

    assert len(buffer) <= buffer.capacity


def test_window_returns_only_the_recent_slice() -> None:
    buffer = TelemetryBuffer(seconds=60.0, expected_rate_hz=50.0)
    for i in range(500):  # 10 s at 50 Hz
        buffer.append(sample(1000.0 + i * 0.02))

    window = buffer.window(2.0)
    assert window
    assert window[-1].timestamp - window[0].timestamp <= 2.0 + 1e-9


def test_sample_at_finds_the_speed_from_before_an_impact() -> None:
    """This is the lookback crash detection depends on."""
    buffer = TelemetryBuffer(seconds=60.0, expected_rate_hz=50.0)
    for i in range(500):
        # Riding at 60 km/h, then stopped dead for the last second.
        speed = 60.0 if i < 450 else 0.0
        buffer.append(sample(1000.0 + i * 0.02, speed_kmh=speed))

    before = buffer.sample_at(2.0)
    assert before is not None
    assert before.metrics.speed_kmh == pytest.approx(60.0)

    now = buffer.latest()
    assert now is not None
    assert now.metrics.speed_kmh == pytest.approx(0.0)


def test_sample_at_reports_nothing_when_history_is_too_short() -> None:
    buffer = TelemetryBuffer(seconds=60.0, expected_rate_hz=50.0)
    buffer.append(sample(1000.0))
    assert buffer.sample_at(5.0) is None


def test_peak_g_force_scans_the_window() -> None:
    buffer = TelemetryBuffer(seconds=60.0, expected_rate_hz=50.0)
    for i in range(100):
        buffer.append(sample(1000.0 + i * 0.02, g_force=1.0))
    buffer.append(sample(1002.0, g_force=6.2))

    assert buffer.peak_g_force(1.0) == pytest.approx(6.2)


def test_empty_buffer_is_safe_to_query() -> None:
    buffer = TelemetryBuffer()
    assert buffer.latest() is None
    assert buffer.oldest() is None
    assert buffer.window(5.0) == []
    assert buffer.span_seconds() == 0.0
    assert buffer.peak_g_force(5.0) == 0.0
