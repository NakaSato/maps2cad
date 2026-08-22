#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "ezdxf",
#   "shapely>=2.0",
#   "pyproj>=3.6",
#   "requests",
#   "rasterio",
#   "numpy",
#   "pillow",
# ]
# ///
"""Convert an OpenStreetMap **file** into the CAD drawing — no network at all.

The other OSM route (`topo2cad.py`) asks Overpass for a box around a GPS
point. This one takes an extract you already have:

    1. open www.openstreetmap.org, pan to the area, press Export
    2. adjust the box if it refuses (the export API caps at ~50,000 nodes)
    3. save the .osm file
    4. draw it:

    uv run scripts/osm2cad.py --input map.osm --outdir output/runs
    uv run scripts/osm2cad.py --input map.osm --epsg 32647 --out site.dxf
    uv run scripts/osm2cad.py --input area.osm.bz2 \\
        --bbox 15.8300,104.3900,15.8380,104.3990 --types building,road \\
        --layer-by highway --db output/staging.sqlite --project "wat-site"

Why it exists: Overpass is rate-limited, sometimes down, and blocked on some
office networks, while an .osm export is a file a surveyor can carry to a
site with no connection at all. It is also the only way to draw an extract
someone else prepared, or a JOSM edit that is not uploaded yet.

The tag rules, the NCS layers and the label conventions are `topo2cad.py`'s —
imported, not copied — so a drawing made from a file and one made from a live
fetch of the same area categorise features identically. What this does *not*
do is supplement with Microsoft ML footprints: the file is the source of
truth here, and inventing footprints the user did not export would be a
surprise, not a service. Contours need a DEM, so there are none; use
`topo2cad.py` when the deliverable needs terrain.

Reads .osm / .xml, .gz, .bz2 and .zip. A .osm.pbf is refused with the one
command that converts it, because decoding protobuf would cost a dependency
every other route here does without.
"""

from __future__ import annotations

import argparse
import bz2
import contextlib
import csv
import gzip
import math
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Tag rules (classify_elements, poi_kind, names_by_lang), NCS layer names,
# text styles and the geometry helpers all come from topo2cad.py — the same
# module mapposter.py imports. Editing a rule there changes this too, which
# is the point: two front doors, one set of drawing conventions.
import topo2cad as t2c                                        # noqa: E402
# The staging layer owns the shared placement rules (label anchors, arrow
# spacing, polygon repair) and the XDATA/attribute-table rules, so every CAD
# writer applies the same ones.
import stage_db as _anchor_rules                              # noqa: E402


class OsmFileError(Exception):
    """Input that cannot be read as OSM XML, with the fix in the message."""


# A .osm.pbf begins with a 4-byte big-endian header length followed by a
# BlobHeader whose type string is 'OSMHeader'; that lands inside the first
# 64 bytes of every file osmium or Geofabrik produces.
def looks_like_pbf(head: bytes) -> bool:
    """True for the protobuf encoding, which this deliberately cannot read."""
    return b"OSMHeader" in head[:64]


PBF_ADVICE = ("Convert it first — `osmium cat -o map.osm map.osm.pbf` — or "
              "re-export the area from www.openstreetmap.org, which hands "
              "you XML.")

XML_SUFFIXES = (".osm", ".xml")


def check_head(head: bytes, name: str) -> None:
    """Fail on a file that is not OSM XML, before the parser says something
    unhelpful about byte 0."""
    if looks_like_pbf(head):
        raise OsmFileError(f"“{name}” is an .osm.pbf (protobuf). {PBF_ADVICE}")
    if not head.lstrip()[:1] == b"<":
        raise OsmFileError(
            f"“{name}” does not start like OSM XML. Export the area from "
            "www.openstreetmap.org (the Export button) and upload the .osm "
            "file it downloads.")


@contextlib.contextmanager
def osm_stream(path: Path):
    """Yield (head_bytes, rest_of_stream) for an OSM XML document.

    Accepts the file plain, gzipped, bzipped, or as the first .osm/.xml
    member of a .zip — the shapes an export or an emailed extract arrives in.
    The head is read off separately so a wrong file type is reported by
    filename rather than as an XML syntax error.
    """
    suffix = path.suffix.lower()
    if suffix == ".pbf" or path.name.lower().endswith(".osm.pbf"):
        raise OsmFileError(f"“{path.name}” is an .osm.pbf (protobuf). "
                           f"{PBF_ADVICE}")
    if suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            members = sorted(m for m in z.namelist()
                             if m.lower().endswith(XML_SUFFIXES)
                             and not m.endswith("/"))
            if not members:
                raise OsmFileError(
                    f"“{path.name}” holds no .osm or .xml file.")
            with z.open(members[0]) as f:
                head = f.read(512)
                check_head(head, members[0])
                yield head, f
        return
    opener = {".gz": gzip.open, ".bz2": bz2.open}.get(suffix, open)
    with opener(path, "rb") as f:
        head = f.read(512)
        check_head(head, path.name)
        yield head, f


def _num(el, key):
    value = el.get(key)
    return None if value is None else float(value)


