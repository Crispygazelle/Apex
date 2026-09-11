"""Turns ordered sensor readings into fused, enriched telemetry.

This is where the layers meet: calibration cleans an IMU sample, the body-frame
acceleration is rotated into the navigation frame, the Kalman filter predicts,
GPS corrects, metrics derive, and a `RideSample` comes out.

The body-to-navigation rotation is the subtle part. The IMU measures forward
and lateral acceleration in the helmet's own frame; the filter needs east and
north. With compass heading psi (0 = north, clockwise), the bike's forward unit
vector is (sin psi, cos psi) and its left unit vector is (-cos psi, sin psi).
"""

from __future__ import annotations

from math import cos, radians, sin

from app.config import AppConfig
from app.geo import GeoOrigin, compass_heading
from app.models import (
    FusionMode,
    MotionState,
    RideSample,
    SensorReading,
    SensorSource,
    SystemState,
)
from app.processing.calibration import Calibrator
from app.processing.kalman import KalmanEngine
from app.processing.metrics import MIN_MOTION_MPS, MetricsCalculator


class Processor:
    """Stateful fusion pipeline for one ride."""

    def __init__(self, config: AppConfig, ride_id: str) -> None:
        self.config = config
        self.ride_id = ride_id

        self.calibrator = Calibrator(config.calibration)
        self.kalman = KalmanEngine(config.fusion)
        self.metrics = MetricsCalculator(config.calibration)

        self.origin: GeoOrigin | None = None
        self._last_accel_enu: tuple[float, float] = (0.0, 0.0)
        self._heading_deg = 0.0
        self._altitude_m = 0.0
        self._gps_valid = False
        self._satellites = 0
        self._started = False

        self._output_period = 1.0 / max(config.pipeline.output_rate_hz, 1.0)
        self._last_emit = 0.0

        # Set by the safety layer to force CRASH_SUSPECTED / SOS_ACTIVE, or by
        # the coordinator to force DEGRADED when a sensor has died.
        self.state_override: SystemState | None = None

        self.imu_count = 0
        self.gps_count = 0

    # --- state ------------------------------------------------------------

    @property
    def system_state(self) -> SystemState:
        if self.state_override is not None:
            return self.state_override
        if not self._started:
            return SystemState.STARTING
        if not self.calibrator.calibrated:
            return SystemState.CALIBRATING
        if not self._gps_valid:
            return SystemState.ACQUIRING_GPS
        return SystemState.RIDING

    # --- entry point ------------------------------------------------------

    def process(self, reading: SensorReading) -> RideSample | None:
        """Fold in one reading; return a sample when one is due."""
        self._started = True

        if reading.source is SensorSource.IMU:
            return self._on_imu(reading)
        if reading.source is SensorSource.GPS:
            return self._on_gps(reading)
        # Audio is consumed by the voice layer, not the telemetry pipeline.
        return None

    # --- IMU --------------------------------------------------------------

    def _on_imu(self, reading: SensorReading) -> RideSample | None:
        self.imu_count += 1
        calibrated = self.calibrator.apply(reading)

        filter_state = self.kalman.state()
        speed = filter_state.speed_mps

        self.metrics.ingest_imu(calibrated, speed)

        self._last_accel_enu = self._body_to_enu(
            forward=calibrated.linear[0],
            lateral=calibrated.linear[1],
            speed=speed,
            filter_heading=compass_heading(filter_state.v_east_mps, filter_state.v_north_mps),
        )
        self.kalman.predict_to(reading.timestamp, *self._last_accel_enu)

        if reading.timestamp - self._last_emit < self._output_period:
            return None
        self._last_emit = reading.timestamp
        return self._build_sample(reading.timestamp)

    def _body_to_enu(
        self, forward: float, lateral: float, speed: float, filter_heading: float
    ) -> tuple[float, float]:
        """Rotate body-frame acceleration into east/north."""
        if speed >= MIN_MOTION_MPS:
            # Moving: the velocity vector is the most reliable heading source.
            self._heading_deg = filter_heading

        psi = radians(self._heading_deg)
        sin_psi, cos_psi = sin(psi), cos(psi)

        east = forward * sin_psi - lateral * cos_psi
        north = forward * cos_psi + lateral * sin_psi
        return east, north

    # --- GPS --------------------------------------------------------------

    def _on_gps(self, reading: SensorReading) -> RideSample | None:
        self.gps_count += 1

        valid = bool(reading.payload.get("valid", False))
        self._satellites = int(reading.get("satellites"))

        if not valid:
            self._gps_valid = False
            return self._build_sample(reading.timestamp)

        latitude = reading.get("latitude")
        longitude = reading.get("longitude")
        self._altitude_m = reading.get("altitude_m")
        speed = reading.get("speed_mps")
        course = reading.get("course_deg")

        if self.origin is None:
            # First fix anchors the local tangent plane for the whole ride.
            self.origin = GeoOrigin(latitude, longitude, self._altitude_m)

        east, north = self.origin.to_enu(latitude, longitude)
        course_rad = radians(course)
        v_east = speed * sin(course_rad)
        v_north = speed * cos(course_rad)

        hdop = reading.get("hdop", 99.0)
        clamped_hdop = min(max(hdop, 0.5), 20.0)
        # 5 m is the NEO-M8N's typical user-equivalent range error; HDOP scales
        # it, so a fix taken with poor satellite geometry is trusted less.
        position_std = max(1.0, clamped_hdop * 5.0)
        velocity_std = max(0.3, clamped_hdop * 0.8)

        # Carry the most recent IMU acceleration across this interval. Passing
        # zero here would silently discard the acceleration for the gap between
        # the last IMU sample and this fix.
        self.kalman.predict_to(reading.timestamp, *self._last_accel_enu)
        self.kalman.correct_gps(
            timestamp=reading.timestamp,
            east_m=east,
            north_m=north,
            v_east_mps=v_east,
            v_north_mps=v_north,
            position_std_m=position_std,
            velocity_std_mps=velocity_std,
        )

        self.metrics.ingest_gps(
            latitude=latitude,
            longitude=longitude,
            altitude_m=self._altitude_m,
            speed_mps=speed,
            valid=True,
        )

        if speed >= MIN_MOTION_MPS:
            self._heading_deg = course

        self._gps_valid = True
        self._last_emit = reading.timestamp
        return self._build_sample(reading.timestamp)

    # --- output -----------------------------------------------------------

    def _build_sample(self, timestamp: float) -> RideSample:
        filter_state = self.kalman.state()
        speed = filter_state.speed_mps

        if self.origin is not None:
            latitude, longitude = self.origin.to_geodetic(
                filter_state.east_m, filter_state.north_m
            )
        else:
            latitude = longitude = 0.0

        heading = (
            compass_heading(filter_state.v_east_mps, filter_state.v_north_mps)
            if speed >= MIN_MOTION_MPS
            else self._heading_deg
        )

        state = MotionState(
            timestamp=timestamp,
            x_m=filter_state.east_m,
            y_m=filter_state.north_m,
            vx_mps=filter_state.v_east_mps,
            vy_mps=filter_state.v_north_mps,
            speed_mps=speed,
            heading_deg=heading,
            ax_mps2=filter_state.accel_east_mps2,
            ay_mps2=filter_state.accel_north_mps2,
            latitude=latitude,
            longitude=longitude,
            altitude_m=self._altitude_m,
            position_confidence=filter_state.position_confidence,
            velocity_confidence=filter_state.velocity_confidence,
            acceleration_confidence=self.calibrator.progress,
            mode=filter_state.mode if self.origin is not None else FusionMode.INIT,
        )

        return RideSample(
            timestamp=timestamp,
            ride_id=self.ride_id,
            state=state,
            metrics=self.metrics.snapshot(speed),
            system_state=self.system_state,
            gps_valid=self._gps_valid,
            satellites=self._satellites,
        )
