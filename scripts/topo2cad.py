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
    # Default extent: 1000 x 750 m. This does not fit A3 at 1:2000 (that
    # caps at 580 x 546 m) — sheet.py falls back to 1:5000 there, 1:2500 on
    # A2 and 1:2000 on A1. All are round scales a reviewer accepts, but
    # check --sheet against the scale you need before plotting.
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
      way["landuse"~"^(residential|commercial|industrial|retail|construction|quarry|military|education|religious)$"]({s},{w},{n},{e});
      way["railway"]({s},{w},{n},{e});
      way["barrier"]({s},{w},{n},{e});
      node["barrier"~"^(gate|lift_gate|swing_gate|entrance)$"]({s},{w},{n},{e});
      way["amenity"="parking"]({s},{w},{n},{e});
      way["power"]({s},{w},{n},{e});
      way["man_made"="pipeline"]({s},{w},{n},{e});
      node["power"~"^(tower|pole|portal|transformer|substation)$"]({s},{w},{n},{e});
      node["natural"="tree"]({s},{w},{n},{e});
      node["highway"="street_lamp"]({s},{w},{n},{e});
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


# Land use that is built on rather than planted. The green branch keeps
# grass, forest, farmland and the rest; these read as zoning on a plan.
BUILT_UP_LANDUSE = {"residential", "commercial", "industrial", "retail",
                    "construction", "quarry", "military", "education",
                    "religious"}


def source_tags(elements):
    """feature_id -> the element's original OSM tags.

    `classify_elements()` keeps only what it draws with — names, highway
    class, POI kind. The attributes a CAD user inspects come from here, so
    the drawing carries the source data rather than a summary of it.
    """
    return {f"{el['type']}/{el['id']}": el.get("tags", {}) for el in elements}


def assign_inner_rings(outers, inners):
    """Group a multipolygon's inner rings by the outer ring containing each.

    A relation with several `outer` members is several separate buildings
    sharing one relation — a mall with two blocks, an atrium in one of them.
    Attaching every inner to the first outer punches a courtyard through the
    wrong building; dropping them, which this used to do, fills in a real
    one. OSM's multipolygon rules define the answer by containment, so ask
    for it: an inner belongs to the outer whose ring encloses it.

    Returns one list of holes per outer, in the outers' own order.
    """
    from shapely.geometry import Polygon

    def as_poly(ring):
        try:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            return None if poly.is_empty else poly
        except Exception:
            return None

    polys = [as_poly(ring) for ring in outers]
    holes = [[] for _ in outers]
    for ring in inners:
        hole = as_poly(ring)
        if hole is None:
            continue
        point = hole.representative_point()
        for i, poly in enumerate(polys):
            if poly is not None and poly.contains(point):
                holes[i].append(ring)
                break
        # An inner inside none of them is a broken relation upstream; it is
        # left out rather than attached to an arbitrary building.
    return holes


