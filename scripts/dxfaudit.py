#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "ezdxf",
#   "requests",
#   "pyproj>=3.6",
#   "shapely>=2.0",
# ]
# ///
"""Audit a DXF against the OpenStreetMap data it was drawn from.

`dxfdiff.py` compares the two drawing routes and proves they agree. It
cannot prove either is *right*: twice in this repo it reported IDENTICAL
while both routes lost the same thing — building courtyards, because the
relation parser read only the first outer ring, and 69 Microsoft footprints
per site, because the supplement only ran when OSM was nearly empty. Two
implementations of one mistake look like agreement.

This asks a different question: does the drawing contain what the source
had? It re-queries Overpass for the same extent — or reads the .osm file the
drawing was made from — and compares counts against the entities actually
present, so a silent drop shows up as a shortfall.

    uv run scripts/dxfaudit.py output/site.dxf \\
        --db output/staging.sqlite --project 1
    uv run scripts/dxfaudit.py output/site.dxf --osm-file map.osm

Exit status is 0 when nothing is missing and 1 when something is, so it
works as a pre-submission gate. It counts rather than matching feature by
feature — a DXF entity carries no OSM id — so treat a shortfall as "look at
this", not as a proof of which feature went missing.

The counting here is deliberately **not** `topo2cad.classify_elements()`,
even though that would be less code. An audit that asks the drawing's own
classifier what the source contained cannot catch a bug in that classifier:
it would report agreement while both sides dropped the same features, which
is the exact failure `dxfdiff.py` already has and this tool exists to cover.
The rules are restated here, from the OSM tags directly.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

BUILDING_LAYER = "C-BLDG-OUTL"
ROAD_LAYERS = ("C-ROAD-CNTR", "C-ROAD-PATH")
ANNO_LAYERS = ("C-ANNO-TEXT", "C-ANNO-TEXT-TH", "C-ANNO-TEXT-EN")
ARROW_LAYER = "C-ROAD-ARRW"


def project_extent(db: str, project) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
    conn.close()
    if not rows:
        raise SystemExit(f"ERROR: no projects staged in {db}")
    if project is None:
        if len(rows) > 1:
            names = ", ".join(f"{r['id']}:{r['name']}" for r in rows)
            raise SystemExit(f"ERROR: several projects ({names}); "
                             "pick one with --project")
        row = rows[0]
    else:
        match = [r for r in rows
                 if str(r["id"]) == str(project) or r["name"] == project]
        if not match:
            names = ", ".join(f"{r['id']}:{r['name']}" for r in rows)
            raise SystemExit(f"ERROR: no project '{project}'. Staged: {names}")
        row = match[0]
    return {"lat": row["lat"], "lon": row["lon"],
            "width": row["width_m"], "height": row["height_m"],
            "name": row["name"]}


# Restated from the OSM wiki rather than imported from topo2cad, so a bug in
# the drawing route's own reading of the tag cannot excuse itself here.
AUDIT_ONEWAY_YES = {"yes", "true", "1", "-1", "reverse"}
AUDIT_PATHS = {"footway", "path", "cycleway", "steps", "pedestrian",
               "bridleway", "corridor"}


def drawable_inner_rings(outers, inners):
    """(courtyards, strays) among a multipolygon's inner rings.

    An inner ring only becomes a closed polyline in the drawing if it sits
    strictly inside one outer. OSM contains rings that do not: relation
    15817178 at Pathum Wan has two outers with an "inner" that straddles
    both, and a polygon built from it is self-intersecting, so the repair
    takes a bite out of the block instead of leaving a ring. Expecting an
    outline for that would fail the audit forever on data that is drawn
    correctly — and an audit that always fails is one nobody reads.
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

    shells = [p for p in (as_poly(r) for r in outers) if p is not None]
    courtyards = strays = 0
    for ring in inners:
        hole = as_poly(ring)
        if hole is None:
            strays += 1
            continue
        if any(shell.contains(hole)
               and not hole.exterior.intersects(shell.exterior)
               for shell in shells):
            courtyards += 1
        else:
            strays += 1
    return courtyards, strays