def parse_osm(head: bytes, stream, chunk: int = 1 << 20):
    """Resolve an OSM XML document into Overpass-shaped elements.

    Returns (elements, stats). Each element is the dict shape
    `classify_elements()` expects — the shape Overpass returns for
    `out tags geom` — so the file route and the network route share every
    tag rule below this point:

        {"type": "way", "id": 1, "tags": {...},
         "geometry": [{"lon": .., "lat": ..}, ...]}

    A raw .osm file is not that: a way carries node *references*, so the
    coordinates are stitched back on here. Untagged ways are kept only as
    geometry for the relations that use them — they are what a multipolygon
    is made of, and drawing them as well would double every courtyard wall.
    Ways whose nodes are not all in the file (an extract cuts through them)
    are counted and skipped rather than drawn short.
    """
    nodes: dict[int, tuple[float, float]] = {}
    tagged_nodes: list[tuple[int, float, float, dict]] = []
    way_refs: dict[int, list[int]] = {}
    tagged_ways: list[tuple[int, dict]] = []
    relations: list[tuple[int, list[tuple[str, int, str]], dict]] = []
    bounds = None

    def tags_of(el):
        return {t.get("k"): t.get("v") for t in el.findall("tag")
                if t.get("k") is not None}

    parser = ET.XMLPullParser(events=("start", "end"))
    root = None
    seen = 0
    data = head
    while True:
        if data:
            parser.feed(data)
        for event, el in parser.read_events():
            if event == "start":
                if root is None:
                    root = el
                continue
            tag = el.tag
            if tag == "node":
                nid = int(el.get("id"))
                lon, lat = _num(el, "lon"), _num(el, "lat")
                if lon is None or lat is None:
                    el.clear()
                    continue
                nodes[nid] = (lon, lat)
                tags = tags_of(el)
                if tags:
                    tagged_nodes.append((nid, lon, lat, tags))
            elif tag == "way":
                wid = int(el.get("id"))
                refs = [int(nd.get("ref")) for nd in el.findall("nd")
                        if nd.get("ref")]
                way_refs[wid] = refs
                tags = tags_of(el)
                if tags:
                    tagged_ways.append((wid, tags))
            elif tag == "relation":
                members = [(m.get("type"), int(m.get("ref")), m.get("role")
                            or "")
                           for m in el.findall("member") if m.get("ref")]
                tags = tags_of(el)
                if tags and members:
                    relations.append((int(el.get("id")), members, tags))
            elif tag == "bounds":
                bounds = (_num(el, "minlat"), _num(el, "minlon"),
                          _num(el, "maxlat"), _num(el, "maxlon"))
            elif tag == "bound" and el.get("box"):
                # osmosis writes <bound box="s,w,n,e"> instead
                try:
                    s, w, n, e = (float(v) for v in el.get("box").split(","))
                    bounds = (s, w, n, e)
                except ValueError:
                    pass
            else:
                continue
            el.clear()
            seen += 1
            if root is not None and seen % 50_000 == 0:
                # Cleared elements stay linked to <osm>; drop the corpses so
                # a 300 MB export does not become 3 GB of resident memory.
                del root[:]
        data = stream.read(chunk)
        if not data:
            break
    parser.close()

    if bounds and any(v is None for v in bounds):
        bounds = None

    def geometry(refs):
        pts = [nodes[r] for r in refs if r in nodes]
        return pts, len(pts) != len(refs)

    elements = []
    for nid, lon, lat, tags in tagged_nodes:
        elements.append({"type": "node", "id": nid, "lon": lon, "lat": lat,
                         "tags": tags})
    incomplete = 0
    for wid, tags in tagged_ways:
        pts, clipped = geometry(way_refs.get(wid, []))
        if len(pts) < 2:
            incomplete += 1
            continue
        incomplete += bool(clipped)
        elements.append({
            "type": "way", "id": wid, "tags": tags,
            "geometry": [{"lon": x, "lat": y} for x, y in pts]})
    for rid, members, tags in relations:
        out_members = []
        for mtype, ref, role in members:
            if mtype != "way":
                continue
            pts, _ = geometry(way_refs.get(ref, []))
            if len(pts) < 3:
                continue
            out_members.append({
                "type": "way", "ref": ref, "role": role,
                "geometry": [{"lon": x, "lat": y} for x, y in pts]})
        if out_members:
            elements.append({"type": "relation", "id": rid, "tags": tags,
                             "members": out_members})

    stats = {"nodes": len(nodes), "ways": len(way_refs),
             "relations": len(relations), "incomplete_ways": incomplete,
             "bounds": bounds}
    return elements, stats


def read_osm_files(paths):
    """Parse several files into one element list, keyed by (type, id).

    OSM ids are global, so the same way exported twice — overlapping tiles,
    a file and its update — is one feature, not two. Later files win, which
    is what someone re-exporting a corrected area expects.
    """
    merged: dict[tuple[str, int], dict] = {}
    totals = {"nodes": 0, "ways": 0, "relations": 0, "incomplete_ways": 0}
    boxes = []
    for path in paths:
        with osm_stream(Path(path)) as (head, stream):
            elements, stats = parse_osm(head, stream)
        for el in elements:
            merged[(el["type"], el["id"])] = el
        for key in totals:
            totals[key] += stats[key]
        if stats["bounds"]:
            boxes.append(stats["bounds"])
    order = {"node": 0, "way": 1, "relation": 2}
    out = sorted(merged.values(), key=lambda el: (order[el["type"]], el["id"]))
    totals["bounds"] = union_bounds(boxes) if boxes else None
    return out, totals


def union_bounds(boxes):
    """Smallest (s, w, n, e) covering every box."""
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def element_bounds(elements):
    """(s, w, n, e) covering every coordinate in the elements, or None.

    Used when the file declares no <bounds> — an extract cut by hand, or one
    concatenated from several. It is the data's own envelope, not a
    requested extent, and the drawing says so by fitting the crop rectangle
    to it exactly.
    """
    lats, lons = [], []
    for el in elements:
        if el["type"] == "node":
            lons.append(el["lon"])
            lats.append(el["lat"])
        for g in el.get("geometry", []):
            lons.append(g["lon"])
            lats.append(g["lat"])
        for m in el.get("members", []):
            for g in m.get("geometry", []):
                lons.append(g["lon"])
                lats.append(g["lat"])
    if not lats:
        return None
    return (min(lats), min(lons), max(lats), max(lons))


def parse_bbox(text):
    """'S,W,N,E' in degrees -> tuple, the order Overpass and this repo use."""
    try:
        s, w, n, e = (float(v) for v in str(text).split(","))
    except ValueError:
        raise OsmFileError("--bbox wants four numbers: S,W,N,E")
    if s >= n or w >= e:
        raise OsmFileError(f"--bbox {text} is inside out; give S,W,N,E.")
    return s, w, n, e


def nominal_extent(box):
    """Width and height in metres for a lat/lon box.

    Uses the same 111,320 m-per-degree approximation `bbox_around()` does,
    on purpose: the crop rectangle, the staged extent and `db2dxf.py`'s
    re-issue are all sized from this number, and a more exact figure here
    would put them a couple of metres apart.
    """
    s, w, n, e = box
    lat = (s + n) / 2
    return ((e - w) * 111320.0 * math.cos(math.radians(lat)),
            (n - s) * 111320.0)


def box_centre(box):
    s, w, n, e = box
    return (s + n) / 2, (w + e) / 2


def intersects_box(pts, box, margin=0.0005):
    """Does this ring or line come within the box (plus the crop margin)?

    Buildings are never trimmed, so an extract wider than --bbox is filtered
    whole-feature here rather than clipped: a footprint straddling the line
    stays a footprint, which is the rule the rest of the repo keeps.
    """
    if not pts:
        return False
    s, w, n, e = box
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (min(lons) <= e + margin and max(lons) >= w - margin
            and min(lats) <= n + margin and max(lats) >= s - margin)


# ---------------------------------------------------------------- selection
# The feature types an import dialog offers. `road` is carriageways and
# `path` is footways/cycleways/steps, kept apart because they are separate
# NCS layers here (a 1.5 m path drawn with two kerb lines reads as a road).
TYPE_CHOICES = ("building", "road", "path", "water", "green", "rail",
                "barrier", "landmark", "power", "tree", "landuse",
                "parking", "plaza")


