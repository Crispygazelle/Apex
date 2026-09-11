"""Derived metrics, with attention to the two that are easy to get wrong."""

from __future__ import annotations

import pytest

from app.config import CalibrationConfig
from app.geo import GeoOrigin
from app.models import ImuSample
from app.processing.calibration import CalibratedImu
from app.processing.metrics import MetricsCalculator, _pitch_from_gravity

GRAVITY = 9.80665


def calibrated(
    *,
    linear: tuple[float, float, float] = (0.0, 0.0, 0.0),
    gravity: tuple[float, float, float] = (0.0, 0.0, GRAVITY),
    gx: float = 0.0,
    gz: float = 0.0,
    dt: float = 0.01,
) -> CalibratedImu:
    total = tuple(linear[i] + gravity[i] for i in range(3))
    return CalibratedImu(
        sample=ImuSample(
            timestamp=0.0,
            ax_mps2=total[0],
            ay_mps2=total[1],
            az_mps2=total[2],
            gx_dps=gx,
            gy_dps=0.0,
            gz_dps=gz,
        ),
        gravity=gravity,
        linear=linear,
        is_stationary=False,
        dt=dt,
    )


def test_g_force_reads_one_at_rest() -> None:
    metrics = MetricsCalculator()
    metrics.ingest_imu(calibrated(), speed_mps=0.0)
    assert metrics.snapshot(0.0).g_force == pytest.approx(1.0, abs=1e-3)


def test_speed_and_maxima_accumulate() -> None:
    metrics = MetricsCalculator()
    metrics.ingest_imu(calibrated(), speed_mps=20.0)
    metrics.snapshot(20.0)
    metrics.ingest_imu(calibrated(), speed_mps=5.0)
    result = metrics.snapshot(5.0)

    assert result.speed_kmh == pytest.approx(18.0)
    assert result.max_speed_kmh == pytest.approx(72.0)


def test_odometer_integrates_filtered_speed_not_raw_fixes() -> None:
    """Guards the GPS odometer bug.

    Summing haversine distance between successive noisy fixes inflates the
    total badly: at 10 Hz with metres of noise, a bike travelling 180 m can
    clock several hundred. Integrating filtered speed does not have that
    failure mode, so a stationary bike must read exactly zero.
    """
    metrics = MetricsCalculator()
    origin = GeoOrigin(12.9716, 77.5946)

    # Parked, but GPS wanders by a few metres every fix for a minute.
    wander = [(3.1, -2.4), (-2.8, 4.0), (4.4, 1.2), (-3.9, -3.3)] * 150
    for east, north in wander:
        latitude, longitude = origin.to_geodetic(east, north)
        metrics.ingest_imu(calibrated(dt=0.1), speed_mps=0.0)
        metrics.ingest_gps(latitude, longitude, 920.0, speed_mps=0.0, valid=True)

    assert metrics.snapshot(0.0).distance_m == 0.0
    # The raw-fix figure is retained only as a diagnostic, and shows why.
    assert metrics.gps_only_distance_m == 0.0  # suppressed below MIN_MOTION_MPS


def test_odometer_matches_distance_travelled() -> None:
    metrics = MetricsCalculator()
    for _ in range(1000):  # 10 s at 100 Hz, steady 15 m/s
        metrics.ingest_imu(calibrated(dt=0.01), speed_mps=15.0)

    assert metrics.snapshot(15.0).distance_m == pytest.approx(150.0, rel=0.01)


def test_lean_comes_from_the_gyro_and_tracks_a_steady_corner() -> None:
    """The accelerometer cannot see lean in a coordinated turn.

    A bike holding 20 degrees of lean reports no lateral force at all, so the
    body-frame linear acceleration below is zero. Only gyro integration,
    anchored by the v*omega/g reference, can recover the angle.
    """
    metrics = MetricsCalculator()
    speed = 15.0
    # The yaw rate that a 20-degree lean implies at this speed.
    target_lean = 20.0
    yaw_rate = _yaw_rate_for_lean(target_lean, speed)

    # Roll in over one second, then hold the corner.
    for _ in range(100):
        metrics.ingest_imu(
            calibrated(gx=target_lean, gz=yaw_rate, dt=0.01), speed_mps=speed
        )
    for _ in range(1000):
        metrics.ingest_imu(calibrated(gx=0.0, gz=yaw_rate, dt=0.01), speed_mps=speed)

    result = metrics.snapshot(speed)
    assert result.lateral_accel_mps2 == pytest.approx(0.0)
    assert result.lean_angle_deg == pytest.approx(target_lean, abs=2.0)