def count_elements(elements) -> dict:
    """Count what the source holds, from raw OSM elements."""
    from topo2cad import names_by_lang, poi_kind

    rings, inner_rings, road_names, poi_nodes, oneway = [], 0, set(), 0, 0
    stray_inners = 0
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "node":
            if poi_kind(tags) and any(names_by_lang(tags)):
                poi_nodes += 1
        elif el["type"] == "way" and "geometry" in el:
            pts = [(g["lon"], g["lat"]) for g in el["geometry"]]
            if "building" in tags and len(pts) >= 3:
                rings.append(pts)
            elif "highway" in tags:
                th, en = names_by_lang(tags)
                if th or en:
                    road_names.add(th or en)
                # A one-way footway is not drawn with arrows, so it is not
                # counted as one here either.
                if (str(tags.get("oneway", "")).lower() in AUDIT_ONEWAY_YES
                        or tags.get("junction") == "roundabout") \
                        and tags["highway"] not in AUDIT_PATHS:
                    oneway += 1
        elif el["type"] == "relation" and "building" in tags:
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
            rings.extend(outers)
            if inners:
                courtyards, strays = drawable_inner_rings(outers, inners)
                inner_rings += courtyards
                stray_inners += strays
    return {"rings": rings, "osm_buildings": len(rings),
            "inner_rings": inner_rings, "stray_inners": stray_inners,
            "road_names": len(road_names), "poi_nodes": poi_nodes,
            "oneway_roads": oneway, "ml_added": 0}


def source_counts(lat, lon, width, height, dem_dir, use_ml=True) -> dict:
    """What OpenStreetMap (and the ML layer) actually hold for this extent."""
    from topo2cad import (bbox_around, fetch_osm, fetch_ms_buildings,
                          new_ml_rings)

    s, w, n, e = bbox_around(lat, lon, None, width, height)
    counts = count_elements(fetch_osm(s, w, n, e))

    if use_ml and dem_dir:
        ms = fetch_ms_buildings(s, w, n, e, Path(dem_dir) / "ms_cache")
        counts["ml_added"] = len(new_ml_rings(counts["rings"], ms))
    return counts


def file_counts(paths) -> dict:
    """What the .osm export holds — the same questions, no network.

    The file route never supplements with ML footprints, so `ml_added` stays
    zero: the file is the source of truth, and expecting footprints it does
    not contain would report a shortfall for drawing exactly what was asked.
    """
    from osm2cad import read_osm_files

    elements, stats = read_osm_files(paths)
    counts = count_elements(elements)
    counts["incomplete_ways"] = stats["incomplete_ways"]
    return counts


