"""Tests for the SQLite staging layer (scripts/stage_db.py).

These run entirely on in-memory databases and synthetic geometry — no
network, no DEM.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import stage_db  # noqa: E402
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
    assert [r["text"] for r in refs] == ["ทล.202"]


def _unnamed(i):
    """An ML footprint as topo2cad.py actually stages one: a code, and no
    name of any kind. display_name holds a *name*, so it stays empty."""
    from shapely.geometry import box

    return {"feature_id": f"ms/{i:05d}", "source": "microsoft_ml",
            "osm_name": "", "code": f"B{i:03d}", "display_name": "",
            "building_type": None, "geom": box(i * 20, 0, i * 20 + 10, 8)}


def test_cad_labels_covers_every_building(db):
    """Every unnamed footprint gets a label — its B### code. Where OSM
    names nothing (0 of 239 at Yasothon), a names-only rule would leave the
    whole building layer mute, and the inventory CSV would be keyed on
    codes that appear nowhere on the sheet."""
    conn, pid = db
    stage_buildings(conn, pid, [_unnamed(i) for i in range(5)])
    rows = conn.execute(
        "SELECT text, cad_layer, text_height FROM cad_labels"
        " WHERE feature_class = 'building_code'").fetchall()
    assert sorted(r["text"] for r in rows) == [f"B{i:03d}" for i in range(5)]
    # Neutral layer: a code is neither Thai nor English, and at a rural
    # site every building label is one — filing them as English would
    # blank a Thai-only plot entirely.
    assert {r["cad_layer"] for r in rows} == {"C-ANNO-TEXT"}
    # Same height a name gets, so the two routes place it identically
    assert {r["text_height"] for r in rows} == {3.5}


def test_a_code_label_sits_where_the_name_label_would(db):
    """The code rides on the building's own interior anchor, so a footprint
    that later gains a name keeps its label in the same place."""
    conn, pid = db
    stage_buildings(conn, pid, [_unnamed(0)])
    code = conn.execute("SELECT label_x, label_y, label_rotation,"
                        " label_offset FROM cad_labels").fetchone()
    anchor = conn.execute("SELECT label_x, label_y FROM staging_buildings"
                          ).fetchone()
    assert (code["label_x"], code["label_y"]) == (anchor["label_x"],
                                                  anchor["label_y"])
    assert code["label_offset"] == 0.0


def test_a_name_wins_over_the_code(db):
    """A named building is labelled by its name and never doubly by its
    code — the two would print on top of each other at one anchor."""
    conn, pid = db
    rec = _unnamed(0) | {"osm_name": "\u0e42\u0e23\u0e07\u0e40\u0e23\u0e35\u0e22\u0e19",
                         "display_name": "\u0e42\u0e23\u0e07\u0e40\u0e23\u0e35\u0e22\u0e19"}
    stage_buildings(conn, pid, [rec])
    classes = [r["feature_class"] for r in
               conn.execute("SELECT feature_class FROM cad_labels")]
    assert "building_code" not in classes
    assert classes == ["building"]


def test_a_verified_name_replaces_the_code_on_the_sheet(db):
    """The revision path: a field crew reads B001 off the plot, sets the
    name, and the re-issue draws the name where the code was."""
    conn, pid = db
    stage_buildings(conn, pid, [_unnamed(0)])
    assert conn.execute("SELECT COUNT(*) FROM cad_labels WHERE"
                        " feature_class = 'building_code'").fetchone()[0] == 1
    record_verified(conn, pid, "ms/00000",
                    "\u0e27\u0e31\u0e14\u0e1b\u0e48\u0e32")
    apply_verified(conn, pid)
    rows = conn.execute("SELECT feature_class, text, cad_layer"
                        " FROM cad_labels").fetchall()
    assert [r["feature_class"] for r in rows] == ["building"]
    assert rows[0]["cad_layer"] == "C-ANNO-TEXT-TH"


def test_a_landmark_area_gets_no_code(db):
    """Landmark areas ride in staging_buildings with their own cad_layer
    and no code; a car park labelled B007 would read as a structure."""
    from shapely.geometry import box

    conn, pid = db
    stage_buildings(conn, pid, [
        {"feature_id": "way/1", "source": "openstreetmap", "osm_name": "",
         "code": "", "display_name": "", "building_type": None,
         "cad_layer": "C-SITE-POI", "geom": box(0, 0, 30, 20)}])
    assert conn.execute("SELECT COUNT(*) FROM cad_labels").fetchone()[0] == 0


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


def test_a_route_number_always_reads_as_a_highway_designation(db):
    """A named road used to show a bare "202" beside its name, which reads
    as a distance, a lane count or a house number. ทล.202 is what the
    number is, so it carries the prefix whether the road is named or not —
    and that puts it on the Thai layer either way."""
    conn, pid = db
    stage_roads(conn, pid, [_road("way/9", "ถนนอรุณประเสริฐ", "202")])
    row = conn.execute("SELECT text, cad_layer FROM cad_labels WHERE"
                       " feature_class = 'road_ref'").fetchone()
    assert row["text"] == "ทล.202"
    assert row["cad_layer"] == "C-ANNO-TEXT-TH"


def test_an_unnamed_road_carries_the_same_prefix(db):
    conn, pid = db
    stage_roads(conn, pid, [_road("way/9", None, "202")])
    row = conn.execute("SELECT text, cad_layer, label_offset FROM cad_labels"
                       " WHERE feature_class = 'road_ref'").fetchone()
    assert row["text"] == "ทล.202"
    # nothing above it to clear, so it sits on the anchor
    assert row["label_offset"] == 0.0


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
    # The ref sits on the Thai layer now that it always carries ทล.; what
    # matters is unchanged — it clears the taller of the two name lines.
    assert offsets["road_ref:C-ANNO-TEXT-TH"] == pytest.approx(6.0 + 5.0 * 1.3)


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


# ------------------------------------------------------------- landmarks
def _poi(fid="node/1", name="ศาลพระภูมิ", x=100.0, y=200.0,
         key="historic", typ="shrine", **kw):
    return {"feature_id": fid, "x": x, "y": y, "poi_key": key,
            "poi_type": typ, "display_name": name, **kw}


def test_poi_symbol_and_label_are_separate_points(db):
    """The circle marks the landmark; the name has to sit clear of it, so
    the anchor is nudged at staging time rather than by the CAD writer."""
    from shapely import wkb
    from stage_db import POI_LABEL_DX, stage_pois

    conn, pid = db
    stage_pois(conn, pid, [_poi()])
    row = conn.execute("SELECT geom_wkb, label_x, label_y FROM"
                       " staging_pois").fetchone()
    pt = wkb.loads(row["geom_wkb"])
    assert (pt.x, pt.y) == (100.0, 200.0)          # symbol on the landmark
    assert row["label_x"] == 100.0 + POI_LABEL_DX  # name clear of it
    assert row["label_y"] == 200.0


def test_poi_label_routes_by_script(db):
    from stage_db import stage_pois

    conn, pid = db
    stage_pois(conn, pid, [_poi("node/1", "ศาลพระภูมิ"),
                           _poi("node/2", "Erawan Shrine", x=50.0)])
    rows = conn.execute("SELECT text, cad_layer FROM cad_labels WHERE"
                        " feature_class = 'poi' ORDER BY text").fetchall()
    assert {r["text"]: r["cad_layer"] for r in rows} == {
        "ศาลพระภูมิ": "C-ANNO-TEXT-TH",
        "Erawan Shrine": "C-ANNO-TEXT-EN",
    }


def test_bilingual_poi_stacks_english_above_thai(db):
    from stage_db import stage_pois

    conn, pid = db
    stage_pois(conn, pid, [_poi(name_th="ศาลพระภูมิ",
                                name_en="Spirit House")])
    rows = conn.execute("SELECT text, label_offset FROM cad_labels WHERE"
                        " feature_class = 'poi' ORDER BY label_offset"
                        ).fetchall()
    assert [r["text"] for r in rows] == ["ศาลพระภูมิ", "Spirit House"]
    assert rows[0]["label_offset"] == 0.0
    assert rows[1]["label_offset"] == pytest.approx(4.0 * 1.3)


def test_landmark_area_stays_off_the_building_layer(db):
    """A hospital campus or car park needs a polygon, an interior anchor and
    an area — so it rides in staging_buildings — but a 3,000 m2 car park
    must not read as a structure on C-BLDG-OUTL."""
    from shapely.geometry import box

    conn, pid = db
    stage_buildings(conn, pid, [
        {"feature_id": "way/1", "source": "openstreetmap",
         "osm_name": "สำนักงานตำรวจแห่งชาติ", "code": "",
         "display_name": "สำนักงานตำรวจแห่งชาติ", "building_type": None,
         "cad_layer": "C-SITE-POI", "geom": box(0, 0, 200, 250)},
        {"feature_id": "way/2", "source": "openstreetmap", "osm_name": "",
         "code": "B001", "display_name": "B001", "building_type": None,
         "geom": box(10, 10, 20, 18)},
    ])
    layers = dict(conn.execute("SELECT cad_layer, COUNT(*) FROM"
                               " staging_buildings GROUP BY 1").fetchall())
    assert layers == {"C-SITE-POI": 1, "C-BLDG-OUTL": 1}
    # ...and it still gets its name, on the Thai layer
    row = conn.execute("SELECT cad_layer FROM cad_labels WHERE text = ?",
                       ("สำนักงานตำรวจแห่งชาติ",)).fetchone()
    assert row["cad_layer"] == "C-ANNO-TEXT-TH"


def test_unnamed_landmark_area_draws_but_does_not_label(db):
    from shapely.geometry import box

    conn, pid = db
    stage_buildings(conn, pid, [
        {"feature_id": "way/3", "source": "openstreetmap", "osm_name": "",
         "code": "", "display_name": "", "building_type": None,
         "cad_layer": "C-SITE-POI", "geom": box(0, 0, 30, 30)}])
    assert conn.execute("SELECT COUNT(*) FROM staging_buildings"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM cad_labels").fetchone()[0] == 0


# --------------------------------------------------------- context linework
def _ctx(fid="way/7", kind="water", layer="C-HYDR-WATR", name="คลองอรชร",
         runs=None, labelled=True, **kw):
    return {"feature_id": fid, "kind": kind, "cad_layer": layer,
            "display_name": name, "labelled": labelled,
            "runs": runs if runs is not None else [[(0, 0), (100, 0)]], **kw}


def test_context_multi_run_feature_redraws_as_many_polylines(db):
    """Clipping the extent can split one canal into several runs. Each run
    was drawn as its own polyline, so each has to come back as one."""
    from shapely import wkb
    from stage_db import stage_context

    conn, pid = db
    stage_context(conn, pid, [_ctx(runs=[[(0, 0), (50, 0)],
                                         [(80, 0), (200, 0)]])])
    geom = wkb.loads(conn.execute("SELECT geom_wkb FROM staging_context"
                                  ).fetchone()[0])
    assert geom.geom_type == "MultiLineString"
    assert len(geom.geoms) == 2


def test_context_closed_ring_survives_the_round_trip(db):
    """A pond or park boundary is drawn as a closed polyline. The flag is
    recovered from the coordinates, so the ring must stay closed in WKB."""
    from shapely import wkb
    from stage_db import stage_context

    conn, pid = db
    ring = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    stage_context(conn, pid, [_ctx(kind="green", layer="C-LAND-VEGT",
                                   name="สวนสาธารณะ", runs=[ring])])
    coords = list(wkb.loads(
        conn.execute("SELECT geom_wkb FROM staging_context").fetchone()[0]
    ).coords)
    assert coords[0] == coords[-1]


def test_rail_and_barrier_stage_but_never_label(db):
    """topo2cad.py draws these without a name, so they must not acquire one
    on the way through staging."""
    from stage_db import stage_context

    conn, pid = db
    stage_context(conn, pid, [
        _ctx("way/1", "rail", "C-RAIL-TRAK", "ทางรถไฟสายเหนือ",
             labelled=False),
        _ctx("way/2", "barrier", "C-BNDY-BARR", "รั้ว", labelled=False),
    ])
    assert conn.execute("SELECT COUNT(*) FROM staging_context"
                        ).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM staging_context WHERE"
                        " label_x IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM cad_labels WHERE"
                        " feature_class = 'context'").fetchone()[0] == 0


def test_context_label_dedupes_within_its_kind(db):
    """One canal mapped as several ways gets one label, on the longest —
    the same rule roads follow."""
    from stage_db import stage_context

    conn, pid = db
    stage_context(conn, pid, [
        _ctx("way/1", runs=[[(0, 0), (100, 0)]]),
        _ctx("way/2", runs=[[(0, 8), (500, 8)]]),
    ])
    rows = conn.execute("SELECT text, label_y FROM cad_labels WHERE"
                        " feature_class = 'context'").fetchall()
    assert [r["text"] for r in rows] == ["คลองอรชร"]
    assert rows[0]["label_y"] == 8          # anchored on the longer way


def test_context_label_routes_by_script(db):
    from stage_db import stage_context

    conn, pid = db
    stage_context(conn, pid, [
        _ctx("way/1", name="คลองอรชร"),
        _ctx("way/2", kind="green", layer="C-LAND-VEGT", name="Skyscape",
             runs=[[(0, 40), (60, 40)]]),
    ])
    rows = conn.execute("SELECT text, cad_layer FROM cad_labels WHERE"
                        " feature_class = 'context'").fetchall()
    assert {r["text"]: r["cad_layer"] for r in rows} == {
        "คลองอรชร": "C-ANNO-TEXT-TH",
        "Skyscape": "C-ANNO-TEXT-EN",
    }


def test_unnamed_context_draws_without_a_label(db):
    from stage_db import stage_context

    conn, pid = db
    stage_context(conn, pid, [_ctx(name="")])
    assert conn.execute("SELECT COUNT(*) FROM staging_context"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM cad_labels WHERE"
                        " feature_class = 'context'").fetchone()[0] == 0


# ------------------------------------------------------ building holes
def test_courtyard_survives_staging_and_redraw(db):
    """A multipolygon building carries its courtyard as an inner ring.
    Storing only the exterior draws a temple or a mall with its atrium
    filled in solid."""
    from shapely import wkb
    from shapely.geometry import Polygon

    conn, pid = db
    outer = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]
    hole = [(40, 40), (60, 40), (60, 60), (40, 60), (40, 40)]
    stage_buildings(conn, pid, [{
        "feature_id": "relation/1", "source": "openstreetmap",
        "osm_name": "วัดมีลานกลาง", "code": "", "display_name": "วัดมีลานกลาง",
        "building_type": None, "geom": Polygon(outer, [hole])}])
    back = wkb.loads(conn.execute("SELECT geom_wkb FROM staging_buildings"
                                  ).fetchone()[0])
    assert len(back.interiors) == 1
    assert back.area == pytest.approx(100 * 100 - 20 * 20)


def test_label_anchor_avoids_the_courtyard(db):
    """representative_point() on a holed polygon must land in the built
    part, not in the open middle where the label would sit on nothing."""
    from shapely.geometry import Point, Polygon

    conn, pid = db
    outer = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]
    hole = [(20, 20), (80, 20), (80, 80), (20, 80), (20, 20)]
    poly = Polygon(outer, [hole])
    stage_buildings(conn, pid, [{
        "feature_id": "relation/2", "source": "openstreetmap",
        "osm_name": "", "code": "B001", "display_name": "B001",
        "building_type": None, "geom": poly}])
    r = conn.execute("SELECT label_x, label_y FROM staging_buildings"
                     ).fetchone()
    anchor = Point(r["label_x"], r["label_y"])
    assert poly.contains(anchor)          # inside the ring of building...
    assert not Polygon(hole).contains(anchor)   # ...not in the courtyard


# ------------------------------------------------- one-way direction arrows
def test_arrow_positions_space_along_the_line_not_per_vertex():
    """An OSM way carries a vertex every few metres through a curve, so
    per-vertex arrows pile up on bends and vanish on straights."""
    line = [(0, 0), (10, 0), (20, 0), (200, 0)]
    marks = stage_db.arrow_positions(line, spacing=60.0)
    xs = [round(x, 3) for x, _y, _rot in marks]
    assert xs == [30.0, 90.0, 150.0]          # first at half a spacing in
    assert all(abs(rot) < 1e-9 for _x, _y, rot in marks)   # due east


def test_arrow_positions_bearing_follows_the_geometry():
    north = stage_db.arrow_positions([(0, 0), (0, 100)], spacing=60.0)
    assert north[0][2] == pytest.approx(90.0)
    west = stage_db.arrow_positions([(100, 0), (0, 0)], spacing=60.0)
    assert abs(west[0][2]) == pytest.approx(180.0)


def test_arrow_positions_short_runs():
    """Too short to read one; long enough for exactly one at the midpoint."""
    assert stage_db.arrow_positions([(0, 0), (5, 0)]) == []
    mid = stage_db.arrow_positions([(0, 0), (40, 0)], spacing=60.0)
    assert len(mid) == 1 and mid[0][0] == pytest.approx(20.0)


def test_arrow_positions_ignores_degenerate_input():
    assert stage_db.arrow_positions([(0, 0)]) == []
    assert stage_db.arrow_positions([(0, 0), (0, 0)]) == []


def test_oneway_arrow_size_is_clamped_both_ways():
    """A 14 m motorway must not get a 14 m arrow, nor a 3 m alley an
    invisible one."""
    assert stage_db.oneway_arrow_size(6.0) == 6.0
    assert stage_db.oneway_arrow_size(14.0) == stage_db.ONEWAY_ARROW_MAX_M
    assert stage_db.oneway_arrow_size(1.5) == stage_db.ONEWAY_ARROW_MIN_M
    assert stage_db.oneway_arrow_size(None) == stage_db.ONEWAY_ARROW_MIN_M


def test_oneway_is_staged_and_survives_a_reopen(tmp_path):
    from shapely.geometry import LineString

    db = tmp_path / "s.sqlite"
    conn = stage_db.connect(db)
    pid = stage_db.create_project(conn, "p", 13.7, 100.5, 500, 400, 32647)
    stage_db.stage_roads(conn, pid, [
        {"feature_id": "way/1", "highway_type": "primary", "road_name": "A",
         "road_ref": None, "carriageway_m": 10.0, "oneway": -1,
         "geom": LineString([(0, 0), (100, 0)])},
        {"feature_id": "way/2", "highway_type": "residential",
         "road_name": "B", "road_ref": None, "carriageway_m": 6.0,
         "geom": LineString([(0, 10), (100, 10)])}])
    conn.close()
    conn = stage_db.connect(db)
    rows = dict(conn.execute("SELECT feature_id, oneway FROM staging_roads"))
    conn.close()
    # Absent means two-way, not NULL: db2dxf tests it directly
    assert rows == {"way/1": -1, "way/2": 0}


def test_repaired_polygon_splits_a_self_intersecting_ring():
    """A ring that closes back on itself — OSM has them; จุฬาลงกรณ์
    มหาวิทยาลัย is one — becomes two polygons under buffer(0). Both CAD
    routes draw this repaired shape, so neither can disagree about how many
    outlines there are or where the label goes."""
    figure_eight = [(0, 0), (4, 0), (4, 4), (0, 4), (0, 0),
                    (6, 0), (10, 0), (10, 4), (6, 4), (6, 0)]
    shape = stage_db.repaired_polygon(figure_eight)
    assert shape.geom_type == "MultiPolygon"
    assert len(stage_db.polygon_parts(shape)) == 2
    from shapely.geometry import Point

    x, y = stage_db.interior_point(shape)
    assert shape.contains(Point(x, y))


def test_repaired_polygon_leaves_a_valid_ring_alone():
    square = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    shape = stage_db.repaired_polygon(square)
    assert shape.geom_type == "Polygon"
    assert stage_db.polygon_parts(shape) == [shape]
    assert shape.area == pytest.approx(100.0)


# ---------------------------------------------------- source attributes
def test_re_extraction_clears_every_staged_table():
    """staging_pois and staging_context were added after the delete list was
    written and were never cleared, so re-running a site at a smaller extent
    left the old run's landmarks and canals in the database — and db2dxf
    drew them, outside the new extent."""
    from shapely.geometry import LineString

    conn = connect(":memory:")
    pid = create_project(conn, "p", 13.7, 100.5, 500, 400, 32647)
    stage_db.stage_pois(conn, pid, [
        {"feature_id": "node/1", "poi_key": "amenity", "poi_type": "school",
         "name_th": "ก", "name_en": "", "display_name": "ก",
         "x": 0, "y": 0, "latitude": 13.7, "longitude": 100.5}])
    stage_db.stage_context(conn, pid, [
        {"feature_id": "way/9", "kind": "water", "cad_layer": "C-HYDR-WATR",
         "name_th": "", "name_en": "", "display_name": "", "labelled": False,
         "runs": [[(0, 0), (10, 0)]]}])
    stage_db.stage_roads(conn, pid, [
        {"feature_id": "way/1", "highway_type": "residential",
         "road_name": None, "road_ref": None, "carriageway_m": 6.0,
         "geom": LineString([(0, 0), (50, 0)])}])
    stage_db.stage_tags(conn, pid, [
        {"feature_id": "way/1", "feature_type": "road",
         "cad_layer": "C-ROAD-CNTR", "display_name": "",
         "key": "highway", "value": "residential"}])

    assert create_project(conn, "p", 13.7, 100.5, 200, 150, 32647) == pid
    left = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in stage_db.STAGED_TABLES}
    assert left == {t: 0 for t in stage_db.STAGED_TABLES}


def test_staged_tags_round_trip_into_xdata():
    conn = connect(":memory:")
    pid = create_project(conn, "p", 13.7, 100.5, 500, 400, 32647)
    stage_db.stage_tags(conn, pid, [
        {"feature_id": "way/1", "feature_type": "building",
         "cad_layer": "C-BLDG-OUTL", "display_name": "B001",
         "key": "building", "value": "yes"},
        {"feature_id": "way/1", "feature_type": "building",
         "cad_layer": "C-BLDG-OUTL", "display_name": "B001",
         "key": "name", "value": "ตลาด"}])
    tags = stage_db.tags_by_feature(conn, pid)
    assert tags == {"way/1": ("OSM", {"building": "yes", "name": "ตลาด"})}
    # ...and that is what a writer turns back into XDATA, id first
    assert stage_db.xdata_tags("way/1", tags["way/1"][1]) == [
        (1000, "@id=way/1"), (1000, "building=yes"), (1000, "name=ตลาด")]


def test_xdata_clips_on_bytes_not_characters():
    """Group code 1000 caps at 255 bytes and Thai is three bytes a
    character, so a character-count clip would still overrun."""
    value = stage_db.xdata_tags("n/1", {"name": "ก" * 300})[1][1]
    assert len(value.encode("utf-8")) <= 255


def test_attribute_rows_only_cover_what_was_drawn():
    drawn = [{"feature_id": "way/1", "feature_type": "building",
              "cad_layer": "C-BLDG-OUTL", "display_name": "B001"}]
    rows = stage_db.attribute_rows(
        drawn, {"way/1": {"b": "2", "a": "1"}, "way/9": {"x": "y"}})
    assert [(r["feature_id"], r["key"]) for r in rows] == [
        ("way/1", "a"), ("way/1", "b")]


def test_write_attribute_csv_keeps_the_agreed_columns(tmp_path):
    out = tmp_path / "attributes.csv"
    stage_db.write_attribute_csv(out, [
        {"feature_id": "way/1", "feature_type": "road",
         "cad_layer": "C-ROAD-CNTR", "display_name": "ถนน",
         "key": "highway", "value": "primary", "extra": "ignored"}])
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(stage_db.ATTR_FIELDS)
    assert lines[1].startswith("way/1,road,C-ROAD-CNTR,ถนน,highway,primary")


def test_tags_keep_their_application_id_per_feature():
    """One project can hold an OSM extraction and a survey import. A
    re-issue must put a shapefile's DBF columns back under GIS, not relabel
    them as OpenStreetMap tags in the CAD attribute browser."""
    conn = connect(":memory:")
    pid = create_project(conn, "p", 13.7, 100.5, 500, 400, 32647)
    stage_db.stage_tags(conn, pid, [
        {"feature_id": "way/1", "feature_type": "building",
         "cad_layer": "C-BLDG-OUTL", "display_name": "",
         "key": "building", "value": "yes"}])
    stage_db.stage_tags(conn, pid, [
        {"feature_id": "gis/plots/00000", "feature_type": "polygon",
         "cad_layer": "C-BLDG-OUTL", "display_name": "แปลง A",
         "key": "PLOT_NO", "value": "12/3"}],
        appid=stage_db.GIS_XDATA_APPID)
    tags = stage_db.tags_by_feature(conn, pid)
    assert tags["way/1"] == ("OSM", {"building": "yes"})
    assert tags["gis/plots/00000"] == ("GIS", {"PLOT_NO": "12/3"})


def test_appid_defaults_to_osm_for_an_older_database():
    """The column arrived after the table did, so MIGRATIONS backfills it —
    everything staged before the split came from OpenStreetMap."""
    conn = connect(":memory:")
    assert ("appid", "TEXT NOT NULL DEFAULT 'OSM'") in \
        stage_db.MIGRATIONS["staging_tags"]
    pid = create_project(conn, "p", 13.7, 100.5, 500, 400, 32647)
    conn.execute("INSERT INTO staging_tags (project_id, feature_id,"
                 " feature_type, cad_layer, key, value)"
                 " VALUES (?,?,?,?,?,?)",
                 (pid, "way/1", "building", "C-BLDG-OUTL", "building", "yes"))
    conn.commit()
    assert stage_db.tags_by_feature(conn, pid)["way/1"][0] == "OSM"


# ------------------------------------------------ spot heights and hatching
def test_spot_grid_insets_from_the_extent():
    """The numbers must not land on the crop line, where the frame or the
    title block sits on them."""
    pts = stage_db.spot_grid(0, 0, 100, 60, columns=3, rows_n=2)
    assert len(pts) == 6
    assert all(0 < x < 100 and 0 < y < 60 for x, y in pts)
    xs = sorted({round(x, 6) for x, _ in pts})
    assert xs == [25.0, 50.0, 75.0]          # evenly spaced, none on an edge


def test_spot_grid_handles_a_degenerate_request():
    assert stage_db.spot_grid(0, 0, 10, 10, columns=0, rows_n=3) == []


def test_spots_round_trip_and_are_cleared_on_re_extraction():
    conn = connect(":memory:")
    pid = create_project(conn, "p", 13.7, 100.5, 500, 400, 32647)
    assert stage_db.stage_spots(conn, pid, [
        {"x": 10.0, "y": 20.0, "elevation_m": 12.5},
        {"x": 30.0, "y": 20.0, "elevation_m": -0.4}]) == 2
    rows = [tuple(r) for r in conn.execute(
        "SELECT x, y, elevation_m, cad_layer FROM staging_spots ORDER BY x")]
    assert rows == [(10.0, 20.0, 12.5, "C-TOPO-SPOT"),
                    (30.0, 20.0, -0.4, "C-TOPO-SPOT")]
    # a re-extraction must not leave last run's levels behind
    create_project(conn, "p", 13.7, 100.5, 200, 150, 32647)
    assert conn.execute("SELECT COUNT(*) FROM staging_spots").fetchone()[0] == 0


def test_hatch_patterns_cover_the_kinds_that_are_areas():
    """Rail and barrier are lines; hatching them would fill a fence."""
    assert set(stage_db.HATCH_PATTERNS) == {"water", "green"}
    for pattern, scale in stage_db.HATCH_PATTERNS.values():
        assert isinstance(pattern, str) and scale > 0


# --------------------------------------------------------- building storeys
@pytest.mark.parametrize("tags,expected", [
    ({"building:levels": "3"}, "3F"),
    ({"building:levels": "12"}, "12F"),
    ({"height": "12"}, "12.0 m"),
    ({"height": "12.5 m"}, "12.5 m"),
    # a storey count is what a plan annotates, so it wins over metres
    ({"building:levels": "3", "height": "9"}, "3F"),
    ({"height": "abc"}, ""),
    ({"building:levels": "900"}, ""),      # not a building, a typo
    ({"height": "0"}, ""),
    ({}, ""),
])
def test_levels_label(tags, expected):
    assert stage_db.levels_label(tags) == expected


def test_levels_label_is_stored_formatted_not_recomputed():
    """One spelling of the convention: the column holds what both writers
    draw, rather than a rule in Python and the same rule again in SQL."""
    conn = connect(":memory:")
    pid = create_project(conn, "p", 13.7, 100.5, 500, 400, 32647)
    from shapely.geometry import box
    stage_buildings(conn, pid, [
        {"feature_id": "way/1", "source": "openstreetmap", "osm_name": "",
         "code": "B001", "display_name": "B001", "building_type": None,
         "addr_house": "99/1", "levels_label": "3F", "geom": box(0, 0, 10, 8)}])
    rows = {r["feature_class"]: (r["text"], r["cad_layer"], r["label_offset"])
            for r in conn.execute("SELECT * FROM cad_labels")}
    assert rows["building_addr"] == ("99/1", "C-ANNO-ADDR", -3.0)
    assert rows["building_levels"] == ("3F", "C-ANNO-ADDR", -5.4)


# -------------------------------------------------------- coordinate grid
@pytest.mark.parametrize("width,height,expected", [
    (400, 300, 100.0),
    (1000, 750, 200.0),
    (8000, 8000, 2000.0),
    (100, 80, 20.0),      # ideal 16.7 -> the next round step up
])
def test_grid_spacing_is_a_round_number(width, height, expected):
    """A grid at 137 m is a grid nobody can read a coordinate off."""
    assert stage_db.grid_spacing(width, height) == expected
    assert stage_db.grid_spacing(width, height) in stage_db.GRID_STEPS


def test_grid_ticks_land_on_round_utm_values():
    """665,700 E is a number a surveyor can use; 665,694.02 is not."""
    east, north = stage_db.grid_ticks(665694.02, 1520106.78, 400, 300, 100.0)
    assert east == [665500.0, 665600.0, 665700.0, 665800.0]
    assert north == [1520000.0, 1520100.0, 1520200.0]
    # every tick inside the extent
    assert all(665494.02 <= x <= 665894.02 for x in east)
    assert all(1519956.78 <= y <= 1520256.78 for y in north)


def test_grid_ticks_handle_a_spacing_wider_than_the_extent():
    east, north = stage_db.grid_ticks(665694.02, 1520106.78, 50, 40, 1000.0)
    assert east == [] and north == []


def test_grid_ticks_reject_a_nonsense_spacing():
    assert stage_db.grid_ticks(0, 0, 100, 100, 0) == ([], [])
    assert stage_db.grid_ticks(0, 0, 100, 100, -5) == ([], [])


# --- One layer table, shared ------------------------------------------------

def test_the_layer_table_and_its_linetypes_live_in_one_place():
    """gis2cad.py carried a reduced copy of the table, which is how an
    imported centreline drew Continuous there and CENTER in its own
    re-issue — and why an import was missing 27 layers."""
    import importlib.util as iu

    scripts = Path(__file__).resolve().parent.parent / "scripts"

    def load(name):
        spec = iu.spec_from_file_location(name, scripts / f"{name}.py")
        mod = iu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    blocks = load("blocks")
    db2dxf = load("db2dxf")
    gis2cad_src = (scripts / "gis2cad.py").read_text(encoding="utf-8")

    db2dxf_src = (scripts / "db2dxf.py").read_text(encoding="utf-8")
    # Equality, not identity: loading blocks through its own spec here
    # makes a second module object. What matters is that neither writer
    # defines a table of its own.
    assert db2dxf.LAYER_STYLE == blocks.LAYER_STYLE
    assert "LAYER_STYLE = blocks.LAYER_STYLE" in db2dxf_src
    assert "LAYER_STYLE = blocks.LAYER_STYLE" in gis2cad_src
    # A centreline is never drawn as a plain line: that is what stops it
    # being read as the edge of pavement beside it.
    assert blocks.LAYER_LINETYPE["C-ROAD-CNTR"] == "CENTER"
    # The pattern is in metres, so without a scale the dashes are
    # sub-millimetre on paper and read as continuous.
    assert blocks.LTSCALE == 5.0


# --- Boundary corner table (setting-out) ------------------------------------

def test_corner_bearings_are_north_based_and_clockwise(tmp_path):
    """A survey table states grid bearings from north, clockwise — what a
    total station is set to. The maths is atan2(dE, dN), not the
    atan2(dy, dx) a plotting library wants, and getting that backwards
    swaps east and north on every leg."""
    from shapely.geometry import Polygon

    rows = stage_db.corner_table(
        Polygon([(100, 200), (130, 200), (130, 220), (100, 220)]))
    assert [r["bearing"] for r in rows] == ["090°00'00\"", "000°00'00\"",
                                            "270°00'00\"", "180°00'00\""]
    assert [r["distance_m"] for r in rows] == [30.0, 20.0, 30.0, 20.0]


def test_the_closing_vertex_is_not_tabled_twice():
    """Shapely repeats the first coordinate to close a ring. Tabling it
    would list one corner twice and give the last leg zero length."""
    from shapely.geometry import Polygon

    poly = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
    assert len(list(poly.exterior.coords)) == 5
    assert len(stage_db.polygon_corners(poly)) == 4
    assert all(r["distance_m"] > 0 for r in stage_db.corner_table(poly))


def test_corner_labels_carry_the_parcel(tmp_path):
    from shapely.geometry import Polygon

    poly = Polygon([(0, 0), (1, 0), (1, 1)])
    assert [r["corner"] for r in stage_db.corner_table(poly, 0)] == \
        ["A1", "A2", "A3"]
    assert [r["corner"] for r in stage_db.corner_table(poly, 1)] == \
        ["B1", "B2", "B3"]
    # I and O are skipped: they read as 1 and 0 on a plotted sheet
    assert "I" not in stage_db.CORNER_LABELS
    assert "O" not in stage_db.CORNER_LABELS


def test_seconds_carry_instead_of_printing_sixty():
    """A bearing of 12°59'60" is not a bearing."""
    import math

    for angle in range(0, 360):
        for frac in (0.0, 0.99999, 0.5):
            deg = angle + frac
            x2 = math.sin(math.radians(deg))
            y2 = math.cos(math.radians(deg))
            text = stage_db.azimuth_dms(0, 0, x2, y2)
            assert "60\"" not in text, text
            assert "'60" not in text, text


