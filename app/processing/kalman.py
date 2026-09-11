"""Constant-velocity Kalman filter fusing IMU acceleration with GPS fixes.

State is [east, north, v_east, v_north] in metres and m/s on the local tangent
plane. IMU acceleration enters as a control input at 100 Hz; GPS position and
velocity arrive as measurements at ~10 Hz.

Differences from the original `kalman.py`:

  - numpy replaces the hand-written matrix helpers, so the update is a real
    matrix operation instead of four independent scalar updates that silently
    assumed a diagonal covariance.
  - the covariance update uses the Joseph form, which stays symmetric and
    positive-definite over a long ride where the naive form drifts.
  - GPS fixes are gated on their normalised innovation, so one wild fix off a
    building reflection cannot yank the state.
  - explicit dead-reckoning mode once GPS has been silent too long, which is
    what the README always claimed but the code never reported.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.config import FusionConfig
from app.models import FusionMode

# Reject a GPS fix whose innovation exceeds this many standard deviations.
# 4 sigma passes essentially all honest noise while catching gross outliers.
INNOVATION_GATE_SIGMA = 4.0

# After this many fixes in a row have been gated out, believe the GPS instead
# of ourselves. Gating protects against a bad fix, but with no escape hatch it
# becomes a trap: once dead reckoning has drifted beyond the gate, every real
# fix looks like an outlier and the filter never accepts another one. Observed
# directly in testing, where the estimate stayed 145 m from truth for the rest
# of the ride after a 12-second tunnel.
MAX_CONSECUTIVE_REJECTIONS = 5

# Initial covariance: position and velocity are both unknown at startup.
INITIAL_POSITION_VARIANCE = 250.0
INITIAL_VELOCITY_VARIANCE = 25.0


@dataclass(frozen=True, slots=True)
class FilterState:
    """Estimator output in the local ENU frame."""

    timestamp: float
    east_m: float
    north_m: float
    v_east_mps: float
    v_north_mps: float
    accel_east_mps2: float
    accel_north_mps2: float
    position_variance: float
    velocity_variance: float
    mode: FusionMode

    @property
    def speed_mps(self) -> float:
        return float(np.hypot(self.v_east_mps, self.v_north_mps))

    @property
    def position_confidence(self) -> float:
        return variance_to_confidence(self.position_variance, 25.0)

    @property
    def velocity_confidence(self) -> float:
        return variance_to_confidence(self.velocity_variance, 4.0)


def variance_to_confidence(variance: float, reference: float) -> float:
    """Map a variance onto 0..1, where `reference` is 'acceptable'."""
    return float(max(0.0, min(1.0, 1.0 / (1.0 + max(variance, 0.0) / reference))))


class KalmanEngine:
    """Predict on IMU, correct on GPS."""

    def __init__(self, config: FusionConfig | None = None) -> None:
        self.config = config or FusionConfig()
        self.reset()

    # --- lifecycle --------------------------------------------------------

    def reset(self, timestamp: float = 0.0) -> None:
        """Return to the pre-fix state. Safe to call mid-ride."""
        self._state = np.zeros(4, dtype=float)
        self._cov = np.diag(
            [
                INITIAL_POSITION_VARIANCE,
                INITIAL_POSITION_VARIANCE,
                INITIAL_VELOCITY_VARIANCE,
                INITIAL_VELOCITY_VARIANCE,
            ]
        ).astype(float)
        self._accel = np.zeros(2, dtype=float)
        self._timestamp = timestamp
        self._last_gps_timestamp: float | None = None
        self._initialised = False
        self._consecutive_rejections = 0
        self.rejected_fixes = 0
        self.reacquisitions = 0

    def initialise(
        self,
        timestamp: float,
        east_m: float,
        north_m: float,
        v_east_mps: float = 0.0,
        v_north_mps: float = 0.0,
    ) -> None:
        """Jump straight to a known position, used on the first valid fix."""
        self._state = np.array([east_m, north_m, v_east_mps, v_north_mps], dtype=float)
        self._cov = np.diag(
            [
                self.config.gps_position_std_m**2,
                self.config.gps_position_std_m**2,
                self.config.gps_velocity_std_mps**2,
                self.config.gps_velocity_std_mps**2,
            ]
        ).astype(float)
        self._timestamp = timestamp
        self._last_gps_timestamp = timestamp
        self._initialised = True

    @property
    def initialised(self) -> bool:
        return self._initialised

    # --- prediction -------------------------------------------------------

    def predict_to(
        self,
        timestamp: float,
        accel_east_mps2: float = 0.0,
        accel_north_mps2: float = 0.0,
    ) -> None:
        """Advance the state to `timestamp` using the given ENU acceleration."""
        if self._timestamp == 0.0:
            self._timestamp = timestamp

        dt = timestamp - self._timestamp
        self._accel = np.array([accel_east_mps2, accel_north_mps2], dtype=float)

        if dt <= 0.0:
            # Synchronization should prevent this, but a duplicate timestamp is
            # harmless: hold the state rather than integrating backwards.
            return

        # Clamp pathological gaps (a stalled thread, or a resumed process) so a
        # single huge dt cannot blow the covariance up to infinity.
        dt = min(dt, 1.0)

        transition = np.array(
            [
                [1.0, 0.0, dt, 0.0],
                [0.0, 1.0, 0.0, dt],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
        )
        control = np.array(
            [
                [0.5 * dt * dt, 0.0],
                [0.0, 0.5 * dt * dt],
                [dt, 0.0],
                [0.0, dt],
            ]
        )

        self._state = transition @ self._state + control @ self._accel
        self._cov = transition @ self._cov @ transition.T + self._process_noise(dt)
        self._timestamp = timestamp

    def _process_noise(self, dt: float) -> np.ndarray:
        """Continuous white-noise-acceleration model, discretised over dt.

        This is deliberately *not* the piecewise-constant-acceleration form
        that the original `kalman.py` used, which grows velocity variance as
        dt^2 rather than dt. That difference is not cosmetic: it makes the
        filter's confidence depend on the sample rate. Injecting dt^2 of noise
        per step means that over a fixed second, running at 100 Hz injects a
        tenth of the uncertainty that 10 Hz would, so raising the IMU rate made
        the filter *more* certain of a velocity it had no new information about.

        The observed symptom was a velocity variance that collapsed to ~0.03,
        giving GPS a Kalman gain of 0.018, so the filter effectively stopped
        listening: it reported 8.7 m/s while GPS insisted on 13.7 m/s.

        In this form the accumulated variance over an interval is sigma^2 * T
        regardless of how finely the interval is stepped.
        """
        q = self.config.process_noise**2
        dt2 = dt * dt
        dt3 = dt2 * dt
        return q * np.array(
            [
                [dt3 / 3.0, 0.0, dt2 / 2.0, 0.0],
                [0.0, dt3 / 3.0, 0.0, dt2 / 2.0],
                [dt2 / 2.0, 0.0, dt, 0.0],
                [0.0, dt2 / 2.0, 0.0, dt],
            ]
        )

    # --- correction -------------------------------------------------------

    def correct_gps(
        self,
        timestamp: float,
        east_m: float,
        north_m: float,
        v_east_mps: float,
        v_north_mps: float,
        position_std_m: float | None = None,
        velocity_std_mps: float | None = None,
    ) -> bool:
        """Fold in a GPS fix. Returns False if the fix was gated out."""
        if not self._initialised:
            self.initialise(timestamp, east_m, north_m, v_east_mps, v_north_mps)
            return True

        position_std = max(position_std_m or self.config.gps_position_std_m, 0.5)
        velocity_std = max(velocity_std_mps or self.config.gps_velocity_std_mps, 0.2)

        measurement = np.array([east_m, north_m, v_east_mps, v_north_mps], dtype=float)
        noise = np.diag(
            [position_std**2, position_std**2, velocity_std**2, velocity_std**2]
        ).astype(float)

        # H is the identity: GPS observes every state component directly.
        innovation = measurement - self._state
        innovation_cov = self._cov + noise

        if self._gated(innovation, innovation_cov):
            self.rejected_fixes += 1
            self._consecutive_rejections += 1

            if self._consecutive_rejections < MAX_CONSECUTIVE_REJECTIONS:
                return False

            # Several consecutive disagreements are not GPS noise; they mean our
            # own estimate has drifted. Re-acquire from the fix.
            self.reacquisitions += 1
            self._consecutive_rejections = 0
            self.initialise(timestamp, east_m, north_m, v_east_mps, v_north_mps)
            return True

        self._consecutive_rejections = 0

        gain = self._cov @ np.linalg.inv(innovation_cov)
        self._state = self._state + gain @ innovation

        # Joseph form: symmetric and positive-definite even after thousands of
        # updates, unlike P = (I - KH) P.
        identity = np.eye(4)
        closed_loop = identity - gain
        self._cov = closed_loop @ self._cov @ closed_loop.T + gain @ noise @ gain.T
        self._cov = 0.5 * (self._cov + self._cov.T)  # scrub rounding asymmetry

        self._last_gps_timestamp = timestamp
        return True

    def _gated(self, innovation: np.ndarray, innovation_cov: np.ndarray) -> bool:
        """True if the fix disagrees with the prediction beyond plausible noise."""
        sigmas = np.sqrt(np.maximum(np.diag(innovation_cov), 1e-9))
        normalised = np.abs(innovation) / sigmas
        # Only gate on position: GPS velocity is derived from Doppler and is
        # noisier in a way that would cause false rejections.
        return bool(np.any(normalised[:2] > INNOVATION_GATE_SIGMA))

    # --- output -----------------------------------------------------------

    @property
    def mode(self) -> FusionMode:
        if not self._initialised or self._last_gps_timestamp is None:
            return FusionMode.INIT
        gap = self._timestamp - self._last_gps_timestamp
        return FusionMode.FUSED if gap <= self.config.max_gps_gap_s else FusionMode.DEAD_RECKONING

    @property
    def gps_gap_s(self) -> float:
        if self._last_gps_timestamp is None:
            return float("inf")
        return max(0.0, self._timestamp - self._last_gps_timestamp)

    def state(self) -> FilterState:
        position_variance = float((self._cov[0, 0] + self._cov[1, 1]) / 2.0)
        velocity_variance = float((self._cov[2, 2] + self._cov[3, 3]) / 2.0)
        return FilterState(
            timestamp=self._timestamp,
            east_m=float(self._state[0]),
            north_m=float(self._state[1]),
            v_east_mps=float(self._state[2]),
            v_north_mps=float(self._state[3]),
            accel_east_mps2=float(self._accel[0]),
            accel_north_mps2=float(self._accel[1]),
            position_variance=position_variance,
            velocity_variance=velocity_variance,
            mode=self.mode,
        )
