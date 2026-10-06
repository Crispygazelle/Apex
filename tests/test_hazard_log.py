"""Hazards logged by voice stay on disk for the map."""

from pathlib import Path

from app.models import HazardReport
from app.storage.hazard_log import HazardLog


def test_hazards_accumulate_and_can_be_filtered_by_ride(tmp_path: Path) -> None:
    log = HazardLog(tmp_path / "hazards.jsonl")
    log.add(HazardReport(1.0, "ride-a", "pothole", 21.14, 79.08))
    log.add(HazardReport(2.0, "ride-b", "construction", 21.15, 79.09))

    assert len(log.all()) == 2
    only_a = log.all("ride-a")
    assert only_a[0]["hazard_type"] == "pothole"
    assert only_a[0]["latitude"] == 21.14