def test_the_corner_csv_holds_what_a_setting_out_crew_needs(tmp_path):
    from shapely.geometry import Polygon

    rows = stage_db.corner_table(
        Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]), 0, "แปลง A")
    out = tmp_path / "corner_coordinates.csv"
    assert stage_db.write_corner_csv(out, rows) == 4
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == \
        "parcel,corner,easting,northing,bearing,distance_m"
    assert "แปลง A" in text


# ------------------------------------------------------- geometry hygiene
def test_a_closed_ring_does_not_repeat_its_first_vertex():
    """Shapely repeats the first vertex to close a ring and the DXF closed
    flag closes it again, so every polygon carried a zero-length closing
    segment — 49 of 49 footprints in a rural extract. OVERKILL strips them,
    an offset or a fillet trips over them, and every downstream tool has to
    special-case them."""
    square = [(0, 0), (10, 0), (10, 5), (0, 5), (0, 0)]
    assert stage_db.ring_points(square) == [(0.0, 0.0), (10.0, 0.0),
                                            (10.0, 5.0), (0.0, 5.0)]


def test_an_open_run_keeps_every_vertex():
    """Only the duplicate goes. A vertex a source actually recorded is that
    source's, not ours to remove."""
    run = [(0, 0), (10, 0), (10, 5)]
    assert stage_db.ring_points(run) == [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)]


