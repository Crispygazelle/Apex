"""Minimal GPX track loader.

The simulator follows a polyline so IMU and GPS stay consistent with one
ground truth. We only need lat/lon vertices; elevation, timing, and
extensions are ignored.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET


def load_gpx(path: str | Path) -> list[tuple[float, float]]:
    """Return [(lat, lon), ...] from the first track in a GPX file."""
    tree = ET.parse(path)
    points: list[tuple[float, float]] = []
    for element in tree.getroot().iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag != "trkpt":
            continue
        points.append((float(element.attrib["lat"]), float(element.attrib["lon"])))
    if len(points) < 2:
        raise ValueError(f"{path} does not contain a usable GPX track")
    return points
