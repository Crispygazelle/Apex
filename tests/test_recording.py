"""Recording and replay: the demo must not depend on a live simulator.

The claim being tested is that a replay is not a puppet show. Recorded raw
readings go back through calibration, the filter and metrics, so a replayed
ride has to land on the same distance and speed as the ride that produced it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import AppConfig
from app.models import SensorReading, SensorSource
from app.sensors import build_sensors
from app.sensors.base import SensorError
from app.sensors.demo import nagpur_airport_profile
from app.sensors.recording import (
    RECORDED_SOURCES,
    Recording,
    RecordingMeta,
    RideRecorder,
    load_recording,
)
from app.sensors.simulated import SimulatedGps, SimulatedImu, RideSimulator

from .harness import VirtualClock, run_offline_ride


def reading(t: float, source: SensorSource = SensorSource.IMU) -> SensorReading:
    return SensorReading(
        timestamp=t, source=source, payload={"ax_mps2": 1.5, "valid": True}, quality=0.9
    )


def write_recording(path: Path, readings: list[SensorReading], **meta_kwargs) -> None:
    recorder = RideRecorder(path, RecordingMeta(ride_id="ride-test", **meta_kwargs))
    recorder.open()
    for item in readings:
        recorder(item)
    recorder.close()


def test_audio_is_never_recorded(tmp_path: Path) -> None:
    """A microphone recording on disk is a privacy liability, not a feature."""
    path = tmp_path / "ride.ndjson"
    recorder = RideRecorder(path, RecordingMeta(ride_id="ride-test"))
    recorder.open()
    recorder(reading(1.0, SensorSource.IMU))
    recorder(
        SensorReading(
            timestamp=1.1, source=SensorSource.MIC, payload={"pcm": b"\x01\x02" * 64}
        )
    )
    recorder.close()

    body = path.read_text(encoding="utf-8")
    assert "pcm" not in body
    assert recorder.readings_written == 1
    assert recorder.skipped_audio == 1
    assert SensorSource.MIC not in RECORDED_SOURCES


def test_round_trip_preserves_payload_and_gaps(tmp_path: Path) -> None:
    path = tmp_path / "ride.ndjson"
    write_recording(
        path,
        [reading(100.0), reading(100.01), reading(100.1, SensorSource.GPS)],
        route_name="Test route",
        destination_latitude=21.09,
        destination_longitude=79.05,
        route_length_m=1234.0,
    )

    loaded = load_recording(path)
    assert loaded.reading_count == 3
    assert loaded.meta.route_name == "Test route"
    assert loaded.destination == (21.09, 79.05)
    assert loaded.route_length_m == 1234.0
    assert loaded.duration_s == pytest.approx(0.1, abs=1e-6)

    imu = list(loaded.per_source[SensorSource.IMU])
    assert imu[0].payload["ax_mps2"] == 1.5
    assert imu[0].quality == pytest.approx(0.9)
    # The gap between readings is what the filter integrates over.
    assert imu[1].timestamp - imu[0].timestamp == pytest.approx(0.01)


def test_voice_script_travels_with_the_recording(tmp_path: Path) -> None:
    """A replay must not depend on whichever profile produced it."""
    path = tmp_path / "ride.ndjson"
    write_recording(
        path,
        [reading(1.0), reading(1.1, SensorSource.GPS)],
        voice_script=[(280.0, "hey apex what's my speed")],
    )
    loaded = load_recording(path)
    assert loaded.voice_script == [(280.0, "hey apex what's my speed")]


def test_a_missing_or_empty_recording_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SensorError, match="not found"):
        load_recording(tmp_path / "absent.ndjson")

    empty = tmp_path / "empty.ndjson"
    empty.write_text(json.dumps({"apex_recording": 1, "ride_id": "x"}) + "\n", encoding="utf-8")
    with pytest.raises(SensorError, match="no replayable readings"):
        load_recording(empty)


def test_a_future_format_version_is_refused(tmp_path: Path) -> None:
    """Silently misreading a newer recording would be worse than failing."""
    path = tmp_path / "future.ndjson"
    path.write_text(json.dumps({"apex_recording": 99}) + "\n", encoding="utf-8")
    with pytest.raises(SensorError, match="v99"):
        load_recording(path)


def test_malformed_lines_are_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "torn.ndjson"
    lines = [
        json.dumps({"apex_recording": 1, "ride_id": "r"}),
        json.dumps({"t": 1.0, "source": "imu", "payload": {}}),
        "{this is not json",
        json.dumps({"t": 1.1, "source": "nonsense", "payload": {}}),
        json.dumps({"t": 1.2, "source": "gps", "payload": {}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    loaded = load_recording(path)
    assert loaded.reading_count == 2


def test_a_truncated_gzip_recording_still_replays(tmp_path: Path) -> None:
    """A ride that ended in a power cut is still a ride.

    Refusing to open it because the gzip end marker is missing would throw away
    everything that was flushed, which is the opposite of robust.
    """
    whole = tmp_path / "whole.ndjson.gz"
    write_recording(whole, [reading(1.0 + i * 0.01) for i in range(400)])

    # Chop the tail off, exactly as an interrupted write would leave it.
    payload = whole.read_bytes()
    torn = tmp_path / "torn.ndjson.gz"
    torn.write_bytes(payload[: int(len(payload) * 0.6)])

    loaded = load_recording(torn, speed=0.0)
    assert 0 < loaded.reading_count < 400


def test_replay_sensors_rebase_timestamps_onto_now(tmp_path: Path) -> None:
    """Downstream must see a ride happening now, with the recorded gaps intact."""
    path = tmp_path / "ride.ndjson"
    write_recording(path, [reading(500.0), reading(500.02), reading(500.0, SensorSource.GPS)])

    clock = VirtualClock(start=9_000.0)
    loaded = load_recording(path, speed=0.0)  # no real sleeping in a test
    config = AppConfig()
    sensors = build_sensors(config, recording=loaded, include_mic=False, time_source=clock)

    assert {s.source for s in sensors.sensors} == {SensorSource.IMU, SensorSource.GPS}
    assert sensors.recording is loaded

    imu = sensors.by_source(SensorSource.IMU)
    imu.open()
    first = imu.read()
    second = imu.read()
    assert first.timestamp == pytest.approx(9_000.0)
    assert second.timestamp - first.timestamp == pytest.approx(0.02)
    assert imu.read() is None  # exhausted, and does not loop by default
    assert imu.exhausted


def test_replay_can_loop_for_an_unattended_demo(tmp_path: Path) -> None:
    """Timestamps must keep climbing across the loop boundary.

    Restarting them would put every reading of the second pass behind the
    reorder window, so the synchronizer would drop the lot and the dashboard
    would sit at zero for the rest of the demo.
    """
    path = tmp_path / "ride.ndjson"
    write_recording(path, [reading(1.0), reading(1.01), reading(1.02)])

    clock = VirtualClock()
    loaded = load_recording(path, speed=0.0, loop=True)
    imu = build_sensors(
        AppConfig(), recording=loaded, include_mic=False, time_source=clock
    ).by_source(SensorSource.IMU)
    imu.open()

    stamps = [imu.read().timestamp for _ in range(9)]  # three passes
    assert len(stamps) == 9
    assert stamps == sorted(stamps), "a loop must not rewind the clock"
    assert len(set(stamps)) == 9, "no timestamp may repeat across passes"
    # And the second pass starts after the first one finished, not on top of it.
    assert stamps[3] > stamps[2]


def test_a_recording_wins_over_the_simulated_backend(tmp_path: Path) -> None:
    """Mixing replayed and freshly generated readings would be incoherent."""
    path = tmp_path / "ride.ndjson"
    write_recording(path, [reading(1.0), reading(1.1, SensorSource.GPS)])

    sensors = build_sensors(
        AppConfig(),
        simulator=RideSimulator(),
        recording=load_recording(path, speed=0.0),
        include_mic=False,
    )
    assert sensors.simulator is None
    assert sensors.recording is not None
    assert all(type(s).__name__.startswith("Replay") for s in sensors.sensors)


def test_a_recorded_ride_replays_to_the_same_distance(tmp_path: Path) -> None:
    """The point of the whole module: replay reproduces the ride, not a summary.

    Recording captures raw IMU and GPS output, so the second pass re-derives
    distance and speed through the real pipeline. Agreement to a few metres
    over a kilometre means nothing is being played back pre-cooked.
    """
    config = AppConfig()
    config.sensors.mic.enabled = False
    live = run_offline_ride(config, duration_s=70.0, profile=nagpur_airport_profile())

    path = tmp_path / "captured.ndjson"
    recorder = RideRecorder(path, RecordingMeta(ride_id="ride-capture"))
    recorder.open()
    for item in live.readings:
        recorder(item)
    recorder.close()

    loaded = load_recording(path, speed=0.0)
    replayed = run_offline_ride(config, duration_s=70.0, recording=loaded)

    assert replayed.samples, "replay produced no telemetry"
    live_km = live.final.metrics.distance_m
    replay_km = replayed.final.metrics.distance_m
    assert live_km > 500.0, "the source ride should actually cover ground"
    assert replay_km == pytest.approx(live_km, rel=0.01), (
        f"replayed {replay_km:.1f} m against a recorded {live_km:.1f} m"
    )
    assert replayed.final.metrics.max_speed_kmh == pytest.approx(
        live.final.metrics.max_speed_kmh, rel=0.01
    )