def test_a_degenerate_ring_is_left_with_something_to_draw():
    """A ring of one repeated point must not come back empty: an entity with
    no vertices is a worse artefact than the duplicate was."""
    assert len(stage_db.ring_points([(1, 1), (1, 1), (1, 1)])) >= 1


def test_a_three_dimensional_ring_flattens_to_its_plan():
    ring = [(0, 0, 5), (10, 0, 6), (10, 5, 7), (0, 0, 5)]
    assert stage_db.ring_points(ring) == [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)]


def test_the_style_table_keeps_the_linetypes_and_drops_the_fonts():
    """setup=True installs 25 text styles for fonts nothing here draws
    with. The linetypes are the load-bearing half: CENTER on a centreline,
    DASHED on the crop line, PHANTOM on a right-of-way."""
    ezdxf = pytest.importorskip("ezdxf")

    doc = ezdxf.new("R2010", setup=stage_db.DXF_SETUP)
    names = {lt.dxf.name for lt in doc.linetypes}
    for needed in ("CENTER", "DASHED", "PHANTOM", "HIDDEN", "Continuous"):
        assert needed in names or needed == "HIDDEN", needed
    assert len(doc.styles) <= 2, "the font table came back"


def test_the_drawing_reports_its_own_extents():
    """A new document carries ezdxf's +/-1e20 sentinel and every writer
    shipped it unchanged, so a viewer that trusts the header opens on empty
    space. Note the value has to reach the *layout*: ezdxf's export copies
    $EXTMIN/$EXTMAX back from there, so setting the header alone looked
    like it worked and changed nothing in the file."""
    ezdxf = pytest.importorskip("ezdxf")

    doc = ezdxf.new("R2010", setup=stage_db.DXF_SETUP)
    assert stage_db.set_drawing_extents(doc) is False, "nothing to measure"
    doc.modelspace().add_lwpolyline([(10, 20), (30, 45)])
    assert stage_db.set_drawing_extents(doc) is True

    import io
    stream = io.StringIO()
    doc.write(stream)
    stream.seek(0)
    back = ezdxf.read(stream)
    assert back.header["$EXTMIN"][0] == pytest.approx(10)
    assert back.header["$EXTMAX"][1] == pytest.approx(45)


