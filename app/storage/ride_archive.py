"""One file per ride, kept on this computer even when InfluxDB is off.

The Influx spool is a retry queue and may delete old files. This archive is
the library the dashboard lists. Samples are thinned to a few per second,
which is what the charts draw.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.models import RideSample

logger = logging.getLogger(__name__)

SUMMARY_SUFFIX = ".summary.json"
TRACK_SUFFIX = ".jsonl"
# About four samples a second. The live Dynamics page uses the same spacing.
SAMPLE_INTERVAL_S = 0.25


class RideArchive:
    """Appends the current ride and can list or reload any ride already saved."""

    def __init__(
        self,
        directory: str | Path,
        *,
        ride_id: str,
        route_name: str = "",
        destination_name: str = "",
        interval_s: float = SAMPLE_INTERVAL_S,
    ) -> None:
        self.directory = Path(directory)
        self.ride_id = _safe_id(ride_id)
        self.route_name = route_name
        self.destination_name = destination_name
        self.interval_s = interval_s
        self._last_stored = float("-inf")
        self._count = 0
        self._started_at = 0.0
        self._last: RideSample | None = None
        self._track = self.directory / f"{self.ride_id}{TRACK_SUFFIX}"
        self._summary_path = self.directory / f"{self.ride_id}{SUMMARY_SUFFIX}"

    def submit(self, sample: RideSample) -> None:
        """Keep this sample if enough time has passed since the last one."""
        if sample.timestamp - self._last_stored < self.interval_s:
            self._last = sample
            return

        self.directory.mkdir(parents=True, exist_ok=True)
        if self._count == 0:
            self._started_at = sample.timestamp
        line = json.dumps(sample.to_dict(), separators=(",", ":"))
        with self._track.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
        self._last_stored = sample.timestamp
        self._last = sample
        self._count += 1
        if self._count == 1 or self._count % 16 == 0:
            self._write_summary()

    def close(self) -> None:
        """Write the summary, including a ride that never moved."""
        if self._last is None and self._count == 0:
            return
        self._write_summary()
        logger.info(
            "Saved ride %s (%d samples, %.0f m)",
            self.ride_id,
            self._count,
            0.0 if self._last is None else self._last.metrics.distance_m,
        )

    def _write_summary(self) -> None:
        sample = self._last
        if sample is None:
            return
        ended = sample.timestamp
        summary = {
            "ride_id": self.ride_id,
            "route": self.route_name,
            "destination": self.destination_name,
            "started_at": self._started_at or ended,
            "ended_at": ended,
            "duration_s": round(max(0.0, ended - (self._started_at or ended)), 1),
            "distance_m": round(sample.metrics.distance_m, 1),
            "max_speed_kmh": round(sample.metrics.max_speed_kmh, 1),
            "max_g_force": round(sample.metrics.max_g_force, 2),
            "samples": self._count,
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        self._summary_path.write_text(json.dumps(summary), encoding="utf-8")

    @staticmethod
    def list_rides(directory: str | Path) -> list[dict]:
        """Newest ride first. A missing folder is an empty library."""
        root = Path(directory)
        if not root.is_dir():
            return []
        rides = []
        for path in root.glob(f"*{SUMMARY_SUFFIX}"):
            try:
                rides.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                logger.warning("Skipping unreadable ride summary %s", path.name)
        rides.sort(key=lambda item: float(item.get("ended_at") or 0.0), reverse=True)
        return rides

    @staticmethod
    def load_ride(directory: str | Path, ride_id: str) -> dict | None:
        """The summary plus every stored sample. None if this ride was never saved."""
        safe = _safe_id(ride_id)
        root = Path(directory)
        summary_path = root / f"{safe}{SUMMARY_SUFFIX}"
        track_path = root / f"{safe}{TRACK_SUFFIX}"
        if not summary_path.is_file() or not track_path.is_file():
            return None
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        samples = []
        for line in track_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                samples.append(json.loads(line))
        return {"ride": summary, "samples": samples}


def _safe_id(ride_id: str) -> str:
    """Ride ids become filenames, so a slash or '..' is refused."""
    if (
        not ride_id
        or ride_id in {".", ".."}
        or "/" in ride_id
        or "\\" in ride_id
        or ".." in ride_id
    ):
        raise ValueError(f"ride id {ride_id!r} cannot be used as a filename")
    return ride_id
