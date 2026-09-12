#!/usr/bin/env python3
"""Measure fusion accuracy and how fast an offline ride runs on this machine.

This is the number the README is allowed to quote. It is *not* battery life,
and it is not a substitute for a stopwatch on the Pi with the real sensors.

    python -m scripts.measure_performance
    python -m scripts.measure_performance --duration 90

Battery life is left blank on purpose. A 18650 on a desk is not a helmet on
a commute, and inventing "6–8 hours" is how the last README went wrong.
"""

from __future__ import annotations

import argparse
import resource
import statistics
import sys
import time
from typing import Any

from app.config import AppConfig
from app.models import FusionMode
from tests.harness import RideResult, run_offline_ride


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def summarise(result: RideResult, wall_s: float, duration_s: float) -> dict[str, Any]:
    fused = result.position_errors_m(after_s=15.0, mode=FusionMode.FUSED)
    raw_gap = result.position_errors_m(after_s=15.0, mode=FusionMode.DEAD_RECKONING)
    speeds = result.speed_errors_mps(after_s=15.0)
    leans = result.lean_errors_deg(after_s=15.0)
    usage = resource.getrusage(resource.RUSAGE_SELF)

    return {
        "ride_seconds": duration_s,
        "wall_seconds": round(wall_s, 3),
        "speedup": round(duration_s / wall_s, 1) if wall_s > 0 else 0.0,
        "samples": len(result.samples),
        "fused_position_mean_m": round(statistics.mean(fused), 2) if fused else None,
        "fused_position_p95_m": round(_percentile(fused, 0.95), 2) if fused else None,
        "fused_position_max_m": round(max(fused), 2) if fused else None,
        "dead_reckoning_mean_m": round(statistics.mean(raw_gap), 2) if raw_gap else None,
        "dead_reckoning_max_m": round(max(raw_gap), 2) if raw_gap else None,
        "speed_mean_mps": round(statistics.mean(speeds), 3) if speeds else None,
        "lean_mean_deg": round(statistics.mean(leans), 3) if leans else None,
        "peak_rss_mb": round(usage.ru_maxrss / (1024 * 1024), 1)
        if sys.platform == "darwin"
        else round(usage.ru_maxrss / 1024, 1),
        "battery": "unmeasured",
    }


def render(summary: dict[str, Any]) -> str:
    lines = [
        "APEX performance (offline synthetic ride, virtual clock)",
        f"  ride duration     {summary['ride_seconds']:.0f} s",
        (
            f"  wall time         {summary['wall_seconds']:.3f} s  "
            f"({summary['speedup']:.0f}x realtime)"
        ),
        f"  samples emitted   {summary['samples']}",
        (
            f"  fused position    mean {summary['fused_position_mean_m']} m   "
            f"p95 {summary['fused_position_p95_m']} m   "
            f"max {summary['fused_position_max_m']} m"
        ),
        (
            f"  dead reckoning    mean {summary['dead_reckoning_mean_m']} m   "
            f"max {summary['dead_reckoning_max_m']} m"
        ),
        f"  speed error       mean {summary['speed_mean_mps']} m/s",
        f"  lean error        mean {summary['lean_mean_deg']} deg",
        f"  peak RSS          {summary['peak_rss_mb']} MB",
        f"  battery           {summary['battery']}",
    ]
    return "\n".join(lines)


def measure(duration_s: float = 90.0) -> dict[str, Any]:
    config = AppConfig()
    config.node.sensor_backend = "sim"
    config.storage.enabled = False
    config.dashboard.enabled = False
    config.voice.enabled = False

    started = time.perf_counter()
    result = run_offline_ride(config, duration_s=duration_s)
    wall_s = time.perf_counter() - started
    return summarise(result, wall_s, duration_s)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=90.0, help="synthetic ride length")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = measure(args.duration)
    print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
