"""Tests for the CAD export path (scripts/topo2cad.py).

Covers the pure geometry and CRS helpers that topo2cad.py owns and
mapposter.py imports. The DEM/CAD stack (rasterio, skimage, ezdxf) is
imported inside main(), so these run without it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import topo2cad  # noqa: E402
from topo2cad import (  # noqa: E402
    _clip_seg,
    bbox_around,
    best_name,
    clip_runs,
    is_thai,
    names_by_lang,
    offset_along_normal,
    poi_kind,
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


# ------------------------------------------------------------- landmarks
def test_poi_kind_identifies_the_three_landmark_keys():
    assert poi_kind({"amenity": "hospital"}) == ("amenity", "hospital")
    assert poi_kind({"tourism": "museum"}) == ("tourism", "museum")
    assert poi_kind({"historic": "monument"}) == ("historic", "monument")


def test_poi_kind_is_ordered_so_a_feature_reports_one_class():
    """A hospital that is also a tourist attraction must not be staged
    twice under two different keys."""
    assert poi_kind({"tourism": "attraction",
                     "amenity": "hospital"}) == ("amenity", "hospital")


def test_poi_kind_ignores_ordinary_features():
    """The query used to ask for every named node, which at a dense site
    returns mall floor markers, shop brands, benches and bus stops."""
    for tags in ({"building": "yes"}, {"highway": "residential"},
                 {"shop": "convenience"}, {"name": "Level 3", "level": "3"},
                 {"amenity": ""}, {}):
        assert poi_kind(tags) is None


# ------------------------------------------------- landmark curation
@pytest.mark.parametrize("tags", [
    {"amenity": "place_of_worship"},   # วัด — the primary Thai landmark
    {"amenity": "school"},
    {"amenity": "hospital"},
    {"amenity": "police"},
    {"amenity": "townhall"},
    {"amenity": "post_office"},
    {"amenity": "marketplace"},
    {"amenity": "fuel"},               # ปั๊มน้ำมัน is real wayfinding here
    {"tourism": "museum"},
    {"historic": "monument"},
])
def test_submission_keeps_civic_landmarks(tags):
    assert poi_kind(tags) is not None


@pytest.mark.parametrize("tags", [
    {"amenity": "restaurant"},
    {"amenity": "cafe"},
    {"amenity": "fast_food"},
    {"amenity": "atm"},
    {"amenity": "bureau_de_change"},
    {"amenity": "bar"},
    {"amenity": "bicycle_parking"},
    {"amenity": "photo_booth"},
    {"amenity": "car_wash"},
    {"tourism": "artwork"},
])
def test_submission_drops_commercial_clutter(tags):
    """105 of 144 landmark nodes over 770 x 410 m in Pathum Wan were food,
    drink and money. A reviewing officer locates a parcel by วัด and
    โรงเรียน, not by which cafe was trading when the survey ran."""
    assert poi_kind(tags) is None


def test_all_poi_restores_everything():
    assert poi_kind({"amenity": "restaurant"}, curated=False) == \
        ("amenity", "restaurant")
    assert poi_kind({"tourism": "artwork"}, curated=False) == \
        ("tourism", "artwork")


def test_historic_is_kept_whole():
    """historic=* is small and every value of it is worth drawing, so it is
    not enumerated — a new value must not silently vanish."""
    for value in ("monument", "memorial", "ruins", "city_gate", "stupa"):
        assert poi_kind({"historic": value}) == ("historic", value)


def test_curation_does_not_change_key_precedence():
    """A curated feature tagged on two keys still reports one class, so it
    cannot be staged twice."""
    assert poi_kind({"tourism": "museum",
                     "amenity": "school"}) == ("amenity", "school")
    # ...and an uncurated amenity does not mask a curated tourism value
    assert poi_kind({"amenity": "restaurant",
                     "tourism": "museum"}) == ("tourism", "museum")


# --------------------------------------------------------- road drafting
def test_pedestrian_ways_are_not_carriageways():
    """A 1.5 m footpath drawn with two offset kerb lines reads as a road on
    the plan, so footways go on C-ROAD-PATH as a single line."""
    from topo2cad import LAYERS, PATH_TYPES

    for kind in ("footway", "path", "cycleway", "steps", "pedestrian"):
        assert kind in PATH_TYPES
    for kind in ("motorway", "trunk", "primary", "residential", "service",
                 "track", "unclassified"):
        assert kind not in PATH_TYPES
    assert LAYERS["road_path"] == "C-ROAD-PATH"


def test_road_layers_follow_the_ncs_split():
    """Centreline, edge of pavement, path and right-of-way are separate so a
    drafter can isolate one without freezing the map."""
    from topo2cad import LAYERS

    assert LAYERS["road_centre"] == "C-ROAD-CNTR"
    assert LAYERS["road_edge"] == "C-ROAD-EDGE"
    assert LAYERS["road_path"] == "C-ROAD-PATH"
    assert LAYERS["road_row"] == "C-ROAD-ROWY"
    assert len({LAYERS["road_centre"], LAYERS["road_edge"],
                LAYERS["road_path"], LAYERS["road_row"]}) == 4


def test_every_road_class_has_a_width():
    """road_edges() falls back to 5 m for an unknown class, but the classes
    that actually carry traffic should be explicit."""
    from topo2cad import ROAD_WIDTH_M

    for kind in ("motorway", "trunk", "primary", "secondary", "tertiary",
                 "residential", "service", "unclassified"):
        assert ROAD_WIDTH_M[kind] > 0
    # a trunk road is wider than a residential street, or the plan lies
    assert ROAD_WIDTH_M["trunk"] > ROAD_WIDTH_M["residential"]
    assert ROAD_WIDTH_M["residential"] > ROAD_WIDTH_M["footway"]


# ------------------------------------------------------------ one-way roads
@pytest.mark.parametrize("tags,expected", [
    ({"oneway": "yes"}, 1),
    ({"oneway": "true"}, 1),
    ({"oneway": "1"}, 1),
    ({"oneway": "-1"}, -1),          # backwards along the way as digitised
    ({"oneway": "reverse"}, -1),
    ({"oneway": "no"}, 0),
    ({}, 0),
    ({"junction": "roundabout"}, 1),   # one-way by definition, often untagged
    ({"junction": "roundabout", "oneway": "no"}, 0),   # explicit wins
    ({"oneway": "alternating"}, 0),    # not a direction this can draw
])
def test_oneway_dir(tags, expected):
    from topo2cad import oneway_dir

    assert oneway_dir(tags) == expected


def test_classify_carries_oneway_through_to_the_writers():
    """The drawing routes read it off the road tuple; if classification
    dropped it, every arrow would silently disappear."""
    from topo2cad import classify_elements

    elements = [{"type": "way", "id": 1,
                 "tags": {"highway": "primary", "oneway": "-1"},
                 "geometry": [{"lon": 100.0, "lat": 13.0},
                              {"lon": 100.001, "lat": 13.0}]}]
    (_names, _ref, _pts, highway, fid, oneway), = \
        classify_elements(elements)["roads"]
    assert (highway, fid, oneway) == ("primary", "way/1", -1)


# ------------------------------------------------ multipolygon inner rings
def test_assign_inner_rings_goes_by_containment_not_order():
    """A relation with two outers is two buildings; the courtyard belongs to
    whichever encloses it. Attaching it to the first would punch a hole
    through the wrong building — dropping it fills in a real one."""
    from topo2cad import assign_inner_rings

    left = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    right = [(20, 0), (30, 0), (30, 10), (20, 10), (20, 0)]
    court = [(24, 4), (26, 4), (26, 6), (24, 6), (24, 4)]      # inside right
    assert assign_inner_rings([left, right], [court]) == [[], [court]]
    assert assign_inner_rings([right, left], [court]) == [[court], []]


def test_assign_inner_rings_drops_one_inside_neither():
    """A broken relation upstream: better no courtyard than one cut into an
    arbitrary building."""
    from topo2cad import assign_inner_rings

    left = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    away = [(50, 50), (52, 50), (52, 52), (50, 52), (50, 50)]
    assert assign_inner_rings([left], [away]) == [[]]


def test_multi_outer_relation_keeps_its_courtyard():
    from topo2cad import classify_elements

    def ring(x0, y0, x1, y1):
        return [{"lon": x0, "lat": y0}, {"lon": x1, "lat": y0},
                {"lon": x1, "lat": y1}, {"lon": x0, "lat": y1},
                {"lon": x0, "lat": y0}]

    elements = [{"type": "relation", "id": 7, "tags": {"building": "yes"},
                 "members": [
                     {"role": "outer", "geometry": ring(0, 0, 1, 1)},
                     {"role": "outer", "geometry": ring(2, 0, 3, 1)},
                     {"role": "inner", "geometry": ring(2.4, 0.4, 2.6, 0.6)}]}]
    buildings = classify_elements(elements)["buildings"]
    holes = {fid: len(h) for _n, (_ext, h), fid in buildings}
    assert holes == {"relation/7/0": 0, "relation/7/1": 1}


# --------------------------------------------------------- road geometry
@pytest.mark.parametrize("tags,highway,expected", [
    ({}, "residential", 6.0),                 # class default, unchanged
    ({"width": "4"}, "residential", 4.0),     # a mapper measured it
    ({"width": "4.5 m"}, "residential", 4.5),
    ({"width": "12'"}, "residential", 3.6576),  # feet, which OSM allows
    ({"lanes": "4"}, "residential", 12.0),    # 3.0 m a lane off the trunk classes
    ({"lanes": "4"}, "primary", 14.0),        # 3.5 m on a highway-standard class
    ({"width": "0.5"}, "residential", 6.0),   # mapping error, ignored
    ({"width": "wide"}, "residential", 6.0),  # unparseable, ignored
    ({"lanes": "0"}, "residential", 6.0),
    ({"width": "4", "lanes": "6"}, "residential", 4.0),   # width wins
])
def test_carriageway_width(tags, highway, expected):
    from topo2cad import carriageway_width

    assert carriageway_width(tags, highway) == pytest.approx(expected, abs=1e-3)


def test_carriageway_width_is_capped():
    """A tagging slip like lanes=99 must not draw a 300 m road."""
    from topo2cad import carriageway_width

    assert carriageway_width({"lanes": "99"}, "motorway") == 40.0


@pytest.mark.parametrize("tags,highway,layer", [
    ({}, "residential", "C-ROAD-CNTR"),
    ({"bridge": "yes"}, "residential", "C-ROAD-BRDG"),
    ({"tunnel": "yes"}, "primary", "C-ROAD-TUNL"),
    ({"bridge": "no"}, "residential", "C-ROAD-CNTR"),
    # A footbridge is still a footway: one line, no kerbs
    ({"bridge": "yes"}, "footway", "C-ROAD-PATH"),
    # Tunnel wins over bridge on the rare way tagged both
    ({"bridge": "yes", "tunnel": "yes"}, "primary", "C-ROAD-TUNL"),
])
def test_road_cad_layer(tags, highway, layer):
    from topo2cad import road_cad_layer

    assert road_cad_layer(tags, highway) == layer


def test_built_up_landuse_is_not_planting():
    """A factory estate is not a park: a reviewer reads the two
    differently, so they are separate layers."""
    from topo2cad import classify_elements

    def area(tags):
        return {"type": "way", "id": 1, "tags": tags,
                "geometry": [{"lon": 100.0, "lat": 13.0},
                             {"lon": 100.001, "lat": 13.0},
                             {"lon": 100.001, "lat": 13.001},
                             {"lon": 100.0, "lat": 13.0}]}

    for value in ("industrial", "residential", "commercial"):
        f = classify_elements([area({"landuse": value})])
        assert len(f["zoning"]) == 1 and f["green"] == []
    for value in ("grass", "forest", "farmland"):
        f = classify_elements([area({"landuse": value})])
        assert len(f["green"]) == 1 and f["zoning"] == []
    # leisure stays planting too
    assert len(classify_elements([area({"leisure": "park"})])["green"]) == 1


# ---------------------------------------------------------- all features
@pytest.mark.parametrize("tags,expected", [
    ({"amenity": "cafe", "air_conditioning": "yes", "name": "X"}, "amenity"),
    ({"shop": "bakery", "operator:en": "Y"}, "shop"),
    ({"highway": "crossing", "crossing:markings": "zebra"}, "highway"),
    ({"entrance": "yes", "wheelchair": "limited"}, "entrance"),
    # nothing primary: fall through to the first key that is not
    # bookkeeping a mapper left behind
    ({"air_conditioning": "yes", "name": "Z"}, "air_conditioning"),
    ({"name": "only a name"}, "name"),
])
def test_first_tag_names_what_a_feature_is(tags, expected):
    """Reporting a run as `operator:en x44` tells a reader nothing about
    their drawing."""
    from topo2cad import _first_tag

    assert _first_tag(tags) == expected


def test_keep_other_captures_what_the_curated_rules_drop():
    from topo2cad import classify_elements

    ring = [{"lon": 100.0, "lat": 13.0}, {"lon": 100.001, "lat": 13.0},
            {"lon": 100.001, "lat": 13.001}, {"lon": 100.0, "lat": 13.0}]
    elements = [
        {"type": "node", "id": 1, "lon": 100.0, "lat": 13.0,
         "tags": {"amenity": "bench"}},
        {"type": "way", "id": 2, "tags": {"man_made": "pier"},
         "geometry": ring[:2]},
        {"type": "way", "id": 3, "tags": {"building": "yes"},
         "geometry": ring},
    ]
    default = classify_elements(elements)
    assert default["other_lines"] == [] and default["other_points"] == []
    assert len(default["buildings"]) == 1        # the rules still claim it

    everything = classify_elements(elements, keep_other=True)
    assert len(everything["buildings"]) == 1     # unchanged
    assert [line[2] for line in everything["other_lines"]] == ["way/2"]
    assert [p[4] for p in everything["other_points"]] == ["amenity"]


def test_untagged_geometry_is_never_kept_as_other():
    """An untagged way is a multipolygon's building material, not a
    feature — asking for everything must not draw courtyard walls twice."""
    from topo2cad import classify_elements

    elements = [{"type": "way", "id": 9, "tags": {},
                 "geometry": [{"lon": 100.0, "lat": 13.0},
                              {"lon": 100.001, "lat": 13.0}]}]
    assert classify_elements(elements, keep_other=True)["other_lines"] == []


# --- Overpass response cache -----------------------------------------------

def test_a_repeat_query_is_served_from_cache(tmp_path, monkeypatch):
    """A retry, a re-plot at another sheet size, or a second run at the same
    coordinate should not re-query someone else's infrastructure."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"elements": [{"type": "node", "id": 1}]}
        return R()

    monkeypatch.setattr(topo2cad.requests, "post", fake_post)
    first = topo2cad._post_overpass("q", cache_dir=tmp_path)
    second = topo2cad._post_overpass("q", cache_dir=tmp_path)
    assert first == second == [{"type": "node", "id": 1}]
    assert len(calls) == 1


