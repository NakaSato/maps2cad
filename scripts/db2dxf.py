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
    "C-BLDG-UNNM": (254, 35),    # footprints OSM has no name for
    "C-ROAD-EDGE": (30, 35),
    "C-ROAD-CNTR": (8, 9),
    "C-ROAD-PATH": (8, 13),      # footways: one line, no edge of pavement
    "C-ROAD-ARRW": (30, 18),     # one-way direction arrows
    "C-ROAD-BRDG": (7, 40),      # bridges: heavier, over what they cross
    "C-ROAD-TUNL": (8, 18),      # tunnels: HIDDEN, under the ground
    "C-ROAD-ROWY": (1, 35),      # right of way, empty and ready to draw
    "C-TOPO-CONT": (8, 13),
    "C-TOPO-MAJR": (8, 25),   # index contours: heavier, labelled
    "C-TOPO-MINR": (8, 9),    # intermediate contours
    "C-ANNO-TEXT": (2, 25),      # language-neutral: B### codes, elevations
    "C-ANNO-TEXT-TH": (2, 25),
    "C-ANNO-TEXT-EN": (7, 25),
    "C-HYDR-WATR": (5, 18),      # context linework: canals, ponds
    "C-LAND-VEGT": (3, 13),      # parks, farmland, cemeteries
    "C-LAND-ZONE": (32, 13),     # built-up land use: residential, industrial
    "C-SITE-PARK": (140, 13),    # parking areas
    "C-ANNO-GRID": (253, 9),     # UTM coordinate grid
    "C-ANNO-DIMS": (2, 18),      # extent dimensions
    "C-ROAD-PLAZ": (8, 18),      # pedestrian areas and plazas
    "C-UTIL-LAMP": (51, 13),     # street lighting
    "C-MISC-OTHR": (9, 9),       # --all-features: whatever no rule claimed
    "C-MISC-SYMB": (9, 9),
    "C-RAIL-TRAK": (250, 18),
    "C-BNDY-BARR": (9, 13),      # walls and fences
    "C-ANNO-SYMB": (6, 18),      # landmark point symbols
    "C-UTIL-POWR": (6, 25),      # power lines, pylons and poles
    "C-UTIL-PIPE": (4, 18),      # pipelines
    "C-LAND-TREE": (3, 13),      # individual trees
    "C-ANNO-ADDR": (8, 13),      # house numbers
    "C-TOPO-SPOT": (8, 18),      # spot heights sampled from the DEM
    "C-SITE-POI": (5, 25),       # landmark grounds with no building tag
    # Named places from a third-party source (Overture), with their own
    # annotation layers so a drafter can freeze C-ANNO-OVTR* and be back to
    # what OpenStreetMap says. Must match LAYERS in topo2cad.py.
    "C-ANNO-OVTR": (214, 13),
    "C-ANNO-OVTR-TH": (214, 18),
    "C-ANNO-OVTR-EN": (214, 18),
    "C-ANNO-EXTN": (7, 35),      # crop rectangle on the requested extent
    "C-ANNO-NORT": (7, 35),
    "C-ANNO-GPSP": (1, 35),
    "C-PROP-LINE": (1, 70),
    "C-PROP-SETB": (2, 25),
}

# Must match TEXT_STYLES / ANNO_STYLE in topo2cad.py — both routes have to
# hand a drafter the same drawing. AutoCAD renders Thai as ??? without a
# text style bound to a font that carries the Thai block.
# MTEXT background mask, as a multiple of the text height. 'canvas'
# means the drawing's own background, so a label crossing a building
# outline or a road edge cuts a clean hole rather than overprinting.
# Passing None here would REMOVE the mask, not add one.
BG_MASK_SCALE = 1.1

TEXT_STYLES = {
    "TH_STYLE": "THSarabunNew.ttf",
    "EN_STYLE": "arial.ttf",
}
ANNO_TEXT_STYLE = {
    "C-ANNO-TEXT": "EN_STYLE",
    "C-ANNO-TEXT-TH": "TH_STYLE",
    "C-ANNO-TEXT-EN": "EN_STYLE",
    # Digits either way, and off both language layers on purpose
    "C-ANNO-ADDR": "EN_STYLE",
    "C-TOPO-SPOT": "EN_STYLE",
    "C-ANNO-GRID": "EN_STYLE",
    "C-ANNO-OVTR": "EN_STYLE",
    "C-ANNO-OVTR-TH": "TH_STYLE",
    "C-ANNO-OVTR-EN": "EN_STYLE",
}