def select_types(features, types):
    """Keep only the requested feature types, dropping the rest.

    `types` is a set from TYPE_CHOICES, or None for everything. Roads split
    on PATH_TYPES, and `landmark` covers both shapes a landmark takes — the
    point symbols and the grounds around them.
    """
    if not types:
        return dict(features)
    keep = dict(features)
    if "building" not in types:
        keep["buildings"] = []
    roads = keep["roads"]
    if "road" not in types:
        roads = [r for r in roads if r[3] in t2c.PATH_TYPES]
    if "path" not in types:
        roads = [r for r in roads if r[3] not in t2c.PATH_TYPES]
    keep["roads"] = roads
    for name, key in (("water", "water"), ("green", "green"),
                      ("rail", "rails"), ("barrier", "barriers")):
        if name not in types:
            keep[key] = []
    if "landmark" not in types:
        keep["pois"], keep["site_pois"] = [], []
    if "power" not in types:
        keep["power"], keep["pipelines"] = [], []
        keep["points"] = [m for m in keep["points"] if m[0] != "power"]
    if "tree" not in types:
        keep["points"] = [m for m in keep["points"] if m[0] != "tree"]
    if "landuse" not in types:
        keep["zoning"] = []
    if "parking" not in types:
        keep["parking"] = []
    if "plaza" not in types:
        keep["plazas"] = []
    if "barrier" not in types:
        keep["points"] = [m for m in keep["points"] if m[0] != "gate"]
    if "power" not in types:
        keep["points"] = [m for m in keep["points"] if m[0] != "lamp"]
    return keep


# ------------------------------------------------------------------- layers
# A layer name may not carry these; AutoCAD rejects the whole table entry.
LAYER_BAD_CHARS = '<>/\\":;?*|=`,\'' + "".join(chr(c) for c in range(32))


def sanitise_layer(value: str) -> str:
    """OSM tag value -> a fragment usable in a DXF layer name."""
    out = "".join("-" if c in LAYER_BAD_CHARS or c.isspace() else c
                  for c in str(value)).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out.upper()[:24]


def layer_variant(base: str, value) -> str:
    """C-ROAD-CNTR + 'residential' -> 'C-ROAD-CNTR-RESIDENTIAL'.

    This is the 'layers distributed by a field value' option an OSM import
    dialog offers. It is a *suffix* on the NCS name rather than a name of its
    own, so freezing C-ROAD-CNTR* still catches every split layer and a
    drafter's existing layer filters keep working.
    """
    frag = sanitise_layer(value) if value is not None else ""
    return f"{base}-{frag}" if frag else base


# ------------------------------------------------------------------- XDATA
# The XDATA and attribute-table rules live in stage_db.py, because all three
# CAD writers need them — including db2dxf.py, which re-attaches the same
# tags from staging_tags so a re-issued drawing does not come back stripped
# of its source data. Re-exported here under their old names so this file
# reads as one piece.
XDATA_APPID = _anchor_rules.XDATA_APPID
XDATA_MAX_TAGS = _anchor_rules.XDATA_MAX_TAGS
ATTR_FIELDS = _anchor_rules.ATTR_FIELDS
xdata_tags = _anchor_rules.xdata_tags
attribute_rows = _anchor_rules.attribute_rows


source_tags = t2c.source_tags


def tags_for(index, fid):
    """Tags for a feature id, falling back past the '/0' a multi-outer
    relation adds to each of its parts."""
    if fid in index:
        return index[fid]
    head = fid.rsplit("/", 1)[0]
    return index.get(head, {})


def drawn_tags(drawn, index):
    """{feature_id: tags} for the features that reached the drawing.

    Resolves the relation-part suffix once, here, so the staging layer and
    the attribute table both work off plain feature ids.
    """
    return {rec["feature_id"]: tags_for(index, rec["feature_id"])
            for rec in drawn}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--input", action="append", required=True, metavar="FILE",
                   help="OSM XML file (.osm/.xml, .gz, .bz2 or .zip); "
                        "repeatable, ids deduplicate across files")
    p.add_argument("--out", help="output DXF path (or use --outdir)")
    p.add_argument("--outdir",
                   help="Group this run in its own folder under DIR: creates "
                        "DIR/<file>_<extent>_<timestamp>/site.dxf")
    p.add_argument("--bbox", metavar="S,W,N,E",
                   help="Crop to this box in degrees. Without it the whole "
                        "file is drawn and the extent rectangle is fitted to "
                        "the data, or to the <bounds> the export declares.")
    p.add_argument("--epsg", type=int,
                   help="Force a projected CRS (e.g. 32647) instead of "
                        "deriving the UTM zone from the data. The drawing is "
                        "in that CRS's units — give a projected system, not "
                        "4326, or the DXF comes out in degrees.")
    p.add_argument("--types", metavar="LIST",
                   help="Comma-separated feature types to import: "
                        + ", ".join(TYPE_CHOICES) + " (default: all)")
    p.add_argument("--layer-by", metavar="TAG",
                   help="Split each layer by the value of this OSM tag, e.g. "
                        "--layer-by highway gives C-ROAD-CNTR-RESIDENTIAL. "
                        "Affects the DXF only; --db always stages the base "
                        "NCS layer, which is the layer set db2dxf.py knows.")
    p.add_argument("--grid", nargs="?", const="auto", metavar="SPACING",
                   help="Draw a UTM coordinate grid: crosses every SPACING "
                        "metres with the easting and northing along two "
                        "edges. Bare --grid picks a round interval.")
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
    p.add_argument("--no-attributes", action="store_true",
                   help="Do not attach the OSM tags to each entity as XDATA "
                        "under the 'OSM' application id. The default carries "
                        "them so a drafter can LIST a building and read its "
                        "source tags.")
    p.add_argument("--all-features", action="store_true",
                   help="Draw everything in the file, not the curated tag "
                        "list: whatever no rule claims lands on "
                        "C-MISC-OTHR / C-MISC-SYMB rather than being "
                        "dropped. No refetch — the export already holds it.")
    p.add_argument("--all-poi", action="store_true",
                   help="Draw every amenity/tourism/historic feature instead "
                        "of only the civic landmarks a submission needs.")
    p.add_argument("--names-only", action="store_true",
                   help="Label only buildings that carry an OSM name, instead "
                        "of falling back to the B### inventory code.")
    p.add_argument("--mono", action="store_true",
                   help="Monochrome: every layer on ACI 7.")
    p.add_argument("--sheet", choices=["A4", "A3", "A2", "A1", "A0"],
                   help="Add a plottable paper-space layout at this size")
    p.add_argument("--scale", default="fit",
                   help="Plot scale denominator for --sheet, or 'fit'")
    p.add_argument("--db", metavar="PATH",
                   help="Also stage the imported features into the SQLite "
                        "layer, so db2dxf.py can re-issue the drawing after "
                        "names are corrected")
    p.add_argument("--project", metavar="NAME",
                   help="Project name for --db (default: the input filename)")
    p.add_argument("--replace", action="store_true",
                   help="Clear the project before staging instead of merging "
                        "into it. The default merges, so importing one "
                        "feature type at a time from the same file — "
                        "buildings, then roads — builds up one drawing "
                        "rather than each run wiping the last.")
    a = p.parse_args(argv)
    if not a.out and not a.outdir:
        p.error("give either --out <file.dxf> or --outdir <dir>")
    if a.types:
        wanted = {t.strip().lower() for t in a.types.split(",") if t.strip()}
        unknown = wanted - set(TYPE_CHOICES)
        if unknown:
            p.error(f"unknown --types {', '.join(sorted(unknown))}; "
                    f"choose from {', '.join(TYPE_CHOICES)}")
        a.types = wanted
    return a


