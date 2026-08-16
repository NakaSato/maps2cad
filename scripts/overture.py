#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "duckdb",
#   "pyproj",
#   "requests",
# ]
# # pyproj and requests are not used here: they are what `topo2cad` imports
# # at module level, and the extent comes from its `bbox_around` so an
# # Overture query covers exactly the box the drawing does.
# ///
"""Named places from Overture Maps, as a second opinion on what is here.

OpenStreetMap is one community's view of a site. Overture publishes a
conflation of several — Meta, Microsoft, Esri, PinMeTo, DAC and OSM itself
— and each place carries the dataset it came from and a confidence score. On a
500 × 400 m extent at Siam Square OSM offers 2 curated landmarks; Overture
offers 3,103 places with names and categories.

    uv run scripts/overture.py --lat 13.7455 --lon 100.5325 \\
        --width 500 --height 400 --min-confidence 0.9

Two things to know before using it on a drawing:

  * **It is not survey data.** Every place keeps its source and confidence,
    and `topo2cad.py --overture` draws them on their own layer so a drafter
    can see which names came from OSM and which from a commercial feed, and
    freeze the latter in one click.
  * **3,103 points is not a site plan.** Filter with `--min-confidence` and
    the category list, or a mall district buries the drawing it was meant
    to inform.

The query runs against Overture's public S3 parquet through DuckDB, which
takes about 20 seconds for the places theme, so results are cached per
extent under `cache/overture/`. The buildings theme was measured at 4.5
minutes for the same box and is deliberately not used here: Microsoft's
quadkey tiles already give footprints at 6 MB a tile.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

# The XDATA application id these ride under in CAD. Not "OSM": labelling a
# Meta or Microsoft record as OpenStreetMap in the attribute browser would
# be a lie, which is the same reason gis2cad.py has its own.
XDATA_APPID = "OVERTURE"

# Set on the fetch subprocess. Its only job is to stop that child spawning
# a child of its own when duckdb is missing there too.
CHILD_ENV = "MAPS2CAD_OVERTURE_FETCH"

S3_BUCKET = "overturemaps-us-west-2"
S3_LIST = (f"https://{S3_BUCKET}.s3.us-west-2.amazonaws.com/"
           "?list-type=2&prefix=release/&delimiter=/")
DEFAULT_MIN_CONFIDENCE = 0.9
# What the cached query asks for. Below this Overture's own documentation
# calls a place a weak signal, and the file would be several times the size
# for rows no drawing should carry.
FETCH_FLOOR = 0.5
CACHE_DIR = Path(os.environ.get("MAPS2CAD_DATA")
                 or Path(__file__).resolve().parent.parent) / "cache" / "overture"


class OvertureError(Exception):
    """The fetch could not produce places, with the reason."""


# Overture's category taxonomy is a few hundred leaves deep — the 500 × 400 m
# box at Siam Square alone returns 101 distinct ones — so this curates the
# same way `POI_SUBMISSION` does on the OSM side, and for the same reason: a
# ผังบริเวณ is read by an officer locating a parcel, and they locate it by
# วัด, โรงเรียน, โรงพยาบาล, สถานีตำรวจ. At confidence >= 0.95 that extent
# still offers 22 japanese_restaurant, 20 clothing_store and 13 jewelry_store,
# which is a mall directory rather than a site plan.
#
# Matched on substrings rather than an exact list on purpose: Overture adds
# leaves between releases, and an exact list silently drops `buddhist_temple`
# the day it appears. The retail words are rejected first because
# `school_supply_store` and `hospital_equipment_store` are shops.
PLACE_REJECT_PARTS = (
    "store", "shop", "restaurant", "cafe", "coffee", "salon", "bar",
    "supply", "boutique", "spa", "dealer", "market", "services",
)
PLACE_KEEP_PARTS = (
    # worship, education, health — the civic fixtures that outlast a tenant
    "temple", "church", "mosque", "shrine", "synagogue", "worship",
    "school", "university", "college", "education", "kindergarten",
    "hospital", "clinic", "medical_center", "health_center",
    # civil authority
    "police", "fire_station", "government", "courthouse", "city_hall",
    "embassy", "post_office", "municipal", "public_and_government",
    # public fixtures and wayfinding anchors
    "museum", "library", "stadium", "park", "monument", "landmark",
    "train_station", "bus_station", "subway", "airport", "ferry",
    "transit", "gas_station", "petrol",
    # A mall is how a parcel is described in a Thai address, so the two
    # anchor classes stay in where the individual units do not.
    "shopping_center", "department_store",
)


def keep_place(category: str) -> bool:
    """Is this category a landmark a submission drawing should carry?"""
    cat = (category or "").lower()
    if not cat:
        return False
    if any(part in cat for part in PLACE_KEEP_PARTS
           if part in ("shopping_center", "department_store")):
        return True
    if any(part in cat for part in PLACE_REJECT_PARTS):
        return False
    return any(part in cat for part in PLACE_KEEP_PARTS)


def latest_release(timeout=60) -> str:
    """Newest release prefix, read from the bucket listing.

    Pinning a release in code means the day Overture publishes the next one
    this either keeps serving stale data or breaks; asking costs one small
    request.
    """
    try:
        with urllib.request.urlopen(S3_LIST, timeout=timeout) as r:
            root = ET.fromstring(r.read())
    except Exception as exc:                       # network, DNS, XML
        raise OvertureError(f"could not list Overture releases: {exc}")
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    names = []
    for prefix in root.findall(".//s3:CommonPrefixes/s3:Prefix", ns):
        text = (prefix.text or "").strip("/").split("/")[-1]
        if text and text != "release":
            names.append(text)
    if not names:
        raise OvertureError("Overture bucket listed no releases")
    return sorted(names)[-1]


def cache_key(box, release) -> str:
    """One file per extent and release.

    Deliberately *not* keyed on the confidence floor: the query costs the
    better part of a minute, and a drafter trying 0.9 then 0.8 then 0.95 to
    see what appears would pay it three times. The cache holds everything
    above FETCH_FLOOR and the floor is applied on the way out.
    """
    s, w, n, e = box
    return f"places_{release}_{s:.5f}_{w:.5f}_{n:.5f}_{e:.5f}.json"


def fetch_places(box, release=None, cache_dir=None, refresh=False):
    """Named places inside the box, cached per extent.

    Returns ([{id, name, category, source, confidence, lon, lat}, ...],
    from_cache). Everything above FETCH_FLOOR — filter it with
    `filter_places()`.
    """
    s, w, n, e = box
    min_confidence = FETCH_FLOOR
    release = release or latest_release()
    cache_dir = Path(cache_dir or CACHE_DIR)
    path = cache_dir / cache_key(box, release)
    if path.is_file() and not refresh:
        return json.loads(path.read_text(encoding="utf-8")), True

    try:
        import duckdb
    except ImportError:
        # A child that still cannot import duckdb must fail, not spawn
        # another child. Under `uv run` the child gets the dependency and
        # this never trips; under a plain interpreter without duckdb it is
        # the difference between one clear error and a fork bomb.
        if os.environ.get(CHILD_ENV):
            raise OvertureError(
                "duckdb is needed to read Overture's parquet and this "
                "interpreter does not have it — add duckdb to the "
                "environment (it is in requirements.txt) or run through "
                "`uv run`")
        # topo2cad.py imports this module but does not declare duckdb: a
        # 20 MB parquet engine has no business in the dependency set of
        # every run when one opt-in flag uses it. So the fetch runs as its
        # own `uv run` of this file, which installs what its PEP 723 header
        # declares, and the cache file is the hand-off. Without this the
        # first extent a drawing asks for would fail on the import while
        # every cached one worked, which is the worst kind of bug to meet.
        return _fetch_via_uv(box, release, cache_dir, path), False

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    query = f"""
        SELECT id,
               names.primary          AS name,
               categories.primary     AS category,
               sources[1].dataset     AS source,
               confidence,
               bbox.xmin              AS lon,
               bbox.ymin              AS lat
        FROM read_parquet('s3://{S3_BUCKET}/release/{release}/'
                          'theme=places/type=place/*')
        WHERE bbox.xmin > {w} AND bbox.xmax < {e}
          AND bbox.ymin > {s} AND bbox.ymax < {n}
          AND confidence >= {min_confidence}
          AND names.primary IS NOT NULL
    """
    try:
        rows = con.execute(query).fetchall()
    except Exception as exc:
        raise OvertureError(f"Overture query failed: {exc}")
    places = [{"id": r[0], "name": r[1], "category": r[2] or "",
               "source": r[3] or "", "confidence": float(r[4] or 0),
               "lon": float(r[5]), "lat": float(r[6])} for r in rows]
    places.sort(key=lambda p: (-p["confidence"], p["name"]))
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(places, ensure_ascii=False), encoding="utf-8")
    return places, False


def _fetch_via_uv(box, release, cache_dir, path):
    """Run this file under `uv run` to fill the cache, then read it."""
    import shutil
    import subprocess

    # `uv run` where uv exists, plain python where it does not — the same
    # fallback serve.py's script_cmd() makes, and for the same reason: the
    # container has no uv and installs the union of dependencies instead.
    # Without this the deploy could never fetch a new extent, only serve
    # cached ones, and would say so as a warning nobody reads.
    uv = shutil.which("uv")
    runner = [uv, "run"] if uv else [sys.executable]
    s, w, n, e = box
    cmd = runner + [str(Path(__file__).resolve()),
                    "--bbox", f"{s},{w},{n},{e}",
                    "--cache-dir", str(cache_dir)]
    if release:
        cmd += ["--release", release]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, CHILD_ENV: "1"})
    if proc.returncode != 0 or not path.is_file():
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise OvertureError("the Overture fetch subprocess failed: "
                            + (detail[-1] if detail else "no output"))
    return json.loads(path.read_text(encoding="utf-8"))


def place_tags(place) -> dict:
    """The attributes that ride with a drawn place.

    Confidence and dataset are the point of carrying these at all: a name
    from a commercial feed is worth having on a plan, and worth being able
    to see where it came from before anyone treats it as surveyed.
    """
    return {"name": place.get("name", ""),
            "category": place.get("category", ""),
            "source": place.get("source", ""),
            "confidence": f"{float(place.get('confidence', 0)):.2f}",
            "dataset": "overture"}


def filter_places(places, min_confidence=DEFAULT_MIN_CONFIDENCE,
                  curated=True):
    """The places a drawing should carry, from everything the cache holds."""
    out = [p for p in places if p.get("confidence", 0) >= min_confidence]
    if curated:
        out = [p for p in out if keep_place(p.get("category"))]
    return out


def drop_known(places, known, metres=25.0):
    """Places OSM already names, so a drawing does not carry both.

    `known` is [(name, lon, lat)] from the OSM side. A match needs the same
    name *and* proximity: two branches of one chain are two places, and the
    same shop mapped twice is one.
    """
    import math

    def norm(text):
        return "".join(str(text).lower().split())

    index = {}
    for name, lon, lat in known:
        index.setdefault(norm(name), []).append((lon, lat))
    kept = []
    for place in places:
        near = index.get(norm(place["name"]), [])
        clash = False
        for lon, lat in near:
            dx = (place["lon"] - lon) * 111320.0 * math.cos(
                math.radians(lat))
            dy = (place["lat"] - lat) * 111320.0
            if math.hypot(dx, dy) <= metres:
                clash = True
                break
        if not clash:
            kept.append(place)
    return kept


def main(argv=None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from topo2cad import bbox_around

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--bbox", metavar="S,W,N,E",
                    help="fetch this box instead of one around --lat/--lon; "
                         "this is how topo2cad.py drives the fetch when its "
                         "own environment has no duckdb")
    ap.add_argument("--cache-dir", help=f"default: {CACHE_DIR}")
    ap.add_argument("--width", type=float, default=200.0)
    ap.add_argument("--height", type=float, default=150.0)
    ap.add_argument("--min-confidence", type=float,
                    default=DEFAULT_MIN_CONFIDENCE,
                    help=f"drop places below this (default "
                         f"{DEFAULT_MIN_CONFIDENCE})")
    ap.add_argument("--all-places", action="store_true",
                    help="keep every category, not only the landmark ones "
                         "a submission drawing carries")
    ap.add_argument("--release", help="Overture release (default: newest)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the cached answer for this extent")
    ap.add_argument("--out", help="write the places to this JSON file too")
    a = ap.parse_args(argv)

    if a.bbox:
        try:
            box = tuple(float(v) for v in a.bbox.split(","))
            if len(box) != 4:
                raise ValueError
        except ValueError:
            print("ERROR: --bbox wants four numbers: S,W,N,E",
                  file=sys.stderr)
            return 1
    elif a.lat is None or a.lon is None:
        print("ERROR: give --lat and --lon, or --bbox S,W,N,E",
              file=sys.stderr)
        return 1
    else:
        box = bbox_around(a.lat, a.lon, None, a.width, a.height)
    try:
        places, cached = fetch_places(box, a.release, cache_dir=a.cache_dir,
                                      refresh=a.refresh)
    except OvertureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    raw = len(places)
    places = filter_places(places, a.min_confidence, not a.all_places)
    print(f"{len(places)} place(s) at confidence >= {a.min_confidence}"
          + (f", curated from {raw}" if len(places) != raw else "")
          + (" (from cache)" if cached else ""))
    by_source = {}
    for p in places:
        by_source[p["source"]] = by_source.get(p["source"], 0) + 1
    for source, count in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"   {source or '(unattributed)':<16} {count}")
    for p in places[:8]:
        print(f"   {p['name'][:34]:36} {p['category'][:22]:24} "
              f"{p['confidence']:.2f}")
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(places, ensure_ascii=False,
                                          indent=1), encoding="utf-8")
        print(f"Written: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
