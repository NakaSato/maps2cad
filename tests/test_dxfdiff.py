"""The parity gate itself (scripts/dxfdiff.py).

Two ways reach a drawing — topo2cad.py draws during extraction, db2dxf.py
draws from the staging layer — and this is what proves they agree. What it
cannot see, nobody sees: it reported IDENTICAL while both routes dropped
building courtyards, and again while both skipped 69 ML footprints.
"""

import importlib.util as iu
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

ezdxf = pytest.importorskip("ezdxf")


def load(name):
    spec = iu.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dxfdiff = load("dxfdiff")


def _doc(build):
    doc = ezdxf.new("R2010", setup=["linetypes"])
    doc.layers.add("C-BLDG-OUTL", color=4)
    build(doc.modelspace())
    return doc


def _geom(doc, tmp_path, name):
    path = tmp_path / name
    doc.saveas(path)
    return dxfdiff.survey(str(path))[5]


def test_the_same_drawing_twice_is_identical(tmp_path):
    def build(msp):
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 5)], close=True,
                           dxfattribs={"layer": "C-BLDG-OUTL"})

    a = _geom(_doc(build), tmp_path, "a.dxf")
    b = _geom(_doc(build), tmp_path, "b.dxf")
    assert a == b
    assert not ((a - b) + (b - a))


def test_a_footprint_in_the_wrong_place_is_caught(tmp_path):
    """The case the counts cannot see: same entity, same layer, same vertex
    count, drawn somewhere else. Before this, two routes could disagree by
    fifty metres and every check passed."""
    def here(msp):
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 5)], close=True,
                           dxfattribs={"layer": "C-BLDG-OUTL"})

    def there(msp):
        msp.add_lwpolyline([(50, 0), (60, 0), (60, 5)], close=True,
                           dxfattribs={"layer": "C-BLDG-OUTL"})

    a = _geom(_doc(here), tmp_path, "a.dxf")
    b = _geom(_doc(there), tmp_path, "b.dxf")
    assert (a - b) + (b - a), "a displaced footprint went unnoticed"


def test_a_closing_vertex_is_a_difference(tmp_path):
    """The ring cleanup changed every polygon in four writers at once. If
    one of them had been missed, this is what says so."""
    def clean(msp):
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 5)], close=True,
                           dxfattribs={"layer": "C-BLDG-OUTL"})

    def doubled(msp):
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 5), (0, 0)], close=True,
                           dxfattribs={"layer": "C-BLDG-OUTL"})

    a = _geom(_doc(clean), tmp_path, "a.dxf")
    b = _geom(_doc(doubled), tmp_path, "b.dxf")
    assert (a - b) + (b - a)


def test_a_symbol_at_the_right_point_and_the_wrong_size_is_caught(tmp_path):
    """Block scale was explicitly outside what this tool compared, so a
    tree that came back pylon-sized was a difference nobody staged and
    nobody could see."""
    def block(doc):
        blk = doc.blocks.new("MARK")
        blk.add_circle((0, 0), 1.0)

    def make(scale):
        doc = ezdxf.new("R2010", setup=["linetypes"])
        block(doc)
        doc.modelspace().add_blockref("MARK", (5, 5),
                                      dxfattribs={"xscale": scale,
                                                  "yscale": scale})
        return doc

    a = _geom(make(1.0), tmp_path, "a.dxf")
    b = _geom(make(3.0), tmp_path, "b.dxf")
    assert (a - b) + (b - a), "a resized symbol went unnoticed"


def test_an_unknown_entity_type_is_not_silently_exempt():
    """A type nothing draws yet still gets compared on what every entity
    has, so adding one does not quietly opt it out of the check."""
    doc = ezdxf.new("R2010", setup=["linetypes"])
    ray = doc.modelspace().add_ray((0, 0), (1, 1))
    assert dxfdiff._shape(ray) is not None