# ------------------------------------------------- junctions in the kerb
def _edge_lengths(result):
    from shapely.geometry import LineString

    return {k: sorted(round(LineString(e).length, 1) for e in v)
            for k, v in result.items()}


def test_a_crossroads_breaks_both_kerb_lines():
    """Each road is offset on its own, so the kerbs used to run straight
    through every junction — 197 edge/edge crossings over 500 x 400 m at
    Pathum Wan, each one a TRIM a drafter had to do by hand."""
    out = stage_db.carriageway_edges([
        ("ew", [(0, 0), (200, 0)], 6.0, True),
        ("ns", [(100, -80), (100, 80)], 6.0, True)])
    # 200 m of kerb less the 6 m the crossing carriageway occupies, in two
    # pieces either side of the opening traffic turns through
    assert _edge_lengths(out)["ew"] == [97.0, 97.0, 97.0, 97.0]
    assert _edge_lengths(out)["ns"] == [77.0, 77.0, 77.0, 77.0]


def test_one_road_split_into_two_ways_keeps_an_unbroken_kerb():
    """The flat cap is what makes this safe. A road split at a node is two
    OSM ways for OSM's convenience and one road on the ground; a round cap
    would eat half a carriageway width of kerb at every such joint, putting
    a gap in a straight road."""
    out = stage_db.carriageway_edges([
        ("west", [(0, 0), (100, 0)], 6.0, True),
        ("east", [(100, 0), (200, 0)], 6.0, True)])
    assert _edge_lengths(out) == {"west": [100.0, 100.0],
                                  "east": [100.0, 100.0]}


