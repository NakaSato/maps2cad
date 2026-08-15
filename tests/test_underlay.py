"""Tests for the raster underlay (scripts/underlay.py).

Rasters are built on the fly with rasterio, so these run offline. The point
of most of them is the *refusals*: an image placed in the wrong projection
still looks like a map, so a silent approximation is the failure mode worth
guarding against.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

rasterio = pytest.importorskip("rasterio")
ezdxf = pytest.importorskip("ezdxf")

import underlay  # noqa: E402


def _tif(path, crs="EPSG:32648", left=435000.0, top=1750000.0, res=0.10,
         w=400, h=300, rotate=False):
    import numpy as np
    from rasterio.transform import Affine, from_origin

    t = from_origin(left, top, res, res)
    if rotate:
        t = t * Affine.rotation(15)
    prof = dict(driver="GTiff", height=h, width=w, count=1, dtype="uint8",
                crs=crs, transform=t)
    with rasterio.open(path, "w", **prof) as d:
        d.write(np.zeros((h, w), dtype="uint8"), 1)
    return path


# ------------------------------------------------------------- refusals
def test_missing_file_names_the_path(tmp_path):
    with pytest.raises(underlay.UnderlayError, match="not found"):
        underlay.raster_info(tmp_path / "nope.tif")


def test_raster_without_a_crs_is_refused(tmp_path):
    """A .tfw world file gives pixel size but not which projection those
    metres are in, so the image cannot be placed."""
    p = _tif(tmp_path / "nocrs.tif", crs=None)
    with pytest.raises(underlay.UnderlayError, match="no CRS"):
        underlay.raster_info(p)


def test_rotated_raster_is_refused_with_the_fix(tmp_path):
    p = _tif(tmp_path / "rot.tif", rotate=True)
    with pytest.raises(underlay.UnderlayError, match="rotated"):
        underlay.raster_info(p)


def test_crs_mismatch_is_refused_rather_than_approximated(tmp_path):
    """Transforming only the corners and stretching the pixels between them
    is wrong by metres in the middle, and wrong invisibly."""
    p = _tif(tmp_path / "wgs.tif", crs="EPSG:4326", left=104.0, top=15.0,
             res=0.0001)
    info = underlay.raster_info(p)
    with pytest.raises(underlay.UnderlayError) as e:
        underlay.check_crs(info, 32648)
    assert "gdalwarp -t_srs EPSG:32648" in str(e.value)   # tells them the fix


def test_matching_crs_passes(tmp_path):
    info = underlay.raster_info(_tif(tmp_path / "ok.tif"))
    assert underlay.check_crs(info, 32648) is None


# ------------------------------------------------------------ placement
def test_image_lands_at_true_scale_in_utm(tmp_path):
    p = _tif(tmp_path / "ortho.tif", left=435000.0, top=1750000.0,
             res=0.10, w=4000, h=3000)
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    info = underlay.attach(doc, msp, p, 32648, dxf_path=tmp_path / "site.dxf")

    img = [e for e in msp if e.dxftype() == "IMAGE"][0]
    size = img.dxf.image_size
    assert img.dxf.insert.x == pytest.approx(435000.0)
    # inserted at the bottom-left, so top minus the raster's height on ground
    assert img.dxf.insert.y == pytest.approx(1750000.0 - 300.0)
    assert img.dxf.u_pixel.x * size[0] == pytest.approx(400.0)
    assert abs(img.dxf.v_pixel.y) * size[1] == pytest.approx(300.0)
    assert info["size_m"] == pytest.approx((400.0, 300.0))


def test_image_gets_its_own_layer_and_is_faded(tmp_path):
    """Its own layer so a drafter freezes it before plotting, faded so
    linework traced over it stays readable."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    underlay.attach(doc, msp, _tif(tmp_path / "o.tif"), 32648)
    img = [e for e in msp if e.dxftype() == "IMAGE"][0]
    assert img.dxf.layer == underlay.LAYER
    assert img.dxf.fade == underlay.DEFAULT_FADE
    assert underlay.LAYER in doc.layers


def test_path_is_stored_relative_to_the_drawing(tmp_path):
    """A DXF references the raster by path and does not embed it, so the
    pair has to survive being moved together."""
    p = _tif(tmp_path / "ortho.tif")
    doc = ezdxf.new("R2010")
    info = underlay.attach(doc, doc.modelspace(), p, 32648,
                           dxf_path=tmp_path / "site.dxf")
    assert info["stored_path"] == "ortho.tif"
    assert not Path(info["stored_path"]).is_absolute()


def test_path_stays_absolute_without_a_drawing_path(tmp_path):
    p = _tif(tmp_path / "ortho.tif")
    doc = ezdxf.new("R2010")
    info = underlay.attach(doc, doc.modelspace(), p, 32648)
    assert Path(info["stored_path"]).is_absolute()


def test_the_raster_is_not_embedded(tmp_path):
    """If the pixels were copied in, a 200 MB orthophoto would become a
    200 MB DXF. They are not — which is exactly why the file must travel
    alongside."""
    p = _tif(tmp_path / "ortho.tif", w=2000, h=2000)
    doc = ezdxf.new("R2010")
    underlay.attach(doc, doc.modelspace(), p, 32648)
    out = tmp_path / "site.dxf"
    doc.saveas(out)
    assert out.stat().st_size < 200_000
    assert "ortho.tif" in out.read_text(encoding="utf-8", errors="replace")


def test_describe_reports_ground_size_and_resolution(tmp_path):
    info = underlay.attach(ezdxf.new("R2010"), ezdxf.new("R2010").modelspace(),
                           _tif(tmp_path / "o.tif", res=0.05, w=2000, h=1000),
                           32648)
    text = underlay.describe(info)
    assert "100 x 50 m" in text
    assert "0.050 m/px" in text
    assert underlay.LAYER in text
