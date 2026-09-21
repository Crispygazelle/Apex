"""Offline tile cache: the map must not need the internet during a demo."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.dashboard.app import create_app
from app.pipeline.coordinator import Coordinator
from app.streaming.websocket import TelemetryHub
from scripts.fetch_tiles import deg_to_tile, tiles_for_bbox

NAGPUR = (21.14631, 79.08491)

# A one-pixel PNG is enough: these tests are about routing and refusal, not
# image content.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


@pytest.fixture
def cached_client(tmp_path: Path, config: AppConfig) -> TestClient:
    tile = tmp_path / "13" / "5801" / "3617.png"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(PNG)
    config.dashboard.tile_cache_dir = str(tmp_path)
    coordinator = Coordinator(config, ride_id="ride-test")
    return TestClient(create_app(coordinator, TelemetryHub()))


def test_a_jpeg_tile_is_served_as_jpeg(tmp_path: Path, config: AppConfig) -> None:
    """Esri dark tiles are JPEG; claiming they are PNG makes the browser warn."""
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    tile = tmp_path / "13" / "5801" / "3617.png"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(jpeg)
    config.dashboard.tile_cache_dir = str(tmp_path)
    client = TestClient(create_app(Coordinator(config, ride_id="ride-test"), TelemetryHub()))
    response = client.get("/tiles/13/5801/3617.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")


def test_a_cached_tile_is_served(cached_client: TestClient) -> None:
    response = cached_client.get("/tiles/13/5801/3617.png")
    assert response.status_code == 200
    assert response.content == PNG
    assert "max-age" in response.headers["cache-control"]


def test_map_config_advertises_the_cache(cached_client: TestClient) -> None:
    body = cached_client.get("/api/map").json()
    assert body["tiles_cached"] is True
    assert body["tile_source"] == "cached"
    # Versioned: refilling the cache has to invalidate already-warm browsers,
    # or a stale provider's tiles survive the fix.
    assert body["tile_url"].startswith("/tiles/{z}/{x}/{y}.png?v=")
    assert body["tile_version"] > 0
    assert "OpenStreetMap" in body["attribution"]
    # The client probes this exact path, so it has to be a tile that exists.
    assert body["probe_url"].startswith("/tiles/13/5801/3617.png?v=")
    assert cached_client.get(body["probe_url"]).status_code == 200


def test_an_uncached_tile_is_404_not_a_proxy(cached_client: TestClient) -> None:
    """Cache-only on purpose: no demo-time reach for the network."""
    assert cached_client.get("/tiles/13/1/1.png").status_code == 404


def test_without_a_cache_the_client_is_told_to_use_osm(config: AppConfig) -> None:
    config.dashboard.tile_cache_dir = "does/not/exist"
    client = TestClient(create_app(Coordinator(config, ride_id="r"), TelemetryHub()))

    body = client.get("/api/map").json()
    assert body["tiles_cached"] is False
    assert body["tile_source"] == "remote"
    assert body["tile_url"] == ""
    assert client.get("/tiles/13/1/1.png").status_code == 404


def test_the_shipped_cache_covers_the_demo_route() -> None:
    """Guards the asset: a missing cache silently costs the map at the venue."""
    cache = Path("config/tiles")
    if not cache.is_dir():
        pytest.skip("tile cache not fetched in this checkout")

    zoom = 16
    x, y = deg_to_tile(*NAGPUR, zoom)
    assert (cache / str(zoom) / str(x) / f"{y}.png").is_file(), (
        "the ride's start point is not in the tile cache; re-run scripts.fetch_tiles"
    )


def test_esri_dark_template_uses_zyx_order() -> None:
    """Esri's REST tiles are z/y/x; slipping into z/x/y caches the wrong city."""
    from scripts.fetch_tiles import DEFAULT_PROVIDER, PROVIDERS

    assert DEFAULT_PROVIDER == "esri-dark"
    url = PROVIDERS["esri-dark"].format(z=16, x=47159, y=28830)
    assert url.endswith("/16/28830/47159")


def test_tile_maths_matches_the_slippy_map_formula() -> None:
    """Hand-checked against the standard Web Mercator tile equations.

    x = (lon + 180) / 360 * 2^z, so 79.08491 at z13 is 0.71968 * 8192 = 5895.
    y = (1 - asinh(tan(lat)) / pi) / 2 * 2^z, giving 0.43989 * 8192 = 3603.
    """
    assert deg_to_tile(*NAGPUR, 13) == (5895, 3603)
    # The origin sits on the corner of the four z1 tiles.
    assert deg_to_tile(0.0, 0.0, 1) == (1, 1)
    # Out-of-range latitudes clamp to the Mercator limit rather than exploding.
    assert deg_to_tile(89.9, 0.0, 2) == (2, 0)

    # A bbox includes a margin so panning does not run off the cache.
    tiles = tiles_for_bbox(21.09, 79.04, 21.15, 79.09, 13)
    assert (13, 5895, 3603) in tiles