def test_a_bridge_is_not_trimmed_by_the_road_beneath_it():
    """A bridge crosses whatever is under it. Cutting the road below would
    draw a junction where there is none, and cutting the bridge would break
    a deck that never touches the ground there."""
    out = stage_db.carriageway_edges([
        ("road", [(0, 0), (200, 0)], 6.0, True),
        ("flyover", [(100, -80), (100, 80)], 8.0, False)])
    assert _edge_lengths(out)["road"] == [200.0, 200.0]
    assert _edge_lengths(out)["flyover"] == [160.0, 160.0]


def test_a_road_never_trims_itself():
    """Or the two ways of a divided carriageway would each erase the
    other's inner kerb."""
    out = stage_db.carriageway_edges([("solo", [(0, 0), (100, 0)], 8.0,
                                       True)])
    assert _edge_lengths(out) == {"solo": [100.0, 100.0]}


def test_a_path_has_no_kerb_to_trim():
    """A footway stages with carriageway_m = 0 — drawing a 1.5 m path with
    two kerb lines makes it read as a road — so it neither gains edges nor
    cuts anyone else's."""
    out = stage_db.carriageway_edges([
        ("road", [(0, 0), (200, 0)], 6.0, True),
        ("footway", [(100, -80), (100, 80)], 0.0, True)])
    assert out.get("footway", []) == []
    assert _edge_lengths(out)["road"] == [200.0, 200.0]


