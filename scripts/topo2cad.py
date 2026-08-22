# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "rasterio",
#   "numpy",
#   "scipy",
#   "scikit-image",
#   "ezdxf",
#   "pyproj",
#   "requests",
#   "shapely>=2.0",
#   "pillow",
# ]
# ///
"""Topo + OSM (buildings w/ names, roads) around a GPS point -> DXF."""


import argparse


import csv


import hashlib


import gzip


import json


import math


import os


import re


import sys


import time


from pathlib import Path


from pyproj import Transformer


import requests


# Measured, not assumed, and the order is the measurement: the same query
# took 11 s on lz4 and 114 s on kumi.systems, which then answered 500.
# maps.mail.ru answered *correctly* after 1142 s — nineteen minutes is not a
# fallback, it is a hang, so it is not here. It used to be third, which is
# exactly what a run fell into when the first two returned 504 and 500
# within a minute of each other; generate_detailed_site_map.py already had
# the right list and this one had drifted from it.
# The NCS layer-name table moved to cad_rules.py — a layer table is a
# drawing rule, and osm_source.py needs it to decide which road layer
# a way belongs on without importing this script back.
from cad_rules import LAYERS  # noqa: E402,F401

# Where the data comes from and what each tag means now lives in
# osm_source.py. Re-exported here in full and by name: four other
# scripts import these from topo2cad and the documentation names
# them that way. The dependency runs one way.
from osm_source import (  # noqa: E402,F401
    BUILT_UP_LANDUSE, HEADERS, IMPLICIT_ONEWAY_JUNCTIONS,
    ML_OVERLAP_MAX, MS_LINKS_URL, ONEWAY_FORWARD, ONEWAY_REVERSE,
    OSM_CACHE_DIR, OSM_CACHE_TTL, OVERPASS_ENV, OVERPASS_URLS,
    PATH_TYPES, POI_KEYS, PUBLIC_OVERPASS_URLS, normalise_overpass,
    overpass_urls,
    POI_SUBMISSION, PRIMARY_TAGS, ROAD_WIDTH_M, THAI_RE, TRUNK_CLASSES,
    _cache_path, _clip_seg, _first_tag, _post_overpass,
    assign_inner_rings, bbox_around, best_name, carriageway_width,
    classify_elements, clip_runs, fetch_ms_buildings, fetch_osm,
    is_thai, lane_width, merge_ml_footprints, ms_release_from_url,
    ms_source_tags, names_by_lang, new_ml_rings, oneway_dir, poi_kind,
    quadkey, road_cad_layer, road_label, source_tags)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--radius", type=float,
                   help="meters; square box of +/-radius instead of a "
                        "width x height rectangle")
    p.add_argument("--width", type=float, help="full box width in meters")
    p.add_argument("--height", type=float, help="full box height in meters")
    p.add_argument("--dem", required=True)
    p.add_argument("--out", help="output DXF path (or use --outdir)")
    p.add_argument("--outdir",
                   help="Group this run in its own folder under DIR: creates "
                        "DIR/<lat>_<lon>_<extent>_<timestamp>/site.dxf")
    p.add_argument("--db", metavar="PATH",
                   help="Also stage the extracted features into a SQLite "
                        "database with CAD label anchors precomputed "
                        "(see scripts/stage_db.py)")
    p.add_argument("--project", metavar="NAME",
                   help="Project name for the staging database "
                        "(default: the coordinate and extent)")
    p.add_argument("--sheet", choices=["A4", "A3", "A2", "A1", "A0"],
                   help="Add a plottable paper-space layout at this sheet "
                        "size, with a title block and a viewport at --scale")
    p.add_argument("--scale", default="fit",
                   help="Plot scale denominator for --sheet (1:SCALE), or "
                        "'fit' to pick the largest round scale that shows "
                        "the whole extent")
    p.add_argument("--underlay", metavar="RASTER",
                   help="Attach a georeferenced image (GeoTIFF) as a tracing "
                        "underlay at true scale, beneath the linework. Use "
                        "this where OSM and the ML footprints have nothing "
                        "and the buildings have to be traced from imagery "
                        "you own. Must already be in the drawing's UTM CRS.")
    p.add_argument("--basemap", nargs="?", const="osm", metavar="PROVIDER",
                   help="Fetch a background map for the extent and place "
                        "it beneath the linework: osm, opentopomap, "
                        "esri-topo, esri-imagery, esri-street, carto-light, "
                        "carto-dark, carto-voyager, osm-hot, cyclosm, or a "
                        "{z}/{x}/{y} tile URL template. Written as "
                        "basemap.tif beside the drawing — the DXF stores a "
                        "path, so keep the pair together. Not staged: a "
                        "db2dxf.py re-issue draws the linework alone.")
    p.add_argument("--basemap-zoom", type=int, metavar="Z",
                   help="Force a tile zoom instead of the sharpest one "
                        "inside the tile cap")
    p.add_argument("--basemap-max-tiles", type=int, default=128, metavar="N",
                   help="Tile budget for the background map (default 128); "
                        "the zoom steps down until the extent fits")
    p.add_argument("--mono", action="store_true",
                   help="Monochrome: every layer on ACI 7, which plots black "
                        "on white and shows white on a dark model space. The "
                        "แผนที่สังเขป schematic look; lineweights still "
                        "separate a trunk road from a footpath.")
    p.add_argument("--no-ml", action="store_true",
                   help="Do not supplement with Microsoft ML building "
                        "footprints. The default adds every ML footprint "
                        "OSM has no building for, because a missing building "
                        "is a worse error on a site plan than one whose "
                        "outline came from a model — the inventory CSV "
                        "records the source of each either way.")
    p.add_argument("--all-poi", action="store_true",
                   help="Draw every amenity/tourism/historic feature instead "
                        "of only the civic landmarks a submission needs. At a "
                        "dense site this is mostly restaurants and cafes: 144 "
                        "landmark points instead of 9 over 770 x 410 m in "
                        "central Bangkok.")
    p.add_argument("--all-features", action="store_true",
                   help="Draw everything OpenStreetMap has in the extent, "
                        "not the curated tag list: whatever no rule claims "
                        "lands on C-MISC-OTHR / C-MISC-SYMB rather than "
                        "being dropped. The run reports what that added, by "
                        "tag, so you can see what the default skips.")
    p.add_argument("--refresh-osm", action="store_true",
                   help="Ignore the cached Overpass response for this "
                        "extent and query again. The cache is a day old at "
                        "most and exists so a retry, a re-plot or a repeat "
                        "run costs nothing — this is for when you know the "
                        "map changed today.")
    p.add_argument("--overture", action="store_true",
                   help="Supplement the landmarks with named places from "
                        "Overture Maps (Meta, Microsoft, Esri and others "
                        "conflated). They land on C-ANNO-OVTR with their "
                        "source and confidence as XDATA, kept off the OSM "
                        "layers so a drafter can see which names came from "
                        "a commercial feed and freeze them in one click.")
    p.add_argument("--overture-confidence", type=float, default=None,
                   metavar="F",
                   help="Drop Overture places below this confidence "
                        "(default 0.9). Overture scores each place; below "
                        "0.9 a dense extent returns hundreds of shop units.")
    p.add_argument("--all-places", action="store_true",
                   help="Keep every Overture category, not only the civic "
                        "landmarks: at Siam Square that is 1,797 places "
                        "instead of 29, mostly restaurants and boutiques.")
    p.add_argument("--grid", nargs="?", const="auto", metavar="SPACING",
                   help="Draw a UTM coordinate grid: crosses at every "
                        "SPACING metres with the easting and northing "
                        "written along two edges. Bare --grid picks a round "
                        "interval giving about six lines across the extent.")
    p.add_argument("--contour-interval", type=float, metavar="M",
                   help="Force the contour interval in metres instead of "
                        "letting the DEM's own range pick one (~10 levels). "
                        "A deliverable that specifies 0.5 m contours needs "
                        "this; the automatic choice is for a first look.")
    p.add_argument("--no-spots", action="store_true",
                   help="Do not sample spot heights off the DEM. The default "
                        "writes a 5 x 5 grid of levelled points on "
                        "C-TOPO-SPOT: contours give the shape of the ground, "
                        "a spot height gives a number to level to.")
    p.add_argument("--hatch", action="store_true",
                   help="Hatch water and vegetation areas with the CAD "
                        "patterns a drafter expects, instead of leaving them "
                        "as outlines. Off by default: a hatch at 1:5000 on a "
                        "dense site is a lot of ink.")
    p.add_argument("--no-attributes", action="store_true",
                   help="Do not attach the source OSM tags to each entity as "
                        "XDATA, and do not write attributes.csv. The default "
                        "carries them, so a drafter can LIST a building and "
                        "read the tags it was drawn from.")
    p.add_argument("--names-only", action="store_true",
                   help="Label only buildings that carry an OSM name. The "
                        "default also labels unnamed footprints with their "
                        "B### inventory code — without it, areas where OSM "
                        "has no building names come out entirely unlabelled.")
    a = p.parse_args()
    if not a.out and not a.outdir:
        p.error("give either --out <file.dxf> or --outdir <dir>")
    # Default extent: 1000 x 750 m. Note this needs 500 mm of paper at
    # 1:2000 against sheet.py's 290 mm A3 viewport, so on the default sheet
    # it lands at 1:5000 — context rather than a site plan. Narrow it, or
    # plot it larger than A3, when the deliverable wants 1:1000; sheet.py
    # picks the scale and warns if the sheet cannot hold it.
    # An explicit --radius still wins, so square boxes keep working.
    if a.radius is None and a.width is None and a.height is None:
        a.width, a.height = 1000.0, 750.0
    if a.radius is not None and a.width is None and a.height is None:
        pass          # radius-only run: bbox_around uses the radius
    if a.outdir:
        extent = (f"{a.width:.0f}x{a.height:.0f}" if a.width and a.height
                  else f"r{a.radius:.0f}")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run = Path(a.outdir) / f"{a.lat:.6f}_{a.lon:.6f}_{extent}_{stamp}"
        run.mkdir(parents=True, exist_ok=True)
        a.out = str(run / "site.dxf")
        print(f"Run folder: {run}")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    return a