def test_a_different_query_is_a_different_cache_entry(tmp_path):
    assert (topo2cad._cache_path("a", tmp_path)
            != topo2cad._cache_path("b", tmp_path))
    # ...and the same query always lands on the same file
    assert (topo2cad._cache_path("a", tmp_path)
            == topo2cad._cache_path("a", tmp_path))


def test_an_outage_falls_back_to_a_stale_cache(tmp_path, monkeypatch):
    """Three Overpass endpoints returned 504 within a minute of each other
    while this was written. A day-old answer beats no drawing."""
    import json as _json

    path = topo2cad._cache_path("q", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps([{"type": "way", "id": 7}]),
                    encoding="utf-8")

    def dead(url, **kwargs):
        raise OSError("504 Server Error: Gateway Timeout")

    monkeypatch.setattr(topo2cad.requests, "post", dead)
    monkeypatch.setattr(topo2cad.time, "sleep", lambda _s: None)
    # ttl=0 makes the entry stale, so this is the outage path, not a hit
    got = topo2cad._post_overpass("q", cache_dir=tmp_path, ttl=0)
    assert got == [{"type": "way", "id": 7}]


def test_without_a_cache_an_outage_still_raises(tmp_path, monkeypatch):
    """Silently drawing nothing would be worse than failing."""
    def dead(url, **kwargs):
        raise OSError("504 Server Error: Gateway Timeout")

    monkeypatch.setattr(topo2cad.requests, "post", dead)
    monkeypatch.setattr(topo2cad.time, "sleep", lambda _s: None)
    with pytest.raises(OSError):
        topo2cad._post_overpass("q", cache_dir=tmp_path)


