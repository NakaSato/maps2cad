"""Tests for the web app's GIS upload handling (scripts/serve.py).

The multipart reader is hand-rolled because stdlib's `cgi` is deprecated and
removed in 3.13, so it is worth pinning.
"""

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

SERVE = Path(__file__).resolve().parent.parent / "scripts" / "serve.py"
sys.path.insert(0, str(SERVE.parent))
spec = importlib.util.spec_from_file_location("serve", SERVE)
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)


def multipart(parts):
    """Build a multipart body: parts = [(name, filename|None, bytes)]."""
    b = b"boundaryXYZ"
    out = b""
    for name, filename, data in parts:
        out += b"--" + b + b"\r\n"
        disp = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        out += disp.encode() + b"\r\n\r\n" + data + b"\r\n"
    out += b"--" + b + b"--\r\n"
    return "multipart/form-data; boundary=boundaryXYZ", out


def test_parse_multipart_splits_fields_and_files():
    ctype, body = multipart([
        ("project", None, "chanthaburi-solar".encode()),
        ("files", "plots.geojson", b'{"type":"FeatureCollection"}'),
        ("files", "fence.zip", b"PK\x03\x04binary"),
    ])
    fields, files = serve.parse_multipart(ctype, body)
    assert fields["project"] == "chanthaburi-solar"
    assert [f[0] for f in files] == ["plots.geojson", "fence.zip"]
    assert files[0][1] == b'{"type":"FeatureCollection"}'
    assert files[1][1] == b"PK\x03\x04binary"      # binary survives intact


def test_parse_multipart_skips_empty_file_inputs():
    ctype, body = multipart([("files", "", b""), ("project", None, b"x")])
    fields, files = serve.parse_multipart(ctype, body)
    assert files == [] and fields["project"] == "x"


def test_parse_multipart_rejects_missing_boundary():
    with pytest.raises(serve.BadRequest, match="boundary"):
        serve.parse_multipart("multipart/form-data", b"whatever")


def test_save_uploads_rejects_files_neither_converter_reads(tmp_path):
    with pytest.raises(serve.BadRequest, match="not a file this reads"):
        serve.save_uploads([("notes.txt", b"hello")], tmp_path)


def test_save_uploads_accepts_an_osm_export(tmp_path):
    written = serve.save_uploads([("map.osm", b"<osm/>")], tmp_path / "d")
    assert [p.name for p in written] == ["map.osm"]


def test_save_uploads_expands_a_zipped_osm_export(tmp_path):
    buf = tmp_path / "src.zip"
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", b"notes")
        z.writestr("area.osm", b"<osm/>")
    written = serve.save_uploads([("export.zip", buf.read_bytes())],
                                 tmp_path / "dest")
    assert [p.name for p in written] == ["area.osm"]


# The upload is routed to a converter by extension: the two draw different
# drawings from the same ground, so guessing is worse than asking.
@pytest.mark.parametrize("names,kind", [
    (["map.osm"], "osm"),
    (["extract.xml"], "osm"),
    (["bangkok.osm.gz"], "osm"),          # suffix is .gz
    (["thailand.osm.pbf"], "osm"),        # accepted here, refused by osm2cad
    (["plots.geojson"], "gis"),
    (["survey.shp"], "gis"),
    (["a.geojson", "b.gpkg"], "gis"),
])
def test_import_kind_routes_by_extension(names, kind):
    assert serve.import_kind([Path(n) for n in names]) == kind


def test_import_kind_refuses_a_mixed_upload():
    with pytest.raises(serve.BadRequest, match="separately"):
        serve.import_kind([Path("map.osm"), Path("plots.geojson")])


@pytest.mark.parametrize("given,expected", [
    ("", None), ("  ", None), ("32647", "32647"), ("EPSG:32647", "32647"),
    ("epsg:4326", "4326"),
])
def test_parse_epsg_accepts_a_code_with_or_without_the_prefix(given, expected):
    assert serve.parse_epsg(given) == expected


def test_parse_epsg_rejects_a_name():
    with pytest.raises(serve.BadRequest, match="not an EPSG code"):
        serve.parse_epsg("UTM 47N")


def test_save_uploads_expands_a_zipped_shapefile(tmp_path):
    buf = tmp_path / "src.zip"
    with zipfile.ZipFile(buf, "w") as z:
        for ext in (".shp", ".dbf", ".shx", ".prj"):
            z.writestr("survey" + ext, b"data")
    written = serve.save_uploads([("fence.zip", buf.read_bytes())],
                                 tmp_path / "dest")
    assert [p.name for p in written] == ["survey.shp"]
    for ext in (".dbf", ".shx", ".prj"):
        assert (tmp_path / "dest" / f"survey{ext}").is_file()


def test_save_uploads_rejects_zip_without_a_shapefile(tmp_path):
    buf = tmp_path / "src.zip"
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", b"nothing useful")
    with pytest.raises(serve.BadRequest, match="no .shp file"):
        serve.save_uploads([("empty.zip", buf.read_bytes())],
                           tmp_path / "dest")


