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
                           dxfattribs={"layer": dxfaudit.BUILDING_LAYERS[0]})
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
                       dxfattribs={"layer": dxfaudit.BUILDING_LAYERS[0]})
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


def test_unnamed_buildings_count_as_buildings(tmp_path):
    """A footprint OSM has no name for draws on C-BLDG-UNNM so a drafter can
    plot the named structures alone. Counting only C-BLDG-OUTL reported a
    SHORTFALL against a drawing holding every building there was — 23 of 77
    at Pathum Wan — which is how an audit teaches people to ignore it."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1, 0), (1, 1)], close=True,
                       dxfattribs={"layer": "C-BLDG-OUTL"})
    msp.add_lwpolyline([(2, 0), (3, 0), (3, 1)], close=True,
                       dxfattribs={"layer": "C-BLDG-UNNM"})
    out = tmp_path / "u.dxf"
    doc.saveas(out)
    assert dxfaudit.drawing_counts(out)["building_polylines"] == 2


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


# ------------------------------------------- roads and context linework
def _way(tags, pts, wid=1):
    return {"type": "way", "id": wid, "tags": tags,
            "geometry": [{"lon": x, "lat": y} for x, y in pts]}


BOX = (14.0, 100.0, 14.01, 100.01)      # s, w, n, e


def test_roads_are_counted_at_all():
    """They were not. A drawing with every road deleted audited as
    "COMPLETE — the drawing carries everything the source holds": 15
    entities removed at Lopburi and not one check moved, 681 at Pathum Wan
    and only the one-way arrows noticed. Roads are the largest category in
    the drawing and nothing looked at them."""
    got = dxfaudit.count_elements(
        [_way({"highway": "residential"}, [(100.002, 14.002),
                                           (100.006, 14.006)], 1),
         _way({"highway": "service"}, [(100.003, 14.003),
                                       (100.007, 14.004)], 2)], box=BOX)
    assert got["roads"] == 2


def test_a_plaza_is_not_a_centreline():
    """highway=pedestrian + area=yes on a closed ring leaves the road
    bucket for C-ROAD-PLAZ and draws closed. Counting it as a centreline is
    what made this check's first run report a 3-way shortfall against a
    drawing that was complete — the tool's assumption was wrong, not the
    drawing, which is the order to check them in."""
    ring = [(100.002, 14.002), (100.004, 14.002), (100.004, 14.004),
            (100.002, 14.002)]
    got = dxfaudit.count_elements(
        [_way({"highway": "pedestrian", "area": "yes"}, ring)], box=BOX)
    assert got["roads"] == 0
    assert got["context"]["plaza"] == 1


def test_an_open_pedestrian_way_is_still_a_road():
    got = dxfaudit.count_elements(
        [_way({"highway": "pedestrian"},
              [(100.002, 14.002), (100.006, 14.006)])], box=BOX)
    assert got["roads"] == 1


def test_context_linework_is_counted_by_category():
    got = dxfaudit.count_elements([
        _way({"waterway": "canal"}, [(100.002, 14.002), (100.006, 14.006)], 1),
        _way({"railway": "rail"}, [(100.003, 14.003), (100.007, 14.004)], 2),
        _way({"landuse": "industrial"},
             [(100.004, 14.004), (100.008, 14.005)], 3),
        _way({"barrier": "wall"}, [(100.005, 14.005), (100.009, 14.006)], 4),
    ], box=BOX)
    assert got["context_total"] == 4
    assert set(got["context"]) == {"water", "rail", "land", "barrier"}


def test_a_way_outside_the_extent_is_not_counted_as_missing():
    """clip_runs() cuts at the boundary plus a margin, so a way well
    outside is correctly absent from the drawing. Reporting it as a
    shortfall would be a false alarm — a worse defect here than the silent
    loss this looks for, because an audit that cries wolf is read once and
    then ignored."""
    far = _way({"highway": "residential"}, [(101.5, 15.5), (101.6, 15.6)])
    assert dxfaudit.count_elements([far], box=BOX)["roads"] == 0


def test_a_building_relation_also_tagged_landuse_does_not_crash():
    """The context branch belongs to the *way* arm. Attached to the
    relation arm it referenced a `pts` that does not exist there — dead for
    every ordinary relation and a NameError for one carrying both tags."""
    rel = {"type": "relation", "id": 9,
           "tags": {"building": "yes", "landuse": "retail"},
           "members": [{"role": "outer", "geometry": [
               {"lon": 100.002, "lat": 14.002},
               {"lon": 100.004, "lat": 14.002},
               {"lon": 100.004, "lat": 14.004},
               {"lon": 100.002, "lat": 14.002}]}]}
    got = dxfaudit.count_elements([rel], box=BOX)
    assert got["osm_buildings"] == 1


def test_the_drawing_side_counts_centrelines_not_kerbs(tmp_path):
    """Edges of pavement are offsets of the centrelines and are trimmed at
    the junctions, so one road can leave anything from nought to four edge
    lines. Counting them would be counting the drawing's own drafting."""
    ezdxf = pytest.importorskip("ezdxf")

    doc = ezdxf.new("R2010", setup=["linetypes"])
    for layer in ("C-ROAD-CNTR", "C-ROAD-PATH", "C-ROAD-EDGE",
                  "C-ROAD-EDGE", "C-ROAD-EDGE"):
        doc.layers.add(layer) if layer not in doc.layers else None
        doc.modelspace().add_lwpolyline([(0, 0), (10, 0)],
                                        dxfattribs={"layer": layer})
    path = tmp_path / "roads.dxf"
    doc.saveas(path)
    assert dxfaudit.drawing_counts(str(path))["roads"] == 2
