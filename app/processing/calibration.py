"""IMU bias correction and gravity separation.

Two jobs, often conflated:

1. **Bias correction.** A gyroscope at rest should read zero, so its resting
   mean *is* its bias and can be removed outright. An accelerometer at rest
   cannot be treated the same way: a reading of (0.2, 0, 9.7) is equally
   explained by sensor bias or by the helmet being mounted slightly tilted, and
   nothing measured at rest can tell those apart. Pretending otherwise would
   bake the mounting angle into the bias and permanently skew the lean angle.
   So we estimate the gyro bias fully, and for the accelerometer we correct
   only what is observable: the magnitude, against known gravity.

2. **Gravity separation.** The accelerometer always reports gravity plus rider
   acceleration. A one-pole low-pass tracks the gravity component, which moves
   slowly as the helmet tilts; subtracting it leaves the linear acceleration
   that `metrics` and the Kalman filter actually want.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import pi, sqrt

from app.config import CalibrationConfig
from app.models import ImuSample, SensorReading

# Stationarity is judged over a window rather than from a single sample.
STATIONARY_WINDOW = 20
# Peak-to-peak accelerometer magnitude allowed while "at rest", in m/s^2.
STATIONARY_ACCEL_SPREAD = 0.6
# How far the mean magnitude may sit from gravity *before* calibration, when it
# must absorb the very bias we have not measured yet.
UNCALIBRATED_MAGNITUDE_TOLERANCE = 1.0
# Once the bias is known the tolerance tightens, because a loose one misreads
# steady acceleration as rest: spread-based detection sees no variation in a
# constant 0.3 g push, so only the magnitude check can catch it.
CALIBRATED_MAGNITUDE_TOLERANCE = 0.35


@dataclass(frozen=True, slots=True)
class CalibratedImu:
    """One IMU sample after correction, split into gravity and motion."""

    sample: ImuSample
    gravity: tuple[float, float, float]
    linear: tuple[float, float, float]
    is_stationary: bool
    dt: float

    @property
    def g_force(self) -> float:
        """Total specific force in g. Reads 1.0 at rest, which is what a rider
        means by 'g-force' and what crash thresholds are quoted against."""
        return self.sample.accel_magnitude / 9.80665


class Calibrator:
    """Stateful per-sample corrector with optional startup auto-calibration."""

    def __init__(self, config: CalibrationConfig) -> None:
        self.config = config

        # Static corrections from a previous bench calibration, applied first.
        self._accel_bias = list(config.accel_bias_mps2)[:3] or [0.0, 0.0, 0.0]
        self._accel_scale = list(config.accel_scale)[:3] or [1.0, 1.0, 1.0]
        self._gyro_bias = list(config.gyro_bias_dps)[:3] or [0.0, 0.0, 0.0]

        # Learned at startup.
        self._auto_pending = config.auto_calibrate_on_start
        self._gyro_samples: list[tuple[float, float, float]] = []
        self._accel_magnitudes: list[float] = []
        self._magnitude_scale = 1.0
        self._resting_gravity: tuple[float, float, float] | None = None

        self._gravity: list[float] | None = None
        self._last_timestamp: float | None = None
        self._motion_window: deque[tuple[tuple[float, ...], tuple[float, ...]]] = deque(
            maxlen=STATIONARY_WINDOW
        )

    # --- state ------------------------------------------------------------

    @property
    def calibrated(self) -> bool:
        """True once the startup routine has finished, or was never needed."""
        return not self._auto_pending

    @property
    def progress(self) -> float:
        if not self._auto_pending:
            return 1.0
        return min(1.0, len(self._gyro_samples) / max(self.config.stationary_samples, 1))

    @property
    def gyro_bias(self) -> tuple[float, float, float]:
        return (self._gyro_bias[0], self._gyro_bias[1], self._gyro_bias[2])

    @property
    def magnitude_scale(self) -> float:
        return self._magnitude_scale

    @property
    def resting_gravity(self) -> tuple[float, float, float] | None:
        """Gravity direction recorded at rest; this is the mounting reference."""
        return self._resting_gravity

    # --- main entry point -------------------------------------------------

    def apply(self, reading: SensorReading) -> CalibratedImu:
        raw_accel = (
            reading.get("ax_mps2"),
            reading.get("ay_mps2"),
            reading.get("az_mps2"),
        )
        raw_gyro = (
            reading.get("gx_dps"),
            reading.get("gy_dps"),
            reading.get("gz_dps"),
        )

        dt = 0.0
        if self._last_timestamp is not None:
            dt = max(0.0, reading.timestamp - self._last_timestamp)
        self._last_timestamp = reading.timestamp

        accel = tuple(
            (raw_accel[i] - self._accel_bias[i]) * self._accel_scale[i] * self._magnitude_scale
            for i in range(3)
        )
        gyro = tuple(raw_gyro[i] - self._gyro_bias[i] for i in range(3))

        stationary = self._is_stationary(accel, gyro)
        if self._auto_pending:
            self._collect(raw_accel, raw_gyro, stationary)

        gravity = self._track_gravity(accel, dt)
        linear = (accel[0] - gravity[0], accel[1] - gravity[1], accel[2] - gravity[2])

        sample = ImuSample(
            timestamp=reading.timestamp,
            ax_mps2=accel[0],
            ay_mps2=accel[1],
            az_mps2=accel[2],
            gx_dps=gyro[0],
            gy_dps=gyro[1],
            gz_dps=gyro[2],
            temperature_c=reading.get("temperature_c"),
        )
        return CalibratedImu(
            sample=sample,
            gravity=gravity,
            linear=linear,
            is_stationary=stationary,
            dt=dt,
        )

    # --- internals --------------------------------------------------------

    def _is_stationary(self, accel: tuple[float, ...], gyro: tuple[float, ...]) -> bool:
        """Detect rest from short-term *variation*, not absolute magnitude.

        Comparing |gyro| against a threshold looks obvious and does not work:
        an uncalibrated gyro's bias is often larger than the threshold, so the
        sensor would never be declared at rest and the bias could never be
        measured. Peak-to-peak spread is immune to constant bias, which is
        exactly the property needed here.
        """
        self._motion_window.append((accel, gyro))
        if len(self._motion_window) < STATIONARY_WINDOW:
            return False

        for axis in range(3):
            values = [g[axis] for _, g in self._motion_window]
            if max(values) - min(values) > self.config.stationary_gyro_threshold_dps:
                return False

        magnitudes = [
            sqrt(sum(component * component for component in a)) for a, _ in self._motion_window
        ]
        if max(magnitudes) - min(magnitudes) > STATIONARY_ACCEL_SPREAD:
            return False

        tolerance = (
            UNCALIBRATED_MAGNITUDE_TOLERANCE
            if self._auto_pending
            else CALIBRATED_MAGNITUDE_TOLERANCE
        )
        mean_magnitude = sum(magnitudes) / len(magnitudes)
        return abs(mean_magnitude - self.config.gravity_mps2) < tolerance

    def _collect(
        self,
        raw_accel: tuple[float, ...],
        raw_gyro: tuple[float, ...],
        stationary: bool,
    ) -> None:
        """Gather resting samples until there are enough to solve for bias."""
        if not stationary:
            # Movement invalidates the window; a partial average would be worse
            # than no correction at all.
            self._gyro_samples.clear()
            self._accel_magnitudes.clear()
            return

        self._gyro_samples.append((raw_gyro[0], raw_gyro[1], raw_gyro[2]))
        self._accel_magnitudes.append(
            sqrt(sum(component * component for component in raw_accel))
        )

        if len(self._gyro_samples) < self.config.stationary_samples:
            return

        count = float(len(self._gyro_samples))
        self._gyro_bias = [
            self._gyro_bias[axis] + sum(s[axis] for s in self._gyro_samples) / count
            for axis in range(3)
        ]

        mean_magnitude = sum(self._accel_magnitudes) / count
        if mean_magnitude > 1.0:
            self._magnitude_scale = self.config.gravity_mps2 / mean_magnitude

        self._resting_gravity = (raw_accel[0], raw_accel[1], raw_accel[2])
        self._auto_pending = False
        self._gyro_samples.clear()
        self._accel_magnitudes.clear()

    def _track_gravity(self, accel: tuple[float, ...], dt: float) -> tuple[float, float, float]:
        if self._gravity is None:
            # Seed from the first sample. This is why no "converge faster while
            # stationary" shortcut is needed: there is no settling transient to
            # shorten, and such a shortcut would be actively harmful, because a
            # steady acceleration can look stationary and would then be
            # absorbed into gravity within a few samples.
            self._gravity = [accel[0], accel[1], accel[2]]
            return (accel[0], accel[1], accel[2])

        if dt <= 0.0:
            return (self._gravity[0], self._gravity[1], self._gravity[2])

        # One-pole low pass: alpha = dt / (dt + RC), RC = 1 / (2*pi*fc).
        rc = 1.0 / (2.0 * pi * max(self.config.gravity_filter_hz, 1e-3))
        alpha = dt / (dt + rc)

        for axis in range(3):
            self._gravity[axis] += alpha * (accel[axis] - self._gravity[axis])

        return (self._gravity[0], self._gravity[1], self._gravity[2])