def test_the_audit_never_reads_the_cache():
    """dxfaudit re-queries on purpose: auditing a drawing against the very
    snapshot it was made from proves only that the file matches itself."""
    source = (Path(__file__).resolve().parent.parent / "scripts"
              / "dxfaudit.py").read_text(encoding="utf-8")
    assert "fetch_osm(s, w, n, e, cache=False)" in source


# --- Layer tables must agree across the three writers -----------------------

def _created_layer_keys(script_name):
    """The LAYERS keys a writer creates, read from its (key, colour, weight)
    table. The lists live inside main(), so this reads the source rather
    than importing the CAD stack."""
    import re

    src = (Path(__file__).resolve().parent.parent / "scripts"
           / script_name).read_text(encoding="utf-8")
    body = src.split("doc.layers.add", 1)[0]
    return set(re.findall(r'\("(\w+)",\s*\d+,\s*\d+\)', body))


def test_every_writer_creates_the_same_layers():
    """dxfdiff compares layer tables, so a layer one route defines and
    another does not is a difference even when every entity matches.

    This caught a real one: adding C-ANNO-OVTR* to topo2cad.py and
    db2dxf.py left osm2cad.py behind, and its own re-issue came back
    DIFFER — three layers in one drawing only.
    """
    topo = _created_layer_keys("topo2cad.py")
    osm = _created_layer_keys("osm2cad.py")
    assert topo and osm, "the layer tables could not be read"
    assert topo == osm, (f"topo2cad only: {sorted(topo - osm)}, "
                         f"osm2cad only: {sorted(osm - topo)}")


