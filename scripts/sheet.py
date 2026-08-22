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


def _text(msp, s, x, y, height, layer, bold=False, mask=False):
    from ezdxf.enums import MTextEntityAlignment

    # The title block is bilingual (มาตราส่วน, วันที่ …), so it always uses
    # the Thai-capable style — AutoCAD renders Latin from it fine, but the
    # SHX default cannot render Thai at all.
    m = msp.add_mtext(str(s), dxfattribs={"layer": layer,
                                          "char_height": height,
                                          "style": TITLE_STYLE})
    m.set_location((x, y), attachment_point=MTextEntityAlignment.MIDDLE_LEFT)
    if mask:
        # The same trick the model-space annotation uses: the text cuts a
        # clean hole through whatever it crosses. None would REMOVE it.
        m.set_bg_color("canvas", scale=1.1)
    return m


ROUND_SCALES = [200, 250, 500, 1000, 1250, 2000, 2500, 5000, 10000, 20000]

# What each layer is, for the legend. Bilingual because the sheet is read
# by a Thai reviewer and filed by an engineer who may not be. Only layers
# that actually carry something are listed — a key to an empty layer is
# noise, and this drawing set creates several deliberately empty ones.
LEGEND_LABELS = {
    "C-BLDG-OUTL": "อาคาร / Building",
    "C-ROAD-CNTR": "ถนน (แนวกลาง) / Road centreline",
    "C-ROAD-EDGE": "ขอบทาง / Edge of pavement",
    "C-ROAD-PATH": "ทางเดิน / Footway",
    "C-ROAD-ARRW": "ทิศทางเดินรถ / One-way",
    "C-ROAD-BRDG": "สะพาน / Bridge",
    "C-ROAD-TUNL": "อุโมงค์ / Tunnel",
    "C-TOPO-MAJR": "เส้นชั้นความสูงหลัก / Index contour",
    "C-TOPO-MINR": "เส้นชั้นความสูงรอง / Contour",
    "C-TOPO-SPOT": "ระดับจุด / Spot height",
    "C-HYDR-WATR": "แหล่งน้ำ / Water",
    "C-LAND-VEGT": "พื้นที่สีเขียว / Vegetation",
    "C-LAND-ZONE": "การใช้ที่ดิน / Land use",
    "C-LAND-TREE": "ต้นไม้ / Tree",
    "C-RAIL-TRAK": "ทางรถไฟ / Railway",
    "C-BNDY-BARR": "รั้ว ประตู / Fence, gate",
    "C-UTIL-POWR": "สายไฟฟ้า เสา / Power line, pylon",
    "C-UTIL-PIPE": "ท่อ / Pipeline",
    "C-SITE-PARK": "ที่จอดรถ / Parking",
    "C-SITE-POI": "พื้นที่สำคัญ / Landmark grounds",
    "C-ANNO-SYMB": "สถานที่สำคัญ / Landmark",
    # Named in the key by its source, deliberately: a reviewer reading the
    # sheet is entitled to know which names came from OSM and which from a
    # commercial feed nobody here surveyed.
    "C-ANNO-OVTR": "สถานที่ (ข้อมูล Overture) / Place (Overture)",
    "C-ANNO-GRID": "กริดพิกัด UTM / UTM grid",
    "C-ANNO-EXTN": "ขอบเขตพื้นที่ / Limit of extent",
    "C-PROP-LINE": "แนวเขตที่ดิน / Property line",
}


def used_layers(doc) -> list:
    """Layers that carry at least one entity in model space, in the order
    the legend lists them."""
    present = {e.dxf.layer for e in doc.modelspace()}
    return [name for name in LEGEND_LABELS if name in present]


# The bar is drawn as four segments, so the step is a quarter of the whole
BAR_SEGMENTS = 4


def nice_bar_length(scale, max_mm=60.0):
    """Ground distance per segment, so the whole bar fits `max_mm`.

    Bounding one segment instead of the bar is how it first came out
    200 mm wide on an A3 sheet — four segments of the length that fitted.
    """
    ground = max_mm * scale / 1000.0        # metres the whole bar may span
    best = 1
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500,
                 1000, 2000, 5000):
        if step * BAR_SEGMENTS <= ground:
            best = step
        else:
            break
    return best


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