def classify_elements(elements, curated=True):
    """Sort raw OSM elements into the per-category lists the drawing steps
    consume: buildings, roads, water, green, rail, barrier, POI points and
    POI areas.

    Elements are Overpass `out tags geom` shape — a way carries `geometry`
    as [{lon, lat}, ...] and a relation carries `members` with a `role` and
    the same geometry. `osm2cad.py` normalises a downloaded .osm file into
    exactly that shape and calls this, so a file import and a live fetch
    categorise identically; the tag rules live here once rather than in each
    front door.
    """
    buildings, roads, pois, site_pois = [], [], [], []
    water, green, rails, barriers = [], [], [], []
    power, pipelines, points, zoning = [], [], [], []
    parking, plazas = [], []

    def kind_of(tags):
        return poi_kind(tags, curated=curated)

    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "node" and tags.get("highway") == "street_lamp":
            points.append(("lamp", el["lon"], el["lat"],
                           f"node/{el['id']}", "street_lamp"))
        elif el["type"] == "node" and tags.get("barrier"):
            # A gate is an access point; a site plan shows where you get in
            points.append(("gate", el["lon"], el["lat"],
                           f"node/{el['id']}", tags["barrier"]))
        elif el["type"] == "node" and (tags.get("power")
                                       or tags.get("natural") == "tree"):
            # Unnamed by nature: a pylon or a tree is identified by its
            # symbol, not by a label, so these never reach the POI branch.
            kind = "tree" if tags.get("natural") == "tree" else "power"
            points.append((kind, el["lon"], el["lat"],
                           f"node/{el['id']}", tags.get("power") or "tree"))
        elif el["type"] == "node" and kind_of(tags) and best_name(tags):
            # Unnamed landmark nodes (a waste basket, a bicycle stand) carry
            # no information a drafter can use, so a name is still required
            pois.append((names_by_lang(tags), el["lon"], el["lat"],
                         kind_of(tags), f"node/{el['id']}"))
        elif el["type"] == "way" and "geometry" in el:
            pts = [(g["lon"], g["lat"]) for g in el["geometry"]]
            fid = f"{el['type']}/{el['id']}"
            if "building" in tags:
                # (exterior, holes) — a way has one ring by definition
                buildings.append((names_by_lang(tags), (pts, []), fid))
            elif "highway" in tags:
                # An area you walk on rather than a line you walk along:
                # drawing a plaza as a path traces its outline as if it
                # were a 2 m footway around the edge.
                if (str(tags.get("area", "")).lower() == "yes"
                        and len(pts) >= 4 and pts[0] == pts[-1]):
                    plazas.append((names_by_lang(tags), pts, fid))
                else:
                    roads.append((names_by_lang(tags), tags.get("ref"), pts,
                                  tags["highway"], fid, oneway_dir(tags)))
            elif "waterway" in tags or tags.get("natural") == "water":
                water.append((names_by_lang(tags), pts, fid))
            elif tags.get("amenity") == "parking" and len(pts) >= 3:
                parking.append((names_by_lang(tags), pts, fid))
            elif tags.get("landuse") in BUILT_UP_LANDUSE:
                zoning.append((names_by_lang(tags), pts, fid))
            elif "leisure" in tags or "landuse" in tags:
                green.append((names_by_lang(tags), pts, fid))
            elif "railway" in tags:
                rails.append((names_by_lang(tags), pts, fid))
            elif "barrier" in tags:
                barriers.append((names_by_lang(tags), pts, fid))
            elif "power" in tags:
                power.append((names_by_lang(tags), pts, fid))
            elif tags.get("man_made") == "pipeline":
                pipelines.append((names_by_lang(tags), pts, fid))
            elif kind_of(tags) and len(pts) >= 3:
                # A landmark mapped as an area but not tagged `building`:
                # hospital and school grounds, temple precincts, car parks.
                # A landmark that IS a building came through the branch
                # above and already has its outline and name.
                site_pois.append((names_by_lang(tags), pts, fid,
                                  kind_of(tags)))
        elif el["type"] == "relation":
            if "building" in tags or kind_of(tags):
                # A multipolygon carries its courtyards as `inner` members.
                # Reading only the first `outer` — as this did — draws a
                # temple or a mall with its atrium filled in solid.
                outers, inners = [], []
                for m in el.get("members", []):
                    if "geometry" not in m:
                        continue
                    ring = [(g["lon"], g["lat"]) for g in m["geometry"]]
                    if len(ring) < 3:
                        continue
                    if m.get("role") == "outer":
                        outers.append(ring)
                    elif m.get("role") == "inner":
                        inners.append(ring)
                if not outers:
                    continue
                fid = f"relation/{el['id']}"
                # Several outers is several separate buildings sharing one
                # relation; each hole goes to the outer that contains it.
                hole_sets = ([inners] if len(outers) == 1
                             else assign_inner_rings(outers, inners))
                for i, outer in enumerate(outers):
                    holes = hole_sets[i]
                    part_id = fid if len(outers) == 1 else f"{fid}/{i}"
                    if "building" in tags:
                        buildings.append((names_by_lang(tags),
                                          (outer, holes), part_id))
                    else:
                        site_pois.append((names_by_lang(tags), outer,
                                          part_id, kind_of(tags)))
    return {"buildings": buildings, "roads": roads, "water": water,
            "green": green, "rails": rails, "barriers": barriers,
            "pois": pois, "site_pois": site_pois,
            "power": power, "pipelines": pipelines, "points": points,
            "zoning": zoning, "parking": parking, "plazas": plazas}


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

