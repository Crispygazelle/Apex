"""Dashboard API and WebSocket, driven through the real ASGI stack."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.dashboard.app import create_app
from app.models import MotionState, RideMetrics, RideSample, SystemState
from app.pipeline.coordinator import Coordinator
from app.streaming.websocket import TelemetryHub


def sample(timestamp: float, speed_kmh: float = 50.0) -> RideSample:
    return RideSample(
        timestamp=timestamp,
        ride_id="ride-test",
        state=MotionState(
            timestamp=timestamp,
            latitude=12.9716,
            longitude=77.5946,
            altitude_m=920.0,
            speed_mps=speed_kmh / 3.6,
        ),
        metrics=RideMetrics(speed_kmh=speed_kmh, g_force=1.05, distance_m=1234.0),
        system_state=SystemState.RIDING,
        gps_valid=True,
        satellites=11,
    )


@pytest.fixture
def coordinator(config: AppConfig) -> Coordinator:
    """A coordinator with a seeded buffer, not actually running."""
    coord = Coordinator(config, ride_id="ride-test")
    for i in range(200):
        coord.buffer.append(sample(1000.0 + i * 0.02, speed_kmh=40.0 + i * 0.05))
    return coord


@pytest.fixture
def client(coordinator: Coordinator) -> TestClient:
    return TestClient(create_app(coordinator, TelemetryHub()))


def test_health_reports_the_ride(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["ride_id"] == "ride-test"
    assert body["samples"] == 200
    # The header renders these, so a missing field shows a placeholder to the
    # rider rather than the helmet identity.
    assert body["helmet_id"] == "helmet01"
    assert body["sensor_backend"] == "sim"


def test_state_returns_the_latest_sample(client: TestClient) -> None:
    body = client.get("/api/state").json()
    assert body["ride_id"] == "ride-test"
    assert body["satellites"] == 11
    assert body["speed_kmh"] == pytest.approx(49.95)


def test_state_is_503_before_any_telemetry(config: AppConfig) -> None:
    empty = Coordinator(config, ride_id="ride-empty")
    with TestClient(create_app(empty, TelemetryHub())) as client:
        response = client.get("/api/state")
        assert response.status_code == 503
        assert response.json()["system_state"] == "starting"


def test_history_returns_a_window(client: TestClient) -> None:
    body = client.get("/api/history?seconds=1").json()
    assert body["count"] > 0
    assert body["count"] <= 51  # 1 s at 50 Hz
    assert body["samples"][0]["timestamp"] < body["samples"][-1]["timestamp"]


def test_history_step_decimates(client: TestClient) -> None:
    """A chart does not need 50 Hz, and a Pi Zero should not serialise it."""
    dense = client.get("/api/history?seconds=4&step=1").json()["count"]
    sparse = client.get("/api/history?seconds=4&step=10").json()["count"]
    assert sparse < dense
    assert sparse == pytest.approx(dense / 10, abs=2)


def test_events_endpoint_is_empty_without_a_director(client: TestClient) -> None:
    body = client.get("/api/events").json()
    assert body["events"] == []
    assert body["ride_id"] == "ride-test"


@pytest.mark.parametrize("query", ["seconds=0", "seconds=-5", "seconds=9999", "step=0"])
def test_history_rejects_out_of_range_queries(client: TestClient, query: str) -> None:
    assert client.get(f"/api/history?{query}").status_code == 422


def test_stats_include_pipeline_and_streaming(client: TestClient) -> None:
    body = client.get("/api/stats").json()
    assert body["pipeline"]["ride_id"] == "ride-test"
    assert body["streaming"]["clients"] == 0
    assert body["storage"] == {"enabled": False}


def test_ride_query_is_501_when_storage_is_disabled(client: TestClient) -> None:
    assert client.get("/api/ride/ride-test").status_code == 501


def test_index_and_assets_are_served(client: TestClient) -> None:
    assert client.get("/").status_code == 200
    assert "APEX" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_websocket_seeds_a_new_client_with_current_state(coordinator: Coordinator) -> None:
    """A fresh browser should not sit blank until the next broadcast."""
    hub = TelemetryHub()
    with (
        TestClient(create_app(coordinator, hub)) as client,
        client.websocket_connect("/ws/telemetry") as socket,
    ):
        first = socket.receive_json()
        assert first["ride_id"] == "ride-test"
        assert hub.client_count == 1

    assert hub.client_count == 0, "disconnect must unregister the client"


def test_websocket_refuses_clients_beyond_capacity(coordinator: Coordinator) -> None:
    from starlette.websockets import WebSocketDisconnect

    from app.config import StreamingConfig

    hub = TelemetryHub(StreamingConfig(max_clients=1))
    with TestClient(create_app(coordinator, hub)) as client, client.websocket_connect(
        "/ws/telemetry"
    ):
        assert hub.client_count == 1

        # The handshake succeeds and the server then closes with 1013 (try
        # again later), so the refusal surfaces on the first read.
        with (
            client.websocket_connect("/ws/telemetry") as rejected,
            pytest.raises(WebSocketDisconnect) as excinfo,
        ):
            rejected.receive_json()
        assert excinfo.value.code == 1013

        assert hub.client_count == 1


def test_sos_endpoints_report_disabled_without_a_safety_layer(client: TestClient) -> None:
    body = client.get("/api/sos").json()
    assert body["enabled"] is False
    assert body["state"] == "idle"

    assert client.post("/api/sos/cancel").status_code == 501


async def test_cancelling_an_sos_over_http(coordinator: Coordinator, config: AppConfig) -> None:
    """Driven in-loop rather than through TestClient.

    TestClient runs the app on its own thread, but in production the dashboard
    shares the fusion loop with the SOS countdown. Exercising it through an
    in-loop ASGI transport is both closer to the real arrangement and the only
    way to arm a countdown, which needs a running loop to create its task.
    """
    import httpx

    from app.safety.monitor import SafetyMonitor
    from app.safety.status_led import RecordingLedBackend
    from tests.test_sos import EVENT, StubNotifier

    config.safety.sos_countdown_s = 30.0
    notifier = StubNotifier()
    safety = SafetyMonitor(
        config,
        coordinator,
        notifier=notifier,
        led_backend=RecordingLedBackend(),
        retry_backoff_s=0.0,
    )
    app = create_app(coordinator, TelemetryHub(), None, safety)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as client:
        body = (await client.get("/api/sos")).json()
        assert body["state"] == "idle"

        safety.sos.arm(EVENT)
        live = (await client.get("/api/sos")).json()
        assert live["enabled"] is True
        assert live["state"] == "countdown"
        assert live["remaining_s"] > 25.0
        # The banner is rebuilt from this on a page reload.
        assert live["event"]["peak_g"] == EVENT.peak_g

        cancelled = (await client.post("/api/sos/cancel")).json()
        assert cancelled["cancelled"] is True
        await safety.sos.wait()

        # A second cancel has nothing to stop and must say so rather than
        # reporting a success that did not happen.
        refused = await client.post("/api/sos/cancel")
        assert refused.status_code == 409
        assert "cancel" in refused.json()["detail"].lower()

    assert notifier.calls == []


def test_stats_include_the_safety_layer(client: TestClient) -> None:
    body = client.get("/api/stats").json()
    assert body["safety"] == {"enabled": False}
    assert body["voice"] == {"enabled": False}
