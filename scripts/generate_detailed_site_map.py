#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "osmnx==2.1.1",
#   "geopandas>=1.0",
#   "matplotlib>=3.9",
#   "shapely>=2.0",
#   "pyproj>=3.6",
#   # --basemap only. This stack is otherwise disjoint from the CAD side's
#   # raster libraries, and a tile backdrop is the one thing that needs
#   # them: the tiles arrive in Web Mercator and have to be reprojected,
#   # not corner-stretched, or the map is wrong by metres in the middle.
#   "rasterio",
#   "pillow",
# ]
# ///
"""Detailed Site Map Generator.

Generates a print-ready site map (vector PDF, optional 300 DPI PNG) from a
WGS 84 coordinate, using OpenStreetMap data: roads, buildings, water and
points of interest. Exports a building inventory CSV and supports verified
manual name overrides. Includes a Thai Government Submission layout profile
(--profile government).

Data (c) OpenStreetMap contributors.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------------
# Colour palette (spec section 8.2)
# ----------------------------------------------------------------------------
COLOURS = {
    "road_major": "#C45A00",
    "road_main": "#E07A00",
    "road_local": "#8A8F98",
    "road_minor": "#B4B8BF",
    "building_fill": "#DCEFF2",
    "building_edge": "#008C99",
    "water_fill": "#D7EBFF",
    "water_edge": "#1D5DB8",
    "site_marker": "#D90429",
    "text_primary": "#102A43",
    "text_secondary": "#486174",
}

# Restrained, low-saturation style for the government profile (spec 13.2)
GOV_COLOURS = {
    "road_major": "#1A1A1A",
    "road_main": "#3D3D3D",
    "road_local": "#6E6E6E",
    "road_minor": "#9A9A9A",
    "building_fill": "#E9ECEF",
    "building_edge": "#4A4F55",
    "water_fill": "#DDE7F0",
    "water_edge": "#5B7A99",
    "site_marker": "#B00020",
    "text_primary": "#000000",
    "text_secondary": "#333333",
}

ROAD_CLASSES = {
    # class -> (highway values, linewidth, zorder)
    "major": ({"motorway", "motorway_link", "trunk", "trunk_link",
               "primary", "primary_link"}, 2.8, 6),
    "main": ({"secondary", "secondary_link", "tertiary", "tertiary_link"},
             2.0, 5),
    "local": ({"residential", "unclassified", "service", "living_street"},
              1.2, 4),
    # everything else (path, footway, track, cycleway, steps, ...)
    "minor": (set(), 0.7, 3),
}

FEATURE_TAGS = {
    "building": True,
    "highway": True,
    "amenity": True,
    "shop": True,
    "office": True,
    "tourism": True,
    "leisure": True,
    "natural": "water",
    "waterway": True,
}

INVENTORY_FIELDS = ["feature_id", "code", "osm_name", "display_name",
                    "building_type", "latitude", "longitude"]

THAI_FONT_CANDIDATES = ["Thonburi", "Noto Sans Thai", "Sukhumvit Set",
                        "Tahoma", "Arial Unicode MS", "Leelawadee UI"]


class SiteMapError(Exception):
    """User-facing error with a clear message (spec NFR-06)."""


@dataclass
class BuildingRecord:
    feature_id: str
    code: str
    osm_name: str
    display_name: str
    building_type: str
    latitude: float
    longitude: float
    geometry: object = field(repr=False, default=None)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate a detailed, print-ready site map from a "
                    "WGS 84 coordinate using OpenStreetMap data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--lat", type=float, required=True,
                   help="Latitude in decimal degrees (WGS 84, -90..90)")
    p.add_argument("--lon", type=float, required=True,
                   help="Longitude in decimal degrees (WGS 84, -180..180)")
    # 1000 x 750 m: wider than A3 holds at 1:2000, so the standard profile
    # reports the scale it actually achieved rather than a nominal one
    p.add_argument("--width", type=float, default=1000.0,
                   help="East-west coverage in metres, centred on the site")
    p.add_argument("--height", type=float, default=750.0,
                   help="North-south coverage in metres, centred on the site")
    p.add_argument("--output",
                   help="Output PDF path (must end in .pdf). Not needed when "
                        "--outdir is given.")
    p.add_argument("--outdir",
                   help="Group this run in its own folder under DIR: creates "
                        "DIR/<lat>_<lon>_<WxH>_<timestamp>/ holding "
                        "site_map.pdf, site_map.png and building_inventory.csv")
    p.add_argument("--png", nargs="?", const="__auto__", default=None,
                   help="Write a 300 DPI PNG preview. Bare --png names it "
                        "after the PDF (or site_map.png inside --outdir); "
                        "pass a path to choose the name.")
    p.add_argument("--inventory", default="building_inventory.csv",
                   help="Building-inventory CSV output path")
    p.add_argument("--labels-csv", default=None,
                   help="CSV of verified building-name overrides "
                        "(matched by feature_id)")
    p.add_argument("--font", default=None,
                   help="Optional TTF/OTF font path (e.g. Thai-capable font)")
    p.add_argument("--no-building-codes", action="store_true",
                   help="Do not display B### codes on unnamed buildings")
    p.add_argument("--title", default="Detailed Site Map",
                   help="Map title (standard profile)")
    p.add_argument("--arrows", action="store_true",
                   help="Draw direction arrows on one-way carriageways. "
                        "Standard profile only — the government submission "
                        "sheet renders what its spec lists, and this is not "
                        "on it.")
    p.add_argument("--basemap", nargs="?", const="osm", metavar="PROVIDER",
                   help="Draw a map backdrop under the linework: any of "
                        "basemap.py's providers, or a {z}/{x}/{y} template. "
                        "Standard profile only, for the same reason.")
    p.add_argument("--basemap-alpha", type=float, default=0.30, metavar="A",
                   help="How strongly the backdrop shows through (default "
                        "0.30 — context under the drawing, not the drawing)")
    p.add_argument("--profile", choices=["standard", "government"],
                   default="standard",
                   help="Layout profile: standard, or Thai Government "
                        "Submission layout")
    # Government-profile title-block fields (spec 13.3)
    g = p.add_argument_group("government profile title block")
    g.add_argument("--project-name", default="")
    g.add_argument("--site-location", default="")
    g.add_argument("--subdistrict", default="", help="Tambon / khwaeng")
    g.add_argument("--district", default="", help="Amphoe / khet")
    g.add_argument("--province", default="")
    g.add_argument("--agency", default="", help="Project owner / agency")
    g.add_argument("--prepared-by", default="")
    g.add_argument("--checked-by", default="")
    g.add_argument("--approved-by", default="")
    g.add_argument("--drawing-no", default="")
    g.add_argument("--sheet", default="1/1", help="Sheet number / total")
    g.add_argument("--revision", default="0")
    p.add_argument("--sheet-size", choices=["A4", "A3", "A2", "A1"],
                   default="A3",
                   help="Sheet size. The standard profile switches to "
                        "portrait when the extent is taller than it is "
                        "wide; the government profile stays landscape.")
    g.add_argument("--final", action="store_true",
                   help="Omit the DRAFT / FOR REVIEW marking "
                        "(default output is marked as a draft)")
    return p.parse_args(argv)


def apply_outdir(a: argparse.Namespace) -> Path | None:
    """Give this run its own folder so repeat generations never overwrite
    each other. Returns the run directory, or None when not grouping."""
    if not a.outdir:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = (f"{a.lat:.6f}_{a.lon:.6f}_{a.width:.0f}x{a.height:.0f}"
            f"_{a.profile}_{stamp}")
    run = Path(a.outdir) / slug
    a.output = str(run / "site_map.pdf")
    # Everything in a run folder gets a predictable name
    if a.png is not None:
        a.png = str(run / "site_map.png")
    a.inventory = str(run / "building_inventory.csv")
    return run


def validate_args(a: argparse.Namespace) -> None:
    if not a.output and not a.outdir:
        raise SiteMapError("Give either --output <file.pdf> or --outdir <dir>.")
    if a.png == "__auto__":       # bare --png: name it after the PDF
        a.png = str(Path(a.output).with_suffix(".png"))
    if not (-90.0 <= a.lat <= 90.0):
        raise SiteMapError(f"Invalid latitude {a.lat}: must be a WGS 84 "
                           "latitude between -90 and 90 decimal degrees.")
    if not (-180.0 <= a.lon <= 180.0):
        raise SiteMapError(f"Invalid longitude {a.lon}: must be a WGS 84 "
                           "longitude between -180 and 180 decimal degrees.")
    if not (math.isfinite(a.width) and math.isfinite(a.height)
            and a.width > 0 and a.height > 0):
        raise SiteMapError("Map width and height must be positive, finite "
                           "metres.")
    if not a.output.lower().endswith(".pdf"):
        raise SiteMapError(f"Output path '{a.output}' must end in .pdf")
    if a.font and not Path(a.font).is_file():
        raise SiteMapError(f"Font file not found: {a.font}")
    if a.labels_csv and not Path(a.labels_csv).is_file():
        raise SiteMapError(f"Labels CSV not found: {a.labels_csv}")
    # Refused rather than ignored. The government layout implements a
    # written spec; a sheet that quietly carried an extra layer because a
    # flag was left on from an earlier run is exactly the sort of thing a
    # reviewing officer is entitled to reject.
    if a.profile == "government" and (a.arrows or a.basemap):
        raise SiteMapError(
            "--arrows and --basemap are standard-profile options. The "
            "government submission sheet renders what its spec lists, and "
            "neither is on it. Drop the flag, or use --profile standard.")


def ensure_parent_dir(path: str) -> None:
    parent = Path(path).resolve().parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SiteMapError(f"Cannot create output directory '{parent}': {e}")
    import os
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise SiteMapError(f"Output directory '{parent}' is not writable.")


# ----------------------------------------------------------------------------
# Coordinate processing (spec 6.3)
# ----------------------------------------------------------------------------
def utm_epsg_for(lat: float, lon: float) -> int:
    zone = int((lon + 180.0) // 6) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def build_extent(lat: float, lon: float, width: float, height: float):
    """Return (epsg, utm_crs, to_wgs transformer, site_xy, rect polygon in
    UTM, rect polygon in EPSG:4326)."""
    from pyproj import CRS, Transformer
    from shapely.geometry import box
    from shapely.ops import transform as shp_transform

    epsg = utm_epsg_for(lat, lon)
    utm = CRS.from_epsg(epsg)
    to_utm = Transformer.from_crs("EPSG:4326", utm, always_xy=True)
    to_wgs = Transformer.from_crs(utm, "EPSG:4326", always_xy=True)

    x, y = to_utm.transform(lon, lat)
    rect = box(x - width / 2, y - height / 2, x + width / 2, y + height / 2)
    rect_wgs = shp_transform(to_wgs.transform, rect)
    return epsg, utm, to_wgs, (x, y), rect, rect_wgs


# ----------------------------------------------------------------------------
# OpenStreetMap retrieval (spec 6.1, 6.2)
# ----------------------------------------------------------------------------
def fetch_features(rect_wgs):
    import osmnx as ox
    ox.settings.use_cache = True
    ox.settings.log_console = False

    try:
        gdf = ox.features_from_polygon(rect_wgs, FEATURE_TAGS)
    except Exception as e:  # noqa: BLE001 - classify below
        import requests

        name = type(e).__name__
        if name == "InsufficientResponseError":
            raise SiteMapError(
                "OpenStreetMap returned no features for this area. "
                "Verify the coordinate and try a larger width/height.")
        if isinstance(e, (requests.exceptions.RequestException, OSError,
                          TimeoutError)) or name in (
                "ResponseStatusCodeError", "URLError", "gaierror"):
            raise SiteMapError(
                f"Could not reach the OpenStreetMap Overpass service "
                f"({name}: {e}). Check network access and retry.")
        raise SiteMapError(f"OpenStreetMap request failed ({name}): {e}")
    if gdf.empty:
        raise SiteMapError(
            "The map result is empty: no roads, buildings or water features "
            "were found inside the requested extent.")
    return gdf


def feature_id_of(idx) -> str:
    """Stable source identifier 'element/id' from a features index entry."""
    try:
        element, osmid = idx
    except (TypeError, ValueError):
        return str(idx)
    return f"{element}/{osmid}"


def split_features(gdf, utm_crs, rect):
    """Project to UTM, clip to the rectangle, split into layers."""
    import geopandas as gpd
    from shapely.geometry.base import BaseGeometry

    gdf = gdf.to_crs(utm_crs)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    try:
        gdf = gpd.clip(gdf, rect, keep_geom_type=False)
    except Exception:
        from shapely.validation import make_valid

        def safe_clip(g):
            try:
                return g.intersection(rect)
            except Exception:
                try:
                    return make_valid(g).intersection(rect)
                except Exception:
                    return None

        gdf = gdf[gdf.geometry.make_valid().intersects(rect)].copy()
        gdf["geometry"] = gdf.geometry.apply(safe_clip)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if gdf.empty:
        raise SiteMapError(
            "After clipping, no features remain inside the requested "
            f"{rect.bounds} extent. Try a larger area.")

    def col(name):
        return gdf[name] if name in gdf.columns else None

    def polygonal(g):
        return g.geom_type in ("Polygon", "MultiPolygon")

    def linear(g):
        return g.geom_type in ("LineString", "MultiLineString")

    import pandas as pd

    building = col("building")
    highway = col("highway")
    natural = col("natural")
    waterway = col("waterway")
    absent = pd.Series(False, index=gdf.index)

    # building=no is an explicit "not a building" mapper statement
    has_building = (building.notna() & (building != "no")) \
        if building is not None else absent
    buildings = gdf[has_building & gdf.geometry.apply(polygonal)]

    roads = gdf[highway.notna()] if highway is not None else gdf.iloc[0:0]
    road_lines = roads[roads.geometry.apply(linear)]
    road_areas = roads[roads.geometry.apply(polygonal)]

    # Rivers are mapped either as natural=water polygons or as polygonal
    # waterway=riverbank/dock features; both must render as water areas.
    natural_water = (natural == "water") if natural is not None else absent
    has_waterway = waterway.notna() if waterway is not None else absent
    water_polys = gdf[(natural_water | has_waterway)
                      & gdf.geometry.apply(polygonal)]
    water_lines = gdf[has_waterway & gdf.geometry.apply(linear)]

    return {
        "buildings": buildings,
        "road_lines": road_lines,
        "road_areas": road_areas,
        "water_polys": water_polys,
        "water_lines": water_lines,
    }


# The one thing this stack borrows from the CAD side: where arrows sit
# along a run. It is pure geometry with no dependencies of its own, and
# sharing it is what makes a poster, a drawing and this sheet put the
# arrows in the same places. Nothing about fetching or tagging crosses
# over — those stay separate on purpose.
def arrow_positions(coords):
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import stage_db

    return stage_db.arrow_positions(coords)


# Restated rather than imported, so this stack keeps its own dependency
# set: importing topo2cad would pull the Overpass/CAD side in behind it.
# Mirrors topo2cad.oneway_dir(); the tag values are a stable OSM
# convention. -1/reverse means "against the way as digitised".
def oneway_of(row) -> int:
    def cell(name):
        value = row.get(name) if hasattr(row, "get") else None
        return "" if value is None else str(value).strip().lower()

    value = cell("oneway")
    if value in ("yes", "true", "1"):
        return 1
    if value in ("-1", "reverse"):
        return -1
    if value in ("no", "false", "0"):
        return 0
    return 1 if cell("junction") in ("roundabout", "circular") else 0


def draw_oneway_arrows(ax, road_lines, colours, width_m) -> int:
    """Direction arrows on one-way carriageways. Returns how many."""
    length = max(6.0, width_m * 0.012)
    drawn = 0
    for _idx, row in road_lines.iterrows():
        direction = oneway_of(row)
        if not direction:
            continue
        for line in iter_parts(row.geometry, ("LineString",)):
            for x, y, rot in arrow_positions(list(line.coords)):
                ang = math.radians(rot + (180.0 if direction < 0 else 0.0))
                dx = math.cos(ang) * length / 2
                dy = math.sin(ang) * length / 2
                ax.annotate("", xy=(x + dx, y + dy), xytext=(x - dx, y - dy),
                            arrowprops=dict(arrowstyle="-|>", lw=0.8,
                                            color=colours["road_major"],
                                            shrinkA=0, shrinkB=0),
                            zorder=4.5)
                drawn += 1
    return drawn


def draw_basemap(ax, args, rect, epsg) -> str:
    """Tile backdrop under the linework; returns the credit to print."""
    import sys as _sys
    import tempfile

    import numpy as np
    from pyproj import Transformer

    _sys.path.insert(0, str(Path(__file__).resolve().parent))

    to_wgs = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    x0, y0, x1, y1 = rect.bounds
    west, south = to_wgs.transform(x0, y0)
    east, north = to_wgs.transform(x1, y1)
    try:
        import rasterio

        import basemap as bm
        with tempfile.TemporaryDirectory() as tmp:
            info = bm.build((south, west, north, east), epsg,
                            Path(tmp) / "bm.tif", provider=args.basemap,
                            max_tiles=128)
            with rasterio.open(info["path"]) as src:
                rgb = np.transpose(src.read([1, 2, 3]), (1, 2, 0))
                bounds = src.bounds
    except bm.BasemapError as exc:
        print(f"NOTE: no background map — {exc}")
        return ""
    except ImportError as exc:                    # raster stack absent
        print(f"NOTE: no background map — {exc}. The backdrop needs "
              "rasterio and pillow; the rest of this script does not.")
        return ""
    ax.imshow(rgb, extent=(bounds.left, bounds.right,
                           bounds.bottom, bounds.top),
              zorder=0.2, alpha=args.basemap_alpha, interpolation="bilinear")
    print(bm.describe(info))
    return info["attribution"]


def classify_road(highway_value: str) -> str:
    for cls in ("major", "main", "local"):
        if highway_value in ROAD_CLASSES[cls][0]:
            return cls
    return "minor"


# ----------------------------------------------------------------------------
# Building inventory and manual labels (spec FR-05, FR-06, FR-07)
# ----------------------------------------------------------------------------
def load_manual_labels(path: str) -> dict[str, str]:
    overrides: dict[str, str] = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames is None:
                raise SiteMapError(f"Labels CSV '{path}' is empty.")
            fields = {name.strip() for name in reader.fieldnames}
            missing = {"feature_id", "display_name"} - fields
            if missing:
                raise SiteMapError(
                    f"Labels CSV '{path}' has an invalid structure: missing "
                    f"required column(s) {sorted(missing)}. Expected at "
                    "least: feature_id, display_name.")
            for row in reader:
                fid = (row.get("feature_id") or "").strip()
                name = (row.get("display_name") or "").strip()
                if fid and name:
                    overrides[fid] = name
    except UnicodeDecodeError:
        raise SiteMapError(
            f"Labels CSV '{path}' is not UTF-8 encoded (Thai files saved "
            "as TIS-620/Windows-874 are common). Re-save it as UTF-8.")
    except (OSError, csv.Error) as e:
        raise SiteMapError(f"Cannot read labels CSV '{path}': {e}")
    return overrides


def _clean_tag(value) -> str:
    """A tag value as a stripped string; osmnx leaves absent tags as NaN."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def thai_first_name(row) -> str:
    """Preferred label for a feature, Thai first.

    Thai mapping convention puts Thai script in the plain `name` tag, but a
    business that trades under an English name overrides that, so `name`
    alone is not a reliable Thai source. `name:th` is, when it is present.
    """
    for key in ("name:th", "name"):
        try:
            value = _clean_tag(row.get(key))
        except (AttributeError, TypeError):
            return ""
        if value:
            return value
    return ""


