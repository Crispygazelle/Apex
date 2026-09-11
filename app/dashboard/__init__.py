"""FastAPI dashboard serving the REST API, the WebSocket, and the UI."""

from __future__ import annotations

from app.dashboard.app import create_app

__all__ = ["create_app"]
