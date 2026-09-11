"""Rider-facing derived quantities: g-force, lean, gradient, distance.

The lean angle deserves an explanation, because the obvious implementation is
wrong. You cannot read motorcycle lean off an accelerometer. In a coordinated
turn the bike leans exactly until gravity and centripetal force combine to
point along its own vertical axis, so a helmet-mounted accelerometer reports
almost no lateral force no matter how hard you are cornering. `atan2(ay, az)`
would report roughly zero lean at full lean.

What does work, and what the README always specified, is integrating the
gyroscope's roll rate. On its own that drifts without bound. So the integrator
is corrected against an independent estimate of the same angle derived from
speed and yaw rate, tan(lean) = v * omega / g, using a complementary filter:
the gyro supplies the fast response, the kinematic reference removes the drift.
"""

from __future__ import annotations

from collections import deque
from math import atan, atan2, degrees, hypot, radians, sqrt

from app.config import CalibrationConfig
from app.geo import haversine_m
from app.models import RideMetrics
from app.processing.calibration import CalibratedImu

# Weight given to the kinematic lean reference each IMU sample. Small, because
# the gyro is trusted for short-term response; this only bleeds off drift.
LEAN_CORRECTION_GAIN = 0.02

# Below this speed the lean reference is meaningless (v * omega goes to zero)
# and GPS position wanders while parked, so both are suppressed.
MIN_MOTION_MPS = 0.5

# Gradient needs a long baseline. GPS altitude carries several metres of noise,
# so a slope taken over 10 m of travel is pure noise: 4 m of error over 10 m of
# run reads as a 40% grade. Over 150 m the same error is under 3%.
GRADIENT_BASELINE_M = 150.0
MIN_GRADIENT_RUN_M = 60.0