def build_inventory(buildings, to_wgs, overrides: dict[str, str]
                    ) -> list[BuildingRecord]:
    """One record per rendered building; deterministic B### codes."""
    records: list[BuildingRecord] = []
    rows = sorted(buildings.iterrows(), key=lambda kv: feature_id_of(kv[0]))
    counter = 0
    for idx, row in rows:
        fid = feature_id_of(idx)
        osm_name = thai_first_name(row)
        btype = row.get("building")
        btype = "" if btype is None else str(btype)
        if osm_name:
            code = ""
        else:
            counter += 1
            code = f"B{counter:03d}"
        # Manual override wins; empty override falls back (spec FR-07)
        display = overrides.get(fid, "") or osm_name or code
        pt = row.geometry.representative_point()
        lon, lat = to_wgs.transform(pt.x, pt.y)
        records.append(BuildingRecord(
            feature_id=fid, code=code, osm_name=osm_name,
            display_name=display, building_type=btype,
            latitude=round(lat, 8), longitude=round(lon, 8),
            geometry=row.geometry))
    return records


def write_inventory(records: list[BuildingRecord], path: str) -> None:
    ensure_parent_dir(path)
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=INVENTORY_FIELDS)
            w.writeheader()
            for r in records:
                w.writerow({k: getattr(r, k) for k in INVENTORY_FIELDS})
    except OSError as e:
        raise SiteMapError(f"Cannot write inventory CSV '{path}': {e}")


