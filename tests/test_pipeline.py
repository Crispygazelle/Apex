"""End-to-end accuracy: does the pipeline actually track the real trajectory?

These are the tests that would fail if the filter were wired backwards, the
body-to-navigation rotation were transposed, or the GPS weighting were wrong.
Asserting only that "a sample was produced" would catch none of that.
"""

from __future__ import annotations

import statistics

import pytest

from app.config import AppConfig
from app.models import FusionMode, SystemState
from app.sensors.simulated import RideProfile

from .harness import run_offline_ride

# Skip the first stretch: the node is calibrating and has no GPS lock yet, so
# comparing against truth before that is meaningless.
SETTLE_S = 12.0


@pytest.fixture(scope="module")
def ride(request: pytest.FixtureRequest):  # noqa: ANN201
    config = AppConfig()
    config.sensors.mic.enabled = False
    return run_offline_ride(config, duration_s=90.0)


def test_produces_telemetry_for_the_whole_ride(ride) -> None:  # noqa: ANN001
    assert len(ride.samples) > 3000  # ~50 Hz for 90 s
    span = ride.samples[-1].timestamp - ride.samples[0].timestamp
    assert span == pytest.approx(90.0, abs=1.0)


def test_position_tracks_ground_truth_while_gps_is_available(ride) -> None:  # noqa: ANN001
    errors = ride.position_errors_m(after_s=SETTLE_S, mode=FusionMode.FUSED)
    assert errors

    mean_error = statistics.fmean(errors)
    p95 = sorted(errors)[int(len(errors) * 0.95)]

    assert mean_error < 1.5, f"mean position error {mean_error:.2f} m"
    assert p95 < 3.0, f"p95 position error {p95:.2f} m"


def test_filtered_position_beats_raw_gps(ride) -> None:  # noqa: ANN001
    """The filter has to earn its place: it should be better than its input.

    The simulated GPS has sigma ~2.9 m per axis, so a single raw fix sits about
    3.6 m from truth on average. Fusing 10 Hz of that with 100 Hz of IMU should
    be substantially better, not merely comparable.
    """
    errors = ride.position_errors_m(after_s=SETTLE_S, mode=FusionMode.FUSED)
    mean_error = statistics.fmean(errors)

    raw_gps_error_m = 3.6
    assert mean_error < raw_gps_error_m / 2.0, (
        f"fusion gained little: {mean_error:.2f} m against {raw_gps_error_m} m raw"
    )


def test_speed_tracks_ground_truth(ride) -> None:  # noqa: ANN001
    errors = ride.speed_errors_mps(after_s=SETTLE_S, mode=FusionMode.FUSED)
    assert statistics.fmean(errors) < 0.5


def test_odometer_is_accurate_on_a_ride_with_continuous_gps() -> None:
    """Without a blackout the odometer should be within a couple of percent."""
    config = AppConfig()
    profile = RideProfile(gps_dropouts=[])
    result = run_offline_ride(config, duration_s=90.0, profile=profile)

    reported = result.final.metrics.distance_m
    true = result.truth[-1].distance_m
    assert reported == pytest.approx(true, rel=0.02), (
        f"reported {reported:.1f} m against a true {true:.1f} m"
    )


def test_odometer_degrades_gracefully_across_a_gps_blackout(ride) -> None:  # noqa: ANN001
    """Twelve seconds of dead reckoning costs distance accuracy, but bounded.

    During a blackout the only speed information is the IMU, whose sustained
    acceleration is partly absorbed by the gravity filter, so the odometer
    undercounts. It must not run away in either direction.
    """
    reported = ride.final.metrics.distance_m
    true = ride.truth[-1].distance_m

    assert reported == pytest.approx(true, rel=0.10), (
        f"reported {reported:.1f} m against a true {true:.1f} m"
    )
    assert reported < true, "a blackout should undercount, never overcount"


