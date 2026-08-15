"""Tests for the SQLite staging layer (scripts/stage_db.py).

These run entirely on in-memory databases and synthetic geometry — no
network, no DEM.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from stage_db import (  # noqa: E402
    _osm_id,
    apply_verified,
    connect,
    create_project,
    interior_point,
    line_label_anchor,
    migrate,
    record_verified,
    split_by_script,
    stage_buildings,
    stage_roads,
)


# ------------------------------------------------------------- identifiers
@pytest.mark.parametrize("feature_id,expected", [
    ("way/1428947528", 1428947528),
    ("relation/12345", 12345),
    ("node/7", 7),
    ("ms/00042", None),          # Microsoft ML footprints have no OSM identity
    ("nonsense", None),
])
def test_osm_id_parsing(feature_id, expected):
    assert _osm_id(feature_id) == expected


# ------------------------------------------------------------ label anchors
def test_interior_point_stays_inside_concave_footprint():
    """The reason the schema does not use a centroid: an L-shaped building
    has its centroid in the notch, outside the polygon."""
    from shapely.geometry import Point, Polygon

    L = Polygon([(0, 0), (30, 0), (30, 10), (10, 10), (10, 30), (0, 30)])
    assert not L.contains(L.centroid)          # centroid would strand a label
    x, y = interior_point(L)
    assert L.contains(Point(x, y))


def test_line_label_anchor_uses_longest_part_and_stays_upright():
    from shapely.geometry import LineString, MultiLineString

    short = LineString([(0, 0), (10, 0)])
    long = LineString([(100, 0), (400, 0)])
    x, y, rot = line_label_anchor(MultiLineString([short, long]))
    assert 100 < x < 400                        # midpoint of the long part
    assert -90 <= rot <= 90

    for coords in ([(0, 0), (-100, 0)], [(0, 0), (0, -100)],
                   [(0, 0), (-70, -70)]):
        _, _, ang = line_label_anchor(LineString(coords))
        assert -90 <= ang <= 90, f"{coords} -> {ang}"


def test_line_label_anchor_handles_degenerate_geometry():
    from shapely.geometry import LineString

    x, y, rot = line_label_anchor(LineString([(5, 5), (5, 5)]))
    assert (x, y, rot) == (None, None, 0.0)


# ------------------------------------------------------------------ schema
@pytest.fixture()
def db():
    conn = connect(":memory:")
    pid = create_project(conn, "test-site", 15.8338, 104.3945, 770, 410, 32648)
    return conn, pid


def test_project_stores_its_own_srid(db):
    conn, pid = db
    row = conn.execute("SELECT srid FROM projects WHERE id = ?",
                       (pid,)).fetchone()
    # Derived per site, not fixed in the DDL: this site is zone 48N
    assert row["srid"] == 32648


def test_building_without_osm_id_is_storable(db):
    """Microsoft ML footprints have no osm_id; they must still stage."""
    from shapely.geometry import box

    conn, pid = db
    n = stage_buildings(conn, pid, [
        {"feature_id": "ms/00001", "source": "microsoft_ml",
         "osm_name": "", "code": "B001", "display_name": "B001",
         "building_type": None, "geom": box(0, 0, 10, 8)},
        {"feature_id": "way/55", "source": "openstreetmap",
         "osm_name": "Substation", "code": "", "display_name": "Substation",
         "building_type": "industrial", "geom": box(20, 0, 30, 8)},
    ])
    assert n == 2
    rows = conn.execute("SELECT feature_id, osm_id, source FROM"
                        " staging_buildings ORDER BY feature_id").fetchall()
    assert rows[0]["osm_id"] is None and rows[0]["source"] == "microsoft_ml"
    assert rows[1]["osm_id"] == 55


def test_multipart_road_geometry_round_trips(db):
    """Clipping an extent produces MultiLineString roads; a fixed
    LineString column would reject them."""
    from shapely import wkb
    from shapely.geometry import LineString, MultiLineString

    conn, pid = db
    geom = MultiLineString([[(0, 0), (50, 0)], [(80, 0), (200, 0)]])
    stage_roads(conn, pid, [{
        "feature_id": "way/9", "geom": geom, "highway_type": "trunk",
        "road_name": "Route A", "road_ref": "202", "carriageway_m": 12.0}])
    blob = conn.execute("SELECT geom_wkb FROM staging_roads").fetchone()[0]
    back = wkb.loads(blob)
    assert back.geom_type == "MultiLineString"
    assert back.equals(geom)


def test_cad_labels_dedupes_divided_carriageway(db):
    """A divided road is several ways sharing one name — the view must
    label it once, on the longest segment."""
    from shapely.geometry import LineString

    conn, pid = db
    stage_roads(conn, pid, [
        {"feature_id": "way/1", "geom": LineString([(0, 0), (100, 0)]),
         "highway_type": "trunk", "road_name": "ถนนอรุณประเสริฐ",
         "road_ref": "202", "carriageway_m": 12.0},
        {"feature_id": "way/2", "geom": LineString([(0, 8), (500, 8)]),
         "highway_type": "trunk", "road_name": "ถนนอรุณประเสริฐ",
         "road_ref": "202", "carriageway_m": 12.0},
        {"feature_id": "way/3", "geom": LineString([(0, 40), (60, 40)]),
         "highway_type": "residential", "road_name": "ถนนเทศบาล 1",
         "road_ref": None, "carriageway_m": 6.0},
    ])
    names = conn.execute(
        "SELECT text, label_y FROM cad_labels WHERE feature_class ="
        " 'road_name' ORDER BY text").fetchall()
    assert [r["text"] for r in names] == ["ถนนอรุณประเสริฐ", "ถนนเทศบาล 1"]
    # kept the longer of the two carriageways
    assert names[0]["label_y"] == 8
    refs = conn.execute("SELECT text FROM cad_labels WHERE feature_class ="
                        " 'road_ref'").fetchall()
    assert [r["text"] for r in refs] == ["202"]


def test_cad_labels_covers_every_building(db):
    from shapely.geometry import box

    conn, pid = db
    stage_buildings(conn, pid, [
        {"feature_id": f"ms/{i:05d}", "source": "microsoft_ml",
         "osm_name": "", "code": f"B{i:03d}", "display_name": f"B{i:03d}",
         "building_type": None, "geom": box(i * 20, 0, i * 20 + 10, 8)}
        for i in range(5)])
    n = conn.execute("SELECT COUNT(*) FROM cad_labels WHERE feature_class ="
                     " 'building'").fetchone()[0]
    assert n == 5


def test_restaging_replaces_previous_run(db):
    """Re-running a site must not accumulate duplicate features."""
    from shapely.geometry import box

    conn, _ = db
    for _ in range(2):
        pid = create_project(conn, "test-site", 15.8338, 104.3945,
                             770, 410, 32648)
        stage_buildings(conn, pid, [
            {"feature_id": "ms/00001", "source": "microsoft_ml",
             "osm_name": "", "code": "B001", "display_name": "B001",
             "building_type": None, "geom": box(0, 0, 10, 8)}])
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM staging_buildings").fetchone()[0] == 1


def test_merge_keeps_existing_features(db):
    """gis2cad --db merges a survey layer into an OSM extraction rather
    than replacing it, so one drawing can carry both."""
    from shapely.geometry import box
    from stage_db import get_or_create_project

    conn, pid = db
    stage_buildings(conn, pid, [
        {"feature_id": "ms/00001", "source": "microsoft_ml", "osm_name": "",
         "code": "B001", "display_name": "B001", "building_type": None,
         "geom": box(0, 0, 10, 8)}])

    same_pid, existed = get_or_create_project(
        conn, "test-site", 15.8338, 104.3945, 770, 410, 32648)
    assert existed is True and same_pid == pid       # no new project
    stage_buildings(conn, same_pid, [
        {"feature_id": "gis/plots/00000", "source": "user_gis",
         "osm_name": "แปลงโซลาร์ A", "code": "",
         "display_name": "แปลงโซลาร์ A", "building_type": None,
         "geom": box(50, 0, 90, 40)}])

    rows = conn.execute("SELECT feature_id, source FROM staging_buildings"
                        " WHERE project_id = ? ORDER BY feature_id",
                        (pid,)).fetchall()
    assert [r["source"] for r in rows] == ["user_gis", "microsoft_ml"]
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1


def test_create_project_still_replaces(db):
    """A fresh extraction of the same site must not duplicate features."""
    from shapely.geometry import box

    conn, pid = db
    stage_buildings(conn, pid, [
        {"feature_id": "ms/00001", "source": "microsoft_ml", "osm_name": "",
         "code": "B001", "display_name": "B001", "building_type": None,
         "geom": box(0, 0, 10, 8)}])
    new_pid = create_project(conn, "test-site", 15.8338, 104.3945,
                             770, 410, 32648)
    assert conn.execute("SELECT COUNT(*) FROM staging_buildings"
                        " WHERE project_id = ?", (new_pid,)).fetchone()[0] == 0


# ------------------------------------------- field-verified names survive
def _one_building(fid="ms/00003", name="B004", code="B004"):
    from shapely.geometry import box
    return {"feature_id": fid, "source": "microsoft_ml", "osm_name": "",
            "code": code, "display_name": name, "building_type": None,
            "geom": box(0, 0, 10, 8)}


def test_verified_name_survives_re_extraction(db):
    """Re-running an OSM pull replaces the staged features. A name someone
    walked out and confirmed must not go with them."""
    from stage_db import record_verified

    conn, pid = db
    stage_buildings(conn, pid, [_one_building()])
    conn.execute("UPDATE staging_buildings SET display_name = ? WHERE"
                 " feature_id = ?", ("EV Charging Canopy", "ms/00003"))
    record_verified(conn, pid, "ms/00003", "EV Charging Canopy")
    conn.commit()

    # A fresh extraction of the same site
    same = create_project(conn, "test-site", 15.8338, 104.3945,
                          770, 410, 32648)
    stage_buildings(conn, same, [_one_building()])

    row = conn.execute("SELECT display_name FROM staging_buildings WHERE"
                       " feature_id = ?", ("ms/00003",)).fetchone()
    assert row["display_name"] == "EV Charging Canopy"


def test_project_id_is_stable_across_re_extraction(db):
    """/project/<id> links and bookmarks must keep working."""
    conn, pid = db
    stage_buildings(conn, pid, [_one_building()])
    again = create_project(conn, "test-site", 15.8338, 104.3945,
                           770, 410, 32648)
    assert again == pid
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    # and the previous features are gone, not duplicated
    assert conn.execute("SELECT COUNT(*) FROM staging_buildings"
                        " WHERE project_id = ?", (pid,)).fetchone()[0] == 0


def test_forgetting_a_name_stops_it_returning(db):
    from stage_db import forget_verified, record_verified

    conn, pid = db
    stage_buildings(conn, pid, [_one_building()])
    record_verified(conn, pid, "ms/00003", "Wrong Name")
    forget_verified(conn, pid, "ms/00003")
    conn.commit()
    same = create_project(conn, "test-site", 15.8338, 104.3945,
                          770, 410, 32648)
    stage_buildings(conn, same, [_one_building()])
    row = conn.execute("SELECT display_name FROM staging_buildings WHERE"
                       " feature_id = ?", ("ms/00003",)).fetchone()
    assert row["display_name"] == "B004"      # back to its code


# ----------------------------------------------------- language annotation
@pytest.mark.parametrize("name,th,en", [
    ("ถนนอรุณประเสริฐ", "ถนนอรุณประเสริฐ", None),
    ("Arun Prasoet Road", None, "Arun Prasoet Road"),
    ("B042", None, "B042"),
    (None, None, None),
    ("", None, None),
])
def test_split_by_script_files_a_lone_name(name, th, en):
    assert split_by_script(name) == (th, en)


def test_split_by_script_does_not_override_explicit_tags():
    assert split_by_script("ถนนอรุณประเสริฐ", "ถนนอรุณประเสริฐ",
                           "Arun Prasoet Road") == \
        ("ถนนอรุณประเสริฐ", "Arun Prasoet Road")


def test_building_name_routes_to_its_language_layer(db):
    conn, pid = db
    stage_buildings(conn, pid, [
        {**_one_building("way/1", "โรงเรียนบ้านนา", ""),
         "osm_name": "โรงเรียนบ้านนา"},
        {**_one_building("way/2", "7-Eleven", ""), "osm_name": "7-Eleven"},
    ])
    rows = conn.execute(
        "SELECT text, cad_layer FROM cad_labels WHERE feature_class ="
        " 'building' ORDER BY text").fetchall()
    assert {r["text"]: r["cad_layer"] for r in rows} == {
        "7-Eleven": "C-ANNO-TEXT-EN",
        "โรงเรียนบ้านนา": "C-ANNO-TEXT-TH",
    }


def test_unnamed_building_code_stays_language_neutral(db):
    """A B### code is neither Thai nor English. At a rural site every
    footprint is coded, so putting codes on a language layer would blank
    the drawing the moment a drafter freezes that layer."""
    conn, pid = db
    stage_buildings(conn, pid, [_one_building()])
    rows = conn.execute(
        "SELECT text, cad_layer FROM cad_labels WHERE feature_class ="
        " 'building'").fetchall()
    assert [(r["text"], r["cad_layer"]) for r in rows] == \
        [("B004", "C-ANNO-TEXT")]


def test_bilingual_building_writes_both_labels_stacked(db):
    conn, pid = db
    stage_buildings(conn, pid, [
        {**_one_building("way/1", "โรงเรียนบ้านนา", ""),
         "name_th": "โรงเรียนบ้านนา", "name_en": "Ban Na School"}])
    rows = conn.execute(
        "SELECT text, cad_layer, label_offset FROM cad_labels"
        " ORDER BY label_offset").fetchall()
    assert [(r["text"], r["cad_layer"]) for r in rows] == [
        ("โรงเรียนบ้านนา", "C-ANNO-TEXT-TH"),
        ("Ban Na School", "C-ANNO-TEXT-EN"),
    ]
    # Thai sits on the anchor, English is nudged clear of it
    assert rows[0]["label_offset"] == 0.0
    assert rows[1]["label_offset"] == pytest.approx(3.5 * 1.3)


def test_english_label_is_not_offset_when_it_is_the_only_one(db):
    conn, pid = db
    stage_buildings(conn, pid, [
        {**_one_building("way/2", "7-Eleven", ""), "osm_name": "7-Eleven"}])
    row = conn.execute("SELECT label_offset FROM cad_labels").fetchone()
    assert row["label_offset"] == 0.0


def _road(fid, name, ref, length=100.0, th=None, en=None):
    from shapely.geometry import LineString
    return {"feature_id": fid, "geom": LineString([(0, 0), (length, 0)]),
            "highway_type": "trunk", "road_name": name, "road_ref": ref,
            "carriageway_m": 12.0, "name_th": th, "name_en": en}


def test_unnamed_road_ref_keeps_its_thai_prefix(db):
    """topo2cad.py draws a bare route number as 'ทล.202' when the road has
    no name; the staging route has to render the same string, on the Thai
    layer that string belongs to."""
    conn, pid = db
    stage_roads(conn, pid, [_road("way/9", None, "202")])
    row = conn.execute("SELECT text, cad_layer FROM cad_labels WHERE"
                       " feature_class = 'road_ref'").fetchone()
    assert row["text"] == "ทล.202"
    assert row["cad_layer"] == "C-ANNO-TEXT-TH"


def test_named_road_ref_is_a_bare_number_on_the_english_layer(db):
    conn, pid = db
    stage_roads(conn, pid, [_road("way/9", "ถนนอรุณประเสริฐ", "202")])
    row = conn.execute("SELECT text, cad_layer FROM cad_labels WHERE"
                       " feature_class = 'road_ref'").fetchone()
    assert row["text"] == "202"
    assert row["cad_layer"] == "C-ANNO-TEXT-EN"


def test_bilingual_road_ref_clears_both_name_labels(db):
    """With a Thai and an English name stacked at the anchor, the route
    number has to clear the taller stack or it overprints the English."""
    conn, pid = db
    stage_roads(conn, pid, [_road("way/9", "ถนนอรุณประเสริฐ", "202",
                                  th="ถนนอรุณประเสริฐ",
                                  en="Arun Prasoet Road")])
    offsets = {r["feature_class"] + ":" + r["cad_layer"]: r["label_offset"]
               for r in conn.execute("SELECT feature_class, cad_layer,"
                                     " label_offset FROM cad_labels")}
    assert offsets["road_name:C-ANNO-TEXT-TH"] == 0.0
    assert offsets["road_name:C-ANNO-TEXT-EN"] == pytest.approx(5.0 * 1.3)
    assert offsets["road_ref:C-ANNO-TEXT-EN"] == pytest.approx(6.0 + 5.0 * 1.3)


def test_verified_thai_name_moves_off_the_neutral_layer(db):
    """Someone walks out, confirms a coded building is 'ศาลาประชาคม', and
    expects it typeset in Thai — not left on the code's neutral layer."""
    conn, pid = db
    stage_buildings(conn, pid, [_one_building()])
    record_verified(conn, pid, "ms/00003", "ศาลาประชาคม")
    conn.commit()
    assert apply_verified(conn, pid) == 1
    row = conn.execute("SELECT text, cad_layer FROM cad_labels").fetchone()
    assert row["text"] == "ศาลาประชาคม"
    assert row["cad_layer"] == "C-ANNO-TEXT-TH"