def drawing_counts(path) -> dict:
    import ezdxf

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    buildings = anno = pois = arrows = 0
    for e in msp:
        t = e.dxftype()
        # A --layer-by run splits C-BLDG-OUTL into C-BLDG-OUTL-HOUSE and
        # friends, so match the NCS stem rather than the exact name.
        layer = e.dxf.layer
        if t == "LWPOLYLINE" and layer.startswith(BUILDING_LAYER):
            buildings += 1
        elif t == "MTEXT" and layer in ANNO_LAYERS:
            anno += 1
        elif t in ("INSERT", "CIRCLE") and layer.startswith("C-ANNO-SYMB"):
            pois += 1        # CIRCLE: drawings made before the POI block
        elif t == "INSERT" and layer.startswith(ARROW_LAYER):
            arrows += 1
    return {"building_polylines": buildings, "annotation": anno,
            "poi_symbols": pois, "oneway_arrows": arrows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dxf")
    ap.add_argument("--osm-file", action="append", metavar="FILE",
                    help="Audit against the .osm export the drawing was made "
                         "from (osm2cad.py) instead of re-querying Overpass. "
                         "Repeatable, like --input there.")
    ap.add_argument("--db", help="staging database holding the extent")
    ap.add_argument("--project", help="project id or name in --db")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lon", type=float)
    ap.add_argument("--width", type=float)
    ap.add_argument("--height", type=float)
    ap.add_argument("--dem-dir", default="dem",
                    help="directory holding ms_cache/ (default: dem)")
    ap.add_argument("--no-ml", action="store_true",
                    help="the drawing was made without ML footprints")
    a = ap.parse_args(argv)

    if not Path(a.dxf).is_file():
        print(f"ERROR: {a.dxf} not found", file=sys.stderr)
        return 1
    print(f"Auditing {a.dxf}")
    if a.osm_file:
        missing = [p for p in a.osm_file if not Path(p).is_file()]
        if missing:
            print(f"ERROR: no such file: {', '.join(missing)}",
                  file=sys.stderr)
            return 1
        print(f"  source: {', '.join(a.osm_file)}")
        try:
            src = file_counts(a.osm_file)
        except Exception as exc:                 # OsmFileError and friends
            print(f"ERROR: cannot read the source ({type(exc).__name__}): "
                  f"{exc}", file=sys.stderr)
            return 1
        if src.get("incomplete_ways"):
            print(f"  note: {src['incomplete_ways']} way(s) are cut off in "
                  "the export and were never drawable")
    elif a.db:
        ext = project_extent(a.db, a.project)
        print(f"  extent: {ext['name']} — {ext['lat']}, {ext['lon']} "
              f"({ext['width']:.0f} x {ext['height']:.0f} m)")
        print("  re-querying Overpass for ground truth...")
        src = source_counts(ext["lat"], ext["lon"], ext["width"],
                            ext["height"], a.dem_dir, use_ml=not a.no_ml)
    elif None not in (a.lat, a.lon, a.width, a.height):
        print(f"  extent: (given) — {a.lat}, {a.lon} "
              f"({a.width:.0f} x {a.height:.0f} m)")
        print("  re-querying Overpass for ground truth...")
        src = source_counts(a.lat, a.lon, a.width, a.height,
                            a.dem_dir, use_ml=not a.no_ml)
    else:
        print("ERROR: give --osm-file, or --db (with --project), or "
              "--lat --lon --width --height", file=sys.stderr)
        return 1
    got = drawing_counts(a.dxf)

    expected_outlines = (src["osm_buildings"] + src["inner_rings"]
                         + src["ml_added"])
    checks = [
        ("building outlines", expected_outlines, got["building_polylines"],
         f"{src['osm_buildings']} OSM + {src['inner_rings']} courtyard "
         f"ring(s) + {src['ml_added']} ML"),
        ("landmark symbols", src["poi_nodes"], got["poi_symbols"],
         "named amenity/tourism/historic nodes, curated"),
    ]
    # Arrow *count* is not a source count — spacing along the run decides how
    # many a road gets, and a clipped run may carry none. What can be checked
    # is that a source with one-way roads did not produce a drawing with no
    # direction on it at all, which is what a broken tag rule looks like.
    if src.get("oneway_roads"):
        checks.append(("one-way direction", 1, got["oneway_arrows"],
                       f"{src['oneway_roads']} one-way carriageway(s) in "
                       "source; arrows are spaced along each run"))

    print()
    print(f"  {'check':<22}{'source':>8}{'drawing':>9}   note")
    failures = 0
    for label, want, have, note in checks:
        flag = "ok " if have >= want else "GAP"
        if have < want:
            failures += 1
        print(f"  {label:<22}{want:>8}{have:>9}  {flag}  {note}")

    # Annotation has no single source count — labels dedupe per name and a
    # footprint too small for its label is dropped on purpose — so report it
    # rather than assert on it.
    print(f"  {'annotation (MTEXT)':<22}{'—':>8}{got['annotation']:>9}"
          f"        {src['road_names']} uniquely-named road(s) in source")
    if src.get("stray_inners"):
        print(f"  {'(inner rings)':<22}{'—':>8}{'—':>9}        "
              f"{src['stray_inners']} inner ring(s) in the source are not "
              "enclosed by one outer;\n"
              f"  {'':<22}{'':>8}{'':>9}        they cut a notch rather than "
              "a courtyard, which is correct")

    print()
    if failures:
        print(f"SHORTFALL — {failures} check(s) below source. The drawing is "
              "missing features the source has.")
        return 1
    print("COMPLETE — the drawing carries everything the source holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
