"""The performance script has to stay runnable; the README quotes it."""

from __future__ import annotations

from scripts.measure_performance import measure, render


def test_offline_measurement_finishes_faster_than_the_ride() -> None:
    summary = measure(20.0)
    assert summary["samples"] > 100
    assert summary["wall_seconds"] < 5.0
    assert summary["fused_position_mean_m"] is not None
    assert summary["fused_position_mean_m"] < 5.0
    assert summary["battery"] == "unmeasured"


def test_report_contains_the_numbers_the_readme_needs() -> None:
    text = render(measure(20.0))
    assert "fused position" in text
    assert "battery" in text
    assert "unmeasured" in text
