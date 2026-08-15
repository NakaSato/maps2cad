"""Tests for the CAD export path (scripts/topo2cad.py).

Covers the pure geometry and CRS helpers that topo2cad.py owns and
mapposter.py imports. The DEM/CAD stack (rasterio, skimage, ezdxf) is
imported inside main(), so these run without it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from topo2cad import (  # noqa: E402
    _clip_seg,
    bbox_around,
    best_name,
    clip_runs,
    is_thai,
    names_by_lang,
    offset_along_normal,
    quadkey,
    utm_epsg_for,
    utm_transformer,
)


# --------------------------------------------------------------- UTM zoning
@pytest.mark.parametrize("lat,lon,epsg", [
    (14.8164876968956, 100.511644184589, 32647),   # Lopburi — zone 47N
    (15.83384548, 104.39445555, 32648),            # Yasothon — zone 48N
    (13.7460, 100.5340, 32647),                    # Bangkok — 47N
    (-33.87, 151.21, 32756),                       # southern hemisphere
    (51.5, -0.13, 32630),
])
def test_utm_epsg_for(lat, lon, epsg):
    assert utm_epsg_for(lat, lon) == epsg


def test_zone_boundary_at_102E():
    """The 102°E boundary is the one that bites in Thailand: sites either
    side must land in different zones."""
    assert utm_epsg_for(15.0, 101.999) == 32647
    assert utm_epsg_for(15.0, 102.001) == 32648


def test_utm_transformer_label_and_projection():
    to_utm, epsg, label = utm_transformer(15.83384548, 104.39445555)
    assert (epsg, label) == (32648, "48N")
    x, y = to_utm.transform(104.39445555, 15.83384548)
    # A valid UTM easting sits within 100k..900k; the old hardcoded 47N put
    # this site at 1,078,368 m, which is what the zone fix prevents.
    assert 100_000 < x < 900_000
    assert x == pytest.approx(435157.54, abs=0.1)
    assert y == pytest.approx(1750649.93, abs=0.1)


def test_wrong_zone_would_distort_scale():
    """Guards the reason the fix exists, not just its output."""
    import math

    from pyproj import Transformer

    lat, lon = 15.83384548, 104.39445555
    lon_east = lon + 0.01
    correct, _, _ = utm_transformer(lat, lon)
    wrong = Transformer.from_crs("EPSG:4326", "EPSG:32647", always_xy=True)

    def span(t):
        x1, y1 = t.transform(lon, lat)
        x2, y2 = t.transform(lon_east, lat)
        return math.hypot(x2 - x1, y2 - y1)

    # Same ground distance, measured in each projection
    assert abs(span(correct) / span(wrong) - 1) > 0.003   # >0.3% apart
    assert span(correct) < span(wrong)


def test_southern_hemisphere_label():
    _, epsg, label = utm_transformer(-33.87, 151.21)
    assert (epsg, label) == (32756, "56S")


# ------------------------------------------------------------------- bbox
def test_bbox_around_radius_is_centred():
    lat, lon = 15.83384548, 104.39445555
    s, w, n, e = bbox_around(lat, lon, 500)
    assert (s + n) / 2 == pytest.approx(lat)
    assert (w + e) / 2 == pytest.approx(lon)
    assert s < lat < n and w < lon < e


def test_bbox_around_width_height_override_radius():
    lat, lon = 15.83384548, 104.39445555
    s, w, n, e = bbox_around(lat, lon, 500, width_m=500, height_m=400)
    # Height 400 m -> +/-200 m in latitude; width 500 m -> +/-250 m longitude
    assert (n - s) * 111320.0 == pytest.approx(400, rel=1e-6)
    import math
    east_m = (e - w) * 111320.0 * math.cos(math.radians(lat))
    assert east_m == pytest.approx(500, rel=1e-6)


# ------------------------------------------------------------- segment clip
def test_clip_seg_inside_outside_and_crossing():
    box = (0, 0, 10, 10)
    assert _clip_seg(2, 2, 8, 8, *box) == ((2, 2), (8, 8))      # fully inside
    assert _clip_seg(-5, 5, -1, 5, *box) is None                # fully outside
    clipped = _clip_seg(-5, 5, 5, 5, *box)                      # crosses edge
    assert clipped is not None
    (x1, _), (x2, _) = clipped
    assert x1 == pytest.approx(0) and x2 == pytest.approx(5)


def test_clip_runs_splits_at_boundary():
    """A road that leaves the box and re-enters elsewhere yields two runs,
    so it is not drawn straight across the gap."""
    # Out through the right edge at y=1, back in through the right edge at
    # y=9 — two separate entries into the box.
    pts = [(1, 1), (9, 1), (20, 1), (20, 9), (9, 9), (1, 9)]
    runs = clip_runs(pts, 0, 0, 10, 10, margin=0)
    assert len(runs) == 2
    for run in runs:
        for x, y in run:
            assert -0.001 <= x <= 10.001 and -0.001 <= y <= 10.001


def test_clip_runs_treats_touch_and_return_as_continuous():
    """Leaving and re-entering at the same point is one unbroken run — the
    clipped path really is continuous there."""
    pts = [(1, 1), (5, 5), (20, 20), (6, 6), (2, 2)]
    runs = clip_runs(pts, 0, 0, 10, 10, margin=0)
    assert len(runs) == 1


def test_clip_runs_keeps_wholly_inside_line_intact():
    pts = [(1, 1), (2, 2), (3, 3)]
    runs = clip_runs(pts, 0, 0, 10, 10, margin=0)
    assert len(runs) == 1
    assert runs[0][0] == (1, 1) and runs[0][-1] == (3, 3)


# --------------------------------------------------------------- misc utils
def test_quadkey_is_stable_and_zoom_9():
    key = quadkey(15.83384548, 104.39445555)
    assert len(key) == 9
    assert set(key) <= {"0", "1", "2", "3"}
    # Same tile for a nearby point, different for a far one
    assert quadkey(15.834, 104.395) == key
    assert quadkey(14.8165, 100.5116) != key


def test_best_name_prefers_local_then_english():
    assert best_name({"name": "ถนนอรุณประเสริฐ"}) == "ถนนอรุณประเสริฐ"
    assert best_name({"name:en": "Arun Prasert Rd"}) == "Arun Prasert Rd"
    assert best_name({"name:th": "ถนน"}) == "ถนน"
    assert best_name({"highway": "residential"}) is None


def test_best_name_takes_thai_over_english():
    """The deliverable is a Thai submission, so name:th outranks name:en
    even when both are tagged."""
    assert best_name({"name:th": "ถนนอรุณประเสริฐ",
                      "name:en": "Arun Prasoet Road"}) == "ถนนอรุณประเสริฐ"
    # ...and a Latin plain `name` must not beat an explicit name:th
    assert best_name({"name": "7-Eleven",
                      "name:th": "เซเว่นอีเลฟเว่น"}) == "เซเว่นอีเลฟเว่น"


# --------------------------------------------------------- script detection
@pytest.mark.parametrize("text,expected", [
    ("ถนนอรุณประเสริฐ", True),
    ("ทล.202", True),            # Thai prefix on a Latin number
    ("Arun Prasoet Road", False),
    ("7-Eleven", False),
    ("B042", False),
    ("202", False),
    ("", False),
    (None, False),
])
def test_is_thai(text, expected):
    assert is_thai(text) is expected


def test_names_by_lang_files_plain_name_by_its_own_script():
    """A business that trades under an English name puts Latin in `name`,
    so `name` alone cannot be assumed Thai."""
    assert names_by_lang({"name": "ถนนอรุณประเสริฐ"}) == \
        ("ถนนอรุณประเสริฐ", None)
    assert names_by_lang({"name": "7-Eleven"}) == (None, "7-Eleven")


def test_names_by_lang_keeps_both_when_both_tagged():
    assert names_by_lang({"name": "ถนนอรุณประเสริฐ",
                          "name:th": "ถนนอรุณประเสริฐ",
                          "name:en": "Arun Prasoet Road"}) == \
        ("ถนนอรุณประเสริฐ", "Arun Prasoet Road")


def test_names_by_lang_explicit_tag_wins_over_plain_name():
    """`name` must not overwrite a slot an explicit tag already filled."""
    assert names_by_lang({"name": "Seven Eleven",
                          "name:en": "7-Eleven"}) == (None, "7-Eleven")


def test_names_by_lang_unnamed_is_empty():
    assert names_by_lang({"building": "yes"}) == (None, None)


# ------------------------------------------------------- label stacking
def test_offset_along_normal_is_plain_y_when_upright():
    x, y = offset_along_normal(100.0, 200.0, 0.0, 6.5)
    assert x == pytest.approx(100.0)
    assert y == pytest.approx(206.5)


def test_offset_along_normal_stays_square_to_a_rotated_label():
    """Road labels rotate along the centreline; a -Y nudge would drift off
    the road, so the offset has to follow the label's own normal."""
    import math

    ang = 30.0
    x, y = offset_along_normal(0.0, 0.0, ang, 10.0)
    # the displacement is perpendicular to the baseline...
    along = x * math.cos(math.radians(ang)) + y * math.sin(math.radians(ang))
    assert along == pytest.approx(0.0, abs=1e-9)
    # ...and exactly the requested distance from the anchor
    assert math.hypot(x, y) == pytest.approx(10.0)


# --------------------------------------------------- GIS import (gis2cad.py)
def test_gis2cad_derives_utm_zone_from_the_data():
    """Your own GIS data should not require you to pick a CRS."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gis2cad", Path(__file__).resolve().parent.parent
        / "scripts" / "gis2cad.py")
    gis2cad = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gis2cad)

    assert gis2cad.utm_epsg_for(12.526, 102.15982) == 32648
    assert gis2cad.utm_epsg_for(14.8165, 100.5116) == 32647
    # Label field detection falls back through common attribute names
    assert gis2cad.pick_name_field(["id", "PLOT_NAME"], None) == "PLOT_NAME"
    assert gis2cad.pick_name_field(["id", "name"], None) == "name"
    assert gis2cad.pick_name_field(["id", "area"], None) is None
    assert gis2cad.pick_name_field(["id", "name"], "id") == "id"
