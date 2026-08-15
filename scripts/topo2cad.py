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
# ]
# ///
"""Topo + OSM (buildings w/ names, roads) around a GPS point -> DXF."""
import argparse
import csv
import gzip
import json
import math
import re
import sys
import time
from pathlib import Path

from pyproj import Transformer
import requests

# rasterio / scipy / skimage / numpy / ezdxf are imported inside main(): the
# geometry and CRS helpers below are pure and stay importable (and testable,
# and usable from mapposter.py) without the DEM and CAD stack.

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HEADERS = {"User-Agent": "topo2cad/1.0 (personal CAD export script)"}


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
    p.add_argument("--all-poi", action="store_true",
                   help="Draw every amenity/tourism/historic feature instead "
                        "of only the civic landmarks a submission needs. At a "
                        "dense site this is mostly restaurants and cafes: 144 "
                        "landmark points instead of 9 over 770 x 410 m in "
                        "central Bangkok.")
    p.add_argument("--names-only", action="store_true",
                   help="Label only buildings that carry an OSM name. The "
                        "default also labels unnamed footprints with their "
                        "B### inventory code — without it, areas where OSM "
                        "has no building names come out entirely unlabelled.")
    a = p.parse_args()
    if not a.out and not a.outdir:
        p.error("give either --out <file.dxf> or --outdir <dir>")
    # Default extent: 560 x 520 m. A3 is 420 x 297 mm, and after the title
    # block and margins sheet.py leaves a 290 x 273 mm viewport, so 1:2000
    # holds at most 580 x 546 m. 560 x 520 plots at 280 x 260 mm — a round
    # scale with ~5 mm of air on each side, and near the viewport's own
    # 1.06:1 aspect so the paper is used in both directions.
    # An explicit --radius still wins, so square boxes keep working.
    if a.radius is None and a.width is None and a.height is None:
        a.width, a.height = 560.0, 520.0
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


def bbox_around(lat, lon, radius_m, width_m=None, height_m=None):
    """Square box of +/-radius_m, or a width_m x height_m rectangle if given."""
    half_w = width_m / 2 if width_m else radius_m
    half_h = height_m / 2 if height_m else radius_m
    dlat = half_h / 111320.0
    dlon = half_w / (111320.0 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon  # S, W, N, E


def fetch_osm(s, w, n, e):
    query = f"""
    [out:json][timeout:90];
    (
      way["building"]({s},{w},{n},{e});
      relation["building"]({s},{w},{n},{e});
      way["highway"]({s},{w},{n},{e});
      way["waterway"]({s},{w},{n},{e});
      way["natural"="water"]({s},{w},{n},{e});
      way["leisure"~"^(park|garden|pitch|playground|golf_course)$"]({s},{w},{n},{e});
      way["landuse"~"^(grass|forest|meadow|orchard|farmland|cemetery)$"]({s},{w},{n},{e});
      way["railway"]({s},{w},{n},{e});
      way["barrier"]({s},{w},{n},{e});
      way["amenity"]({s},{w},{n},{e});
      way["tourism"]({s},{w},{n},{e});
      way["historic"]({s},{w},{n},{e});
      relation["amenity"]({s},{w},{n},{e});
      relation["tourism"]({s},{w},{n},{e});
      relation["historic"]({s},{w},{n},{e});
      node["amenity"]({s},{w},{n},{e});
      node["tourism"]({s},{w},{n},{e});
      node["historic"]({s},{w},{n},{e});
    );
    out tags geom;
    """
    last_err = None
    for attempt in range(3):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=180)
                r.raise_for_status()
                return r.json()["elements"]
            except Exception as exc:
                last_err = exc
                print(f"Overpass endpoint failed ({url}): {exc}")
        wait = 20 * (attempt + 1)
        print(f"All endpoints failed, retrying in {wait}s...")
        time.sleep(wait)
    raise last_err


MS_LINKS_URL = ("https://minedbuildings.z5.web.core.windows.net/"
                "global-buildings/dataset-links.csv")