# ----------------------------------------------------------------------------
# Fonts (spec 8.3)
# ----------------------------------------------------------------------------
def configure_font(font_path: str | None, warn_if_no_thai: bool) -> str:
    import matplotlib
    from matplotlib import font_manager as fm

    matplotlib.rcParams["pdf.fonttype"] = 42  # keep text as text in the PDF
    matplotlib.rcParams["svg.fonttype"] = "none"

    if font_path:
        try:
            fm.fontManager.addfont(font_path)
            family = fm.FontProperties(fname=font_path).get_name()
        except Exception as e:
            raise SiteMapError(f"Cannot load font '{font_path}': {e}")
        matplotlib.rcParams["font.family"] = [family, "DejaVu Sans"]
        return family

    # OSM labels in Thailand are commonly Thai, so always prefer a
    # Thai-capable system font when none is supplied (spec 8.3).
    installed = {f.name for f in fm.fontManager.ttflist}
    for cand in THAI_FONT_CANDIDATES:
        if cand in installed:
            matplotlib.rcParams["font.family"] = [cand, "DejaVu Sans"]
            return cand
    if warn_if_no_thai:
        print("WARNING: no Thai-capable font found; Thai text may render as "
              "boxes. Supply one with --font /path/to/font.ttf",
              file=sys.stderr)
    return matplotlib.rcParams["font.family"][0]


