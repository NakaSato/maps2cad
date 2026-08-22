#!/usr/bin/env python3
"""Drawing rules every CAD route applies, with no database in sight.

Four writers reach a DXF — topo2cad.py draws during extraction, osm2cad.py
from an .osm export, gis2cad.py from a supplied GIS file, db2dxf.py from the
SQLite staging layer — and they are required to produce the same drawing.
That only holds because the rules deciding *what a drawing looks like* live
in one place instead of four: where a label anchors, how a ring closes,
where the kerb stops at a junction, how big text is at a plot scale, which
tags ride along as XDATA, what a bearing reads as.

These were in stage_db.py, which grew to 2,000 lines by being both the
staging database and the rulebook. They are separated here because they are
different things: nothing below touches a connection, and the dependency
runs one way — stage_db.py imports this, never the reverse. That is what
lets a rule be tested without a database, and what stops the rulebook
growing a schema.

stage_db.py re-exports every name here, so `stage_db.interior_point(...)`
keeps working; call sites did not have to move and neither did the
documentation that names them.
"""

from __future__ import annotations

import csv
import math
import re
import textwrap
from pathlib import Path


# Mirrors is_thai() in topo2cad.py — U+0E00–U+0E7F is the Thai block. Kept
# local so this module stays importable without the CAD/DEM stack.
THAI_RE = re.compile(r"[฀-๿]")


def is_thai(text) -> bool:
    """True if the string contains any Thai character."""
    return bool(text) and bool(THAI_RE.search(str(text)))


def split_by_script(name, name_th=None, name_en=None):
    """Resolve (name_th, name_en) for a staged feature.

    An explicit tag wins. Otherwise the single resolved name is filed by its
    own script, so a caller that knows nothing about languages — an older
    script, or a test — still gets its label onto the right layer instead of
    losing it.
    """
    th = name_th or None
    en = name_en or None
    if name and not (th or en):
        if is_thai(name):
            th = name
        else:
            en = name
    return th, en


def interior_point(geom):
    """A point guaranteed inside the polygon (centroids are not)."""
    pt = geom.representative_point()
    return pt.x, pt.y


# Coordinate grid. A survey sheet carries one so a reader can pick a
# northing and easting off the paper; this repo drew none, which is the
# sort of omission a reviewer notices before anything else.
GRID_STEPS = (10.0, 20.0, 25.0, 50.0, 100.0, 200.0, 250.0, 500.0, 1000.0,
              2000.0, 5000.0)


def grid_spacing(width_m, height_m, target=6):
    """A round grid interval giving roughly `target` lines across the
    extent — the same family of round numbers a scale bar uses, because a
    grid at 137 m is a grid nobody can read a coordinate off."""
    span = max(float(width_m), float(height_m))
    ideal = span / max(target, 1)
    for step in GRID_STEPS:
        if step >= ideal:
            return step
    return GRID_STEPS[-1]


def grid_ticks(centre_x, centre_y, width_m, height_m, spacing):
    """(eastings, northings) of the grid lines inside the extent.

    Round UTM values, not offsets from the centre: a grid line at
    665,700 E is a number a surveyor can use, one at 665,694.02 is not.
    Computed from the nominal extent so all three writers — which each know
    only the centre and the metres — agree without sharing geometry.
    """
    if spacing <= 0:
        return [], []
    west, east = centre_x - width_m / 2, centre_x + width_m / 2
    south, north = centre_y - height_m / 2, centre_y + height_m / 2

    def series(lo, hi):
        first = math.ceil(lo / spacing) * spacing
        out, value = [], first
        while value <= hi:
            out.append(round(value, 6))
            value += spacing
        return out

    return series(west, east), series(south, north)


# Hatch pattern per context kind, at a scale that reads between 1:500 and
# 1:5000. ANSI31 is the CAD convention for water and AR-SAND for ground
# cover, both in ezdxf's standard pattern table, so a drafter sees the fill
# AutoCAD would draw. It lives here rather than in topo2cad because
# db2dxf.py hatches the same rows and must not import the Overpass side to
# find out how.
HATCH_PATTERNS = {"water": ("ANSI31", 4.0), "green": ("AR-SAND", 0.6)}


def hatch_area(msp, points, kind, layer):
    """Fill one closed run with the pattern its kind uses.

    Not associative: the boundary is a separate polyline on its own layer,
    and a drafter editing the outline expects to refresh the hatch rather
    than have it silently follow.
    """
    pattern, scale = HATCH_PATTERNS[kind]
    hatch = msp.add_hatch(dxfattribs={"layer": layer})
    hatch.set_pattern_fill(pattern, scale=scale)
    hatch.paths.add_polyline_path(points, is_closed=True)
    return hatch


