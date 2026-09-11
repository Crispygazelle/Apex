"""Convert Kalman MotionState objects into InfluxDB telemetry."""

from __future__ import annotations

from dataclasses import asdict

from influxdb_client import Point

from kalman import MotionState
from influx_db import InfluxDBWriter


class TelemetryWriter:
    """Converts filtered MotionState into InfluxDB telemetry."""

    def __init__(
        self,
        influx_writer: InfluxDBWriter,
        helmet_id: str = "helmet01",
    ) -> None:

        self.influx_writer = influx_writer
        self.helmet_id = helmet_id

    def write(self, state: MotionState) -> None:
        """Write one filtered MotionState to InfluxDB."""

        point = (
            Point("helmet_telemetry")
            .tag("helmet_id", self.helmet_id)
            .tag("mode", state.mode)
            .field("x_m", state.x_m)
            .field("y_m", state.y_m)
            .field("vx_mps", state.vx_mps)
            .field("vy_mps", state.vy_mps)
            .field("speed_mps", state.speed_mps)
            .field("heading_deg", state.heading_deg)
            .field("ax_mps2", state.ax_mps2)
            .field("ay_mps2", state.ay_mps2)
            .field(
                "position_confidence",
                state.position_confidence,
            )
            .field(
                "velocity_confidence",
                state.velocity_confidence,
            )
            .field(
                "acceleration_confidence",
                state.acceleration_confidence,
            )
            .time(state.timestamp)
        )

        self.influx_writer.write_point(point)

    def write_dict(self, state: MotionState) -> dict:
        """Return MotionState as a normal dictionary."""

        return asdict(state)
