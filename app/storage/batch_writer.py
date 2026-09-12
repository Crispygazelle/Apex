"""Batched persistence with a disk spool for when InfluxDB is unreachable.

A helmet spends much of its time away from the database: no WiFi in a tunnel,
no WiFi on a highway, and the Pi may boot before the network does. Writing each
sample straight through would be slow and would lose the ride whenever the
server blinked.

So samples accumulate in memory and are flushed by size or by age. If the write
fails, the batch is appended to a newline-delimited JSON spool on disk and
replayed once the server returns. The spool is bounded: past `max_spool_mb` the
*oldest* file is discarded, because filling the card would take the whole node
down, and recent telemetry is worth more than old telemetry.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.config import StorageConfig
from app.models import CrashEvent, HazardReport, RideSample
from app.storage.influxdb import InfluxWriter

logger = logging.getLogger(__name__)

SPOOL_SUFFIX = ".jsonl"
# Kept out of the replay glob on purpose: crash records are not sample rows and
# must not be fed back through the telemetry replay path.
CRASH_LOG_NAME = "crashes.log"
HAZARD_LOG_NAME = "hazards.log"
# Reconnect attempts are spaced so a long outage does not spin the CPU.
RECONNECT_INTERVAL_S = 30.0


@dataclass
class WriterStats:
    submitted: int = 0
    written: int = 0
    spooled: int = 0
    replayed: int = 0
    dropped: int = 0
    flushes: int = 0
    failed_flushes: int = 0
    spool_files: int = 0
    spool_bytes: int = 0
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


class BatchWriter:
    """Accumulates samples and persists them, spooling to disk on failure."""

    def __init__(self, config: StorageConfig, writer: InfluxWriter) -> None:
        self.config = config
        self.writer = writer
        self.stats = WriterStats()
        self.spool_dir = Path(config.spool_dir)

        self._pending: list[RideSample] = []
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._wakeup = asyncio.Event()
        self._last_reconnect_attempt = 0.0

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Connect, replay anything left over from a previous run, then poll."""
        self.spool_dir.mkdir(parents=True, exist_ok=True)

        if await asyncio.to_thread(self.writer.connect):
            await self._replay_spool()
        else:
            self.stats.last_error = self.writer.last_error

        self._stopping.clear()
        self._task = asyncio.create_task(self._flush_loop(), name="apex-batch-writer")

    async def stop(self) -> None:
        """Flush what is held, then shut down. A clean exit loses nothing."""
        self._stopping.set()
        self._wakeup.set()

        if self._task is not None:
            await self._task
            self._task = None

        await self.flush()
        self.writer.close()

    # --- ingestion --------------------------------------------------------

    def submit(self, sample: RideSample) -> None:
        """Queue a sample. Cheap and synchronous, so it is safe as a subscriber."""
        self.stats.submitted += 1
        self._pending.append(sample)

        if len(self._pending) >= self.config.batch_size:
            # Do not make a full batch wait out the flush interval.
            self._wakeup.set()

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def record_crash(self, event: CrashEvent) -> bool:
        """Persist a crash event, to disk always and to InfluxDB if reachable.

        Unlike telemetry, a crash record is written to the card unconditionally
        rather than only when the database write fails. It is a handful of
        bytes once in the life of a helmet, and it is the one record that must
        survive a node that never reconnects and a card pulled from a wreck.
        """
        await asyncio.to_thread(self._append_crash_log, event)

        # Flush pending telemetry first so the seconds leading up to the impact
        # are in the database alongside the event itself.
        await self.flush()

        written = await asyncio.to_thread(self.writer.write_crash, event)
        if not written:
            self.stats.last_error = self.writer.last_error
            logger.error("Crash event not written to InfluxDB; disk copy retained")
        return written

    def _append_crash_log(self, event: CrashEvent) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        path = self.spool_dir / CRASH_LOG_NAME
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(event), separators=(",", ":")))
                handle.write("\n")
        except OSError as exc:
            logger.error("Could not write crash log: %s", exc)

    async def record_hazard(self, hazard: HazardReport) -> bool:
        """Persist a rider-logged hazard to disk, and to InfluxDB if reachable."""
        await asyncio.to_thread(self._append_hazard_log, hazard)
        written = await asyncio.to_thread(self.writer.write_hazard, hazard)
        if not written:
            self.stats.last_error = self.writer.last_error
            logger.info("Hazard kept on disk; InfluxDB was not reachable")
        return written

    def _append_hazard_log(self, hazard: HazardReport) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        path = self.spool_dir / HAZARD_LOG_NAME
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(hazard), separators=(",", ":")))
                handle.write("\n")
        except OSError as exc:
            logger.error("Could not write hazard log: %s", exc)

    async def _flush_loop(self) -> None:
        interval = max(0.5, self.config.flush_interval_s)

        while not self._stopping.is_set():
            # A timeout just means the interval elapsed with no full batch,
            # which is the ordinary case.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=interval)
            self._wakeup.clear()

            if self._pending:
                await self.flush()

            if not self.writer.healthy:
                await self._try_reconnect()

    # --- flushing ---------------------------------------------------------

    async def flush(self) -> int:
        """Persist everything pending. Returns the number of samples handled."""
        if not self._pending:
            return 0

        batch = self._pending
        self._pending = []
        self.stats.flushes += 1

        # The InfluxDB client is synchronous, so it goes to a thread. A blocking
        # HTTP call on the event loop would stall the dashboard and the fusion
        # loop for as long as the server took to answer.
        written = await asyncio.to_thread(self.writer.write_samples, batch)

        if written:
            self.stats.written += len(batch)
            return len(batch)

        self.stats.failed_flushes += 1
        self.stats.last_error = self.writer.last_error
        await asyncio.to_thread(self._spool, batch)
        return len(batch)

    # --- spooling ---------------------------------------------------------

    def _spool(self, batch: list[RideSample]) -> None:
        """Append a failed batch to disk as newline-delimited JSON."""
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        path = self.spool_dir / f"{batch[0].ride_id}-{int(batch[0].timestamp)}{SPOOL_SUFFIX}"

        try:
            with path.open("a", encoding="utf-8") as handle:
                for sample in batch:
                    handle.write(json.dumps(sample.to_dict(), separators=(",", ":")))
                    handle.write("\n")
        except OSError as exc:
            self.stats.dropped += len(batch)
            self.stats.last_error = f"spool write failed: {exc}"
            logger.error("Could not spool %d samples: %s", len(batch), exc)
            return

        self.stats.spooled += len(batch)
        logger.info("Spooled %d samples to %s", len(batch), path.name)
        self._enforce_spool_limit()

    def spool_files(self) -> list[Path]:
        """Spool files, oldest first."""
        if not self.spool_dir.exists():
            return []
        return sorted(self.spool_dir.glob(f"*{SPOOL_SUFFIX}"), key=lambda p: p.stat().st_mtime)

    def _enforce_spool_limit(self) -> None:
        files = self.spool_files()
        total = sum(path.stat().st_size for path in files)
        limit = self.config.max_spool_mb * 1024 * 1024

        # Always keep at least the newest file. The ceiling is best-effort: if
        # one batch on its own exceeds the limit, evicting down to nothing
        # would throw away the most recent telemetry to honour a soft cap,
        # which defeats the purpose of spooling in the first place.
        while total > limit and len(files) > 1:
            victim = files.pop(0)  # oldest first
            try:
                size = victim.stat().st_size
                victim.unlink()
            except OSError:
                break
            total -= size
            self.stats.dropped += 1
            logger.warning(
                "Spool exceeded %.0f MB; dropped oldest file %s",
                self.config.max_spool_mb,
                victim.name,
            )

        remaining = self.spool_files()
        self.stats.spool_files = len(remaining)
        self.stats.spool_bytes = sum(path.stat().st_size for path in remaining)

    async def _replay_spool(self) -> int:
        """Push spooled batches back to InfluxDB, oldest first."""
        files = self.spool_files()
        if not files:
            return 0

        logger.info("Replaying %d spooled file(s) to InfluxDB", len(files))
        replayed = 0

        for path in files:
            rows = await asyncio.to_thread(_read_spool_file, path)
            if not rows:
                path.unlink(missing_ok=True)
                continue

            if not await asyncio.to_thread(self.writer.write_rows, rows):
                # Still unreachable. Leave the rest on disk for the next attempt
                # rather than dropping it.
                logger.warning("Replay stopped at %s; InfluxDB still unavailable", path.name)
                break

            replayed += len(rows)
            path.unlink(missing_ok=True)

        self.stats.replayed += replayed
        self._enforce_spool_limit()
        if replayed:
            logger.info("Replayed %d spooled samples", replayed)
        return replayed

    async def _try_reconnect(self) -> None:
        now = time.monotonic()
        if now - self._last_reconnect_attempt < RECONNECT_INTERVAL_S:
            return
        self._last_reconnect_attempt = now

        if await asyncio.to_thread(self.writer.connect):
            await self._replay_spool()


def _read_spool_file(path: Path) -> list[dict[str, Any]]:
    """Read a spool file, tolerating a truncated final line.

    A power cut mid-write leaves a partial line. Discarding that one line is
    correct; discarding the whole file because of it is not.
    """
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rows.append(json.loads(stripped))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed line in %s", path.name)
    except OSError as exc:
        logger.error("Cannot read spool file %s: %s", path.name, exc)
        return []
    return rows