def levels_label(tags) -> str:
    """Storeys or height as a drafter writes it: "3F", or "12.0 m".

    Stored formatted rather than as two numeric columns, deliberately. The
    alternative is a rule in Python for the extraction route and the same
    rule again in the cad_labels view for the re-issue, and two spellings
    of one convention is exactly the drift this layer exists to prevent.

    `building:levels` wins over `height`: a storey count is what a site
    plan annotates, and it survives a mapper who guessed the metres.
    """
    levels = str(tags.get("building:levels", "")).strip()
    if levels:
        try:
            count = float(levels)
        except ValueError:
            count = 0.0
        if 0 < count < 200:
            return f"{count:g}F"
    raw = str(tags.get("height", "")).strip().lower().removesuffix("m").strip()
    try:
        metres = float(raw)
    except ValueError:
        return ""
    return f"{metres:.1f} m" if 0 < metres < 1000 else ""


# ------------------------------------------------------- setting-out table
# A parcel drawn without its corner coordinates is a picture. What a Thai
# reviewer reads off a ผังบริเวณ — and what anyone setting the boundary out
# on the ground works from — is a table: each corner by number, its easting
# and northing, and the bearing and distance to the next one. This is
# computed from the geometry, never stored, for the same reason the grid is:
# every writer has the polygon and none of them should disagree about it.
CORNER_LABELS = "ABCDEFGHJKLMNPQRSTUVWXYZ"      # no I or O: they read as 1/0


def corner_label(parcel_index: int, corner_index: int) -> str:
    """A1, A2 ... B1, B2 — parcel by letter, corner by number."""
    letter = CORNER_LABELS[parcel_index % len(CORNER_LABELS)]
    return f"{letter}{corner_index + 1}"


def polygon_corners(geom):
    """The exterior vertices of a polygon, without the repeated closing one.

    Shapely closes a ring by repeating the first coordinate; tabling that
    would list one corner twice and give the last leg a zero length.
    """
    ring = list(getattr(geom, "exterior", geom).coords)
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    return ring


def azimuth_dms(x1, y1, x2, y2) -> str:
    """Grid bearing from (x1,y1) to (x2,y2) as D°M'S", clockwise from north.

    North-based and clockwise because that is what a survey table states
    and what a total station is set to; the maths here is atan2(dE, dN),
    not the atan2(dy, dx) a plotting library wants.
    """
    deg = math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360.0
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s = int(round((m_full - m) * 60))
    if s == 60:                     # rounding up carries, or 12°59'60" prints
        s, m = 0, m + 1
    if m == 60:
        m, d = 0, (d + 1) % 360
    return f"{d:03d}°{m:02d}'{s:02d}\""


def corner_table(geom, parcel_index: int = 0, name: str = ""):
    """[{parcel, corner, easting, northing, bearing, distance_m}] for one
    polygon, each row giving the leg **to the next** corner and closing back
    to the first."""
    corners = polygon_corners(geom)
    rows = []
    for i, (x, y) in enumerate(corners):
        nx, ny = corners[(i + 1) % len(corners)]
        rows.append({
            "parcel": name or f"parcel {parcel_index + 1}",
            "corner": corner_label(parcel_index, i),
            "easting": round(x, 3), "northing": round(y, 3),
            "bearing": azimuth_dms(x, y, nx, ny),
            "distance_m": round(math.hypot(nx - x, ny - y), 3)})
    return rows


def write_corner_csv(path, rows) -> int:
    """The setting-out table beside the drawing."""
    import csv

    fields = ["parcel", "corner", "easting", "northing", "bearing",
              "distance_m"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: r[k] for k in fields} for r in rows)
    return len(rows)


def repaired_polygon(exterior, holes=()):
    """The polygon a writer should draw *and* stage.

    OSM carries self-intersecting rings — a university boundary that crosses
    itself, a building traced in a bow tie — and `buffer(0)` splits those
    into two polygons. The extraction routes used to draw the raw ring while
    staging the repaired one, so the drawing had one outline where its
    re-issue had two, with the label 97 m away in a different lobe. Both go
    through this, so what is drawn is what is stored.
    """
    from shapely.geometry import Polygon

    poly = Polygon(exterior, list(holes) if holes else None)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly


