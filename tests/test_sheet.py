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


# ------------------------------------------------------ legend + scale bar
@pytest.mark.parametrize("scale,step,bar_mm", [
    (500, 5, 40.0),
    (1000, 10, 40.0),
    (2000, 25, 50.0),
    (5000, 50, 40.0),
    (20000, 250, 50.0),
])
def test_scale_bar_fits_the_paper(scale, step, bar_mm):
    """Bounding one segment rather than the whole bar is how it first came
    out 200 mm wide on an A3 sheet."""
    from sheet import BAR_SEGMENTS, nice_bar_length

    assert nice_bar_length(scale) == step
    assert step * 1000 / scale * BAR_SEGMENTS == pytest.approx(bar_mm)
    assert step * 1000 / scale * BAR_SEGMENTS <= 60.0


def test_legend_lists_only_layers_that_carry_something():
    """A key to an empty layer is noise, and this drawing set creates
    several deliberately empty ones (C-PROP-LINE, C-ROAD-ROWY)."""
    ezdxf = pytest.importorskip("ezdxf")
    from sheet import used_layers

    doc = ezdxf.new("R2010", setup=True)
    for name in ("C-BLDG-OUTL", "C-ROAD-CNTR", "C-PROP-LINE"):
        doc.layers.add(name)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (1, 0), (1, 1)],
                       dxfattribs={"layer": "C-BLDG-OUTL"})
    msp.add_line((0, 0), (5, 5), dxfattribs={"layer": "C-ROAD-CNTR"})
    assert used_layers(doc) == ["C-BLDG-OUTL", "C-ROAD-CNTR"]


def test_sheet_carries_a_legend_and_a_bar():
    ezdxf = pytest.importorskip("ezdxf")
    from sheet import add_sheet

    doc = ezdxf.new("R2010", setup=True)
    doc.layers.add("C-BLDG-OUTL", color=4)
    doc.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 10)],
                                    dxfattribs={"layer": "C-BLDG-OUTL"})
    layout = add_sheet(doc, {"centre": (100.0, 200.0), "srid": 32647,
                             "extent": (400, 300)}, size="A3", scale=2000)
    texts = [e.text for e in layout if e.dxftype() == "MTEXT"]
    assert any("LEGEND" in s for s in texts)
    assert any("อาคาร" in s for s in texts)          # the layer that is used
    assert any("metres" in s for s in texts)         # the bar's unit label
    # ...and nothing about a layer with no entities on it
    assert not any("Property line" in s for s in texts)


# ------------------------------------------------------ extent dimensions
def test_extent_dimensions_measure_the_extent():
    """Real DIMENSION entities, not lines with a number beside them: a
    drafter expects to select one and see it behave like a dimension."""
    ezdxf = pytest.importorskip("ezdxf")
    import blocks

    doc = ezdxf.new("R2010", setup=True)
    doc.styles.add("EN_STYLE", font="arial.ttf")
    doc.layers.add("C-ANNO-DIMS", color=2)
    msp = doc.modelspace()
    blocks.add_extent_dimensions(doc, msp, 665694.0, 1520106.0, 400.0, 300.0,
                                 "C-ANNO-DIMS")
    dims = msp.query("DIMENSION")
    assert len(dims) == 2
    assert {round(d.get_measurement()) for d in dims} == {400, 300}
    assert {d.dxf.layer for d in dims} == {"C-ANNO-DIMS"}
    assert {d.dxf.dimstyle for d in dims} == {blocks.DIM_STYLE}
    # rendered, or there is nothing for a viewer to draw
    for d in dims:
        block = doc.blocks[d.dxf.geometry]
        kinds = {e.dxftype() for e in block}
        assert "LINE" in kinds and "MTEXT" in kinds


def test_dim_style_scales_with_the_drawing():
    """The same style has to read on a 200 m site plan and an 8 km
    locality map."""
    ezdxf = pytest.importorskip("ezdxf")
    import blocks

    for extent, expect_bigger in ((200.0, False), (8000.0, True)):
        doc = ezdxf.new("R2010", setup=True)
        doc.layers.add("C-ANNO-DIMS")
        blocks.add_extent_dimensions(doc, doc.modelspace(), 0, 0, extent,
                                     extent, "C-ANNO-DIMS")
        height = doc.dimstyles.get(blocks.DIM_STYLE).dxf.dimtxt
        assert (height > 10.0) is expect_bigger
        assert height >= 2.0          # never smaller than legible


def test_every_sheet_size_carries_the_data_attribution():
    """ODbL requires the credit, and the title block used to lay it out last
    and drop whatever fell through the bottom of the frame. On A4 that was
    all of it: the sheet went out crediting nobody, with the field-survey
    note drawn where the credit should have been, so the omission read as a
    design rather than a loss.
    """
    ezdxf = pytest.importorskip("ezdxf")
    import sheet as sheet_mod

    credits = ["Data © OpenStreetMap contributors (ODbL);",
               "   Microsoft ML footprints (ODbL); Copernicus",
               "   DEM 30 m (ESA)"]
    for size in ("A4", "A3", "A2", "A1", "A0"):
        doc = ezdxf.new("R2010", setup=True)
        sheet_mod.add_sheet(doc, {"centre": (0.0, 0.0), "extent": (250, 200),
                                  "lat": 14.8, "lon": 100.5, "srid": 32647,
                                  "source": credits},
                            size=size, scale=1000)
        psp = doc.layouts.get("SHEET")
        texts = [e.text for e in psp if e.dxftype() == "MTEXT"]
        for line in credits:
            assert line in texts, f"{size} dropped {line!r}"
        # and nothing was drawn outside the frame to achieve it
        lowest = min(e.dxf.insert.y for e in psp if e.dxftype() == "MTEXT")
        assert lowest >= 10, f"{size} drew text below the 10 mm margin"


def test_a_large_sheet_keeps_its_signature_spacing():
    """The credit is made to fit by compressing the signature rows, which
    have slack in them — but only as far as the sheet demands, so A3 and
    everything larger is untouched."""
    ezdxf = pytest.importorskip("ezdxf")
    import sheet as sheet_mod

    def rows(size):
        doc = ezdxf.new("R2010", setup=True)
        sheet_mod.add_sheet(doc, {"centre": (0.0, 0.0), "extent": (250, 200),
                                  "lat": 14.8, "lon": 100.5, "srid": 32647,
                                  "source": ["Data © OpenStreetMap (ODbL)"]},
                            size=size, scale=1000)
        return sorted(round(e.dxf.insert.y, 2) for e in doc.layouts.get("SHEET")
                      if e.dxftype() == "MTEXT" and "PREPARED" in e.text
                      or e.dxftype() == "MTEXT" and "CHECKED" in e.text)

    a3 = rows("A3")
    assert len(a3) == 2
    # the natural step, uncompressed
    assert round(a3[1] - a3[0], 2) == sheet_mod.SIGNATURE_STEP
