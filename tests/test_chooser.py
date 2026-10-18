"""The gate in front of a ride: hardware only when the sensors answer."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import AppConfig
from app.dashboard.chooser import create_chooser
from app.main import needs_chooser
from app.sensors.probe import HARDWARE_REQUIRED, probe_hardware


def _args(**overrides: object) -> SimpleNamespace:
    base = dict(
        backend=None,
        replay=None,
        record=None,
        demo_sprint=False,
        simulate_crash=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_an_explicit_backend_skips_the_gate() -> None:
    config = AppConfig()
    config.dashboard.enabled = True
    assert needs_chooser(_args(), config) is True
    assert needs_chooser(_args(backend="hardware"), config) is False
    assert needs_chooser(_args(backend="sim"), config) is False
    assert needs_chooser(_args(demo_sprint=True), config) is False
    config.dashboard.enabled = False
    assert needs_chooser(_args(), config) is False


def test_a_laptop_has_no_helmet_hardware() -> None:
    probe = probe_hardware(AppConfig())
    assert probe["available"] is False
    assert probe["reason"] == HARDWARE_REQUIRED


def test_hardware_is_refused_when_nothing_is_connected() -> None:
    chosen: list[str] = []
    client = TestClient(
        create_chooser({"available": False, "reason": HARDWARE_REQUIRED}, chosen.append)
    )
    refused = client.post("/api/session", json={"mode": "hardware"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == HARDWARE_REQUIRED
    assert chosen == []

    accepted = client.post("/api/session", json={"mode": "sim"})
    assert accepted.status_code == 200
    assert accepted.json()["mode"] == "sim"
    assert chosen == ["sim"]


def test_hardware_is_accepted_when_the_probe_says_so() -> None:
    chosen: list[str] = []
    client = TestClient(create_chooser({"available": True, "reason": ""}, chosen.append))
    assert client.post("/api/session", json={"mode": "hardware"}).status_code == 200
    assert chosen == ["hardware"]
    again = client.post("/api/session", json={"mode": "sim"})
    assert again.status_code == 409