def utm_epsg_for(lat, lon):
    """UTM zone EPSG for a coordinate. Thailand spans 47N and 48N, and a
    site east of 102°E projected into 47N carries real scale error, so the
    zone is derived rather than assumed."""
    zone = min(max(int((lon + 180) // 6) + 1, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


def utm_transformer(lat, lon):
    """Returns (transformer to UTM, epsg, human label like '48N')."""
    epsg = utm_epsg_for(lat, lon)
    zone = epsg - (32600 if lat >= 0 else 32700)
    label = f"{zone}{'N' if lat >= 0 else 'S'}"
    return (Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True),
            epsg, label)


# What the elevation source can actually support.
#
# The Copernicus DEM posts are 1 arc-second apart — about 30 m on the
# ground in Thailand — so a 250 x 200 m site is 56 elevation samples, and
# every contour between them is interpolation. The vertical figure is the
# binding one: the Copernicus DEM Product Handbook specifies *relative*
# vertical accuracy better than 2 m on slopes of 20% or less, which is the
# right number for contours because a contour expresses shape across a
# site rather than height above a datum. (Absolute accuracy is < 4 m LE90,
# and measures ~7.7 m LE90 against airborne LiDAR.)
#
# So the automatic interval never goes below 2 m. It used to start at 0.5
# m and pick whatever gave about ten levels, which on the flat Thai central
# plain meant 0.5 m contours drawn from data that cannot resolve 2 m — a
# submission drawing asserting shape nobody measured. --contour-interval
# still forces a finer one, with a warning, because a deliverable that
# specifies an interval is someone making that call deliberately.
DEM_GROUND_SAMPLE_M = 30.0


DEM_MIN_CONTOUR_M = 2.0


CONTOUR_INTERVALS = (0.5, 1, 2, 5, 10, 20, 50)


def auto_contour_interval(span, floor: float = DEM_MIN_CONTOUR_M,
                          max_levels: int = 12) -> float:
    """The finest round interval that is honest about `span` metres of relief.

    Shared with mapposter.py rather than restated there: a poster and a
    drawing of one site must not disagree about how much terrain detail the
    DEM supports. Returns the coarsest interval when even the largest would
    draw too many lines, which is the same thing the loop did before.
    """
    for interval in CONTOUR_INTERVALS:
        if interval >= floor and span / interval <= max_levels:
            return interval
    return CONTOUR_INTERVALS[-1]


# ezdxf writes UTF-8 either way, but AutoCAD renders Thai as ??? unless the
# text style points at a font that carries the Thai block. THSarabunNew is
# the Thai government document standard; AutoCAD substitutes if it is not
# installed, which is still better than the SHX default that cannot render
# Thai at all.
# MTEXT background mask, as a multiple of the text height. 'canvas'
# means the drawing's own background, so a label crossing a building
# outline or a road edge cuts a clean hole rather than overprinting.
# Passing None here would REMOVE the mask, not add one.
BG_MASK_SCALE = 1.1


TEXT_STYLES = {
    "TH_STYLE": "THSarabunNew.ttf",
    "EN_STYLE": "arial.ttf",
}


# Annotation layer -> (ACI colour, text style) for the language split.
ANNO_STYLE = {
    "C-ANNO-TEXT": (2, "EN_STYLE"),      # neutral: codes, elevations, N, GPS
    "C-ANNO-TEXT-TH": (2, "TH_STYLE"),
    "C-ANNO-TEXT-EN": (7, "EN_STYLE"),
    # House numbers are digits and separators in either script, so they
    # take the Latin style and stay off both language layers — freezing
    # Thai or English must not blank the addresses.
    "C-ANNO-ADDR": (8, "EN_STYLE"),
    # An elevation is a number; it belongs with the neutral annotation
    "C-TOPO-SPOT": (8, "EN_STYLE"),
    "C-ANNO-GRID": (253, "EN_STYLE"),
    # Overture place names keep the language split inside their own layer
    # family, so freezing C-ANNO-OVTR* takes the symbols and the names with
    # it and never leaves a label pointing at nothing.
    "C-ANNO-OVTR": (214, "EN_STYLE"),
    "C-ANNO-OVTR-TH": (214, "TH_STYLE"),
    "C-ANNO-OVTR-EN": (214, "EN_STYLE"),
}


# Vertical gap between the English and Thai label of the same feature, as a
# multiple of char height. English sits above Thai.
LANG_OFFSET = 1.3


def add_text_styles(doc):
    """Register the Thai/Latin text styles on a fresh document. Safe to call
    twice — ezdxf raises if the style already exists."""
    for name, font in TEXT_STYLES.items():
        if name not in doc.styles:
            doc.styles.add(name, font=font)


def offset_along_normal(x, y, rotation_deg, distance):
    """Shift a label perpendicular to its own baseline.

    Road labels are rotated to read along the centreline, so a plain -Y
    nudge would drift off the road. This keeps the offset square to the
    text however it is rotated; at rotation 0 it reduces to +Y.
    """
    rad = math.radians(rotation_deg or 0.0)
    return (x - distance * math.sin(rad), y + distance * math.cos(rad))


def apply_mono(doc, keep=("0",)):
    """Force every layer to ACI 7 for a monochrome schematic sheet.

    ACI 7 renders black on a white paper layout and white in a dark model
    space, so one setting suits both. Lineweights are left alone: colour is
    what the แผนที่สังเขป style drops, while line weight is what still
    separates a trunk road from a footpath once it has gone.
    """
    for layer in doc.layers:
        if layer.dxf.name not in keep:
            layer.dxf.color = 7


def stage_to_db(a, utm_epsg, inventory, building_geoms, road_records,
                contours=(), contour_layers=None,
                poi_points=(), poi_areas=(), context=(), attributes=(),
                spots=(), merge=False):
    """Stage what was just drawn into the SQLite layer, with CAD label
    anchors precomputed so the drawing step is plain SELECTs.

    `merge` keeps what is already staged under this project name instead of
    replacing it, which is what an *import* wants: bringing in one feature
    type at a time from the same file, or adding a survey layer beside an
    extraction. Re-extracting a coordinate wants the opposite — the site is
    being refreshed, and last run's features must not linger — so
    `topo2cad.py` leaves it False and `gis2cad.py`/`osm2cad.py` set it.
    """
    from pyproj import Transformer
    from shapely.geometry import LineString, MultiLineString

    import stage_db

    to_wgs = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326",
                                  always_xy=True)
    extent = (f"{a.width:.0f}x{a.height:.0f}" if a.width and a.height
              else f"r{a.radius:.0f}")
    project = a.project or f"{a.lat:.6f}_{a.lon:.6f}_{extent}"

    conn = stage_db.connect(a.db)
    width = a.width or (a.radius * 2)
    height = a.height or (a.radius * 2)
    if merge:
        pid, existed = stage_db.get_or_create_project(
            conn, project, a.lat, a.lon, width, height, utm_epsg)
    else:
        pid, existed = stage_db.create_project(
            conn, project, a.lat, a.lon, width, height, utm_epsg), False

    b_rows = []
    for row in inventory:
        rings = building_geoms.get(row["feature_id"])
        if not rings:
            continue
        pts, holes = rings
        if len(pts) < 3:
            continue
        # Holes are staged with the polygon, so db2dxf.py's interiors loop
        # redraws the same courtyards rather than a solid footprint. Repaired
        # by the same helper the drawing step used, so the two agree.
        poly = stage_db.repaired_polygon(pts, holes)
        if poly.is_empty:
            continue
        b_rows.append({**row, "geom": poly})

    # Landmark areas ride in the same table: they need a polygon, an interior
    # label anchor and an area, which is what it stores. Their cad_layer is
    # what keeps them off C-BLDG-OUTL and out of the building inventory.
    n_sp = 0
    for rec in poi_areas:
        poly = stage_db.repaired_polygon(rec["geom_pts"])
        if poly.is_empty:
            continue
        b_rows.append({k: v for k, v in rec.items() if k != "geom_pts"}
                      | {"geom": poly, "cad_layer": LAYERS["site_poi"],
                         "source": "openstreetmap", "code": "",
                         "osm_name": rec.get("display_name", "")})
        n_sp += 1
    n_b = stage_db.stage_buildings(conn, pid, b_rows, to_wgs=to_wgs) - n_sp

    # osm2cad.py calls this with its own Namespace, so the factor is read
    # defensively: a caller that never resolved a sheet stages at 1:1000.
    n_p = stage_db.stage_pois(
        conn, pid, list(poi_points),
        label_dx=stage_db.POI_LABEL_DX * getattr(a, "anno_scale", 1.0))
    n_x = stage_db.stage_context(conn, pid, list(context))

    r_rows = []
    for rec in road_records:
        runs = [LineString(r) for r in rec["runs"] if len(r) >= 2]
        if not runs:
            continue
        geom = runs[0] if len(runs) == 1 else MultiLineString(runs)
        r_rows.append({**rec, "geom": geom})
    n_r = stage_db.stage_roads(conn, pid, r_rows)

    c_rows = [{"elevation_m": lev, "geom": LineString(pts),
               "cad_layer": contour_layers.get(lev, "C-TOPO-MINR")}
              for lev, pts in contours if len(pts) >= 2]
    n_c = stage_db.stage_contours(conn, pid, c_rows)

    # The source tags travel with the drawing, so db2dxf.py can re-attach
    # the same XDATA instead of re-issuing a drawing stripped of it.
    n_t = stage_db.stage_tags(conn, pid, list(attributes))
    n_sp_h = stage_db.stage_spots(conn, pid, list(spots))

    labels = conn.execute("SELECT COUNT(*) FROM cad_labels WHERE"
                          " project_id = ?", (pid,)).fetchone()[0]
    # Only a merge reports them, so only a merge pays for the counts
    totals = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id = ?",
                              (pid,)).fetchone()[0]
              for t in ("staging_buildings", "staging_roads")} if existed \
        else {}
    conn.close()
    verb = "Merged into" if existed else "Staged to"
    print(f"{verb} {a.db}: project '{project}' (id {pid}) — "
          f"{n_b} buildings, {n_r} roads, {n_c} contours, "
          f"{n_p} POI points, {n_sp} POI areas, {n_x} context, "
          f"{n_t} tags, {n_sp_h} spot heights, "
          f"{labels} CAD labels ready")
    if existed:
        # A re-issue draws everything staged, not just this import
        print(f"  project now holds {totals['staging_buildings']} buildings "
              f"and {totals['staging_roads']} roads in total")