def test_save_uploads_flattens_paths_from_the_archive(tmp_path):
    """A zip entry must not be able to write outside the upload folder."""
    buf = tmp_path / "src.zip"
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../escape.shp", b"data")
        z.writestr("nested/dir/survey.dbf", b"data")
    dest = tmp_path / "dest"
    written = serve.save_uploads([("x.zip", buf.read_bytes())], dest)
    assert all(dest in p.parents for p in written)
    assert not (tmp_path.parent / "escape.shp").exists()
    assert (dest / "survey.dbf").is_file()


# gis2cad reads a file's own fields the way the OSM routes read tags
def test_row_attributes_drops_empty_and_nan_cells():
    import importlib.util as iu

    spec = iu.spec_from_file_location(
        "gis2cad", Path(serve.__file__).parent / "gis2cad.py")
    gis2cad = iu.module_from_spec(spec)
    spec.loader.exec_module(gis2cad)

    class Row(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    row = Row({"PLOT_NO": "12/3", "owner": "  ", "area": float("nan"),
               "note": None, "geometry": "POLYGON(...)", "_layer": None,
               "n": 2.5})
    assert gis2cad.row_attributes(row, list(row)) == {"PLOT_NO": "12/3",
                                                      "n": "2.5"}


def test_an_import_adopts_the_projects_crs(tmp_path, monkeypatch):
    """Merging into an existing project means adopting its CRS.

    gis2cad derives a UTM zone from the file's own data, so a survey whose
    centroid falls the other side of 102°E would stage in zone 48 inside a
    zone 47 drawing — hundreds of kilometres of offset that looks like
    nothing until the DXF opens.
    """
    import importlib.util as iu

    spec = iu.spec_from_file_location(
        "stage_db", Path(serve.__file__).parent / "stage_db.py")
    stage_db = iu.module_from_spec(spec)
    spec.loader.exec_module(stage_db)

    db = tmp_path / "staging.sqlite"
    conn = stage_db.connect(db)
    stage_db.create_project(conn, "site-a", 13.7455, 100.5325, 300, 200,
                            32647)
    conn.close()
    monkeypatch.setattr(serve, "STAGING_DB", db)

    assert serve.project_srid("site-a") == "32647"
    # An unknown project has no CRS to inherit, and the converter derives
    # its own — which is right for a first import.
    assert serve.project_srid("not-staged") is None


def test_project_srid_is_none_without_a_staging_database(tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(serve, "STAGING_DB", tmp_path / "absent.sqlite")
    assert serve.project_srid("anything") is None


def test_the_import_only_drawing_has_its_own_name():
    """An import merged into a site yields two drawings, and site.dxf has
    to be the combined one: a user who imports a survey into their site and
    downloads "the DXF" means the site, not their two boundary lines."""
    assert serve.KINDS["dxf"] == "site.dxf"
    assert serve.KINDS["import_dxf"] == "import.dxf"
    assert serve.KINDS["import_dxf"] != serve.KINDS["dxf"]


def _map_only_params():
    return serve.parse_form({"lat": ["13.7455"], "lon": ["100.5325"],
                             "width": ["300"], "height": ["200"],
                             "export": ["map"]})


def test_the_site_map_overlays_the_projects_survey_data(tmp_path,
                                                        monkeypatch):
    """A user who imports a parcel gets it on the submission sheet too, not
    only in the DXF — the two exports describe the same site."""
    import importlib.util as iu

    spec = iu.spec_from_file_location(
        "stage_db", Path(serve.__file__).parent / "stage_db.py")
    stage_db = iu.module_from_spec(spec)
    spec.loader.exec_module(stage_db)

    db = tmp_path / "staging.sqlite"
    conn = stage_db.connect(db)
    stage_db.create_project(conn, "13.745500_100.532500_300x200",
                            13.7455, 100.5325, 300, 200, 32647)
    conn.close()
    monkeypatch.setattr(serve, "STAGING_DB", db)
    monkeypatch.setattr(serve, "OUT", tmp_path / "web")

    seen = []
    monkeypatch.setattr(serve, "run_step",
                        lambda cmd, what, **kw: seen.append(cmd) or "")
    serve.run_generator(_map_only_params())
    cmd = seen[0]
    assert "--overlay-db" in cmd
    assert cmd[cmd.index("--overlay-project") + 1] == \
        "13.745500_100.532500_300x200"


def test_a_first_run_asks_for_no_overlay(tmp_path, monkeypatch):
    """Nothing is staged yet on a first run, and pointing the renderer at a
    project that does not exist would fail the export for an overlay's
    sake."""
    monkeypatch.setattr(serve, "STAGING_DB", tmp_path / "absent.sqlite")
    monkeypatch.setattr(serve, "OUT", tmp_path / "web")
    seen = []
    monkeypatch.setattr(serve, "run_step",
                        lambda cmd, what, **kw: seen.append(cmd) or "")
    serve.run_generator(_map_only_params())
    assert "--overlay-db" not in seen[0]