def offset_along_normal(x, y, rotation_deg, distance):
    """Shift a label perpendicular to its own baseline; at rotation 0 this
    is a plain +Y nudge. Mirrors topo2cad.offset_along_normal."""
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

    # Placement rules the extraction route also calls, so a drawing and its
    # re-issue agree on where a label anchor and a direction arrow land.
    import blocks
    import stage_db

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--project", help="project id or name")
    ap.add_argument("--out", help="output DXF (default: <project>.dxf)")
    ap.add_argument("--no-labels", action="store_true",
                    help="geometry only, leave C-ANNO-TEXT empty")
    ap.add_argument("--no-contours", action="store_true")
    ap.add_argument("--no-spots", action="store_true",
                    help="Leave the staged spot heights off the drawing")
    ap.add_argument("--grid", nargs="?", const="auto", metavar="SPACING",
                    help="Draw the UTM coordinate grid (same rule as "
                         "topo2cad.py --grid)")
    ap.add_argument("--hatch", action="store_true",
                    help="Hatch water and vegetation areas (same patterns "
                         "topo2cad.py --hatch uses)")
    ap.add_argument("--no-attributes", action="store_true",
                    help="Do not re-attach the staged OSM tags as XDATA, and "
                         "do not write attributes.csv")
    ap.add_argument("--mono", action="store_true",
                    help="Monochrome: every layer on ACI 7 (same as "
                         "topo2cad.py --mono)")
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
    doc.layers.get("C-ROAD-ROWY").dxf.linetype = "PHANTOM"
    doc.layers.get("C-ROAD-CNTR").dxf.linetype = "CENTER"
    doc.layers.get("C-ANNO-EXTN").dxf.linetype = "DASHED"
    doc.layers.get("C-ROAD-TUNL").dxf.linetype = "HIDDEN"
    doc.header["$LTSCALE"] = 5.0

    # The source OSM tags were staged with the features, so a re-issue is not
    # a drawing stripped of its attributes. Same appid, same 255-byte clip,
    # same order as the extraction routes — the rules live in stage_db.
    feature_tags = {} if a.no_attributes else stage_db.tags_by_feature(conn,
                                                                       pid)
    # One project can hold both: OSM tags on the extraction, a survey's own
    # fields on what gis2cad imported beside it.
    for appid in {ap for ap, _tags in feature_tags.values()}:
        if appid not in doc.appids:
            doc.appids.add(appid)

    def attach(entity, feature_id):
        appid, tags = feature_tags.get(feature_id, (None, None))
        if tags and entity is not None:
            entity.set_xdata(appid, stage_db.xdata_tags(feature_id, tags))

    # ---- geometry: three SELECTs -------------------------------------
    n_b = 0
    for row in conn.execute("SELECT feature_id, geom_wkb, cad_layer FROM"
                            " staging_buildings WHERE project_id = ?", (pid,)):
        first = True
        for poly in parts(wkb.loads(row["geom_wkb"]), "Polygon"):
            entity = msp.add_lwpolyline(
                list(poly.exterior.coords), close=True,
                dxfattribs={"layer": row["cad_layer"]})
            if first:
                attach(entity, row["feature_id"])
                first = False
            for ring in poly.interiors:      # courtyards stay open
                msp.add_lwpolyline(list(ring.coords), close=True,
                                   dxfattribs={"layer": row["cad_layer"]})
            n_b += 1

    n_r = n_e = n_a = 0
    for row in conn.execute("SELECT feature_id, geom_wkb, carriageway_m,"
                            " cad_layer, oneway FROM staging_roads"
                            " WHERE project_id = ?", (pid,)):
        # A footway stages with carriageway_m = 0 and its own cad_layer, so
        # it draws as one line with no edge of pavement — matching
        # topo2cad.py, which never offsets a 1.5 m path.
        width = row["carriageway_m"]
        width = 5.0 if width is None else width
        for line in parts(wkb.loads(row["geom_wkb"]), "LineString"):
            coords = list(line.coords)
            attach(msp.add_lwpolyline(
                coords, dxfattribs={"layer": row["cad_layer"]}),
                row["feature_id"])
            n_r += 1
            if width > 0:
                for edge in road_edges(coords, width):
                    msp.add_lwpolyline(edge,
                                       dxfattribs={"layer": "C-ROAD-EDGE"})
                    n_e += 1
            # Direction arrows, placed by stage_db's rule — the same call
            # the extraction route makes, so a re-issue puts them on the
            # same metre. Paths stage with carriageway_m = 0 and get none.
            if row["oneway"] and width > 0:
                size = stage_db.oneway_arrow_size(width)
                for ax, ay, rot in stage_db.arrow_positions(coords):
                    blocks.add_oneway_arrow(
                        doc, msp, ax, ay, size,
                        rot + (180.0 if row["oneway"] < 0 else 0.0),
                        "C-ROAD-ARRW")
                    n_a += 1

    # Context linework. A run whose first and last vertex coincide was drawn
    # closed by topo2cad.py — a pond or a park boundary — so the closed flag
    # is recovered from the coordinates rather than stored beside them.
    n_x = 0
    for row in conn.execute("SELECT feature_id, geom_wkb, cad_layer FROM"
                            " staging_context WHERE project_id = ?", (pid,)):
        for line in parts(wkb.loads(row["geom_wkb"]), "LineString"):
            coords = list(line.coords)
            if len(coords) < 2:
                continue
            attach(msp.add_lwpolyline(
                coords, close=coords[0] == coords[-1],
                dxfattribs={"layer": row["cad_layer"]}), row["feature_id"])
            n_x += 1

    # Flow direction on waterways: derived, not staged — an open run on
    # the water layer has a direction, a closed one is a pond.
    n_f = 0
    for row in conn.execute("SELECT kind, geom_wkb, cad_layer FROM"
                            " staging_context WHERE project_id = ?"
                            " ORDER BY feature_id", (pid,)):
        if row["kind"] != "water":
            continue
        for line in parts(wkb.loads(row["geom_wkb"]), "LineString"):
            coords = list(line.coords)
            if len(coords) >= 2 and coords[0] != coords[-1]:
                for ax, ay, rot in stage_db.arrow_positions(coords):
                    blocks.add_oneway_arrow(doc, msp, ax, ay,
                                            stage_db.FLOW_ARROW_M, rot,
                                            row["cad_layer"])
                    n_f += 1

    # Landmark point symbols. The areas came through staging_buildings above
    # already, carrying their own C-SITE-POI cad_layer.
    n_p = 0
    for row in conn.execute("SELECT feature_id, geom_wkb, cad_layer FROM"
                            " staging_pois WHERE project_id = ?", (pid,)):
        for pt in parts(wkb.loads(row["geom_wkb"]), "Point"):
            layer = row["cad_layer"]
            attach(blocks.add_symbol(doc, msp, pt.x, pt.y,
                                     blocks.symbol_size(layer), layer),
                   row["feature_id"])
            n_p += 1

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

    # Spot heights: the elevation this route cannot sample for itself, so
    # it draws exactly what extraction staged.
    n_s = 0
    if not a.no_spots:
        for row in conn.execute(
                "SELECT x, y, elevation_m, cad_layer FROM staging_spots"
                " WHERE project_id = ? ORDER BY y, x", (pid,)):
            msp.add_circle((row["x"], row["y"]), radius=0.6,
                           dxfattribs={"layer": row["cad_layer"]})
            m = msp.add_mtext(f"{row['elevation_m']:+.1f}", dxfattribs={
                "layer": row["cad_layer"], "char_height": 2.5,
                "style": ANNO_TEXT_STYLE.get(row["cad_layer"], "EN_STYLE")})
            m.set_location((row["x"] + 2.5, row["y"]),
                           attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
            m.set_bg_color("canvas", scale=BG_MASK_SCALE)
            n_s += 1

    # Hatching, from the same rows and the same patterns the extraction
    # route uses — stage_db has no hatch of its own to disagree with.
    n_h = 0
    if a.hatch:
        for row in conn.execute(
                "SELECT kind, geom_wkb, cad_layer FROM staging_context"
                " WHERE project_id = ? ORDER BY feature_id", (pid,)):
            if row["kind"] not in stage_db.HATCH_PATTERNS:
                continue
            for line in parts(wkb.loads(row["geom_wkb"]), "LineString"):
                coords = list(line.coords)
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    stage_db.hatch_area(msp, coords, row["kind"],
                                        row["cad_layer"])
                    n_h += 1

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
            m.set_bg_color("canvas", scale=BG_MASK_SCALE)
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
                m.set_bg_color("canvas", scale=BG_MASK_SCALE)
                n_t += 1

    # Site marker at the staged centre, in project metres
    from pyproj import Transformer
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{proj['srid']}",
                                  always_xy=True)
    cx, cy = to_utm.transform(proj["lon"], proj["lat"])

    def grid_text(text, x, y, height, rotation):
        m = msp.add_mtext(text, dxfattribs={
            "layer": "C-ANNO-GRID", "char_height": height,
            "style": "EN_STYLE"})
        m.set_location((x, y), rotation=rotation,
                       attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
        m.set_bg_color("canvas", scale=BG_MASK_SCALE)
        return m

    # North arrow, derived from the staged extent (drawing furniture, so it
    # is not staged — the drawing is true-north-up in UTM either way)
    half_w, half_h = proj["width_m"] / 2, proj["height_m"] / 2
    # Crop rectangle, from the same staged extent topo2cad.py draws it from
    msp.add_lwpolyline([(cx - half_w, cy - half_h), (cx + half_w, cy - half_h),
                        (cx + half_w, cy + half_h), (cx - half_w, cy + half_h)],
                       close=True, dxfattribs={"layer": "C-ANNO-EXTN"})
    ax_ = cx + half_w * 0.94
    ay = cy + half_h * 0.90
    sz = min(proj["width_m"], proj["height_m"]) * 0.02
    import blocks
    if a.grid:
        spacing = (stage_db.grid_spacing(proj["width_m"], proj["height_m"])
                   if str(a.grid) == "auto" else float(a.grid))
        eastings, northings = stage_db.grid_ticks(
            cx, cy, proj["width_m"], proj["height_m"], spacing)
        arm = min(proj["width_m"], proj["height_m"]) * 0.006
        for gx in eastings:
            for gy in northings:
                msp.add_line((gx - arm, gy), (gx + arm, gy),
                             dxfattribs={"layer": "C-ANNO-GRID"})
                msp.add_line((gx, gy - arm), (gx, gy + arm),
                             dxfattribs={"layer": "C-ANNO-GRID"})
        for gx in eastings:
            grid_text(f"{gx:,.0f} E", gx, cy - proj["height_m"] / 2 + arm * 2,
                      arm * 1.6, 0.0)
        for gy in northings:
            grid_text(f"{gy:,.0f} N", cx - proj["width_m"] / 2 + arm * 2, gy,
                      arm * 1.6, 90.0)

    blocks.add_extent_dimensions(doc, msp, cx, cy, proj["width_m"],
                                 proj["height_m"], "C-ANNO-DIMS")
    blocks.add_north_arrow(doc, msp, ax_, ay, sz, "C-ANNO-NORT")
    msp.add_circle((cx, cy), radius=5, dxfattribs={"layer": "C-ANNO-GPSP"})
    m = msp.add_mtext(f"GPS {proj['lat']},{proj['lon']}",
                      dxfattribs={"layer": "C-ANNO-TEXT", "char_height": 5.0,
                                  "style": "EN_STYLE"})
    m.set_location((cx + 40, cy),
                   attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
    m.set_bg_color("canvas", scale=BG_MASK_SCALE)

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

    if a.mono:
        apply_mono(doc)
    doc.saveas(out)

    # The attribute table travels with the re-issue too, or a corrected
    # drawing would arrive without the source data the first one had.
    n_at = 0
    if feature_tags:
        rows = [dict(r) for r in conn.execute(
            "SELECT feature_id, feature_type, cad_layer, display_name,"
            " key, value FROM staging_tags WHERE project_id = ?"
            " ORDER BY feature_id, key", (pid,))]
        n_at = stage_db.write_attribute_csv(
            out.with_name("attributes.csv"), rows)
    conn.close()
    print(f"Project '{proj['name']}' (EPSG:{proj['srid']}, "
          f"{proj['width_m']:.0f} x {proj['height_m']:.0f} m)")
    print(f"  {n_b} building outlines, {n_r} road centrelines "
          f"(+{n_e} edges, {n_a} one-way arrows), {n_c} contours, "
          f"{n_x} context lines, {n_p} POI symbols, {n_t} MTEXT")
    if n_s or n_h or n_f:
        print(f"  {n_s} spot heights, {n_h} hatched area(s), "
              f"{n_f} flow arrows")
    if n_at:
        print(f"  {n_at} source tags re-attached as XDATA and written to "
              f"{out.with_name('attributes.csv').name}")
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
