"""APEX entrypoint: assemble the configured pieces and run them in one loop.

    python -m app.main                      # simulated ride, live console output
    python -m app.main --duration 30        # stop after 30 seconds
    python -m app.main --backend hardware   # talk to the real sensors on a Pi
    python -m app.main --dashboard          # serve the web dashboard
    python -m app.main --storage            # persist to InfluxDB

Storage, streaming, the dashboard, and the voice layer all attach to the
coordinator as subscribers, so each is optional and none of them can reach into
the fusion code.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import dataclass, field
from typing import Any

from app.config import AppConfig, load_config
from app.models import RideSample
from app.pipeline.coordinator import Coordinator
from app.storage.batch_writer import BatchWriter
from app.storage.influxdb import InfluxWriter
from app.streaming.websocket import TelemetryHub

logger = logging.getLogger("apex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apex",
        description="Edge telemetry node for motorcycle helmets",
    )
    parser.add_argument("--config", default=None, help="path to the YAML config")
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
    parser.add_argument("--quiet", action="store_true", help="hide the live telemetry line")
    parser.add_argument("--log-level", default=None, help="override node.log_level")

    for name, help_text in (
        ("dashboard", "serve the web dashboard"),
        ("storage", "persist telemetry to InfluxDB"),
    ):
        group = parser.add_mutually_exclusive_group()
        group.add_argument(f"--{name}", dest=name, action="store_true", help=help_text)
        group.add_argument(
            f"--no-{name}", dest=name, action="store_false", help=f"do not {help_text}"
        )

    # None means "leave whatever the config file says". Without this, the
    # store_false actions would default their dest to True and silently force
    # both services on.
    parser.set_defaults(dashboard=None, storage=None)
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Uvicorn's per-request logging would fight the console telemetry line.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def resolve_config(args: argparse.Namespace) -> AppConfig:
    config = load_config(args.config)
    if args.backend:
        config.node.sensor_backend = args.backend
    if args.log_level:
        config.node.log_level = args.log_level
    if args.dashboard is not None:
        config.dashboard.enabled = args.dashboard
    if args.storage is not None:
        config.storage.enabled = args.storage
    return config


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


@dataclass
class ApexNode:
    """Everything the process owns, assembled from config."""

    config: AppConfig
    coordinator: Coordinator
    hub: TelemetryHub | None = None
    batch_writer: BatchWriter | None = None
    reporter: ConsoleReporter | None = None
    _server_task: asyncio.Task[None] | None = field(default=None, init=False)
    _server: Any = field(default=None, init=False)

    async def start_services(self) -> None:
        """Bring up storage and the dashboard before the ride begins."""
        if self.batch_writer is not None:
            await self.batch_writer.start()

        if self.config.dashboard.enabled and self.hub is not None:
            await self._start_dashboard()

    async def _start_dashboard(self) -> None:
        import uvicorn

        from app.dashboard.app import create_app

        app = create_app(self.coordinator, self.hub, self.batch_writer)
        server_config = uvicorn.Config(
            app,
            host=self.config.dashboard.host,
            port=self.config.dashboard.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(server_config)
        # We install our own handlers; uvicorn's would swallow the first Ctrl-C
        # and leave the pipeline running.
        self._server.install_signal_handlers = lambda: None
        self._server_task = asyncio.create_task(self._server.serve(), name="apex-dashboard")

        logger.info(
            "Dashboard on http://%s:%d",
            self.config.dashboard.host,
            self.config.dashboard.port,
        )

    async def stop_services(self) -> None:
        if self._server is not None and self._server_task is not None:
            self._server.should_exit = True
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                async with asyncio.timeout(5.0):
                    await self._server_task
            self._server_task = None
            self._server = None

        if self.hub is not None:
            await self.hub.close()

        if self.batch_writer is not None:
            await self.batch_writer.stop()

    def summary(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"pipeline": self.coordinator.stats()}
        if self.hub is not None:
            payload["streaming"] = self.hub.stats()
        if self.batch_writer is not None:
            payload["storage"] = self.batch_writer.stats.as_dict()
        return payload


def build_node(config: AppConfig, *, quiet: bool = False) -> ApexNode:
    """Wire the coordinator to whichever consumers the config enables."""
    coordinator = Coordinator(config)
    node = ApexNode(config=config, coordinator=coordinator)

    if not quiet:
        node.reporter = ConsoleReporter()
        coordinator.subscribe_sample(node.reporter)

    # The dashboard needs the hub, so streaming comes up whenever either is on.
    if config.streaming.enabled or config.dashboard.enabled:
        node.hub = TelemetryHub(config.streaming)
        coordinator.subscribe_sample(node.hub.broadcast)

    if config.storage.enabled:
        writer = InfluxWriter(config.storage, helmet_id=config.node.helmet_id)
        node.batch_writer = BatchWriter(config.storage, writer)
        coordinator.subscribe_sample(node.batch_writer.submit)

    return node


async def async_main(args: argparse.Namespace) -> int:
    config = resolve_config(args)
    configure_logging(config.node.log_level)

    node = build_node(config, quiet=args.quiet)
    coordinator = node.coordinator

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, coordinator.stop)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(sig, lambda *_: coordinator.stop())

    await node.start_services()
    try:
        await coordinator.run(duration_s=args.duration)
    finally:
        await node.stop_services()
        if node.reporter is not None:
            node.reporter.finish()

    summary = node.summary()
    logger.info("Summary: %s", summary)

    if summary["pipeline"]["samples_emitted"] == 0:
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
