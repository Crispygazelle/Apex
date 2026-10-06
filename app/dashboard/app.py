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

from app.storage.hazard_log import HazardLog
from app.storage.influxdb import InfluxUnavailableError
from app.storage.ride_archive import RideArchive

if TYPE_CHECKING:
    from app.pipeline.coordinator import Coordinator
    from app.pipeline.events import EventLog
    from app.safety.monitor import SafetyMonitor
    from app.storage.batch_writer import BatchWriter
    from app.streaming.websocket import TelemetryHub
    from app.voice.assistant import VoiceAssistant

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
# Tiles never change, so let the browser keep them for the length of a demo.
TILE_CACHE_HEADER = "public, max-age=604800, immutable"


def create_app(
    coordinator: Coordinator,
    hub: TelemetryHub,
    batch_writer: BatchWriter | None = None,
    safety: SafetyMonitor | None = None,
    voice: VoiceAssistant | None = None,
    event_log: EventLog | None = None,
    ride_archive: RideArchive | None = None,
    hazard_log: HazardLog | None = None,
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

    tile_dir = Path(coordinator.config.dashboard.tile_cache_dir or "")
    sample_tile = next(iter(sorted(tile_dir.glob("*/*/*.png"))), None) if tile_dir.is_dir() else None
    tiles_available = sample_tile is not None
    # Tile paths are stable, so a browser that cached them once will keep them
    # even after the cache on disk is refilled from a different provider. The
    # token changes when the files do, which is the only thing that reliably
    # busts an already-warm browser cache.
    tile_version = int(sample_tile.stat().st_mtime) if sample_tile is not None else 0
    if tiles_available:
        logger.info("Serving offline map tiles from %s (v%d)", tile_dir, tile_version)

    @app.get("/tiles/{zoom}/{x}/{y}.png")
    async def tile(zoom: int, x: int, y: int) -> FileResponse:
        """One cached map tile.

        Strictly cache-only: a demo should not discover at the worst moment
        that the map needs the internet. Gaps are handled by the client, which
        upscales the deepest cached zoom rather than showing holes.
        """
        if not tiles_available:
            raise HTTPException(status_code=404, detail="no tile cache configured")
        # Ints from the path already rule out traversal, but resolve anyway so
        # the check is on the real path rather than on how it was spelled.
        candidate = (tile_dir / str(zoom) / str(x) / f"{y}.png").resolve()
        if tile_dir.resolve() not in candidate.parents or not candidate.is_file():
            raise HTTPException(status_code=404, detail="tile not cached")
        # Esri serves JPEG; we keep the .png path so the client URL stays
        # stable. The browser sniffs the magic bytes, but sending the real
        # type avoids a console warning on every tile.
        with candidate.open("rb") as handle:
            magic = handle.read(3)
        media = "image/jpeg" if magic == b"\xff\xd8\xff" else "image/png"
        return FileResponse(
            candidate,
            media_type=media,
            headers={"Cache-Control": TILE_CACHE_HEADER},
        )

    @app.get("/api/map")
    async def map_config() -> dict[str, Any]:
        """Tells the client whether to use local tiles or reach for OSM."""
        dashboard = coordinator.config.dashboard
        # A known-good tile path, so the client can test the layer without
        # guessing coordinates that may not be in the cache.
        probe = ""
        if sample_tile is not None:
            zoom, x = sample_tile.parent.parent.name, sample_tile.parent.name
            probe = f"/tiles/{zoom}/{x}/{sample_tile.stem}.png"
        return {
            "tiles_cached": tiles_available,
            # Cached is the demo default. Remote CARTO is only advertised when
            # this checkout has no tile cache at all; the client must not reach
            # the network while local tiles exist.
            "tile_source": "cached" if tiles_available else "remote",
            "tile_url": (
                f"/tiles/{{z}}/{{x}}/{{y}}.png?v={tile_version}" if tiles_available else ""
            ),
            "probe_url": f"{probe}?v={tile_version}" if probe else "",
            "tile_version": tile_version,
            "attribution": "© Esri · © OpenStreetMap contributors",
            "min_zoom": dashboard.tile_min_zoom,
            "max_zoom": dashboard.tile_max_zoom,
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

    # --- demo director ----------------------------------------------------
    #
    # Physics injections reach into the simulator, so they only exist on a
    # simulated ride. A replay is a fixed record of readings and a live helmet
    # is a motorcycle; fabricating a pothole in either would be a lie about
    # where the number came from. Speaking to the assistant works everywhere,
    # because that is a real command down the real intent path.

    def _require_simulator() -> Any:
        simulator = coordinator.simulator
        if simulator is None:
            raise HTTPException(
                status_code=409,
                detail="event injection needs a simulated ride; this node is "
                f"running {'a replay' if coordinator.recording else 'real sensors'}",
            )
        return simulator

    @app.post("/api/demo/pothole")
    async def demo_pothole() -> dict[str, Any]:
        """Drop a pothole just ahead, so the g-spike lands while you narrate."""
        at_m = _require_simulator().inject_pothole()
        logger.info("Demo: pothole injected at %.0f m", at_m)
        return {"injected": "pothole", "at_m": round(at_m, 1)}

    @app.post("/api/demo/brake")
    async def demo_brake(
        target_kmh: float = Query(default=10.0, ge=0.0, le=80.0),
        duration_s: float = Query(default=3.0, gt=0.0, le=15.0),
    ) -> dict[str, Any]:
        """Demand a hard stop now, for the red trail and the brake marker."""
        try:
            _require_simulator().inject_brake(
                target_mps=target_kmh / 3.6, duration_s=duration_s
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        logger.info("Demo: braking to %.0f km/h for %.1fs", target_kmh, duration_s)
        return {"injected": "brake", "target_kmh": target_kmh, "duration_s": duration_s}

    @app.post("/api/demo/crash")
    async def demo_crash() -> dict[str, Any]:
        """Rehearse the impact and the SOS countdown. Cancellable as usual."""
        at_s = _require_simulator().inject_crash()
        logger.warning("Demo: crash rehearsal triggered at t+%.1fs", at_s)
        return {"injected": "crash", "at_s": round(at_s, 1)}

    @app.post("/api/demo/say")
    async def demo_say(text: str = Query(min_length=1, max_length=200)) -> dict[str, Any]:
        """Put a phrase through wake word, intent parsing and the real handler."""
        if voice is None:
            raise HTTPException(status_code=501, detail="the voice layer is disabled")
        reply = await voice.hear(text)
        return {"rider": text, "apex": reply}

    @app.get("/api/demo")
    async def demo_capabilities() -> dict[str, Any]:
        """What the director panel should offer on this particular node."""
        simulator = coordinator.simulator
        return {
            "can_inject": simulator is not None,
            "can_brake": simulator is not None and simulator.supports_speed_injection,
            "can_speak": voice is not None,
            "phrases": [
                "hey apex what's my speed",
                "hey apex how far am I to the destination",
                "hey apex log a pothole here",
                "hey apex status report",
            ],
        }

    archive_dir = ride_archive.directory if ride_archive is not None else None

    @app.get("/api/rides")
    async def list_rides() -> dict[str, Any]:
        """Saved rides on this computer, newest first."""
        if archive_dir is None:
            return {"rides": []}
        return {"rides": RideArchive.list_rides(archive_dir)}

    @app.get("/api/rides/{ride_id}")
    async def get_saved_ride(ride_id: str) -> dict[str, Any]:
        """One saved ride: its summary and the thinned track."""
        if archive_dir is None:
            raise HTTPException(status_code=404, detail="no ride archive")
        try:
            payload = RideArchive.load_ride(archive_dir, ride_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="ride not found") from exc
        if payload is None:
            raise HTTPException(status_code=404, detail="ride not found")
        return payload

    @app.get("/api/hazards")
    async def list_hazards(ride_id: str | None = None) -> dict[str, Any]:
        """Logged hazards. Omit ride_id for every ride on this computer."""
        if hazard_log is None:
            return {"hazards": []}
        return {"hazards": hazard_log.all(ride_id)}

    @app.post("/api/destination")
    async def set_destination(
        latitude: float = Query(ge=-90, le=90),
        longitude: float = Query(ge=-180, le=180),
    ) -> dict[str, Any]:
        """Drop a pin. Remaining becomes the straight-line distance to it."""
        processor = coordinator.processor
        processor.destination = (latitude, longitude)
        processor.route_length_m = 0.0
        processor.destination_kind = "straight"
        return {"destination_kind": "straight", "latitude": latitude, "longitude": longitude}

    @app.post("/api/destination/clear")
    async def clear_destination() -> dict[str, Any]:
        """Ride with no finish line. The remaining card goes blank."""
        processor = coordinator.processor
        processor.destination = None
        processor.route_length_m = 0.0
        processor.destination_kind = "none"
        return {"destination_kind": "none"}

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        route = coordinator.sensor_set.route
        recording = coordinator.recording
        backend = coordinator.config.node.sensor_backend
        return {
            "status": "ok",
            "helmet_id": coordinator.config.node.helmet_id,
            "ride_id": coordinator.ride_id,
            "system_state": coordinator.system_state.value,
            # A replay is neither the simulator nor the hardware, and calling it
            # either would misrepresent where the numbers came from.
            "sensor_backend": "replay" if recording is not None else backend,
            "replay_speed": recording.speed if recording is not None else 0.0,
            "samples": len(coordinator.buffer),
            "route": route.route_name if route is not None else "",
            "destination": route.destination_name if route is not None else "",
            "tile_source": "cached" if tiles_available else "none",
            "voice": voice is not None,
        }

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app
