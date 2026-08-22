#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "shapely",
# ]
# ///
"""Where the map data comes from, and what each element is.

Reaching Overpass and Microsoft's ML footprints, clipping to the extent, and
the tag rules that decide whether a way is a building, a carriageway, a
footpath, a canal or a pedestrian square. Everything *before* a drawing
exists.

This was the top half of topo2cad.py, which four other scripts already
imported piecemeal — mapposter.py for `fetch_osm`/`clip_runs`/`new_ml_rings`,
osm2cad.py for the whole tag ruleset, dxfaudit.py and gisqa.py for the
sources — each one pulling a 2,300-line CAD script in to ask what a tag
means. The rules live in one place for the reason they always did: the file
route and the Overpass route must not drift on what a `highway` is, and an
audit that asks the drawing's own classifier what the source contained
cannot catch a bug in that classifier.

The dependency runs one way — topo2cad.py imports this and re-exports every
name, so `topo2cad.fetch_osm(...)` keeps working and no call site moved.
Heavy imports stay out: this pulls `requests` and, inside the one function
that needs it, `shapely`. No rasterio, no ezdxf, no DEM.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

import requests

from cad_rules import LAYERS


PUBLIC_OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# An Overpass instance the operator controls, from MAPS2CAD_OVERPASS. The
# public mirrors decline an address that has asked for too much — a shared
# cloud IP answers ECONNREFUSED while its network is fine — and no number
# of public mirrors fixes that, because the refusal is about the caller.
#
# Comma- or whitespace-separated, so a primary and a standby can both be
# named. They go *first* and the public mirrors stay behind them: a private
# instance that is down should fall back, not fail.
#
# Only ever read from the environment, never from a request. serve.py takes
# basemap providers by name for the same reason — a URL accepted from a
# browser form would let anyone point this fetcher at any host.
OVERPASS_ENV = "MAPS2CAD_OVERPASS"


def normalise_overpass(url: str) -> str:
    """One endpoint in the form this stack POSTs to.

    The two stacks want different shapes — this one posts to
    `.../api/interpreter`, generate_detailed_site_map.py hands osmnx
    `.../api` and osmnx appends the rest. Someone setting one environment
    variable should not have to know that, so either form is accepted here
    and normalised.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/interpreter"):
        return url
    return url + "/interpreter" if url.endswith("/api") else url + "/interpreter"


def overpass_urls(env_value=None) -> list[str]:
    """Operator endpoints first, then the public mirrors."""
    raw = os.environ.get(OVERPASS_ENV, "") if env_value is None else env_value
    mine = [normalise_overpass(u) for u in raw.replace(",", " ").split()]
    mine = [u for u in mine if u]
    return mine + [u for u in PUBLIC_OVERPASS_URLS if u not in mine]


OVERPASS_URLS = overpass_urls()


HEADERS = {"User-Agent": "topo2cad/1.0 (personal CAD export script)"}


