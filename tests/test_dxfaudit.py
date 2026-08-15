"""Tests for the source-coverage audit (scripts/dxfaudit.py).

Only the offline half is exercised here: counting what a DXF contains, and
resolving an extent from the staging database. `source_counts()` hits
Overpass by design and is left to the opt-in network path.

The reason this tool exists is worth restating: dxfdiff.py compares the two
drawing routes and reported IDENTICAL while both of them lost building
courtyards, and again while both skipped 69 Microsoft footprints per site.
Two implementations of one mistake look like agreement, so a check that only
compares them can never see it.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

ezdxf = pytest.importorskip("ezdxf")

import dxfaudit  # noqa: E402


def _drawing(tmp_path, buildings=0, anno=0, pois=0, pre_block=False):
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for i in range(buildings):
        msp.add_lwpolyline([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)],
                           close=True,
                           dxfattribs={"layer": dxfaudit.BUILDING_LAYER})
    for i in range(anno):
        msp.add_mtext(f"B{i:03d}", dxfattribs={"layer": "C-ANNO-TEXT"})
    for i in range(pois):
        if pre_block:
            msp.add_circle((i, 0), radius=2,
                           dxfattribs={"layer": "C-ANNO-SYMB"})
        else:
            blk = doc.blocks.new(name=f"S{i}")
            blk.add_circle((0, 0), radius=1)
            msp.add_blockref(f"S{i}", insert=(i, 0),
                             dxfattribs={"layer": "C-ANNO-SYMB"})
    out = tmp_path / "d.dxf"
    doc.saveas(out)
    return out


def test_counts_only_the_layers_that_matter(tmp_path):
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1, 0), (1, 1)], close=True,
                       dxfattribs={"layer": dxfaudit.BUILDING_LAYER})
    # a road and a contour must not be counted as buildings
    msp.add_lwpolyline([(0, 0), (5, 5)], dxfattribs={"layer": "C-ROAD-CNTR"})
    msp.add_lwpolyline([(0, 0), (5, 5)], dxfattribs={"layer": "C-TOPO-MAJR"})
    # a title-block label must not be counted as annotation
    msp.add_mtext("x", dxfattribs={"layer": "C-ANNO-TTLB-TEXT"})
    msp.add_mtext("y", dxfattribs={"layer": "C-ANNO-TEXT-TH"})
    out = tmp_path / "d.dxf"
    doc.saveas(out)
    got = dxfaudit.drawing_counts(out)
    assert got["building_polylines"] == 1
    assert got["annotation"] == 1


def test_counts_courtyard_rings_as_outlines(tmp_path):
    """An inner ring is its own closed polyline on the building layer, so
    the audit expects one outline per ring — which is what makes a dropped
    courtyard show as a shortfall."""
    out = _drawing(tmp_path, buildings=4)
    assert dxfaudit.drawing_counts(out)["building_polylines"] == 4


def test_poi_symbols_counted_as_blocks_or_bare_circles(tmp_path):
    """Landmark symbols became blocks partway through; an older drawing has
    loose circles and must still audit."""
    assert dxfaudit.drawing_counts(
        _drawing(tmp_path, pois=3))["poi_symbols"] == 3
    assert dxfaudit.drawing_counts(
        _drawing(tmp_path, pois=3, pre_block=True))["poi_symbols"] == 3


def _db(tmp_path, projects):
    path = tmp_path / "s.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT,"
                 " lat REAL, lon REAL, width_m REAL, height_m REAL,"
                 " srid INTEGER)")
    conn.executemany("INSERT INTO projects VALUES (?,?,?,?,?,?,?)", projects)
    conn.commit()
    conn.close()
    return str(path)


def test_extent_comes_from_the_staged_project(tmp_path):
    db = _db(tmp_path, [(1, "site-a", 13.746, 100.534, 1000, 750, 32647)])
    ext = dxfaudit.project_extent(db, None)
    assert (ext["lat"], ext["lon"]) == (13.746, 100.534)
    assert (ext["width"], ext["height"]) == (1000, 750)
    assert ext["name"] == "site-a"


def test_several_projects_require_choosing_one(tmp_path):
    db = _db(tmp_path, [(1, "a", 13.7, 100.5, 100, 100, 32647),
                        (2, "b", 15.8, 104.4, 100, 100, 32648)])
    with pytest.raises(SystemExit, match="pick one"):
        dxfaudit.project_extent(db, None)
    assert dxfaudit.project_extent(db, 2)["name"] == "b"
    assert dxfaudit.project_extent(db, "a")["name"] == "a"


def test_unknown_project_lists_what_is_there(tmp_path):
    db = _db(tmp_path, [(1, "only-one", 13.7, 100.5, 100, 100, 32647)])
    with pytest.raises(SystemExit, match="only-one"):
        dxfaudit.project_extent(db, "typo")


def test_empty_database_is_an_error(tmp_path):
    with pytest.raises(SystemExit, match="no projects"):
        dxfaudit.project_extent(_db(tmp_path, []), None)


def test_missing_dxf_exits_nonzero(tmp_path):
    assert dxfaudit.main([str(tmp_path / "nope.dxf"), "--lat", "1",
                          "--lon", "1", "--width", "1", "--height", "1"]) == 1


def test_no_extent_given_exits_nonzero(tmp_path):
    out = _drawing(tmp_path)
    assert dxfaudit.main([str(out)]) == 1
