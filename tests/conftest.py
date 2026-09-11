"""Shared fixtures. Every test runs against the simulated backend."""

from __future__ import annotations

import pytest

from app.config import AppConfig


@pytest.fixture
def config() -> AppConfig:
    """Defaults with everything that needs a network or a device switched off."""
    cfg = AppConfig()
    cfg.node.sensor_backend = "sim"
    cfg.sensors.mic.enabled = False
    cfg.storage.enabled = False
    cfg.streaming.enabled = False
    cfg.dashboard.enabled = False
    return cfg
