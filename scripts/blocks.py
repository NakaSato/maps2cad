#!/usr/bin/env python3
"""Reusable CAD blocks shared by the drawing writers.

Drawing the north arrow as loose circles, solids and text means a drafter
who wants to move it has to window-select three entities and hope they got
all of them. As a block it is one object: drag it, scale it, or delete it in
a single pick, and every drawing in the set carries an identical one.

Geometry is defined at **unit size** — a circle of radius 1 about the
origin — and scaled at insertion, so one definition serves a 500 m extent
and a 5 km one. Block geometry sits on layer "0", which is the CAD
convention that makes it inherit the layer of the INSERT rather than
carrying its own; that way the arrow follows whatever layer the drawing
puts it on without the block needing to know the layer names.
"""

from __future__ import annotations

NORTH_ARROW = "NORTH_ARROW"
POI_SYMBOL = "POI_SYMB"
ONEWAY_ARROW = "ONEWAY_ARROW"


def ensure_north_arrow(doc, style: str = "EN_STYLE") -> str:
    """Define the north arrow block once per document. Returns its name."""
    from ezdxf.enums import MTextEntityAlignment

    if NORTH_ARROW in doc.blocks:
        return NORTH_ARROW
    blk = doc.blocks.new(name=NORTH_ARROW)
    blk.add_circle((0, 0), radius=1.0, dxfattribs={"layer": "0"})
    blk.add_solid([(-0.3, -0.6), (0.3, -0.6), (0.0, 0.8)],
                  dxfattribs={"layer": "0"})
    label = blk.add_mtext("N", dxfattribs={
        "layer": "0", "char_height": 0.6,
        "style": style if style in doc.styles else "Standard"})
    label.set_location((0.0, 1.5),
                       attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
    return NORTH_ARROW


def add_north_arrow(doc, layout, x: float, y: float, size: float,
                    layer: str, style: str = "EN_STYLE"):
    """Place the arrow at (x, y), `size` being the circle radius in drawing
    units. The drawing is true-north-up in UTM, so it is never rotated."""
    ensure_north_arrow(doc, style)
    return layout.add_blockref(NORTH_ARROW, insert=(x, y), dxfattribs={
        "layer": layer, "xscale": size, "yscale": size, "zscale": size})


def ensure_poi_symbol(doc) -> str:
    """Define the landmark point symbol once per document.

    A circle of radius 1 about the origin, scaled at insertion. As a block
    rather than a loose circle, a drafter can redefine POI_SYMB once and
    every landmark on the drawing restyles with it — a triangle for a
    temple, a cross for a hospital — without touching the geometry.
    """
    if POI_SYMBOL in doc.blocks:
        return POI_SYMBOL
    blk = doc.blocks.new(name=POI_SYMBOL)
    blk.add_circle((0, 0), radius=1.0, dxfattribs={"layer": "0"})
    return POI_SYMBOL


def add_poi_symbol(doc, layout, x: float, y: float, size: float, layer: str):
    """Place a landmark symbol at (x, y); `size` is the circle radius."""
    ensure_poi_symbol(doc)
    return layout.add_blockref(POI_SYMBOL, insert=(x, y), dxfattribs={
        "layer": layer, "xscale": size, "yscale": size, "zscale": size})


TREE_SYMBOL = "TREE_SYMB"
PYLON_SYMBOL = "PYLON_SYMB"


def ensure_tree_symbol(doc) -> str:
    """A tree: a circle with a small cross at its trunk.

    Unit radius about the origin like the others, so one definition serves
    a 200 m plan and a 2 km one. The cross is what separates it from the
    landmark circle at a glance on a printed sheet.
    """
    if TREE_SYMBOL in doc.blocks:
        return TREE_SYMBOL
    blk = doc.blocks.new(name=TREE_SYMBOL)
    blk.add_circle((0, 0), radius=1.0, dxfattribs={"layer": "0"})
    blk.add_line((-0.35, 0), (0.35, 0), dxfattribs={"layer": "0"})
    blk.add_line((0, -0.35), (0, 0.35), dxfattribs={"layer": "0"})
    return TREE_SYMBOL


def ensure_pylon_symbol(doc) -> str:
    """A pylon or power pole: a square with its diagonals, the convention
    for a tower on a utility plan."""
    if PYLON_SYMBOL in doc.blocks:
        return PYLON_SYMBOL
    blk = doc.blocks.new(name=PYLON_SYMBOL)
    blk.add_lwpolyline([(-1, -1), (1, -1), (1, 1), (-1, 1)], close=True,
                       dxfattribs={"layer": "0"})
    blk.add_line((-1, -1), (1, 1), dxfattribs={"layer": "0"})
    blk.add_line((-1, 1), (1, -1), dxfattribs={"layer": "0"})
    return PYLON_SYMBOL


GATE_SYMBOL = "GATE_SYMB"


def ensure_gate_symbol(doc) -> str:
    """A gate: the two leaves swung open, which is how a gate is drawn on
    a site plan — an access point, not a blob."""
    if GATE_SYMBOL in doc.blocks:
        return GATE_SYMBOL
    blk = doc.blocks.new(name=GATE_SYMBOL)
    blk.add_line((-1, 0), (-0.2, 0), dxfattribs={"layer": "0"})
    blk.add_line((0.2, 0), (1, 0), dxfattribs={"layer": "0"})
    blk.add_arc((-0.2, 0), radius=0.8, start_angle=0, end_angle=70,
                dxfattribs={"layer": "0"})
    blk.add_arc((0.2, 0), radius=0.8, start_angle=110, end_angle=180,
                dxfattribs={"layer": "0"})
    return GATE_SYMBOL


LAMP_SYMBOL = "LAMP_SYMB"


def ensure_lamp_symbol(doc) -> str:
    """A street lamp: the post with its head, drawn as a circle on a short
    stem so it reads differently from a tree at plan scale."""
    if LAMP_SYMBOL in doc.blocks:
        return LAMP_SYMBOL
    blk = doc.blocks.new(name=LAMP_SYMBOL)
    blk.add_circle((0, 0), radius=0.55, dxfattribs={"layer": "0"})
    blk.add_line((0, -0.55), (0, -1.4), dxfattribs={"layer": "0"})
    blk.add_line((-0.5, -1.4), (0.5, -1.4), dxfattribs={"layer": "0"})
    return LAMP_SYMBOL


# Which symbol belongs on which layer. Both CAD routes go through
# add_symbol(), so a tree drawn during extraction and the same tree redrawn
# from the staging layer cannot come out as different marks — db2dxf.py
# knows only the layer a point was staged on.
SYMBOL_FOR_LAYER = {
    "C-LAND-TREE": ensure_tree_symbol,
    "C-UTIL-POWR": ensure_pylon_symbol,
    "C-BNDY-BARR": ensure_gate_symbol,
    "C-UTIL-LAMP": ensure_lamp_symbol,
}


# Plan size of each mark in drawing units (metres). Kept beside the symbol
# table rather than stored per row: db2dxf.py knows only the layer a point
# was staged on, and a tree that came back the size of a pylon would be a
# difference nobody staged.
SIZE_FOR_LAYER = {"C-LAND-TREE": 1.5, "C-UTIL-POWR": 2.0,
                  # A boundary corner is a point, not a feature: the mark
                  # says "here", the label says which row of the table.
                  "C-PROP-CORN": 0.6,
                  "C-BNDY-BARR": 2.0, "C-UTIL-LAMP": 1.5,
                  # smallest of the lot: --all-features draws hundreds of
                  # these and they must not swamp the drawing
                  "C-MISC-SYMB": 1.2}
DEFAULT_SYMBOL_SIZE = 2.0


def symbol_size(layer: str) -> float:
    return SIZE_FOR_LAYER.get(layer, DEFAULT_SYMBOL_SIZE)


def add_symbol(doc, layout, x: float, y: float, size: float, layer: str):
    """Place the symbol that belongs on `layer` — a landmark circle unless
    the layer says otherwise."""
    ensure = SYMBOL_FOR_LAYER.get(layer, ensure_poi_symbol)
    name = ensure(doc)
    return layout.add_blockref(name, insert=(x, y), dxfattribs={
        "layer": layer, "xscale": size, "yscale": size, "zscale": size})


# Dimensions are DIMENSION entities with a style of their own, not lines
# with a number beside them: a drafter expects to select one, see the
# extension lines highlight, and have it update if the geometry moves.
DIM_STYLE = "MAPS2CAD"


def ensure_dim_style(doc, text_height: float, style: str = "EN_STYLE"):
    """Define the dimension style once per document, sized for the drawing.

    Text height is passed in rather than fixed: the same style has to read
    on a 200 m site plan and an 8 km locality map, and a 2.5 mm number on
    the second is invisible.
    """
    if DIM_STYLE in doc.dimstyles:
        return DIM_STYLE
    dim = doc.dimstyles.add(DIM_STYLE)
    dim.dxf.dimtxsty = style if style in doc.styles else "Standard"
    dim.dxf.dimtxt = text_height              # text height, drawing units
    dim.dxf.dimasz = text_height * 0.8        # arrow size
    dim.dxf.dimexe = text_height * 0.4        # extension beyond the line
    dim.dxf.dimexo = text_height * 0.3        # offset from the geometry
    dim.dxf.dimgap = text_height * 0.25
    dim.dxf.dimdec = 0                        # whole metres: this is an
    dim.dxf.dimlfac = 1.0                     # extent, not a setting-out
    dim.dxf.dimpost = "<> m"
    return DIM_STYLE


def add_extent_dimensions(doc, layout, centre_x, centre_y, width_m,
                          height_m, layer):
    """Dimension the extent rectangle: one horizontal, one vertical.

    Placed outside the crop line so they never sit on the drawing, and
    rendered so the DXF carries the numbers a reviewer would otherwise
    have to measure.
    """
    text_height = max(2.0, min(width_m, height_m) * 0.012)
    ensure_dim_style(doc, text_height)
    half_w, half_h = width_m / 2.0, height_m / 2.0
    west, east = centre_x - half_w, centre_x + half_w
    south, north = centre_y - half_h, centre_y + half_h
    offset = text_height * 2.5

    out = []
    horizontal = layout.add_linear_dim(
        base=(centre_x, south - offset), p1=(west, south), p2=(east, south),
        dimstyle=DIM_STYLE, dxfattribs={"layer": layer})
    horizontal.render()
    out.append(horizontal)
    vertical = layout.add_linear_dim(
        base=(west - offset, centre_y), p1=(west, south), p2=(west, north),
        angle=90.0, dimstyle=DIM_STYLE, dxfattribs={"layer": layer})
    vertical.render()
    out.append(vertical)
    return out


def ensure_oneway_arrow(doc) -> str:
    """Define the direction-of-travel arrow once per document.

    Unit length along +X about the origin, so the insertion point sits on
    the centreline and the block's rotation is the road's bearing. A shaft
    and an open head rather than a filled triangle: a solid at plot scale
    fills in to a blob on a 4 m arrow, and an open head still reads at 1:5000.
    """
    if ONEWAY_ARROW in doc.blocks:
        return ONEWAY_ARROW
    blk = doc.blocks.new(name=ONEWAY_ARROW)
    blk.add_line((-0.5, 0), (0.5, 0), dxfattribs={"layer": "0"})
    blk.add_line((0.5, 0), (0.25, 0.18), dxfattribs={"layer": "0"})
    blk.add_line((0.5, 0), (0.25, -0.18), dxfattribs={"layer": "0"})
    return ONEWAY_ARROW


def add_oneway_arrow(doc, layout, x: float, y: float, size: float,
                     rotation: float, layer: str):
    """Place a direction arrow at (x, y), `size` being its length in drawing
    units and `rotation` the direction of travel in degrees."""
    ensure_oneway_arrow(doc)
    return layout.add_blockref(ONEWAY_ARROW, insert=(x, y), dxfattribs={
        "layer": layer, "xscale": size, "yscale": size, "zscale": size,
        "rotation": rotation})


# ---------------------------------------------------------------- layers
# The one layer table. It lived in db2dxf.py and, in a reduced form, in
# gis2cad.py — which is how a survey centreline came to draw Continuous in
# the import and CENTER in its own re-issue, and why gis2cad's drawings
# were missing 27 layers db2dxf defines. topo2cad.py and osm2cad.py keep
# their own (key, colour, weight) lists because they name layers through
# LAYERS keys, and a test compares the three sets rather than trusting
# anyone to remember.
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
    # Drawn heavier than the centreline it is offset from: the bank is the
    # edge a setback is measured from, the centreline only says where the
    # water runs.
    "C-HYDR-BANK": (5, 25),      # the two banks of a river or canal
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
    "C-PROP-CORN": (1, 25),      # boundary corner marks and their labels
    "C-PROP-SETB": (2, 25),
}