def quadkey(lat, lon, z=9):
    sl = math.sin(math.radians(lat))
    x = (lon + 180) / 360
    y = 0.5 - math.log((1 + sl) / (1 - sl)) / (4 * math.pi)
    tx, ty = int(x * 2**z), int(y * 2**z)
    return "".join(str((((ty >> (z - 1 - i)) & 1) << 1) | ((tx >> (z - 1 - i)) & 1))
                   for i in range(z))


def fetch_ms_buildings(s, w, n, e, cache_dir):
    """Microsoft Global ML Building Footprints intersecting the bbox (unnamed)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    links = cache_dir / "dataset-links.csv"
    if not links.exists():
        print("Downloading MS buildings index...")
        links.write_bytes(requests.get(MS_LINKS_URL, headers=HEADERS, timeout=300).content)
    keys = {quadkey(la, lo) for la in (s, n) for lo in (w, e)}
    urls = [line.split(",")[2] for line in links.read_text().splitlines()
            if line.split(",")[1:2] and line.split(",")[1] in keys]
    footprints = []
    for url in urls:
        tile = cache_dir / url.rsplit("/quadkey=", 1)[1].replace("/", "_")
        if not tile.exists():
            print(f"Downloading MS buildings tile {tile.name}...")
            tile.write_bytes(requests.get(url, headers=HEADERS, timeout=600).content)
        with gzip.open(tile, "rt") as f:
            for line in f:
                geom = json.loads(line)["geometry"]
                if geom["type"] != "Polygon":
                    continue
                ring = geom["coordinates"][0]
                if any(s <= la <= n and w <= lo <= e for lo, la in ring):
                    footprints.append([(lo, la) for lo, la in ring])
    return footprints


def _clip_seg(x1, y1, x2, y2, xmin, ymin, xmax, ymax):
    """Liang-Barsky segment/box clip; returns clipped endpoints or None."""
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - xmin), (dx, xmax - x1), (-dy, y1 - ymin), (dy, ymax - y1)):
        if p == 0:
            if q < 0:
                return None
        else:
            r = q / p
            if p < 0:
                if r > t1:
                    return None
                t0 = max(t0, r)
            else:
                if r < t0:
                    return None
                t1 = min(t1, r)
    return (x1 + t0 * dx, y1 + t0 * dy), (x1 + t1 * dx, y1 + t1 * dy)


def clip_runs(pts, s, w, n, e, margin=0.0005):
    """Clip a lon/lat polyline to the bbox, cutting segments at the box edges."""
    xmin, xmax = w - margin, e + margin
    ymin, ymax = s - margin, n + margin
    runs, cur = [], []
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        seg = _clip_seg(x1, y1, x2, y2, xmin, ymin, xmax, ymax)
        if seg is None:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
            continue
        (a1, b1), (a2, b2) = seg
        if not cur or abs(cur[-1][0] - a1) > 1e-9 or abs(cur[-1][1] - b1) > 1e-9:
            if len(cur) >= 2:
                runs.append(cur)
            cur = [(a1, b1)]
        cur.append((a2, b2))
    if len(cur) >= 2:
        runs.append(cur)
    return runs


# Thai mapping convention puts Thai script in the plain `name` tag, but a
# business that prefers its English trading name overrides that, so `name`
# alone is not a reliable Thai source. U+0E00–U+0E7F is the Thai block.
THAI_RE = re.compile(r"[฀-๿]")


def is_thai(text) -> bool:
    """True if the string contains any Thai character."""
    return bool(text) and bool(THAI_RE.search(str(text)))


def names_by_lang(tags):
    """Resolve (thai, english) from OSM tags.

    `name:th` / `name:en` win outright. A plain `name` fills whichever slot
    its own script says it belongs to, so a Thai `name` never lands on the
    English layer and vice versa.
    """
    th = tags.get("name:th")
    en = tags.get("name:en")
    plain = tags.get("name")
    if plain:
        if is_thai(plain):
            th = th or plain
        else:
            en = en or plain
    return (th or None, en or None)


def best_name(tags):
    """Single best label, Thai first — the output is a Thai submission."""
    th, en = names_by_lang(tags)
    return th or en


# A landmark ("สถานที่สำคัญ") is one of these three OSM keys. The query used
# to ask for node["name"], which at a dense site returns mostly furniture:
# in a 770x410 m extent over Pathum Wan, 186 of 293 named nodes were mall
# floor markers, shop brands, benches and bus stops, each drawing a symbol
# and a label onto the sheet.
POI_KEYS = ("amenity", "tourism", "historic")

# ...but most of what those keys return is not what a submission drawing is
# for. Over a 770 x 410 m extent at Pathum Wan, 105 of 144 landmark nodes are
# restaurants, cafes, ATMs and money changers. A ผังบริเวณ is read by an
# officer locating a parcel, and they locate it by วัด, โรงเรียน, โรงพยาบาล,
# สถานีตำรวจ, ที่ว่าการอำเภอ — civic and institutional fixtures that outlast
# any tenant. These are the values kept by default; --all-poi restores the
# unfiltered behaviour. A value missing here is a one-line addition.
POI_SUBMISSION = {
    "amenity": {
        # worship and education — the two most-used Thai landmarks
        "place_of_worship", "monastery",
        "school", "university", "college", "kindergarten",
        # health
        "hospital", "clinic",
        # civil authority and public service
        "police", "fire_station", "townhall", "courthouse", "embassy",
        "public_building", "community_centre", "post_office", "prison",
        # public fixtures a reviewer will recognise on the ground
        "marketplace", "bus_station", "library",
        "fuel",          # ปั๊มน้ำมัน is a genuine wayfinding landmark here
    },
    "tourism": {"museum", "attraction", "viewpoint", "zoo", "aquarium",
                "theme_park"},
    # historic=* is small and inherently submission-relevant — a monument,
    # ruins, a city gate are all worth drawing — so it is kept whole.
    "historic": None,
}


def poi_kind(tags, curated=True):
    """(key, value) of the landmark tag on this feature, or None.

    Ordered, so a hospital tagged both amenity and tourism reports as
    amenity. `curated` keeps only the values a submission drawing needs;
    pass False for every amenity/tourism/historic feature OSM holds.
    """
    for key in POI_KEYS:
        value = tags.get(key)
        if not value:
            continue
        if not curated:
            return (key, value)
        allowed = POI_SUBMISSION.get(key, set())
        if allowed is None or value in allowed:
            return (key, value)
    return None


# Carriageway width in metres per highway class, used to draw each road as
# two parallel edges (the CAD convention) rather than a single centreline.
ROAD_WIDTH_M = {
    "motorway": 14.0, "motorway_link": 8.0,
    "trunk": 12.0, "trunk_link": 8.0,
    "primary": 10.0, "primary_link": 7.0,
    "secondary": 9.0, "secondary_link": 6.5,
    "tertiary": 8.0, "tertiary_link": 6.0,
    "residential": 6.0, "unclassified": 6.0, "living_street": 5.0,
    "service": 4.0, "track": 3.5,
    "footway": 2.0, "path": 1.5, "cycleway": 2.0, "steps": 1.5,
    "pedestrian": 6.0,
}


# CAD layer names follow the NCS/AIA convention (discipline-major-minor) so
# the DXF drops straight into an engineering drawing set. All annotation is
# isolated on C-ANNO-TEXT so drafters can toggle labels in one click.
LAYERS = {
    "building": "C-BLDG-OUTL",
    "road_edge": "C-ROAD-EDGE",     # the two carriageway edges (double lines)
    "road_centre": "C-ROAD-CNTR",   # centreline
    # Annotation splits by language so a drafter can LAYFRZ one script and
    # plot a single-language sheet. Language-neutral text (B### codes,
    # contour elevations, the GPS tag, the north arrow) stays on the base
    # C-ANNO-TEXT layer and survives freezing either language.
    "anno": "C-ANNO-TEXT",
    "anno_th": "C-ANNO-TEXT-TH",
    "anno_en": "C-ANNO-TEXT-EN",
    # NCS splits topography into index (every 5th, heavier and labelled) and
    # intermediate contours, which is what a reviewer expects to see
    "contour_major": "C-TOPO-MAJR",
    "contour_minor": "C-TOPO-MINR",
    "water": "C-HYDR-WATR",
    "green": "C-LAND-VEGT",
    "rail": "C-RAIL-TRAK",
    "barrier": "C-BNDY-BARR",
    "poi": "C-ANNO-SYMB",
    # Landmark grounds that carry no building tag — hospital and school
    # campuses, temple precincts, car parks. Kept off C-BLDG-OUTL so a
    # 3,000 m2 car park does not read as a structure.
    "site_poi": "C-SITE-POI",
    "north": "C-ANNO-NORT",
    "site": "C-ANNO-GPSP",
    "property": "C-PROP-LINE",
    "setback": "C-PROP-SETB",
}


# ezdxf writes UTF-8 either way, but AutoCAD renders Thai as ??? unless the
# text style points at a font that carries the Thai block. THSarabunNew is
# the Thai government document standard; AutoCAD substitutes if it is not
# installed, which is still better than the SHX default that cannot render
# Thai at all.
TEXT_STYLES = {
    "TH_STYLE": "THSarabunNew.ttf",
    "EN_STYLE": "arial.ttf",
}

# Annotation layer -> (ACI colour, text style) for the language split.
ANNO_STYLE = {
    "C-ANNO-TEXT": (2, "EN_STYLE"),      # neutral: codes, elevations, N, GPS
    "C-ANNO-TEXT-TH": (2, "TH_STYLE"),
    "C-ANNO-TEXT-EN": (7, "EN_STYLE"),
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


def road_label(tags):
    """Road name with its route number, e.g. 'ถนนอรุณประเสริฐ (ทล.202)'."""
    name = best_name(tags)
    ref = tags.get("ref")
    if name and ref:
        return f"{name} ({ref})"
    return name or (f"ทล.{ref}" if ref else None)


def road_edges(points, width_m):
    """Both edges of a carriageway as coordinate lists. Falls back to the
    centreline when the geometry is too kinked to offset cleanly."""
    from shapely.geometry import LineString

    line = LineString(points)
    if line.length < 0.5:
        return []
    edges = []
    for side in (width_m / 2, -width_m / 2):
        try:
            off = line.offset_curve(side)
        except Exception:
            return [list(line.coords)]
        for part in (off.geoms if off.geom_type == "MultiLineString"
                     else [off]):
            if not part.is_empty and len(part.coords) >= 2:
                edges.append(list(part.coords))
    return edges or [list(line.coords)]


def stage_to_db(a, utm_epsg, inventory, building_geoms, road_records,
                contours=(), contour_layers=None,
                poi_points=(), poi_areas=(), context=()):
    """Stage what was just drawn into the SQLite layer, with CAD label
    anchors precomputed so the drawing step is plain SELECTs."""
    from pyproj import Transformer
    from shapely.geometry import LineString, MultiLineString, Polygon

    import stage_db

    to_wgs = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326",
                                  always_xy=True)
    extent = (f"{a.width:.0f}x{a.height:.0f}" if a.width and a.height
              else f"r{a.radius:.0f}")
    project = a.project or f"{a.lat:.6f}_{a.lon:.6f}_{extent}"

    conn = stage_db.connect(a.db)
    pid = stage_db.create_project(
        conn, project, a.lat, a.lon,
        a.width or (a.radius * 2), a.height or (a.radius * 2), utm_epsg)

    b_rows = []
    for row in inventory:
        pts = building_geoms.get(row["feature_id"])
        if not pts or len(pts) < 3:
            continue
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            continue
        b_rows.append({**row, "geom": poly})

    # Landmark areas ride in the same table: they need a polygon, an interior
    # label anchor and an area, which is what it stores. Their cad_layer is
    # what keeps them off C-BLDG-OUTL and out of the building inventory.
    n_sp = 0
    for rec in poi_areas:
        poly = Polygon(rec["geom_pts"])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty:
            continue
        b_rows.append({k: v for k, v in rec.items() if k != "geom_pts"}
                      | {"geom": poly, "cad_layer": LAYERS["site_poi"],
                         "source": "openstreetmap", "code": "",
                         "osm_name": rec.get("display_name", "")})
        n_sp += 1
    n_b = stage_db.stage_buildings(conn, pid, b_rows, to_wgs=to_wgs) - n_sp

    n_p = stage_db.stage_pois(conn, pid, list(poi_points))
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

    labels = conn.execute("SELECT COUNT(*) FROM cad_labels WHERE"
                          " project_id = ?", (pid,)).fetchone()[0]
    conn.close()
    print(f"Staged to {a.db}: project '{project}' (id {pid}) — "
          f"{n_b} buildings, {n_r} roads, {n_c} contours, "
          f"{n_p} POI points, {n_sp} POI areas, {n_x} context, "
          f"{labels} CAD labels ready")


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
    # pick a "nice" interval giving ~10 levels
    for interval in (0.5, 1, 2, 5, 10, 20, 50):
        if span / interval <= 12:
            break
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
    elements = fetch_osm(s, w, n, e)
    buildings, roads, pois = [], [], []
    site_pois = []

    def kind_of(tags):
        return poi_kind(tags, curated=not a.all_poi)

    water, green, rails, barriers = [], [], [], []
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "node" and kind_of(tags) and best_name(tags):
            # Unnamed landmark nodes (a waste basket, a bicycle stand) carry
            # no information a drafter can use, so a name is still required
            pois.append((names_by_lang(tags), el["lon"], el["lat"],
                         kind_of(tags), f"node/{el['id']}"))
        elif el["type"] == "way" and "geometry" in el:
            pts = [(g["lon"], g["lat"]) for g in el["geometry"]]
            name = best_name(tags)
            fid = f"{el['type']}/{el['id']}"
            if "building" in tags:
                buildings.append((names_by_lang(tags), pts,
                                  f"{el['type']}/{el['id']}"))
            elif "highway" in tags:
                roads.append((names_by_lang(tags), tags.get("ref"), pts,
                              tags["highway"], f"{el['type']}/{el['id']}"))
            elif "waterway" in tags or tags.get("natural") == "water":
                water.append((names_by_lang(tags), pts, fid))
            elif "leisure" in tags or "landuse" in tags:
                green.append((names_by_lang(tags), pts, fid))
            elif "railway" in tags:
                rails.append((names_by_lang(tags), pts, fid))
            elif "barrier" in tags:
                barriers.append((names_by_lang(tags), pts, fid))
            elif kind_of(tags) and len(pts) >= 3:
                # A landmark mapped as an area but not tagged `building`:
                # hospital and school grounds, temple precincts, car parks.
                # A landmark that IS a building came through the branch
                # above and already has its outline and name.
                site_pois.append((names_by_lang(tags), pts,
                                  f"{el['type']}/{el['id']}", kind_of(tags)))
        elif el["type"] == "relation":
            if "building" in tags or kind_of(tags):
                for m in el.get("members", []):
                    if m.get("role") == "outer" and "geometry" in m:
                        pts = [(g["lon"], g["lat"]) for g in m["geometry"]]
                        if "building" in tags:
                            buildings.append((names_by_lang(tags), pts,
                                              f"relation/{el['id']}"))
                        elif len(pts) >= 3:
                            site_pois.append(
                                (names_by_lang(tags), pts,
                                 f"relation/{el['id']}", kind_of(tags)))
                        break
    print(f"OSM: {len(buildings)} buildings, {len(roads)} roads, {len(water)} water, "
          f"{len(green)} green, {len(rails)} rail, {len(barriers)} barriers, "
          f"{len(pois)} POI points, {len(site_pois)} POI areas")

    if len(buildings) < 20:
        print("Few OSM buildings — supplementing with Microsoft ML footprints...")
        ms = fetch_ms_buildings(s, w, n, e, Path(a.dem).parent / "ms_cache")
        buildings += [((None, None), pts, f"ms/{i:05d}")
                      for i, pts in enumerate(ms)]
        print(f"MS footprints added: {len(ms)}")

    # ---- DXF -------------------------------------------------------------
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    # (layer, color, lineweight 1/100 mm) — roads/buildings heavy, context thin
    add_text_styles(doc)
    for key, color, lw in [("contour_major", 8, 25), ("contour_minor", 8, 9),
                           ("building", 4, 50), ("anno", 2, 25),
                           ("anno_th", 2, 25), ("anno_en", 7, 25),
                           ("road_edge", 30, 35), ("road_centre", 8, 9),
                           ("water", 5, 18), ("green", 3, 13),
                           ("rail", 250, 18), ("barrier", 9, 13),
                           ("poi", 6, 18), ("site_poi", 5, 25),
                           ("north", 7, 35), ("site", 1, 35)]:
        layer = doc.layers.add(LAYERS[key], color=color)
        layer.dxf.lineweight = lw
    # Site-plan layers, empty and ready to draw on (OSM has no private parcels):
    prop = doc.layers.add(LAYERS["property"], color=1, linetype="PHANTOM")
    prop.dxf.lineweight = 70
    setb = doc.layers.add(LAYERS["setback"], color=2, linetype="DASHED")
    setb.dxf.lineweight = 25

    def mtext(label, x, y, height, rotation=0.0, layer=None):
        """MTEXT anchored Middle Center on the annotation layer, so the
        label grows symmetrically about its insertion point. `layer`
        selects the language sub-layer; it defaults to the neutral one."""
        layer = layer or LAYERS["anno"]
        m = msp.add_mtext(str(label), dxfattribs={
            "layer": layer, "char_height": height,
            "style": ANNO_STYLE[layer][1]})
        m.set_location((x, y), rotation=rotation,
                       attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
        return m

    def mtext_bilingual(th, en, x, y, height, rotation=0.0, fallback=None):
        """Write the Thai and English labels of one feature onto their own
        layers, English stacked above Thai when both exist. Falls back to
        `fallback` (a B### code) on the neutral layer when neither name is
        known. Returns the number of MTEXT entities written."""
        n = 0
        if th:
            mtext(th, x, y, height, rotation, LAYERS["anno_th"])
            n += 1
        if en:
            ex, ey = ((x, y) if not th else
                      offset_along_normal(x, y, rotation,
                                          height * LANG_OFFSET))
            mtext(en, ex, ey, height, rotation, LAYERS["anno_en"])
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

    # Buildings: outline, then a label centred inside every footprint —
    # its name when OSM has one, otherwise a B### code carried in the
    # inventory CSV so field teams can fill the name in later.
    from shapely.geometry import Polygon

    inventory = []
    staged_geoms = {}
    counter = 0
    for (th, en), pts, fid in sorted(buildings, key=lambda b: b[2]):
        ux, uy = to_utm.transform(*zip(*pts))
        upts = list(zip(ux, uy))
        msp.add_lwpolyline(upts, close=True,
                           dxfattribs={"layer": LAYERS["building"]})
        name = th or en
        code = ""
        if not name:
            counter += 1
            code = f"B{counter:03d}"
        label = name or code
        # ST_Centroid-style centroids fall outside concave footprints (~3% of
        # buildings in a dense extent), so anchor on a guaranteed interior
        # point instead — equivalent to PostGIS ST_PointOnSurface.
        try:
            poly = Polygon(upts)
            pt = poly.representative_point() if poly.is_valid else None
            cx, cy = (pt.x, pt.y) if pt else (float(np.mean(ux)),
                                              float(np.mean(uy)))
        except Exception:
            cx, cy = float(np.mean(ux)), float(np.mean(uy))
        if name or not a.names_only:
            mtext_bilingual(th, en, cx, cy, 3.5,
                            fallback=None if a.names_only else code)
        staged_geoms[fid] = upts
        blon, blat = to_wgs.transform(cx, cy)
        inventory.append({"feature_id": fid, "code": code,
                          "osm_name": name or "", "display_name": label,
                          "name_th": th or "", "name_en": en or "",
                          "source": "openstreetmap" if not fid.startswith("ms/")
                          else "microsoft_ml",
                          "latitude": round(blat, 8),
                          "longitude": round(blon, 8)})

    # Roads: both carriageway edges (CAD convention) plus a thin centreline,
    # labelled once per unique name with its route number.
    staged_roads = []
    for (th, en), ref, pts, highway, fid in roads:
        name = th or en
        width_m = ROAD_WIDTH_M.get(highway, 5.0)
        road_runs = []
        for run in clip_runs(pts, s, w, n, e):
            ux, uy = to_utm.transform(*zip(*run))
            upts = list(zip(ux, uy))
            if len(upts) < 2:
                continue
            road_runs.append(upts)
            for edge in road_edges(upts, width_m):
                msp.add_lwpolyline(edge,
                                   dxfattribs={"layer": LAYERS["road_edge"]})
            msp.add_lwpolyline(upts,
                               dxfattribs={"layer": LAYERS["road_centre"]})
        if road_runs:
            staged_roads.append({
                "feature_id": fid, "highway_type": highway,
                "road_name": name, "road_ref": ref,
                "name_th": th or "", "name_en": en or "",
                "carriageway_m": width_m, "runs": road_runs})

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
        # Mirrors the road_ref branch of cad_labels: an unnamed road carries
        # the Thai 'ทล.' prefix and sits on the anchor, a named one shows a
        # bare number clear of the name stack above it.
        th, en, name = rec["name_th"], rec["name_en"], rec["road_name"]
        if not name:
            text, off = f"ทล.{rec['road_ref']}", 0.0
        else:
            text = rec["road_ref"]
            off = 6.0 + (5.0 * LANG_OFFSET if th and en else 0.0)
        rx, ry = offset_along_normal(x, y, rot, off)
        mtext(text, rx, ry, 4.0, rotation=rot,
              layer=LAYERS["anno_th" if is_thai(text) else "anno_en"])

    label_longest(staged_roads, lambda r: r["road_name"], emit_road_name)
    label_longest(staged_roads, lambda r: r["road_ref"], emit_road_ref)

    staged_context = []

    def draw_lines(features, kind, layer, label=False, text_h=4.0):
        """Context linework — canals, parks, railways, walls. Each feature
        may survive clipping as several runs; every run is staged so
        db2dxf.py can redraw the same polylines."""
        for (th, en), pts, fid in sorted(features, key=lambda f: f[2]):
            name = th or en
            runs = []
            for run in clip_runs(pts, s, w, n, e):
                ux, uy = to_utm.transform(*zip(*run))
                upts = list(zip(ux, uy))
                closed = run[0] == run[-1]
                msp.add_lwpolyline(upts, close=closed,
                                   dxfattribs={"layer": layer})
                runs.append(upts)
            if runs:
                staged_context.append({
                    "feature_id": fid, "kind": kind, "cad_layer": layer,
                    "name_th": th or "", "name_en": en or "",
                    "display_name": name or "", "labelled": bool(label),
                    "runs": runs})

    draw_lines(water, "water", LAYERS["water"], label=True)
    draw_lines(green, "green", LAYERS["green"], label=True)
    draw_lines(rails, "rail", LAYERS["rail"])
    draw_lines(barriers, "barrier", LAYERS["barrier"])

    # Context names dedupe within their own kind, matching the view's
    # PARTITION BY project_id, kind, display_name.
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
        msp.add_lwpolyline(upts, close=True,
                           dxfattribs={"layer": LAYERS["site_poi"]})
        try:
            poly = Polygon(upts)
            pt = poly.representative_point() if poly.is_valid else None
            cx, cy = (pt.x, pt.y) if pt else (float(np.mean(ux)),
                                              float(np.mean(uy)))
        except Exception:
            cx, cy = float(np.mean(ux)), float(np.mean(uy))
        mtext_bilingual(th, en, cx, cy, 3.5)
        staged_site_pois.append({"feature_id": fid, "poi_key": kind[0],
                                 "poi_type": kind[1], "name_th": th or "",
                                 "name_en": en or "",
                                 "display_name": th or en or "",
                                 "geom_pts": upts})

    staged_pois = []
    for (th, en), plon, plat, kind, fid in sorted(pois, key=lambda p: p[4]):
        px, py = to_utm.transform(plon, plat)
        msp.add_circle((px, py), radius=2, dxfattribs={"layer": LAYERS["poi"]})
        mtext_bilingual(th, en, px + 3, py, 4.0)
        staged_pois.append({"feature_id": fid,
                            "poi_key": kind[0], "poi_type": kind[1],
                            "name_th": th or "", "name_en": en or "",
                            "display_name": th or en or "",
                            "x": px, "y": py,
                            "latitude": plat, "longitude": plon})

    # North arrow at top-right corner (drawing is true-north-up in UTM).
    # Sized from the nominal extent rather than the projected bbox corners,
    # because db2dxf.py only has the nominal metres to work from and the two
    # differ by ~2 m — bbox_around approximates a degree as 111,320 m.
    cx, cy = to_utm.transform(a.lon, a.lat)
    ext_w = a.width or (a.radius * 2)
    ext_h = a.height or (a.radius * 2)
    ax_ = cx + (ext_w / 2) * 0.94
    ay = cy + (ext_h / 2) * 0.90
    sz = min(ext_w, ext_h) * 0.02
    msp.add_circle((ax_, ay), radius=sz, dxfattribs={"layer": LAYERS["north"]})
    msp.add_solid([(ax_ - sz * 0.3, ay - sz * 0.6),
                   (ax_ + sz * 0.3, ay - sz * 0.6),
                   (ax_, ay + sz * 0.8)],
                  dxfattribs={"layer": LAYERS["north"]})
    mtext("N", ax_, ay + sz * 1.5, sz * 0.6)

    msp.add_circle((cx, cy), radius=5, dxfattribs={"layer": LAYERS["site"]})
    mtext(f"GPS {a.lat},{a.lon}", cx + 40, cy, 5.0)

    if a.sheet:
        import sheet as sheet_mod
        ext_w = a.width or a.radius * 2
        ext_h = a.height or a.radius * 2
        if str(a.scale).lower() == "fit":
            a.scale, _, _ = sheet_mod.fitting_scale(ext_w, ext_h, a.sheet)
        else:
            a.scale = int(a.scale)
        sheet_mod.add_sheet(doc, {
            "project": a.project or Path(a.out).stem,
            "lat": a.lat, "lon": a.lon, "centre": (cx, cy),
            "srid": utm_epsg,
            "extent": (a.width or a.radius * 2, a.height or a.radius * 2),
            "date": time.strftime("%Y-%m-%d"),
        }, size=a.sheet, scale=a.scale)
        print(f"Sheet: {a.sheet} paper space at 1:{a.scale:,}")

    doc.saveas(a.out)
    print(f"Saved: {a.out}")

    # Building inventory beside the DXF: one row per drawn footprint, so a
    # B### code on the drawing can be resolved to a verified name later.
    inv_path = Path(a.out).with_name("building_inventory.csv")
    with open(inv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "feature_id", "code", "osm_name", "display_name",
            "name_th", "name_en", "source",
            "latitude", "longitude"])
        writer.writeheader()
        writer.writerows(inventory)
    named = sum(1 for r in inventory if r["osm_name"])
    print(f"Inventory: {len(inventory)} buildings ({named} named, "
          f"{len(inventory) - named} coded) -> {inv_path}")

    if a.db:
        stage_to_db(a, utm_epsg, inventory, staged_geoms, staged_roads,
                    contours, contour_layers,
                    poi_points=staged_pois, poi_areas=staged_site_pois,
                    context=staged_context)
    print(f"CRS: EPSG:{utm_epsg} (UTM {utm_label}), units = meters. "
          f"Center at UTM ({cx:.1f}, {cy:.1f})")


if __name__ == "__main__":
    sys.exit(main())
