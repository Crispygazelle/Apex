"""Demo director: drive the narrative on cue, without lying about the source.

Injection is a simulator feature. The interesting assertions here are the
refusals: a replay and a live helmet must not be able to fabricate a pothole,
because the whole value of the telemetry is that it came from somewhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.dashboard.app import create_app
from app.models import SensorReading, SensorSource
from app.pipeline.coordinator import Coordinator
from app.sensors.demo import nagpur_airport_profile
from app.sensors.recording import RecordingMeta, RideRecorder, load_recording
from app.sensors.simulated import RideProfile, RideSimulator
from app.streaming.websocket import TelemetryHub


@pytest.fixture
def sim_client(config: AppConfig) -> tuple[TestClient, RideSimulator]:
    simulator = RideSimulator(RideProfile(gps_dropouts=[]))
    coordinator = Coordinator(
        config, ride_id="ride-test", simulator=simulator, include_mic=False
    )
    return TestClient(create_app(coordinator, TelemetryHub())), simulator


def test_pothole_is_placed_ahead_of_the_wheel(
    sim_client: tuple[TestClient, RideSimulator],
) -> None:
    client, simulator = sim_client
    simulator.state_at(20.0)  # ride a little way in
    before = list(simulator.profile.potholes_m)

    body = client.post("/api/demo/pothole").json()

    assert body["injected"] == "pothole"
    assert len(simulator.profile.potholes_m) == len(before) + 1
    # Ahead, not underneath: a pothole at the current distance would already
    # have been passed by the time the IMU is sampled again.
    assert body["at_m"] > simulator.distance_m


def test_brake_lowers_the_speed_on_the_demo_route(config: AppConfig) -> None:
    """Uses the real route, because braking is a path-mode capability."""
    simulator = RideSimulator(nagpur_airport_profile())
    coordinator = Coordinator(
        config, ride_id="ride-demo", simulator=simulator, include_mic=False
    )
    client = TestClient(create_app(coordinator, TelemetryHub()))

    simulator.state_at(40.0)
    cruising = simulator.state_at(41.0).speed_mps
    assert cruising > 10.0
    assert client.get("/api/demo").json()["can_brake"] is True

    assert client.post("/api/demo/brake?target_kmh=8&duration_s=4").status_code == 200

    slowest = cruising
    t = 41.0
    while t < 45.0:
        slowest = min(slowest, simulator.state_at(t).speed_mps)
        t += 0.25
    assert slowest < cruising / 2.0


def test_brake_is_refused_where_it_would_fake_the_physics(
    sim_client: tuple[TestClient, RideSimulator],
) -> None:
    """The analytic profile differentiates its own speed curve.

    Clamping it would hand the IMU a step change worth hundreds of g, which the
    crash detector would rightly believe. Refusing is the honest answer.
    """
    client, simulator = sim_client
    assert simulator.supports_speed_injection is False

    response = client.post("/api/demo/brake")
    assert response.status_code == 409
    assert "no path" in response.json()["detail"]
    assert client.get("/api/demo").json()["can_brake"] is False


def test_brake_arguments_are_bounded(sim_client: tuple[TestClient, RideSimulator]) -> None:
    client, _ = sim_client
    assert client.post("/api/demo/brake?duration_s=0").status_code == 422
    assert client.post("/api/demo/brake?target_kmh=-5").status_code == 422
    assert client.post("/api/demo/brake?duration_s=600").status_code == 422


def test_crash_rehearsal_schedules_an_impact(
    sim_client: tuple[TestClient, RideSimulator],
) -> None:
    client, simulator = sim_client
    simulator.state_at(30.0)
    assert simulator.profile.crash_at_s is None

    client.post("/api/demo/crash")

    assert simulator.profile.crash_at_s == pytest.approx(30.0, abs=0.1)
    # The bike really does stop, which is what the detector corroborates with.
    assert simulator.state_at(31.0).speed_mps == pytest.approx(0.0, abs=0.1)


def test_injection_is_refused_on_a_replay(tmp_path: Path, config: AppConfig) -> None:
    path = tmp_path / "ride.ndjson"
    recorder = RideRecorder(path, RecordingMeta(ride_id="r"))
    recorder.open()
    for i in range(3):
        recorder(SensorReading(timestamp=float(i), source=SensorSource.IMU, payload={}))
        recorder(SensorReading(timestamp=float(i), source=SensorSource.GPS, payload={}))
    recorder.close()

    coordinator = Coordinator(
        config,
        ride_id="ride-replay",
        recording=load_recording(path, speed=0.0),
        include_mic=False,
    )
    client = TestClient(create_app(coordinator, TelemetryHub()))

    for endpoint in ("pothole", "brake", "crash"):
        response = client.post(f"/api/demo/{endpoint}")
        assert response.status_code == 409
        assert "replay" in response.json()["detail"]

    assert client.get("/api/demo").json()["can_inject"] is False
    assert client.get("/api/health").json()["sensor_backend"] == "replay"


def test_injection_is_refused_without_a_simulator(config: AppConfig) -> None:
    """Stands in for the hardware backend, which has no simulator either."""
    coordinator = Coordinator(config, ride_id="ride-test", include_mic=False)
    coordinator.sensor_set.simulator = None
    client = TestClient(create_app(coordinator, TelemetryHub()))

    response = client.post("/api/demo/pothole")
    assert response.status_code == 409
    assert "real sensors" in response.json()["detail"]


def test_speaking_is_refused_when_voice_is_off(
    sim_client: tuple[TestClient, RideSimulator],
) -> None:
    client, _ = sim_client
    assert client.post("/api/demo/say?text=hello").status_code == 501
    assert client.get("/api/demo").json()["can_speak"] is False


def test_a_spoken_phrase_goes_through_the_real_intent_path(config: AppConfig) -> None:
    """Not a canned reply: the phrase is parsed and answered from telemetry."""
    from app.voice.assistant import VoiceAssistant
    from app.voice.stt import FakeTranscriber
    from app.voice.tts import RecordingSpeaker
    from tests.test_voice import seed

    config.voice.enabled = True
    coordinator = Coordinator(config, ride_id="ride-test", include_mic=False)
    seed(coordinator, speed_kmh=61.0)
    voice = VoiceAssistant(
        config, coordinator, transcriber=FakeTranscriber(), speaker=RecordingSpeaker()
    )
    client = TestClient(create_app(coordinator, TelemetryHub(), None, None, voice))

    body = client.post("/api/demo/say?text=hey apex what's my speed").json()
    assert "61" in body["apex"]
    assert body["rider"] == "hey apex what's my speed"


def test_say_rejects_an_empty_or_oversized_phrase(config: AppConfig) -> None:
    coordinator = Coordinator(config, ride_id="ride-test", include_mic=False)
    client = TestClient(create_app(coordinator, TelemetryHub()))
    assert client.post("/api/demo/say?text=").status_code == 422
    assert client.post(f"/api/demo/say?text={'x' * 500}").status_code == 422


def test_cancelling_a_rehearsal_lets_the_bike_move_again() -> None:
    """I'm OK clears the scripted stop. The route speed comes back."""
    simulator = RideSimulator(nagpur_airport_profile())
    simulator.state_at(40.0)
    cruising = simulator.state_at(41.0).speed_mps
    assert cruising > 10.0

    simulator.inject_crash()
    stopped = simulator.state_at(48.0).speed_mps
    assert stopped < 2.0

    simulator.resume_after_rehearsal()
    recovered = simulator.state_at(70.0).speed_mps
    assert recovered > 8.0


def test_a_dropped_pin_is_straight_line_and_clear_removes_it(config: AppConfig) -> None:
    coordinator = Coordinator(config, ride_id="ride-test", include_mic=False)
    client = TestClient(create_app(coordinator, TelemetryHub()))
    placed = client.post("/api/destination?latitude=21.1&longitude=79.1")
    assert placed.status_code == 200
    assert coordinator.processor.destination_kind == "straight"
    assert coordinator.processor.destination == (21.1, 79.1)

    cleared = client.post("/api/destination/clear")
    assert cleared.json()["destination_kind"] == "none"
    assert coordinator.processor.destination is None