class MetricsCalculator:
    """Accumulates derived metrics across a ride."""

    def __init__(self, config: CalibrationConfig | None = None) -> None:
        self.config = config or CalibrationConfig()
        self.gravity = self.config.gravity_mps2

        self._lean_deg = 0.0
        self._pitch_deg = 0.0
        self._longitudinal = 0.0
        self._lateral = 0.0
        self._vertical = 0.0
        self._g_force = 1.0

        self._distance_m = 0.0
        self._max_speed_kmh = 0.0
        self._max_g_force = 0.0
        self._gradient_pct = 0.0

        self._last_fix: tuple[float, float, float] | None = None
        self._altitude_window: deque[tuple[float, float]] = deque()
        self._gps_distance_m = 0.0

    # --- inputs -----------------------------------------------------------

    def ingest_imu(self, calibrated: CalibratedImu, speed_mps: float) -> None:
        """Update orientation, acceleration, and the odometer."""
        sample = calibrated.sample

        self._longitudinal, self._lateral, self._vertical = calibrated.linear
        self._g_force = calibrated.g_force
        self._max_g_force = max(self._max_g_force, self._g_force)

        self._pitch_deg = _pitch_from_gravity(calibrated.gravity)
        self._update_lean(sample.gx_dps, sample.gz_dps, speed_mps, calibrated.dt)

        # Integrate the *filtered* speed rather than summing distances between
        # raw fixes. Summing raw fixes is the classic GPS odometer bug: at 10 Hz
        # with 3 m of position noise, consecutive fixes differ by ~4 m of pure
        # noise, which accumulates to tens of metres per second of phantom
        # travel. A parked bike would clock up kilometres.
        if speed_mps >= MIN_MOTION_MPS and calibrated.dt > 0.0:
            self._distance_m += speed_mps * calibrated.dt

    def _update_lean(
        self, roll_rate_dps: float, yaw_rate_dps: float, speed_mps: float, dt: float
    ) -> None:
        if dt <= 0.0:
            return

        # Fast path: integrate the gyro.
        integrated = self._lean_deg + roll_rate_dps * dt

        if speed_mps < MIN_MOTION_MPS:
            # Standing still, the bike is upright and being held, so decay to
            # zero rather than integrating gyro noise forever.
            self._lean_deg = integrated * 0.98
            return

        # Slow path: the angle a bike must hold to balance this turn.
        reference = degrees(atan(speed_mps * radians(yaw_rate_dps) / self.gravity))
        self._lean_deg = (
            1.0 - LEAN_CORRECTION_GAIN
        ) * integrated + LEAN_CORRECTION_GAIN * reference

    def ingest_gps(
        self,
        latitude: float,
        longitude: float,
        altitude_m: float,
        speed_mps: float,
        *,
        valid: bool,
    ) -> None:
        """Feed the gradient estimator and track raw GPS ground distance."""
        if not valid:
            return

        if self._last_fix is not None and speed_mps >= MIN_MOTION_MPS:
            previous_lat, previous_lon, _ = self._last_fix
            step = haversine_m(previous_lat, previous_lon, latitude, longitude)
            # A jump larger than physically possible between fixes is a bad
            # fix, not travel; 100 m at 10 Hz would be 3600 km/h.
            if step < 100.0:
                # Kept for comparison against the filtered odometer, not used
                # as the reported distance.
                self._gps_distance_m += step

        self._last_fix = (latitude, longitude, altitude_m)

        if speed_mps >= MIN_MOTION_MPS:
            self._altitude_window.append((self._distance_m, altitude_m))
            self._trim_altitude_window()
            self._recompute_gradient()

    def _trim_altitude_window(self) -> None:
        cutoff = self._distance_m - GRADIENT_BASELINE_M
        while len(self._altitude_window) > 2 and self._altitude_window[0][0] < cutoff:
            self._altitude_window.popleft()

    def _recompute_gradient(self) -> None:
        """Least-squares slope of altitude against distance over the window."""
        if len(self._altitude_window) < 4:
            return

        points = list(self._altitude_window)
        run = points[-1][0] - points[0][0]
        if run < MIN_GRADIENT_RUN_M:
            # Not enough baseline yet; keep the previous value rather than
            # publishing noise.
            return

        n = float(len(points))
        mean_distance = sum(p[0] for p in points) / n
        mean_altitude = sum(p[1] for p in points) / n
        covariance = sum((p[0] - mean_distance) * (p[1] - mean_altitude) for p in points)
        variance = sum((p[0] - mean_distance) ** 2 for p in points)
        if variance <= 1e-6:
            return

        self._gradient_pct = (covariance / variance) * 100.0

    @property
    def gps_only_distance_m(self) -> float:
        """Distance summed from raw fixes. Inflated by noise; diagnostic only."""
        return self._gps_distance_m

    # --- output -----------------------------------------------------------

    def snapshot(self, speed_mps: float) -> RideMetrics:
        speed_kmh = speed_mps * 3.6
        self._max_speed_kmh = max(self._max_speed_kmh, speed_kmh)

        return RideMetrics(
            speed_kmh=speed_kmh,
            g_force=self._g_force,
            lean_angle_deg=self._lean_deg,
            pitch_deg=self._pitch_deg,
            longitudinal_accel_mps2=self._longitudinal,
            lateral_accel_mps2=self._lateral,
            vertical_accel_mps2=self._vertical,
            gradient_pct=self._gradient_pct,
            distance_m=self._distance_m,
            max_speed_kmh=self._max_speed_kmh,
            max_g_force=self._max_g_force,
        )


def _pitch_from_gravity(gravity: tuple[float, float, float]) -> float:
    """Nose-up angle from the tracked gravity vector.

    At rest and level the vector reads (0, 0, +g) and this returns 0. Pitched
    nose-up by theta it reads g*(-sin, 0, cos), which recovers +theta.
    """
    gx, gy, gz = gravity
    horizontal = hypot(gy, gz)
    if horizontal < 1e-6 and abs(gx) < 1e-6:
        return 0.0
    return degrees(atan2(-gx, horizontal))


def instantaneous_speed_mps(
    lat1: float, lon1: float, lat2: float, lon2: float, dt: float
) -> float:
    """Haversine speed between successive fixes, as the README specifies.

    The Kalman filter's velocity estimate is better, but this is the honest
    GPS-only figure and is used to sanity-check it.
    """
    if dt <= 0.0:
        return 0.0
    return haversine_m(lat1, lon1, lat2, lon2) / dt


def roll_from_gravity(gravity: tuple[float, float, float]) -> float:
    """Static tilt about the forward axis.

    Valid only when stationary or travelling straight. Kept because it is the
    correct way to read tilt at rest, and `Calibrator` uses it as a mounting
    reference, but it is deliberately not used for lean while cornering.
    """
    _, gy, gz = gravity
    if abs(gy) < 1e-9 and abs(gz) < 1e-9:
        return 0.0
    return degrees(atan2(gy, sqrt(gz * gz + 1e-12)))