def test_migration_backfills_an_existing_staging_file(db):
    """An older staging database has no name_th/name_en. Until it is
    backfilled every name falls through to the neutral layer, which is a
    degraded drawing rather than a lost one; after the backfill each name is
    typeset in its own script."""
    from stage_db import _backfill_languages

    conn, pid = db
    stage_buildings(conn, pid, [
        {**_one_building("way/1", "โรงเรียนบ้านนา", ""),
         "osm_name": "โรงเรียนบ้านนา"}])
    stage_roads(conn, pid, [_road("way/9", "ถนนอรุณประเสริฐ", None)])
    # simulate the pre-migration file
    for table in ("staging_buildings", "staging_roads"):
        conn.execute(f"UPDATE {table} SET name_th = NULL, name_en = NULL")
    conn.commit()
    before = conn.execute("SELECT text, cad_layer FROM cad_labels").fetchall()
    # nothing is dropped — both names survive on the neutral layer
    assert sorted(r["text"] for r in before) == \
        ["ถนนอรุณประเสริฐ", "โรงเรียนบ้านนา"]
    assert {r["cad_layer"] for r in before} == {"C-ANNO-TEXT"}

    migrate(conn)     # columns already exist, so this is the backfill path
    _backfill_languages(conn)
    conn.commit()
    rows = conn.execute("SELECT text, cad_layer FROM cad_labels"
                        " ORDER BY text").fetchall()
    assert {r["text"]: r["cad_layer"] for r in rows} == {
        "โรงเรียนบ้านนา": "C-ANNO-TEXT-TH",
        "ถนนอรุณประเสริฐ": "C-ANNO-TEXT-TH",
    }
