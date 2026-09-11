"""InfluxDB connection and point conversion.

Deliberately thin: it opens a client, converts samples to points, writes, and
reports whether it is healthy. Everything about *when* to write, and what to do
when the write fails, lives in `batch_writer`.

`influxdb_client` is imported inside `connect()` so the module stays importable
with storage disabled, and so a missing dependency surfaces as a clear message
rather than an ImportError at startup.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import StorageConfig
from app.models import CrashEvent, HazardReport, RideSample

logger = logging.getLogger(__name__)

NANOS_PER_SECOND = 1_000_000_000


class InfluxUnavailableError(RuntimeError):
    """Raised when InfluxDB cannot be reached or is misconfigured."""


class InfluxWriter:
    """Synchronous InfluxDB v2 writer with a health check."""

    def __init__(self, config: StorageConfig, helmet_id: str = "helmet01") -> None:
        self.config = config
        self.helmet_id = helmet_id
        self._client: Any = None
        self._write_api: Any = None
        self._query_api: Any = None
        self._point_cls: Any = None
        self._healthy = False
        self.last_error = ""

    @property
    def healthy(self) -> bool:
        return self._healthy

    def connect(self) -> bool:
        """Open a client and verify the server answers. Never raises."""
        try:
            from influxdb_client import InfluxDBClient, Point
            from influxdb_client.client.write_api import SYNCHRONOUS
        except ImportError as exc:
            self.last_error = (
                "influxdb-client is not installed; run pip install -e '.' to get it"
            )
            logger.warning("%s (%s)", self.last_error, exc)
            self._healthy = False
            return False

        try:
            self._client = InfluxDBClient(
                url=self.config.url,
                token=self.config.token,
                org=self.config.org,
                timeout=5_000,
            )
            # ping() returns False rather than raising when the server is down.
            if not self._client.ping():
                raise InfluxUnavailableError(f"no response from {self.config.url}")

            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
            self._query_api = self._client.query_api()
            self._point_cls = Point
            self._healthy = True
            self.last_error = ""
            logger.info("InfluxDB connected at %s (bucket %s)", self.config.url, self.config.bucket)
        except Exception as exc:  # noqa: BLE001 - storage must never break the ride
            self.last_error = str(exc)
            self._healthy = False
            logger.warning("InfluxDB unavailable at %s: %s", self.config.url, exc)

        return self._healthy

    # --- writing ----------------------------------------------------------

    def write_samples(self, samples: list[RideSample]) -> bool:
        """Write a batch. Returns False so the caller can spool instead."""
        if not self._healthy or self._point_cls is None:
            return False

        points = [self._sample_to_point(sample) for sample in samples]
        return self._write(points)

    def write_rows(self, rows: list[dict[str, Any]]) -> bool:
        """Write flat dicts, as produced by `RideSample.to_dict`.

        This is the replay path for the disk spool: the spool stores the JSON
        projection rather than pickled dataclasses, so a spool written by an
        older build can still be replayed after an upgrade.
        """
        if not self._healthy or self._point_cls is None:
            return False

        points = []
        for row in rows:
            point = self._point_cls(self.config.measurement).tag("helmet_id", self.helmet_id)

            for key, value in row.items():
                if key in ("timestamp", "ride_id", "fusion_mode", "system_state"):
                    continue
                if isinstance(value, bool):
                    point = point.field(key, int(value))
                elif isinstance(value, (int, float)):
                    point = point.field(key, float(value))

            point = (
                point.tag("ride_id", str(row.get("ride_id", "unknown")))
                .tag("fusion_mode", str(row.get("fusion_mode", "unknown")))
                .tag("system_state", str(row.get("system_state", "unknown")))
                .time(_to_nanos(float(row.get("timestamp", 0.0))))
            )
            points.append(point)

        return self._write(points) if points else True

    def write_hazard(self, hazard: HazardReport) -> bool:
        if not self._healthy or self._point_cls is None:
            return False

        point = (
            self._point_cls("hazard")
            .tag("helmet_id", self.helmet_id)
            .tag("ride_id", hazard.ride_id)
            .tag("hazard_type", hazard.hazard_type)
            .field("latitude", hazard.latitude)
            .field("longitude", hazard.longitude)
            .field("note", hazard.note)
            .time(_to_nanos(hazard.timestamp))
        )
        return self._write([point])

    def write_crash(self, crash: CrashEvent) -> bool:
        if not self._healthy or self._point_cls is None:
            return False

        point = (
            self._point_cls("crash")
            .tag("helmet_id", self.helmet_id)
            .tag("ride_id", crash.ride_id)
            .tag("confirmed", str(crash.confirmed).lower())
            .field("peak_g", crash.peak_g)
            .field("speed_before_kmh", crash.speed_before_kmh)
            .field("speed_after_kmh", crash.speed_after_kmh)
            .field("latitude", crash.latitude)
            .field("longitude", crash.longitude)
            .field("reason", crash.reason)
            .time(_to_nanos(crash.timestamp))
        )
        return self._write([point])

    def _write(self, points: list[Any]) -> bool:
        try:
            self._write_api.write(bucket=self.config.bucket, org=self.config.org, record=points)
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self._healthy = False
            logger.warning("InfluxDB write failed, will spool: %s", exc)
            return False

    def _sample_to_point(self, sample: RideSample) -> Any:
        state = sample.state
        metrics = sample.metrics

        point = (
            self._point_cls(self.config.measurement)
            .tag("helmet_id", self.helmet_id)
            .tag("ride_id", sample.ride_id)
            .tag("fusion_mode", state.mode.value)
            .tag("system_state", sample.system_state.value)
            .field("latitude", state.latitude)
            .field("longitude", state.longitude)
            .field("altitude_m", state.altitude_m)
            .field("east_m", state.x_m)
            .field("north_m", state.y_m)
            .field("speed_mps", state.speed_mps)
            .field("speed_kmh", metrics.speed_kmh)
            .field("heading_deg", state.heading_deg)
            .field("g_force", metrics.g_force)
            .field("lean_angle_deg", metrics.lean_angle_deg)
            .field("pitch_deg", metrics.pitch_deg)
            .field("longitudinal_accel_mps2", metrics.longitudinal_accel_mps2)
            .field("lateral_accel_mps2", metrics.lateral_accel_mps2)
            .field("gradient_pct", metrics.gradient_pct)
            .field("distance_m", metrics.distance_m)
            .field("position_confidence", state.position_confidence)
            .field("velocity_confidence", state.velocity_confidence)
            .field("satellites", sample.satellites)
            .time(_to_nanos(sample.timestamp))
        )
        return point

    # --- reading ----------------------------------------------------------

    def query_ride(self, ride_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        """Fetch a stored ride, newest-last, for post-ride analysis."""
        if not self._healthy or self._query_api is None:
            raise InfluxUnavailableError(self.last_error or "InfluxDB is not connected")

        flux = f"""
        from(bucket: "{self.config.bucket}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "{self.config.measurement}")
          |> filter(fn: (r) => r.ride_id == "{ride_id}")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> sort(columns: ["_time"])
          |> limit(n: {int(limit)})
        """

        try:
            tables = self._query_api.query(flux, org=self.config.org)
        except Exception as exc:  # noqa: BLE001
            raise InfluxUnavailableError(str(exc)) from exc

        rows: list[dict[str, Any]] = []
        for table in tables:
            for record in table.records:
                values = {k: v for k, v in record.values.items() if not k.startswith("_")}
                values["time"] = record.get_time().isoformat()
                rows.append(values)
        return rows

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                logger.debug("InfluxDB close raised, ignoring", exc_info=True)
            self._client = None
            self._write_api = None
            self._query_api = None
        self._healthy = False


def _to_nanos(epoch_seconds: float) -> int:
    """InfluxDB wants integer nanoseconds; our timestamps are float seconds."""
    return int(epoch_seconds * NANOS_PER_SECOND)
