"""Tests for the source-counting half of scripts/dxfaudit.py.

The audit exists to catch what dxfdiff cannot: both routes dropping the same
feature. That only works if its idea of "what the source holds" is right —
and independent of the drawing code, which is why the counting is restated
there from the OSM tags rather than imported from topo2cad.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dxfaudit  # noqa: E402


def ring(x0, y0, x1, y1):
    return [{"lon": x0, "lat": y0}, {"lon": x1, "lat": y0},
            {"lon": x1, "lat": y1}, {"lon": x0, "lat": y1},
            {"lon": x0, "lat": y0}]


def coords(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def test_drawable_inner_ring_inside_one_outer_counts_as_a_courtyard():
    outer = coords(0, 0, 10, 10)
    court = coords(4, 4, 6, 6)
    assert dxfaudit.drawable_inner_rings([outer], [court]) == (1, 0)


def test_inner_ring_straddling_two_outers_is_a_stray_not_a_shortfall():
    """Relation 15817178 at Pathum Wan: the polygon is self-intersecting, so
    the repair takes a bite out of the block and there is no ring to draw.
    Counting it as a missing outline would fail the audit forever on data
    that is drawn correctly."""
    left, right = coords(0, 0, 10, 10), coords(11, 0, 21, 10)
    straddling = coords(8, 4, 13, 6)
    assert dxfaudit.drawable_inner_rings([left, right], [straddling]) == (0, 1)


def test_inner_ring_touching_the_outer_boundary_is_a_stray():
    """buffer(0) dissolves a hole that reaches the shell, so no closed
    polyline comes out of it."""
    outer = coords(0, 0, 10, 10)
    touching = coords(0, 4, 5, 6)          # shares the west edge
    assert dxfaudit.drawable_inner_rings([outer], [touching]) == (0, 1)


def test_count_elements_counts_outers_and_courtyards_separately():
    elements = [{"type": "relation", "id": 1, "tags": {"building": "yes"},
                 "members": [
                     {"role": "outer", "geometry": ring(0, 0, 10, 10)},
                     {"role": "inner", "geometry": ring(4, 4, 6, 6)}]}]
    counts = dxfaudit.count_elements(elements)
    assert counts["osm_buildings"] == 1
    assert counts["inner_rings"] == 1 and counts["stray_inners"] == 0


@pytest.mark.parametrize("tags,counted", [
    ({"highway": "primary", "oneway": "yes"}, 1),
    ({"highway": "primary", "oneway": "-1"}, 1),
    ({"highway": "primary", "junction": "roundabout"}, 1),
    ({"highway": "primary"}, 0),
    ({"highway": "primary", "oneway": "no"}, 0),
    # A one-way footway is not drawn with arrows, so it is not expected
    ({"highway": "footway", "oneway": "yes"}, 0),
])
def test_count_elements_counts_one_way_carriageways(tags, counted):
    elements = [{"type": "way", "id": 1, "tags": tags,
                 "geometry": ring(0, 0, 1, 1)}]
    assert dxfaudit.count_elements(elements)["oneway_roads"] == counted


def test_count_elements_reads_an_osm_file_the_same_way(tmp_path):
    """--osm-file must produce the counts the Overpass path would."""
    src = tmp_path / "map.osm"
    src.write_text("""<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
  <node id="1" lat="0.0000" lon="0.0000"/>
  <node id="2" lat="0.0000" lon="0.0010"/>
  <node id="3" lat="0.0010" lon="0.0010"/>
  <node id="4" lat="0.0010" lon="0.0000"/>
  <node id="9" lat="0.0005" lon="0.0005">
    <tag k="amenity" v="school"/><tag k="name" v="โรงเรียนทดสอบ"/>
  </node>
  <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/>
    <nd ref="1"/><tag k="building" v="yes"/></way>
  <way id="20"><nd ref="1"/><nd ref="3"/>
    <tag k="highway" v="residential"/><tag k="oneway" v="yes"/>
    <tag k="name" v="ถนนทดสอบ"/></way>
</osm>
""", encoding="utf-8")
    counts = dxfaudit.file_counts([src])
    assert counts["osm_buildings"] == 1
    assert counts["poi_nodes"] == 1
    assert counts["oneway_roads"] == 1
    assert counts["road_names"] == 1
    assert counts["ml_added"] == 0        # the file route never adds ML