def test_the_staging_writer_defines_exactly_those_layers():
    import importlib.util as iu

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    spec = iu.spec_from_file_location("db2dxf", scripts / "db2dxf.py")
    db2dxf = iu.module_from_spec(spec)
    spec.loader.exec_module(db2dxf)

    # The three empty site-plan layers are added outside the colour table
    extra = ("property", "setback", "road_row", "corner")
    names = {topo2cad.LAYERS[k]
             for k in _created_layer_keys("topo2cad.py") | set(extra)}
    assert set(db2dxf.LAYER_STYLE) == names, (
        f"db2dxf only: {sorted(set(db2dxf.LAYER_STYLE) - names)}, "
        f"extraction only: {sorted(names - set(db2dxf.LAYER_STYLE))}")


def test_both_routes_can_suppress_the_building_codes():
    """--names-only is a drawing choice, and both writers have to honour it
    or a re-issue silently disagrees with the drawing it re-issues.

    The flag existed on topo2cad.py for a long time while nothing read it,
    and the extraction route drew no codes at all — so the web form's
    checkbox moved nothing, and building_inventory.csv was keyed on B###
    codes that appeared nowhere on the sheet.
    """
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    topo = (scripts / "topo2cad.py").read_text(encoding="utf-8")
    db2 = (scripts / "db2dxf.py").read_text(encoding="utf-8")
    stage = (scripts / "stage_db.py").read_text(encoding="utf-8")

    for name, src in (("topo2cad.py", topo), ("db2dxf.py", db2)):
        assert '"--names-only"' in src, f"{name} does not offer --names-only"
        assert "names_only" in src, f"{name} never reads it"

    # The two halves have to agree on the string, or the filter silently
    # matches nothing and --names-only becomes a no-op again.
    assert "'building_code'" in stage, "the view stopped emitting the class"
    assert '"building_code"' in db2, "db2dxf stopped filtering on the class"