# ----------------------------------------------------------------------------
# Rendering helpers
# ----------------------------------------------------------------------------
def halo(width=2.0, colour="white"):
    import matplotlib.patheffects as pe
    return [pe.withStroke(linewidth=width, foreground=colour)]


def iter_parts(geom, types):
    """Yield every part of `geom` matching `types`, descending into
    Multi* and GeometryCollection (the clip step can produce either)."""
    if geom is None or geom.is_empty:
        return
    if geom.geom_type in types:
        yield geom
    elif hasattr(geom, "geoms"):
        for part in geom.geoms:
            yield from iter_parts(part, types)


def polygon_patch(poly, **kwargs):
    """A matplotlib patch for a shapely Polygon, with true holes so
    features lying inside interior rings stay visible."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath
    from shapely.geometry.polygon import orient

    # Exterior CCW, holes CW — required by the nonzero winding fill rule
    poly = orient(poly)
    verts: list = []
    codes: list = []
    for ring in [poly.exterior, *poly.interiors]:
        pts = list(ring.coords)
        verts += pts
        codes += ([MplPath.MOVETO] + [MplPath.LINETO] * (len(pts) - 2)
                  + [MplPath.CLOSEPOLY])
    return PathPatch(MplPath(verts, codes), **kwargs)


def longest_line(geom):
    lines = list(iter_parts(geom, ("LineString",)))
    return max(lines, key=lambda g: g.length) if lines else None


def label_angle(line) -> float:
    """Upright text angle (degrees) following the road direction."""
    a = line.interpolate(0.4, normalized=True)
    b = line.interpolate(0.6, normalized=True)
    ang = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
    if ang > 90:
        ang -= 180
    elif ang < -90:
        ang += 180
    return ang


def draw_roads(ax, road_lines, road_areas, colours):
    # Pedestrian plazas etc. render as a filled road surface
    for _, row in road_areas.iterrows():
        for poly in iter_parts(row.geometry, ("Polygon",)):
            ax.add_patch(polygon_patch(poly,
                                       facecolor=colours["road_minor"],
                                       edgecolor=colours["road_local"],
                                       linewidth=0.4, zorder=3))
    for _, row in road_lines.iterrows():
        cls = classify_road(str(row.get("highway")))
        _, lw, z = ROAD_CLASSES[cls]
        for part in iter_parts(row.geometry, ("LineString",)):
            xs, ys = part.xy
            ax.plot(xs, ys, color=colours[f"road_{cls}"], linewidth=lw,
                    zorder=z, solid_capstyle="round")


def draw_road_labels(ax, road_lines, colours, m_per_pt, min_len_m=35.0):
    """Each unique named road labelled once, along its geometry (FR-03).
    Returns (count, skipped names, occupied boxes) — road names outrank
    building labels, which yield to the boxes returned here."""
    from shapely.ops import linemerge, unary_union

    if road_lines.empty or not {"name", "name:th"} & set(road_lines.columns):
        return 0, [], []
    # Resolve name:th over name per row before grouping, so a carriageway
    # tagged both ways still dedupes to one label (FR-03).
    named = road_lines.assign(
        _label=[thai_first_name(r) for _, r in road_lines.iterrows()])
    named = named[named["_label"] != ""]
    count = 0
    skipped: list[str] = []
    boxes: list[tuple[float, float, float, float]] = []
    for name, group in named.groupby("_label"):
        name = str(name).strip()
        if not name:
            continue
        lines = list(iter_parts(unary_union(list(group.geometry)),
                                ("LineString",)))
        if not lines:
            continue
        merged = linemerge(lines) if len(lines) > 1 else lines[0]
        line = longest_line(merged)
        if line is None or line.length < min_len_m:
            # Usually a sliver clipped at the map edge: too short to carry
            # a legible label, reported so the operator can verify (§12).
            skipped.append(name)
            continue
        mid = line.interpolate(0.5, normalized=True)
        angle = label_angle(line)
        ax.text(mid.x, mid.y, name, rotation=angle,
                rotation_mode="anchor", ha="center", va="center",
                fontsize=6.5, color=colours["text_primary"],
                path_effects=halo(2.2), zorder=9, clip_on=True)
        # Axis-aligned bounds of the rotated label, for building labels
        # to avoid
        tw = len(name) * 6.5 * 0.55 * m_per_pt
        th = 6.5 * 1.15 * m_per_pt
        rad = math.radians(angle)
        hw = (abs(tw * math.cos(rad)) + abs(th * math.sin(rad))) / 2
        hh = (abs(tw * math.sin(rad)) + abs(th * math.cos(rad))) / 2
        boxes.append((mid.x - hw, mid.y - hh, mid.x + hw, mid.y + hh))
        count += 1
    return count, skipped, boxes


def label_fits(label: str, fs: float, geom, m_per_pt: float) -> bool:
    """Whether `label` physically fits inside the footprint at map scale.
    Text metrics are approximate (0.55 em average advance) but scale
    correctly, which is what keeps dense extents legible (spec 8.4)."""
    minx, miny, maxx, maxy = geom.bounds
    text_w = len(label) * fs * 0.55 * m_per_pt
    text_h = fs * 1.15 * m_per_pt
    return text_w <= (maxx - minx) and text_h <= (maxy - miny)


def draw_buildings(ax, records, colours, show_codes: bool, m_per_pt: float,
                   occupied=None):
    for r in records:
        for poly in iter_parts(r.geometry, ("Polygon",)):
            ax.add_patch(polygon_patch(poly,
                                       facecolor=colours["building_fill"],
                                       edgecolor=colours["building_edge"],
                                       linewidth=0.5, zorder=2))

    dropped = 0
    candidates = []
    for r in records:
        label = r.display_name
        is_code_only = bool(r.code) and label == r.code
        if is_code_only and not show_codes:
            continue
        area = r.geometry.area
        fs = 5.5 if is_code_only else min(6.0, max(4.5, math.sqrt(area) / 6))
        # A name that cannot fit falls back to the shorter B### code; a
        # code that cannot fit is dropped from the map. Either way the
        # full name stays in the inventory CSV (spec 8.4).
        if not label_fits(label, fs, r.geometry, m_per_pt):
            if not (r.code and show_codes and label != r.code):
                dropped += 1
                continue
            label, is_code_only, fs = r.code, True, 5.5
            if not label_fits(label, fs, r.geometry, m_per_pt):
                dropped += 1
                continue
        candidates.append((0 if not is_code_only else 1, -area,
                           label, fs, r.geometry, is_code_only))

    # Verified names outrank codes, then larger footprints; a label that
    # would collide with one already placed is dropped rather than
    # overprinted (spec 8.4 readability).
    placed: list[tuple[float, float, float, float]] = []
    road_boxes = list(occupied or [])

    def clashes(box, obstacles):
        return any(box[0] < o[2] and box[2] > o[0]
                   and box[1] < o[3] and box[3] > o[1] for o in obstacles)

    for _, _, label, fs, geom, is_code in sorted(candidates,
                                                 key=lambda c: (c[0], c[1])):
        # Verified names are orientation information the operator needs
        # (13.6 landmarks), so they yield only to other building labels;
        # B### codes are redundant with the inventory, so they also yield
        # to road names.
        obstacles = placed + road_boxes if is_code else placed
        hw = len(label) * fs * 0.55 * m_per_pt / 2
        hh = fs * 1.15 * m_per_pt / 2
        minx, miny, maxx, maxy = geom.bounds
        rep = geom.representative_point()
        # A crowded spot does not mean the whole footprint is unusable:
        # try a few interior positions before giving the label up.
        spots = [(rep.x, rep.y)]
        for dx, dy in ((0, 0.25), (0, -0.25), (0.25, 0), (-0.25, 0)):
            spots.append((rep.x + dx * (maxx - minx),
                          rep.y + dy * (maxy - miny)))
        for px, py in spots:
            box = (px - hw, py - hh, px + hw, py + hh)
            if box[0] < minx or box[2] > maxx or box[1] < miny \
                    or box[3] > maxy or clashes(box, obstacles):
                continue
            placed.append(box)
            ax.text(px, py, label, ha="center", va="center", fontsize=fs,
                    color=colours["text_primary"], path_effects=halo(1.8),
                    zorder=8, clip_on=True)
            break
        else:
            dropped += 1
    return dropped


def draw_water(ax, water_polys, water_lines, colours):
    for _, row in water_polys.iterrows():
        for poly in iter_parts(row.geometry, ("Polygon",)):
            ax.add_patch(polygon_patch(poly,
                                       facecolor=colours["water_fill"],
                                       edgecolor=colours["water_edge"],
                                       linewidth=0.6, zorder=1))
    for _, row in water_lines.iterrows():
        for part in iter_parts(row.geometry, ("LineString",)):
            xs, ys = part.xy
            ax.plot(xs, ys, color=colours["water_edge"], linewidth=1.2,
                    zorder=1)


def draw_site_marker(ax, xy, colours):
    ax.plot(*xy, marker="o", markersize=11, color=colours["site_marker"],
            markeredgecolor="white", markeredgewidth=1.6, zorder=12)


def draw_north_arrow(ax, colours):
    ax.annotate("N", xy=(0.965, 0.955), xytext=(0.965, 0.865),
                xycoords="axes fraction", textcoords="axes fraction",
                ha="center", va="center", fontsize=11, fontweight="bold",
                color=colours["text_primary"],
                path_effects=halo(2.5),
                arrowprops=dict(arrowstyle="-|>", linewidth=1.6,
                                color=colours["text_primary"]),
                zorder=15)


def nice_scale_length(width_m: float) -> float:
    target = width_m / 4.0
    for cand in (1000, 500, 250, 200, 100, 50, 25, 20, 10, 5):
        if cand <= target:
            return float(cand)
    return max(1.0, round(target))


def draw_scale_bar(ax, rect, colours):
    x0, y0, x1, _ = rect.bounds
    width_m = x1 - x0
    length = nice_scale_length(width_m)
    margin_x = width_m * 0.03
    margin_y = (rect.bounds[3] - y0) * 0.05
    bx, by = x0 + margin_x, y0 + margin_y
    seg = length / 2
    for i, colour in enumerate(["black", "white"]):
        ax.fill([bx + i * seg, bx + (i + 1) * seg, bx + (i + 1) * seg,
                 bx + i * seg],
                [by, by, by + width_m * 0.008, by + width_m * 0.008],
                facecolor=colour, edgecolor="black", linewidth=0.6,
                zorder=15, clip_on=False)
    for value, xpos in ((0, bx), (seg, bx + seg), (length, bx + length)):
        ax.text(xpos, by + width_m * 0.013, f"{value:g}", ha="center",
                va="bottom", fontsize=6, color=colours["text_primary"],
                path_effects=halo(2), zorder=15)
    ax.text(bx + length / 2, by - width_m * 0.008, "metres", ha="center",
            va="top", fontsize=5.5, color=colours["text_secondary"],
            path_effects=halo(2), zorder=15)
    return length


def legend_handles(colours, show_codes: bool):
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    handles = [
        Line2D([], [], color=colours["road_major"], linewidth=2.8,
               label="Major road (motorway/trunk/primary)"),
        Line2D([], [], color=colours["road_main"], linewidth=2.0,
               label="Main local road (secondary/tertiary)"),
        Line2D([], [], color=colours["road_local"], linewidth=1.2,
               label="Local road (residential/service)"),
        Line2D([], [], color=colours["road_minor"], linewidth=0.7,
               label="Minor access / path"),
        Patch(facecolor=colours["building_fill"],
              edgecolor=colours["building_edge"], label="Building footprint"),
        Patch(facecolor=colours["water_fill"],
              edgecolor=colours["water_edge"], label="Water"),
        Line2D([], [], marker="o", linestyle="none", markersize=8,
               color=colours["site_marker"], markeredgecolor="white",
               label="Site coordinate"),
    ]
    if show_codes:
        handles.append(Line2D([], [], linestyle="none", marker="$B001$",
                              markersize=16, color=colours["text_primary"],
                              label="Unnamed building (see inventory CSV)"))
    return handles


def drawn_width_inches(fig, ax, width_m: float, height_m: float) -> float:
    # aspect='equal' letterboxes the drawn map inside the axes box at draw
    # time; use the constrained dimension, not the nominal box width.
    fw, fh = fig.get_size_inches()
    pos = ax.get_position()
    box_w_in, box_h_in = fw * pos.width, fh * pos.height
    return min(box_w_in, box_h_in * (width_m / height_m))


def metres_per_point(fig, ax, width_m: float, height_m: float) -> float:
    """Ground metres covered by one typographic point on the sheet."""
    return width_m / (drawn_width_inches(fig, ax, width_m, height_m) * 72.0)


def representative_fraction(fig, ax, width_m: float, height_m: float) -> int:
    drawn_w_in = drawn_width_inches(fig, ax, width_m, height_m)
    denom = (width_m / (drawn_w_in * 0.0254))
    # round to 2 significant figures for display
    if denom <= 0:
        return 0
    mag = 10 ** (int(math.floor(math.log10(denom))) - 1)
    return int(round(denom / mag) * mag)


# ----------------------------------------------------------------------------
# Layout: standard profile
# ----------------------------------------------------------------------------
def render_standard(args, layers, records, site_xy, rect, epsg, stats):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colours = COLOURS
    # Match the sheet to the extent so a portrait area does not waste
    # most of the page (the government profile stays landscape per 13.9).
    sheet_w, sheet_h = SHEET_SIZES[args.sheet_size]
    if args.height > args.width:
        sheet_w, sheet_h = sheet_h, sheet_w
    orientation = "landscape" if sheet_w >= sheet_h else "portrait"
    fig = plt.figure(figsize=(sheet_w, sheet_h))
    fig.patch.set_facecolor("white")
    ax = fig.add_axes([0.04, 0.225, 0.92, 0.695])
    stats["road_labels"] = _draw_map_body(ax, args, layers, records,
                                          site_xy, rect, colours, epsg)

    fig.suptitle(args.title, fontsize=18, fontweight="bold",
                 color=colours["text_primary"], y=0.965)
    fig.text(0.5, 0.935,
             f"Centre: {args.lat:.8f}°, {args.lon:.8f}° (WGS 84)  |  "
             f"Coverage: {args.width:.0f} m × {args.height:.0f} m",
             ha="center", fontsize=10, color=colours["text_secondary"])

    draw_north_arrow(ax, colours)
    draw_scale_bar(ax, rect, colours)

    # Metadata strip (spec FR-10), outside the map frame
    x, y = site_xy
    rf = representative_fraction(fig, ax, args.width, args.height)
    meta_left = (
        f"Geographic (WGS 84 / EPSG:4326): Lat {args.lat:.8f}°, "
        f"Lon {args.lon:.8f}°\n"
        f"Projected (EPSG:{epsg}): E {x:,.2f} m, N {y:,.2f} m\n"
        f"Coverage: {args.width:.0f} m × {args.height:.0f} m   |   "
        f"Approx. scale 1:{rf:,} at {args.sheet_size} {orientation}"
    )
    meta_right = (
        f"Roads: {stats['roads']} segments ({stats['road_labels']} "
        f"road-name labels)   Buildings: {stats['buildings']} "
        f"({stats['named_buildings']} named, {stats['coded_buildings']} "
        f"coded)   Water features: {stats['water']}\n"
        f"Data © OpenStreetMap contributors (ODbL). Retrieved "
        f"{stats['retrieved']}.\n"
        # The backdrop's own licence line, printed only when one was drawn
        + (f"{getattr(args, '_basemap_credit', '')}\n"
           if getattr(args, "_basemap_credit", "") else "") +
        f"Generated {stats['generated']} — community data; verify names "
        "before legal/engineering use."
    )
    fig.text(0.04, 0.095, meta_left, fontsize=8,
             color=colours["text_primary"], va="top")
    fig.text(0.52, 0.095, meta_right, fontsize=8,
             color=colours["text_secondary"], va="top")

    ax.legend(handles=legend_handles(colours,
                                     not args.no_building_codes),
              loc="upper center", bbox_to_anchor=(0.5, -0.015),
              ncol=4, fontsize=7.5, frameon=True, edgecolor="#CCCCCC")
    return fig


# ----------------------------------------------------------------------------
# Layout: Thai Government Submission profile (spec section 13)
# ----------------------------------------------------------------------------
SHEET_SIZES = {"A4": (11.69, 8.27), "A3": (16.54, 11.69),
               "A2": (23.39, 16.54), "A1": (33.11, 23.39)}


def render_government(args, layers, records, site_xy, rect, epsg, stats):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colours = GOV_COLOURS
    sheet = SHEET_SIZES[args.sheet_size]
    fig = plt.figure(figsize=sheet)
    fig.patch.set_facecolor("white")

    # Block positions are figure fractions but font sizes are absolute
    # points, so type must scale with the sheet or it overruns the border
    # and collides on smaller sheets (spec 8.3, 11: nothing clipped).
    s = sheet[0] / SHEET_SIZES["A3"][0]

    # Formal sheet border and inner frame (13.2)
    fig.add_artist(plt.Rectangle((0.012, 0.017), 0.976, 0.966, fill=False,
                                 linewidth=2.0, edgecolor="black",
                                 transform=fig.transFigure))
    fig.add_artist(plt.Rectangle((0.02, 0.028), 0.96, 0.944, fill=False,
                                 linewidth=0.8, edgecolor="black",
                                 transform=fig.transFigure))

    # Map frame on the left; information column on the right
    ax = fig.add_axes([0.035, 0.06, 0.63, 0.86])
    stats["road_labels"] = _draw_map_body(ax, args, layers, records,
                                          site_xy, rect, colours)
    draw_north_arrow(ax, colours)
    draw_scale_bar(ax, rect, colours)

    right_x = 0.69
    col_w = 0.285
    y = 0.955

    def block(lines, title=None, dy=0.0138, fs=7.6, title_fs=8.4):
        nonlocal y
        if title:
            fig.text(right_x, y, title, fontsize=title_fs * s,
                     fontweight="bold", color="black", va="top")
            y -= 0.019
        for line in lines:
            fig.text(right_x, y, line, fontsize=fs * s, color="black",
                     va="top")
            y -= dy
        y -= 0.010
        fig.add_artist(plt.Line2D([right_x, right_x + col_w], [y + 0.004] * 2,
                                  linewidth=0.6, color="black",
                                  transform=fig.transFigure))
        y -= 0.012

    # Title (13.3)
    fig.text(right_x + col_w / 2, 0.972,
             "แผนผังแสดงที่ตั้งโครงการ", fontsize=11 * s, fontweight="bold",
             ha="center", va="top")
    fig.text(right_x + col_w / 2, 0.953, "PROJECT LOCATION AND SITE MAP",
             fontsize=9 * s, ha="center", va="top")
    y = 0.938
    fig.add_artist(plt.Line2D([right_x, right_x + col_w], [y] * 2,
                              linewidth=1.2, color="black",
                              transform=fig.transFigure))
    y -= 0.014

    admin = ", ".join(v for v in [
        f"ต./แขวง {args.subdistrict}" if args.subdistrict else "",
        f"อ./เขต {args.district}" if args.district else "",
        f"จ. {args.province}" if args.province else ""] if v)
    rf = representative_fraction(fig, ax, args.width, args.height)
    block([
        f"ชื่อโครงการ (Project): {args.project_name or '—'}",
        f"สถานที่ตั้ง (Location): {args.site_location or '—'}",
        f"เขตปกครอง: {admin or '—'}",
        f"หน่วยงานเจ้าของโครงการ (Agency): {args.agency or '—'}",
        f"วันที่จัดทำ (Date): {stats['generated_date']}",
        f"มาตราส่วน (Scale): ≈ 1:{rf:,} ({args.sheet_size} landscape)",
        f"เลขที่แบบ (Drawing no.): {args.drawing_no or '—'}    "
        f"แผ่นที่ (Sheet): {args.sheet}    แก้ไขครั้งที่ (Rev.): "
        f"{args.revision}",
    ], title="ข้อมูลโครงการ / PROJECT INFORMATION")

    # Coordinate statement (13.4) — latitude before longitude; E before N
    x, ycoord = site_xy
    zone = epsg - (32600 if args.lat >= 0 else 32700)
    hemi = "N" if args.lat >= 0 else "S"
    block([
        "Geographic coordinate (WGS 84 / EPSG:4326)",
        f"  Latitude:  {abs(args.lat):.8f}° {'N' if args.lat >= 0 else 'S'}",
        f"  Longitude: {abs(args.lon):.8f}° "
        f"{'E' if args.lon >= 0 else 'W'}",
        "Grid coordinate",
        f"  Projection: WGS 84 / UTM Zone {zone}{hemi}   EPSG: {epsg}",
        f"  Easting (E):  {x:,.2f} m",
        f"  Northing (N): {ycoord:,.2f} m",
        f"Coverage: {args.width:.0f} m × {args.height:.0f} m",
    ], title="พิกัดที่ตั้ง / COORDINATES")

    # Legend (13.5)
    leg_ax = fig.add_axes([right_x, y - 0.15, col_w, 0.145])
    leg_ax.axis("off")
    leg_ax.legend(handles=legend_handles(colours,
                                         not args.no_building_codes),
                  loc="upper left", fontsize=6.8 * s, frameon=False, ncol=1,
                  title="สัญลักษณ์ / LEGEND", title_fontsize=8 * s,
                  alignment="left")
    y -= 0.165
    fig.add_artist(plt.Line2D([right_x, right_x + col_w], [y] * 2,
                              linewidth=0.6, color="black",
                              transform=fig.transFigure))
    y -= 0.012

    # Certification block (13.10)
    def sig(role_th, role_en, name):
        filled = name if name else "________________________"
        return [f"{role_th} ({role_en}): {filled}",
                "ตำแหน่ง (Position): ____________________  "
                "วันที่ (Date): __________"]
    block(sig("ผู้จัดทำ", "Prepared by", args.prepared_by) + [""]
          + sig("ผู้ตรวจสอบ", "Checked by", args.checked_by) + [""]
          + sig("ผู้อนุมัติ", "Approved by", args.approved_by) + [""]
          + ["ตราประทับ (Official stamp):", "", ""],
          title="การรับรอง / CERTIFICATION", dy=0.0148, fs=7.2)

    # Data source and accuracy statement (13.5, 13.11)
    block([
        f"Source: © OpenStreetMap contributors (ODbL), retrieved "
        f"{stats['retrieved']}.",
        "This map is prepared for project-location reference. Coordinates",
        "are based on WGS 84 and the stated UTM projection. Road, building,",
        "and place-name data shall be verified against field conditions and",
        "authoritative agency records before use for engineering, cadastral,",
        "permitting, or legal purposes.",
    ], title="แหล่งข้อมูลและความถูกต้อง / DATA & ACCURACY", fs=6.6,
        dy=0.0115)

    if not args.final:
        fig.text(0.35, 0.5, "DRAFT / FOR REVIEW", fontsize=52 * s,
                 color="#B00020", alpha=0.18, ha="center", va="center",
                 rotation=30, fontweight="bold", zorder=100)
    return fig


def _draw_map_body(ax, args, layers, records, site_xy, rect, colours,
                   epsg=None):
    x0, y0, x1, y1 = rect.bounds
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.set_facecolor("white")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_linewidth(1.4)
        spine.set_edgecolor("black")

    m_per_pt = metres_per_point(ax.get_figure(), ax, args.width, args.height)
    if getattr(args, "basemap", None) and epsg:
        args._basemap_credit = draw_basemap(ax, args, rect, epsg)
    draw_water(ax, layers["water_polys"], layers["water_lines"], colours)
    draw_roads(ax, layers["road_lines"], layers["road_areas"], colours)
    # Road names are placed first so building labels can yield to them
    count, skipped, road_boxes = draw_road_labels(
        ax, layers["road_lines"], colours, m_per_pt)
    dropped = draw_buildings(ax, records, colours,
                             not args.no_building_codes, m_per_pt,
                             occupied=road_boxes)
    if getattr(args, "arrows", False):
        n = draw_oneway_arrows(ax, layers["road_lines"], colours, args.width)
        print(f"One-way arrows: {n}")
    draw_site_marker(ax, site_xy, colours)
    if skipped:
        shown = ", ".join(skipped[:5])
        more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
        print(f"NOTE: {len(skipped)} named road(s) too short inside the "
              f"extent to carry a label: {shown}{more}. Usually slivers "
              "clipped at the map edge.")
    if dropped:
        print(f"NOTE: {dropped} building label(s) omitted — the footprint "
              "is too small to hold the text at this scale. Every building "
              "remains in the inventory CSV; use a smaller area for a "
              "larger printed scale.")
    return count


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        run_dir = apply_outdir(args)
        validate_args(args)
        if run_dir:
            print(f"Run folder: {run_dir}")
        ensure_parent_dir(args.output)
        ensure_parent_dir(args.inventory)
        if args.png:
            ensure_parent_dir(args.png)

        family = configure_font(args.font,
                                warn_if_no_thai=args.profile == "government")
        print(f"Font: {family}")

        print(f"Building {args.width:.0f} m x {args.height:.0f} m extent "
              f"centred on ({args.lat}, {args.lon}) ...")
        epsg, utm, to_wgs, site_xy, rect, rect_wgs = build_extent(
            args.lat, args.lon, args.width, args.height)
        print(f"Projected CRS: EPSG:{epsg} "
              f"(site E {site_xy[0]:,.2f}, N {site_xy[1]:,.2f})")

        # Validate the labels CSV before the (slow) network fetch.
        overrides = load_manual_labels(args.labels_csv) \
            if args.labels_csv else {}
        if overrides:
            print(f"Loaded {len(overrides)} manual label(s) from "
                  f"{args.labels_csv}")

        print("Retrieving OpenStreetMap features ...")
        retrieved = datetime.now().strftime("%Y-%m-%d %H:%M")
        gdf = fetch_features(rect_wgs)
        layers = split_features(gdf, utm, rect)

        records = build_inventory(layers["buildings"], to_wgs, overrides)
        unmatched = sorted(set(overrides) - {r.feature_id for r in records})
        if unmatched:
            shown = ", ".join(unmatched[:5])
            more = f" (+{len(unmatched) - 5} more)" if len(unmatched) > 5 \
                else ""
            print(f"WARNING: {len(unmatched)} manual label(s) matched no "
                  f"rendered building and were NOT applied: {shown}{more}. "
                  "Check the feature_id values (Excel can mangle them).",
                  file=sys.stderr)
        if args.labels_csv and Path(args.inventory).resolve() == \
                Path(args.labels_csv).resolve():
            print(f"NOTE: inventory not rewritten — '{args.inventory}' is "
                  "your curated labels file. Pass a different --inventory "
                  "path to export an updated copy.")
        else:
            write_inventory(records, args.inventory)
            print(f"Building inventory: {len(records)} rows -> "
                  f"{args.inventory}")

        stats = {
            "roads": len(layers["road_lines"]),
            "named_roads": int(layers["road_lines"]["name"].notna().sum())
            if "name" in layers["road_lines"].columns else 0,
            "buildings": len(records),
            "named_buildings": sum(1 for r in records
                                   if not r.code or r.display_name != r.code),
            "coded_buildings": sum(1 for r in records
                                   if r.code and r.display_name == r.code),
            "water": len(layers["water_polys"]) + len(layers["water_lines"]),
            "retrieved": retrieved,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "generated_date": datetime.now().strftime("%Y-%m-%d"),
        }
        print(f"Features in extent: {stats['roads']} road segments, "
              f"{stats['buildings']} buildings, {stats['water']} water")

        print(f"Rendering ({args.profile} profile) ...")
        if args.profile == "government":
            fig = render_government(args, layers, records, site_xy, rect,
                                    epsg, stats)
        else:
            fig = render_standard(args, layers, records, site_xy, rect,
                                  epsg, stats)

        try:
            fig.savefig(args.output, format="pdf")
        except Exception as e:
            raise SiteMapError(f"Cannot write PDF '{args.output}' "
                               f"({type(e).__name__}): {e}")
        print(f"PDF written: {args.output}")

        if args.png:
            try:
                fig.savefig(args.png, format="png", dpi=300)
            except Exception as e:
                raise SiteMapError(f"Cannot write PNG '{args.png}' "
                                   f"({type(e).__name__}): {e}")
            print(f"PNG written (300 DPI): {args.png}")
        return 0

    except SiteMapError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001 - last-resort clear message
        import traceback
        traceback.print_exc()
        print(f"ERROR: unexpected failure ({type(e).__name__}): {e}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
