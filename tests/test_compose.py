"""compose.py: the routing, the CRS carry-over and the provenance table.

The converters themselves are subprocesses and are tested where they live;
what has to hold here is that a composed run cannot silently mix CRSs,
cannot route a file to the wrong converter, and cannot report a source it
did not draw.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compose  # noqa: E402
import stage_db  # noqa: E402


def test_files_route_to_the_same_converter_the_web_upload_uses(tmp_path):
    osm = tmp_path / "extract.osm"
    gis = tmp_path / "boundary.geojson"
    for f in (osm, gis):
        f.write_text("{}", encoding="utf-8")
    assert compose.plan_imports([osm, gis]) == [(osm, "osm"), (gis, "gis")]


def test_a_missing_file_stops_the_run_before_any_step(tmp_path):
    with pytest.raises(SystemExit):
        compose.plan_imports([tmp_path / "nope.geojson"])


def test_step_names_are_predictable_and_safe(tmp_path):
    assert compose.step_name(2, Path("/x/แปลง ที่ดิน.geojson")).startswith(
        "step2_")
    assert "/" not in compose.step_name(3, Path("/x/a b.osm"))
    assert compose.step_name(3, Path("/x/a b.osm")).endswith(".dxf")


def test_imports_inherit_the_projects_crs(tmp_path):
    """Each converter derives its own UTM zone from its own data. A survey
    file whose centroid falls the other side of 102°E would stage in zone 48
    inside a zone 47 project — a kilometre-scale error that looks like
    nothing until the drawing opens."""
    db = tmp_path / "s.sqlite"
    conn = stage_db.connect(db)
    stage_db.create_project(conn, "p", 13.7455, 100.5325, 500, 400, 32647)
    conn.close()
    assert compose.project_srid(db, "p") == 32647
    assert compose.project_srid(db, "other") is None
    assert compose.project_srid(tmp_path / "absent.sqlite", "p") is None


def test_the_extent_comes_from_the_request_not_the_imports(tmp_path):
    """An import carries features, not an extent — but the crop line,
    dimensions and grid are drawn from the project row."""
    db = tmp_path / "s.sqlite"
    conn = stage_db.connect(db)
    stage_db.create_project(conn, "p", 0.0, 0.0, 0.0, 0.0, 32647)
    conn.close()
    assert compose.set_extent(db, "p", 13.7455, 100.5325, 500, 400)
    conn = stage_db.connect(db)
    row = conn.execute("SELECT * FROM projects WHERE name = 'p'").fetchone()
    conn.close()
    assert (row["width_m"], row["height_m"]) == (500, 400)
    assert round(row["lat"], 4) == 13.7455


def _staged(tmp_path):
    from shapely.geometry import LineString, Polygon

    conn = stage_db.connect(tmp_path / "p.sqlite")
    pid = stage_db.create_project(conn, "p", 13.7, 100.5, 500, 400, 32647)
    stage_db.stage_buildings(conn, pid, [
        {"feature_id": "way/1", "source": "openstreetmap", "osm_name": "A",
         "code": "", "display_name": "A", "building_type": None,
         "geom": Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])},
        {"feature_id": "gis/plot/0", "source": "user_gis:boundary.geojson",
         "osm_name": "", "code": "", "display_name": "แปลง",
         "building_type": None,
         "geom": Polygon([(20, 0), (30, 0), (30, 10), (20, 10)])}])
    stage_db.stage_roads(conn, pid, [
        {"feature_id": "gis/plot/1", "source": "user_gis:boundary.geojson",
         "highway_type": "user_gis", "road_name": "รั้ว", "road_ref": None,
         "carriageway_m": 0.0, "geom": LineString([(0, 0), (50, 50)])}])
    stage_db.stage_contours(conn, pid, [
        {"elevation_m": 2.0, "geom": LineString([(0, 0), (5, 5)])}])
    return conn, pid


def test_provenance_names_every_source_including_the_file(tmp_path):
    pytest.importorskip("shapely")
    conn, pid = _staged(tmp_path)
    rows = stage_db.provenance(conn, pid)
    counts = {(r["source"], r["feature_class"]): r["count"] for r in rows}
    assert counts[("openstreetmap", "building")] == 1
    assert counts[("user_gis:boundary.geojson", "building")] == 1
    # The hole this closes: stage_roads never wrote `source`, so a survey
    # centreline read as OpenStreetMap in the staging layer.
    assert counts[("user_gis:boundary.geojson", "road")] == 1
    # DEM-derived rows are a source too, and used to have no column at all
    assert counts[("copernicus_dem", "contour")] == 1
    conn.close()


def test_provenance_csv_and_text_agree(tmp_path):
    pytest.importorskip("shapely")
    conn, pid = _staged(tmp_path)
    rows = stage_db.provenance(conn, pid)
    out = tmp_path / "sources.csv"
    assert stage_db.write_provenance_csv(out, rows) == len(rows)
    text = out.read_text(encoding="utf-8")
    assert "user_gis:boundary.geojson" in text
    report = stage_db.format_provenance(rows)
    for row in rows:
        assert row["source"] in report
    assert "(nothing staged)" in stage_db.format_provenance([])
    conn.close()