# ------------------------------------------------ what the DEM can support
def test_the_automatic_interval_never_beats_the_dem():
    """The Copernicus DEM Product Handbook specifies relative vertical
    accuracy better than 2 m on slopes of 20% or less. The interval used to
    start at 0.5 m and pick whatever gave about ten levels, so on the flat
    Thai central plain — 6.9 m of relief over a 250 m site — a submission
    drawing carried 0.5 m contours drawn from data that cannot resolve 2 m.
    """
    for span in (0.5, 1.0, 3.0, 6.9, 12.0, 24.0):
        assert topo2cad.auto_contour_interval(span) >= \
            topo2cad.DEM_MIN_CONTOUR_M, f"{span} m of relief"


def test_the_interval_still_grows_with_the_relief():
    """The floor is a floor, not a fixed interval: 400 m of relief must not
    come back as 400 contour lines."""
    assert topo2cad.auto_contour_interval(6.9) == 2
    assert topo2cad.auto_contour_interval(40) == 5
    assert topo2cad.auto_contour_interval(150) == 20
    # Even relief nothing in the table divides finely enough stays bounded
    assert topo2cad.auto_contour_interval(1e6) == max(
        topo2cad.CONTOUR_INTERVALS)


def test_a_survey_grade_source_could_go_finer():
    """The floor is a property of the source, not of contours. Passing a
    better one back opens the finer intervals up again, which is what makes
    this a rule about the DEM rather than a hardcoded minimum."""
    assert topo2cad.auto_contour_interval(3.0, floor=0.1) == 0.5


def test_the_poster_and_the_drawing_share_the_rule():
    """A poster and a CAD sheet of one site disagreeing about how much
    terrain the DEM supports is the drift this import prevents."""
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "mapposter.py").read_text(encoding="utf-8")
    assert "auto_contour_interval" in src
    # ...and does not keep a copy of the ladder to drift against
    assert "(0.5, 1, 2, 5, 10, 20, 50)" not in src
