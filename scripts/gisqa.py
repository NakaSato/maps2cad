#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "pyproj>=3.6",
#   "shapely>=2.0",
# ]
# ///
"""Check the OSM data for a site against Microsoft's ML footprints.

Two independent sources see the same ground: OpenStreetMap, traced by
people, and Microsoft's building footprints, predicted from imagery. Where
they agree, the geometry is probably right. Where they disagree, something
is worth a look before the drawing goes out:

    uv run scripts/gisqa.py --lat 15.83384548 --lon 104.39445555 \\
        --width 500 --height 400 --out output/gis_quality.csv

It reports, it does not repair. An auto-corrected outline carries 1–3 m of
boundary error and looks exactly as authoritative in a DXF as a surveyed
one — the same reason `underlay.py` refuses to trace buildings for you.
What this produces is a list of feature ids and a reason to check each,
which a drafter or a field team can act on.

The checks:

  * `no_ml_support`   an OSM building the ML layer sees nothing at. Often a
                      demolished building, sometimes a mapping error, and
                      occasionally a roof the model missed.
  * `poor_overlap`    both sources have a building here, but the outlines
                      agree on less than half their combined area (IoU).
                      Usually a traced outline that has drifted.
  * `ml_only`         a footprint OSM has never mapped. `topo2cad.py`
                      already draws these; this counts them so you know how
                      much of the drawing is modelled rather than surveyed.
  * `near_duplicate`  two OSM buildings covering nearly the same ground —
                      typically `building` and `building:part` on one
                      structure, which is upstream data rather than an
                      error, but it doubles the outline in CAD.
  * `sliver`          a footprint under `--min-area` m². Usually a stray
                      node or a fragment of a split polygon.
  * `self_intersect`  a ring that crosses itself, which every writer here
                      has to repair before it can draw it.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Fractions of overlap. IoU (intersection over union) is the standard
# measure of agreement between two outlines: 1.0 is identical, 0.5 is a
# metre or two of drift on a small building, below 0.3 is two different
# buildings.
POOR_OVERLAP = 0.5
DUPLICATE_IOU = 0.8
MIN_AREA_M2 = 4.0


def iou(a, b) -> float:
    """Intersection over union of two polygons, 0.0 when they miss."""
    if a is None or b is None or a.is_empty or b.is_empty:
        return 0.0
    inter = a.intersection(b).area
    if inter <= 0:
        return 0.0
    union = a.union(b).area
    return inter / union if union > 0 else 0.0


def as_polygon(ring):
    """A valid polygon from a ring, or None if it cannot be one."""
    from shapely.geometry import Polygon

    if ring is None or len(ring) < 3:
        return None
    try:
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
    except Exception:
        return None
    if poly.is_empty or poly.area <= 0:
        return None
    return poly


def compare(osm_features, ml_polys, poor=POOR_OVERLAP,
            duplicate=DUPLICATE_IOU, min_area=MIN_AREA_M2):
    """Findings for one site.

    `osm_features` is [(feature_id, polygon, was_valid)], `ml_polys` a list
    of polygons — both already projected to metres, because every threshold
    here is an area in square metres and a fraction of one.
    """
    from shapely.strtree import STRtree

    findings = []
    matched_ml = set()
    tree = STRtree(ml_polys) if ml_polys else None

    for fid, poly, was_valid in osm_features:
        if poly is None:
            findings.append({"feature_id": fid, "issue": "unbuildable",
                             "detail": "ring could not be made a polygon",
                             "area_m2": "", "iou": ""})
            continue
        if not was_valid:
            findings.append({"feature_id": fid, "issue": "self_intersect",
                             "detail": "ring crosses itself; repaired to draw",
                             "area_m2": f"{poly.area:.1f}", "iou": ""})
        if poly.area < min_area:
            findings.append({"feature_id": fid, "issue": "sliver",
                             "detail": f"under {min_area:g} m²",
                             "area_m2": f"{poly.area:.1f}", "iou": ""})
            continue

        # Against the *union* of every ML footprint under this building,
        # not the best single one. A mall is one OSM polygon and five ML
        # pieces of roof; scoring it against the largest piece reported
        # every large building in Pathum Wan as a disagreement, which is
        # the check crying wolf rather than the data being wrong.
        hits = [j for j in (tree.query(poly) if tree is not None else [])
                if ml_polys[j].intersects(poly)]
        matched_ml.update(hits)
        best_score = 0.0
        if hits:
            from shapely.ops import unary_union

            best_score = iou(poly, unary_union([ml_polys[j] for j in hits]))
        if best_score == 0.0:
            findings.append({
                "feature_id": fid, "issue": "no_ml_support",
                "detail": "no ML footprint here — demolished, or mis-mapped",
                "area_m2": f"{poly.area:.1f}", "iou": "0.00"})
        elif best_score < poor:
            findings.append({
                "feature_id": fid, "issue": "poor_overlap",
                "detail": "outlines disagree; check against imagery",
                "area_m2": f"{poly.area:.1f}", "iou": f"{best_score:.2f}"})

    # Near-duplicates within OSM itself: one structure mapped twice
    polys = [(fid, poly) for fid, poly, _v in osm_features if poly is not None]
    if polys:
        tree2 = STRtree([p for _f, p in polys])
        seen = set()
        for i, (fid, poly) in enumerate(polys):
            for j in tree2.query(poly):
                if j <= i or (i, j) in seen:
                    continue
                score = iou(poly, polys[j][1])
                if score >= duplicate:
                    seen.add((i, j))
                    findings.append({
                        "feature_id": fid, "issue": "near_duplicate",
                        "detail": f"covers the same ground as {polys[j][0]}",
                        "area_m2": f"{poly.area:.1f}", "iou": f"{score:.2f}"})

    ml_only = len(ml_polys) - len(matched_ml)
    return findings, ml_only


def main(argv=None) -> int:
    from pyproj import Transformer

    from topo2cad import (bbox_around, classify_elements, fetch_ms_buildings,
                          fetch_osm, utm_epsg_for)

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--width", type=float, default=1000.0)
    ap.add_argument("--height", type=float, default=750.0)
    ap.add_argument("--out", default="gis_quality.csv",
                    help="CSV of findings (default: gis_quality.csv)")
    ap.add_argument("--dem-dir", default="dem",
                    help="directory holding ms_cache/ (default: dem)")
    ap.add_argument("--min-area", type=float, default=MIN_AREA_M2,
                    metavar="M2", help="below this a footprint is a sliver")
    a = ap.parse_args(argv)

    s, w, n, e = bbox_around(a.lat, a.lon, None, a.width, a.height)
    epsg = utm_epsg_for(a.lat, a.lon)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}",
                                  always_xy=True)
    print(f"Checking {a.width:.0f} × {a.height:.0f} m at {a.lat}, {a.lon} "
          f"(EPSG:{epsg})")

    print("  fetching OpenStreetMap...")
    features = classify_elements(fetch_osm(s, w, n, e))
    osm = []
    for _names, (ext, holes), fid in features["buildings"]:
        ux, uy = to_utm.transform(*zip(*ext))
        ring = list(zip(ux, uy))
        poly = as_polygon(ring)
        from shapely.geometry import Polygon
        was_valid = bool(len(ring) >= 3 and Polygon(ring).is_valid)
        osm.append((fid, poly, was_valid))

    print("  fetching Microsoft ML footprints...")
    ml_rings = fetch_ms_buildings(s, w, n, e, Path(a.dem_dir) / "ms_cache")
    ml = []
    for ring in ml_rings:
        ux, uy = to_utm.transform(*zip(*ring))
        poly = as_polygon(list(zip(ux, uy)))
        if poly is not None:
            ml.append(poly)

    findings, ml_only = compare(osm, ml, min_area=a.min_area)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["feature_id", "issue",
                                               "detail", "area_m2", "iou"])
        writer.writeheader()
        writer.writerows(findings)

    counts = {}
    for row in findings:
        counts[row["issue"]] = counts.get(row["issue"], 0) + 1
    print(f"\n  {len(osm)} OSM building(s), {len(ml)} ML footprint(s)")
    print(f"  {ml_only} ML footprint(s) OSM has not mapped "
          "(topo2cad draws these)")
    if counts:
        print("\n  findings:")
        for issue, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"    {issue:<16} {count}")
    else:
        print("\n  no findings — the two sources agree everywhere.")
    if counts.get("poor_overlap"):
        print("\n  Note: ML footprints are traced from *roofs* and OSM "
              "outlines are usually drawn at the wall. In a dense city with "
              "overhangs and towers that is a real, systematic difference — "
              "read poor_overlap as \"worth an eye\", not \"wrong\".")
    print(f"\nWritten: {out}")
    print("Reported, not repaired: an auto-corrected outline looks as "
          "authoritative in a DXF as a surveyed one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
