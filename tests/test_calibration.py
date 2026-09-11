"""Bias estimation and gravity separation."""

from __future__ import annotations

import random

import pytest

from app.config import CalibrationConfig
from app.models import SensorReading, SensorSource
from app.processing.calibration import Calibrator

GRAVITY = 9.80665
GYRO_BIAS = (1.4, -0.8, 0.6)


def resting_reading(timestamp: float, rng: random.Random) -> SensorReading:
    """An IMU at rest, with bias larger than the stationarity threshold."""
    return SensorReading(
        timestamp=timestamp,
        source=SensorSource.IMU,
        payload={
            "ax_mps2": 0.18 + rng.gauss(0, 0.02),
            "ay_mps2": -0.12 + rng.gauss(0, 0.02),
            "az_mps2": GRAVITY + 0.25 + rng.gauss(0, 0.02),
            "gx_dps": GYRO_BIAS[0] + rng.gauss(0, 0.05),
            "gy_dps": GYRO_BIAS[1] + rng.gauss(0, 0.05),
            "gz_dps": GYRO_BIAS[2] + rng.gauss(0, 0.05),
            "temperature_c": 31.0,
        },
    )


@pytest.fixture
def calibration_config() -> CalibrationConfig:
    return CalibrationConfig(auto_calibrate_on_start=True, stationary_samples=60)


def test_gyro_bias_is_found_even_though_it_exceeds_the_motion_threshold(
    calibration_config: CalibrationConfig,
) -> None:
    """The bug this guards against: judging rest by absolute |gyro|.

    The gyro bias here has magnitude ~1.7 deg/s against a 2.0 deg/s threshold,
    so with noise the magnitude test intermittently fails and the calibration
    window resets forever. Spread-based detection is immune to that.
    """
    calibrator = Calibrator(calibration_config)
    rng = random.Random(3)

    t = 0.0
    for _ in range(200):
        t += 0.01
        calibrator.apply(resting_reading(t, rng))

    assert calibrator.calibrated
    for axis, expected in enumerate(GYRO_BIAS):
        assert calibrator.gyro_bias[axis] == pytest.approx(expected, abs=0.05)


def test_corrected_gyro_reads_near_zero_at_rest(
    calibration_config: CalibrationConfig,
) -> None:
    calibrator = Calibrator(calibration_config)
    rng = random.Random(5)

    t = 0.0
    for _ in range(200):
        t += 0.01
        result = calibrator.apply(resting_reading(t, rng))

    assert abs(result.sample.gx_dps) < 0.2
    assert abs(result.sample.gy_dps) < 0.2
    assert abs(result.sample.gz_dps) < 0.2


def test_magnitude_is_scaled_onto_known_gravity(
    calibration_config: CalibrationConfig,
) -> None:
    calibrator = Calibrator(calibration_config)
    rng = random.Random(7)

    t = 0.0
    for _ in range(200):
        t += 0.01
        result = calibrator.apply(resting_reading(t, rng))

    assert result.sample.accel_magnitude == pytest.approx(GRAVITY, abs=0.1)
    assert result.g_force == pytest.approx(1.0, abs=0.01)


def test_motion_prevents_calibration_from_completing(
    calibration_config: CalibrationConfig,
) -> None:
    """A partial average taken while moving is worse than no correction."""
    calibrator = Calibrator(calibration_config)
    rng = random.Random(11)

    t = 0.0
    for i in range(200):
        t += 0.01
        reading = resting_reading(t, rng)
        if i % 10 == 0:
            reading = SensorReading(
                timestamp=t,
                source=SensorSource.IMU,
                payload={**reading.payload, "gx_dps": 60.0},
            )
        calibrator.apply(reading)

    assert not calibrator.calibrated


