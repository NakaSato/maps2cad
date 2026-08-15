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
