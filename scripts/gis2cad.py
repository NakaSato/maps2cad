#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "geopandas>=1.0",
#   "shapely>=2.0",
#   "pyproj>=3.6",
#   "ezdxf",
#   "rasterio",
# ]
# ///
"""Convert your own GIS data into the CAD drawing.

For sites where OpenStreetMap and the ML footprint layers have nothing —
new plots, plantations, land the survey team measured themselves — feed in
GeoJSON, Shapefile, GeoPackage, KML or GML and get the same NCS-layered DXF
in true UTM metres:

    uv run scripts/gis2cad.py --input parcels.geojson --out output/site.dxf
    uv run scripts/gis2cad.py --input survey.shp --name-field PLOT_NAME \\
        --layer C-PROP-LINE --out output/parcels.dxf
    uv run scripts/gis2cad.py --input blocks.geojson --input access.geojson \\
        --db output/staging.sqlite --project "phase-2" --out output/phase2.dxf

Geometry type decides the default layer: polygons become building outlines,
lines become road centrelines with both carriageway edges, points become
symbols. Override per input with --layer. The UTM zone is derived from the
data itself, so distances come out in metres without you choosing a CRS.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The staging layer owns the XDATA and attribute-table rules, so a survey
# import writes them exactly the way the OSM routes do.
import stage_db                                               # noqa: E402

# Fields of a file the user supplied are not OpenStreetMap tags, so they go
# under their own application id — one drawing can carry both when a survey
# is merged into an extraction.
GIS_APPID = stage_db.GIS_XDATA_APPID

LAYERS = {
    "polygon": "C-BLDG-OUTL",
    "line": "C-ROAD-CNTR",
    "line_edge": "C-ROAD-EDGE",
    "point": "C-ANNO-SYMB",
    "anno": "C-ANNO-TEXT",
    "anno_th": "C-ANNO-TEXT-TH",
    "anno_en": "C-ANNO-TEXT-EN",
}
LAYER_STYLE = {
    "C-BLDG-OUTL": (4, 50), "C-ROAD-CNTR": (8, 9), "C-ROAD-EDGE": (30, 35),
    "C-TOPO-MAJR": (8, 25), "C-TOPO-MINR": (8, 9),
    "C-ANNO-SYMB": (6, 18), "C-ANNO-TEXT": (2, 25), "C-PROP-LINE": (1, 70),
    "C-PROP-SETB": (2, 25), "C-TOPO-CONT": (8, 13), "C-HYDR-WATR": (5, 18),
    "C-LAND-VEGT": (3, 13), "C-ANNO-GPSP": (1, 35),
    "C-ANNO-TEXT-TH": (2, 25), "C-ANNO-TEXT-EN": (7, 25),
}

# Must match TEXT_STYLES in topo2cad.py / db2dxf.py.
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
}

# name:th first: the deliverable is a Thai submission, and a supplied file
# that carries both wants the Thai one on the drawing.
NAME_FIELDS = ["name:th", "name", "Name", "NAME", "title", "label", "LABEL",
               "description", "PLOT_NAME", "owner", "ref"]

# Mirrors is_thai() in topo2cad.py — U+0E00–U+0E7F is the Thai block.
THAI_RE = re.compile(r"[฀-๿]")


def is_thai(text) -> bool:
    """True if the string contains any Thai character."""
    return bool(text) and bool(THAI_RE.search(str(text)))


def anno_layer_for(text) -> str:
    """Language layer for a label from a source with no language tags: the
    script of the text itself is the only signal available."""
    return LAYERS["anno_th"] if is_thai(text) else LAYERS["anno_en"]


def utm_epsg_for(lat: float, lon: float) -> int:
    zone = min(max(int((lon + 180) // 6) + 1, 1), 60)
    return (32600 if lat >= 0 else 32700) + zone


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
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == wanted:
        yield geom
    elif hasattr(geom, "geoms"):
        for g in geom.geoms:
            yield from parts(g, wanted)


# Columns this adds for its own bookkeeping; they are not the user's data
# and have no business in the attribute table.
INTERNAL_COLUMNS = {"geometry", "_layer", "_namefield"}


def row_attributes(row, columns=None) -> dict:
    """The feature's own fields, as {name: value} strings.

    A shapefile's DBF columns are exactly the attributes a CAD user wants
    hanging off the entity — the same thing OSM tags are on the other
    routes. Empty cells and pandas' NaN are dropped rather than written as
    the word "nan", which is what str() gives you.
    """
    out = {}
    for key in (columns if columns is not None else row.index):
        if key in INTERNAL_COLUMNS:
            continue
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in ("nan", "none", "nat"):
            out[str(key)] = text
    return out


def pick_name_field(columns, explicit):
    if explicit:
        return explicit
    for candidate in NAME_FIELDS:
        if candidate in columns:
            return candidate
    return None


def road_edges(coords, width_m):
    from shapely.geometry import LineString

    line = LineString(coords)
    if line.length < 0.5 or width_m <= 0:
        return []
    out = []
    for side in (width_m / 2, -width_m / 2):
        try:
            off = line.offset_curve(side)
        except Exception:
            return []
        for p in parts(off, "LineString"):
            if len(p.coords) >= 2:
                out.append(list(p.coords))
    return out


def stage(a, frames, epsg, centre, attrs=()):
    """Stage the imported features alongside anything already staged for
    this project, so one drawing can carry OSM data and your own survey."""
    from pyproj import Transformer

    import stage_db

    name = a.project or Path(a.input[0]).stem
    to_wgs = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    conn = stage_db.connect(a.db)
    # Extent is only metadata here; a merge keeps the OSM run's values
    pid, existed = stage_db.get_or_create_project(
        conn, name, centre.y, centre.x, 0.0, 0.0, epsg)

    b_rows, r_rows = [], []
    for path, gdf in frames:
        gdf = gdf.to_crs(f"EPSG:{epsg}")
        field = gdf["_namefield"].iloc[0]
        for i, (_, row) in enumerate(gdf.iterrows()):
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            label = ""
            if field:
                value = row.get(field)
                if value is not None and str(value).strip().lower() not in (
                        "", "nan", "none"):
                    label = str(value).strip()
            fid = f"gis/{path.stem}/{i:05d}"
            polys = list(parts(geom, "Polygon"))
            lines = list(parts(geom, "LineString"))
            if polys:
                from shapely.geometry import MultiPolygon
                b_rows.append({
                    "feature_id": fid, "source": "user_gis",
                    "osm_name": label, "code": "",
                    "display_name": label or fid.split("/")[-1],
                    "building_type": None,
                    "geom": polys[0] if len(polys) == 1
                    else MultiPolygon(polys)})
            elif lines:
                from shapely.geometry import MultiLineString
                r_rows.append({
                    "feature_id": fid, "highway_type": "user_gis",
                    "road_name": label or None, "road_ref": None,
                    "carriageway_m": a.width,
                    "geom": lines[0] if len(lines) == 1
                    else MultiLineString(lines)})

    n_b = stage_db.stage_buildings(conn, pid, b_rows, to_wgs=to_wgs) \
        if b_rows else 0
    n_r = stage_db.stage_roads(conn, pid, r_rows) if r_rows else 0
    # Under GIS, not OSM: a re-issue must not relabel a shapefile's columns
    # as OpenStreetMap tags in the CAD attribute browser.
    stage_db.stage_tags(conn, pid, list(attrs), appid=GIS_APPID)
    total_b = conn.execute("SELECT COUNT(*) FROM staging_buildings WHERE"
                           " project_id = ?", (pid,)).fetchone()[0]
    total_r = conn.execute("SELECT COUNT(*) FROM staging_roads WHERE"
                           " project_id = ?", (pid,)).fetchone()[0]
    conn.close()
    verb = "merged into" if existed else "staged to"
    print(f"{verb} '{name}' (id {pid}) in {a.db}: +{n_b} polygons, "
          f"+{n_r} lines — project now holds {total_b} buildings, "
          f"{total_r} roads")


def main(argv=None) -> int:
    import ezdxf
    import geopandas as gpd
    from ezdxf.enums import MTextEntityAlignment

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", action="append", required=True, metavar="FILE",
                    help="GIS file (GeoJSON/SHP/GPKG/KML/GML); repeatable")
    ap.add_argument("--out", help="output DXF (default: <first input>.dxf)")
    ap.add_argument("--layer", action="append", default=[],
                    help="CAD layer for the matching --input (repeatable)")
    ap.add_argument("--name-field", action="append", default=[],
                    help="attribute holding the label, per --input")
    ap.add_argument("--width", type=float, default=6.0, metavar="M",
                    help="carriageway width for line inputs, metres")
    ap.add_argument("--epsg", type=int,
                    help="force a projected CRS instead of deriving the "
                         "UTM zone from the data")
    ap.add_argument("--underlay", metavar="RASTER",
                    help="Attach a georeferenced image (GeoTIFF) as a tracing "
                         "underlay at true scale, on its own layer beneath "
                         "the linework. Must already be in the drawing's UTM "
                         "CRS. The DXF stores a path, not the pixels, so keep "
                         "the image beside the .dxf file.")
    ap.add_argument("--no-labels", action="store_true")
    ap.add_argument("--no-attributes", action="store_true",
                    help="Do not attach the file's own fields to each entity "
                         "as XDATA, and do not write attributes.csv")
    ap.add_argument("--mono", action="store_true",
                    help="Monochrome: every layer on ACI 7 (same as "
                         "topo2cad.py --mono)")
    ap.add_argument("--db", metavar="PATH",
                    help="Also stage these features into the SQLite layer, "
                         "merging into --project if it already exists so your "
                         "survey data and the OSM extraction share a drawing")
    ap.add_argument("--project", metavar="NAME",
                    help="Project name for --db (default: the input filename)")
    a = ap.parse_args(argv)

    frames = []
    for i, path in enumerate(a.input):
        p = Path(path)
        if not p.is_file():
            print(f"ERROR: no such file: {p}", file=sys.stderr)
            return 1
        try:
            gdf = gpd.read_file(p)
        except Exception as e:
            print(f"ERROR: cannot read {p} ({type(e).__name__}): {e}",
                  file=sys.stderr)
            return 1
        if gdf.empty:
            print(f"WARNING: {p} holds no features — skipping")
            continue
        if gdf.crs is None:
            print(f"WARNING: {p} declares no CRS; assuming EPSG:4326 "
                  "(lat/lon). Pass data with a CRS to be certain.")
            gdf = gdf.set_crs("EPSG:4326")
        gdf["_layer"] = a.layer[i] if i < len(a.layer) else None
        gdf["_namefield"] = pick_name_field(
            gdf.columns, a.name_field[i] if i < len(a.name_field) else None)
        frames.append((p, gdf))

    if not frames:
        print("ERROR: nothing to draw", file=sys.stderr)
        return 1

    # Derive the projected CRS from the data itself
    wgs = frames[0][1].to_crs("EPSG:4326")
    c = wgs.geometry.union_all().centroid
    epsg = a.epsg or utm_epsg_for(c.y, c.x)
    zone = epsg - (32600 if c.y >= 0 else 32700)
    print(f"Projected CRS: EPSG:{epsg} "
          f"(UTM {zone}{'N' if c.y >= 0 else 'S'}), units = metres")

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
    if not a.no_attributes:
        doc.appids.add(GIS_APPID)

    if a.underlay:
        import underlay as ul
        try:
            # `out` is resolved further down; the underlay only needs
            # the directory, to store a path relative to the drawing.
            dxf_path = a.out or Path(a.input[0]).with_suffix(".dxf")
            info = ul.attach(doc, msp, a.underlay, epsg,
                             dxf_path=dxf_path)
            print(ul.describe(info))
        except ul.UnderlayError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1

    def mtext(text, x, y, height=3.5, rotation=0.0):
        layer = anno_layer_for(text)
        m = msp.add_mtext(str(text), dxfattribs={
            "layer": layer, "char_height": height,
            "style": ANNO_TEXT_STYLE[layer]})
        m.set_location((x, y), rotation=rotation,
                       attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
        m.set_bg_color("canvas", scale=BG_MASK_SCALE)

    counts = {"polygon": 0, "line": 0, "point": 0, "label": 0, "edge": 0}
    drawn, feature_tags = [], {}
    for path, gdf in frames:
        gdf = gdf.to_crs(f"EPSG:{epsg}")
        forced = gdf["_layer"].iloc[0]
        field = gdf["_namefield"].iloc[0]
        columns = [c for c in gdf.columns if c not in INTERNAL_COLUMNS]
        for i, (_, row) in enumerate(gdf.iterrows()):
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            label = None
            if field and not a.no_labels:
                value = row.get(field)
                if value is not None and str(value).strip().lower() not in (
                        "", "nan", "none"):
                    label = str(value).strip()
            # The same id stage() computes, so the drawing, the attribute
            # table and the staging layer describe one feature.
            fid = f"gis/{path.stem}/{i:05d}"
            tags = {} if a.no_attributes else row_attributes(row, columns)

            def record(kind, layer):
                if tags:
                    feature_tags[fid] = tags
                    drawn.append({"feature_id": fid, "feature_type": kind,
                                  "cad_layer": layer,
                                  "display_name": label or ""})

            def attach(entity):
                if tags and entity is not None:
                    entity.set_xdata(GIS_APPID,
                                     stage_db.xdata_tags(fid, tags))

            for poly in parts(geom, "Polygon"):
                layer = forced or LAYERS["polygon"]
                attach(msp.add_lwpolyline(list(poly.exterior.coords),
                                          close=True,
                                          dxfattribs={"layer": layer}))
                for ring in poly.interiors:
                    msp.add_lwpolyline(list(ring.coords), close=True,
                                       dxfattribs={"layer": layer})
                counts["polygon"] += 1
                record("polygon", layer)
                if label:
                    pt = poly.representative_point()
                    mtext(label, pt.x, pt.y)
                    counts["label"] += 1

            for line in parts(geom, "LineString"):
                coords = list(line.coords)
                layer = forced or LAYERS["line"]
                attach(msp.add_lwpolyline(coords, dxfattribs={"layer": layer}))
                counts["line"] += 1
                record("line", layer)
                if not forced:
                    for edge in road_edges(coords, a.width):
                        msp.add_lwpolyline(edge, dxfattribs={
                            "layer": LAYERS["line_edge"]})
                        counts["edge"] += 1
                if label and line.length > 1:
                    mid = line.interpolate(0.5, normalized=True)
                    b = line.interpolate(min(1.0, 0.55), normalized=True)
                    ang = math.degrees(math.atan2(b.y - mid.y, b.x - mid.x))
                    ang = ang - 180 if ang > 90 else (
                        ang + 180 if ang < -90 else ang)
                    mtext(label, mid.x, mid.y, 5.0, rotation=ang)
                    counts["label"] += 1

            for pt in parts(geom, "Point"):
                layer = forced or LAYERS["point"]
                attach(msp.add_circle((pt.x, pt.y), radius=2,
                                      dxfattribs={"layer": layer}))
                counts["point"] += 1
                record("point", layer)
                if label:
                    mtext(label, pt.x + 4, pt.y, 4.0)
                    counts["label"] += 1
        print(f"  {path.name}: {len(gdf)} feature(s), CRS "
              f"{gdf.crs.to_string() if gdf.crs else '?'}"
              + (f", labels from '{field}'" if field else ", no label field"))

    attrs = stage_db.attribute_rows(drawn, feature_tags)
    if a.db:
        stage(a, frames, epsg, c, attrs)

    out = Path(a.out) if a.out else Path(a.input[0]).with_suffix(".dxf")
    out.parent.mkdir(parents=True, exist_ok=True)
    if a.mono:
        apply_mono(doc)
    doc.saveas(out)
    if attrs:
        attr_path = out.with_name("attributes.csv")
        stage_db.write_attribute_csv(attr_path, attrs)
        print(f"Attributes: {len(attrs)} fields on {len(drawn)} feature(s) "
              f"-> {attr_path}")
    print(f"Drawn: {counts['polygon']} polygons, {counts['line']} lines "
          f"(+{counts['edge']} edges), {counts['point']} points, "
          f"{counts['label']} labels")
    print(f"Saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