def test_a_stub_shorter_than_the_minimum_is_not_drawn():
    """A trimmed fragment of a few centimetres is noise, not kerb."""
    out = stage_db.carriageway_edges([
        ("stub", [(0, 0), (0.2, 0)], 6.0, True)])
    assert out.get("stub", []) == []


# ----------------------------------------------- annotation on the paper
def test_annotation_is_unchanged_at_the_scale_it_was_drawn_for():
    """Every height in this repo was picked to read at 1:1000 — a building
    name at 3.5 m plots at 3.5 mm. The factor must leave that alone, or the
    scale a site plan is actually read at changes for no reason."""
    assert stage_db.annotation_scale(1000) == 1.0


@pytest.mark.parametrize("scale", [500, 1000, 2000, 5000, 20000])
def test_a_label_plots_the_same_size_at_every_scale(scale):
    """Nothing rescaled annotation for the sheet, so the *default*
    1000 x 750 m extent on A3 — 1:5000 — plotted building names at 0.70 mm
    and house numbers at 0.44 mm. ISO 3098 sets 2.5 mm as the smallest
    drafting size, so the default sheet went out unreadable."""
    for height_m in (2.2, 2.5, 3.5, 4.0, 5.0):
        on_paper = height_m * stage_db.annotation_scale(scale) / scale * 1000
        assert on_paper == pytest.approx(height_m)
        assert on_paper >= 2.2