def test_progresses_through_the_expected_states(ride) -> None:  # noqa: ANN001
    states = [s.system_state for s in ride.samples]

    assert states[0] in (SystemState.CALIBRATING, SystemState.STARTING)
    assert SystemState.ACQUIRING_GPS in states  # GPS cold start
    assert states[-1] is SystemState.RIDING


def test_dead_reckoning_engages_during_the_gps_dropout(ride) -> None:  # noqa: ANN001
    """The profile blacks GPS out from t=45 s to t=57 s."""
    start = ride.samples[0].timestamp
    during = [
        s for s in ride.samples if 50.0 <= (s.timestamp - start) <= 56.0
    ]
    assert during
    assert all(s.state.mode is FusionMode.DEAD_RECKONING for s in during)

    # ...and recovers afterwards.
    after = [s for s in ride.samples if 62.0 <= (s.timestamp - start) <= 66.0]
    assert after
    assert all(s.state.mode is FusionMode.FUSED for s in after)


def test_position_survives_the_dropout_without_diverging(ride) -> None:  # noqa: ANN001
    """Dead reckoning must degrade gracefully, not fly off."""
    start = ride.samples[0].timestamp
    pairs = [
        (sample, truth)
        for sample, truth in zip(ride.samples, ride.truth, strict=True)
        if 45.0 <= (sample.timestamp - start) <= 57.0
    ]
    assert pairs

    from app.geo import haversine_m

    errors = [
        haversine_m(s.state.latitude, s.state.longitude, t.latitude, t.longitude)
        for s, t in pairs
    ]
    # Twelve seconds of unaided inertial navigation on a consumer MEMS IMU
    # drifts; it must stay in the tens of metres, not kilometres.
    assert max(errors) < 150.0, f"peak dropout error {max(errors):.1f} m"


def test_confidence_falls_during_the_dropout_and_recovers(ride) -> None:  # noqa: ANN001
    start = ride.samples[0].timestamp

    def mean_confidence(lo: float, hi: float) -> float:
        window = [
            s.state.position_confidence
            for s in ride.samples
            if lo <= (s.timestamp - start) <= hi
        ]
        return statistics.fmean(window)

    assert mean_confidence(55.0, 57.0) < mean_confidence(40.0, 44.0)
    assert mean_confidence(62.0, 66.0) > mean_confidence(55.0, 57.0)


def test_braking_event_registers_as_deceleration(ride) -> None:  # noqa: ANN001
    """The profile brakes hard around t=65 s (offset by the 4 s standing start)."""
    start = ride.samples[0].timestamp
    window = [
        s for s in ride.samples if 66.0 <= (s.timestamp - start) <= 72.0
    ]
    assert window

    hardest = min(s.metrics.longitudinal_accel_mps2 for s in window)
    assert hardest < -1.5, f"hardest braking was only {hardest:.2f} m/s^2"


def test_lean_angle_has_the_right_shape_and_magnitude(ride) -> None:  # noqa: ANN001
    errors = ride.lean_errors_deg(after_s=SETTLE_S)
    assert statistics.fmean(errors) < 4.0

    leans = [s.metrics.lean_angle_deg for s in ride.samples]
    # A curving road must produce lean in both directions.
    assert max(leans) > 3.0
    assert min(leans) < -3.0


def test_no_readings_are_lost_or_reordered(ride) -> None:  # noqa: ANN001
    timestamps = [s.timestamp for s in ride.samples]
    assert timestamps == sorted(timestamps)
    assert ride.processor.imu_count > 8000  # 100 Hz for 90 s


def test_a_stationary_ride_reports_zero_distance() -> None:
    """The most embarrassing possible bug: a parked bike clocking kilometres."""
    config = AppConfig()
    profile = RideProfile(stationary_s=40.0, gps_lock_s=3.0, gps_dropouts=[])
    result = run_offline_ride(config, duration_s=35.0, profile=profile)

    assert result.final.metrics.distance_m == pytest.approx(0.0, abs=1.0)
    assert result.final.metrics.speed_kmh < 3.0