def test_lean_reference_removes_gyro_drift() -> None:
    """An uncorrected integrator would run away; the reference must pin it."""
    metrics = MetricsCalculator()
    speed = 15.0

    # Residual 0.5 deg/s of roll bias, travelling dead straight for 60 s.
    for _ in range(6000):
        metrics.ingest_imu(calibrated(gx=0.5, gz=0.0, dt=0.01), speed_mps=speed)

    # Unbounded integration would reach 30 degrees. The reference says upright.
    assert abs(metrics.snapshot(speed).lean_angle_deg) < 3.0


def test_lean_decays_to_upright_when_stopped() -> None:
    metrics = MetricsCalculator()
    for _ in range(200):
        metrics.ingest_imu(calibrated(gx=10.0, gz=20.0, dt=0.01), speed_mps=15.0)
    for _ in range(2000):
        metrics.ingest_imu(calibrated(gx=0.0, gz=0.0, dt=0.01), speed_mps=0.0)

    assert abs(metrics.snapshot(0.0).lean_angle_deg) < 1.0


def test_gradient_needs_a_long_baseline_before_it_reports() -> None:
    """Short baselines turn GPS altitude noise into fictional cliffs."""
    metrics = MetricsCalculator()
    origin = GeoOrigin(12.9716, 77.5946)

    # 20 m of travel with 4 m of altitude noise: a naive slope would read
    # tens of percent. Nothing should be published yet.
    for i in range(10):
        for _ in range(20):
            metrics.ingest_imu(calibrated(dt=0.01), speed_mps=10.0)
        latitude, longitude = origin.to_geodetic(0.0, i * 2.0)
        noise = 4.0 if i % 2 else -4.0
        metrics.ingest_gps(latitude, longitude, 920.0 + noise, speed_mps=10.0, valid=True)

    assert metrics.snapshot(10.0).gradient_pct == 0.0


def test_gradient_recovers_a_real_slope() -> None:
    metrics = MetricsCalculator()
    origin = GeoOrigin(12.9716, 77.5946)

    # A true 5% climb sampled over 400 m.
    for _ in range(200):
        for _ in range(20):  # 2 m of travel per fix at 10 m/s, 100 Hz
            metrics.ingest_imu(calibrated(dt=0.01), speed_mps=10.0)
        distance = metrics.snapshot(10.0).distance_m
        latitude, longitude = origin.to_geodetic(0.0, distance)
        metrics.ingest_gps(
            latitude, longitude, 920.0 + 0.05 * distance, speed_mps=10.0, valid=True
        )

    assert metrics.snapshot(10.0).gradient_pct == pytest.approx(5.0, abs=0.5)


def test_invalid_fixes_are_ignored() -> None:
    metrics = MetricsCalculator()
    metrics.ingest_gps(0.0, 0.0, 0.0, speed_mps=0.0, valid=False)
    assert metrics.snapshot(0.0).distance_m == 0.0


@pytest.mark.parametrize(
    ("gravity", "expected"),
    [
        ((0.0, 0.0, GRAVITY), 0.0),
        ((-GRAVITY * 0.5, 0.0, GRAVITY * 0.866), 30.0),  # nose up
        ((GRAVITY * 0.5, 0.0, GRAVITY * 0.866), -30.0),  # nose down
    ],
)
def test_pitch_from_gravity(gravity: tuple[float, float, float], expected: float) -> None:
    assert _pitch_from_gravity(gravity) == pytest.approx(expected, abs=0.1)


def _yaw_rate_for_lean(lean_deg: float, speed_mps: float) -> float:
    """Inverse of tan(lean) = v * omega / g, in deg/s."""
    from math import degrees, radians, tan

    return degrees(tan(radians(lean_deg)) * GRAVITY / speed_mps)


def test_calibration_config_gravity_is_respected() -> None:
    metrics = MetricsCalculator(CalibrationConfig(gravity_mps2=9.7))
    assert metrics.gravity == pytest.approx(9.7)
