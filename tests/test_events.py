"""Threshold event detector: one line per spike, not a firehose."""

from __future__ import annotations

from app.models import MotionState, RideMetrics, RideSample, SystemState
from app.pipeline.events import ThresholdDetector


def sample(
    timestamp: float,
    *,
    speed_kmh: float = 50.0,
    accel: float = 0.0,
    g_force: float = 1.05,
    lean: float = 0.0,
    state: SystemState = SystemState.RIDING,
) -> RideSample:
    return RideSample(
        timestamp=timestamp,
        ride_id="ride-test",
        state=MotionState(timestamp=timestamp, latitude=21.14, longitude=79.08),
        metrics=RideMetrics(
            speed_kmh=speed_kmh,
            g_force=g_force,
            lean_angle_deg=lean,
            longitudinal_accel_mps2=accel,
        ),
        system_state=state,
        gps_valid=True,
    )


def test_hard_brake_is_logged_once() -> None:
    detector = ThresholdDetector()
    first = detector.observe(sample(10.0, speed_kmh=80.0, accel=-4.2, g_force=1.6))
    assert first is not None
    assert first.kind == "hard_brake"
    assert "sudden braking" in first.text
    assert detector.observe(sample(10.2, speed_kmh=70.0, accel=-4.0, g_force=1.5)) is None


def test_fast_zone_fires_on_entry_only() -> None:
    detector = ThresholdDetector()
    assert detector.observe(sample(1.0, speed_kmh=50.0)) is None
    entered = detector.observe(sample(2.0, speed_kmh=86.0))
    assert entered is not None
    assert entered.kind == "fast_zone"
    assert detector.observe(sample(3.0, speed_kmh=88.0)) is None


def test_pothole_g_spike_is_not_a_brake() -> None:
    detector = ThresholdDetector()
    event = detector.observe(sample(5.0, speed_kmh=40.0, accel=0.4, g_force=2.4))
    assert event is not None
    assert event.kind == "high_g"
    assert "pothole" in event.text


def test_quiet_states_are_ignored() -> None:
    detector = ThresholdDetector()
    assert (
        detector.observe(
            sample(1.0, speed_kmh=80.0, accel=-5.0, state=SystemState.CALIBRATING)
        )
        is None
    )
