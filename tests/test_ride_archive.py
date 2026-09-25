"""The on-disk ride library, independent of InfluxDB."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.dashboard.app import create_app
from app.models import MotionState, RideMetrics, RideSample, SystemState
from app.pipeline.coordinator import Coordinator
from app.storage.ride_archive import RideArchive
from app.streaming.websocket import TelemetryHub


def sample(timestamp: float, speed_kmh: float, distance_m: float) -> RideSample:
    return RideSample(
        timestamp=timestamp,
        ride_id="ride-a",
        state=MotionState(
            timestamp=timestamp,
            latitude=21.14,
            longitude=79.08,
            x_m=distance_m,
            y_m=5.0,
            speed_mps=speed_kmh / 3.6,
        ),
        metrics=RideMetrics(
            speed_kmh=speed_kmh,
            distance_m=distance_m,
            max_speed_kmh=speed_kmh,
            max_g_force=1.2,
            longitudinal_accel_mps2=-0.4,
        ),
        system_state=SystemState.RIDING,
        gps_valid=True,
    )


def test_a_ride_is_thinned_and_can_be_read_back(tmp_path: Path) -> None:
    archive = RideArchive(tmp_path, ride_id="ride-a", route_name="Sitabuldi", interval_s=0.25)
    archive.submit(sample(1_000.0, 10.0, 0.0))
    archive.submit(sample(1_000.1, 12.0, 1.0))  # inside the interval, dropped
    archive.submit(sample(1_000.3, 40.0, 12.0))
    archive.close()

    listed = RideArchive.list_rides(tmp_path)
    assert [item["ride_id"] for item in listed] == ["ride-a"]
    assert listed[0]["route"] == "Sitabuldi"
    assert listed[0]["distance_m"] == 12.0
    assert listed[0]["max_speed_kmh"] == 40.0
    assert listed[0]["samples"] == 2

    loaded = RideArchive.load_ride(tmp_path, "ride-a")
    assert loaded is not None
    assert len(loaded["samples"]) == 2
    assert loaded["samples"][-1]["speed_kmh"] == 40.0
    assert loaded["samples"][-1]["east_m"] == 12.0


def test_a_ride_id_cannot_escape_the_folder(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RideArchive.load_ride(tmp_path, "../secrets")


def test_the_dashboard_lists_a_saved_ride(tmp_path: Path, config: AppConfig) -> None:
    archive = RideArchive(tmp_path, ride_id="ride-a", route_name="Airport")
    archive.submit(sample(2_000.0, 30.0, 100.0))
    archive.close()
    client = TestClient(
        create_app(
            Coordinator(config, ride_id="live"),
            TelemetryHub(),
            ride_archive=archive,
        )
    )
    body = client.get("/api/rides").json()
    assert body["rides"][0]["ride_id"] == "ride-a"
    detail = client.get("/api/rides/ride-a").json()
    assert detail["samples"][0]["distance_m"] == 100.0
    assert client.get("/api/rides/missing").status_code == 404
    assert client.get("/api/rides/..%2Fsecrets").status_code == 404


def test_rides_endpoint_is_empty_without_an_archive(config: AppConfig) -> None:
    client = TestClient(create_app(Coordinator(config, ride_id="live"), TelemetryHub()))
    assert client.get("/api/rides").json() == {"rides": []}
