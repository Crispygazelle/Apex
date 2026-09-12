"""Polyline interpolation and the Nagpur demo GPX."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.geo import GeoOrigin, haversine_m
from app.sensors.demo import GPX_PATH, nagpur_airport_profile
from app.sensors.gpx import load_gpx
from app.sensors.path import RidePath
from app.sensors.simulated import RideProfile, RideSimulator


def test_gpx_loader_reads_the_shipped_nagpur_route() -> None:
    points = load_gpx(GPX_PATH)
    assert len(points) > 100
    path = RidePath(points)
    assert 10_000 < path.length_m < 14_000
    start = path.point(0.0)
    end = path.point(path.length_m)
    # Sitabuldi / Zero Mile to Sonegaon Airport, not a scribble around one junction.
    assert haversine_m(start[0], start[1], end[0], end[1]) > 4_000


def test_missing_gpx_is_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty.gpx"
    empty.write_text("<gpx></gpx>", encoding="utf-8")
    with pytest.raises(ValueError, match="usable"):
        load_gpx(empty)


def test_interpolation_stays_on_the_segment() -> None:
    origin = GeoOrigin(21.14631, 79.08491)
    east = origin.to_geodetic(1000.0, 0.0)
    path = RidePath([(origin.latitude, origin.longitude), east])
    mid = path.point(500.0)
    assert haversine_m(origin.latitude, origin.longitude, mid[0], mid[1]) == pytest.approx(
        500.0, rel=0.01
    )
    assert path.heading_deg(10.0) == pytest.approx(90.0, abs=1.0)


def test_path_follower_drives_east_along_waypoints() -> None:
    origin = GeoOrigin(21.14631, 79.08491)
    east = origin.to_geodetic(2000.0, 0.0)
    profile = RideProfile(
        start_latitude=origin.latitude,
        start_longitude=origin.longitude,
        waypoints=[(origin.latitude, origin.longitude), east],
        speed_zones=[(0.0, 10_000.0, 10.0)],
        stationary_s=0.0,
        spool_up_s=0.05,
        gps_lock_s=0.0,
        gps_dropouts=[],
        max_accel_mps2=20.0,
        max_brake_mps2=20.0,
    )
    sim = RideSimulator(profile)
    state = sim.state_at(20.0)
    assert state.speed_mps == pytest.approx(10.0, abs=0.3)
    assert state.distance_m == pytest.approx(200.0, abs=5.0)
    assert haversine_m(origin.latitude, origin.longitude, state.latitude, state.longitude) == (
        pytest.approx(200.0, abs=8.0)
    )
    assert state.heading_deg == pytest.approx(90.0, abs=5.0)
    assert sim.destination == east
    assert sim.route_length_m == pytest.approx(2000.0, rel=0.01)


def test_hard_brake_zone_produces_forward_deceleration() -> None:
    origin = GeoOrigin(21.14631, 79.08491)
    east = origin.to_geodetic(800.0, 0.0)
    profile = RideProfile(
        start_latitude=origin.latitude,
        start_longitude=origin.longitude,
        waypoints=[(origin.latitude, origin.longitude), east],
        speed_zones=[(0.0, 120.0, 20.0), (120.0, 800.0, 4.0)],
        stationary_s=0.0,
        spool_up_s=0.05,
        gps_dropouts=[],
        max_accel_mps2=8.0,
        max_brake_mps2=8.0,
    )
    sim = RideSimulator(profile)
    hardest = 0.0
    t = 0.0
    while t < 20.0:
        state = sim.state_at(t)
        hardest = min(hardest, state.accel_forward_mps2)
        t += 0.05
    assert hardest < -3.0


def test_demo_profile_has_a_destination_and_a_voice_script() -> None:
    profile = nagpur_airport_profile()
    assert profile.destination_name == "Nagpur Airport"
    assert any("pothole" in line for _, line in profile.voice_script)
    assert any("destination" in line for _, line in profile.voice_script)
    assert profile.potholes_m
    assert profile.speed_zones


def test_path_corners_do_not_unwind_lean() -> None:
    """OSM vertices are sharp; the IMU must not report a 90° snap."""
    from app.config import AppConfig
    from tests.harness import run_offline_ride

    result = run_offline_ride(
        AppConfig(), duration_s=45.0, profile=nagpur_airport_profile()
    )
    leans = [abs(sample.metrics.lean_angle_deg) for sample in result.samples]
    assert leans
    assert max(leans) < 70.0, f"lean wound up to {max(leans):.1f}°"
