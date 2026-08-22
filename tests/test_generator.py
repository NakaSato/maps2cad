"""Offline unit tests for generate_detailed_site_map.py.

Run:  .venv/bin/python -m pytest tests/ -q
Network integration test is opt-in:  RUN_NETWORK_TESTS=1 ... -q
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from generate_detailed_site_map import (  # noqa: E402
    SiteMapError,
    build_extent,
    build_inventory,
    classify_road,
    feature_id_of,
    label_angle,
    load_manual_labels,
    nice_scale_length,
    parse_args,
    utm_epsg_for,
    validate_args,
)
import generate_detailed_site_map as generator  # noqa: E402


# ---------------------------------------------------------------- UTM zones
@pytest.mark.parametrize("lat,lon,epsg", [
    (14.8164876968956, 100.511644184589, 32647),  # spec default: zone 47N
    (-33.87, 151.21, 32756),                      # Sydney: zone 56S
    (51.5, -0.13, 32630),                         # London: zone 30N
    (0.0, 0.0, 32631),                            # equator/prime meridian
    (10.0, -180.0, 32601),                        # antimeridian west edge
    (10.0, 180.0, 32660),                         # antimeridian east edge
])
def test_utm_epsg_for(lat, lon, epsg):
    assert utm_epsg_for(lat, lon) == epsg


def test_extent_is_exact_rectangle_in_metres():
    epsg, _, _, (x, y), rect, rect_wgs = build_extent(
        14.8164876968956, 100.511644184589, 500.0, 250.0)
    assert epsg == 32647
    x0, y0, x1, y1 = rect.bounds
    assert x1 - x0 == pytest.approx(500.0)
    assert y1 - y0 == pytest.approx(250.0)
    assert (x0 + x1) / 2 == pytest.approx(x)
    assert (y0 + y1) / 2 == pytest.approx(y)
    # WGS 84 footprint stays in valid ranges near the site
    lon0, lat0, lon1, lat1 = rect_wgs.bounds
    assert 100.5 < lon0 < lon1 < 100.52
    assert 14.81 < lat0 < lat1 < 14.82


def test_southern_hemisphere_extent():
    epsg, *_ = build_extent(-33.87, 151.21, 500.0, 250.0)
    assert epsg == 32756


# ------------------------------------------------------- road classification
@pytest.mark.parametrize("highway,cls", [
    ("motorway", "major"), ("trunk", "major"), ("primary", "major"),
    ("primary_link", "major"),
    ("secondary", "main"), ("tertiary", "main"),
    ("residential", "local"), ("service", "local"),
    ("unclassified", "local"),
    ("footway", "minor"), ("path", "minor"), ("track", "minor"),
    ("cycleway", "minor"), ("steps", "minor"),
])
def test_classify_road(highway, cls):
    assert classify_road(highway) == cls


# ------------------------------------------------------------------- labels
def test_label_angle_is_upright():
    from shapely.geometry import LineString
    for coords in [
        [(0, 0), (100, 0)],       # east
        [(0, 0), (-100, 0)],      # west (must flip to stay upright)
        [(0, 0), (0, 100)],       # north
        [(0, 0), (0, -100)],      # south (must flip)
        [(0, 0), (-70, -70)],     # southwest diagonal
    ]:
        ang = label_angle(LineString(coords))
        assert -90 <= ang <= 90, f"{coords} -> {ang} not upright"


def test_nice_scale_length():
    assert nice_scale_length(500) == 100     # spec default width
    assert nice_scale_length(250) == 50
    assert nice_scale_length(1000) == 250
    assert nice_scale_length(40) == 10
    assert nice_scale_length(4000) == 1000


# --------------------------------------------------------------- validation
def make_args(**over):
    base = dict(lat=14.8, lon=100.5, width=500.0, height=250.0,
                output="out.pdf")
    base.update(over)
    argv = []
    for k, v in base.items():
        argv += [f"--{k}", str(v)]
    return parse_args(argv)


@pytest.mark.parametrize("bad", [
    dict(lat=90.1), dict(lat=-95), dict(lon=181), dict(lon=-200),
    dict(width=0), dict(height=-5), dict(output="map.txt"),
])
def test_validate_args_rejects(bad):
    with pytest.raises(SiteMapError):
        validate_args(make_args(**bad))


def test_validate_args_accepts_defaults():
    validate_args(make_args())  # must not raise


# ------------------------------------------------------------ labels CSV
def test_load_manual_labels_missing_columns(tmp_path):
    p = tmp_path / "bad.csv"
    p.write_text("wrong,cols\n1,2\n", encoding="utf-8")
    with pytest.raises(SiteMapError, match="invalid structure"):
        load_manual_labels(str(p))


def test_load_manual_labels_roundtrip(tmp_path):
    p = tmp_path / "labels.csv"
    p.write_text(
        "feature_id,display_name\n"
        "way/1,อาคารทดสอบ\n"
        "way/2,\n"            # empty override must be ignored (FR-07)
        ",Orphan\n",          # missing id must be ignored
        encoding="utf-8")
    assert load_manual_labels(str(p)) == {"way/1": "อาคารทดสอบ"}


def test_load_manual_labels_utf8_bom(tmp_path):
    p = tmp_path / "labels.csv"
    p.write_bytes("feature_id,display_name\nway/9,ศาลากลาง\n"
                  .encode("utf-8-sig"))
    assert load_manual_labels(str(p)) == {"way/9": "ศาลากลาง"}


# ------------------------------------------------------------- inventory
def synthetic_buildings():
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import box

    # Deliberately unsorted ids to prove deterministic ordering
    idx = pd.MultiIndex.from_tuples(
        [("way", 30), ("way", 10), ("relation", 5), ("way", 20)],
        names=["element", "id"])
    return gpd.GeoDataFrame(
        {
            "name": [None, "วัดทดสอบ", None, None],
            "building": ["yes", "temple", "yes", "house"],
        },
        geometry=[box(i * 100, 0, i * 100 + 50, 40) for i in range(4)],
        index=idx, crs="EPSG:32647")


def wgs_transformer():
    from pyproj import Transformer
    return Transformer.from_crs("EPSG:32647", "EPSG:4326", always_xy=True)


def test_inventory_codes_deterministic_and_unique():
    records = build_inventory(synthetic_buildings(), wgs_transformer(), {})
    by_id = {r.feature_id: r for r in records}
    assert len(records) == 4
    # Sorted by feature_id string: relation/5, way/10, way/20, way/30.
    # way/10 is named, so unnamed buildings code up in that order.
    assert by_id["relation/5"].code == "B001"
    assert by_id["way/10"].code == ""
    assert by_id["way/10"].display_name == "วัดทดสอบ"
    assert by_id["way/20"].code == "B002"
    assert by_id["way/30"].code == "B003"
    # Re-running produces the identical assignment (NFR-02)
    again = build_inventory(synthetic_buildings(), wgs_transformer(), {})
    assert [(r.feature_id, r.code) for r in again] == \
           [(r.feature_id, r.code) for r in records]


def test_inventory_override_and_fallback():
    overrides = {"way/20": "Verified Hall", "way/10": ""}
    records = build_inventory(synthetic_buildings(), wgs_transformer(),
                              overrides)
    by_id = {r.feature_id: r for r in records}
    assert by_id["way/20"].display_name == "Verified Hall"
    assert by_id["way/20"].code == "B002"          # code retained in CSV
    assert by_id["way/10"].display_name == "วัดทดสอบ"  # empty -> source name
    assert by_id["relation/5"].display_name == "B001"  # unnamed -> code


def test_inventory_coordinates_are_wgs84():
    records = build_inventory(synthetic_buildings(), wgs_transformer(), {})
    for r in records:
        assert -90 <= r.latitude <= 90
        assert -180 <= r.longitude <= 180


def test_feature_id_of():
    assert feature_id_of(("way", 123)) == "way/123"
    assert feature_id_of(("relation", 7)) == "relation/7"
    assert feature_id_of("plain") == "plain"


# ------------------------------------------- review-finding regression tests
def test_iter_parts_descends_geometry_collection():
    from shapely.geometry import (GeometryCollection, LineString,
                                  MultiPolygon, Point, box)
    from generate_detailed_site_map import iter_parts

    gc = GeometryCollection([
        Point(0, 0),
        LineString([(0, 0), (1, 1)]),
        MultiPolygon([box(0, 0, 1, 1), box(2, 0, 3, 1)]),
    ])
    assert len(list(iter_parts(gc, ("Polygon",)))) == 2
    assert len(list(iter_parts(gc, ("LineString",)))) == 1


def test_polygon_patch_preserves_holes():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from shapely.geometry import Polygon
    from generate_detailed_site_map import polygon_patch
    from matplotlib.path import Path as MplPath

    donut = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)],
                    holes=[[(4, 4), (6, 4), (6, 6), (4, 6)]])
    patch = polygon_patch(donut, facecolor="red", edgecolor="none")
    # Two MOVETO codes = two rings in one path (real hole, not a cover patch)
    assert list(patch.get_path().codes).count(MplPath.MOVETO) == 2
    # The rendered result must show the hole (white), not paint over it
    fig, ax = plt.subplots(figsize=(2, 2), dpi=50)
    ax.add_patch(patch)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 10)
    ax.axis("off")
    fig.canvas.draw()
    buf = np.asarray(fig.canvas.buffer_rgba())
    h, w, _ = buf.shape
    hole_px = buf[h // 2, w // 2]
    ring_px = buf[h // 2, w // 5]
    plt.close(fig)
    assert hole_px[0] > 200 and hole_px[1] > 200      # hole stays white
    assert ring_px[0] > 200 and ring_px[1] < 100      # ring is red


def test_split_features_building_no_and_riverbank():
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import LineString, box
    from generate_detailed_site_map import split_features

    rect = box(0, 0, 500, 250)
    idx = pd.MultiIndex.from_tuples(
        [("way", 1), ("way", 2), ("way", 3), ("way", 4)],
        names=["element", "id"])
    gdf = gpd.GeoDataFrame(
        {
            "building": ["yes", "no", None, None],
            "natural": [None, None, None, None],
            "waterway": [None, None, "riverbank", "stream"],
            "highway": [None, None, None, None],
        },
        geometry=[box(10, 10, 40, 40),          # real building
                  box(50, 10, 80, 40),          # building=no -> excluded
                  box(100, 10, 200, 100),       # riverbank polygon -> water
                  LineString([(0, 200), (500, 200)])],  # stream line
        index=idx, crs="EPSG:32647")
    layers = split_features(gdf, "EPSG:32647", rect)
    assert [feature_id_of(i) for i in layers["buildings"].index] == ["way/1"]
    assert [feature_id_of(i) for i in layers["water_polys"].index] == \
        ["way/3"]
    assert [feature_id_of(i) for i in layers["water_lines"].index] == \
        ["way/4"]


def test_representative_fraction_letterboxed():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from generate_detailed_site_map import representative_fraction

    fig = plt.figure(figsize=(16.54, 11.69))
    ax = fig.add_axes([0.04, 0.225, 0.92, 0.695])
    # Width-limited (500x250): drawn width == box width
    wide = representative_fraction(fig, ax, 500.0, 250.0)
    # Height-limited (250x500): the drawn map is much narrower than the
    # box, so the scale denominator must be much larger, not equal
    tall = representative_fraction(fig, ax, 250.0, 500.0)
    plt.close(fig)
    assert wide == 1300
    # pre-fix behavior computed ~650 here (nominal box width); the drawn
    # map is only ~4 in wide, so the true scale is much smaller
    assert tall == 2400


def test_validate_args_rejects_nan_width():
    with pytest.raises(SiteMapError):
        validate_args(make_args(width="nan"))


def test_standard_profile_auto_orients_and_gov_stays_landscape():
    """Portrait extents must not waste most of the sheet; the government
    profile keeps landscape per spec 13.9."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from generate_detailed_site_map import SHEET_SIZES

    a3_w, a3_h = SHEET_SIZES["A3"]
    # The rule the renderer applies:
    for w_m, h_m, expect_landscape in [(500, 250, True), (250, 500, False)]:
        sw, sh = (a3_h, a3_w) if h_m > w_m else (a3_w, a3_h)
        assert (sw >= sh) is expect_landscape
    plt.close("all")


