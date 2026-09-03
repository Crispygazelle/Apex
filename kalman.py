"""State-estimation engine for fusing IMU and GPS observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import atan2, degrees, sqrt
from typing import Dict, Optional


@dataclass(frozen=True)
class SensorObservation:
    """Structured observation consumed by the fusion engine."""

    timestamp: float
    source: str
    data: Dict[str, float] = field(default_factory=dict)
    quality: float = 1.0
    sequence: int = 0


@dataclass
class MotionState:
    """Filtered motion state exposed to downstream logic."""

    timestamp: float = 0.0
    x_m: float = 0.0
    y_m: float = 0.0
    vx_mps: float = 0.0
    vy_mps: float = 0.0
    speed_mps: float = 0.0
    heading_deg: float = 0.0
    ax_mps2: float = 0.0
    ay_mps2: float = 0.0
    position_confidence: float = 0.0
    velocity_confidence: float = 0.0
    acceleration_confidence: float = 0.0
    mode: str = "init"


class KalmanEngine:
    """Constant-velocity Kalman filter with IMU acceleration control input."""

    def __init__(
        self,
        process_noise: float = 0.8,
        gps_position_std_m: float = 4.0,
        gps_velocity_std_mps: float = 1.2,
    ) -> None:
        self.process_noise = max(process_noise, 1e-6)
        self.gps_position_std_m = max(gps_position_std_m, 1e-3)
        self.gps_velocity_std_mps = max(gps_velocity_std_mps, 1e-3)

        self._state = [0.0, 0.0, 0.0, 0.0]  # x, y, vx, vy
        self._cov = self._scaled_identity(4, 250.0)
        self._last_timestamp: Optional[float] = None
        self._accel = [0.0, 0.0]
        self._accel_confidence = 0.0
        self._mode = "init"

    def reset(self, timestamp: Optional[float] = None) -> MotionState:
        """Reset estimator state and covariance."""
        self._state = [0.0, 0.0, 0.0, 0.0]
        self._cov = self._scaled_identity(4, 250.0)
        self._last_timestamp = timestamp
        self._accel = [0.0, 0.0]
        self._accel_confidence = 0.0
        self._mode = "init"
        return self.get_state()

    def push_observation(self, observation: SensorObservation) -> MotionState:
        """Process one IMU/GPS observation and return the latest fused state."""
        if self._last_timestamp is None:
            self._last_timestamp = observation.timestamp

        dt = max(0.0, observation.timestamp - self._last_timestamp)
        if dt > 0:
            self._predict(dt)
            self._last_timestamp = observation.timestamp

        if observation.source == "imu":
            self._integrate_imu(observation)
            self._mode = "predict-only"
        elif observation.source == "gps":
            self._update_with_gps(observation)
            self._mode = "fused"
        else:
            self._mode = "unknown-source"

        return self.get_state()

    def get_state(self) -> MotionState:
        """Return current filtered state and confidence values."""
        vx = float(self._state[2])
        vy = float(self._state[3])
        speed = sqrt(vx * vx + vy * vy)
        heading = degrees(atan2(vy, vx)) if speed > 1e-3 else 0.0

        position_cov = float((self._cov[0][0] + self._cov[1][1]) / 2.0)
        velocity_cov = float((self._cov[2][2] + self._cov[3][3]) / 2.0)

        return MotionState(
            timestamp=float(self._last_timestamp or 0.0),
            x_m=float(self._state[0]),
            y_m=float(self._state[1]),
            vx_mps=vx,
            vy_mps=vy,
            speed_mps=speed,
            heading_deg=heading,
            ax_mps2=float(self._accel[0]),
            ay_mps2=float(self._accel[1]),
            position_confidence=self._cov_to_confidence(position_cov),
            velocity_confidence=self._cov_to_confidence(velocity_cov),
            acceleration_confidence=self._accel_confidence,
            mode=self._mode,
        )

    def _predict(self, dt: float) -> None:
        x, y, vx, vy = self._state
        ax, ay = self._accel

        self._state[0] = x + (vx * dt) + (0.5 * ax * dt * dt)
        self._state[1] = y + (vy * dt) + (0.5 * ay * dt * dt)
        self._state[2] = vx + (ax * dt)
        self._state[3] = vy + (ay * dt)

        f = [
            [1.0, 0.0, dt, 0.0],
            [0.0, 1.0, 0.0, dt],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]

        q_base = self.process_noise ** 2
        dt2 = dt * dt
        dt3 = dt2 * dt
        dt4 = dt2 * dt2
        q = [
            [q_base * dt4 / 4.0, 0.0, q_base * dt3 / 2.0, 0.0],
            [0.0, q_base * dt4 / 4.0, 0.0, q_base * dt3 / 2.0],
            [q_base * dt3 / 2.0, 0.0, q_base * dt2, 0.0],
            [0.0, q_base * dt3 / 2.0, 0.0, q_base * dt2],
        ]

        self._cov = self._add_matrix(self._mul_matrix(self._mul_matrix(f, self._cov), self._transpose(f)), q)

    def _integrate_imu(self, observation: SensorObservation) -> None:
        ax = float(observation.data.get("ax_mps2", 0.0))
        ay = float(observation.data.get("ay_mps2", 0.0))
        self._accel = [ax, ay]

        clipped_quality = max(0.0, min(1.0, observation.quality))
        self._accel_confidence = (0.85 * self._accel_confidence) + (0.15 * clipped_quality)

    def _update_with_gps(self, observation: SensorObservation) -> None:
        zx = float(observation.data.get("x_m", self._state[0]))
        zy = float(observation.data.get("y_m", self._state[1]))
        zvx = float(observation.data.get("vx_mps", self._state[2]))
        zvy = float(observation.data.get("vy_mps", self._state[3]))

        pos_std = max(float(observation.data.get("position_std_m", self.gps_position_std_m)), 0.5)
        vel_std = max(float(observation.data.get("velocity_std_mps", self.gps_velocity_std_mps)), 0.2)
        quality_scale = 1.0 / max(0.25, min(observation.quality, 1.0))

        r = [
            (pos_std * quality_scale) ** 2,
            (pos_std * quality_scale) ** 2,
            (vel_std * quality_scale) ** 2,
            (vel_std * quality_scale) ** 2,
        ]

        # For H = identity and independent measurement noise, scalar updates are stable and fast.
        measurements = [zx, zy, zvx, zvy]
        for i, z in enumerate(measurements):
            innovation = z - self._state[i]
            s = self._cov[i][i] + r[i]
            if s <= 1e-9:
                continue

            k = [self._cov[row][i] / s for row in range(4)]
            for row in range(4):
                self._state[row] += k[row] * innovation

            old_cov_col = [self._cov[i][col] for col in range(4)]
            for row in range(4):
                for col in range(4):
                    self._cov[row][col] -= k[row] * old_cov_col[col]

    @staticmethod
    def _scaled_identity(size: int, scalar: float) -> list[list[float]]:
        matrix = [[0.0 for _ in range(size)] for _ in range(size)]
        for i in range(size):
            matrix[i][i] = scalar
        return matrix

    @staticmethod
    def _transpose(matrix: list[list[float]]) -> list[list[float]]:
        rows = len(matrix)
        cols = len(matrix[0])
        return [[matrix[r][c] for r in range(rows)] for c in range(cols)]

    @staticmethod
    def _mul_matrix(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
        rows = len(left)
        cols = len(right[0])
        k_dim = len(right)

        out = [[0.0 for _ in range(cols)] for _ in range(rows)]
        for r in range(rows):
            for k in range(k_dim):
                lv = left[r][k]
                if lv == 0.0:
                    continue
                for c in range(cols):
                    out[r][c] += lv * right[k][c]
        return out

    @staticmethod
    def _add_matrix(left: list[list[float]], right: list[list[float]]) -> list[list[float]]:
        rows = len(left)
        cols = len(left[0])
        return [[left[r][c] + right[r][c] for c in range(cols)] for r in range(rows)]

    @staticmethod
    def _cov_to_confidence(variance: float) -> float:
        safe_var = max(variance, 1e-9)
        return max(0.0, min(1.0, 1.0 / (1.0 + safe_var / 25.0)))