def test_a_drawing_with_no_sheet_keeps_the_reference_sizes():
    """Model space with no plot scale has nothing to scale to."""
    for missing in (None, "fit", "", 0):
        assert stage_db.annotation_scale(missing) == 1.0


def test_a_code_bigger_than_its_building_is_not_drawn():
    """Sizing annotation correctly made the real problem visible: 400-odd
    footprints each carrying a 3.5 mm code is a solid mass of overlapping
    text. A 4-character code 17.5 m tall needs ~42 m of footprint."""
    assert not stage_db.label_fits("B001", 17.5, 12.0, 10.0)
    assert stage_db.label_fits("B001", 3.5, 12.0, 10.0)


def test_a_code_needs_room_in_both_directions():
    """A long thin shed is wide enough for the text and not tall enough."""
    assert not stage_db.label_fits("B001", 5.0, 100.0, 3.0)


def test_a_label_with_nothing_to_fit_inside_is_kept():
    """The test only applies where a box is known; absent one, dropping the
    label would lose it for no reason."""
    assert stage_db.label_fits("B001", 17.5, None, None)
    assert stage_db.label_fits("", 3.5, 1.0, 1.0)


# ------------------------------------------- roads and landmarks as tables
def test_the_road_inventory_lists_every_way(db):
    """Buildings have had an inventory from the start and roads never did,
    so the thing most often wanted off a site plan — which road is which and
    what number it carries — meant opening the DXF and clicking a line."""
    from shapely.geometry import LineString

    conn, pid = db
    stage_roads(conn, pid, [
        {"feature_id": "way/1", "geom": LineString([(0, 0), (300, 0)]),
         "highway_type": "primary", "road_name": "ถนนลพบุรี-ชัยนาท",
         "road_ref": "311", "official_name": "ถนนพระรามที่ ๑",
         "carriageway_m": 14.0, "oneway": 0},
        {"feature_id": "way/2", "geom": LineString([(0, 40), (60, 40)]),
         "highway_type": "residential", "road_name": None, "road_ref": None,
         "carriageway_m": 6.0, "oneway": 0}])
    rows = stage_db.road_inventory_rows(conn, pid)
    assert [r["feature_id"] for r in rows] == ["way/1", "way/2"]  # longest 1st
    assert rows[0]["road_ref"] == "311"
    # The formal designation is a different string from the everyday name
    # and never reached anything a reader could open.
    assert rows[0]["official_name"] == "ถนนพระรามที่ ๑"
    assert rows[0]["length_m"] == pytest.approx(300.0)