def _scale_bar(psp, x, y, scale, layer_frame, layer_text):
    """A divided graphic scale bar: the thing a reader measures with when
    the sheet has been photocopied and the ratio no longer holds."""
    step = nice_bar_length(scale)
    seg_mm = step * 1000.0 / scale
    height = 1.6
    for i in range(BAR_SEGMENTS):
        x0 = x + i * seg_mm
        # Alternating filled and open boxes, the standard bar
        if i % 2 == 0:
            psp.add_solid([(x0, y), (x0 + seg_mm, y),
                           (x0, y + height), (x0 + seg_mm, y + height)],
                          dxfattribs={"layer": layer_frame})
        else:
            psp.add_lwpolyline([(x0, y), (x0 + seg_mm, y),
                                (x0 + seg_mm, y + height), (x0, y + height)],
                               close=True, dxfattribs={"layer": layer_frame})
    for i in (0, BAR_SEGMENTS // 2, BAR_SEGMENTS):
        _text(psp, f"{step * i:g}", x + i * seg_mm - 1.5, y - 2.0, 1.8,
              layer_text, mask=True)
    _text(psp, "เมตร / metres", x + BAR_SEGMENTS * seg_mm + 2.5, y + 0.6, 1.8,
          layer_text, mask=True)
    return BAR_SEGMENTS * seg_mm


def _legend(psp, doc, x, y, layer_frame, layer_text, max_rows=14):
    """A key to the layers this drawing actually uses.

    Drawn over the viewport corner on a masked box, which is where a
    drafter expects it and what keeps the viewport at the size
    fitting_scale() promised — making room by shrinking it would quietly
    change the plot scale of every existing sheet.
    """
    names = used_layers(doc)[:max_rows]
    if not names:
        return
    row_h, width = 4.2, 62.0
    height = row_h * (len(names) + 1) + 2
    # No fill behind it. A SOLID in "white" is ACI 255, which is white only
    # in AutoCAD's palette and plots as a black rectangle elsewhere, and a
    # WIPEOUT renders the same way in the plot preview. Each label carries
    # its own background mask instead — the convention the model-space
    # annotation already uses, and one that survives every renderer.
    psp.add_lwpolyline([(x, y - height), (x + width, y - height),
                        (x + width, y), (x, y)], close=True,
                       dxfattribs={"layer": layer_frame, "lineweight": 35})
    _text(psp, "สัญลักษณ์ / LEGEND", x + 3, y - 3.0, 2.2, layer_text,
          mask=True)
    row_y = y - 3.0 - row_h
    for name in names:
        colour = doc.layers.get(name).dxf.color if name in doc.layers else 7
        psp.add_line((x + 3, row_y), (x + 12, row_y),
                     dxfattribs={"layer": layer_frame, "color": colour,
                                 "lineweight": 35})
        _text(psp, LEGEND_LABELS[name], x + 14, row_y, 1.9, layer_text,
              mask=True)
        row_y -= row_h


def _corner_table(psp, rows, x, y, layer_frame, layer_text, max_rows=12):
    """The setting-out table on the sheet, under the legend.

    A CSV beside the drawing is what a machine reads; a reviewer reads the
    sheet, and a boundary with no coordinates on the paper is a boundary
    nobody can check. Long tables are capped and point at the CSV rather
    than running off the sheet — a table that overruns the frame is not on
    the sheet at all, the same rule the title-block credits follow.
    """
    if not rows:
        return 0.0
    shown = rows[:max_rows]
    row_h, width = 3.6, 92.0
    height = row_h * (len(shown) + 2) + 3
    psp.add_lwpolyline([(x, y - height), (x + width, y - height),
                        (x + width, y), (x, y)], close=True,
                       dxfattribs={"layer": layer_frame, "lineweight": 35})
    _text(psp, "ตารางค่าพิกัดมุมเขต / BOUNDARY CORNERS", x + 3, y - 3.0, 2.2,
          layer_text, mask=True)
    # Column x-offsets: corner, easting, northing, bearing, distance
    cols = (3, 14, 36, 58, 78)
    head_y = y - 3.0 - row_h
    for offset, head in zip(cols, ("มุม", "E (m)", "N (m)", "ทิศทาง", "ระยะ")):
        _text(psp, head, x + offset, head_y, 1.8, layer_text, mask=True)
    row_y = head_y - row_h
    for row in shown:
        for offset, value in zip(cols, (
                row["corner"], f"{row['easting']:,.3f}",
                f"{row['northing']:,.3f}", row["bearing"],
                f"{row['distance_m']:,.2f}")):
            _text(psp, value, x + offset, row_y, 1.7, layer_text, mask=True)
        row_y -= row_h
    if len(rows) > max_rows:
        _text(psp, f"+{len(rows) - max_rows} more — see "
                   "corner_coordinates.csv",
              x + 3, row_y, 1.6, layer_text, mask=True)
        height += row_h
    return height


# Signature rows: the natural spacing, and the tightest that still reads.
# A label at 1.8 mm over a ruled line at 2.2 mm needs 3.2 mm between them,
# so the minimum leaves ~3 mm of clear space under the rule.
SIGNATURE_STEP = 9.7
SIGNATURE_STEP_MIN = 6.4


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

    # ---- scale bar and legend, over the viewport corner ------------------
    bar_x = margin + 6
    bar_y = margin + 8
    _scale_bar(psp, bar_x, bar_y, scale, LAYERS["frame"], LAYERS["text"])
    legend_top = ph - margin - 6
    _legend(psp, doc, margin + 6, legend_top,
            LAYERS["frame"], LAYERS["text"])
    # Under the legend, on the same left edge: both are read before the
    # drawing is, and both sit over the viewport rather than shrinking it.
    corners = info.get("corners") or []
    if corners:
        used = 4.2 * (len(used_layers(doc)[:14]) + 1) + 2
        _corner_table(psp, corners, margin + 6, legend_top - used - 6,
                      LAYERS["frame"], LAYERS["text"])

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

    # Attribution. `source` may be one string or several lines, because a
    # composed drawing credits every source that supplied a line — see
    # stage_db.credit_lines().
    credits = info.get("source") or "Data © OpenStreetMap contributors (ODbL)"
    if isinstance(credits, str):
        credits = [credits]

    # The credit is not discretionary — ODbL requires it, and this block
    # used to be laid out last and simply dropped whatever fell through the
    # bottom of the frame. On A4 that was the whole of it: every A4 sheet
    # went out crediting nobody, with the "verify against field survey" note
    # drawn below where the credit should have been, so the omission read as
    # a design rather than a loss.
    #
    # So the signature rows give way instead. They are the block with slack
    # in it — a ruled line and a role — and they compress only as far as the
    # sheet actually demands, which leaves A3 and every larger sheet exactly
    # as they were.
    roles = (("ผู้จัดทำ", "PREPARED BY", "prepared_by"),
             ("ผู้ตรวจสอบ", "CHECKED BY", "checked_by"),
             ("ผู้อนุมัติ", "APPROVED BY", "approved_by"))
    credit_h = len(credits) * 2.6 + 0.4
    # 6.9 = the separator gap below the signatures (4) plus the clearance
    # the note needs above the frame (2.9).
    room = (y - 6.9 - credit_h - margin) / len(roles)
    step = min(SIGNATURE_STEP, max(room, SIGNATURE_STEP_MIN))
    for role_th, role_en, key in roles:
        _text(psp, f"{role_th} / {role_en}", tx, y, 1.8, tl)
        _text(psp, info.get(key) or "_" * 22, tx, y - 3.2, 2.2, tl)
        y -= step

    _line(psp, bx, y + 2, pw - margin, y + 2, LAYERS["frame"], 25)
    y -= 4
    for line in credits:
        # A sheet too small to hold the credit even compressed is a sheet
        # this drawing should not be plotted on; drawing outside the frame
        # would not fix it. add_sheet already warns on a scale that will not
        # fit, and this says the same thing about the attribution.
        if y - 3 < margin:
            print(f"WARNING: the {size} title block cannot hold the data "
                  f"attribution ({len(credits)} lines); plot larger, or the "
                  "sheet goes out crediting nobody.")
            break
        _text(psp, line, tx, y, 1.6, tl)
        y -= 2.6
    y -= 0.4
    _text(psp, "Verify against field survey before construction.",
          tx, y, 1.6, tl)
    return layout