def bbox_around(lat, lon, radius_m, width_m=None, height_m=None):
    """Square box of +/-radius_m, or a width_m x height_m rectangle if given."""
    half_w = width_m / 2 if width_m else radius_m
    half_h = height_m / 2 if height_m else radius_m
    dlat = half_h / 111320.0
    dlon = half_w / (111320.0 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon  # S, W, N, E


def fetch_osm(s, w, n, e, everything=False, cache=True):
    """Elements in the box. `everything` asks for every *tagged* element
    rather than the curated tag list.

    The curated query is the default because a submission drawing wants the
    features a reviewer reads, not every bench and bin. But "the map shows
    it and the drawing does not" is a fair complaint, and this is the
    answer to it: nothing tagged is left behind, and whatever no rule
    claims lands on C-MISC-OTHR rather than being dropped.
    """
    if everything:
        # [~"."~"."] is Overpass for "carries at least one tag", which
        # skips the untagged nodes and ways that are only geometry for
        # something else — asking for those would multiply the response
        # by ten and add nothing drawable.
        query = f"""
        [out:json][timeout:180];
        nwr[~"."~"."]({s},{w},{n},{e});
        out tags geom;
        """
        return _post_overpass(query, cache=cache)
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
    return _post_overpass(query, cache=cache)


# Overpass is someone else's infrastructure and it is not always up: three
# endpoints failed with 504 within a minute of each other while this was
# being written. A response cache turns that from "the run died" into "the
# run used this morning's answer", makes a repeat run instant, and lets a
# re-plot work with no network at all — the same bargain basemap.py makes
# with tiles and overture.py with places.
OSM_CACHE_DIR = Path(os.environ.get("MAPS2CAD_DATA")
                     or Path(__file__).resolve().parent.parent) \
    / "cache" / "overpass"


# A day. OSM changes, and a submission drawing must not be built from a
# stale snapshot without anyone choosing that — but a same-day re-run, a
# re-plot at another sheet size, or a retry after a failure should not
# re-query. `--refresh-osm` ignores the cache outright.
OSM_CACHE_TTL = 24 * 3600


def _cache_path(query, cache_dir=None):
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:20]
    return Path(cache_dir or OSM_CACHE_DIR) / f"overpass_{digest}.json"


def _post_overpass(query, cache=True, cache_dir=None, ttl=OSM_CACHE_TTL):
    path = _cache_path(query, cache_dir)
    if cache and path.is_file():
        age = time.time() - path.stat().st_mtime
        if age < ttl:
            try:
                elements = json.loads(path.read_text(encoding="utf-8"))
                print(f"Overpass: {len(elements)} element(s) from cache "
                      f"({age / 3600:.1f} h old)")
                return elements
            except (ValueError, OSError):
                pass            # a truncated cache file is not an error
    last_err = None
    for attempt in range(3):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=300)
                r.raise_for_status()
                elements = r.json()["elements"]
                if cache:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps(elements),
                                        encoding="utf-8")
                    except OSError as exc:      # read-only disk, full disk
                        print(f"  (not cached: {exc})")
                return elements
            except Exception as exc:
                last_err = exc
                print(f"Overpass endpoint failed ({url}): {exc}")
        wait = 20 * (attempt + 1)
        print(f"All endpoints failed, retrying in {wait}s...")
        time.sleep(wait)
    # Every endpoint is down. An expired cache entry is a far better answer
    # than no drawing at all — say how old it is and let the run continue.
    if cache and path.is_file():
        try:
            elements = json.loads(path.read_text(encoding="utf-8"))
            age = (time.time() - path.stat().st_mtime) / 3600
            print(f"WARNING: Overpass is unreachable — drawing from a "
                  f"cached response {age:.1f} h old ({len(elements)} "
                  f"elements). Re-run when it is back for current data.")
            return elements
        except (ValueError, OSError):
            pass
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


def ms_release_from_url(url: str) -> dict:
    """Region and release date out of a dataset-links.csv row's URL.

    The path is .../global-buildings/<release>/global-buildings.geojsonl/
    RegionName=<region>/quadkey=<key>/part-... — the only place the release
    is stated. Pure, so the parsing is testable without the 30,000-line
    index or the network.
    """
    out = {}
    for part in url.split("/"):
        if part.startswith("RegionName="):
            out["region"] = part.split("=", 1)[1]
        elif part.startswith("quadkey="):
            out["quadkey"] = part.split("=", 1)[1]
    marker = "/global-buildings/"
    if marker in url:
        rest = url.split(marker, 1)[1].split("/", 1)[0]
        # The next path element is the release date on a data URL and
        # "dataset-links.csv" on the index itself; only take a date.
        if len(rest) == 10 and rest.count("-") == 2:
            out["release"] = rest
    return out


