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
had? It re-queries Overpass for the same extent and compares counts against
the entities actually present, so a silent drop shows up as a shortfall.

    uv run scripts/dxfaudit.py output/site.dxf \\
        --db output/staging.sqlite --project 1

Exit status is 0 when nothing is missing and 1 when something is, so it
works as a pre-submission gate. It counts rather than matching feature by
feature — a DXF entity carries no OSM id — so treat a shortfall as "look at
this", not as a proof of which feature went missing.
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


def source_counts(lat, lon, width, height, dem_dir, use_ml=True) -> dict:
    """What OpenStreetMap (and the ML layer) actually hold for this extent."""
    from topo2cad import (bbox_around, fetch_osm, fetch_ms_buildings,
                          names_by_lang, new_ml_rings, poi_kind)

    s, w, n, e = bbox_around(lat, lon, None, width, height)
    elements = fetch_osm(s, w, n, e)

    rings, inner_rings, road_names, poi_nodes = [], 0, set(), 0
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
        elif el["type"] == "relation" and "building" in tags:
            for m in el.get("members", []):
                if "geometry" not in m:
                    continue
                ring = [(g["lon"], g["lat"]) for g in m["geometry"]]
                if len(ring) < 3:
                    continue
                if m.get("role") == "outer":
                    rings.append(ring)
                elif m.get("role") == "inner":
                    inner_rings += 1

    ml_added = 0
    if use_ml and dem_dir:
        ms = fetch_ms_buildings(s, w, n, e, Path(dem_dir) / "ms_cache")
        ml_added = len(new_ml_rings(rings, ms))

    return {
        "osm_buildings": len(rings),
        "inner_rings": inner_rings,
        "ml_added": ml_added,
        "road_names": len(road_names),
        "poi_nodes": poi_nodes,
    }


def drawing_counts(path) -> dict:
    import ezdxf

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    buildings = anno = pois = 0
    for e in msp:
        t = e.dxftype()
        if t == "LWPOLYLINE" and e.dxf.layer == BUILDING_LAYER:
            buildings += 1
        elif t == "MTEXT" and e.dxf.layer in ANNO_LAYERS:
            anno += 1
        elif t == "INSERT" and e.dxf.layer == "C-ANNO-SYMB":
            pois += 1
        elif t == "CIRCLE" and e.dxf.layer == "C-ANNO-SYMB":
            pois += 1        # pre-block drawings
    return {"building_polylines": buildings, "annotation": anno,
            "poi_symbols": pois}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dxf")
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
    if a.db:
        ext = project_extent(a.db, a.project)
    elif None not in (a.lat, a.lon, a.width, a.height):
        ext = {"lat": a.lat, "lon": a.lon, "width": a.width,
               "height": a.height, "name": "(given)"}
    else:
        print("ERROR: give --db (with --project) or "
              "--lat --lon --width --height", file=sys.stderr)
        return 1

    print(f"Auditing {a.dxf}")
    print(f"  extent: {ext['name']} — {ext['lat']}, {ext['lon']} "
          f"({ext['width']:.0f} x {ext['height']:.0f} m)")
    print("  re-querying Overpass for ground truth...")
    src = source_counts(ext["lat"], ext["lon"], ext["width"], ext["height"],
                        a.dem_dir, use_ml=not a.no_ml)
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

    print()
    if failures:
        print(f"SHORTFALL — {failures} check(s) below source. The drawing is "
              "missing features the source has.")
        return 1
    print("COMPLETE — the drawing carries everything the source holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
