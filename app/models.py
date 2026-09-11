"""Data contracts passed between every layer of the pipeline.

Everything here is immutable. A reading produced by a sensor thread is handed to
the event loop and then fanned out to storage, streaming, and safety, so shared
mutable state would be a race waiting to happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SensorSource(StrEnum):
    """Origin of a reading. Values double as InfluxDB tag values."""

    IMU = "imu"
    GPS = "gps"
    MIC = "mic"


class FusionMode(StrEnum):
    """How the estimator arrived at the current state."""

    INIT = "init"
    DEAD_RECKONING = "dead_reckoning"
    FUSED = "fused"


class SystemState(StrEnum):
    """Top-level node state. Drives the status LED and voice gating."""

    STARTING = "starting"
    CALIBRATING = "calibrating"
    ACQUIRING_GPS = "acquiring_gps"
    RIDING = "riding"
    CRASH_SUSPECTED = "crash_suspected"
    SOS_ACTIVE = "sos_active"
    DEGRADED = "degraded"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class SensorReading:
    """One sample from one sensor.

    `timestamp` is epoch seconds taken from the monotonic clock in `app.clock`,
    never from `time.time()` directly, so an NTP correction mid-ride cannot make
    dt negative and blow up the filter.
    """

    timestamp: float
    source: SensorSource
    payload: dict[str, Any] = field(default_factory=dict)
    quality: float = 1.0

    def get(self, key: str, default: float = 0.0) -> float:
        value = self.payload.get(key, default)
        return float(value) if isinstance(value, (int, float)) else default


@dataclass(frozen=True, slots=True)
class ImuSample:
    """Calibrated IMU data in SI units, body frame."""

    timestamp: float
    ax_mps2: float
    ay_mps2: float
    az_mps2: float
    gx_dps: float
    gy_dps: float
    gz_dps: float
    temperature_c: float = 0.0

    @property
    def accel_magnitude(self) -> float:
        return (self.ax_mps2**2 + self.ay_mps2**2 + self.az_mps2**2) ** 0.5


@dataclass(frozen=True, slots=True)
class GpsFix:
    """A parsed GPS position fix."""

    timestamp: float
    latitude: float
    longitude: float
    altitude_m: float = 0.0
    speed_mps: float = 0.0
    course_deg: float = 0.0
    satellites: int = 0
    hdop: float = 99.0
    valid: bool = False

    @property
    def position_std_m(self) -> float:
        """Rough horizontal accuracy estimate from HDOP.

        5 m is the typical NEO-M8N UERE; HDOP scales it.
        """
        return max(1.0, min(self.hdop, 20.0) * 5.0)


@dataclass(frozen=True, slots=True)
class MotionState:
    """Filtered kinematic state in the local ENU frame, plus geodetic position."""

    timestamp: float = 0.0
    x_m: float = 0.0
    y_m: float = 0.0
    vx_mps: float = 0.0
    vy_mps: float = 0.0
    speed_mps: float = 0.0
    heading_deg: float = 0.0
    ax_mps2: float = 0.0
    ay_mps2: float = 0.0
    latitude: float = 0.0
    longitude: float = 0.0
    altitude_m: float = 0.0
    position_confidence: float = 0.0
    velocity_confidence: float = 0.0
    acceleration_confidence: float = 0.0
    mode: FusionMode = FusionMode.INIT


@dataclass(frozen=True, slots=True)
class RideMetrics:
    """Rider-facing derived quantities."""

    speed_kmh: float = 0.0
    g_force: float = 0.0
    lean_angle_deg: float = 0.0
    pitch_deg: float = 0.0
    longitudinal_accel_mps2: float = 0.0
    lateral_accel_mps2: float = 0.0
    vertical_accel_mps2: float = 0.0
    gradient_pct: float = 0.0
    distance_m: float = 0.0
    max_speed_kmh: float = 0.0
    max_g_force: float = 0.0


@dataclass(frozen=True, slots=True)
class RideSample:
    """The unit of record: one fused, enriched telemetry point.

    This is what lands in the ring buffer, InfluxDB, and the WebSocket feed.
    """

    timestamp: float
    ride_id: str
    state: MotionState
    metrics: RideMetrics
    system_state: SystemState = SystemState.RIDING
    gps_valid: bool = False
    satellites: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Flat JSON-safe projection for the WebSocket and REST layers."""
        return {
            "timestamp": self.timestamp,
            "ride_id": self.ride_id,
            "system_state": self.system_state.value,
            "gps_valid": self.gps_valid,
            "satellites": self.satellites,
            "latitude": self.state.latitude,
            "longitude": self.state.longitude,
            "altitude_m": self.state.altitude_m,
            "speed_kmh": self.metrics.speed_kmh,
            "speed_mps": self.state.speed_mps,
            "heading_deg": self.state.heading_deg,
            "g_force": self.metrics.g_force,
            "lean_angle_deg": self.metrics.lean_angle_deg,
            "pitch_deg": self.metrics.pitch_deg,
            "longitudinal_accel_mps2": self.metrics.longitudinal_accel_mps2,
            "lateral_accel_mps2": self.metrics.lateral_accel_mps2,
            "gradient_pct": self.metrics.gradient_pct,
            "distance_m": self.metrics.distance_m,
            "max_speed_kmh": self.metrics.max_speed_kmh,
            "max_g_force": self.metrics.max_g_force,
            "fusion_mode": self.state.mode.value,
            "position_confidence": self.state.position_confidence,
            "velocity_confidence": self.state.velocity_confidence,
        }


@dataclass(frozen=True, slots=True)
class HazardReport:
    """A GPS-tagged hazard logged by the rider."""

    timestamp: float
    ride_id: str
    hazard_type: str
    latitude: float
    longitude: float
    note: str = ""


@dataclass(frozen=True, slots=True)
class CrashEvent:
    """A confirmed or suspected impact."""

    timestamp: float
    ride_id: str
    peak_g: float
    speed_before_kmh: float
    speed_after_kmh: float
    latitude: float
    longitude: float
    confirmed: bool = False
    reason: str = ""