def main():
    import numpy as np
    import rasterio
    from rasterio.windows import from_bounds
    from rasterio.transform import xy as px2geo
    from scipy.ndimage import gaussian_filter
    from skimage import measure
    import ezdxf
    from ezdxf.enums import MTextEntityAlignment
    from shapely.geometry import LineString, MultiLineString

    # The staging layer owns the label-anchor rules; both CAD routes call
    # the same function so a drawing and its re-issue agree on placement.
    import stage_db as _anchor_rules

    a = parse_args()
    s, w, n, e = bbox_around(a.lat, a.lon, a.radius, a.width, a.height)
    to_utm, utm_epsg, utm_label = utm_transformer(a.lat, a.lon)
    to_wgs = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326",
                                  always_xy=True)
    print(f"Projected CRS: EPSG:{utm_epsg} (UTM {utm_label})")

    # ---- DEM -> contours -------------------------------------------------
    with rasterio.open(a.dem) as src:
        win = from_bounds(w, s, e, n, src.transform)
        dem = src.read(1, window=win).astype(float)
        wtrans = src.window_transform(win)
        nodata = src.nodata
    if nodata is not None:
        dem[dem == nodata] = np.nan
    print(f"DEM window: {dem.shape}, elev min/max = {np.nanmin(dem):.1f}/{np.nanmax(dem):.1f} m")

    smooth = gaussian_filter(dem, sigma=1.5)
    lo, hi = np.nanpercentile(smooth, [2, 98])
    span = max(hi - lo, 1.0)
    # How much ground each contour is actually built from. 250 x 200 m is
    # 56 elevation posts, and a drawing does not say so anywhere: the lines
    # come out looking exactly like surveyed contours.
    print(f"DEM: {dem.size} post(s) across the extent at "
          f"~{DEM_GROUND_SAMPLE_M:g} m spacing")
    # pick a "nice" interval giving ~10 levels, but never finer than the
    # source's own accuracy — see DEM_MIN_CONTOUR_M.
    interval = a.contour_interval or 0.0
    if interval <= 0:
        interval = auto_contour_interval(span)
    elif interval < DEM_MIN_CONTOUR_M:
        print(f"WARNING: a {interval:g} m interval is finer than the "
              f"{DEM_GROUND_SAMPLE_M:g} m DEM's stated relative vertical "
              f"accuracy ({DEM_MIN_CONTOUR_M:g} m); the shape between "
              "contours is interpolation, not measurement. Drawing them "
              "because you asked for them.")
    elif span / interval > 400:
        # 0.1 m contours over 40 m of relief is 400 lines nobody can read
        print(f"WARNING: a {interval:g} m interval over {span:.0f} m of "
              "relief would draw hundreds of contours; drawing them anyway.")
    levels = np.arange(math.floor(lo / interval) * interval,
                       math.ceil(hi / interval) * interval + interval, interval)
    print(f"Contour interval: {interval} m ({len(levels)} levels)")

    contours = []  # (elev, [(x_utm, y_utm), ...])
    for lev in levels:
        for seg in measure.find_contours(smooth, lev):
            if len(seg) < 3:
                continue
            rows, cols = seg[:, 0], seg[:, 1]
            xs, ys = px2geo(wtrans, rows, cols)
            ux, uy = to_utm.transform(xs, ys)
            contours.append((float(lev), list(zip(ux, uy))))

    # ---- OSM buildings + roads ------------------------------------------
    print("Fetching OSM data (Overpass)...")
    elements = fetch_osm(s, w, n, e, everything=a.all_features,
                          cache=not a.refresh_osm)
    # The tag rules live in classify_elements() so osm2cad.py's file route
    # sorts a downloaded extract into exactly the same categories.
    features = classify_elements(elements, curated=not a.all_poi,
                                 keep_other=a.all_features)
    buildings, roads = features["buildings"], features["roads"]
    water, green = features["water"], features["green"]
    rails, barriers = features["rails"], features["barriers"]
    pois, site_pois = features["pois"], features["site_pois"]
    power, pipelines = features["power"], features["pipelines"]
    point_marks = features["points"]
    zoning, parking = features["zoning"], features["parking"]
    plazas = features["plazas"]
    other_lines = features["other_lines"]
    other_points = features["other_points"]
    print(f"OSM: {len(buildings)} buildings, {len(roads)} roads, {len(water)} water, "
          f"{len(green)} green, {len(rails)} rail, {len(barriers)} barriers, "
          f"{len(pois)} POI points, {len(site_pois)} POI areas, "
          f"{len(power)} power, {len(pipelines)} pipeline, "
          f"{len(point_marks)} pylon/tree/gate, {len(zoning)} land-use, "
          f"{len(parking)} parking, {len(plazas)} plaza")

    if a.all_features:
        import collections as _collections
        kinds = _collections.Counter(
            [p[4] for p in other_points]
            + [_first_tag(source_tags(elements).get(f[2], {}))
               for f in other_lines])
        summary = ", ".join(f"{k}×{n}" for k, n in kinds.most_common(8))
        print(f"All features: {len(other_lines)} extra line(s), "
              f"{len(other_points)} extra point(s)"
              + (f" — {summary}" if summary else ""))

    ml_tags = {}
    if not a.no_ml:
        # Always supplement, not only when OSM is nearly empty. The old
        # "fewer than 20" rule meant a mapped area got OSM alone: at Pathum
        # Wan that drew 274 buildings while 64 further ML footprints sat on
        # ground OSM has nothing for. A building missing from a site plan is
        # a worse error than one whose outline came from a model.
        print("Supplementing with Microsoft ML footprints...")
        ms_cache = Path(a.dem).parent / "ms_cache"
        ms = fetch_ms_buildings(s, w, n, e, ms_cache)
        added = merge_ml_footprints(buildings, ms)
        if added:
            ml_tags = ms_source_tags(s, w, n, e, ms_cache)
        print(f"MS footprints: {len(ms)} available, {added} added, "
              f"{len(ms) - added} already mapped in OSM")

    # ---- DXF -------------------------------------------------------------
    doc = ezdxf.new("R2010", setup=_anchor_rules.DXF_SETUP)
    msp = doc.modelspace()
    # (layer, color, lineweight 1/100 mm) — roads/buildings heavy, context thin
    add_text_styles(doc)
    for key, color, lw in [("contour_plain", 8, 13),
                           ("contour_major", 8, 25), ("contour_minor", 8, 9),
                           ("building", 4, 50),
                           ("building_unnamed", 254, 35), ("anno", 2, 25),
                           ("anno_th", 2, 25), ("anno_en", 7, 25),
                           ("road_edge", 30, 35), ("road_centre", 8, 9),
                           ("road_path", 8, 13), ("road_arrow", 30, 18),
                           ("road_bridge", 7, 40), ("road_tunnel", 8, 18),
                           ("water", 5, 18), ("water_bank", 5, 25),
                           ("green", 3, 13),
                           ("rail", 250, 18), ("barrier", 9, 13),
                           ("poi", 6, 18), ("site_poi", 5, 25),
                           ("power", 6, 25), ("pipeline", 4, 18),
                           ("tree", 3, 13), ("addr", 8, 13),
                           ("spot", 8, 18), ("zoning", 32, 13),
                           ("grid", 253, 9), ("dims", 2, 18),
                           ("plaza", 8, 18), ("lamp", 51, 13),
                           ("other", 9, 9), ("other_point", 9, 9),
                           ("parking", 140, 13),
                           ("overture", 214, 13), ("overture_th", 214, 18),
                           ("overture_en", 214, 18),
                           ("extent", 7, 35),
                           ("north", 7, 35), ("site", 1, 35)]:
        layer = doc.layers.add(LAYERS[key], color=color)
        layer.dxf.lineweight = lw
    # Site-plan layers, empty and ready to draw on (OSM has no private parcels):
    prop = doc.layers.add(LAYERS["property"], color=1, linetype="PHANTOM")
    prop.dxf.lineweight = 70
    setb = doc.layers.add(LAYERS["setback"], color=2, linetype="DASHED")
    setb.dxf.lineweight = 25
    corner = doc.layers.add(LAYERS["corner"], color=1)
    corner.dxf.lineweight = 25
    row = doc.layers.add(LAYERS["road_row"], color=1, linetype="PHANTOM")
    row.dxf.lineweight = 35
    # NCS convention: a centreline is drawn with the CENTER linetype so it is
    # never mistaken for the edge of pavement beside it.
    doc.layers.get(LAYERS["road_centre"]).dxf.linetype = "CENTER"
    # Dashed, so the crop line cannot be mistaken for a fence, a wall or a
    # property boundary — it is a limit of extent, not surveyed geometry.
    doc.layers.get(LAYERS["extent"]).dxf.linetype = "DASHED"
    # A tunnel is under the ground this plan describes, so it plots hidden
    doc.layers.get(LAYERS["road_tunnel"]).dxf.linetype = "HIDDEN"
    # Dashes are in drawing units — metres here — so without a scale the
    # CENTER pattern is sub-millimetre on paper and reads as continuous.
    doc.header["$LTSCALE"] = 5.0

    # Source attributes: the OSM tags ride on each entity as extended data,
    # and the same rows are written beside the drawing and staged, so a
    # db2dxf.py re-issue can put them back.
    tag_index = source_tags(elements)
    drawn = []

    # The plot scale has to be known before a single label is written, not
    # when the sheet is added at the end: annotation is sized in metres of
    # ground and only the scale says what that is on paper.
    if a.sheet:
        import sheet as _sheet
        ext_w = a.width or a.radius * 2
        ext_h = a.height or a.radius * 2
        if str(a.scale).lower() == "fit":
            a.scale, _, _ = _sheet.fitting_scale(ext_w, ext_h, a.sheet)
        else:
            a.scale = int(a.scale)
        anno = _anchor_rules.annotation_scale(a.scale)
        if anno != 1.0:
            print(f"Annotation scaled x{anno:g} for 1:{a.scale:,} — a "
                  f"{3.5 * anno:.0f} m label is 3.5 mm on the sheet")
    else:
        anno = 1.0
    # stage_to_db() runs well after this and needs the same factor, so it
    # travels on the args rather than through six call signatures.
    a.anno_scale = anno

    # Which XDATA application id a feature's attributes belong under. The
    # id is the honest answer to "where did this come from" in the CAD
    # attribute browser, so a modelled footprint must not file its
    # provenance under OSM — the same reason gis2cad.py has its own.
    appid_index = {}

    def appid_for(fid):
        return appid_index.get(fid, _anchor_rules.XDATA_APPID)

    # A modelled footprint carries no OSM tags, so it used to reach the
    # drawing carrying nothing at all: 49 of 126 entities at this site with
    # no way to tell a predicted outline from a traced one once both are
    # black polylines. Its provenance is the same for every footprint in a
    # run — one release of one region — so it is built once and shared.
    for _names, _geom, fid in buildings:
        if fid.startswith("ms/") and ml_tags:
            tag_index[fid] = ml_tags
            appid_index[fid] = _anchor_rules.MS_XDATA_APPID

    if not a.no_attributes:
        doc.appids.add(_anchor_rules.XDATA_APPID)

    def attach(entity, fid):
        tags = tag_index.get(fid) or tag_index.get(fid.rsplit("/", 1)[0], {})
        if not a.no_attributes and entity is not None and tags:
            appid = appid_for(fid)
            if appid not in doc.appids:
                doc.appids.add(appid)
            entity.set_xdata(appid, _anchor_rules.xdata_tags(fid, tags))

    def record(fid, kind, layer, name):
        drawn.append({"feature_id": fid, "feature_type": kind,
                      "cad_layer": layer, "display_name": name or "",
                      "appid": appid_for(fid)})

    if a.underlay:
        import underlay as ul
        try:
            print(ul.describe(ul.attach(doc, msp, a.underlay, utm_epsg,
                                        dxf_path=a.out)))
        except ul.UnderlayError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    def mtext(label, x, y, height, rotation=0.0, layer=None):
        """MTEXT anchored Middle Center on the annotation layer, so the
        label grows symmetrically about its insertion point. `layer`
        selects the language sub-layer; it defaults to the neutral one."""
        layer = layer or LAYERS["anno"]
        m = msp.add_mtext(str(label), dxfattribs={
            "layer": layer, "char_height": height * anno,
            "style": ANNO_STYLE[layer][1]})
        m.set_location((x, y), rotation=rotation,
                       attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
        # Background mask: a label crossing a building outline or a road
        # edge punches a clean hole through it instead of overprinting.
        m.set_bg_color("canvas", scale=BG_MASK_SCALE)
        return m

    def mtext_bilingual(th, en, x, y, height, rotation=0.0, fallback=None,
                        family=("anno_th", "anno_en")):
        """Write the Thai and English labels of one feature onto their own
        layers, English stacked above Thai when both exist. Falls back to
        `fallback` (a B### code) on the neutral layer when neither name is
        known. Returns the number of MTEXT entities written.

        `family` names the pair of LAYERS keys to write onto: Overture
        places use their own so a drafter freezing the third-party source
        loses the names with the symbols rather than either alone.
        """
        n = 0
        if th:
            mtext(th, x, y, height, rotation, LAYERS[family[0]])
            n += 1
        if en:
            ex, ey = ((x, y) if not th else
                      offset_along_normal(x, y, rotation,
                                          height * anno * LANG_OFFSET))
            mtext(en, ex, ey, height, rotation, LAYERS[family[1]])
            n += 1
        if not n and fallback:
            mtext(fallback, x, y, height, rotation)
            n += 1
        return n

    # True 3D polylines (Z per vertex), so Civil 3D can build a surface from
    # them; every 5th contour is an index contour, drawn heavier and labelled.
    index_step = interval * 5
    contour_layers = {}
    for lev, pts in contours:
        major = abs(lev / index_step - round(lev / index_step)) < 1e-6
        layer = LAYERS["contour_major" if major else "contour_minor"]
        contour_layers[lev] = layer
        msp.add_polyline3d([(x, y, lev) for x, y in pts],
                           dxfattribs={"layer": layer})
        if major:
            # Same anchor and rotation the staging layer computes, or the
            # elevation shifts ~20 m between a drawing and its re-issue
            mx, my, rot = _anchor_rules.line_label_anchor(LineString(pts))
            if mx is not None:
                mtext(f"{lev:g}", mx, my, 2.5, rotation=rot)

    # Spot heights: the DEM sampled on a grid inset from the extent, so a
    # reviewer can read levels across the site rather than interpolating
    # between contours. Staged, because db2dxf.py has no DEM to sample.
    staged_spots = []
    if not a.no_spots:
        cx0, cy0 = to_utm.transform(w, s)
        cx1, cy1 = to_utm.transform(e, n)
        with rasterio.open(a.dem) as src:
            for gx, gy in _anchor_rules.spot_grid(cx0, cy0, cx1, cy1):
                glon, glat = to_wgs.transform(gx, gy)
                try:
                    value = next(src.sample([(glon, glat)]))[0]
                except (StopIteration, IndexError):
                    continue
                if value is None or (src.nodata is not None
                                     and value == src.nodata):
                    continue
                elev = float(value)
                if not math.isfinite(elev):
                    continue
                msp.add_circle((gx, gy), radius=0.6,
                               dxfattribs={"layer": LAYERS["spot"]})
                # {:+.1f}, not "+" + the number: a point below datum
                # would otherwise be labelled "+-0.3"
                mtext(f"{elev:+.1f}", gx + 2.5 * anno, gy, 2.5,
                      layer=LAYERS["spot"])
                staged_spots.append({"x": gx, "y": gy, "elevation_m": elev})
        print(f"Spot heights: {len(staged_spots)} sampled from the DEM")

    # Buildings: outline, then a label centred inside every footprint —
    # its name when OSM has one, otherwise a B### code carried in the
    # inventory CSV so field teams can fill the name in later.
    inventory = []
    staged_geoms = {}
    counter = 0
    n_nofit = 0
    for (th, en), (ext, holes), fid in sorted(buildings, key=lambda b: b[2]):
        ux, uy = to_utm.transform(*zip(*ext))
        upts = list(zip(ux, uy))
        uholes = []
        for hole in holes:
            hx, hy = to_utm.transform(*zip(*hole))
            uholes.append(list(zip(hx, hy)))
        name = th or en
        code = ""
        if not name:
            counter += 1
            code = f"B{counter:03d}"
        # An unnamed footprint still gets a label: its B### inventory code,
        # which is the handle building_inventory.csv is keyed on and the one
        # a field crew writes a verified name against. Without it the CSV
        # numbers nothing a reader can find on the sheet, and where OSM
        # names no buildings — 0 of 239 at Yasothon, 0 of 49 here — the
        # whole building layer comes out mute. --names-only opts out.
        # The footprint is still drawn on its own layer either way, so the
        # sheet says plainly which buildings the source actually identified.
        b_layer = LAYERS["building" if name else "building_unnamed"]
        # Drawn from the repaired geometry, which is also what gets staged:
        # a self-intersecting ring becomes two polygons under buffer(0), and
        # drawing the raw ring while staging the repaired one gave a drawing
        # one outline where its re-issue had two.
        shape = _anchor_rules.repaired_polygon(upts, uholes)
        first = True
        for part in _anchor_rules.polygon_parts(shape):
            entity = msp.add_lwpolyline(
                _anchor_rules.ring_points(part.exterior.coords),
                close=True,
                dxfattribs={"layer": b_layer})
            if first:
                attach(entity, fid)
                first = False
            # Courtyards stay open: each inner ring is its own closed
            # polyline on the same layer, which is how a CAD island reads.
            for ring in part.interiors:
                msp.add_lwpolyline(_anchor_rules.ring_points(ring.coords),
                                   close=True,
                                   dxfattribs={"layer": b_layer})
        # ST_Centroid-style centroids fall outside concave footprints (~3% of
        # buildings in a dense extent), so anchor on a guaranteed interior
        # point instead — equivalent to PostGIS ST_PointOnSurface. It is the
        # staging layer's own call, on the same shape, so a re-issue agrees.
        try:
            cx, cy = _anchor_rules.interior_point(shape)
        except Exception:
            cx, cy = float(np.mean(ux)), float(np.mean(uy))
        # The code rides in on `fallback`, so it lands on the neutral
        # layer at the same anchor and height cad_labels gives it — a
        # re-issue has to put it in the same place. It is also dropped
        # where the footprint has no room for it at the plotted size: the
        # same test db2dxf.py applies to the same box, since a code bigger
        # than its building is noise and the inventory CSV keeps them all.
        fallback = None
        if code and not a.names_only:
            bx0, by0, bx1, by1 = shape.bounds
            if _anchor_rules.label_fits(code, 3.5 * anno,
                                        bx1 - bx0, by1 - by0):
                fallback = code
            else:
                n_nofit += 1
        mtext_bilingual(th, en, cx, cy, 3.5, fallback=fallback)
        # House number under the label, small and language-neutral — the
        # same row cad_labels emits, at the same offset, so a re-issue puts
        # it in the same place.
        btags = tag_index.get(fid) or {}
        house = btags.get("addr:housenumber")
        if house:
            hx, hy = offset_along_normal(cx, cy, 0.0, -3.0 * anno)
            mtext(house, hx, hy, 2.2, layer=LAYERS["addr"])
        # Storeys under the number, at the offset cad_labels uses
        levels = _anchor_rules.levels_label(btags)
        if levels:
            lx2, ly2 = offset_along_normal(cx, cy, 0.0, -5.4 * anno)
            mtext(levels, lx2, ly2, 2.2, layer=LAYERS["addr"])
        staged_geoms[fid] = (upts, uholes)
        record(fid, "building", b_layer, name or "")
        blon, blat = to_wgs.transform(cx, cy)
        inventory.append({"feature_id": fid, "code": code,
                          "osm_name": name or "", "display_name": name or "",
                          "cad_layer": b_layer,
                          "name_th": th or "", "name_en": en or "",
                          "addr_house": house or "",
                          "levels_label": levels,
                          "source": "openstreetmap" if not fid.startswith("ms/")
                          else "microsoft_ml",
                          "latitude": round(blat, 8),
                          "longitude": round(blon, 8)})

    if n_nofit:
        print(f"Building codes: {n_nofit} left off footprints too small to "
              f"hold them at this scale — all are in the inventory CSV")

    # Roads: both carriageway edges (CAD convention) plus a thin centreline,
    # labelled once per unique name with its route number.
    staged_roads = []
    import blocks as _blocks
    # The kerb lines are trimmed against each other, so the whole network
    # has to be projected and clipped before any of it is drawn. A run is
    # keyed by (feature id, index): one way clipped into two runs is two
    # separate carriageways as far as the trim is concerned.
    plan = []
    for (th, en), ref, pts, highway, fid, oneway in roads:
        name = th or en
        road_tags = tag_index.get(fid) or {}
        # Measured where OSM has it, guessed by class only where it does not
        width_m = carriageway_width(road_tags, highway)
        is_path = highway in PATH_TYPES
        cad_layer = road_cad_layer(road_tags, highway)
        # The formal designation — ถนนพระรามที่ ๑ — which OSM keeps apart
        # from the everyday name. It goes in the road inventory either way,
        # and stands in as the label where the road has no plain name, so a
        # highway that OSM only names officially stops drawing as a bare
        # number.
        official = (road_tags.get("official_name:th")
                    or road_tags.get("official_name") or "")
        if not (th or en) and official:
            th, en = names_by_lang({"name": official})
            name = th or en     # recomputed: the fallback just supplied one
        road_runs = []
        for i, run in enumerate(clip_runs(pts, s, w, n, e)):
            ux, uy = to_utm.transform(*zip(*run))
            upts = list(zip(ux, uy))
            if len(upts) < 2:
                continue
            road_runs.append(upts)
            plan.append({"key": (fid, i), "pts": upts, "fid": fid,
                         "width_m": 0.0 if is_path else width_m,
                         "cad_layer": cad_layer, "is_path": is_path,
                         "oneway": oneway,
                         # Only a carriageway at grade takes part in the
                         # trim; a bridge crosses whatever is beneath it.
                         "at_grade": cad_layer == LAYERS["road_centre"]})
        if road_runs:
            record(fid, "path" if is_path else "road", cad_layer, name)
            staged_roads.append({
                "feature_id": fid, "highway_type": highway,
                "road_name": name, "road_ref": ref,
                "name_th": th or "", "name_en": en or "",
                "cad_layer": cad_layer, "oneway": oneway,
                "official_name": official,
                # 0 tells the staging route not to offset edges either
                "carriageway_m": 0.0 if is_path else width_m,
                "runs": road_runs})

    trimmed = _anchor_rules.carriageway_edges(
        [(r["key"], r["pts"], r["width_m"], r["at_grade"]) for r in plan])
    n_edges = 0
    for r in plan:
        for edge in trimmed.get(r["key"], ()):
            msp.add_lwpolyline(edge,
                               dxfattribs={"layer": LAYERS["road_edge"]})
            n_edges += 1
        attach(msp.add_lwpolyline(r["pts"],
                                  dxfattribs={"layer": r["cad_layer"]}),
               r["fid"])
        # Direction of travel, spaced along the run by the same rule
        # db2dxf.py applies to the staged geometry. Paths are excluded:
        # a one-way footpath is not a traffic instruction.
        if r["oneway"] and not r["is_path"]:
            size = _anchor_rules.oneway_arrow_size(r["width_m"])
            for ax, ay, rot in _anchor_rules.arrow_positions(r["pts"]):
                _blocks.add_oneway_arrow(
                    doc, msp, ax, ay, size,
                    rot + (180.0 if r["oneway"] < 0 else 0.0),
                    LAYERS["road_arrow"])
    if plan:
        print(f"Roads: {len(plan)} run(s), {n_edges} edge line(s) after "
              "trimming at the junctions")

    # Linear labels are anchored by the same function the staging layer uses,
    # so a drawing and its re-issue put the name in the same place. Anchoring
    # on the first clipped run instead moved names by up to 287 m on a 770 m
    # extent — a third of the sheet — because staging picks the longest.
    def runs_geom(runs):
        lines = [LineString(r) for r in runs if len(r) >= 2]
        if not lines:
            return None
        return lines[0] if len(lines) == 1 else MultiLineString(lines)

    def label_longest(records, key_of, emit):
        """One label per unique key, placed on the longest feature carrying
        it — the rule cad_labels applies with ROW_NUMBER ... ORDER BY
        length_m DESC."""
        best = {}
        for rec in records:
            key = key_of(rec)
            if not key:
                continue
            geom = runs_geom(rec["runs"])
            if geom is None:
                continue
            if key not in best or geom.length > best[key][0].length:
                best[key] = (geom, rec)
        for geom, rec in best.values():
            lx, ly, rot = _anchor_rules.line_label_anchor(geom)
            if lx is not None:
                emit(rec, lx, ly, rot)

    def emit_road_name(rec, x, y, rot):
        mtext_bilingual(rec["name_th"] or None, rec["name_en"] or None,
                        x, y, 5.0, rotation=rot)

    def emit_road_ref(rec, x, y, rot):
        # Mirrors the road_ref branch of cad_labels: the number always
        # carries the Thai 'ทล.' prefix, because a bare "311" beside a road
        # name reads as a distance, a lane count or a house number rather
        # than as the highway designation it is.
        th, en, name = rec["name_th"], rec["name_en"], rec["road_name"]
        text = f"ทล.{rec['road_ref']}"
        if not name:
            off = 0.0
        else:
            off = 6.0 + (5.0 * LANG_OFFSET if th and en else 0.0)
        # Every offset here is a distance on the sheet, not on the ground:
        # the number sits clear of the name stack above it, and that gap has
        # to grow with the text it is clearing.
        rx, ry = offset_along_normal(x, y, rot, off * anno)
        mtext(text, rx, ry, 4.0, rotation=rot,
              layer=LAYERS["anno_th" if is_thai(text) else "anno_en"])

    label_longest(staged_roads, lambda r: r["road_name"], emit_road_name)
    label_longest(staged_roads, lambda r: r["road_ref"], emit_road_ref)

    staged_context = []

    n_banks = [0]

    def draw_lines(features, kind, layer, label=False, text_h=4.0):
        """Context linework — canals, parks, railways, walls. Each feature
        may survive clipping as several runs; every run is staged so
        db2dxf.py can redraw the same polylines."""
        for (th, en), pts, fid in sorted(features, key=lambda f: f[2]):
            name = th or en
            # A watercourse is drawn with its banks as well as its
            # centreline, the way a carriageway is: a river as one line
            # says where the water runs and nothing about how wide it is,
            # and the bank is the edge a setback is measured from.
            width_m = (_anchor_rules.waterway_width(tag_index.get(fid) or {},
                                                    kind)
                       if kind == "water" else 0.0)
            # Whether the *source* was an area, before clipping. A pond or
            # a park that crosses the extent comes back from clip_runs as
            # open runs, and testing the clipped run for closure said "not
            # an area" about every area big enough to reach the edge —
            # which is most of them. It is still a pond.
            is_area = len(pts) >= 4 and pts[0] == pts[-1]
            runs = []
            for run in clip_runs(pts, s, w, n, e):
                ux, uy = to_utm.transform(*zip(*run))
                upts = list(zip(ux, uy))
                closed = run[0] == run[-1]
                attach(msp.add_lwpolyline(upts, close=closed,
                                          dxfattribs={"layer": layer}), fid)
                for bank in _anchor_rules.water_banks(upts, width_m):
                    msp.add_lwpolyline(
                        bank, dxfattribs={"layer": LAYERS["water_bank"]})
                    n_banks[0] += 1
                runs.append(upts)
            if runs:
                record(fid, kind, layer, name)
                staged_context.append({
                    "feature_id": fid, "kind": kind, "cad_layer": layer,
                    "name_th": th or "", "name_en": en or "",
                    "display_name": name or "", "labelled": bool(label),
                    "width_m": width_m, "is_area": is_area, "runs": runs})

    draw_lines(water, "water", LAYERS["water"], label=True)
    draw_lines(green, "green", LAYERS["green"], label=True)
    if n_banks[0]:
        print(f"Watercourse banks: {n_banks[0]} line(s) offset from the "
              "centreline onto C-HYDR-BANK")
    if a.hatch:
        # Closed runs only: an open canal centreline has no area to fill.
        # db2dxf.py hatches the same rows, recovering "closed" the same way.
        n_hatch = 0
        for rec in staged_context:
            if rec["kind"] not in _anchor_rules.HATCH_PATTERNS:
                continue
            if not rec.get("is_area"):
                continue        # an open canal centreline has no area
            for run in rec["runs"]:
                if len(run) >= 3:
                    # A run clipped at the extent is filled closed: the
                    # fill stops at the crop line, which is where the
                    # drawing stops.
                    _anchor_rules.hatch_area(msp, run, rec["kind"],
                                             rec["cad_layer"])
                    n_hatch += 1
        print(f"Hatched: {n_hatch} water/vegetation area(s)")
    draw_lines(rails, "rail", LAYERS["rail"])
    draw_lines(barriers, "barrier", LAYERS["barrier"])
    draw_lines(zoning, "zoning", LAYERS["zoning"], label=True)
    draw_lines(parking, "parking", LAYERS["parking"], label=True)
    draw_lines(plazas, "plaza", LAYERS["plaza"], label=True)
    draw_lines(other_lines, "other", LAYERS["other"], label=True)
    draw_lines(power, "power", LAYERS["power"])
    draw_lines(pipelines, "pipeline", LAYERS["pipeline"])

    # Context names dedupe within their own kind, matching the view's
    # PARTITION BY project_id, kind, display_name.
    # Flow direction on canals and rivers: the same spacing rule the road
    # arrows use, on the direction OSM digitised the way in.
    n_flow = 0
    for rec in staged_context:
        if rec["kind"] != "water":
            continue
        for run in rec["runs"]:
            if len(run) >= 2 and run[0] != run[-1]:      # a line, not a pond
                for ax, ay, rot in _anchor_rules.arrow_positions(run):
                    _blocks.add_oneway_arrow(
                        doc, msp, ax, ay, _anchor_rules.FLOW_ARROW_M, rot,
                        LAYERS["water"])
                    n_flow += 1
    if n_flow:
        print(f"Flow arrows: {n_flow} on waterways")

    label_longest(
        [r for r in staged_context if r["labelled"]],
        lambda r: (r["kind"], r["display_name"]) if r["display_name"] else None,
        lambda r, x, y, rot: mtext_bilingual(
            r["name_th"] or None, r["name_en"] or None, x, y, 4.0,
            rotation=rot))

    # Landmark areas: outline plus a name at a guaranteed interior point,
    # drawn before the point symbols so a symbol inside a campus stays on top
    staged_site_pois = []
    for (th, en), pts, fid, kind in sorted(site_pois, key=lambda p: p[2]):
        ux, uy = to_utm.transform(*zip(*pts))
        upts = list(zip(ux, uy))
        shape = _anchor_rules.repaired_polygon(upts)
        first = True
        for part in _anchor_rules.polygon_parts(shape):
            entity = msp.add_lwpolyline(
                _anchor_rules.ring_points(part.exterior.coords),
                close=True,
                dxfattribs={"layer": LAYERS["site_poi"]})
            if first:
                attach(entity, fid)
                first = False
            for ring in part.interiors:
                msp.add_lwpolyline(_anchor_rules.ring_points(ring.coords),
                                   close=True,
                                   dxfattribs={"layer": LAYERS["site_poi"]})
        try:
            cx, cy = _anchor_rules.interior_point(shape)
        except Exception:
            cx, cy = float(np.mean(ux)), float(np.mean(uy))
        mtext_bilingual(th, en, cx, cy, 3.5)
        record(fid, "landmark_area", LAYERS["site_poi"], th or en)
        staged_site_pois.append({"feature_id": fid, "poi_key": kind[0],
                                 "poi_type": kind[1], "name_th": th or "",
                                 "name_en": en or "",
                                 "display_name": th or en or "",
                                 "geom_pts": upts})

    staged_pois = []
    # Everything --all-features caught that no rule claimed. Named ones get
    # their name; the rest are a symbol saying something is there.
    for (th, en), plon, plat, fid, key in sorted(other_points,
                                                 key=lambda m: m[3]):
        px, py = to_utm.transform(plon, plat)
        layer = LAYERS["other_point"]
        attach(_blocks.add_symbol(doc, msp, px, py,
                                  _blocks.symbol_size(layer), layer), fid)
        # The POI label convention — POI_LABEL_DX across, 4.0 high —
        # because that is what stage_pois stores and cad_labels emits.
        # Drawing it at 2 m and 2.5 put every one of these 1 m from where
        # its re-issue drew it.
        # Named in OSM or not labelled at all — the symbol says something
        # is there, and a made-up title says something OSM never did.
        title = (th or en) or ""
        if title:
            mtext_bilingual(th, en, px + _anchor_rules.POI_LABEL_DX * anno,
                            py, 4.0)
        record(fid, key or "other", layer, title)
        staged_pois.append({"feature_id": fid, "poi_key": key or "other",
                            "poi_type": key or "", "name_th": th or "",
                            "name_en": en or "",
                            "display_name": title,
                            "cad_layer": LAYERS["other_point"],
                            "x": px, "y": py,
                            "latitude": plat, "longitude": plon})

    # Pylons, poles and trees: a symbol and nothing else. They stage in the
    # POI table with an empty display_name, which cad_labels already skips,
    # so db2dxf.py redraws the mark without inventing a label for it.
    for kind, plon, plat, fid, ptype in sorted(point_marks,
                                               key=lambda m: m[3]):
        px, py = to_utm.transform(plon, plat)
        layer = LAYERS[{"tree": "tree", "gate": "barrier",
                        "lamp": "lamp"}.get(kind, "power")]
        attach(_blocks.add_symbol(doc, msp, px, py,
                                  _blocks.symbol_size(layer), layer), fid)
        record(fid, kind, layer, "")
        staged_pois.append({"feature_id": fid,
                            "poi_key": {"tree": "natural",
                                        "gate": "barrier",
                                        "lamp": "highway"}.get(kind, "power"),
                            "poi_type": ptype, "name_th": "", "name_en": "",
                            "display_name": "", "cad_layer": layer,
                            "x": px, "y": py,
                            "latitude": plat, "longitude": plon})
    for (th, en), plon, plat, kind, fid in sorted(pois, key=lambda p: p[4]):
        px, py = to_utm.transform(plon, plat)
        import blocks
        attach(blocks.add_poi_symbol(doc, msp, px, py, 2.0, LAYERS["poi"]),
               fid)
        mtext_bilingual(th, en, px + _anchor_rules.POI_LABEL_DX * anno, py, 4.0)
        record(fid, "landmark", LAYERS["poi"], th or en)
        staged_pois.append({"feature_id": fid,
                            "poi_key": kind[0], "poi_type": kind[1],
                            "name_th": th or "", "name_en": en or "",
                            "display_name": th or en or "",
                            "x": px, "y": py,
                            "latitude": plat, "longitude": plon})

    # Overture places: a second opinion on what is here, kept visibly
    # separate. OSM is one community's view of a site; Overture conflates
    # Meta, Microsoft, Esri and others and scores each place, so it finds
    # names OSM never had — and carries names no one here can trace to a
    # survey. That is why the source and the confidence ride on every one of
    # them as XDATA and why they never touch the OSM annotation layers.
    if a.overture:
        import overture as _overture

        try:
            places, from_cache = _overture.fetch_places((s, w, n, e))
        except _overture.OvertureError as exc:
            print(f"WARNING: Overture unavailable: {exc}", file=sys.stderr)
            places, from_cache = [], False
        raw = len(places)
        floor = (a.overture_confidence if a.overture_confidence is not None
                 else _overture.DEFAULT_MIN_CONFIDENCE)
        places = _overture.filter_places(places, floor, not a.all_places)
        # Anything OSM already names is OSM's — the drawing must not carry
        # one shop twice under two sources, and where they agree the OSM
        # name is the one a Thai reviewer and a local mapper both wrote.
        known = ([(r["osm_name"], r["longitude"], r["latitude"])
                  for r in inventory if r["osm_name"]]
                 + [(p["display_name"], p["longitude"], p["latitude"])
                    for p in staged_pois if p["display_name"]])
        kept = _overture.drop_known(places, known)
        print(f"Overture: {len(kept)} place(s) added "
              f"({raw} in the extent, {raw - len(places)} outside the "
              f"landmark filter or under confidence {floor}, "
              f"{len(places) - len(kept)} already named in OSM)"
              + (" [cached]" if from_cache else ""))
        if kept and not a.no_attributes:
            doc.appids.add(_overture.XDATA_APPID)
        for place in sorted(kept, key=lambda p: p["id"]):
            fid = f"overture/{place['id']}"
            px, py = to_utm.transform(place["lon"], place["lat"])
            layer = LAYERS["overture"]
            entity = _blocks.add_symbol(doc, msp, px, py,
                                        _blocks.symbol_size(layer), layer)
            tags = _overture.place_tags(place)
            tag_index[fid] = tags
            if not a.no_attributes and entity is not None:
                entity.set_xdata(_overture.XDATA_APPID,
                                 _anchor_rules.xdata_tags(fid, tags))
            th, en = _anchor_rules.split_by_script(place["name"])
            mtext_bilingual(th, en, px + _anchor_rules.POI_LABEL_DX * anno, py, 4.0,
                            family=("overture_th", "overture_en"))
            drawn.append({"feature_id": fid, "feature_type": "overture_place",
                          "cad_layer": layer, "display_name": place["name"],
                          "appid": _overture.XDATA_APPID})
            staged_pois.append({"feature_id": fid, "source": "overture",
                                "poi_key": "place",
                                "poi_type": place["category"],
                                "name_th": th or "", "name_en": en or "",
                                "display_name": place["name"],
                                "cad_layer": layer, "x": px, "y": py,
                                "latitude": place["lat"],
                                "longitude": place["lon"]})

    # North arrow at top-right corner (drawing is true-north-up in UTM).
    # Sized from the nominal extent rather than the projected bbox corners,
    # because db2dxf.py only has the nominal metres to work from and the two
    # differ by ~2 m — bbox_around approximates a degree as 111,320 m.
    cx, cy = to_utm.transform(a.lon, a.lat)
    ext_w = a.width or (a.radius * 2)
    ext_h = a.height or (a.radius * 2)
    # Crop rectangle on the requested extent
    hw, hh = ext_w / 2, ext_h / 2
    msp.add_lwpolyline([(cx - hw, cy - hh), (cx + hw, cy - hh),
                        (cx + hw, cy + hh), (cx - hw, cy + hh)],
                       close=True, dxfattribs={"layer": LAYERS["extent"]})
    ax_ = cx + (ext_w / 2) * 0.94
    ay = cy + (ext_h / 2) * 0.90
    sz = min(ext_w, ext_h) * 0.02
    import blocks
    if a.grid:
        spacing = (_anchor_rules.grid_spacing(ext_w, ext_h)
                   if str(a.grid) == "auto" else float(a.grid))
        eastings, northings = _anchor_rules.grid_ticks(cx, cy, ext_w, ext_h,
                                                       spacing)
        arm = min(ext_w, ext_h) * 0.006
        for gx in eastings:
            for gy in northings:
                msp.add_line((gx - arm, gy), (gx + arm, gy),
                             dxfattribs={"layer": LAYERS["grid"]})
                msp.add_line((gx, gy - arm), (gx, gy + arm),
                             dxfattribs={"layer": LAYERS["grid"]})
        # Written along the south and west edges, inside the crop line
        for gx in eastings:
            mtext(f"{gx:,.0f} E", gx, cy - ext_h / 2 + arm * 2, arm * 1.6,
                  layer=LAYERS["grid"])
        for gy in northings:
            mtext(f"{gy:,.0f} N", cx - ext_w / 2 + arm * 2, gy, arm * 1.6,
                  rotation=90.0, layer=LAYERS["grid"])
        print(f"Grid: {spacing:g} m, {len(eastings)} x {len(northings)} "
              "lines")

    blocks.add_extent_dimensions(doc, msp, cx, cy, ext_w, ext_h,
                                 LAYERS["dims"])
    blocks.add_north_arrow(doc, msp, ax_, ay, sz, LAYERS["north"])

    msp.add_circle((cx, cy), radius=5, dxfattribs={"layer": LAYERS["site"]})
    # Clear of the site marker by a distance on the sheet, not on the
    # ground: at 1:5000 a fixed 40 m puts a 25 m tag on top of it.
    mtext(f"GPS {a.lat},{a.lon}", cx + 40 * anno, cy, 5.0)

    if a.basemap:
        import basemap as bm
        try:
            info = bm.attach(doc, msp, (s, w, n, e), utm_epsg, a.out,
                             provider=a.basemap, zoom=a.basemap_zoom,
                             max_tiles=a.basemap_max_tiles)
        except bm.BasemapError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(bm.describe(info))
        # The credit rides on the basemap's own layer, so freezing the
        # backdrop removes the attribution with it rather than leaving a
        # drawing crediting a map it no longer shows.
        credit = msp.add_mtext(info["attribution"], dxfattribs={
            "layer": bm.LAYER, "char_height": max(2.0, ext_h * 0.006),
            "style": "EN_STYLE"})
        credit.set_location((cx, cy - hh + ext_h * 0.012),
                            attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
        credit.set_bg_color("canvas", scale=BG_MASK_SCALE)

    if a.sheet:
        import sheet as sheet_mod
        # The same credit line db2dxf.py derives from the staging layer,
        # built here from what was actually drawn — a re-issue of this
        # drawing must not come back with a different title block.
        drawn_sources = {"openstreetmap", "copernicus_dem"}
        drawn_sources.update(r["source"] for r in inventory)
        drawn_sources.update(p.get("source", "openstreetmap")
                             for p in staged_pois)
        sheet_mod.add_sheet(doc, {
            "source": _anchor_rules.credit_lines(drawn_sources),
            "project": a.project or Path(a.out).stem,
            "lat": a.lat, "lon": a.lon, "centre": (cx, cy),
            "srid": utm_epsg,
            "extent": (a.width or a.radius * 2, a.height or a.radius * 2),
            "date": time.strftime("%Y-%m-%d"),
        }, size=a.sheet, scale=a.scale)
        print(f"Sheet: {a.sheet} paper space at 1:{a.scale:,}")

    if a.mono:
        apply_mono(doc)
        print("Monochrome: all layers set to ACI 7")
    # ezdxf writes UTF-8 regardless; what decides whether a
    # reader sees the Thai is the font the STYLE points at.
    _anchor_rules.check_fonts(TEXT_STYLES,
                     Path(a.out).with_name("fonts.txt"))
    _anchor_rules.set_drawing_extents(doc)
    doc.saveas(a.out)
    print(f"Saved: {a.out}")

    # Building inventory beside the DXF: one row per drawn footprint, so a
    # B### code on the drawing can be resolved to a verified name later.
    inv_path = Path(a.out).with_name("building_inventory.csv")
    with open(inv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "feature_id", "code", "osm_name", "display_name",
            "name_th", "name_en", "addr_house", "levels_label",
            "cad_layer", "source",
            "latitude", "longitude"])
        writer.writeheader()
        writer.writerows(inventory)
    named = sum(1 for r in inventory if r["osm_name"])
    print(f"Inventory: {len(inventory)} buildings ({named} named, "
          f"{len(inventory) - named} coded) -> {inv_path}")

    # The attribute table: every source tag of every drawn feature, in a
    # form that opens outside AutoCAD. It is also the complete record —
    # XDATA stops at XDATA_MAX_TAGS per entity, this does not.
    attrs = []
    if not a.no_attributes:
        attrs = _anchor_rules.attribute_rows(
            drawn, {rec["feature_id"]:
                    (tag_index.get(rec["feature_id"])
                     or tag_index.get(rec["feature_id"].rsplit("/", 1)[0], {}))
                    for rec in drawn})
        attr_path = Path(a.out).with_name("attributes.csv")
        _anchor_rules.write_attribute_csv(attr_path, attrs)
        print(f"Attributes: {len(attrs)} tags on {len(drawn)} drawn features "
              f"-> {attr_path}")

    # Roads have never had an inventory while buildings always did, so the
    # thing a reader most often wants off a site plan — which road is which
    # and what number it carries — could only be had by opening the DXF and
    # clicking a line.
    road_rows = [{"feature_id": r["feature_id"], "road_ref": r["road_ref"],
                  "highway_type": r["highway_type"],
                  "road_name": r["road_name"], "name_th": r["name_th"],
                  "name_en": r["name_en"],
                  "official_name": r.get("official_name", ""),
                  "cad_layer": r["cad_layer"],
                  "carriageway_m": r["carriageway_m"],
                  "oneway": r["oneway"],
                  "length_m": sum(LineString(run).length
                                  for run in r["runs"] if len(run) >= 2),
                  "source": "openstreetmap"}
                 for r in staged_roads]
    if road_rows:
        road_path = Path(a.out).with_name("road_inventory.csv")
        _anchor_rules.write_road_csv(road_path, road_rows)
        named = sum(1 for r in road_rows if r["road_name"])
        refd = sum(1 for r in road_rows if r["road_ref"])
        print(f"Roads: {len(road_rows)} ({named} named, {refd} numbered) "
              f"-> {road_path}")

    # สถานที่สำคัญใกล้เคียง: what is nearby, how far, and which way. The
    # drawing has always carried these as symbols; a ผังบริเวณ is read
    # alongside a list of them, and there was none.
    if staged_pois:
        poi_rows = []
        for rec in staged_pois:
            # Named landmarks only. staging_pois also carries trees, pylons
            # and gates, which stage with an empty display_name precisely so
            # they never grow a label; a list of nearby places that opens
            # with ninety trees is not a list anybody reads.
            # The curated OSM branch leaves cad_layer to stage_pois's
            # default, so read it the same way that table does.
            layer = rec.get("cad_layer") or "C-ANNO-SYMB"
            if (not rec.get("display_name")
                    or layer not in _anchor_rules.LANDMARK_LAYERS):
                continue
            de, dn = rec["x"] - cx, rec["y"] - cy
            poi_rows.append({
                "feature_id": rec["feature_id"], "poi_key": rec.get("poi_key"),
                "poi_type": rec.get("poi_type"),
                "kind_th": _anchor_rules.poi_kind_thai(rec.get("poi_type")),
                "display_name": rec.get("display_name", ""),
                "name_th": rec.get("name_th", ""),
                "name_en": rec.get("name_en", ""),
                "distance_m": round(math.hypot(de, dn), 1),
                "bearing": _anchor_rules.bearing_text(de, dn),
                "latitude": rec.get("latitude"),
                "longitude": rec.get("longitude"),
                "cad_layer": layer,
                "source": rec.get("source", "openstreetmap")})
        poi_rows.sort(key=lambda r: (r["distance_m"], r["feature_id"]))
        poi_path = Path(a.out).with_name("landmark_inventory.csv")
        _anchor_rules.write_poi_csv(poi_path, poi_rows)
        print(f"Landmarks: {len(poi_rows)} nearby place(s) -> {poi_path}")

    if a.db:
        stage_to_db(a, utm_epsg, inventory, staged_geoms, staged_roads,
                    contours, contour_layers,
                    poi_points=staged_pois, poi_areas=staged_site_pois,
                    context=staged_context, attributes=attrs,
                    spots=staged_spots)
    print(f"CRS: EPSG:{utm_epsg} (UTM {utm_label}), units = meters. "
          f"Center at UTM ({cx:.1f}, {cy:.1f})")


if __name__ == "__main__":
    sys.exit(main())