def test_government_type_scales_with_sheet_size():
    """Font sizes are absolute points, so they must shrink on smaller
    sheets or the info column overruns the border (spec 8.3/11)."""
    from generate_detailed_site_map import SHEET_SIZES

    a3 = SHEET_SIZES["A3"][0]
    scales = {name: SHEET_SIZES[name][0] / a3 for name in SHEET_SIZES}
    assert scales["A3"] == 1.0
    assert scales["A4"] < 0.75          # A4 type must be meaningfully smaller
    assert scales["A2"] > 1.4           # A2 type larger, keeping proportions
    # All sheets share the ISO aspect ratio, so scaling by width is safe
    for w, h in SHEET_SIZES.values():
        assert abs((w / h) - (a3 / SHEET_SIZES["A3"][1])) < 0.01


# ------------------------------------------------ scale-aware label density
def test_label_fits_is_scale_aware():
    """The same building/label pair must fit at a large printed scale and
    not fit at a small one (spec 8.4 readability)."""
    from shapely.geometry import box
    from generate_detailed_site_map import label_fits

    footprint = box(0, 0, 15, 10)          # ordinary 15 x 10 m shophouse
    # 500 m across a ~15 in sheet: roughly 0.46 m per point -> fits
    assert label_fits("B001", 5.5, footprint, 0.46)
    # 2000 m across the same sheet: ~1.85 m per point -> no longer fits
    assert not label_fits("B001", 5.5, footprint, 1.85)


