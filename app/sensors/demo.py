"""The audience-facing simulated ride: Sitabuldi to Nagpur Airport.

A real ~12 km OSM drive via Ambazari Lake, with speed zones, potholes, and a
scripted helmet conversation. Tests keep using the analytic `RideProfile`
defaults; only `python -m app.main` in sim mode loads this.
"""

from __future__ import annotations

from pathlib import Path

from app.sensors.gpx import load_gpx
from app.sensors.path import RidePath
from app.sensors.simulated import RideProfile

GPX_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "rides" / "nagpur-sitabuldi-airport.gpx"
)

# Target speeds along the GPX, in metres and m/s.
# Fast stretches sit on the straighter Ambazari / Airport Road sections;
# the crawl bands sit on the lake loop and the airport-approach corners.
_SPEED_ZONES: list[tuple[float, float, float]] = [
    (0.0, 420.0, 13.9),  # ~50 km/h out of Sitabuldi
    (420.0, 980.0, 24.4),  # first fast burst, purple trail
    (980.0, 1320.0, 8.3),  # tight junction
    (1320.0, 4650.0, 25.0),  # ~90 km/h out toward Ambazari
    (4650.0, 4920.0, 5.0),  # hard brake before the lake turn
    (4920.0, 6750.0, 7.8),  # lake loop / tight turns
    (6750.0, 8450.0, 24.4),  # second fast stretch
    (8450.0, 8780.0, 4.5),  # hard brake, almost a stop
    (8780.0, 10400.0, 15.3),  # cruise
    (10400.0, 10720.0, 5.5),  # airport-approach corner
    (10720.0, 11900.0, 11.1),
    (11900.0, 30_000.0, 3.5),  # roll into the destination
]


def nagpur_airport_profile() -> RideProfile:
    """Sitabuldi (A) → Ambazari Lake → Sonegaon Airport (B)."""
    waypoints = load_gpx(GPX_PATH)
    start_lat, start_lon = waypoints[0]
    return RideProfile(
        start_latitude=start_lat,
        start_longitude=start_lon,
        start_altitude_m=310.0,
        waypoints=waypoints,
        gps_dropouts=[],
        gps_lock_s=5.0,
        stationary_s=4.0,
        spool_up_s=6.0,
        hill_amplitude_m=12.0,
        hill_period_s=200.0,
        brake_at_s=1e9,
        speed_zones=list(_SPEED_ZONES),
        potholes_m=[4480.0, 9180.0],
        pothole_peak_g=2.45,
        max_accel_mps2=2.4,
        max_brake_mps2=7.8,
        route_name="Sitabuldi → Ambazari → Airport",
        destination_name="Nagpur Airport",
        voice_script=[
            (280.0, "hey apex what's my speed"),
            (700.0, "hey apex how far am I to the destination"),
            (2400.0, "hey apex status report"),
            (4460.0, "hey apex log a pothole here"),
            (5300.0, "hey apex how far to the destination"),
            (7200.0, "hey apex what's my speed"),
            (11650.0, "hey apex how far am I to the destination"),
        ],
    )


# First ~1.2 km of the same GPX: spool, purple, brake, pothole, voice, arrive.
# Sized so a talk can see every beat before the room's attention moves on.
SPRINT_LENGTH_M = 1180.0


def clip_waypoints(
    waypoints: list[tuple[float, float]], max_m: float
) -> list[tuple[float, float]]:
    """Keep the start of a polyline and stop on the road at `max_m`."""
    path = RidePath(waypoints)
    if path.length_m <= max_m:
        return list(path.waypoints)
    clipped: list[tuple[float, float]] = []
    for point, distance in zip(path.waypoints, path.cum_m, strict=True):
        if distance <= max_m:
            clipped.append(point)
            continue
        clipped.append(path.point(max_m))
        break
    return clipped


def nagpur_sprint_profile() -> RideProfile:
    """A ~90 s highlight of the same Sitabuldi start, for a first impression."""
    waypoints = clip_waypoints(load_gpx(GPX_PATH), SPRINT_LENGTH_M)
    start_lat, start_lon = waypoints[0]
    return RideProfile(
        start_latitude=start_lat,
        start_longitude=start_lon,
        start_altitude_m=310.0,
        waypoints=waypoints,
        gps_dropouts=[],
        gps_lock_s=4.0,
        stationary_s=3.0,
        spool_up_s=4.0,
        hill_amplitude_m=8.0,
        hill_period_s=90.0,
        brake_at_s=1e9,
        speed_zones=[
            (0.0, 80.0, 12.0),
            (80.0, 500.0, 25.0),  # purple trail
            (500.0, 540.0, 4.0),  # hard brake
            (540.0, 1000.0, 18.0),
            (1000.0, 30_000.0, 8.5),
        ],
        potholes_m=[620.0],
        pothole_peak_g=2.45,
        max_accel_mps2=2.8,
        max_brake_mps2=7.8,
        route_name="Sitabuldi → Ambazari approach",
        destination_name="Ambazari approach",
        voice_script=[
            (90.0, "hey apex what's my speed"),
            (400.0, "hey apex how far am I to the destination"),
            (640.0, "hey apex log a pothole here"),
            (1050.0, "hey apex how far am I to the destination"),
        ],
    )
