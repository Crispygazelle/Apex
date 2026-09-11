"""Batched writes, and the disk spool that makes an outage survivable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import StorageConfig
from app.models import MotionState, RideMetrics, RideSample
from app.storage.batch_writer import BatchWriter, _read_spool_file
from app.storage.influxdb import _to_nanos


class FakeInflux:
    """Stands in for InfluxWriter, with a switch for being unreachable."""

    def __init__(self, *, reachable: bool = True) -> None:
        self.reachable = reachable
        self.healthy = reachable
        self.last_error = "" if reachable else "connection refused"
        self.written: list[RideSample] = []
        self.written_rows: list[dict[str, Any]] = []
        self.connects = 0
        self.closed = False

    def connect(self) -> bool:
        self.connects += 1
        self.healthy = self.reachable
        self.last_error = "" if self.reachable else "connection refused"
        return self.healthy

    def write_samples(self, samples: list[RideSample]) -> bool:
        if not self.reachable:
            self.healthy = False
            return False
        self.written.extend(samples)
        return True

    def write_rows(self, rows: list[dict[str, Any]]) -> bool:
        if not self.reachable:
            return False
        self.written_rows.extend(rows)
        return True

    def close(self) -> None:
        self.closed = True


def sample(timestamp: float, ride_id: str = "ride-1") -> RideSample:
    return RideSample(
        timestamp=timestamp,
        ride_id=ride_id,
        state=MotionState(timestamp=timestamp, latitude=12.9, longitude=77.6, speed_mps=10.0),
        metrics=RideMetrics(speed_kmh=36.0, g_force=1.1),
    )


@pytest.fixture
def storage_config(tmp_path: Path) -> StorageConfig:
    return StorageConfig(
        enabled=True,
        batch_size=5,
        flush_interval_s=0.5,
        spool_dir=str(tmp_path / "spool"),
        max_spool_mb=1.0,
    )


async def test_samples_reach_influx_on_flush(storage_config: StorageConfig) -> None:
    influx = FakeInflux()
    writer = BatchWriter(storage_config, influx)

    for i in range(3):
        writer.submit(sample(1000.0 + i))
    assert influx.written == []  # nothing written until flushed

    written = await writer.flush()
    assert written == 3
    assert len(influx.written) == 3
    assert writer.stats.written == 3
    assert writer.pending == 0


async def test_flush_of_an_empty_batch_is_a_no_op(storage_config: StorageConfig) -> None:
    writer = BatchWriter(storage_config, FakeInflux())
    assert await writer.flush() == 0
    assert writer.stats.flushes == 0


async def test_an_unreachable_database_spools_to_disk(storage_config: StorageConfig) -> None:
    """The whole point: an outage must not lose the ride."""
    influx = FakeInflux(reachable=False)
    writer = BatchWriter(storage_config, influx)
    await writer.start()

    for i in range(4):
        writer.submit(sample(1000.0 + i))
    await writer.flush()

    files = writer.spool_files()
    assert len(files) == 1
    assert writer.stats.spooled == 4
    assert writer.stats.failed_flushes == 1

    rows = _read_spool_file(files[0])
    assert len(rows) == 4
    assert rows[0]["ride_id"] == "ride-1"
    assert rows[0]["speed_kmh"] == pytest.approx(36.0)

    await writer.stop()


async def test_spool_is_replayed_once_the_database_returns(
    storage_config: StorageConfig,
) -> None:
    influx = FakeInflux(reachable=False)
    writer = BatchWriter(storage_config, influx)
    await writer.start()

    for i in range(6):
        writer.submit(sample(1000.0 + i))
    await writer.flush()
    assert writer.spool_files()

    # Server comes back; a new writer picks up where the last one left off,
    # which is the reboot case.
    influx.reachable = True
    recovered = BatchWriter(storage_config, influx)
    await recovered.start()

    assert recovered.stats.replayed == 6
    assert len(influx.written_rows) == 6
    assert recovered.spool_files() == []

    await recovered.stop()
    await writer.stop()


async def test_replay_stops_and_keeps_data_if_the_database_drops_again(
    storage_config: StorageConfig,
) -> None:
    influx = FakeInflux(reachable=False)
    writer = BatchWriter(storage_config, influx)
    await writer.start()

    for batch in range(3):
        for i in range(4):
            writer.submit(sample(2000.0 + batch * 100 + i, ride_id=f"ride-{batch}"))
        await writer.flush()

    assert len(writer.spool_files()) == 3

    # Reachable for the first file, then gone again.
    class Flaky(FakeInflux):
        def write_rows(self, rows: list[dict[str, Any]]) -> bool:
            if len(self.written_rows) >= 4:
                return False
            self.written_rows.extend(rows)
            return True

    flaky = Flaky(reachable=True)
    recovered = BatchWriter(storage_config, flaky)
    await recovered.start()

    assert recovered.stats.replayed == 4
    # The untransmitted files must still be on disk, not dropped.
    assert len(recovered.spool_files()) == 2

    await recovered.stop()
    await writer.stop()


async def test_spool_is_bounded_and_drops_the_oldest(tmp_path: Path) -> None:
    """A full card takes the node down, so the spool has a hard ceiling."""
    config = StorageConfig(
        enabled=True,
        batch_size=10,
        spool_dir=str(tmp_path / "spool"),
        max_spool_mb=0.002,  # 2 KB, small enough to overflow quickly
    )
    influx = FakeInflux(reachable=False)
    writer = BatchWriter(config, influx)
    await writer.start()

    for batch in range(12):
        for i in range(10):
            writer.submit(sample(3000.0 + batch * 100 + i, ride_id=f"ride-{batch:02d}"))
        await writer.flush()

    assert writer.stats.dropped > 0

    # The newest file must survive even when it alone exceeds the cap; an
    # eviction loop that runs to empty throws away the telemetry that matters
    # most in order to honour a soft limit.
    names = [p.name for p in writer.spool_files()]
    assert names, "eviction must never empty the spool completely"
    assert "ride-11" in names[-1], f"kept the wrong file: {names}"
    assert all("ride-00" not in name for name in names)

    await writer.stop()


async def test_stop_flushes_what_is_still_pending(storage_config: StorageConfig) -> None:
    influx = FakeInflux()
    writer = BatchWriter(storage_config, influx)
    await writer.start()

    writer.submit(sample(1000.0))
    writer.submit(sample(1001.0))
    await writer.stop()

    assert len(influx.written) == 2
    assert influx.closed


async def test_a_full_batch_flushes_without_waiting_for_the_interval(
    storage_config: StorageConfig,
) -> None:
    storage_config.flush_interval_s = 30.0  # far longer than the test
    influx = FakeInflux()
    writer = BatchWriter(storage_config, influx)
    await writer.start()

    for i in range(storage_config.batch_size):
        writer.submit(sample(1000.0 + i))

    import asyncio

    async with asyncio.timeout(3.0):
        while not influx.written:
            await asyncio.sleep(0.05)

    assert len(influx.written) == storage_config.batch_size
    await writer.stop()


def test_a_truncated_spool_line_does_not_discard_the_file(tmp_path: Path) -> None:
    """A power cut mid-write leaves a partial line; the rest is still good."""
    path = tmp_path / "ride.jsonl"
    path.write_text(
        json.dumps({"timestamp": 1.0, "ride_id": "r"}) + "\n"
        + json.dumps({"timestamp": 2.0, "ride_id": "r"}) + "\n"
        + '{"timestamp": 3.0, "ride_i',  # cut off by the power loss
        encoding="utf-8",
    )

    rows = _read_spool_file(path)
    assert len(rows) == 2


def test_a_missing_spool_file_reads_as_empty(tmp_path: Path) -> None:
    assert _read_spool_file(tmp_path / "absent.jsonl") == []


def test_timestamps_convert_to_integer_nanoseconds() -> None:
    assert _to_nanos(1.5) == 1_500_000_000
    assert isinstance(_to_nanos(1789161771.78972), int)
