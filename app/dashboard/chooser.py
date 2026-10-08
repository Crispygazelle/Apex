"""The screen shown before a ride starts, when nobody passed --backend.

The Pi service passes --backend hardware and never comes here. This page only
exists so a person at the laptop or the bench can choose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from app.sensors.probe import HARDWARE_REQUIRED

STATIC_DIR = Path(__file__).parent / "static"
CHOOSER_PAGE = STATIC_DIR / "choose.html"


def create_chooser(
    probe: dict[str, Any],
    on_choose: Callable[[str], None],
) -> FastAPI:
    """Serve the gate. `on_choose` runs once, with "sim" or "hardware"."""
    app = FastAPI(title="APEX", docs_url=None, openapi_url=None)
    chosen = False

    @app.get("/api/capabilities")
    async def capabilities() -> dict[str, Any]:
        return {"hardware": bool(probe["available"]), "reason": probe["reason"]}

    @app.post("/api/session")
    async def session(body: dict[str, Any]) -> JSONResponse:
        nonlocal chosen
        mode = str(body.get("mode", ""))
        if mode not in {"sim", "hardware"}:
            return JSONResponse({"detail": "choose sim or hardware"}, status_code=422)
        if mode == "hardware" and not probe["available"]:
            return JSONResponse(
                {"detail": HARDWARE_REQUIRED, "follow": "sim"},
                status_code=409,
            )
        if chosen:
            return JSONResponse({"detail": "a ride has already been chosen"}, status_code=409)
        chosen = True
        on_choose(mode)
        return JSONResponse({"mode": mode})

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            CHOOSER_PAGE,
            headers={"Cache-Control": "no-store"},
        )

    return app
