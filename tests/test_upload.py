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
