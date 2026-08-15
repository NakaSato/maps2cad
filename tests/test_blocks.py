"""Tests for the shared CAD blocks (scripts/blocks.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

ezdxf = pytest.importorskip("ezdxf")

import blocks  # noqa: E402


def test_block_is_defined_once_per_document():
    """Both CAD writers call this; a second call must reuse the definition
    rather than raise or duplicate it."""
    doc = ezdxf.new("R2010")
    a = blocks.ensure_north_arrow(doc)
    b = blocks.ensure_north_arrow(doc)
    assert a == b == blocks.NORTH_ARROW
    assert sum(1 for blk in doc.blocks
               if blk.name == blocks.NORTH_ARROW) == 1


def test_block_geometry_sits_on_layer_zero():
    """Layer 0 is the convention that makes block content inherit the
    INSERT's layer, so the arrow follows whatever layer it is placed on."""
    doc = ezdxf.new("R2010")
    blocks.ensure_north_arrow(doc)
    blk = doc.blocks.get(blocks.NORTH_ARROW)
    assert {e.dxf.layer for e in blk} == {"0"}
    assert {e.dxftype() for e in blk} == {"CIRCLE", "SOLID", "MTEXT"}


def test_block_is_unit_sized_so_one_definition_serves_every_extent():
    doc = ezdxf.new("R2010")
    blocks.ensure_north_arrow(doc)
    circle = [e for e in doc.blocks.get(blocks.NORTH_ARROW)
              if e.dxftype() == "CIRCLE"][0]
    assert circle.dxf.radius == pytest.approx(1.0)
    assert circle.dxf.center.x == pytest.approx(0.0)
    assert circle.dxf.center.y == pytest.approx(0.0)


def test_insert_carries_position_scale_and_layer():
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    blocks.add_north_arrow(doc, msp, 435000.0, 1750000.0, 8.2, "C-ANNO-NORT")
    ins = [e for e in msp if e.dxftype() == "INSERT"][0]
    assert ins.dxf.name == blocks.NORTH_ARROW
    assert ins.dxf.insert.x == pytest.approx(435000.0)
    assert ins.dxf.insert.y == pytest.approx(1750000.0)
    assert ins.dxf.xscale == pytest.approx(8.2)
    assert ins.dxf.yscale == pytest.approx(8.2)
    assert ins.dxf.layer == "C-ANNO-NORT"


def test_one_pick_moves_the_whole_arrow():
    """The reason it is a block at all: three loose entities means a
    drafter window-selects and hopes; one INSERT is one pick."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    blocks.add_north_arrow(doc, msp, 0, 0, 5.0, "C-ANNO-NORT")
    assert len(list(msp)) == 1


def test_missing_text_style_falls_back_rather_than_raising():
    """add_sheet() and the writers may run on a document that has not
    registered EN_STYLE yet."""
    doc = ezdxf.new("R2010")
    blocks.ensure_north_arrow(doc, style="NOT_REGISTERED")
    label = [e for e in doc.blocks.get(blocks.NORTH_ARROW)
             if e.dxftype() == "MTEXT"][0]
    assert label.dxf.style == "Standard"
