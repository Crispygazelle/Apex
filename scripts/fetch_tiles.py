"""Pre-fetch OpenStreetMap tiles for a demo route so the map never goes grey.

A helmet is offline by default and a venue's WiFi is not something to stake a
presentation on. This walks the bounding box of a GPX track and stores the
tiles the dashboard will ask for, which `app.dashboard` then serves locally.

    python -m scripts.fetch_tiles config/rides/nagpur-sitabuldi-airport.gpx

Provider matters. openstreetmap.org's own servers are volunteer-funded and
their usage policy forbids bulk downloading; asking them for a few hundred
tiles earns a repeated "Access blocked" image rather than a map, which is worse
than an error because it caches successfully. CARTO's public dark tiles now
watermark every square with "API key required", which also caches like success.
Esri's World Dark Gray canvas is the default: it is dark, labelled, and usable
for this kind of offline demo cache. Attribution is rendered by the dashboard.

Fetches are serial with a small delay, and anything already on disk is skipped,
so a second run costs almost nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.sensors.gpx import load_gpx

PROVIDERS = {
    # Dark labelled streets. Esri uses z/y/x, not the usual z/x/y.
    "esri-dark": (
        "https://server.arcgisonline.com/ArcGIS/rest/services/"
        "Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
    ),
    # These now watermark every tile unless you have a key. Kept so an
    # old command line does not silently switch provider.
    "carto-dark": "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
    "carto-light": "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    # Present for completeness. Expect to be blocked; see the module docstring.
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
}
DEFAULT_PROVIDER = "esri-dark"
USER_AGENT = "apex-helmet-telemetry/0.1 (offline demo tile cache for a student project)"
DEFAULT_CACHE = Path("config/tiles")
# z13 gives context, z16 is close enough to read street names while riding.
DEFAULT_ZOOMS = (13, 14, 15, 16)
REQUEST_DELAY_S = 0.12
# Past this many identical images in a row, the provider is serving a notice
# rather than a map and there is no point filling a cache with it.
IDENTICAL_TILE_LIMIT = 8


def deg_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Slippy-map tile covering a coordinate at one zoom level."""
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(x, n - 1), min(y, n - 1)


def tiles_for_bbox(
    south: float, west: float, north: float, east: float, zoom: int, margin: int = 1
) -> list[tuple[int, int, int]]:
    """Every tile touching a bounding box, plus a margin so panning is covered."""
    x0, y0 = deg_to_tile(north, west, zoom)
    x1, y1 = deg_to_tile(south, east, zoom)
    n = 2**zoom
    out = []
    for x in range(max(0, min(x0, x1) - margin), min(n - 1, max(x0, x1) + margin) + 1):
        for y in range(max(0, min(y0, y1) - margin), min(n - 1, max(y0, y1) + margin) + 1):
            out.append((zoom, x, y))
    return out


class BlockedProviderError(RuntimeError):
    """The provider is answering every request with the same image."""


def fetch_tile(
    zoom: int, x: int, y: int, cache: Path, template: str, *, force: bool = False
) -> tuple[str, str]:
    """Download one tile unless cached. Returns (outcome, content hash)."""
    target = cache / str(zoom) / str(x) / f"{y}.png"
    if not force and target.exists() and target.stat().st_size > 0:
        return "cached", ""

    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        template.format(z=zoom, x=x, y=y), headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  failed {zoom}/{x}/{y}: {exc}", file=sys.stderr)
        return "failed", ""

    if not payload:
        return "failed", ""
    # Write via a temporary name so an interrupted run cannot leave a truncated
    # tile that later looks cached.
    scratch = target.with_suffix(".part")
    scratch.write_bytes(payload)
    scratch.replace(target)
    time.sleep(REQUEST_DELAY_S)
    return "fetched", hashlib.md5(payload, usedforsecurity=False).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gpx", help="GPX track whose bounding box should be cached")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE), help="output directory")
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDERS),
        default=DEFAULT_PROVIDER,
        help="tile source (osm will refuse bulk requests)",
    )
    parser.add_argument(
        "--zoom",
        type=int,
        nargs="+",
        default=list(DEFAULT_ZOOMS),
        help="zoom levels to cache",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report the tile count and exit"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite tiles already on disk (needed after a watermarked cache)",
    )
    args = parser.parse_args(argv)

    points = load_gpx(args.gpx)
    lats = [lat for lat, _ in points]
    lons = [lon for _, lon in points]
    south, north = min(lats), max(lats)
    west, east = min(lons), max(lons)
    print(f"{Path(args.gpx).name}: {len(points)} points, bbox {south:.4f},{west:.4f} to {north:.4f},{east:.4f}")

    wanted: list[tuple[int, int, int]] = []
    for zoom in sorted(args.zoom):
        level = tiles_for_bbox(south, west, north, east, zoom)
        wanted.extend(level)
        print(f"  z{zoom}: {len(level)} tiles")
    print(f"total {len(wanted)} tiles (~{len(wanted) * 18 / 1024:.1f} MB at 18 KB each)")

    if args.dry_run:
        return 0

    cache = Path(args.cache)
    template = PROVIDERS[args.provider]
    tally = {"fetched": 0, "cached": 0, "failed": 0}
    hashes: set[str] = set()

    print(f"provider {args.provider}")
    for index, (zoom, x, y) in enumerate(wanted, start=1):
        outcome, digest = fetch_tile(zoom, x, y, cache, template, force=args.force)
        tally[outcome] += 1
        if digest:
            hashes.add(digest)

        # A provider that has decided to block us returns a perfectly valid PNG
        # saying so. Caching a few hundred copies of that notice would look like
        # success and fail only in front of an audience.
        if tally["fetched"] >= IDENTICAL_TILE_LIMIT and len(hashes) == 1:
            print(
                f"\nEvery tile from {args.provider} is byte-identical: it is "
                "serving a placeholder, not a map.\n"
                "Delete the cache directory and try --provider esri-dark.",
                file=sys.stderr,
            )
            return 2

        if index % 50 == 0 or index == len(wanted):
            print(f"  {index}/{len(wanted)} — {tally}")

    print(f"done: {tally}, {len(hashes)} distinct images")
    return 1 if tally["failed"] and tally["fetched"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