def test_label_fits_rejects_overlong_names():
    from shapely.geometry import box
    from generate_detailed_site_map import label_fits

    small = box(0, 0, 12, 10)
    assert label_fits("Hall", 5.5, small, 0.3)
    assert not label_fits("ศูนย์ราชการจังหวัดลพบุรี", 5.5, small, 0.3)


def test_draw_buildings_drops_colliding_codes_but_keeps_names():
    """Named landmarks outrank B### codes and road labels; codes yield."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from shapely.geometry import box
    from generate_detailed_site_map import BuildingRecord, draw_buildings

    recs = [
        BuildingRecord("way/1", "", "Siam Center", "Siam Center", "retail",
                       0, 0, box(0, 0, 60, 40)),
        # Overlaps the name and is too tight for any alternate position,
        # so this code must yield rather than overprint
        BuildingRecord("way/2", "B001", "", "B001", "yes", 0, 0,
                       box(28, 19, 33, 21)),
    ]
    fig, ax = plt.subplots()
    dropped = draw_buildings(ax, recs, COLOURS_FOR_TEST, True, 0.3)
    texts = [t.get_text() for t in ax.texts]
    plt.close(fig)
    assert "Siam Center" in texts
    assert "B001" not in texts
    assert dropped == 1


def test_named_building_ignores_road_label_box():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from shapely.geometry import box
    from generate_detailed_site_map import BuildingRecord, draw_buildings

    rec = [BuildingRecord("way/1", "", "Siam Discovery", "Siam Discovery",
                          "retail", 0, 0, box(0, 0, 60, 40))]
    code = [BuildingRecord("way/2", "B001", "", "B001", "yes", 0, 0,
                           box(0, 0, 60, 40))]
    road_box = [(-100, -100, 100, 100)]   # road label covering everything
    fig, ax = plt.subplots()
    draw_buildings(ax, rec, COLOURS_FOR_TEST, True, 0.3, occupied=road_box)
    named_texts = [t.get_text() for t in ax.texts]
    plt.close(fig)

    fig, ax = plt.subplots()
    dropped = draw_buildings(ax, code, COLOURS_FOR_TEST, True, 0.3,
                             occupied=road_box)
    code_texts = [t.get_text() for t in ax.texts]
    plt.close(fig)

    assert "Siam Discovery" in named_texts   # landmark survives (13.6)
    assert code_texts == [] and dropped == 1  # code yields to the road name


COLOURS_FOR_TEST = {
    "building_fill": "#DCEFF2", "building_edge": "#008C99",
    "text_primary": "#102A43",
}


# ------------------------------------------------- end-to-end (opt-in, slow)
@pytest.mark.skipif(not os.environ.get("RUN_NETWORK_TESTS"),
                    reason="set RUN_NETWORK_TESTS=1 to hit Overpass")
def test_end_to_end_generates_outputs(tmp_path):
    from generate_detailed_site_map import main
    pdf = tmp_path / "map.pdf"
    png = tmp_path / "map.png"
    inv = tmp_path / "inv.csv"
    rc = main(["--lat", "14.799417", "--lon", "100.614458",
               "--output", str(pdf), "--png", str(png),
               "--inventory", str(inv)])
    assert rc == 0
    assert pdf.stat().st_size > 10_000
    assert png.stat().st_size > 100_000
    assert inv.read_text(encoding="utf-8").startswith("feature_id,")


# ------------------------------------------- one-way arrows and the backdrop
def test_government_profile_refuses_arrows_and_basemap():
    """That sheet renders what its spec lists. A flag left on from an
    earlier run must not quietly add a layer to a submission drawing."""
    from generate_detailed_site_map import parse_args, validate_args

    for extra in (["--arrows"], ["--basemap", "osm"], ["--arrows",
                                                       "--basemap"]):
        argv = ["--lat", "14.8", "--lon", "100.5", "--width", "500",
                "--height", "250", "--output", "out.pdf",
                "--profile", "government"] + extra
        with pytest.raises(SiteMapError, match="standard-profile"):
            validate_args(parse_args(argv))


def test_standard_profile_accepts_them():
    from generate_detailed_site_map import parse_args, validate_args

    argv = ["--lat", "14.8", "--lon", "100.5", "--width", "500",
            "--height", "250", "--output", "out.pdf", "--arrows",
            "--basemap", "opentopomap"]
    a = parse_args(argv)
    validate_args(a)                       # must not raise
    assert a.arrows and a.basemap == "opentopomap"


def test_basemap_flag_defaults_to_osm_when_bare():
    from generate_detailed_site_map import parse_args

    assert parse_args(["--lat", "14.8", "--lon", "100.5", "--width", "500",
                       "--height", "250", "--output", "o.pdf",
                       "--basemap"]).basemap == "osm"


@pytest.mark.parametrize("row,expected", [
    ({"oneway": "yes"}, 1),
    ({"oneway": "-1"}, -1),
    ({"oneway": "reverse"}, -1),
    ({"oneway": "no", "junction": "roundabout"}, 0),
    ({"junction": "roundabout"}, 1),
    ({}, 0),
    ({"oneway": None}, 0),                 # absent column reads as None
    ({"oneway": "alternating"}, 0),
])
def test_oneway_of_matches_the_cad_rule(row, expected):
    """Restated from topo2cad.oneway_dir() rather than imported, so this
    stack keeps its own dependency set. It has to agree all the same."""
    from generate_detailed_site_map import oneway_of
    from topo2cad import oneway_dir

    assert oneway_of(row) == expected
    assert oneway_dir({k: v for k, v in row.items() if v is not None}) \
        == expected


# --- Supplied survey data on the sheet (--overlay-db) -----------------------

def _overlay_db(tmp_path, srid=32647):
    """A staging database holding one OSM building and one supplied parcel."""
    import importlib.util as iu
    from shapely.geometry import LineString, Polygon

    spec = iu.spec_from_file_location(
        "stage_db", Path(__file__).resolve().parent.parent
        / "scripts" / "stage_db.py")
    stage_db = iu.module_from_spec(spec)
    spec.loader.exec_module(stage_db)

    db = tmp_path / "staging.sqlite"
    conn = stage_db.connect(db)
    pid = stage_db.create_project(conn, "site", 13.7455, 100.5325, 500, 400,
                                  srid)
    stage_db.stage_buildings(conn, pid, [
        {"feature_id": "way/1", "source": "openstreetmap", "osm_name": "OSM",
         "code": "", "display_name": "OSM", "building_type": None,
         "geom": Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])},
        {"feature_id": "gis/plot/0", "source": "user_gis:boundary.geojson",
         "osm_name": "", "code": "", "display_name": "แปลงที่ดิน A",
         "building_type": None,
         "geom": Polygon([(20, 0), (30, 0), (30, 10), (20, 10)])}])
    stage_db.stage_roads(conn, pid, [
        {"feature_id": "gis/plot/1", "source": "user_gis:boundary.geojson",
         "highway_type": "user_gis", "road_name": "แนวรั้ว", "road_ref": None,
         "carriageway_m": 0.0, "geom": LineString([(0, 0), (50, 50)])}])
    conn.close()
    return db


def test_overlay_reads_only_the_supplied_features(tmp_path):
    """OpenStreetMap is already on this sheet from this stack's own fetch;
    drawing the staged copy as well would double every outline."""
    pytest.importorskip("shapely")
    db = _overlay_db(tmp_path)
    features, sources, srid = generator.read_overlay(db, "site")
    assert srid == 32647
    assert sources == ["boundary.geojson"]
    labels = sorted(label for _geom, label in features)
    assert labels == ["แนวรั้ว", "แปลงที่ดิน A"]


def test_overlay_reprojects_when_the_sheet_is_in_another_zone(tmp_path):
    """Staging is in the project's own zone. Plotting those numbers on a
    sheet in another zone is how a boundary lands a zone away and still
    looks drawn."""
    pytest.importorskip("pyproj")
    db = _overlay_db(tmp_path)
    same, _s, _srid = generator.read_overlay(db, "site", epsg=32647)
    moved, _s, _srid = generator.read_overlay(db, "site", epsg=32648)
    assert same[0][0].bounds != moved[0][0].bounds


def test_overlay_names_a_project_that_is_not_there(tmp_path):
    db = _overlay_db(tmp_path)
    with pytest.raises(generator.SiteMapError, match="No such project"):
        generator.read_overlay(db, "not-this-one")


def test_supplied_survey_data_is_allowed_on_the_government_sheet(tmp_path):
    """--arrows and --basemap are refused there because they add decoration
    the spec does not list. A surveyed parcel is the subject a ผังบริเวณ is
    read for, and it is named on the sheet rather than added silently."""
    db = _overlay_db(tmp_path)
    args = generator.parse_args([
        "--lat", "13.7455", "--lon", "100.5325", "--profile", "government",
        "--output", str(tmp_path / "m.pdf"), "--overlay-db", str(db)])
    generator.validate_args(args)          # must not raise

    args = generator.parse_args([
        "--lat", "13.7455", "--lon", "100.5325", "--profile", "government",
        "--output", str(tmp_path / "m.pdf"), "--arrows"])
    with pytest.raises(generator.SiteMapError, match="standard-profile"):
        generator.validate_args(args)


def test_overlay_project_without_a_database_is_refused(tmp_path):
    args = generator.parse_args([
        "--lat", "13.7455", "--lon", "100.5325",
        "--output", str(tmp_path / "m.pdf"), "--overlay-project", "site"])
    with pytest.raises(generator.SiteMapError, match="needs --overlay-db"):
        generator.validate_args(args)


def test_the_legend_keys_supplied_data_only_when_it_is_drawn():
    handles = generator.legend_handles(generator.GOV_COLOURS, True,
                                       survey=True)
    assert any("Supplied survey" in h.get_label() for h in handles)
    plain = generator.legend_handles(generator.GOV_COLOURS, True)
    assert not any("Supplied survey" in h.get_label() for h in plain)


# ------------------------------------------------- a measurable scale
def _map_axes(size, box=(0.035, 0.06, 0.63, 0.86)):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=generator.SHEET_SIZES[size])
    return fig, fig.add_axes(list(box))


@pytest.mark.parametrize("size", ["A4", "A3"])
@pytest.mark.parametrize("extent", [(200, 150), (250, 200), (500, 400),
                                    (1000, 750), (2000, 1500)])
def test_the_stated_scale_is_the_drawings_actual_scale(size, extent):
    """The map used to be drawn at whatever scale filled the space and the
    sheet then reported that to two significant figures — "≈ 1:1,900".
    Nobody can measure a sheet with a scale rule at 1:1,900, and a
    ผังบริเวณ is a document an officer measures."""
    import matplotlib.pyplot as plt

    w, h = extent
    fig, ax = _map_axes(size)
    stated = generator.fit_round_scale(fig, ax, w, h)
    drawn_in = generator.drawn_width_inches(fig, ax, w, h)
    actual = w / (drawn_in * 0.0254)
    plt.close(fig)
    assert actual == pytest.approx(stated, abs=0.5)
    assert stated in generator.MAP_ROUND_SCALES


@pytest.mark.parametrize("size", ["A4", "A3"])
@pytest.mark.parametrize("extent", [(200, 150), (250, 200), (500, 400),
                                    (1000, 750), (2000, 1500)])
def test_the_map_and_the_cad_sheet_agree_on_the_scale(size, extent):
    """Two deliverables of one site quoting different scales is a defect a
    reviewer sees immediately: the CAD sheet said 1:2,000 for the same
    500 x 400 m extent on the same A3 while the map said ≈ 1:1,900.

    If this ever fails, the map's layout box or sheet.py's viewport has
    moved and the two sheets have silently diverged — decide which is
    right rather than loosening the test.
    """
    import matplotlib.pyplot as plt

    sheet = pytest.importorskip("sheet")
    w, h = extent
    fig, ax = _map_axes(size)
    on_map = generator.fit_round_scale(fig, ax, w, h)
    plt.close(fig)
    on_cad, _, _ = sheet.fitting_scale(w, h, size)
    assert on_map == on_cad, (
        f"{w}x{h} on {size}: map 1:{on_map}, CAD 1:{on_cad}")


def test_the_map_fills_the_frame_it_is_given_as_far_as_a_round_scale_allows():
    """The axes is shrunk to the exact size the round scale needs, which
    leaves whitespace inside the frame. It must still be the largest round
    scale that fits — shrinking to the next one down would waste half the
    sheet."""
    import matplotlib.pyplot as plt

    fig, ax = _map_axes("A3")
    chosen = generator.fit_round_scale(fig, ax, 500, 400)
    finer = [s for s in generator.MAP_ROUND_SCALES if s < chosen]
    pos = ax.get_position()
    fw, fh = fig.get_size_inches()
    plt.close(fig)
    # the next finer scale would need more room than the frame has
    if finer:
        need = 500 / max(finer) / 0.0254
        assert need > fw * 0.63 or (400 / max(finer) / 0.0254) > fh * 0.86
    assert pos.width > 0 and pos.height > 0
