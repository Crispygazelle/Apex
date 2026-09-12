"""Arc-length interpolation along a geodetic polyline.

Used by the ride simulator so a scripted A-to-B journey stays on the road
instead of weaving through whatever the heading sine happens to point at.
"""

from __future__ import annotations

from bisect import bisect_right

from app.geo import bearing_deg, haversine_m


class RidePath:
    """A list of lat/lon vertices with precomputed cumulative distance."""

    def __init__(self, waypoints: list[tuple[float, float]]) -> None:
        if len(waypoints) < 2:
            raise ValueError("A ride path needs at least two waypoints")
        self.waypoints = [(float(lat), float(lon)) for lat, lon in waypoints]
        self.cum_m = [0.0]
        for prev, curr in zip(self.waypoints, self.waypoints[1:]):
            self.cum_m.append(self.cum_m[-1] + haversine_m(prev[0], prev[1], curr[0], curr[1]))
        self.length_m = self.cum_m[-1]

    def point(self, distance_m: float) -> tuple[float, float]:
        """Latitude, longitude at `distance_m` along the path."""
        if distance_m <= 0.0:
            return self.waypoints[0]
        if distance_m >= self.length_m:
            return self.waypoints[-1]

        index = bisect_right(self.cum_m, distance_m) - 1
        index = max(0, min(index, len(self.waypoints) - 2))
        start_s = self.cum_m[index]
        span = self.cum_m[index + 1] - start_s
        frac = 0.0 if span <= 1e-6 else (distance_m - start_s) / span
        lat0, lon0 = self.waypoints[index]
        lat1, lon1 = self.waypoints[index + 1]
        return lat0 + frac * (lat1 - lat0), lon0 + frac * (lon1 - lon0)

    def heading_deg(self, distance_m: float, lookahead_m: float = 28.0) -> float:
        """Compass heading, looking a short distance ahead so corners are smooth."""
        here = self.point(distance_m)
        ahead = self.point(min(self.length_m, distance_m + max(lookahead_m, 1.0)))
        if haversine_m(here[0], here[1], ahead[0], ahead[1]) < 0.5:
            if len(self.waypoints) < 2:
                return 0.0
            prev, last = self.waypoints[-2], self.waypoints[-1]
            return bearing_deg(prev[0], prev[1], last[0], last[1])
        return bearing_deg(here[0], here[1], ahead[0], ahead[1])
