#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "rasterio",
#   "numpy",
#   "pillow",
# ]
# ///
"""Fetch a slippy-map background for an extent and write it as a GeoTIFF.

A CAD drawing of bare linework gives a reviewer nothing to orient by. This
pulls the map tiles covering the extent, mosaics them, reprojects them into
the drawing's UTM CRS and hands the result to `underlay.py`, which places it
at true scale beneath everything else:

    uv run scripts/basemap.py --bbox 15.830,104.390,15.838,104.399 \\
        --epsg 32648 --out output/basemap.tif
    uv run scripts/osm2cad.py --input map.osm --basemap osm --out site.dxf
    uv run scripts/topo2cad.py --lat 15.8338 --lon 104.3945 \\
        --dem dem/dem_n15_e104.tif --basemap esri-imagery --out site.dxf

It is a **backdrop, not survey data**. Nothing is traced from it and nothing
is staged from it: a re-issue through `db2dxf.py` draws the linework alone,
exactly as `--underlay` already behaves. Freeze the layer before plotting a
submission sheet if the reviewing agency wants linework only — the
attribution text sits on that same layer, so it disappears with the map it
credits rather than crediting a map that is no longer shown.

**Tile servers are somebody else's infrastructure.** OpenStreetMap's usage
policy forbids bulk downloading, so every tile is cached on disk and reused,
the fetch is sequential with a real User-Agent, and the tile count is capped
(`--max-tiles`, default 128) with the zoom stepped down until the extent
fits. A 1,000 x 750 m site takes about 42 tiles at zoom 18. Do not point
this at a province.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

# Web Mercator (EPSG:3857) half-circumference in metres: the x/y extreme of
# the tile grid, and the number every tile calculation is scaled from.
MERCATOR_ORIGIN = 20037508.342789244
TILE_PX = 256
WEB_MERCATOR_EPSG = 3857

# Providers this can fetch. `url` is a template — Esri orders the path
# {z}/{y}/{x}, which is why the placeholders are named rather than
# positional. `attribution` is drawn onto the drawing, not just printed:
# a map with the credit stripped off is a licence breach, and the one place
# it cannot be forgotten is the drawing itself.
PROVIDERS = {
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "Background map © OpenStreetMap contributors (ODbL)",
        "max_zoom": 19,
    },
    # Contour lines and hillshade over OSM data — the closest thing to a
    # topographic sheet without a DEM, and the one to reach for when the
    # drawing has no contours of its own (osm2cad.py never has any).
    # It renders to zoom 17 and serves nothing above it.
    "opentopomap": {
        "url": "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution": "Background map © OpenStreetMap contributors, SRTM · "
                       "style © OpenTopoMap (CC-BY-SA)",
        "max_zoom": 17,
    },
    "esri-topo": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Topographic map © Esri, USGS, NGA, NASA, CGIAR, "
                       "OpenStreetMap contributors",
        "max_zoom": 19,
    },
    "esri-imagery": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Imagery © Esri, Maxar, Earthstar Geographics",
        "max_zoom": 19,
    },
    "esri-street": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Street map © Esri, HERE, Garmin, "
                       "OpenStreetMap contributors",
        "max_zoom": 19,
    },
    # Muted styles: a backdrop is meant to sit *under* linework, and the
    # standard OSM palette competes with it on a plotted sheet.
    "carto-light": {
        "url": "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "attribution": "Background map © OpenStreetMap contributors, © CARTO",
        "max_zoom": 19,
    },
    "carto-dark": {
        "url": "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "attribution": "Background map © OpenStreetMap contributors, © CARTO",
        "max_zoom": 19,
    },
    "carto-voyager": {
        "url": "https://basemaps.cartocdn.com/rastertiles/voyager/"
               "{z}/{x}/{y}.png",
        "attribution": "Background map © OpenStreetMap contributors, © CARTO",
        "max_zoom": 19,
    },
    # Humanitarian style: heavier on tracks, water points and buildings,
    # which is what a rural Thai site actually has.
    "osm-hot": {
        "url": "https://a.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png",
        "attribution": "Background map © OpenStreetMap contributors · "
                       "style © Humanitarian OSM Team",
        "max_zoom": 19,
    },
    "cyclosm": {
        "url": "https://a.tile-cyclosm.openstreetmap.fr/cyclosm/"
               "{z}/{x}/{y}.png",
        "attribution": "Background map © OpenStreetMap contributors · "
                       "style © CyclOSM",
        "max_zoom": 19,
    },
}

DEFAULT_PROVIDER = "osm"
# One cache for every run, beside the osmnx one and gitignored with it. A
# per-run folder would re-fetch the same tiles for every re-plot of the same
# site, which is exactly the behaviour a tile usage policy asks you not to
# have.
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "tiles"
DEFAULT_MAX_TILES = 128
# Faded hard: this is a backdrop for linework, not something to read.
DEFAULT_FADE = 65
HEADERS = {"User-Agent": "maps2cad/1.0 (personal CAD export script)"}


class BasemapError(Exception):
    """The background map cannot be built — with the reason, not a stack."""


def resolve_provider(name):
    """Provider spec from a name, or from a raw {z}/{x}/{y} URL template.

    A custom template is how a WMTS endpoint or an agency's own tile service
    gets used; it carries no attribution this can know, so the caller is
    told to add one.
    """
    if name in PROVIDERS:
        return dict(PROVIDERS[name], name=name)
    if "{z}" in str(name) and "{x}" in str(name) and "{y}" in str(name):
        return {"name": "custom", "url": str(name), "max_zoom": 22,
                "attribution": "Background map © its provider — replace this "
                               "credit with the service's own wording"}
    raise BasemapError(
        f"Unknown basemap '{name}'. Choose one of "
        f"{', '.join(sorted(PROVIDERS))}, or give a tile URL template "
        "containing {z}, {x} and {y}.")


# Web Mercator stops here: the projection runs to infinity at the poles, and
# the tile grid is square, so every implementation clamps to this latitude.
MERCATOR_MAX_LAT = 85.05112877980659


def deg2tile(lat, lon, zoom):
    """Slippy-map tile containing a coordinate. Standard OSM formula."""
    n = 2 ** zoom
    # Clamped before the formula, not after: at ±90° the log term divides by
    # zero, which is a crash rather than a wrong answer.
    lat = min(max(lat, -MERCATOR_MAX_LAT), MERCATOR_MAX_LAT)
    x = int((lon + 180.0) / 360.0 * n)
    sin_lat = math.sin(math.radians(lat))
    y = int((0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * n)
    # A coordinate exactly on the antimeridian or past the Mercator limit
    # would index one tile off the end of the grid.
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_range(box, zoom):
    """(x0, y0, x1, y1) inclusive tile range covering an S,W,N,E box.

    y counts down from the north, so the northern edge gives the smaller y.
    """
    s, w, n, e = box
    x0, y0 = deg2tile(n, w, zoom)
    x1, y1 = deg2tile(s, e, zoom)
    return x0, y0, max(x1, x0), max(y1, y0)


def tile_count(box, zoom):
    x0, y0, x1, y1 = tile_range(box, zoom)
    return (x1 - x0 + 1) * (y1 - y0 + 1)


def choose_zoom(box, max_tiles=DEFAULT_MAX_TILES, max_zoom=19, min_zoom=10):
    """Sharpest zoom whose tile count stays inside the cap.

    Stepping down rather than clipping the extent: a backdrop that covers
    half the drawing is worse than one a level coarser, and the alternative
    — fetching hundreds of tiles — is exactly what the tile usage policy
    asks nobody to do.
    """
    for zoom in range(max_zoom, min_zoom - 1, -1):
        if tile_count(box, zoom) <= max_tiles:
            return zoom
    return min_zoom


def tile_url(template, x, y, zoom):
    return template.format(z=zoom, x=x, y=y)


def tile_size_m(zoom):
    """Ground size of one tile in Web Mercator metres."""
    return 2 * MERCATOR_ORIGIN / (2 ** zoom)


def mosaic_origin(x0, y0, zoom):
    """(west, north, pixel size) of a mosaic whose top-left tile is (x0, y0).

    Kept free of rasterio so the tile arithmetic — the part that silently
    misplaces a whole basemap when it is wrong — can be tested without GDAL
    installed.
    """
    size = tile_size_m(zoom)
    return (-MERCATOR_ORIGIN + x0 * size, MERCATOR_ORIGIN - y0 * size,
            size / TILE_PX)


def mosaic_transform(x0, y0, zoom):
    """Affine transform of a tile mosaic whose top-left tile is (x0, y0)."""
    from rasterio.transform import from_origin

    west, north, res = mosaic_origin(x0, y0, zoom)
    return from_origin(west, north, res, res)


def cache_path(cache_dir, provider_name, zoom, x, y):
    return Path(cache_dir) / provider_name / str(zoom) / str(x) / f"{y}.png"


def decode_tile(data):
    """PNG/JPEG bytes -> an (h, w, 3) uint8 array, or None if unreadable.

    Tiles arrive as RGB, RGBA or paletted depending on the provider, and a
    502 page arrives as HTML. Everything is normalised to RGB here so the
    mosaic has one dtype and one band count.
    """
    import io

    import numpy as np
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as img:
            return np.asarray(img.convert("RGB"), dtype="uint8")
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def mosaic(tiles, x0, y0, nx, ny):
    """Stitch {(x, y): array} into one (3, H, W) array.

    A tile that failed or was never fetched stays white rather than black:
    a gap in the backdrop should read as blank paper, not as a hole burned
    into the drawing.
    """
    import numpy as np

    canvas = np.full((ny * TILE_PX, nx * TILE_PX, 3), 255, dtype="uint8")
    for (x, y), arr in tiles.items():
        if arr is None:
            continue
        row, col = (y - y0) * TILE_PX, (x - x0) * TILE_PX
        h, w = arr.shape[0], arr.shape[1]
        canvas[row:row + h, col:col + w] = arr[:TILE_PX, :TILE_PX]
    return np.transpose(canvas, (2, 0, 1))


def fetch_tiles(provider, box, zoom, cache_dir, session=None, pause=0.0):
    """Download every tile covering the box, reusing the disk cache.

    Sequential on purpose. The cache is the part that matters: a second run
    over the same site — a re-plot, a different sheet size, a corrected name
    — costs no requests at all.
    """
    import requests

    x0, y0, x1, y1 = tile_range(box, zoom)
    session = session or requests.Session()
    tiles, fetched, cached, failed = {}, 0, 0, 0
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            path = cache_path(cache_dir, provider["name"], zoom, x, y)
            if path.is_file():
                tiles[(x, y)] = decode_tile(path.read_bytes())
                cached += 1
                continue
            try:
                r = session.get(tile_url(provider["url"], x, y, zoom),
                                headers=HEADERS, timeout=30)
                r.raise_for_status()
                arr = decode_tile(r.content)
            except Exception:
                arr = None
            if arr is None:
                failed += 1
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            tiles[(x, y)] = arr
            fetched += 1
            if pause:
                time.sleep(pause)
    return tiles, {"fetched": fetched, "cached": cached, "failed": failed,
                   "range": (x0, y0, x1, y1)}


def build(box, target_epsg, out_path, provider=DEFAULT_PROVIDER, zoom=None,
          max_tiles=DEFAULT_MAX_TILES, cache_dir=None, session=None):
    """Fetch, mosaic and reproject a backdrop for `box` into `out_path`.

    The GeoTIFF comes out in the drawing's own CRS, because `underlay.py`
    refuses anything else — placing a Web Mercator image by its corners in a
    UTM drawing would stretch it by metres through the middle, and it would
    still look like a map while doing so.
    """
    spec = resolve_provider(provider)
    cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
    zoom = int(zoom) if zoom else choose_zoom(
        box, max_tiles, spec.get("max_zoom", 19))
    count = tile_count(box, zoom)
    if count > max_tiles:
        raise BasemapError(
            f"{count} tiles at zoom {zoom} is over the {max_tiles}-tile cap. "
            "Lower --basemap-zoom, raise --basemap-max-tiles, or draw a "
            "smaller extent — tile servers are shared infrastructure.")

    tiles, stats = fetch_tiles(spec, box, zoom, cache_dir, session)
    if not tiles:
        raise BasemapError(
            f"No tiles could be fetched from {spec['name']}. Check the "
            "network, or run without a background map.")
    # GDAL is only needed once there is something to write, so the tile
    # budget and a dead provider are reported without loading it.
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    x0, y0, x1, y1 = stats["range"]
    data = mosaic(tiles, x0, y0, x1 - x0 + 1, y1 - y0 + 1)
    src_transform = mosaic_transform(x0, y0, zoom)
    src_crs = rasterio.CRS.from_epsg(WEB_MERCATOR_EPSG)
    dst_crs = rasterio.CRS.from_epsg(target_epsg)

    height, width = data.shape[1], data.shape[2]
    left, top = src_transform * (0, 0)
    right, bottom = src_transform * (width, height)
    dst_transform, dst_w, dst_h = calculate_default_transform(
        src_crs, dst_crs, width, height, left, bottom, right, top)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", driver="GTiff", width=dst_w,
                       height=dst_h, count=3, dtype="uint8", crs=dst_crs,
                       transform=dst_transform, compress="deflate") as dst:
        for band in range(3):
            reproject(
                source=data[band], destination=rasterio.band(dst, band + 1),
                src_transform=src_transform, src_crs=src_crs,
                dst_transform=dst_transform, dst_crs=dst_crs,
                # Bilinear on a rendered map keeps labels legible; nearest
                # leaves them jagged once the grid is rotated into UTM.
                resampling=Resampling.bilinear,
                src_nodata=None, dst_nodata=None)
    # Blankness is judged on the mosaic, not the written file: a dataset
    # opened for writing is not readable, and every tile having failed is
    # exactly the case worth reporting.
    return {"path": out_path, "provider": spec["name"],
            "attribution": spec["attribution"], "zoom": zoom,
            "tiles": len(tiles), "pixels": (int(dst_w), int(dst_h)),
            "res_m": abs(dst_transform.a),
            "blank": bool(np.all(data == 255)), **stats}


# Its own layer, frozen in one click before a linework-only plot. Kept off
# C-SITE-ORTH, which underlay.py uses for imagery the user owns and traces
# from: a fetched backdrop is reference, not evidence, and a drafter should
# be able to drop one without dropping the other.
LAYER = "C-ANNO-BMAP"


def attach(doc, msp, box, target_epsg, dxf_path, provider=DEFAULT_PROVIDER,
           zoom=None, max_tiles=DEFAULT_MAX_TILES, fade=DEFAULT_FADE,
           cache_dir=None):
    """Build the backdrop beside the drawing and place it beneath everything.

    Written as `basemap.tif` next to the .dxf because the DXF stores a path,
    not the pixels — the pair has to travel together, and a run folder is
    what gets zipped and sent.
    """
    import underlay as ul

    out = Path(dxf_path).with_name("basemap.tif")
    info = build(box, target_epsg, out, provider=provider, zoom=zoom,
                 max_tiles=max_tiles, cache_dir=cache_dir)
    ul.attach(doc, msp, out, target_epsg, dxf_path=dxf_path, layer=LAYER,
              fade=fade)
    return info


def describe(info) -> str:
    return (f"Basemap: {info['provider']} at zoom {info['zoom']} — "
            f"{info['tiles']} tiles ({info['fetched']} fetched, "
            f"{info['cached']} cached"
            + (f", {info['failed']} failed" if info["failed"] else "")
            + f"), {info['res_m']:.2f} m/px -> {info['path'].name}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", required=True, metavar="S,W,N,E")
    ap.add_argument("--epsg", type=int, required=True,
                    help="CRS of the drawing the GeoTIFF is for")
    ap.add_argument("--out", required=True)
    ap.add_argument("--provider", default=DEFAULT_PROVIDER,
                    help=f"{', '.join(sorted(PROVIDERS))}, or a tile URL "
                         "template with {z}/{x}/{y}")
    ap.add_argument("--zoom", type=int, help="default: the sharpest zoom "
                                             "inside the tile cap")
    ap.add_argument("--max-tiles", type=int, default=DEFAULT_MAX_TILES)
    ap.add_argument("--cache-dir",
                    help="default: cache/tiles/ in the repo, shared by every "
                         "run so a re-plot costs no requests")
    a = ap.parse_args(argv)
    try:
        box = tuple(float(v) for v in a.bbox.split(","))
        if len(box) != 4:
            raise ValueError
    except ValueError:
        print("ERROR: --bbox wants four numbers: S,W,N,E", file=sys.stderr)
        return 1
    try:
        info = build(box, a.epsg, a.out, provider=a.provider, zoom=a.zoom,
                     max_tiles=a.max_tiles, cache_dir=a.cache_dir)
    except BasemapError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(describe(info))
    print(info["attribution"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
