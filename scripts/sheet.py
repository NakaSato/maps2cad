#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["ezdxf"]
# ///
"""Paper-space sheet and title block for the CAD drawings.

Model space holds the survey in real metres; this adds the sheet you
actually plot: a paper-space layout at a fixed scale with a viewport onto
the site, a border, and a title block carrying the project identity,
coordinates, CRS and signature boxes.

Imported by topo2cad.py and db2dxf.py; both expose --sheet and --scale.
"""

from __future__ import annotations

# ISO sheets in millimetres, landscape
SHEET_MM = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420),
            "A1": (841, 594), "A0": (1189, 841)}

# Title block occupies a strip down the right-hand edge
BLOCK_W = {"A4": 80, "A3": 100, "A2": 120, "A1": 140, "A0": 160}

LAYERS = {
    "frame": "C-ANNO-TTLB",     # sheet border and title block linework
    "text": "C-ANNO-TTLB-TEXT",
    "viewport": "C-ANNO-VPRT",
}

# Must match TEXT_STYLES in topo2cad.py — a sheet is added to a document the
# CAD writers have already set up, so the style is normally there already.
TITLE_STYLE = "TH_STYLE"
TITLE_FONT = "THSarabunNew.ttf"


def _line(msp, x1, y1, x2, y2, layer, lw=25):
    msp.add_line((x1, y1), (x2, y2),
                 dxfattribs={"layer": layer, "lineweight": lw})


def _text(msp, s, x, y, height, layer, bold=False):
    from ezdxf.enums import MTextEntityAlignment

    # The title block is bilingual (มาตราส่วน, วันที่ …), so it always uses
    # the Thai-capable style — AutoCAD renders Latin from it fine, but the
    # SHX default cannot render Thai at all.
    m = msp.add_mtext(str(s), dxfattribs={"layer": layer,
                                          "char_height": height,
                                          "style": TITLE_STYLE})
    m.set_location((x, y), attachment_point=MTextEntityAlignment.MIDDLE_LEFT)
    return m


ROUND_SCALES = [200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000, 20000]


def fitting_scale(extent_w_m, extent_h_m, size="A3"):
    """Smallest round scale at which the extent fits the viewport of `size`.
    Returns (scale, vp_w_mm, vp_h_mm)."""
    pw, ph = SHEET_MM[size.upper()]
    margin = 10
    vp_w = pw - margin - BLOCK_W[size.upper()] - margin * 2
    vp_h = ph - margin * 2 - 4
    for s in ROUND_SCALES:
        if (extent_w_m * 1000 / s) <= vp_w and (extent_h_m * 1000 / s) <= vp_h:
            return s, vp_w, vp_h
    return ROUND_SCALES[-1], vp_w, vp_h