def test_the_landmark_list_leaves_out_the_map_furniture(db):
    """staging_pois also carries trees, pylons and gates, which stage with
    an empty display_name precisely so they never grow a label. A list of
    สถานที่สำคัญใกล้เคียง that opens with ninety trees is not a list."""
    conn, pid = db
    stage_db.stage_pois(conn, pid, [
        {"feature_id": "node/1", "x": 30.0, "y": 40.0, "poi_key": "amenity",
         "poi_type": "school", "display_name": "โรงเรียนบ้านนา"},
        {"feature_id": "node/2", "x": 10.0, "y": 0.0, "poi_key": "natural",
         "poi_type": "tree", "display_name": "",
         "cad_layer": "C-LAND-TREE"}])
    rows = stage_db.poi_inventory_rows(conn, pid, centre=(0.0, 0.0))
    assert [r["feature_id"] for r in rows] == ["node/1"]
    assert rows[0]["kind_th"] == "โรงเรียน"
    assert rows[0]["distance_m"] == pytest.approx(50.0)


def test_landmark_bearings_are_north_based_and_clockwise():
    """atan2(dE, dN), not the atan2(dy, dx) a plotting library wants —
    that swaps east and north on every reading. Same convention as
    corner_table()."""
    assert stage_db.bearing_text(0, 100).startswith("000°")
    assert stage_db.bearing_text(100, 0).startswith("090°")
    assert stage_db.bearing_text(0, -100).startswith("180°")
    assert stage_db.bearing_text(-100, 0).startswith("270°")


def test_a_landmark_kind_reads_in_thai():
    """A ผังบริเวณ lists nearby places by kind, and "place_of_worship" is
    not that word. Overture adds taxonomy leaves between releases, so its
    categories are matched as substrings, longest first — or "school" would
    claim "language_school" before the more specific word got a look."""
    assert stage_db.poi_kind_thai("place_of_worship") == "วัด/ศาสนสถาน"
    assert stage_db.poi_kind_thai("school") == "โรงเรียน"
    assert stage_db.poi_kind_thai("language_school") == "โรงเรียนสอนภาษา"
    assert stage_db.poi_kind_thai("buddhist_temple") == "วัด"
    assert stage_db.poi_kind_thai("shopping_center") == "ศูนย์การค้า"
    # Nothing matched is left empty rather than guessed; the row still
    # carries its raw poi_type.
    assert stage_db.poi_kind_thai("nail_salon") == ""
    assert stage_db.poi_kind_thai("") == ""


def test_a_landmark_list_without_a_centre_states_no_distance(db):
    """Guessing a distance from a centre nobody supplied would be a number
    stated as though someone had measured it."""
    conn, pid = db
    stage_db.stage_pois(conn, pid, [
        {"feature_id": "node/1", "x": 30.0, "y": 40.0, "poi_key": "amenity",
         "poi_type": "school", "display_name": "โรงเรียนบ้านนา"}])
    rows = stage_db.poi_inventory_rows(conn, pid)
    assert rows[0]["distance_m"] == "" and rows[0]["bearing"] == ""


def test_the_rules_do_not_import_the_database():
    """cad_rules.py holds what a drawing looks like; stage_db.py holds what
    the database keeps. The dependency runs one way, and it staying that way
    is what lets a drawing rule be tested without a connection."""
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "cad_rules.py").read_text(encoding="utf-8")
    for line in src.split("\n"):
        stripped = line.strip()
        assert not stripped.startswith(("import stage_db", "from stage_db")), \
            line
    assert "sqlite3" not in src


def test_every_rule_is_still_reachable_through_stage_db():
    """Eight modules and the documentation call these as stage_db.<name>.
    The seam moved; the call sites did not, and must not have to."""
    import ast

    import cad_rules

    # Read the facade's own list rather than everything cad_rules holds: a
    # rule only the CAD writers need (the NCS layer table) does not have to
    # be reachable through the database module. What must hold is that every
    # name the facade claims actually resolves, to the *same object*.
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "stage_db.py").read_text(encoding="utf-8")
    claimed = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.ImportFrom) and node.module == "cad_rules":
            claimed += [a.name for a in node.names]
    assert len(claimed) > 50, "the facade lost its export list"
    missing = [n for n in claimed if not hasattr(stage_db, n)]
    assert not missing, missing
    drifted = [n for n in claimed
               if getattr(stage_db, n) is not getattr(cad_rules, n)]
    assert not drifted, drifted
    # and it is the same object, not a copy that can drift
    assert stage_db.interior_point is cad_rules.interior_point
    assert stage_db.carriageway_edges is cad_rules.carriageway_edges
