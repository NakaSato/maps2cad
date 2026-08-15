"""Tests for the plottable paper-space sheet (scripts/sheet.py)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sheet import BLOCK_W, SHEET_MM, fitting_scale  # noqa: E402


def test_sheet_sizes_are_iso_landscape():
    for name, (w, h) in SHEET_MM.items():
        assert w > h, f"{name} should be landscape"
        # Each size is the next one halved along the long edge
    assert SHEET_MM["A3"] == (420, 297)
    assert SHEET_MM["A1"] == (841, 594)


def test_fitting_scale_accounts_for_the_title_block():
    """The usable viewport is the sheet minus the title block and margins,
    which is why 770 x 410 m does not fit A3 at 1:2000."""
    scale, vp_w, vp_h = fitting_scale(770, 410, "A3")
    pw, _ = SHEET_MM["A3"]
    assert vp_w == pw - 10 - BLOCK_W["A3"] - 20
    assert 770 * 1000 / 2000 > vp_w        # 385 mm needed, less available
    assert 770 * 1000 / scale <= vp_w      # the chosen scale does fit
    assert 410 * 1000 / scale <= vp_h


def test_fitting_scale_picks_larger_scale_on_a_bigger_sheet():
    a3, _, _ = fitting_scale(770, 410, "A3")
    a2, _, _ = fitting_scale(770, 410, "A2")
    a1, _, _ = fitting_scale(770, 410, "A1")
    assert a2 < a3 and a1 <= a2            # bigger sheet, finer scale
    assert a2 == 2000                      # 770 x 410 m plots at 1:2000 on A2


def test_fitting_scale_returns_round_numbers():
    for w, h in ((100, 80), (500, 250), (770, 410), (2000, 1000)):
        for size in ("A4", "A3", "A2", "A1"):
            scale, _, _ = fitting_scale(w, h, size)
            assert scale in (200, 250, 500, 1000, 1250, 2000, 2500,
                             5000, 10000, 20000)


def test_add_sheet_builds_a_viewport_at_the_requested_scale():
    ezdxf = pytest.importorskip("ezdxf")
    from sheet import add_sheet

    doc = ezdxf.new("R2010", setup=True)
    add_sheet(doc, {"project": "test", "lat": 15.83, "lon": 104.39,
                    "centre": (435157.5, 1750649.9), "srid": 32648,
                    "extent": (770, 410), "date": "2026-08-15"},
              size="A2", scale=2000)
    assert "SHEET" in doc.layout_names()
    layout = doc.layouts.get("SHEET")
    vps = [e for e in layout if e.dxftype() == "VIEWPORT" and e.dxf.id != 1]
    assert len(vps) == 1
    vp = vps[0]
    # view_height (metres) / viewport height (mm) * 1000 == the scale
    assert vp.dxf.view_height * 1000 / vp.dxf.height == pytest.approx(2000)
    assert vp.dxf.view_center_point.x == pytest.approx(435157.5)
    texts = [e.text for e in layout if e.dxftype() == "MTEXT"]
    assert any("EPSG:32648" in t for t in texts)
    assert any("1:2,000" in t for t in texts)


def test_add_sheet_rejects_unknown_size():
    ezdxf = pytest.importorskip("ezdxf")
    from sheet import add_sheet

    doc = ezdxf.new("R2010", setup=True)
    with pytest.raises(ValueError, match="unknown sheet size"):
        add_sheet(doc, {"centre": (0, 0), "srid": 32648}, size="B3")
