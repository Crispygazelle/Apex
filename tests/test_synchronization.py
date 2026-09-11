"""Out-of-order reassembly, which is the whole reason this class exists."""

from __future__ import annotations

from app.models import SensorReading, SensorSource
from app.processing.synchronization import FrameSynchronizer


def reading(timestamp: float, source: SensorSource = SensorSource.IMU) -> SensorReading:
    return SensorReading(timestamp=timestamp, source=source, payload={"ax_mps2": 0.0})


def test_holds_readings_inside_the_reorder_window() -> None:
    sync = FrameSynchronizer(reorder_window_s=0.3)
    sync.push(reading(10.0))

    # Nothing is older than the watermark yet, so nothing may be released.
    assert list(sync.drain()) == []
    assert len(sync) == 1


def test_releases_in_timestamp_order_not_arrival_order() -> None:
    sync = FrameSynchronizer(reorder_window_s=0.2)

    # A GPS fix stamped earlier arrives after a later IMU sample, which is
    # exactly what independent sensor threads produce.
    sync.push(reading(10.10, SensorSource.IMU))
    sync.push(reading(10.05, SensorSource.GPS))
    sync.push(reading(10.40, SensorSource.IMU))

    released = [r.timestamp for r in sync.drain()]
    assert released == [10.05, 10.10]
    assert released == sorted(released)


def test_drops_readings_that_arrive_after_their_slot_has_passed() -> None:
    sync = FrameSynchronizer(reorder_window_s=0.1)
    sync.push(reading(10.0))
    sync.push(reading(10.5))
    assert [r.timestamp for r in sync.drain()] == [10.0]

    # 9.9 predates what we already emitted; admitting it would hand the filter
    # a negative dt.
    assert sync.push(reading(9.9)) is False
    assert sync.stats.dropped_late == 1


def test_flush_releases_everything_for_shutdown() -> None:
    sync = FrameSynchronizer(reorder_window_s=5.0)
    for i in range(5):
        sync.push(reading(100.0 + i))

    assert list(sync.drain()) == []  # window is wide, nothing due
    assert len(list(sync.drain(flush=True))) == 5
    assert len(sync) == 0


def test_zero_window_passes_readings_straight_through() -> None:
    sync = FrameSynchronizer(reorder_window_s=0.0)
    sync.push(reading(1.0))
    assert [r.timestamp for r in sync.drain()] == [1.0]


def test_stats_track_depth_and_throughput() -> None:
    sync = FrameSynchronizer(reorder_window_s=0.5)
    for i in range(10):
        sync.push(reading(50.0 + i * 0.1))
    list(sync.drain())

    assert sync.stats.accepted == 10
    assert sync.stats.max_depth == 10
    assert sync.stats.emitted > 0
