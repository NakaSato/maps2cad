#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "ezdxf",
#   "shapely>=2.0",
#   "pyproj>=3.6",
# ]
# ///
"""Draw a DXF from the SQLite staging layer — no network, no DEM.

Every label anchor and rotation was computed at staging time, so drawing is
plain SELECTs. Use this to re-issue a drawing after correcting names,
without re-fetching from Overpass:

    uv run scripts/stage_db.py --db output/staging.sqlite --info
    uv run scripts/stage_db.py --db output/staging.sqlite \\
        --set-name ms/00042="ศาลาประชาคม" --project 1
    uv run scripts/db2dxf.py --db output/staging.sqlite --project 1 \\
        --out output/revised.dxf
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path

# Same NCS layer set the extraction path writes, so drawings from either
# route drop into the same sheet set.
LAYER_STYLE = {
    "C-BLDG-OUTL": (4, 50),
    "C-ROAD-EDGE": (30, 35),
    "C-ROAD-CNTR": (8, 9),
    "C-TOPO-CONT": (8, 13),
    "C-TOPO-MAJR": (8, 25),   # index contours: heavier, labelled
    "C-TOPO-MINR": (8, 9),    # intermediate contours
    "C-ANNO-TEXT": (2, 25),      # language-neutral: B### codes, elevations
    "C-ANNO-TEXT-TH": (2, 25),
    "C-ANNO-TEXT-EN": (7, 25),
    "C-ANNO-NORT": (7, 35),
    "C-ANNO-GPSP": (1, 35),
    "C-PROP-LINE": (1, 70),
    "C-PROP-SETB": (2, 25),
}

# Must match TEXT_STYLES / ANNO_STYLE in topo2cad.py — both routes have to
# hand a drafter the same drawing. AutoCAD renders Thai as ??? without a
# text style bound to a font that carries the Thai block.
TEXT_STYLES = {
    "TH_STYLE": "THSarabunNew.ttf",
    "EN_STYLE": "arial.ttf",
}
ANNO_TEXT_STYLE = {
    "C-ANNO-TEXT": "EN_STYLE",
    "C-ANNO-TEXT-TH": "TH_STYLE",
    "C-ANNO-TEXT-EN": "EN_STYLE",
}


def offset_along_normal(x, y, rotation_deg, distance):
    """Shift a label perpendicular to its own baseline; at rotation 0 this
    is a plain +Y nudge. Mirrors topo2cad.offset_along_normal."""
    rad = math.radians(rotation_deg or 0.0)
    return (x - distance * math.sin(rad), y + distance * math.cos(rad))


def parts(geom, wanted):
    if geom.is_empty:
        return
    if geom.geom_type == wanted:
        yield geom
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            yield from parts(g, wanted)


def road_edges(coords, width_m):
    """Both carriageway edges; falls back to the centreline if offsetting
    fails on a kinked line."""
    from shapely.geometry import LineString

    line = LineString(coords)
    if line.length < 0.5:
        return []
    out = []
    for side in (width_m / 2, -width_m / 2):
        try:
            off = line.offset_curve(side)
        except Exception:
            return [list(line.coords)]
        for p in parts(off, "LineString"):
            if len(p.coords) >= 2:
                out.append(list(p.coords))
    return out or [list(line.coords)]


def resolve_project(conn, wanted):
    rows = conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
    if not rows:
        raise SystemExit("ERROR: no projects staged in this database.")
    if wanted is None:
        if len(rows) > 1:
            names = ", ".join(f"{r['id']}:{r['name']}" for r in rows)
            raise SystemExit(
                f"ERROR: several projects staged ({names}). "
                "Pick one with --project.")
        return rows[0]
    for r in rows:
        if str(r["id"]) == str(wanted) or r["name"] == wanted:
            return r
    names = ", ".join(f"{r['id']}:{r['name']}" for r in rows)
    raise SystemExit(f"ERROR: no project '{wanted}'. Staged: {names}")


def main(argv=None) -> int:
    import ezdxf
    from ezdxf.enums import MTextEntityAlignment
    from shapely import wkb

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--project", help="project id or name")
    ap.add_argument("--out", help="output DXF (default: <project>.dxf)")
    ap.add_argument("--no-labels", action="store_true",
                    help="geometry only, leave C-ANNO-TEXT empty")
    ap.add_argument("--no-contours", action="store_true")
    ap.add_argument("--sheet", choices=["A4", "A3", "A2", "A1", "A0"],
                    help="Add a plottable paper-space layout with title block")
    ap.add_argument("--scale", default="fit",
                    help="Plot scale denominator for --sheet (1:SCALE), or "
                         "'fit' to show the whole extent")
    a = ap.parse_args(argv)

    if not Path(a.db).is_file():
        print(f"ERROR: {a.db} not found", file=sys.stderr)
        return 1
    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    proj = resolve_project(conn, a.project)
    pid = proj["id"]
    out = Path(a.out) if a.out else Path(f"{proj['name']}.dxf")
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    for style, font in TEXT_STYLES.items():
        if style not in doc.styles:
            doc.styles.add(style, font=font)
    for name, (color, lw) in LAYER_STYLE.items():
        layer = doc.layers.add(name, color=color)
        layer.dxf.lineweight = lw
    doc.layers.get("C-PROP-LINE").dxf.linetype = "PHANTOM"
    doc.layers.get("C-PROP-SETB").dxf.linetype = "DASHED"

    # ---- geometry: three SELECTs -------------------------------------
    n_b = 0
    for row in conn.execute("SELECT geom_wkb, cad_layer FROM staging_buildings"
                            " WHERE project_id = ?", (pid,)):
        for poly in parts(wkb.loads(row["geom_wkb"]), "Polygon"):
            msp.add_lwpolyline(list(poly.exterior.coords), close=True,
                               dxfattribs={"layer": row["cad_layer"]})
            for ring in poly.interiors:      # courtyards stay open
                msp.add_lwpolyline(list(ring.coords), close=True,
                                   dxfattribs={"layer": row["cad_layer"]})
            n_b += 1

    n_r = n_e = 0
    for row in conn.execute("SELECT geom_wkb, carriageway_m, cad_layer"
                            " FROM staging_roads WHERE project_id = ?",
                            (pid,)):
        width = row["carriageway_m"] or 5.0
        for line in parts(wkb.loads(row["geom_wkb"]), "LineString"):
            coords = list(line.coords)
            msp.add_lwpolyline(coords,
                               dxfattribs={"layer": row["cad_layer"]})
            n_r += 1
            for edge in road_edges(coords, width):
                msp.add_lwpolyline(edge,
                                   dxfattribs={"layer": "C-ROAD-EDGE"})
                n_e += 1

    n_c = 0
    if not a.no_contours:
        for row in conn.execute("SELECT elevation_m, geom_wkb, cad_layer"
                                " FROM staging_contours WHERE project_id = ?",
                                (pid,)):
            for line in parts(wkb.loads(row["geom_wkb"]), "LineString"):
                msp.add_polyline3d(
                    [(x, y, row["elevation_m"]) for x, y in line.coords],
                    dxfattribs={"layer": row["cad_layer"]})
                n_c += 1

    # ---- annotation: one SELECT against the view ----------------------
    n_t = 0
    if not a.no_labels:
        for row in conn.execute(
                "SELECT text, label_x, label_y, label_rotation, text_height,"
                " cad_layer, label_offset FROM cad_labels"
                " WHERE project_id = ?", (pid,)):
            if row["text"] is None or row["label_x"] is None:
                continue
            layer = row["cad_layer"]
            # The view stacks a feature's English label above its Thai one
            # by handing back a perpendicular distance rather than moved
            # coordinates, so the anchor stays the feature's own point.
            lx, ly = offset_along_normal(row["label_x"], row["label_y"],
                                         row["label_rotation"],
                                         row["label_offset"] or 0.0)
            m = msp.add_mtext(str(row["text"]), dxfattribs={
                "layer": layer,
                "char_height": row["text_height"],
                "style": ANNO_TEXT_STYLE.get(layer, "EN_STYLE")})
            m.set_location(
                (lx, ly),
                rotation=row["label_rotation"],
                attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
            n_t += 1

        # Contour elevations are not in the view (they are numbers, not
        # names). Only index contours are labelled, matching topo2cad.py.
        if not a.no_contours:
            for row in conn.execute(
                    "SELECT elevation_m, label_x, label_y, label_rotation"
                    " FROM staging_contours WHERE project_id = ?"
                    " AND label_x IS NOT NULL AND cad_layer = 'C-TOPO-MAJR'",
                    (pid,)):
                m = msp.add_mtext(f"{row['elevation_m']:g}", dxfattribs={
                    "layer": "C-ANNO-TEXT", "char_height": 2.5,
                    "style": "EN_STYLE"})
                m.set_location((row["label_x"], row["label_y"]),
                               rotation=row["label_rotation"],
                               attachment_point=
                               MTextEntityAlignment.MIDDLE_CENTER)
                n_t += 1

    # Site marker at the staged centre, in project metres
    from pyproj import Transformer
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{proj['srid']}",
                                  always_xy=True)
    cx, cy = to_utm.transform(proj["lon"], proj["lat"])

    # North arrow, derived from the staged extent (drawing furniture, so it
    # is not staged — the drawing is true-north-up in UTM either way)
    half_w, half_h = proj["width_m"] / 2, proj["height_m"] / 2
    ax_ = cx + half_w * 0.94
    ay = cy + half_h * 0.90
    sz = min(proj["width_m"], proj["height_m"]) * 0.02
    msp.add_circle((ax_, ay), radius=sz, dxfattribs={"layer": "C-ANNO-NORT"})
    msp.add_solid([(ax_ - sz * 0.3, ay - sz * 0.6),
                   (ax_ + sz * 0.3, ay - sz * 0.6),
                   (ax_, ay + sz * 0.8)],
                  dxfattribs={"layer": "C-ANNO-NORT"})
    n = msp.add_mtext("N", dxfattribs={"layer": "C-ANNO-TEXT",
                                       "char_height": sz * 0.6,
                                       "style": "EN_STYLE"})
    n.set_location((ax_, ay + sz * 1.5),
                   attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
    n_t += 1
    msp.add_circle((cx, cy), radius=5, dxfattribs={"layer": "C-ANNO-GPSP"})
    m = msp.add_mtext(f"GPS {proj['lat']},{proj['lon']}",
                      dxfattribs={"layer": "C-ANNO-TEXT", "char_height": 5.0,
                                  "style": "EN_STYLE"})
    m.set_location((cx + 40, cy),
                   attachment_point=MTextEntityAlignment.MIDDLE_CENTER)

    if a.sheet:
        import datetime
        import sheet as sheet_mod
        if str(a.scale).lower() == "fit":
            a.scale, _, _ = sheet_mod.fitting_scale(
                proj["width_m"], proj["height_m"], a.sheet)
        else:
            a.scale = int(a.scale)
        sheet_mod.add_sheet(doc, {
            "project": proj["name"], "lat": proj["lat"], "lon": proj["lon"],
            "centre": (cx, cy), "srid": proj["srid"],
            "extent": (proj["width_m"], proj["height_m"]),
            "date": datetime.date.today().isoformat(),
        }, size=a.sheet, scale=a.scale)
        print(f"  sheet: {a.sheet} paper space at 1:{a.scale:,}")

    doc.saveas(out)
    conn.close()
    print(f"Project '{proj['name']}' (EPSG:{proj['srid']}, "
          f"{proj['width_m']:.0f} x {proj['height_m']:.0f} m)")
    print(f"  {n_b} building outlines, {n_r} road centrelines "
          f"(+{n_e} edges), {n_c} contours, {n_t} MTEXT")
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