# Linetype per layer, where it is not Continuous. NCS convention: a
# centreline is never mistaken for the edge of pavement beside it, a crop
# line is never mistaken for a fence, a tunnel is under the ground the plan
# describes, and the two empty site-plan layers are ready to draw on.
LAYER_LINETYPE = {
    "C-ROAD-CNTR": "CENTER",
    "C-ANNO-EXTN": "DASHED",
    "C-ROAD-TUNL": "HIDDEN",
    "C-PROP-LINE": "PHANTOM",
    "C-PROP-SETB": "DASHED",
    "C-ROAD-ROWY": "PHANTOM",
}

# The dash pattern is in drawing units — metres here — so without a scale
# CENTER is sub-millimetre on paper and reads as continuous.
LTSCALE = 5.0


def apply_layer_table(doc):
    """Define every layer, its colour, weight and linetype. Returns names."""
    for name, (color, lw) in LAYER_STYLE.items():
        layer = (doc.layers.get(name) if name in doc.layers
                 else doc.layers.add(name, color=color))
        layer.dxf.color = color
        layer.dxf.lineweight = lw
    for name, linetype in LAYER_LINETYPE.items():
        if name in doc.layers:
            doc.layers.get(name).dxf.linetype = linetype
    doc.header["$LTSCALE"] = LTSCALE
    return list(LAYER_STYLE)


