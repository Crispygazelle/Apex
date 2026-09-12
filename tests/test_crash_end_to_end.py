"""A scripted crash driven through the real fusion pipeline.

The unit tests in `test_crash.py` hand the detector samples built by hand,
which proves the decision logic but not that a crash actually *looks* like
that by the time it has been through calibration, the Kalman filter, and the
metrics layer. The gravity low-pass in particular reshapes acceleration, and
a detector tuned against synthetic input could easily miss real input.

So these run the simulator's scripted crash through the genuine pipeline.
"""

from __future__ import annotations

import pytest

from app.config import AppConfig
from app.models import SystemState
from app.pipeline.buffer import TelemetryBuffer
from app.safety.crash import CrashDetector
from app.sensors.simulated import RideProfile
from tests.harness import run_offline_ride

# The profile's speed wave peaks near here, so the crash happens at a
# representative ~49 km/h rather than in a trough.
CRASH_AT_S = 58.0
# The same wave bottoms out at ~21 km/h here, which is where the proportional
# speed rule earns its place.
LOW_SPEED_CRASH_AT_S = 40.0


def detect(config: AppConfig, profile: RideProfile, duration_s: float = 75.0):
    """Run a ride and feed every emitted sample to a detector."""
    result = run_offline_ride(config, duration_s=duration_s, profile=profile)
    buffer = TelemetryBuffer(
        seconds=config.pipeline.buffer_seconds,
        expected_rate_hz=config.pipeline.output_rate_hz,
    )
    detector = CrashDetector(config.safety, buffer, ride_id="test-ride")

    events = []
    for sample in result.samples:
        buffer.append(sample)
        event = detector.observe(sample)
        if event is not None:
            events.append(event)
    return result, detector, events


@pytest.fixture
def crash_profile() -> RideProfile:
    # No GPS dropout: the crash is the subject of these tests, and a blackout
    # at the same time would confound speed corroboration.
    return RideProfile(crash_at_s=CRASH_AT_S, gps_dropouts=[])


def test_a_scripted_crash_is_detected_through_the_real_pipeline(
    config: AppConfig, crash_profile: RideProfile
) -> None:
    result, detector, events = detect(config, crash_profile)

    assert len(events) == 1, f"expected one crash, got {len(events)}"
    event = events[0]
    assert event.confirmed
    assert event.peak_g > config.safety.crash_g_threshold

    # The impact must be recognised within a few seconds of it happening.
    ride_start = result.samples[0].timestamp
    detected_at = event.timestamp - ride_start
    assert abs(detected_at - CRASH_AT_S) < 1.0, f"impact timed at t+{detected_at:.1f}s"

    # And it must have found a real speed drop, not a rounding artefact.
    assert event.speed_before_kmh > 30.0
    assert event.speed_after_kmh < 5.0


def test_a_low_speed_crash_is_caught_through_the_real_pipeline(config: AppConfig) -> None:
    """~21 km/h: below the absolute drop threshold, so the proportional rule
    is the only thing standing between the rider and a missed crash."""
    profile = RideProfile(
        crash_at_s=LOW_SPEED_CRASH_AT_S,
        gps_dropouts=[],
        # Default cruise is ~80 km/h, whose trough is still above the absolute
        # drop threshold. Pin a slower ride so only the proportional rule can
        # confirm the impact.
        cruise_mps=12.0,
    )
    _, _, events = detect(config, profile, duration_s=55.0)

    assert len(events) == 1
    event = events[0]
    assert event.confirmed
    assert event.speed_before_kmh < config.safety.speed_drop_kmh + 2.0
    assert event.speed_after_kmh < 5.0


def test_the_ride_before_the_crash_produces_no_false_alarms(
    config: AppConfig, crash_profile: RideProfile
) -> None:
    """A full minute of ordinary riding, including hard braking and cornering."""
    _, detector, events = detect(config, crash_profile, duration_s=CRASH_AT_S - 1.0)

    assert events == []
    assert detector.stats.triggers == 0, (
        f"normal riding tripped the g threshold: {detector.stats.last_reason}"
    )


def test_an_uneventful_ride_never_triggers(config: AppConfig) -> None:
    """The default 90 s profile has braking, hills, and a GPS blackout."""
    _, detector, events = detect(config, RideProfile(), duration_s=90.0)

    assert events == []
    assert detector.stats.confirmed == 0


def test_the_helmet_really_does_go_quiet_after_the_impact(
    config: AppConfig, crash_profile: RideProfile
) -> None:
    """Guards the simulator: without stillness the corroboration is untested."""
    result, _, _ = detect(config, crash_profile)
    ride_start = result.samples[0].timestamp

    settled = [
        sample
        for sample in result.samples
        if sample.timestamp - ride_start > CRASH_AT_S + 3.0
    ]
    assert settled, "ride must continue past the crash"

    worst = max(abs(sample.metrics.g_force - 1.0) for sample in settled)
    assert worst <= config.safety.stillness_g_tolerance, (
        f"post-crash motion of {worst:.2f} g would defeat the stillness test"
    )


def test_the_impact_survives_the_gravity_filter(
    config: AppConfig, crash_profile: RideProfile
) -> None:
    """The 0.05 Hz low-pass must not absorb a tenth-of-a-second spike."""
    result, _, _ = detect(config, crash_profile)
    ride_start = result.samples[0].timestamp

    window = [
        sample.metrics.g_force
        for sample in result.samples
        if CRASH_AT_S <= sample.timestamp - ride_start <= CRASH_AT_S + 0.5
    ]
    assert window, "no samples during the impact"
    assert max(window) > config.safety.crash_g_threshold


def test_the_crash_state_is_reported_on_the_samples(
    config: AppConfig, crash_profile: RideProfile
) -> None:
    """Whatever a dashboard or a rider sees must reflect the escalation."""
    from app.safety.status_led import PATTERNS

    _, _, events = detect(config, crash_profile)
    assert events

    # The LED has a distinct, unmistakable pattern for the two crash states.
    assert PATTERNS[SystemState.CRASH_SUSPECTED].red
    assert PATTERNS[SystemState.SOS_ACTIVE].red
    assert not PATTERNS[SystemState.RIDING].red