def ms_source_tags(s, w, n, e, cache_dir) -> dict:
    """What to say on an ML footprint about where it came from.

    A predicted outline and a traced one are indistinguishable once they
    are both black polylines on C-BLDG-UNNM, and the difference is exactly
    what a reviewer needs before treating one as survey. `method` is the
    load-bearing word; the release makes the run reproducible, since
    Microsoft reissues a region and the outlines move.

    Reads the already-cached index — no network. Returns {} when the index
    is missing, so a drawing is never lost to a provenance lookup.
    """
    tags = {"source": "microsoft_ml",
            "dataset": "Microsoft Global ML Building Footprints",
            "method": "predicted from imagery, not surveyed",
            "licence": "ODbL"}
    links = Path(cache_dir) / "dataset-links.csv"
    if not links.exists():
        return tags
    keys = {quadkey(la, lo) for la in (s, n) for lo in (w, e)}
    seen = {}
    try:
        for line in links.read_text().splitlines():
            cols = line.split(",")
            if len(cols) > 2 and cols[1] in keys:
                seen |= ms_release_from_url(cols[2])
    except OSError:
        return tags
    # The quadkey is per tile and an extent can straddle four of them, so
    # it is deliberately not carried: one value would be wrong for the
    # other three footprints it labelled.
    for key in ("region", "release"):
        if seen.get(key):
            tags[key] = seen[key]
    return tags


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


# The keys that say what a feature *is*, in the order OSM itself treats as
# primary. Picking "the first key that is not obviously a qualifier"
# reported features as `air_conditioning` and `operator:en`, which tells a
# reader nothing about their drawing.
PRIMARY_TAGS = ("amenity", "shop", "office", "craft", "healthcare",
                "tourism", "historic", "leisure", "highway", "railway",
                "aeroway", "waterway", "natural", "landuse", "man_made",
                "power", "barrier", "building", "place", "boundary",
                "emergency", "military", "public_transport", "entrance",
                "advertising", "attraction", "geological")


def _first_tag(tags) -> str:
    """The key that best says what this feature is."""
    for key in PRIMARY_TAGS:
        if tags.get(key):
            return key
    # Nothing primary: fall back to the first key that is not a name, an
    # address part, or bookkeeping a mapper left behind.
    for key in sorted(tags):
        if not key.startswith(("name", "addr:", "source", "note", "fixme",
                               "check_date", "wikipedia", "wikidata",
                               "operator", "ref", "description", "layer",
                               "level", "created_by")):
            return key
    return next(iter(sorted(tags)), "")


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


def classify_elements(elements, curated=True, keep_other=False):
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
    other_lines, other_points = [], []

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
        elif el["type"] == "node" and keep_other and tags:
            # Every tagged node no rule above claimed: a bench, a bus stop,
            # a shop, a hydrant. Named or not — the symbol says something
            # is there, which is the point of asking for everything.
            other_points.append((names_by_lang(tags), el["lon"], el["lat"],
                                 f"node/{el['id']}", _first_tag(tags)))
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
            elif keep_other and tags and len(pts) >= 2:
                # `tags` matters: an untagged way is a multipolygon's
                # building material, and drawing it here would trace every
                # courtyard wall a second time.
                other_lines.append((names_by_lang(tags), pts, fid))
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
            "zoning": zoning, "parking": parking, "plazas": plazas,
            "other_lines": other_lines, "other_points": other_points}


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


def road_label(tags):
    """Road name with its route number, e.g. 'ถนนอรุณประเสริฐ (ทล.202)'."""
    name = best_name(tags)
    ref = tags.get("ref")
    if name and ref:
        return f"{name} ({ref})"
    return name or (f"ทล.{ref}" if ref else None)


# Fraction of an ML footprint that must already be covered by a building
# before it counts as a duplicate rather than a new one. Half is well clear
# of the metre-scale disagreement between a traced and a modelled outline,
# and well below the >90% a genuine duplicate shows.
ML_OVERLAP_MAX = 0.5


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