def carriageway_width(tags, highway) -> float:
    """Metres across the carriageway, from the tags where OSM has them.

    The class default is a guess — every `residential` road drawn 6.0 m
    whether it is a 4 m soi or an 8 m avenue. A mapper who typed `width` or
    `lanes` measured or counted something, so that wins:

      width=4        -> 4.0     (also "4 m", "4.5m")
      lanes=4        -> 12.0    (3 m a lane, the Thai rural standard)
      neither        -> the class default

    A parsed width under a metre is ignored: `width=0.5` on a carriageway
    is a mapping error, and drawing kerbs half a metre apart hides the road
    rather than sizing it.
    """
    raw = str(tags.get("width", "")).strip().lower()
    if raw:
        number = ""
        for ch in raw:
            if ch.isdigit() or (ch == "." and "." not in number):
                number += ch
            elif number:
                break
        try:
            metres = float(number)
        except ValueError:
            metres = 0.0
        # "12'" and "12 ft" are feet; OSM allows both and they are not rare
        if metres >= 1.0:
            if "'" in raw or "ft" in raw:
                metres *= 0.3048
            if metres >= 1.0:
                return metres
    lanes = str(tags.get("lanes", "")).strip()
    if lanes.isdigit() and int(lanes) > 0:
        return min(int(lanes) * lane_width(highway), 40.0)
    return ROAD_WIDTH_M.get(highway, 5.0)


# Lane widths: 3.5 m on the classes built to a highway standard, 3.0 m on
# everything else, which is what a Thai soi actually measures. A dual
# carriageway is two OSM ways, so counting lanes per way is also what stops
# `lanes` from doubling the road.
TRUNK_CLASSES = {"motorway", "motorway_link", "trunk", "trunk_link",
                 "primary", "primary_link"}


def lane_width(highway) -> float:
    return 3.5 if highway in TRUNK_CLASSES else 3.0


def road_cad_layer(tags, highway) -> str:
    """Which road layer this way belongs on.

    A bridge and a tunnel are drawn on their own layers because a drafter
    plotting a site plan needs them separable — a tunnel is under the
    ground the plan describes, and a bridge crosses whatever is beneath it.
    Paths keep their own layer regardless: a footbridge is still a footway.
    """
    if highway in PATH_TYPES:
        return LAYERS["road_path"]
    if str(tags.get("tunnel", "")).lower() not in ("", "no"):
        return LAYERS["road_tunnel"]
    if str(tags.get("bridge", "")).lower() not in ("", "no"):
        return LAYERS["road_bridge"]
    return LAYERS["road_centre"]


# Drawn as a single line on C-ROAD-PATH with no edge-of-pavement offset.
PATH_TYPES = {"footway", "path", "cycleway", "steps", "pedestrian",
              "bridleway", "corridor"}

# OSM spells one-way several ways, and two of them mean "backwards along the
# way as drawn" — getting that wrong points every arrow on the sliproad at
# oncoming traffic, which is worse than drawing none.
ONEWAY_FORWARD = {"yes", "true", "1"}
ONEWAY_REVERSE = {"-1", "reverse"}
# A roundabout is one-way by definition and is very often not tagged so.
IMPLICIT_ONEWAY_JUNCTIONS = {"roundabout", "circular"}


def oneway_dir(tags) -> int:
    """1 with the geometry, -1 against it, 0 for two-way.

    `oneway=no` wins over an implicit roundabout: a mapper who typed it
    meant it.
    """
    value = str(tags.get("oneway", "")).strip().lower()
    if value in ONEWAY_FORWARD:
        return 1
    if value in ONEWAY_REVERSE:
        return -1
    if value in ("no", "false", "0"):
        return 0
    if tags.get("junction", "").lower() in IMPLICIT_ONEWAY_JUNCTIONS:
        return 1
    return 0


