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
                  "C-BNDY-BARR": 2.0, "C-UTIL-LAMP": 1.5}
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