def polygon_parts(geom):
    """The Polygon pieces of a repaired shape, in db2dxf.py's draw order."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", ())
            if g.geom_type == "Polygon" and not g.is_empty]


def line_label_anchor(geom):
    """Midpoint of the longest part, plus an upright rotation in degrees."""
    parts = [g for g in (geom.geoms if geom.geom_type.startswith("Multi")
                         else [geom]) if not g.is_empty]
    if not parts:
        return None, None, 0.0
    line = max(parts, key=lambda g: g.length)
    if line.length <= 0:
        return None, None, 0.0
    mid = line.interpolate(0.5, normalized=True)
    a = line.interpolate(max(0.0, 0.5 - 0.05), normalized=True)
    b = line.interpolate(min(1.0, 0.5 + 0.05), normalized=True)
    ang = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
    if ang > 90:
        ang -= 180
    elif ang < -90:
        ang += 180
    return mid.x, mid.y, ang


# Direction-of-travel arrows on one-way carriageways. The rule lives here,
# beside line_label_anchor(), for the same reason: all three CAD writers
# place them, and a drawing whose re-issue puts the arrows somewhere else is
# a drawing nobody can check against its own revision.
ONEWAY_SPACING_M = 60.0        # along the run, between arrows


ONEWAY_MIN_RUN_M = 12.0        # shorter than this, a run gets none


ONEWAY_ARROW_MIN_M = 3.0


ONEWAY_ARROW_MAX_M = 10.0


# Water flows the way the OSM way is digitised — that is the convention
# waterway=* relies on, and it is the only direction information a canal
# carries. Drawn with the same arrow at a fixed size, because a canal has
# no carriageway width to scale from.
FLOW_ARROW_M = 5.0


def oneway_arrow_size(carriageway_m) -> float:
    """Arrow length for a carriageway width, clamped so a 14 m motorway
    does not get a 14 m arrow and a 3 m alley does not get an invisible one."""
    width = carriageway_m or 0.0
    return max(ONEWAY_ARROW_MIN_M, min(float(width), ONEWAY_ARROW_MAX_M))


def arrow_positions(coords, spacing=ONEWAY_SPACING_M,
                    min_length=ONEWAY_MIN_RUN_M):
    """[(x, y, bearing_degrees), ...] along a polyline, in its own direction.

    Arrows are spaced by distance along the line rather than one per vertex:
    an OSM way carries a vertex every few metres through a curve, so
    per-vertex arrows would pile up on bends and vanish on straights. The
    first sits half a spacing in, so a run never opens with an arrow sitting
    on the junction it starts at. A run shorter than `min_length` gets none
    — there is no room to read one — and anything between that and a full
    spacing gets exactly one, at its midpoint.

    The bearing is the direction of travel *as digitised*; a caller with
    `oneway=-1` adds 180.
    """
    pts = [(float(x), float(y)) for x, y in coords]
    if len(pts) < 2:
        return []
    segs, total = [], 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        if length <= 0:
            continue
        segs.append((total, length, x1, y1, x2, y2))
        total += length
    if not segs or total < min_length:
        return []
    if total < spacing:
        marks = [total / 2]
    else:
        marks = []
        d = spacing / 2
        while d < total:
            marks.append(d)
            d += spacing
    out = []
    for mark in marks:
        for start, length, x1, y1, x2, y2 in segs:
            if mark <= start + length or (start, length) == segs[-1][:2]:
                t = min(max((mark - start) / length, 0.0), 1.0)
                out.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t,
                            math.degrees(math.atan2(y2 - y1, x2 - x1))))
                break
    return out


# ------------------------------------------------------- source attributes
# Extended entity data, the AutoCAD mechanism an OSM importer uses to hang
# the source tags off each entity: select a building, LIST it, and the tags
# are there. Group code 1000 is a string capped at 255 *bytes* — Thai is
# three bytes a character, so the cap is applied after encoding.
XDATA_APPID = "OSM"          # OpenStreetMap tags


GIS_XDATA_APPID = "GIS"      # fields of a file the user supplied


MS_XDATA_APPID = "MICROSOFT"  # Microsoft's ML building footprints


XDATA_MAX_TAGS = 40


ATTR_FIELDS = ["feature_id", "feature_type", "cad_layer", "display_name",
               "key", "value"]


# What ezdxf should set up in a new document. `setup=True` also installs 25
# text styles for fonts nothing here draws with — the writers register
# TH_STYLE and EN_STYLE themselves — and a drafter opening the file sees a
# style table mostly full of entries the drawing never used. The linetypes
# are the part that is load-bearing: CENTER on a centreline, DASHED on the
# crop line, PHANTOM on a right-of-way. Dimension arrowheads are created on
# demand when a DIMENSION renders, so they do not need it either.
DXF_SETUP = ["linetypes"]


def set_drawing_extents(doc) -> bool:
    """Write $EXTMIN/$EXTMAX from what the drawing actually holds.

    A new document carries the ezdxf defaults, +/-1e20, which every writer
    here shipped unchanged. AutoCAD recalculates on its own ZOOM EXTENTS,
    but a viewer that trusts the header opens on empty space and the file
    reports no bounds at all to anything reading it without a CAD engine.

    Returns False when the drawing has no geometry to measure, leaving the
    header alone rather than writing a degenerate box.
    """
    from ezdxf import bbox

    msp = doc.modelspace()
    extents = bbox.extents(msp, fast=True)
    if not extents.has_data:
        return False
    # The header is not where this lives. ezdxf's update_extents() runs on
    # every export and copies $EXTMIN/$EXTMAX back from the modelspace
    # layout, so writing the header alone is overwritten on the way out —
    # which is why setting it looked like it worked and changed nothing in
    # the file.
    msp.dxf.extmin = extents.extmin
    msp.dxf.extmax = extents.extmax
    doc.header["$EXTMIN"] = tuple(extents.extmin)
    doc.header["$EXTMAX"] = tuple(extents.extmax)
    return True


# U+0E01 THAI CHARACTER KO KAI. A font that has this has the Thai block;
# one that does not will render every Thai label as ??? or as boxes.
THAI_PROBE = "\u0e01"


def font_report(styles, thai_styles=("TH_STYLE",)) -> list[dict]:
    """What each text style will actually be drawn with, and whether that
    font can render Thai.

    ezdxf writes UTF-8 whatever happens, so the text is *in* the file; what
    decides whether a reader sees it is the font the STYLE points at. The
    styles name `THSarabunNew.ttf` and `arial.ttf` by filename and nothing
    ever checked they exist. When one does not, ezdxf silently substitutes:
    on the machine this was written on THSarabunNew is absent and the
    substitute is Arial Unicode, which happens to carry Thai — so every
    plot preview looked right by luck. AutoCAD on a machine whose
    substitute is an SHX renders the same drawing's Thai as ???.

    Returns one dict per style: declared, present, resolved, has_thai,
    needs_thai. Never raises — a font check must not be what loses a
    drawing.
    """
    out = []
    try:
        from ezdxf.fonts import fonts as _f
        manager = _f.font_manager
    except Exception:
        return out
    for style, declared in styles.items():
        row = {"style": style, "declared": declared,
               "needs_thai": style in thai_styles,
               "present": False, "resolved": declared, "has_thai": None}
        try:
            row["present"] = bool(manager.has_font(declared))
            face = manager.get_font_face(declared)
            row["resolved"] = getattr(face, "filename", declared)
            ttf = manager.ttf_font_from_font_face(face)
            row["has_thai"] = any(ord(THAI_PROBE) in t.cmap
                                  for t in ttf["cmap"].tables)
        except Exception:
            pass
        out.append(row)
    return out


def font_warnings(report) -> list[str]:
    """The lines worth saying out loud. Empty when every style resolves to
    the font it names."""
    lines = []
    for row in report:
        if row["present"]:
            continue
        where = row["resolved"]
        if row["needs_thai"] and row["has_thai"] is False:
            lines.append(
                f"{row['style']} wants {row['declared']}, which is not "
                f"installed; it falls back to {where}, which has no Thai "
                "glyphs — every Thai label will render as ???")
        elif row["needs_thai"]:
            lines.append(
                f"{row['style']} wants {row['declared']}, which is not "
                f"installed here; this plot used {where}. A reader without "
                f"{row['declared']} may see ??? for Thai")
        else:
            lines.append(f"{row['style']} wants {row['declared']}, which is "
                         f"not installed here; this plot used {where}")
    return lines


def write_font_note(path, report) -> int:
    """List the fonts the drawing needs, beside the drawing.

    A DXF cannot carry a font, only its name, so the requirement has to
    travel some other way or the recipient just sees ??? and has nothing
    telling them why. /zip/<job> packages a run under its on-disk names, so
    this rides along with the deliverable.
    """
    lines = ["Fonts this drawing needs",
             "========================",
             "",
             "A DXF references fonts by name; it cannot embed them. Install",
             "these on the machine that opens the drawing, or the text will",
             "be substituted — Thai has no glyphs in the usual substitutes",
             "and renders as ??? or as boxes.",
             ""]
    for row in report:
        need = " (carries the Thai script — required for Thai labels)" \
            if row["needs_thai"] else ""
        lines.append(f"  {row['style']:<10} {row['declared']}{need}")
    lines += ["",
              "THSarabunNew is one of Thailand's national fonts and is the",
              "face Thai government documents are set in.",
              ""]
    path = Path(path)
    path.write_text("\n".join(lines), encoding="utf-8")
    return len(report)


def check_fonts(styles, out_path=None, thai_styles=("TH_STYLE",)):
    """Warn about substituted fonts and leave the requirement beside the
    drawing. Called by every writer, so no route ships a drawing whose
    Thai will silently vanish without saying so."""
    report = font_report(styles, thai_styles)
    for line in font_warnings(report):
        print(f"WARNING: {line}")
    if report and out_path is not None:
        try:
            write_font_note(out_path, report)
        except OSError:
            pass
    return report


# A carriageway shorter than this has no meaningful edges to offset, and a
# trimmed fragment shorter than this is noise rather than kerb.
MIN_EDGE_M = 0.5


# Annotation heights in this repo are metres of ground, and every one of
# them was picked to read at 1:1000 — a building name at 3.5 m plots at
# 3.5 mm, a spot level at 2.5 m plots at 2.5 mm, which are the sizes a
# drafter expects. Nothing rescaled them for the sheet, so the *default*
# 1000 x 750 m extent on A3 — which fitting_scale() puts at 1:5000 — plotted
# building names at 0.70 mm, road names at 1.00 mm and house numbers at
# 0.44 mm. ISO 3098 sets 2.5 mm as the smallest drafting size and legibility
# gives out below about 1.8 mm, so the default sheet went out unreadable.
#
# The reference is therefore 1:1000, and the factor is the plot scale over
# it: 1.0 at 1:1000, so nothing about the scale a site plan is read at
# changes, and 5.0 at 1:5000, which puts the same 3.5 mm on the paper.
ANNOTATION_REFERENCE_SCALE = 1000.0


# Width of one character as a fraction of the text height. MTEXT is
# proportional, so this is an average over mixed digits and Latin caps —
# enough to tell a 4-character code that will not fit a 6 m shed from one
# that will, which is all this decides.
CHAR_ASPECT = 0.6


def label_fits(text, height_m, box_w_m, box_h_m) -> bool:
    """Whether a label at its *plotted* size still fits what it labels.

    The CAD routes had no such test, and it did not show while annotation
    was fixed at the 1:1000 sizes because at 1:5000 the text was too small
    to read, let alone collide. Sizing it correctly made the real problem
    visible: the default 1000 x 750 m extent holds 400-odd buildings, and a
    3.5 mm code on every one of them is a solid mass of overlapping text.

    So a code is drawn only where its footprint has room for it. This is
    the rule generate_detailed_site_map.py already applies on the other
    stack — and the same priority: a B### code is recoverable from
    building_inventory.csv against the same feature id, so dropping one
    loses nothing a reader cannot get back, while a name is the
    identification the sheet exists to carry.
    """
    if not text or not height_m or box_w_m is None or box_h_m is None:
        return True
    return (len(str(text)) * height_m * CHAR_ASPECT <= float(box_w_m)
            and height_m <= float(box_h_m))


def annotation_scale(plot_scale) -> float:
    """Multiplier for model-space text heights and their offsets.

    Both CAD routes take the same --sheet/--scale, derive this from them and
    apply it to the same numbers, so a drawing and its re-issue put text of
    the same size in the same place. A drawing with no sheet has no plot
    scale to work from and keeps the 1:1000 sizes.
    """
    try:
        scale = float(plot_scale)
    except (TypeError, ValueError):
        return 1.0
    return scale / ANNOTATION_REFERENCE_SCALE if scale > 0 else 1.0


def road_edges(coords, width_m):
    """Both edges of one carriageway, offset from its centreline.

    Falls back to the centreline when the geometry is too kinked to offset
    cleanly — a self-crossing run makes offset_curve return something
    unusable, and one line is better than none.
    """
    from shapely.geometry import LineString

    line = LineString(coords)
    if line.length < MIN_EDGE_M or width_m <= 0:
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


def carriageway_edges(roads):
    """Every road's edges of pavement, trimmed where the roads meet.

    `roads` is [(key, coords, width_m, at_grade), ...]; the return is
    {key: [[(x, y), ...], ...]} in the same order the offsets came out.

    Each road is offset independently, which is why the kerb lines used to
    run straight through every junction: a four-road rural site drew 6
    edge/edge crossings and 6 more against the centrelines, and a drafter
    had to TRIM each one by hand before the drawing read as a road network.
    So each edge has the *other* carriageways subtracted from it. What that
    produces is the drafting convention rather than an approximation of it:
    a side road's kerb stops at the through road's kerb, and the through
    road's kerb breaks at the mouth of the junction, which is the opening
    traffic turns through.

    Three details carry it.

    **Flat caps.** `cap_style=2` is what makes a road split into several
    OSM ways safe: two collinear ways meeting end to end lose nothing at
    all, where a round cap eats half a carriageway width of kerb at every
    way boundary — a gap in a straight road, at a joint that exists for
    OSM's convenience and means nothing on the ground. Measured: 100.0 m
    kept of 100.0 with flat caps, 97.0 with round.

    **Only at grade.** A bridge crosses whatever is beneath it and a tunnel
    runs under it, so neither trims nor is trimmed: cutting the road under
    a flyover would draw a junction where there is none. `at_grade` is the
    caller's call, from the layer the way is drawn on.

    **A road never trims itself**, or the two ways of a divided carriageway
    would each erase the other's inner kerb.
    """
    from shapely import STRtree
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    prepared = []
    for key, coords, width_m, at_grade in roads:
        line = LineString(coords)
        if line.length < MIN_EDGE_M or not width_m or width_m <= 0:
            continue
        prepared.append((key, line, float(width_m), bool(at_grade)))

    # The carriageway each road occupies. Mitred joins, so a bend does not
    # bulge a round elbow into the road it is about to meet.
    polys = [line.buffer(w / 2, cap_style=2, join_style=2)
             for _k, line, w, _g in prepared]
    at_grade_ix = [i for i, r in enumerate(prepared) if r[3]]
    tree = STRtree([polys[i] for i in at_grade_ix]) if at_grade_ix else None

    out = {}
    for i, (key, line, width_m, at_grade) in enumerate(prepared):
        edges = road_edges(list(line.coords), width_m)
        if at_grade and tree is not None:
            kept = []
            for edge in edges:
                el = LineString(edge)
                # Only the carriageways this edge could actually touch: the
                # union of every road against every road is quadratic, and
                # a dense extent carries hundreds of them.
                near = [at_grade_ix[j] for j in tree.query(el)
                        if at_grade_ix[j] != i]
                if not near:
                    kept.append(edge)
                    continue
                cut = el.difference(unary_union([polys[j] for j in near]))
                for part in (cut.geoms if hasattr(cut, "geoms") else [cut]):
                    if (not part.is_empty and part.geom_type == "LineString"
                            and part.length >= MIN_EDGE_M):
                        kept.append(list(part.coords))
            edges = kept
        out[key] = edges
    return out


def ring_points(coords):
    """A closed ring's vertices for `add_lwpolyline(..., close=True)`.

    Shapely repeats the first vertex to close a ring; the DXF closed flag
    closes it again. Both together give every polygon a zero-length
    closing segment — 49 of 49 footprints in a rural extract — which
    AutoCAD's OVERKILL strips, an offset or a fillet trips over, and every
    downstream tool has to special-case. The flag is the DXF way to say
    closed, so the repeated vertex is what goes.

    Only the duplicate is dropped: a ring is not otherwise cleaned here,
    because a vertex a source actually recorded is that source's, not
    ours to remove.
    """
    pts = [(float(x), float(y)) for x, y, *_ in
           ((c[0], c[1]) if len(c) < 3 else c for c in coords)]
    while len(pts) > 2 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def clip_bytes(text, limit: int = 255) -> str:
    raw = str(text).encode("utf-8")
    if len(raw) <= limit:
        return str(text)
    return raw[:limit].decode("utf-8", "ignore")


def xdata_tags(feature_id: str, tags: dict, max_tags: int = XDATA_MAX_TAGS):
    """[(1000, 'key=value'), ...] for one feature, id first.

    Sorted, so two runs of the same source produce byte-identical drawings.
    A long tag list is truncated with a marker rather than silently: AutoCAD
    caps XDATA at 16 KB per entity, and a machine-generated `source:...`
    history can approach it. The attribute CSV carries the full set.
    """
    out = [(1000, clip_bytes(f"@id={feature_id}"))]
    items = sorted(tags.items())
    for key, value in items[:max_tags]:
        out.append((1000, clip_bytes(f"{key}={value}")))
    if len(items) > max_tags:
        out.append((1000, f"@truncated={len(items) - max_tags} more tags"))
    return out


def attribute_rows(drawn, tags_by_id):
    """One row per (drawn feature, tag), sorted — the attribute table.

    `drawn` describes what reached the drawing, so a feature dropped by a
    type filter or a crop is absent from the table too: it documents the
    DXF, not the source it came from.
    """
    rows = []
    for rec in drawn:
        for key, value in sorted(tags_by_id.get(rec["feature_id"], {}).items()):
            rows.append({**rec, "key": key, "value": value})
    return sorted(rows, key=lambda r: (r["feature_id"], r["key"]))


def spot_grid(west, south, east, north, columns=5, rows_n=5):
    """Sample points for spot heights, inset from the extent.

    A grid rather than a scatter: a reviewer reads levels across a site by
    comparing neighbours, and the inset keeps the numbers off the crop line
    where the frame or the title block would sit on them.
    """
    if columns < 1 or rows_n < 1:
        return []
    span_x, span_y = east - west, north - south
    step_x, step_y = span_x / (columns + 1), span_y / (rows_n + 1)
    return [(west + step_x * (i + 1), south + step_y * (j + 1))
            for j in range(rows_n) for i in range(columns)]


def write_attribute_csv(path, rows) -> int:
    """The attribute table beside the drawing, for review outside CAD."""
    import csv

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ATTR_FIELDS)
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in ATTR_FIELDS} for r in rows)
    return len(rows)


# How far the landmark name sits from its symbol, in metres. Must match the
# `px + 3` in topo2cad.py, or the two CAD routes place the label differently.
POI_LABEL_DX = 3.0


ROAD_FIELDS = ["feature_id", "road_ref", "highway_type", "road_name",
               "name_th", "name_en", "official_name", "cad_layer",
               "carriageway_m", "oneway", "length_m", "source"]


def write_road_csv(path, rows) -> int:
    """The road inventory, beside the drawing."""
    import csv as _csv

    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=ROAD_FIELDS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            out = dict(row)
            length = out.get("length_m")
            if isinstance(length, (int, float)):
                out["length_m"] = f"{length:.1f}"
            w.writerow(out)
    return len(rows)


# The Thai word a reviewer reads, per OSM value. A ผังบริเวณ lists
# สถานที่สำคัญใกล้เคียง by kind, and "place_of_worship" is not that word.
POI_THAI = {
    "place_of_worship": "วัด/ศาสนสถาน", "monastery": "วัด",
    "school": "โรงเรียน", "university": "มหาวิทยาลัย",
    "college": "วิทยาลัย", "kindergarten": "โรงเรียนอนุบาล",
    "hospital": "โรงพยาบาล", "clinic": "คลินิก",
    "police": "สถานีตำรวจ", "fire_station": "สถานีดับเพลิง",
    "townhall": "ที่ว่าการอำเภอ/เทศบาล", "courthouse": "ศาล",
    "embassy": "สถานทูต", "public_building": "อาคารราชการ",
    "community_centre": "ศูนย์ชุมชน", "post_office": "ที่ทำการไปรษณีย์",
    "prison": "เรือนจำ", "marketplace": "ตลาด",
    "bus_station": "สถานีขนส่ง", "library": "ห้องสมุด", "fuel": "ปั๊มน้ำมัน",
    "museum": "พิพิธภัณฑ์", "attraction": "สถานที่ท่องเที่ยว",
    "viewpoint": "จุดชมวิว", "zoo": "สวนสัตว์", "aquarium": "สถานแสดงพันธุ์สัตว์น้ำ",
    "theme_park": "สวนสนุก", "monument": "อนุสาวรีย์", "memorial": "อนุสรณ์สถาน",
    "ruins": "โบราณสถาน", "city_gate": "ประตูเมือง", "temple": "วัด",
}


# Overture names its categories differently and adds taxonomy leaves between
# releases, so these are matched as substrings — the same reason
# overture.keep_place() does. Longest first, or "school" would claim
# "language_school" before the more specific word got a look.
POI_THAI_PARTS = (
    ("buddhist_temple", "วัด"), ("language_school", "โรงเรียนสอนภาษา"),
    ("shopping_center", "ศูนย์การค้า"), ("department_store", "ห้างสรรพสินค้า"),
    ("art_museum", "พิพิธภัณฑ์ศิลปะ"), ("train_station", "สถานีรถไฟ"),
    ("bus_station", "สถานีขนส่ง"), ("gas_station", "ปั๊มน้ำมัน"),
    ("fire_station", "สถานีดับเพลิง"), ("post_office", "ที่ทำการไปรษณีย์"),
    ("city_hall", "ศาลากลาง/ที่ว่าการ"), ("courthouse", "ศาล"),
    ("government", "หน่วยงานราชการ"), ("municipal", "หน่วยงานเทศบาล"),
    ("university", "มหาวิทยาลัย"), ("kindergarten", "โรงเรียนอนุบาล"),
    ("hospital", "โรงพยาบาล"), ("clinic", "คลินิก"), ("museum", "พิพิธภัณฑ์"),
    ("library", "ห้องสมุด"), ("stadium", "สนามกีฬา"), ("monument", "อนุสาวรีย์"),
    ("landmark", "สถานที่สำคัญ"), ("subway", "สถานีรถไฟฟ้าใต้ดิน"),
    ("airport", "สนามบิน"), ("ferry", "ท่าเรือ"), ("transit", "สถานีขนส่ง"),
    ("worship", "ศาสนสถาน"), ("church", "โบสถ์"), ("mosque", "มัสยิด"),
    ("shrine", "ศาลเจ้า"), ("temple", "วัด"), ("school", "โรงเรียน"),
    ("college", "วิทยาลัย"), ("education", "สถานศึกษา"), ("police", "สถานีตำรวจ"),
    ("embassy", "สถานทูต"), ("park", "สวนสาธารณะ"), ("petrol", "ปั๊มน้ำมัน"),
)


def poi_kind_thai(poi_type: str) -> str:
    """The Thai word a reviewer reads for one landmark, or ''.

    A ผังบริเวณ lists สถานที่สำคัญใกล้เคียง by kind, and
    "place_of_worship" is not that word. Empty when nothing matches rather
    than guessed — the row still carries its raw poi_type.
    """
    key = (poi_type or "").lower()
    if not key:
        return ""
    if key in POI_THAI:
        return POI_THAI[key]
    for part, word in POI_THAI_PARTS:
        if part in key:
            return word
    return ""


# Which staged points are landmarks. staging_pois also carries the map
# furniture — trees, pylons, gates — which stages with an empty
# display_name so cad_labels drops it. A list of สถานที่สำคัญใกล้เคียง that
# opens with 90 trees is not a list anybody reads.
LANDMARK_LAYERS = ("C-ANNO-SYMB", "C-ANNO-OVTR")


POI_FIELDS = ["feature_id", "poi_key", "poi_type", "kind_th", "display_name",
              "name_th", "name_en", "distance_m", "bearing", "latitude",
              "longitude", "cad_layer", "source"]


def bearing_text(d_east: float, d_north: float) -> str:
    """North-based clockwise bearing, as a surveyor writes it.

    atan2(dE, dN), not the atan2(dy, dx) a plotting library wants — that
    swaps east and north on every reading. Same convention as corner_table().
    """
    deg = math.degrees(math.atan2(d_east, d_north)) % 360.0
    whole = int(deg)
    minutes = int(round((deg - whole) * 60))
    if minutes == 60:
        whole, minutes = (whole + 1) % 360, 0
    return f"{whole:03d}°{minutes:02d}′"


def write_poi_csv(path, rows) -> int:
    """The landmark list, beside the drawing."""
    import csv as _csv

    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=POI_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)


# What each staged source is called on a printed sheet. The title block
# credits the data, and a sheet carrying a survey boundary, Microsoft
# footprints and Overture places while crediting only OpenStreetMap is
# wrong in both directions: it credits a source that did not supply the
# line, and it fails to credit the ones that did.
SOURCE_CREDITS = {
    "openstreetmap": "OpenStreetMap contributors (ODbL)",
    "microsoft_ml": "Microsoft ML footprints (ODbL)",
    "overture": "Overture Maps (ODbL/CDLA)",
    # The resolution belongs in the credit. Contours off a 30 m global DEM
    # and contours off a survey plot the same on paper, and a reviewer is
    # entitled to know which one they are reading before treating the shape
    # as ground truth.
    "copernicus_dem": "Copernicus DEM 30 m (ESA)",
}


# Characters that fit one line of the title block's smallest text at the
# narrowest sheet. An honest credit that overruns the frame is not on the
# sheet at all, so the lines are wrapped rather than trusted to fit.
CREDIT_WIDTH = 46


def credit_lines(sources, max_files: int = 2,
                 width: int = CREDIT_WIDTH) -> list[str]:
    """Attribution lines for a title block, from staged source names.

    Sources are recorded per file (`user_gis:boundary.geojson`), so the
    prefix decides the credit and the file names are listed after it — up
    to `max_files`, because a title block is 55 mm wide and an honest line
    that overruns the frame is not on the sheet at all.
    """
    order = list(SOURCE_CREDITS)
    known, files, osm_files = [], [], []
    for source in sorted(set(s for s in sources if s),
                         key=lambda s: (order.index(s.partition(":")[0])
                                        if s.partition(":")[0] in order
                                        else len(order), s)):
        head, _, rest = source.partition(":")
        credit = SOURCE_CREDITS.get(head)
        if head == "user_gis":
            files.append(rest or "supplied file")
        elif credit:
            if rest:
                osm_files.append(rest)
            if credit not in known:
                known.append(credit)
        else:                       # a source added later, named honestly
            known.append(head)
    import textwrap

    lines = []
    if known:
        lines += textwrap.wrap("Data © " + "; ".join(known), width,
                               subsequent_indent="   ")
    if osm_files:
        shown = osm_files[:max_files]
        more = len(osm_files) - len(shown)
        lines += textwrap.wrap("OSM extract: " + ", ".join(shown)
                               + (f" +{more} more" if more else ""), width,
                               subsequent_indent="   ")
    if files:
        shown = files[:max_files]
        more = len(files) - len(shown)
        lines += textwrap.wrap("Supplied survey data: " + ", ".join(shown)
                               + (f" +{more} more" if more else ""), width,
                               subsequent_indent="   ")
    return lines
