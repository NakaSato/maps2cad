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


def test_save_uploads_rejects_non_gis_files(tmp_path):
    with pytest.raises(serve.BadRequest, match="not a GIS file"):
        serve.save_uploads([("notes.txt", b"hello")], tmp_path)


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