# How a corner label sits beside its mark, in metres. Small: a parcel
# corner is read at plan scale, not from across the sheet, and a 3 m offset
# (what a landmark label uses) puts A1 inside the neighbouring plot.
CORNER_LABEL_DX = 1.5
CORNER_LABEL_HEIGHT = 2.2


def add_corner_marks(doc, layout, parcels, layer="C-PROP-CORN",
                     style="EN_STYLE"):
    """Mark and label every boundary corner; return the setting-out rows.

    `parcels` is [(shapely polygon, name)]. Both writers that can hold a
    supplied parcel — gis2cad.py drawing the import and db2dxf.py drawing
    the project it joined — call this, so the marks, the labels and the
    table cannot disagree between a drawing and its re-issue.
    """
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import stage_db

    from ezdxf.enums import MTextEntityAlignment

    rows = []
    for index, (geom, name) in enumerate(parcels):
        table = stage_db.corner_table(geom, index, name)
        rows.extend(table)
        for row in table:
            x, y = row["easting"], row["northing"]
            add_symbol(doc, layout, x, y, symbol_size(layer), layer)
            label = layout.add_mtext(row["corner"], dxfattribs={
                "layer": layer, "char_height": CORNER_LABEL_HEIGHT,
                "style": style if style in doc.styles else "Standard"})
            label.set_location(
                (x + CORNER_LABEL_DX, y),
                attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
            # The same background mask every other label carries: a corner
            # label lands on the boundary line by definition.
            label.set_bg_color("canvas", scale=1.1)
    return rows