def accelerating_reading(timestamp: float, forward: float) -> SensorReading:
    return SensorReading(
        timestamp=timestamp,
        source=SensorSource.IMU,
        payload={
            "ax_mps2": 0.18 + forward,
            "ay_mps2": -0.12,
            "az_mps2": GRAVITY + 0.25,
            "gx_dps": GYRO_BIAS[0],
            "gy_dps": GYRO_BIAS[1],
            "gz_dps": GYRO_BIAS[2],
        },
    )


def settled_calibrator(config: CalibrationConfig, seed: int) -> tuple[Calibrator, float]:
    calibrator = Calibrator(config)
    rng = random.Random(seed)
    t = 0.0
    for _ in range(200):
        t += 0.01
        calibrator.apply(resting_reading(t, rng))
    assert calibrator.calibrated
    return calibrator, t


def test_gravity_is_separated_from_rider_acceleration(
    calibration_config: CalibrationConfig,
) -> None:
    calibrator, t = settled_calibrator(calibration_config, seed=13)

    # Brake or accelerate hard for a fifth of a second. Gravity must stay on z
    # and the manoeuvre must survive into `linear`.
    result = None
    for _ in range(20):
        t += 0.01
        result = calibrator.apply(accelerating_reading(t, forward=3.0))

    assert result is not None
    assert result.linear[0] == pytest.approx(3.0, abs=0.6)
    assert result.gravity[2] == pytest.approx(GRAVITY, abs=0.3)
    assert not result.is_stationary


def test_gravity_filter_is_slow_enough_to_pass_a_braking_event(
    calibration_config: CalibrationConfig,
) -> None:
    """Guards the cutoff frequency.

    At a 0.5 Hz cutoff the gravity estimate has a 0.32 s time constant, so it
    chases real acceleration and a three-second braking event is almost
    entirely reclassified as gravity. The observed symptom was hard braking
    reported as -0.86 m/s^2 when the truth was near -2.7.
    """
    calibrator, t = settled_calibrator(calibration_config, seed=17)

    # Three seconds of steady 3 m/s^2 deceleration, as in a real stop.
    minimum = 0.0
    for _ in range(300):
        t += 0.01
        result = calibrator.apply(accelerating_reading(t, forward=-3.0))
        minimum = min(minimum, result.linear[0])

    assert minimum < -2.0, f"braking attenuated to {minimum:.2f} m/s^2"


def test_gravity_still_follows_a_slow_attitude_change(
    calibration_config: CalibrationConfig,
) -> None:
    """The other half of the tradeoff: a long climb must be absorbed."""
    calibrator, t = settled_calibrator(calibration_config, seed=19)

    # Thirty seconds tilted nose-up, which is a hill, not acceleration.
    tilt = -GRAVITY * 0.1
    result = None
    for _ in range(3000):
        t += 0.01
        result = calibrator.apply(accelerating_reading(t, forward=tilt))

    assert result is not None
    assert result.linear[0] == pytest.approx(0.0, abs=0.2)
    assert result.gravity[0] == pytest.approx(0.18 + tilt, abs=0.2)


def test_static_config_bias_is_applied_without_auto_calibration() -> None:
    config = CalibrationConfig(
        auto_calibrate_on_start=False,
        gyro_bias_dps=[1.0, 2.0, 3.0],
        accel_bias_mps2=[0.5, 0.0, 0.0],
    )
    calibrator = Calibrator(config)
    assert calibrator.calibrated

    result = calibrator.apply(
        SensorReading(
            timestamp=1.0,
            source=SensorSource.IMU,
            payload={
                "ax_mps2": 0.5,
                "ay_mps2": 0.0,
                "az_mps2": GRAVITY,
                "gx_dps": 1.0,
                "gy_dps": 2.0,
                "gz_dps": 3.0,
            },
        )
    )

    assert result.sample.ax_mps2 == pytest.approx(0.0, abs=1e-6)
    assert result.sample.gx_dps == pytest.approx(0.0, abs=1e-6)
    assert result.sample.gz_dps == pytest.approx(0.0, abs=1e-6)