def main(argv=None) -> int:
    import ezdxf
    from ezdxf.enums import MTextEntityAlignment
    from pyproj import Transformer
    from shapely.geometry import LineString, MultiLineString

    import blocks

    a = parse_args(argv)

    try:
        elements, stats = read_osm_files(a.input)
    except OsmFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except (ET.ParseError, zipfile.BadZipFile, OSError) as exc:
        print(f"ERROR: cannot read the OSM file ({type(exc).__name__}): "
              f"{exc}", file=sys.stderr)
        return 1
    print(f"Read {len(a.input)} file(s): {stats['nodes']} nodes, "
          f"{stats['ways']} ways, {stats['relations']} relations"
          + (f" ({stats['incomplete_ways']} ways incomplete in the extract)"
             if stats["incomplete_ways"] else ""))
    if not elements:
        print("ERROR: the file holds no tagged features to draw.",
              file=sys.stderr)
        return 1

    try:
        box = parse_bbox(a.bbox) if a.bbox else None
    except OsmFileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    # Precedence: what you asked for, then what the export says it covers,
    # then the envelope of the data itself.
    source = "--bbox"
    if box is None:
        box, source = (stats["bounds"], "the file's <bounds>") \
            if stats["bounds"] else (element_bounds(elements), "the data")
    if box is None:
        print("ERROR: no coordinates in the file.", file=sys.stderr)
        return 1
    s, w, n, e = box
    lat, lon = box_centre(box)
    ext_w, ext_h = nominal_extent(box)
    print(f"Extent from {source}: {s:.6f},{w:.6f} to {n:.6f},{e:.6f} "
          f"({ext_w:.0f} x {ext_h:.0f} m)")

    epsg = a.epsg or t2c.utm_epsg_for(lat, lon)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    to_wgs = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    if a.epsg:
        print(f"Projected CRS: EPSG:{epsg} (forced)")
        if 4000 <= epsg < 5000:
            print("WARNING: that is a geographic CRS — the drawing will be "
                  "in degrees, not metres, and CAD distances will be "
                  "meaningless. Pass a projected system.")
    else:
        zone = epsg - (32600 if lat >= 0 else 32700)
        print(f"Projected CRS: EPSG:{epsg} "
              f"(UTM {zone}{'N' if lat >= 0 else 'S'}), units = metres")

    features = select_types(
        t2c.classify_elements(elements, curated=not a.all_poi,
                              keep_other=a.all_features), a.types)
    tag_index = source_tags(elements)

    # --bbox filters whole features; nothing is trimmed to it. Linework is
    # clipped by clip_runs() further down with the usual ~55 m overhang, so
    # roads cross the crop line cleanly instead of stopping on it.
    if a.bbox:
        before = sum(len(v) for v in features.values())
        features["buildings"] = [
            b for b in features["buildings"] if intersects_box(b[1][0], box)]
        features["site_pois"] = [
            p for p in features["site_pois"] if intersects_box(p[1], box)]
        features["pois"] = [
            p for p in features["pois"]
            if intersects_box([(p[1], p[2])], box)]
        dropped = before - sum(len(v) for v in features.values())
        if dropped:
            print(f"--bbox: {dropped} feature(s) outside the box dropped")

    buildings, roads = features["buildings"], features["roads"]
    water, green = features["water"], features["green"]
    rails, barriers = features["rails"], features["barriers"]
    pois, site_pois = features["pois"], features["site_pois"]
    power, pipelines = features["power"], features["pipelines"]
    point_marks = features["points"]
    zoning, parking = features["zoning"], features["parking"]
    plazas = features["plazas"]
    other_lines = features["other_lines"]
    other_points = features["other_points"]
    print(f"OSM: {len(buildings)} buildings, {len(roads)} roads, "
          f"{len(water)} water, {len(green)} green, {len(rails)} rail, "
          f"{len(barriers)} barriers, {len(pois)} POI points, "
          f"{len(site_pois)} POI areas, {len(power)} power, "
          f"{len(pipelines)} pipeline, {len(point_marks)} pylon/tree, "
          f"{len(zoning)} land-use, {len(parking)} parking, "
          f"{len(plazas)} plaza"
          + (f", {len(other_lines)} other line(s), {len(other_points)} "
             "other point(s)" if a.all_features else ""))

    # The output path is resolved before drawing, not after: a background
    # map is written beside the .dxf, and the DXF stores a path to it.
    out = Path(a.out) if a.out else None
    if a.outdir:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run = Path(a.outdir) / (f"{Path(a.input[0]).stem}_"
                                f"{ext_w:.0f}x{ext_h:.0f}_{stamp}")
        run.mkdir(parents=True, exist_ok=True)
        out = run / "site.dxf"
        print(f"Run folder: {run}")
    out.parent.mkdir(parents=True, exist_ok=True)
    project = a.project or Path(a.input[0]).stem
    # Provenance names the file, not just "openstreetmap": a project can
    # hold a live Overpass extract and two .osm exports at once, and a
    # source column that says the same thing for all three cannot report
    # what the drawing is made of.
    names = [Path(f).name for f in a.input]
    source_label = ("openstreetmap:" + (names[0] if len(names) == 1
                                        else f"{len(names)} files"))

    # ---- DXF ------------------------------------------------------------
    doc = ezdxf.new("R2010", setup=_anchor_rules.DXF_SETUP)
    msp = doc.modelspace()
    t2c.add_text_styles(doc)
    for key, color, lw in [("contour_plain", 8, 13),
                           ("contour_major", 8, 25), ("contour_minor", 8, 9),
                           ("building", 4, 50),
                           ("building_unnamed", 254, 35), ("anno", 2, 25),
                           ("anno_th", 2, 25), ("anno_en", 7, 25),
                           ("road_edge", 30, 35), ("road_centre", 8, 9),
                           ("road_path", 8, 13), ("road_arrow", 30, 18),
                           ("road_bridge", 7, 40), ("road_tunnel", 8, 18),
                           ("water", 5, 18), ("green", 3, 13),
                           ("rail", 250, 18), ("barrier", 9, 13),
                           ("poi", 6, 18), ("site_poi", 5, 25),
                           ("power", 6, 25), ("pipeline", 4, 18),
                           ("tree", 3, 13), ("addr", 8, 13),
                           ("zoning", 32, 13), ("parking", 140, 13),
                           ("grid", 253, 9), ("dims", 2, 18),
                           ("plaza", 8, 18), ("lamp", 51, 13),
                           ("other", 9, 9), ("other_point", 9, 9),
                           # Created empty: this route has no DEM to sample,
                           # but db2dxf.py defines the layer either way and
                           # the two layer tables have to agree.
                           ("spot", 8, 18),
                           # Same reason: this route never fetches Overture
                           # places — the file is the source of truth — but
                           # db2dxf.py defines their layers, and a layer
                           # table that differs is a difference dxfdiff
                           # reports even when every entity matches.
                           ("overture", 214, 13), ("overture_th", 214, 18),
                           ("overture_en", 214, 18),
                           ("extent", 7, 35),
                           ("north", 7, 35), ("site", 1, 35)]:
        layer = doc.layers.add(t2c.LAYERS[key], color=color)
        layer.dxf.lineweight = lw
    prop = doc.layers.add(t2c.LAYERS["property"], color=1, linetype="PHANTOM")
    prop.dxf.lineweight = 70
    setb = doc.layers.add(t2c.LAYERS["setback"], color=2, linetype="DASHED")
    setb.dxf.lineweight = 25
    corner = doc.layers.add(t2c.LAYERS["corner"], color=1)
    corner.dxf.lineweight = 25
    row = doc.layers.add(t2c.LAYERS["road_row"], color=1, linetype="PHANTOM")
    row.dxf.lineweight = 35
    doc.layers.get(t2c.LAYERS["road_centre"]).dxf.linetype = "CENTER"
    doc.layers.get(t2c.LAYERS["extent"]).dxf.linetype = "DASHED"
    doc.layers.get(t2c.LAYERS["road_tunnel"]).dxf.linetype = "HIDDEN"
    doc.header["$LTSCALE"] = 5.0
    if not a.no_attributes:
        doc.appids.add(XDATA_APPID)

    layer_cache = {}

    def layer_for(base, fid):
        """The entity's layer, split by --layer-by when asked. A new variant
        inherits the colour and lineweight of the NCS layer it came from, so
        the split changes how the drawing organises, not how it reads."""
        if not a.layer_by:
            return base
        name = layer_variant(base, tags_for(tag_index, fid).get(a.layer_by))
        if name != base and name not in layer_cache:
            parent = doc.layers.get(base)
            new = doc.layers.add(name, color=parent.dxf.color)
            new.dxf.lineweight = parent.dxf.lineweight
            new.dxf.linetype = parent.dxf.linetype
            layer_cache[name] = True
        return name

    def attach(entity, fid):
        """Hang the source OSM tags off the entity as extended data."""
        if a.no_attributes or entity is None:
            return
        tags = tags_for(tag_index, fid)
        if tags:
            entity.set_xdata(XDATA_APPID, xdata_tags(fid, tags))

    # Annotation is sized in metres of ground; only the plot scale says
    # what that is on paper. Resolved here, before a label is written, and
    # by the same shared rule the other two writers use.
    if a.sheet:
        import sheet as _sheet
        anno = _anchor_rules.annotation_scale(
            _sheet.fitting_scale(ext_w, ext_h, a.sheet)[0]
            if str(a.scale).lower() == "fit" else int(a.scale))
    else:
        anno = 1.0

    def mtext(label, x, y, height, rotation=0.0, layer=None):
        layer = layer or t2c.LAYERS["anno"]
        m = msp.add_mtext(str(label), dxfattribs={
            "layer": layer, "char_height": height * anno,
            "style": t2c.ANNO_STYLE[layer][1]})
        m.set_location((x, y), rotation=rotation,
                       attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
        # A label crossing a building outline or a road edge punches a clean
        # hole through it instead of overprinting. None would REMOVE the mask.
        m.set_bg_color("canvas", scale=t2c.BG_MASK_SCALE)
        return m

    def mtext_bilingual(th, en, x, y, height, rotation=0.0, fallback=None):
        """Thai and Latin on their own layers, English stacked above Thai."""
        count = 0
        if th:
            mtext(th, x, y, height, rotation, t2c.LAYERS["anno_th"])
            count += 1
        if en:
            ex, ey = ((x, y) if not th else
                      t2c.offset_along_normal(x, y, rotation,
                                              height * anno
                                              * t2c.LANG_OFFSET))
            mtext(en, ex, ey, height, rotation, t2c.LAYERS["anno_en"])
            count += 1
        if not count and fallback:
            mtext(fallback, x, y, height, rotation)
            count += 1
        return count

    # ---- buildings -------------------------------------------------------
    # What actually reached the drawing, in drawing order: the attribute
    # table is built from this rather than from the file, so it describes
    # the DXF a drafter has rather than the export it came from.
    drawn = []
    inventory, staged_geoms = [], {}
    counter = 0
    for (th, en), (ext, holes), fid in sorted(buildings, key=lambda b: b[2]):
        ux, uy = to_utm.transform(*zip(*ext))
        upts = list(zip(ux, uy))
        uholes = []
        for hole in holes:
            hx, hy = to_utm.transform(*zip(*hole))
            uholes.append(list(zip(hx, hy)))
        name = th or en
        # Named footprints on C-BLDG-OUTL, anonymous ones on their own
        # layer: the sheet says which buildings OSM identifies.
        base = t2c.LAYERS["building" if name else "building_unnamed"]
        poly_layer = layer_for(base, fid)
        # Draw the repaired geometry, which is what gets staged: a
        # self-intersecting ring becomes two polygons, and drawing the raw
        # ring while staging the repaired one makes a re-issue differ.
        shape = _anchor_rules.repaired_polygon(upts, uholes)
        first = True
        for part in _anchor_rules.polygon_parts(shape):
            entity = msp.add_lwpolyline(
                _anchor_rules.ring_points(part.exterior.coords), close=True,
                                        dxfattribs={"layer": poly_layer})
            if first:
                attach(entity, fid)
                first = False
            for ring in part.interiors:         # courtyards stay open
                msp.add_lwpolyline(_anchor_rules.ring_points(ring.coords),
                                   close=True,
                                   dxfattribs={"layer": poly_layer})
        code = ""
        if not name:
            counter += 1
            code = f"B{counter:03d}"
        # The anchor is the staging layer's own call on the same shape, so
        # the label sits in the same place in both drawings.
        try:
            cx, cy = _anchor_rules.interior_point(shape)
        except Exception:
            cx, cy = sum(ux) / len(ux), sum(uy) / len(uy)
        # An unnamed footprint is labelled by its B### code, the handle
        # building_inventory.csv is keyed on. cad_labels emits the same
        # code at the same anchor, so a re-issue of this import agrees
        # with it — the code has to reach both drawings or neither.
        mtext_bilingual(th, en, cx, cy, 3.5,
                        fallback=None if a.names_only else code)
        btags = tags_for(tag_index, fid)
        house = btags.get("addr:housenumber")
        if house:
            hx, hy = t2c.offset_along_normal(cx, cy, 0.0, -3.0 * anno)
            mtext(house, hx, hy, 2.2, layer=t2c.LAYERS["addr"])
        levels = _anchor_rules.levels_label(btags)
        if levels:
            lx2, ly2 = t2c.offset_along_normal(cx, cy, 0.0, -5.4 * anno)
            mtext(levels, lx2, ly2, 2.2, layer=t2c.LAYERS["addr"])
        staged_geoms[fid] = (upts, uholes)
        drawn.append({"feature_id": fid, "feature_type": "building",
                      "cad_layer": poly_layer, "display_name": name or ""})
        blon, blat = to_wgs.transform(cx, cy)
        inventory.append({"feature_id": fid, "code": code,
                          "osm_name": name or "", "display_name": name or "",
                          "cad_layer": base,
                          "name_th": th or "", "name_en": en or "",
                          "addr_house": house or "",
                          "levels_label": levels,
                          "source": source_label,
                          "latitude": round(blat, 8),
                          "longitude": round(blon, 8)})

    # ---- roads -----------------------------------------------------------
    staged_roads = []
    road_plan = []
    for (th, en), ref, pts, highway, fid, oneway in roads:
        road_tags = tags_for(tag_index, fid)
        width_m = t2c.carriageway_width(road_tags, highway)
        is_path = highway in t2c.PATH_TYPES
        base = t2c.road_cad_layer(road_tags, highway)
        cad_layer = layer_for(base, fid)
        official = (road_tags.get("official_name:th")
                    or road_tags.get("official_name") or "")
        if not (th or en) and official:
            th, en = t2c.names_by_lang({"name": official})
        road_runs = []
        for i, run in enumerate(t2c.clip_runs(pts, s, w, n, e)):
            ux, uy = to_utm.transform(*zip(*run))
            upts = list(zip(ux, uy))
            if len(upts) < 2:
                continue
            road_runs.append(upts)
            # Drawn in a second pass: the kerb lines are trimmed against
            # each other, so the whole network has to exist first. Note
            # `base`, not `cad_layer` — under --layer-by the layer carries a
            # tag suffix, and whether a way is at grade is a property of the
            # way, not of how the drawing chose to file it.
            road_plan.append({"key": (fid, i), "pts": upts, "fid": fid,
                              "width_m": 0.0 if is_path else width_m,
                              "cad_layer": cad_layer, "is_path": is_path,
                              "oneway": oneway,
                              "at_grade": base == t2c.LAYERS["road_centre"]})
        if road_runs:
            drawn.append({"feature_id": fid,
                          "feature_type": "path" if is_path else "road",
                          "cad_layer": cad_layer,
                          "display_name": (th or en) or ""})
            staged_roads.append({
                "feature_id": fid, "highway_type": highway,
                "source": source_label,
                "road_name": th or en, "road_ref": ref,
                "name_th": th or "", "name_en": en or "",
                # Staging keeps the base NCS layer: db2dxf.py draws from a
                # fixed layer table, so a --layer-by variant staged here
                # would re-issue onto a layer that table has no entry for.
                "cad_layer": base, "oneway": oneway,
                "official_name": official,
                "carriageway_m": 0.0 if is_path else width_m,
                "runs": road_runs})

    trimmed = _anchor_rules.carriageway_edges(
        [(r["key"], r["pts"], r["width_m"], r["at_grade"])
         for r in road_plan])
    n_edges = 0
    for r in road_plan:
        for edge in trimmed.get(r["key"], ()):
            msp.add_lwpolyline(
                edge, dxfattribs={"layer": t2c.LAYERS["road_edge"]})
            n_edges += 1
        attach(msp.add_lwpolyline(r["pts"],
                                  dxfattribs={"layer": r["cad_layer"]}),
               r["fid"])
        # Direction of travel, by the same shared rule the other two
        # writers use. Paths are excluded: a one-way footpath is not a
        # traffic instruction.
        if r["oneway"] and not r["is_path"]:
            size = _anchor_rules.oneway_arrow_size(r["width_m"])
            for ax, ay, rot in _anchor_rules.arrow_positions(r["pts"]):
                blocks.add_oneway_arrow(
                    doc, msp, ax, ay, size,
                    rot + (180.0 if r["oneway"] < 0 else 0.0),
                    t2c.LAYERS["road_arrow"])

    def runs_geom(runs):
        lines = [LineString(r) for r in runs if len(r) >= 2]
        if not lines:
            return None
        return lines[0] if len(lines) == 1 else MultiLineString(lines)

    def label_longest(records, key_of, emit):
        """One label per unique key, on the longest feature carrying it —
        the rule cad_labels applies with ROW_NUMBER ... ORDER BY length_m."""
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
        # Always 'ทล.', matching cad_labels and topo2cad.py: a bare "311"
        # beside a road name reads as a distance or a lane count.
        th, en, name = rec["name_th"], rec["name_en"], rec["road_name"]
        text = f"ทล.{rec['road_ref']}"
        off = 0.0 if not name else (
            (6.0 + (5.0 * t2c.LANG_OFFSET if th and en else 0.0)) * anno)
        rx, ry = t2c.offset_along_normal(x, y, rot, off)
        mtext(text, rx, ry, 4.0, rotation=rot,
              layer=t2c.LAYERS["anno_th" if t2c.is_thai(text) else "anno_en"])

    label_longest(staged_roads, lambda r: r["road_name"], emit_road_name)
    label_longest(staged_roads, lambda r: r["road_ref"], emit_road_ref)

    # ---- context linework ------------------------------------------------
    staged_context = []

    def draw_lines(items, kind, base, label=False):
        for (th, en), pts, fid in sorted(items, key=lambda f: f[2]):
            cad_layer = layer_for(base, fid)
            runs = []
            for run in t2c.clip_runs(pts, s, w, n, e):
                ux, uy = to_utm.transform(*zip(*run))
                upts = list(zip(ux, uy))
                closed = run[0] == run[-1]
                attach(msp.add_lwpolyline(upts, close=closed,
                                          dxfattribs={"layer": cad_layer}),
                       fid)
                runs.append(upts)
            if runs:
                # The feature's own name, not `name` from the enclosing
                # scope: that was the *last building's*, and on a file with
                # no buildings it was not assigned at all.
                drawn.append({"feature_id": fid, "feature_type": kind,
                              "cad_layer": cad_layer,
                              "display_name": (th or en) or ""})
                staged_context.append({
                    "feature_id": fid, "kind": kind, "cad_layer": base,
                    "source": source_label,
                    "name_th": th or "", "name_en": en or "",
                    "display_name": (th or en) or "", "labelled": bool(label),
                    "runs": runs})

    draw_lines(water, "water", t2c.LAYERS["water"], label=True)
    draw_lines(green, "green", t2c.LAYERS["green"], label=True)
    draw_lines(rails, "rail", t2c.LAYERS["rail"])
    draw_lines(barriers, "barrier", t2c.LAYERS["barrier"])
    draw_lines(zoning, "zoning", t2c.LAYERS["zoning"], label=True)
    draw_lines(parking, "parking", t2c.LAYERS["parking"], label=True)
    draw_lines(plazas, "plaza", t2c.LAYERS["plaza"], label=True)
    draw_lines(other_lines, "other", t2c.LAYERS["other"], label=True)
    draw_lines(power, "power", t2c.LAYERS["power"])
    draw_lines(pipelines, "pipeline", t2c.LAYERS["pipeline"])

    # Flow direction on waterways, by the same rule the other routes use
    for rec in staged_context:
        if rec["kind"] != "water":
            continue
        for run in rec["runs"]:
            if len(run) >= 2 and run[0] != run[-1]:
                for ax, ay, rot in _anchor_rules.arrow_positions(run):
                    blocks.add_oneway_arrow(doc, msp, ax, ay,
                                            _anchor_rules.FLOW_ARROW_M, rot,
                                            t2c.LAYERS["water"])

    label_longest(
        [r for r in staged_context if r["labelled"]],
        lambda r: (r["kind"], r["display_name"]) if r["display_name"] else None,
        lambda r, x, y, rot: mtext_bilingual(
            r["name_th"] or None, r["name_en"] or None, x, y, 4.0,
            rotation=rot))

    # ---- landmarks -------------------------------------------------------
    staged_site_pois = []
    for (th, en), pts, fid, kind in sorted(site_pois, key=lambda p: p[2]):
        ux, uy = to_utm.transform(*zip(*pts))
        upts = list(zip(ux, uy))
        poi_layer = layer_for(t2c.LAYERS["site_poi"], fid)
        shape = _anchor_rules.repaired_polygon(upts)
        first = True
        for part in _anchor_rules.polygon_parts(shape):
            entity = msp.add_lwpolyline(
                _anchor_rules.ring_points(part.exterior.coords), close=True,
                                        dxfattribs={"layer": poi_layer})
            if first:
                attach(entity, fid)
                first = False
            for ring in part.interiors:
                msp.add_lwpolyline(_anchor_rules.ring_points(ring.coords),
                                   close=True,
                                   dxfattribs={"layer": poi_layer})
        try:
            cx, cy = _anchor_rules.interior_point(shape)
        except Exception:
            cx, cy = sum(ux) / len(ux), sum(uy) / len(uy)
        mtext_bilingual(th, en, cx, cy, 3.5)
        drawn.append({"feature_id": fid, "feature_type": "landmark_area",
                      "cad_layer": t2c.LAYERS["site_poi"],
                      "display_name": th or en or ""})
        staged_site_pois.append({"feature_id": fid, "poi_key": kind[0],
                                 "poi_type": kind[1], "name_th": th or "",
                                 "name_en": en or "",
                                 "display_name": th or en or "",
                                 "geom_pts": upts})

    staged_pois = []
    for (th, en), plon, plat, fid, key in sorted(other_points,
                                                 key=lambda m: m[3]):
        px, py = to_utm.transform(plon, plat)
        base = t2c.LAYERS["other_point"]
        layer = layer_for(base, fid)
        attach(blocks.add_symbol(doc, msp, px, py,
                                 blocks.symbol_size(base), layer), fid)
        title = (th or en) or ""
        if title:
            mtext_bilingual(th, en,
                            px + _anchor_rules.POI_LABEL_DX * anno,
                            py, 4.0)
        drawn.append({"feature_id": fid, "feature_type": key or "other",
                      "cad_layer": layer, "display_name": title})
        staged_pois.append({"feature_id": fid, "source": source_label,
                            "poi_key": key or "other",
                            "poi_type": key or "", "name_th": th or "",
                            "name_en": en or "",
                            "display_name": title,
                            "cad_layer": t2c.LAYERS["other_point"],
                            "x": px, "y": py,
                            "latitude": plat, "longitude": plon})

    # Pylons, poles and trees: a symbol and no label. They stage in the POI
    # table with an empty display_name, which cad_labels skips.
    for kind, plon, plat, fid, ptype in sorted(point_marks,
                                               key=lambda m: m[3]):
        px, py = to_utm.transform(plon, plat)
        base = t2c.LAYERS[{"tree": "tree", "gate": "barrier",
                           "lamp": "lamp"}.get(kind, "power")]
        layer = layer_for(base, fid)
        attach(blocks.add_symbol(doc, msp, px, py,
                                 blocks.symbol_size(base), layer), fid)
        drawn.append({"feature_id": fid, "feature_type": kind,
                      "cad_layer": layer, "display_name": ""})
        staged_pois.append({"feature_id": fid, "source": source_label,
                            "poi_key": {"tree": "natural",
                                        "gate": "barrier",
                                        "lamp": "highway"}.get(kind, "power"),
                            "poi_type": ptype, "name_th": "", "name_en": "",
                            "display_name": "", "cad_layer": base,
                            "x": px, "y": py,
                            "latitude": plat, "longitude": plon})
    for (th, en), plon, plat, kind, fid in sorted(pois, key=lambda p: p[4]):
        px, py = to_utm.transform(plon, plat)
        attach(blocks.add_poi_symbol(doc, msp, px, py, 2.0,
                                     layer_for(t2c.LAYERS["poi"], fid)), fid)
        mtext_bilingual(th, en, px + _anchor_rules.POI_LABEL_DX * anno,
                        py, 4.0)
        drawn.append({"feature_id": fid, "feature_type": "landmark",
                      "cad_layer": t2c.LAYERS["poi"],
                      "display_name": th or en or ""})
        staged_pois.append({"feature_id": fid, "source": source_label,
                            "poi_key": kind[0], "poi_type": kind[1],
                            "name_th": th or "", "name_en": en or "",
                            "display_name": th or en or "",
                            "x": px, "y": py,
                            "latitude": plat, "longitude": plon})

    # ---- furniture -------------------------------------------------------
    # Sized from the nominal extent in metres, not the projected corners, so
    # a db2dxf.py re-issue — which has only the nominal figure — matches.
    cx, cy = to_utm.transform(lon, lat)
    hw, hh = ext_w / 2, ext_h / 2
    msp.add_lwpolyline([(cx - hw, cy - hh), (cx + hw, cy - hh),
                        (cx + hw, cy + hh), (cx - hw, cy + hh)],
                       close=True, dxfattribs={"layer": t2c.LAYERS["extent"]})
    if a.grid:
        spacing = (_anchor_rules.grid_spacing(ext_w, ext_h)
                   if str(a.grid) == "auto" else float(a.grid))
        eastings, northings = _anchor_rules.grid_ticks(cx, cy, ext_w, ext_h,
                                                       spacing)
        arm = min(ext_w, ext_h) * 0.006
        for gx in eastings:
            for gy in northings:
                msp.add_line((gx - arm, gy), (gx + arm, gy),
                             dxfattribs={"layer": t2c.LAYERS["grid"]})
                msp.add_line((gx, gy - arm), (gx, gy + arm),
                             dxfattribs={"layer": t2c.LAYERS["grid"]})
        for gx in eastings:
            mtext(f"{gx:,.0f} E", gx, cy - hh + arm * 2, arm * 1.6,
                  layer=t2c.LAYERS["grid"])
        for gy in northings:
            mtext(f"{gy:,.0f} N", cx - hw + arm * 2, gy, arm * 1.6,
                  rotation=90.0, layer=t2c.LAYERS["grid"])
        print(f"Grid: {spacing:g} m, {len(eastings)} x {len(northings)} "
              "lines")

    blocks.add_extent_dimensions(doc, msp, cx, cy, ext_w, ext_h,
                                 t2c.LAYERS["dims"])
    blocks.add_north_arrow(doc, msp, cx + hw * 0.94, cy + hh * 0.90,
                           min(ext_w, ext_h) * 0.02, t2c.LAYERS["north"])
    msp.add_circle((cx, cy), radius=5, dxfattribs={"layer": t2c.LAYERS["site"]})
    # Unformatted, like topo2cad.py: db2dxf.py prints the staged REAL back
    # with str(), so a formatted tag here would make a drawing and its
    # re-issue differ on the one label that is not geometry.
    mtext(f"GPS {lat},{lon}", cx + 40 * anno, cy, 5.0)

    if a.basemap:
        import basemap as bm
        try:
            info = bm.attach(doc, msp, box, epsg, out, provider=a.basemap,
                             zoom=a.basemap_zoom,
                             max_tiles=a.basemap_max_tiles)
        except (bm.BasemapError, ImportError) as exc:
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
        credit.set_bg_color("canvas", scale=t2c.BG_MASK_SCALE)

    if a.sheet:
        import sheet as sheet_mod
        scale = (sheet_mod.fitting_scale(ext_w, ext_h, a.sheet)[0]
                 if str(a.scale).lower() == "fit" else int(a.scale))
        sheet_mod.add_sheet(doc, {
            # Names the export the drawing was made from: "OpenStreetMap"
            # alone does not tell a reviewer which extract, or when.
            "source": _anchor_rules.credit_lines([source_label]),
            "project": project, "lat": lat, "lon": lon, "centre": (cx, cy),
            "srid": epsg, "extent": (ext_w, ext_h),
            "date": time.strftime("%Y-%m-%d"),
        }, size=a.sheet, scale=scale)
        print(f"Sheet: {a.sheet} paper space at 1:{scale:,}")

    if a.mono:
        t2c.apply_mono(doc)
        print("Monochrome: all layers set to ACI 7")
    # ezdxf writes UTF-8 regardless; what decides whether a
    # reader sees the Thai is the font the STYLE points at.
    _anchor_rules.check_fonts(t2c.TEXT_STYLES,
                     Path(out).with_name("fonts.txt"))
    _anchor_rules.set_drawing_extents(doc)
    doc.saveas(out)
    print(f"Saved: {out}")

    # The same two tables the other routes write. An .osm export carries
    # roads and landmarks like any other source, and a reader wants the
    # list either way.
    road_rows = [{"feature_id": r["feature_id"], "road_ref": r["road_ref"],
                  "highway_type": r["highway_type"],
                  "road_name": r["road_name"], "name_th": r["name_th"],
                  "name_en": r["name_en"],
                  "official_name": r.get("official_name", ""),
                  "cad_layer": r["cad_layer"],
                  "carriageway_m": r["carriageway_m"], "oneway": r["oneway"],
                  "length_m": sum(LineString(run).length
                                  for run in r["runs"] if len(run) >= 2),
                  "source": source_label}
                 for r in staged_roads]
    if road_rows:
        rp = out.with_name("road_inventory.csv")
        _anchor_rules.write_road_csv(rp, road_rows)
        print(f"Roads: {len(road_rows)} -> {rp}")
    poi_rows = []
    for rec in staged_pois:
        layer = rec.get("cad_layer") or "C-ANNO-SYMB"
        if (not rec.get("display_name")
                or layer not in _anchor_rules.LANDMARK_LAYERS):
            continue
        de, dn = rec["x"] - cx, rec["y"] - cy
        poi_rows.append({
            "feature_id": rec["feature_id"], "poi_key": rec.get("poi_key"),
            "poi_type": rec.get("poi_type"),
            "kind_th": _anchor_rules.poi_kind_thai(rec.get("poi_type")),
            "display_name": rec.get("display_name", ""),
            "name_th": rec.get("name_th", ""),
            "name_en": rec.get("name_en", ""),
            "distance_m": round(math.hypot(de, dn), 1),
            "bearing": _anchor_rules.bearing_text(de, dn),
            "latitude": rec.get("latitude"), "longitude": rec.get("longitude"),
            "cad_layer": layer, "source": rec.get("source", source_label)})
    if poi_rows:
        poi_rows.sort(key=lambda r: (r["distance_m"], r["feature_id"]))
        pp = out.with_name("landmark_inventory.csv")
        _anchor_rules.write_poi_csv(pp, poi_rows)
        print(f"Landmarks: {len(poi_rows)} nearby place(s) -> {pp}")

    inv_path = out.with_name("building_inventory.csv")
    with open(inv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "feature_id", "code", "osm_name", "display_name",
            "name_th", "name_en", "addr_house", "levels_label",
            "cad_layer", "source",
            "latitude", "longitude"])
        writer.writeheader()
        writer.writerows(inventory)
    named = sum(1 for r in inventory if r["osm_name"])
    print(f"Inventory: {len(inventory)} buildings ({named} named, "
          f"{len(inventory) - named} coded) -> {inv_path}")

    # The attribute table: the same tags the entities carry as XDATA, in a
    # form that opens outside AutoCAD. It is also the complete record —
    # XDATA stops at XDATA_MAX_TAGS per entity, this does not.
    feature_tags = drawn_tags(drawn, tag_index)
    attrs = attribute_rows(drawn, feature_tags)
    attr_path = out.with_name("attributes.csv")
    _anchor_rules.write_attribute_csv(attr_path, attrs)
    print(f"Attributes: {len(attrs)} tags on {len(drawn)} drawn features "
          f"-> {attr_path}")

    if a.db:
        if a.layer_by:
            print("Note: --layer-by splits the DXF only; the staging layer "
                  "keeps the base NCS layers db2dxf.py draws from.")
        staging = argparse.Namespace(
            db=a.db, project=project, lat=lat, lon=lon,
            width=ext_w, height=ext_h, radius=None, anno_scale=anno)
        t2c.stage_to_db(staging, epsg, inventory, staged_geoms, staged_roads,
                        contours=(), contour_layers={},
                        poi_points=staged_pois, poi_areas=staged_site_pois,
                        context=staged_context, attributes=attrs,
                        merge=not a.replace)
    if not a.no_attributes:
        print(f"XDATA: OSM tags attached to each entity under app id "
              f"'{XDATA_APPID}' — select an entity and LIST it to read them")
    print(f"CRS: EPSG:{epsg}. Centre at ({cx:.1f}, {cy:.1f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