def add_sheet(doc, info: dict, size: str = "A3", scale: int = 2000,
              name: str = "SHEET"):
    """Create a paper-space layout with a viewport at 1:`scale`.

    info keys (all optional except centre/srid):
      project, location, subdistrict, district, province, agency,
      prepared_by, checked_by, approved_by, drawing_no, sheet_no, revision,
      date, centre (x, y in model units), srid, extent (w, h in metres),
      source
    """
    size = size.upper()
    if size not in SHEET_MM:
        raise ValueError(f"unknown sheet size {size!r}; "
                         f"expected one of {', '.join(SHEET_MM)}")
    pw, ph = SHEET_MM[size]
    bw = BLOCK_W[size]
    margin = 10

    # add_sheet is also callable on a document this module did not build
    # (a test, or a future writer), so do not assume the style is present.
    if TITLE_STYLE not in doc.styles:
        doc.styles.add(TITLE_STYLE, font=TITLE_FONT)

    for layer, color in ((LAYERS["frame"], 7), (LAYERS["text"], 7),
                         (LAYERS["viewport"], 8)):
        if layer not in doc.layers:
            doc.layers.add(layer, color=color)

    layout = doc.layouts.new(name) if name not in doc.layouts else \
        doc.layouts.get(name)
    layout.page_setup(size=(pw, ph), margins=(0, 0, 0, 0), units="mm")
    psp = layout

    # Sheet border and inner frame
    _line(psp, margin, margin, pw - margin, margin, LAYERS["frame"], 50)
    _line(psp, pw - margin, margin, pw - margin, ph - margin,
          LAYERS["frame"], 50)
    _line(psp, pw - margin, ph - margin, margin, ph - margin,
          LAYERS["frame"], 50)
    _line(psp, margin, ph - margin, margin, margin, LAYERS["frame"], 50)

    # Title block strip on the right
    bx = pw - margin - bw
    _line(psp, bx, margin, bx, ph - margin, LAYERS["frame"], 35)

    # Viewport onto model space, at exactly 1:scale
    vp_w = bx - margin * 2
    vp_h = ph - margin * 2 - 4
    cx, cy = info.get("centre", (0.0, 0.0))

    # A sheet that silently crops the site is worse than a smaller scale,
    # so check the extent fits and say so plainly if it does not.
    ext = info.get("extent")
    if ext:
        need_w, need_h = ext[0] * 1000 / scale, ext[1] * 1000 / scale
        if need_w > vp_w + 0.5 or need_h > vp_h + 0.5:
            fits, _, _ = fitting_scale(ext[0], ext[1], size)
            shows_w, shows_h = vp_w * scale / 1000, vp_h * scale / 1000
            print(f"WARNING: {ext[0]:.0f} × {ext[1]:.0f} m does not fit "
                  f"{size} at 1:{scale:,} — the viewport shows "
                  f"{shows_w:.0f} × {shows_h:.0f} m and the rest is cropped. "
                  f"Use 1:{fits:,} on {size}, or a larger sheet.")
    # 1 mm on paper = `scale` mm on the ground = scale/1000 metres
    view_height_m = vp_h * scale / 1000.0
    psp.add_viewport(
        center=(margin + vp_w / 2, margin + 2 + vp_h / 2),
        size=(vp_w, vp_h),
        view_center_point=(cx, cy),
        view_height=view_height_m,
        dxfattribs={"layer": LAYERS["viewport"]})

    # ---- title block contents ------------------------------------------
    tx = bx + 4
    y = ph - margin - 8
    tl = LAYERS["text"]

    def row(label, value, gap=6.0, h=2.4, label_h=1.8):
        nonlocal y
        if label:
            _text(psp, label, tx, y, label_h, tl)
            y -= 3.4
        _text(psp, value or "—", tx, y, h, tl)
        y -= gap

    _text(psp, info.get("title_th", "แผนผังแสดงที่ตั้งโครงการ"),
          tx, y, 3.2, tl)
    y -= 4.6
    _text(psp, info.get("title_en", "PROJECT LOCATION AND SITE MAP"),
          tx, y, 2.4, tl)
    y -= 5
    _line(psp, bx, y + 1.5, pw - margin, y + 1.5, LAYERS["frame"], 35)
    y -= 4

    row("ชื่อโครงการ / PROJECT", info.get("project"))
    row("สถานที่ตั้ง / LOCATION", info.get("location"))
    admin = ", ".join(v for v in (info.get("subdistrict"),
                                  info.get("district"),
                                  info.get("province")) if v)
    row("เขตปกครอง / ADMIN AREA", admin)
    row("หน่วยงาน / AGENCY", info.get("agency"))

    _line(psp, bx, y + 2, pw - margin, y + 2, LAYERS["frame"], 25)
    y -= 3
    row("พิกัด / COORDINATES (WGS 84)",
        f"{info.get('lat', 0):.6f}, {info.get('lon', 0):.6f}")
    row("GRID (UTM)",
        f"E {cx:,.2f}  N {cy:,.2f}")
    row("ระบบพิกัด / CRS", f"EPSG:{info.get('srid', '—')}")
    ext = info.get("extent")
    row("ขอบเขต / EXTENT",
        f"{ext[0]:.0f} × {ext[1]:.0f} m" if ext else None)
    row("มาตราส่วน / SCALE", f"1:{scale:,}  ({size})")

    _line(psp, bx, y + 2, pw - margin, y + 2, LAYERS["frame"], 25)
    y -= 3
    row("เลขที่แบบ / DRAWING No.", info.get("drawing_no"))
    row("แผ่นที่ / SHEET", info.get("sheet_no", "1/1"))
    row("แก้ไขครั้งที่ / REVISION", info.get("revision", "0"))
    row("วันที่ / DATE", info.get("date"))

    _line(psp, bx, y + 2, pw - margin, y + 2, LAYERS["frame"], 25)
    y -= 3
    for role_th, role_en, key in (("ผู้จัดทำ", "PREPARED BY", "prepared_by"),
                                  ("ผู้ตรวจสอบ", "CHECKED BY", "checked_by"),
                                  ("ผู้อนุมัติ", "APPROVED BY", "approved_by")):
        _text(psp, f"{role_th} / {role_en}", tx, y, 1.8, tl)
        y -= 3.2
        _text(psp, info.get(key) or "_" * 22, tx, y, 2.2, tl)
        y -= 6.5

    _line(psp, bx, y + 2, pw - margin, y + 2, LAYERS["frame"], 25)
    y -= 4
    _text(psp, info.get("source", "Data © OpenStreetMap contributors (ODbL)"),
          tx, y, 1.6, tl)
    y -= 3
    _text(psp, "Verify against field survey before construction.",
          tx, y, 1.6, tl)
    return layout
