"""APEX entrypoint: wire the configured pieces together and run.

    python -m app.main                      # simulated ride, live console output
    python -m app.main --duration 30        # stop after 30 seconds
    python -m app.main --backend hardware   # talk to the real sensors on a Pi
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from app.config import AppConfig, load_config
from app.models import RideSample
from app.pipeline.coordinator import Coordinator

logger = logging.getLogger("apex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apex",
        description="Edge telemetry node for motorcycle helmets",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to the YAML config (default: config/apex.yaml)",
    )
    parser.add_argument(
        "--backend",
        choices=("sim", "hardware"),
        default=None,
        help="override node.sensor_backend",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="stop after this many seconds (default: run until interrupted)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="do not print the live telemetry line",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="override node.log_level, e.g. DEBUG",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


class ConsoleReporter:
    """Prints a single rewriting status line, throttled to stay readable."""

    def __init__(self, period_s: float = 0.5) -> None:
        self.period_s = period_s
        self._last = 0.0
        self._printed = False

    def __call__(self, sample: RideSample) -> None:
        if sample.timestamp - self._last < self.period_s:
            return
        self._last = sample.timestamp

        metrics = sample.metrics
        state = sample.state
        line = (
            f"{sample.system_state.value:<15} "
            f"{metrics.speed_kmh:6.1f} km/h  "
            f"{metrics.g_force:4.2f} g  "
            f"lean {metrics.lean_angle_deg:+6.1f} deg  "
            f"grade {metrics.gradient_pct:+5.1f}%  "
            f"{metrics.distance_m / 1000.0:6.3f} km  "
            f"{state.latitude:9.5f},{state.longitude:9.5f}  "
            f"sats {sample.satellites:2d}  "
            f"{state.mode.value}"
        )
        sys.stdout.write("\r" + line.ljust(132))
        sys.stdout.flush()
        self._printed = True

    def finish(self) -> None:
        if self._printed:
            sys.stdout.write("\n")
            sys.stdout.flush()


def resolve_config(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config)
    if args.backend:
        config.node.sensor_backend = args.backend
    if args.log_level:
        config.node.log_level = args.log_level
    return config


async def async_main(args: argparse.Namespace) -> int:
    config = resolve_config(args)
    configure_logging(config.node.log_level)

    coordinator = Coordinator(config)
    reporter = ConsoleReporter()
    if not args.quiet:
        coordinator.subscribe_sample(reporter)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, coordinator.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: coordinator.stop())

    try:
        await coordinator.run(duration_s=args.duration)
    finally:
        reporter.finish()

    stats = coordinator.stats()
    logger.info("Summary: %s", stats)

    if stats["samples_emitted"] == 0:
        logger.error("No telemetry was produced; check sensor configuration")
        return 1
    return 0


def run() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
