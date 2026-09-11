"""Local tangent plane conversions and bearings."""

from __future__ import annotations

import pytest

from app.geo import (
    GeoOrigin,
    angle_difference_deg,
    bearing_deg,
    compass_heading,
    haversine_m,
)

BENGALURU = (12.9716, 77.5946)


def test_enu_round_trip_is_lossless_at_ride_scale() -> None:
    origin = GeoOrigin(*BENGALURU)

    for east, north in [(0.0, 0.0), (250.0, -400.0), (-12_000.0, 8_000.0)]:
        latitude, longitude = origin.to_geodetic(east, north)
        back_east, back_north = origin.to_enu(latitude, longitude)
        assert back_east == pytest.approx(east, abs=1e-6)
        assert back_north == pytest.approx(north, abs=1e-6)


def test_enu_agrees_with_haversine_over_a_kilometre() -> None:
    origin = GeoOrigin(*BENGALURU)
    latitude, longitude = origin.to_geodetic(600.0, 800.0)  # 1000 m away

    measured = haversine_m(origin.latitude, origin.longitude, latitude, longitude)
    assert measured == pytest.approx(1000.0, rel=0.002)


def test_north_is_zero_and_east_is_ninety() -> None:
    origin = GeoOrigin(*BENGALURU)

    north_point = origin.to_geodetic(0.0, 1000.0)
    east_point = origin.to_geodetic(1000.0, 0.0)

    assert bearing_deg(*BENGALURU, *north_point) == pytest.approx(0.0, abs=0.1)
    assert bearing_deg(*BENGALURU, *east_point) == pytest.approx(90.0, abs=0.1)


@pytest.mark.parametrize(
    ("east", "north", "expected"),
    [
        (0.0, 10.0, 0.0),  # north
        (10.0, 0.0, 90.0),  # east
        (0.0, -10.0, 180.0),  # south
        (-10.0, 0.0, 270.0),  # west
        (10.0, 10.0, 45.0),  # north-east
    ],
)
def test_compass_heading_uses_navigation_convention(
    east: float, north: float, expected: float
) -> None:
    assert compass_heading(east, north) == pytest.approx(expected, abs=1e-6)


def test_compass_heading_of_a_stationary_vehicle_is_defined() -> None:
    assert compass_heading(0.0, 0.0) == 0.0


def test_haversine_is_zero_for_the_same_point() -> None:
    assert haversine_m(*BENGALURU, *BENGALURU) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [(10.0, 350.0, 20.0), (350.0, 10.0, -20.0), (0.0, 180.0, -180.0), (90.0, 90.0, 0.0)],
)
def test_angle_difference_wraps_the_short_way(a: float, b: float, expected: float) -> None:
    assert angle_difference_deg(a, b) == pytest.approx(expected)
