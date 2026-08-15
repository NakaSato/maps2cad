#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "ezdxf",
#   "rasterio",
# ]
# ///
"""Attach a georeferenced raster to a drawing as a tracing underlay.

Where OpenStreetMap and the ML footprint layers have nothing — and they
genuinely do have nothing in places; a 5 x 5 km box at 12.526001,
102.159820 returns zero footprints — the way to get buildings into the CAD
file is to trace them from imagery you own. This puts that imagery into the
DXF at true scale, on its own layer, underneath the linework.

It deliberately does not detect buildings. An auto-traced footprint carries
1-3 m of boundary error and looks exactly as authoritative in a DXF as a
surveyed one, which is the wrong thing to hand a reviewing agency. A
drafter tracing a 3 cm/px drone orthophoto produces geometry someone
actually looked at, and that distinction is the whole point of the tool.

    uv run scripts/gis2cad.py --input plots.geojson \\
        --underlay ortho.tif --out output/site.dxf

Two things to know about DXF rasters:

  * The DXF stores a *path*, not the pixels. The image file has to travel
    with the drawing — keep it beside the .dxf, which is what this writes.
  * The raster must already be in the drawing's UTM CRS. Placing a
    differently-projected image by its corner coordinates would skew it,
    silently, by metres. Reproject first; the error message says how.
"""

from __future__ import annotations

import os
from pathlib import Path

# A raster underlay is a reference, not surveyed content, so it gets a layer
# of its own that a drafter can freeze in one click before plotting.
LAYER = "C-SITE-ORTH"

# Faded so traced linework reads clearly over it. 0 = full strength.
DEFAULT_FADE = 50


class UnderlayError(Exception):
    """Something about the raster prevents honest placement."""


def raster_info(path) -> dict:
    """Georeferencing of a raster, or a UnderlayError explaining why not."""
    import rasterio

    p = Path(path)
    if not p.is_file():
        raise UnderlayError(f"Underlay not found: {p}")
    try:
        with rasterio.open(p) as src:
            if src.crs is None:
                raise UnderlayError(
                    f"{p.name} carries no CRS, so there is no way to know "
                    "where it belongs. Georeference it first (a .tfw world "
                    "file alone is not enough — the CRS must be declared).")
            t = src.transform
            # b and d are the rotation/skew terms. A rotated raster cannot be
            # placed by an axis-aligned insert + size without shearing it.
            if abs(t.b) > 1e-9 or abs(t.d) > 1e-9:
                raise UnderlayError(
                    f"{p.name} is rotated relative to north. Reproject it to "
                    "a north-up grid first:\n"
                    f"    gdalwarp -t_srs EPSG:<zone> {p.name} north_up.tif")
            return {
                "path": p,
                "epsg": src.crs.to_epsg(),
                "crs": str(src.crs),
                "bounds": src.bounds,       # left, bottom, right, top
                "pixels": (src.width, src.height),
                "res": (abs(t.a), abs(t.e)),
            }
    except UnderlayError:
        raise
    except Exception as e:                   # rasterio/GDAL read failure
        raise UnderlayError(f"Could not read {p.name}: {e}")


def check_crs(info: dict, target_epsg: int) -> None:
    """Refuse a mismatch rather than placing the image approximately.

    Transforming only the corners of a differently-projected raster and
    stretching the pixels between them is wrong by metres in the middle —
    and wrong in a way nobody notices, because the image still looks like a
    map. A submission drawing cannot carry that.
    """
    if info["epsg"] != target_epsg:
        raise UnderlayError(
            f"{info['path'].name} is in {info['crs']} but the drawing is in "
            f"EPSG:{target_epsg}. Placing it anyway would skew it by metres. "
            f"Reproject it first:\n"
            f"    gdalwarp -t_srs EPSG:{target_epsg} "
            f"{info['path'].name} reprojected.tif")


def attach(doc, msp, raster_path, target_epsg, dxf_path=None,
           layer=LAYER, fade=DEFAULT_FADE) -> dict:
    """Place `raster_path` in the drawing at true scale. Returns its info.

    The stored path is relative to the DXF when both sit in the same tree,
    so the pair can be zipped and opened elsewhere; AutoCAD resolves a
    relative reference against the drawing's own folder.
    """
    info = raster_info(raster_path)
    check_crs(info, target_epsg)

    if layer not in doc.layers:
        doc.layers.add(layer, color=8)

    b = info["bounds"]
    width_m, height_m = b.right - b.left, b.top - b.bottom
    if width_m <= 0 or height_m <= 0:
        raise UnderlayError(f"{info['path'].name} has an empty extent.")

    stored = info["path"].resolve()
    if dxf_path:
        try:
            stored = Path(os.path.relpath(stored, Path(dxf_path).resolve().parent))
        except ValueError:
            pass                    # different drive on Windows; keep absolute

    image_def = doc.add_image_def(filename=str(stored),
                                  size_in_pixel=info["pixels"])
    image = msp.add_image(image_def,
                          insert=(b.left, b.bottom),
                          size_in_units=(width_m, height_m),
                          dxfattribs={"layer": layer})
    image.dxf.fade = fade
    # Underneath everything, so it never hides the linework traced from it.
    try:
        msp.set_redraw_order({image.dxf.handle: "0"})
    except Exception:
        pass          # draw order is advisory; DRAWORDER in CAD still works
    info["size_m"] = (width_m, height_m)
    info["stored_path"] = str(stored)
    return info


def describe(info: dict) -> str:
    w, h = info["size_m"]
    px, py = info["pixels"]
    rx, ry = info["res"]
    return (f"Underlay: {info['path'].name} — {w:,.0f} x {h:,.0f} m "
            f"({px} x {py} px, {rx:.3f} m/px) on {LAYER}")