# CAD layer names follow the NCS/AIA convention (discipline-major-minor) so
# the DXF drops straight into an engineering drawing set. All annotation is
# isolated on C-ANNO-TEXT so drafters can toggle labels in one click.
LAYERS = {
    "building": "C-BLDG-OUTL",
    "road_edge": "C-ROAD-EDGE",     # the two carriageway edges (double lines)
    "road_centre": "C-ROAD-CNTR",   # centreline, CENTER linetype
    # Footways, cycleways and steps are not carriageways: drawing a 1.5 m
    # path with two offset kerb lines makes it read as a road on the plan.
    "road_path": "C-ROAD-PATH",
    # Direction-of-travel arrows on one-way carriageways, from the OSM
    # `oneway` tag (and the roundabouts that imply it). Their own layer so a
    # drafter can plot the drawing without traffic direction on it.
    "road_arrow": "C-ROAD-ARRW",
    # A bridge crosses whatever is under it and a tunnel runs beneath the
    # ground the plan describes; a drafter needs both separable from the
    # carriageways at grade.
    "road_bridge": "C-ROAD-BRDG",
    "road_tunnel": "C-ROAD-TUNL",
    # No OSM source for a legal right-of-way, so this is created empty and
    # ready for a drafter to draw the ROW onto, like C-PROP-LINE.
    "road_row": "C-ROAD-ROWY",
    # Annotation splits by language so a drafter can LAYFRZ one script and
    # plot a single-language sheet. Language-neutral text (B### codes,
    # contour elevations, the GPS tag, the north arrow) stays on the base
    # C-ANNO-TEXT layer and survives freezing either language.
    "anno": "C-ANNO-TEXT",
    "anno_th": "C-ANNO-TEXT-TH",
    "anno_en": "C-ANNO-TEXT-EN",
    # NCS splits topography into index (every 5th, heavier and labelled) and
    # intermediate contours, which is what a reviewer expects to see
    # staging_contours defaults cad_layer to C-TOPO-CONT, so db2dxf.py
    # defines it; create it here too or the two layer tables disagree even
    # when every entity matches. Empty unless a contour arrives undifferentiated.
    "contour_plain": "C-TOPO-CONT",
    "contour_major": "C-TOPO-MAJR",
    "contour_minor": "C-TOPO-MINR",
    "water": "C-HYDR-WATR",
    "green": "C-LAND-VEGT",
    "rail": "C-RAIL-TRAK",
    "barrier": "C-BNDY-BARR",
    "poi": "C-ANNO-SYMB",
    # Utilities and planting. Power infrastructure is on almost every Thai
    # site plan and OSM maps it well: lines on C-UTIL-POWR with the pylons
    # and poles as symbols on the same layer, pipelines beside them.
    "power": "C-UTIL-POWR",
    "pipeline": "C-UTIL-PIPE",
    "tree": "C-LAND-TREE",
    # Spot heights: the elevation at a point, which is what a surveyor
    # levels to. Contours give the shape, a spot height gives the number.
    "spot": "C-TOPO-SPOT",
    # Built-up land use — residential, commercial, industrial. Kept off
    # C-LAND-VEGT, which is planting: a factory estate is not a park, and a
    # reviewer reads the two differently.
    "zoning": "C-LAND-ZONE",
    # UTM coordinate grid: crosses at the intersections with the easting
    # and northing written along two edges, which is how a survey sheet
    # lets a reader take a coordinate off the paper.
    "grid": "C-ANNO-GRID",
    # Real DIMENSION entities on the extent, so the drawing states its own
    # size instead of leaving a reviewer to measure it.
    "dims": "C-ANNO-DIMS",
    # A plaza or a covered walkway is an area you walk on, not a line you
    # walk along; drawn closed so it reads as surface on the plan.
    "plaza": "C-ROAD-PLAZ",
    # Street lighting rides with the other utilities.
    "lamp": "C-UTIL-LAMP",
    # Parking: drawn whatever the POI filter says, because a site plan
    # needs the parking whether or not a car park counts as a landmark.
    "parking": "C-SITE-PARK",
    # House numbers, small and language-neutral, under the building label.
    "addr": "C-ANNO-ADDR",
    # Landmark grounds that carry no building tag — hospital and school
    # campuses, temple precincts, car parks. Kept off C-BLDG-OUTL so a
    # 3,000 m2 car park does not read as a structure.
    "site_poi": "C-SITE-POI",
    "north": "C-ANNO-NORT",
    "site": "C-ANNO-GPSP",
    # The requested extent, drawn as a closed rectangle. Features are not
    # trimmed to it — a building straddling the edge stays whole — so this
    # is the crop line a drafter trims or clips a viewport to.
    "extent": "C-ANNO-EXTN",
    "property": "C-PROP-LINE",
    "setback": "C-PROP-SETB",
}


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


