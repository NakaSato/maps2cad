"""Tests for the OSM-vs-ML quality check (scripts/gisqa.py).

All offline: the comparison takes polygons in metres, so the fixtures are
plain boxes and the thresholds mean what they say.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gisqa  # noqa: E402


def box(x0, y0, x1, y1):
    from shapely.geometry import box as _box

    return _box(x0, y0, x1, y1)


def test_iou_of_identical_and_disjoint_shapes():
    a = box(0, 0, 10, 10)
    assert gisqa.iou(a, a) == pytest.approx(1.0)
    assert gisqa.iou(a, box(100, 100, 110, 110)) == 0.0
    # half overlap: 50 shared of 150 combined
    assert gisqa.iou(a, box(5, 0, 15, 10)) == pytest.approx(50 / 150)


def test_iou_is_safe_on_nothing():
    assert gisqa.iou(None, box(0, 0, 1, 1)) == 0.0
    assert gisqa.iou(box(0, 0, 1, 1), None) == 0.0


def test_agreeing_sources_produce_no_findings():
    osm = [("way/1", box(0, 0, 10, 10), True)]
    findings, ml_only = gisqa.compare(osm, [box(0, 0, 10, 10)])
    assert findings == [] and ml_only == 0


def test_a_building_ml_does_not_see_is_flagged():
    osm = [("way/1", box(0, 0, 10, 10), True)]
    findings, _ = gisqa.compare(osm, [box(500, 500, 510, 510)])
    assert [f["issue"] for f in findings] == ["no_ml_support"]


def test_a_mall_split_into_roof_pieces_is_not_a_disagreement():
    """One OSM polygon against five ML pieces of the same roof. Scoring it
    against the largest single piece reported every mall in Pathum Wan as
    wrong, which is the check crying wolf."""
    osm = [("way/1", box(0, 0, 100, 20), True)]
    pieces = [box(i * 20, 0, i * 20 + 20, 20) for i in range(5)]
    findings, ml_only = gisqa.compare(osm, pieces)
    assert findings == []
    assert ml_only == 0            # every piece counts as matched


def test_a_drifted_outline_is_flagged_with_its_score():
    osm = [("way/1", box(0, 0, 10, 10), True)]
    findings, _ = gisqa.compare(osm, [box(7, 0, 17, 10)])
    assert len(findings) == 1
    assert findings[0]["issue"] == "poor_overlap"
    assert 0.0 < float(findings[0]["iou"]) < gisqa.POOR_OVERLAP


def test_slivers_are_flagged_and_not_compared():
    osm = [("way/1", box(0, 0, 1, 1), True)]        # 1 m², under the floor
    findings, _ = gisqa.compare(osm, [])
    assert [f["issue"] for f in findings] == ["sliver"]


def test_a_self_intersecting_ring_is_reported_once():
    """Every writer here repairs these before drawing; the drafter should
    still know the source data is broken."""
    osm = [("way/1", box(0, 0, 10, 10), False)]
    findings, _ = gisqa.compare(osm, [box(0, 0, 10, 10)])
    assert [f["issue"] for f in findings] == ["self_intersect"]


def test_building_and_building_part_read_as_near_duplicates():
    """Upstream data rather than an error, but it doubles the outline in
    CAD, so it is worth naming."""
    osm = [("way/1", box(0, 0, 10, 10), True),
           ("way/2", box(0, 0, 10, 9.5), True)]
    findings, _ = gisqa.compare(osm, [box(0, 0, 10, 10)])
    dupes = [f for f in findings if f["issue"] == "near_duplicate"]
    assert len(dupes) == 1 and "way/2" in dupes[0]["detail"]


def test_ml_only_counts_what_osm_has_never_mapped():
    osm = [("way/1", box(0, 0, 10, 10), True)]
    ml = [box(0, 0, 10, 10), box(50, 50, 60, 60), box(80, 80, 90, 90)]
    _findings, ml_only = gisqa.compare(osm, ml)
    assert ml_only == 2


def test_as_polygon_repairs_or_refuses():
    assert gisqa.as_polygon([(0, 0), (1, 0)]) is None          # not an area
    bow = [(0, 0), (4, 0), (4, 4), (0, 0), (6, 0), (10, 0), (10, 4), (6, 0)]
    assert gisqa.as_polygon(bow) is not None                   # repaired
