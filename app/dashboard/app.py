"""FastAPI dashboard: REST for history, WebSocket for live telemetry.

This runs inside the same event loop as the fusion pipeline, so the handlers
read the ring buffer by attribute access with no IPC and no database round
trip. That is the whole reason for the single-process design.

Handlers must not block. Anything that talks to InfluxDB goes through a thread,
because the client is synchronous and a slow query would otherwise stall the
100 Hz pipeline sharing this loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.storage.influxdb import InfluxUnavailableError

if TYPE_CHECKING:
    from app.pipeline.coordinator import Coordinator
    from app.pipeline.events import EventLog
    from app.safety.monitor import SafetyMonitor
    from app.storage.batch_writer import BatchWriter
    from app.streaming.websocket import TelemetryHub
    from app.voice.assistant import VoiceAssistant

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    coordinator: Coordinator,
    hub: TelemetryHub,
    batch_writer: BatchWriter | None = None,
    safety: SafetyMonitor | None = None,
    voice: VoiceAssistant | None = None,
    event_log: EventLog | None = None,
) -> FastAPI:
    """Build the dashboard around an already-running coordinator."""
    app = FastAPI(
        title="APEX telemetry",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.get("/api/state")
    async def get_state() -> JSONResponse:
        """Most recent fused sample."""
        sample = coordinator.latest
        if sample is None:
            return JSONResponse(
                {"detail": "no telemetry yet", "system_state": coordinator.system_state.value},
                status_code=503,
            )
        return JSONResponse(sample.to_dict())

    @app.get("/api/stats")
    async def get_stats() -> dict[str, Any]:
        """Pipeline, sensor, streaming, and storage health in one place."""
        payload: dict[str, Any] = {"pipeline": coordinator.stats(), "streaming": hub.stats()}
        payload["safety"] = safety.stats() if safety is not None else {"enabled": False}
        payload["voice"] = (
            {"enabled": True, **voice.stats.as_dict()}
            if voice is not None
            else {"enabled": False}
        )
        payload["storage"] = (
            {
                **batch_writer.stats.as_dict(),
                "healthy": batch_writer.writer.healthy,
                "pending": batch_writer.pending,
            }
            if batch_writer is not None
            else {"enabled": False}
        )
        return payload

    @app.get("/api/history")
    async def get_history(
        seconds: float = Query(default=60.0, gt=0.0, le=1800.0),
        step: int = Query(default=1, ge=1, le=100),
    ) -> dict[str, Any]:
        """Recent samples from the ring buffer, for drawing a trace on load.

        `step` decimates the result: the buffer holds 50 Hz, which is far more
        than a chart needs and a lot of JSON to serialise on a Pi Zero.
        """
        window = coordinator.buffer.window(seconds)
        return {
            "ride_id": coordinator.ride_id,
            "count": len(window[::step]),
            "buffer_span_s": round(coordinator.buffer.span_seconds(), 2),
            "samples": [sample.to_dict() for sample in window[::step]],
        }

    @app.get("/api/events")
    async def get_events() -> dict[str, Any]:
        """Voice exchanges and threshold events for the current ride."""
        return {
            "ride_id": coordinator.ride_id,
            "events": event_log.as_dicts() if event_log is not None else [],
        }

    @app.get("/api/ride/{ride_id}")
    async def get_ride(
        ride_id: str, limit: int = Query(default=5000, ge=1, le=50_000)
    ) -> dict[str, Any]:
        """A stored ride from InfluxDB, for post-ride analysis."""
        if batch_writer is None:
            raise HTTPException(status_code=501, detail="storage is disabled")

        try:
            # Synchronous client: keep the query off the event loop.
            rows = await asyncio.to_thread(batch_writer.writer.query_ride, ride_id, limit)
        except InfluxUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return {"ride_id": ride_id, "count": len(rows), "samples": rows}

    @app.get("/api/sos")
    async def get_sos() -> dict[str, Any]:
        """Current escalation state, so a reloaded page can restore the banner."""
        if safety is None:
            return {"enabled": False, "state": "idle", "remaining_s": 0.0}
        return {"enabled": True, **safety.sos.stats()}

    @app.post("/api/sos/cancel")
    async def cancel_sos(request: Request) -> dict[str, Any]:
        """Rider (or pillion) calls off a countdown.

        Returns 409 rather than 200 when the countdown has already expired.
        Reporting success for a cancel that did nothing would leave whoever
        pressed the button believing no one is on the way.
        """
        if safety is None:
            raise HTTPException(status_code=501, detail="safety layer is disabled")

        # Calling off an emergency is the most consequential thing this API
        # does, so who did it goes in the journal. An SOS that was cancelled
        # and cannot be accounted for afterwards is its own kind of incident.
        client = request.client.host if request.client else "unknown"
        agent = request.headers.get("user-agent", "")
        origin = f"dashboard at {client} ({agent[:80]})" if agent else f"dashboard at {client}"
        logger.warning("SOS cancel requested by %s", origin)

        if not safety.cancel_sos(origin):
            raise HTTPException(
                status_code=409,
                detail=f"cannot cancel while SOS is {safety.sos.state.value}",
            )
        return {"cancelled": True, **safety.sos.stats()}

    @app.websocket("/ws/telemetry")
    async def telemetry_socket(websocket: WebSocket) -> None:
        await websocket.accept()

        if not hub.register(websocket):
            await websocket.close(code=1013, reason="too many clients")
            return

        # Seed the client with current state so gauges are populated instantly
        # rather than staying blank until the next broadcast.
        latest = coordinator.latest
        if latest is not None:
            await websocket.send_json(latest.to_dict())

        try:
            while True:
                # The hub pushes; this read exists only to notice a disconnect
                # and to accept the occasional client ping.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            logger.debug("Telemetry socket closed unexpectedly", exc_info=True)
        finally:
            hub.unregister(websocket)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        sim = coordinator.simulator
        return {
            "status": "ok",
            "helmet_id": coordinator.config.node.helmet_id,
            "ride_id": coordinator.ride_id,
            "system_state": coordinator.system_state.value,
            "sensor_backend": coordinator.config.node.sensor_backend,
            "samples": len(coordinator.buffer),
            "route": sim.profile.route_name if sim is not None else "",
            "destination": sim.profile.destination_name if sim is not None else "",
        }

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app