# Fraction of an ML footprint that must already be covered by a building
# before it counts as a duplicate rather than a new one. Half is well clear
# of the metre-scale disagreement between a traced and a modelled outline,
# and well below the >90% a genuine duplicate shows.
ML_OVERLAP_MAX = 0.5


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


def merge_ml_footprints(buildings, ms_rings) -> int:
    """Append ML footprints that no OSM building already covers.

    OSM and the ML layer overlap heavily in mapped areas — 272 ML footprints
    against 274 OSM buildings at Pathum Wan — so adding all of them would
    double-draw most of the block.

    A footprint counts as already mapped when it overlaps an existing
    building by more than half of whichever of the two is smaller. Two
    weaker rules were tried and measured at Pathum Wan first: testing
    whether its representative point falls inside a building let 12
    duplicate pairs through, because a modelled outline and a traced one
    disagree by a metre or two and the point lands just outside; testing
    only what fraction of the ML footprint itself is covered let 11 through,
    because the ML layer merges rows of small buildings into one blob that
    covers each of them entirely while they cover little of it.

    `buildings` is mutated in place. Returns how many were added.
    """
    keep = new_ml_rings([ext for _n, (ext, _h), _f in buildings], ms_rings)
    for i, ring in keep:
        buildings.append(((None, None), (ring, []), f"ms/{i:05d}"))
    return len(keep)


