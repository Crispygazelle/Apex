"""GPS-tagged hazards logged by the rider, kept across rides.

The live map reads this file on load. A new log is appended when the voice
layer accepts one, including when InfluxDB is off.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.models import HazardReport


class HazardLog:
    """Append-only JSONL of hazard rows."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def add(self, report: HazardReport) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "timestamp": report.timestamp,
            "ride_id": report.ride_id,
            "hazard_type": report.hazard_type or "unspecified",
            "latitude": report.latitude,
            "longitude": report.longitude,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")))
            handle.write("\n")
        return row

    def all(self, ride_id: str | None = None) -> list[dict]:
        if not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ride_id is not None and row.get("ride_id") != ride_id:
                continue
            rows.append(row)
        return rows
