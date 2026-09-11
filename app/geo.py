"""Geodesy helpers.

The Kalman filter works in metres, but GPS speaks degrees and the dashboard map
needs degrees back. A local east-north-up tangent plane anchored at the first
fix bridges the two. Over a single ride the flat-earth approximation costs well
under a metre, which is an order of magnitude below NEO-M8N noise.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, atan2, cos, degrees, radians, sin, sqrt

# WGS-84 semi-major axis.
EARTH_RADIUS_M = 6378137.0


@dataclass(frozen=True, slots=True)
class GeoOrigin:
    """Anchor point for the local ENU frame, set from the first valid fix."""

    latitude: float
    longitude: float
    altitude_m: float = 0.0

    @property
    def metres_per_deg_lat(self) -> float:
        return EARTH_RADIUS_M * radians(1.0)

    @property
    def metres_per_deg_lon(self) -> float:
        return EARTH_RADIUS_M * radians(1.0) * cos(radians(self.latitude))

    def to_enu(self, latitude: float, longitude: float) -> tuple[float, float]:
        """Geodetic degrees to local (east, north) metres."""
        east = (longitude - self.longitude) * self.metres_per_deg_lon
        north = (latitude - self.latitude) * self.metres_per_deg_lat
        return east, north

    def to_geodetic(self, east_m: float, north_m: float) -> tuple[float, float]:
        """Local (east, north) metres back to geodetic degrees."""
        latitude = self.latitude + north_m / self.metres_per_deg_lat
        lon_scale = self.metres_per_deg_lon
        longitude = self.longitude + east_m / lon_scale if abs(lon_scale) > 1e-6 else self.longitude
        return latitude, longitude


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two geodetic points."""
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = phi2 - phi1
    dlambda = radians(lon2 - lon1)

    a = sin(dphi / 2.0) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * asin(min(1.0, sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing in degrees, 0 = north, increasing clockwise."""
    phi1, phi2 = radians(lat1), radians(lat2)
    dlambda = radians(lon2 - lon1)

    y = sin(dlambda) * cos(phi2)
    x = cos(phi1) * sin(phi2) - sin(phi1) * cos(phi2) * cos(dlambda)
    return degrees(atan2(y, x)) % 360.0


def compass_heading(east_mps: float, north_mps: float) -> float:
    """Compass heading of an ENU velocity vector, 0 = north, clockwise."""
    if abs(east_mps) < 1e-6 and abs(north_mps) < 1e-6:
        return 0.0
    return degrees(atan2(east_mps, north_mps)) % 360.0


def angle_difference_deg(a: float, b: float) -> float:
    """Signed smallest difference a - b, wrapped to [-180, 180)."""
    return (a - b + 180.0) % 360.0 - 180.0