def new_ml_rings(existing_rings, ms_rings):
    """ML footprints no existing building already covers.

    Returns [(index_in_ms_rings, ring), ...]; the index keeps the ms/#####
    feature ids stable against the fetched list rather than the kept one.
    Shared with mapposter.py, which holds bare rings rather than the tuples
    the CAD path uses.
    """
    from shapely.geometry import Polygon
    from shapely.strtree import STRtree

    def as_poly(ring):
        if len(ring) < 3:
            return None
        try:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
        except Exception:
            return None
        return None if poly.is_empty or poly.area <= 0 else poly

    existing = [p for p in (as_poly(r) for r in existing_rings) if p]
    tree = STRtree(existing) if existing else None

    keep = []
    for i, ring in enumerate(ms_rings):
        poly = as_poly(ring)
        if poly is None:
            continue
        if tree is not None:
            # query() is a bounding-box prefilter, so measure the real
            # intersection. Compare against the *smaller* of the two areas,
            # not the ML footprint's own: the ML layer often merges a row of
            # small OSM buildings into one blob, which covers each of them
            # entirely while they cover only a fraction of it. Judging by
            # the footprint's own area alone admits that blob and duplicates
            # every building under it.
            if any(poly.intersection(existing[j]).area
                   / min(poly.area, existing[j].area) > ML_OVERLAP_MAX
                   for j in tree.query(poly)
                   if min(poly.area, existing[j].area) > 0):
                continue
        keep.append((i, ring))
    return keep


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
    # pick a "nice" interval giving ~10 levels
    interval = a.contour_interval or 0.0
    if interval <= 0:
        for interval in (0.5, 1, 2, 5, 10, 20, 50):
            if span / interval <= 12:
                break
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
    elements = fetch_osm(s, w, n, e)
    # The tag rules live in classify_elements() so osm2cad.py's file route
    # sorts a downloaded extract into exactly the same categories.
    features = classify_elements(elements, curated=not a.all_poi)
    buildings, roads = features["buildings"], features["roads"]
    water, green = features["water"], features["green"]
    rails, barriers = features["rails"], features["barriers"]
    pois, site_pois = features["pois"], features["site_pois"]
    power, pipelines = features["power"], features["pipelines"]
    point_marks = features["points"]
    zoning, parking = features["zoning"], features["parking"]
    plazas = features["plazas"]
    print(f"OSM: {len(buildings)} buildings, {len(roads)} roads, {len(water)} water, "
          f"{len(green)} green, {len(rails)} rail, {len(barriers)} barriers, "
          f"{len(pois)} POI points, {len(site_pois)} POI areas, "
          f"{len(power)} power, {len(pipelines)} pipeline, "
          f"{len(point_marks)} pylon/tree/gate, {len(zoning)} land-use, "
          f"{len(parking)} parking, {len(plazas)} plaza")

    if not a.no_ml:
        # Always supplement, not only when OSM is nearly empty. The old
        # "fewer than 20" rule meant a mapped area got OSM alone: at Pathum
        # Wan that drew 274 buildings while 64 further ML footprints sat on
        # ground OSM has nothing for. A building missing from a site plan is
        # a worse error than one whose outline came from a model.
        print("Supplementing with Microsoft ML footprints...")
        ms = fetch_ms_buildings(s, w, n, e, Path(a.dem).parent / "ms_cache")
        added = merge_ml_footprints(buildings, ms)
        print(f"MS footprints: {len(ms)} available, {added} added, "
              f"{len(ms) - added} already mapped in OSM")

    # ---- DXF -------------------------------------------------------------
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    # (layer, color, lineweight 1/100 mm) — roads/buildings heavy, context thin
    add_text_styles(doc)
    for key, color, lw in [("contour_plain", 8, 13),
                           ("contour_major", 8, 25), ("contour_minor", 8, 9),
                           ("building", 4, 50), ("anno", 2, 25),
                           ("anno_th", 2, 25), ("anno_en", 7, 25),
                           ("road_edge", 30, 35), ("road_centre", 8, 9),
                           ("road_path", 8, 13), ("road_arrow", 30, 18),
                           ("road_bridge", 7, 40), ("road_tunnel", 8, 18),
                           ("water", 5, 18), ("green", 3, 13),
                           ("rail", 250, 18), ("barrier", 9, 13),
                           ("poi", 6, 18), ("site_poi", 5, 25),
                           ("power", 6, 25), ("pipeline", 4, 18),
                           ("tree", 3, 13), ("addr", 8, 13),
                           ("spot", 8, 18), ("zoning", 32, 13),
                           ("grid", 253, 9), ("dims", 2, 18),
                           ("plaza", 8, 18), ("lamp", 51, 13),
                           ("parking", 140, 13),
                           ("extent", 7, 35),
                           ("north", 7, 35), ("site", 1, 35)]:
        layer = doc.layers.add(LAYERS[key], color=color)
        layer.dxf.lineweight = lw
    # Site-plan layers, empty and ready to draw on (OSM has no private parcels):
    prop = doc.layers.add(LAYERS["property"], color=1, linetype="PHANTOM")
    prop.dxf.lineweight = 70
    setb = doc.layers.add(LAYERS["setback"], color=2, linetype="DASHED")
    setb.dxf.lineweight = 25
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

    if not a.no_attributes:
        doc.appids.add(_anchor_rules.XDATA_APPID)

    def attach(entity, fid):
        tags = tag_index.get(fid) or tag_index.get(fid.rsplit("/", 1)[0], {})
        if not a.no_attributes and entity is not None and tags:
            entity.set_xdata(_anchor_rules.XDATA_APPID,
                             _anchor_rules.xdata_tags(fid, tags))

    def record(fid, kind, layer, name):
        drawn.append({"feature_id": fid, "feature_type": kind,
                      "cad_layer": layer, "display_name": name or ""})

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
            "layer": layer, "char_height": height,
            "style": ANNO_STYLE[layer][1]})
        m.set_location((x, y), rotation=rotation,
                       attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
        # Background mask: a label crossing a building outline or a road
        # edge punches a clean hole through it instead of overprinting.
        m.set_bg_color("canvas", scale=BG_MASK_SCALE)
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
                mtext(f"{elev:+.1f}", gx + 2.5, gy, 2.5,
                      layer=LAYERS["spot"])
                staged_spots.append({"x": gx, "y": gy, "elevation_m": elev})
        print(f"Spot heights: {len(staged_spots)} sampled from the DEM")

    # Buildings: outline, then a label centred inside every footprint —
    # its name when OSM has one, otherwise a B### code carried in the
    # inventory CSV so field teams can fill the name in later.
    inventory = []
    staged_geoms = {}
    counter = 0
    for (th, en), (ext, holes), fid in sorted(buildings, key=lambda b: b[2]):
        ux, uy = to_utm.transform(*zip(*ext))
        upts = list(zip(ux, uy))
        uholes = []
        for hole in holes:
            hx, hy = to_utm.transform(*zip(*hole))
            uholes.append(list(zip(hx, hy)))
        # Drawn from the repaired geometry, which is also what gets staged:
        # a self-intersecting ring becomes two polygons under buffer(0), and
        # drawing the raw ring while staging the repaired one gave a drawing
        # one outline where its re-issue had two.
        shape = _anchor_rules.repaired_polygon(upts, uholes)
        first = True
        for part in _anchor_rules.polygon_parts(shape):
            entity = msp.add_lwpolyline(
                list(part.exterior.coords), close=True,
                dxfattribs={"layer": LAYERS["building"]})
            if first:
                attach(entity, fid)
                first = False
            # Courtyards stay open: each inner ring is its own closed
            # polyline on the same layer, which is how a CAD island reads.
            for ring in part.interiors:
                msp.add_lwpolyline(list(ring.coords), close=True,
                                   dxfattribs={"layer": LAYERS["building"]})
        name = th or en
        code = ""
        if not name:
            counter += 1
            code = f"B{counter:03d}"
        label = name or code
        # ST_Centroid-style centroids fall outside concave footprints (~3% of
        # buildings in a dense extent), so anchor on a guaranteed interior
        # point instead — equivalent to PostGIS ST_PointOnSurface. It is the
        # staging layer's own call, on the same shape, so a re-issue agrees.
        try:
            cx, cy = _anchor_rules.interior_point(shape)
        except Exception:
            cx, cy = float(np.mean(ux)), float(np.mean(uy))
        if name or not a.names_only:
            mtext_bilingual(th, en, cx, cy, 3.5,
                            fallback=None if a.names_only else code)
        # House number under the label, small and language-neutral — the
        # same row cad_labels emits, at the same offset, so a re-issue puts
        # it in the same place.
        btags = tag_index.get(fid) or {}
        house = btags.get("addr:housenumber")
        if house:
            hx, hy = offset_along_normal(cx, cy, 0.0, -3.0)
            mtext(house, hx, hy, 2.2, layer=LAYERS["addr"])
        # Storeys under the number, at the offset cad_labels uses
        levels = _anchor_rules.levels_label(btags)
        if levels:
            lx2, ly2 = offset_along_normal(cx, cy, 0.0, -5.4)
            mtext(levels, lx2, ly2, 2.2, layer=LAYERS["addr"])
        staged_geoms[fid] = (upts, uholes)
        record(fid, "building", LAYERS["building"], label)
        blon, blat = to_wgs.transform(cx, cy)
        inventory.append({"feature_id": fid, "code": code,
                          "osm_name": name or "", "display_name": label,
                          "name_th": th or "", "name_en": en or "",
                          "addr_house": house or "",
                          "levels_label": levels,
                          "source": "openstreetmap" if not fid.startswith("ms/")
                          else "microsoft_ml",
                          "latitude": round(blat, 8),
                          "longitude": round(blon, 8)})

    # Roads: both carriageway edges (CAD convention) plus a thin centreline,
    # labelled once per unique name with its route number.
    staged_roads = []
    import blocks as _blocks
    for (th, en), ref, pts, highway, fid, oneway in roads:
        name = th or en
        road_tags = tag_index.get(fid) or {}
        # Measured where OSM has it, guessed by class only where it does not
        width_m = carriageway_width(road_tags, highway)
        is_path = highway in PATH_TYPES
        cad_layer = road_cad_layer(road_tags, highway)
        road_runs = []
        for run in clip_runs(pts, s, w, n, e):
            ux, uy = to_utm.transform(*zip(*run))
            upts = list(zip(ux, uy))
            if len(upts) < 2:
                continue
            road_runs.append(upts)
            if not is_path:
                for edge in road_edges(upts, width_m):
                    msp.add_lwpolyline(
                        edge, dxfattribs={"layer": LAYERS["road_edge"]})
            attach(msp.add_lwpolyline(upts, dxfattribs={"layer": cad_layer}),
                   fid)
            # Direction of travel, spaced along the run by the same rule
            # db2dxf.py applies to the staged geometry. Paths are excluded:
            # a one-way footpath is not a traffic instruction.
            if oneway and not is_path:
                size = _anchor_rules.oneway_arrow_size(width_m)
                for ax, ay, rot in _anchor_rules.arrow_positions(upts):
                    _blocks.add_oneway_arrow(
                        doc, msp, ax, ay, size,
                        rot + (180.0 if oneway < 0 else 0.0),
                        LAYERS["road_arrow"])
        if road_runs:
            record(fid, "path" if is_path else "road", cad_layer, name)
            staged_roads.append({
                "feature_id": fid, "highway_type": highway,
                "road_name": name, "road_ref": ref,
                "name_th": th or "", "name_en": en or "",
                "cad_layer": cad_layer, "oneway": oneway,
                # 0 tells the staging route not to offset edges either
                "carriageway_m": 0.0 if is_path else width_m,
                "runs": road_runs})

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
                attach(msp.add_lwpolyline(upts, close=closed,
                                          dxfattribs={"layer": layer}), fid)
                runs.append(upts)
            if runs:
                record(fid, kind, layer, name)
                staged_context.append({
                    "feature_id": fid, "kind": kind, "cad_layer": layer,
                    "name_th": th or "", "name_en": en or "",
                    "display_name": name or "", "labelled": bool(label),
                    "runs": runs})

    draw_lines(water, "water", LAYERS["water"], label=True)
    draw_lines(green, "green", LAYERS["green"], label=True)
    if a.hatch:
        # Closed runs only: an open canal centreline has no area to fill.
        # db2dxf.py hatches the same rows, recovering "closed" the same way.
        n_hatch = 0
        for rec in staged_context:
            if rec["kind"] not in _anchor_rules.HATCH_PATTERNS:
                continue
            for run in rec["runs"]:
                if len(run) >= 4 and run[0] == run[-1]:
                    _anchor_rules.hatch_area(msp, run, rec["kind"],
                                             rec["cad_layer"])
                    n_hatch += 1
        print(f"Hatched: {n_hatch} water/vegetation area(s)")
    draw_lines(rails, "rail", LAYERS["rail"])
    draw_lines(barriers, "barrier", LAYERS["barrier"])
    draw_lines(zoning, "zoning", LAYERS["zoning"], label=True)
    draw_lines(parking, "parking", LAYERS["parking"], label=True)
    draw_lines(plazas, "plaza", LAYERS["plaza"], label=True)
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
                list(part.exterior.coords), close=True,
                dxfattribs={"layer": LAYERS["site_poi"]})
            if first:
                attach(entity, fid)
                first = False
            for ring in part.interiors:
                msp.add_lwpolyline(list(ring.coords), close=True,
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
        mtext_bilingual(th, en, px + 3, py, 4.0)
        record(fid, "landmark", LAYERS["poi"], th or en)
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
    mtext(f"GPS {a.lat},{a.lon}", cx + 40, cy, 5.0)

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

    if a.mono:
        apply_mono(doc)
        print("Monochrome: all layers set to ACI 7")
    doc.saveas(a.out)
    print(f"Saved: {a.out}")

    # Building inventory beside the DXF: one row per drawn footprint, so a
    # B### code on the drawing can be resolved to a verified name later.
    inv_path = Path(a.out).with_name("building_inventory.csv")
    with open(inv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "feature_id", "code", "osm_name", "display_name",
            "name_th", "name_en", "addr_house", "levels_label", "source",
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
