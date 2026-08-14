# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "rasterio",
#   "numpy",
#   "scipy",
#   "scikit-image",
#   "ezdxf",
#   "pyproj",
#   "requests",
#   "matplotlib",
# ]
# ///
"""Black-and-white poster-style site map (PNG + PDF) around a GPS point."""
import argparse
import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from rasterio.transform import xy as px2geo
from scipy.ndimage import gaussian_filter
from skimage import measure
from pyproj import Transformer
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, patches

from topo2cad import bbox_around, fetch_osm, fetch_ms_buildings, clip_runs, best_name

ROAD_W = {"motorway": 3.0, "trunk": 3.0, "primary": 2.6, "secondary": 2.2,
          "tertiary": 1.8, "residential": 1.0, "unclassified": 1.0}


def thai_font():
    names = {f.name for f in font_manager.fontManager.ttflist}
    for n in ("Sarabun", "Noto Sans Thai", "Thonburi", "Ayuthaya"):
        if n in names:
            return n
    return "DejaVu Sans"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--radius", type=float, default=150.0)
    p.add_argument("--dem", required=True)
    p.add_argument("--out", default="poster.png")
    p.add_argument("--title", default="ผังบริเวณ / SITE MAP")
    args = p.parse_args()

    s, w, n, e = bbox_around(args.lat, args.lon, args.radius)
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32647", always_xy=True)
    ux0, uy0 = to_utm.transform(w, s)
    ux1, uy1 = to_utm.transform(e, n)

    plt.rcParams["font.family"] = thai_font()
    fig, ax = plt.subplots(figsize=(14, 14))
    ax.set_facecolor("white")
    ax.set_aspect("equal")

    # contours from DEM
    with rasterio.open(args.dem) as src:
        win = from_bounds(w, s, e, n, src.transform)
        dem = src.read(1, window=win).astype(float)
        wtrans = src.window_transform(win)
    smooth = gaussian_filter(dem, sigma=1.5)
    lo, hi = np.nanpercentile(smooth, [2, 98])
    for interval in (0.5, 1, 2, 5, 10, 20, 50):
        if (hi - lo) / interval <= 12:
            break
    for lev in np.arange(math.floor(lo), math.ceil(hi) + interval, interval):
        for seg in measure.find_contours(smooth, lev):
            xs, ys = px2geo(wtrans, seg[:, 0], seg[:, 1])
            cx, cy = to_utm.transform(xs, ys)
            ax.plot(cx, cy, color="0.8", lw=0.5, zorder=1)

    # OSM + MS buildings
    elements = fetch_osm(s, w, n, e)
    buildings, pois = [], []
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "node" and best_name(tags):
            pois.append((best_name(tags), el["lon"], el["lat"]))
        elif el["type"] == "way" and "geometry" in el:
            pts = [(g["lon"], g["lat"]) for g in el["geometry"]]
            if "building" in tags:
                buildings.append(pts)
            elif "highway" in tags:
                lw = ROAD_W.get(tags["highway"], 0.7)
                for run in clip_runs(pts, s, w, n, e):
                    bx, by = to_utm.transform(*zip(*run))
                    ax.plot(bx, by, color="black", lw=lw, zorder=3,
                            solid_capstyle="round")
                name = best_name(tags)
                if name and len(pts) > 1:
                    mid = len(pts) // 2
                    mx, my = to_utm.transform(*pts[mid])
                    if ux0 < mx < ux1 and uy0 < my < uy1:
                        ax.annotate(name, (mx, my), fontsize=9, color="0.25",
                                    zorder=6, xytext=(2, 2),
                                    textcoords="offset points")
            elif "waterway" in tags or tags.get("natural") == "water":
                for run in clip_runs(pts, s, w, n, e):
                    bx, by = to_utm.transform(*zip(*run))
                    ax.plot(bx, by, color="0.55", lw=2.0, zorder=2)
    if len(buildings) < 20:
        buildings += fetch_ms_buildings(s, w, n, e, Path(args.dem).parent / "ms_cache")
    for ring in buildings:
        bx, by = to_utm.transform(*zip(*ring))
        ax.fill(bx, by, facecolor="0.2", edgecolor="black", lw=0.4, zorder=4)
    for name, plon, plat in pois:
        px_, py_ = to_utm.transform(plon, plat)
        if ux0 < px_ < ux1 and uy0 < py_ < uy1:
            ax.plot(px_, py_, "o", ms=4, color="black", zorder=6)
            ax.annotate(name, (px_, py_), fontsize=10, color="black", zorder=6,
                        xytext=(4, 4), textcoords="offset points")

    # pin at GPS point
    gx, gy = to_utm.transform(args.lon, args.lat)
    ax.plot(gx, gy, marker="v", ms=16, color="black", zorder=7)
    ax.plot(gx, gy, marker=".", ms=5, color="white", zorder=8)

    # north arrow (map is true-north-up)
    nx = ux1 - (ux1 - ux0) * 0.07
    ny = uy1 - (uy1 - uy0) * 0.09
    asz = (uy1 - uy0) * 0.035
    ax.annotate("N", (nx, ny + asz * 1.25), fontsize=18, ha="center",
                weight="bold", zorder=9)
    ax.add_patch(patches.Polygon([(nx - asz * 0.35, ny - asz), (nx, ny + asz),
                                  (nx + asz * 0.35, ny - asz), (nx, ny - asz * 0.5)],
                                 closed=True, facecolor="black", zorder=9))

    # scale bar (nice length ~1/5 of width)
    span = ux1 - ux0
    bar = 10 ** math.floor(math.log10(span / 5))
    if span / 5 / bar >= 5:
        bar *= 5
    elif span / 5 / bar >= 2:
        bar *= 2
    bx0, by_ = ux0 + span * 0.05, uy0 + (uy1 - uy0) * 0.05
    ax.plot([bx0, bx0 + bar], [by_, by_], color="black", lw=3, zorder=9)
    ax.annotate(f"{bar:g} m", (bx0 + bar / 2, by_), fontsize=11, ha="center",
                xytext=(0, 5), textcoords="offset points", zorder=9)

    # frame + title
    ax.set_xlim(ux0, ux1)
    ax.set_ylim(uy0, uy1)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_linewidth(2)
    ax.set_title(args.title, fontsize=20, pad=16, weight="bold")
    ax.set_xlabel(f"GPS {args.lat}, {args.lon}   |   UTM 47N (EPSG:32647)   |   "
                  f"รัศมี {args.radius:g} m", fontsize=11, labelpad=10)

    out = Path(args.out)
    fig.savefig(out, dpi=300, bbox_inches="tight", facecolor="white")
    fig.savefig(out.with_suffix(".pdf"), dpi=300, bbox_inches="tight",
                facecolor="white")
    print(f"Saved: {out} and {out.with_suffix('.pdf')} (font: {plt.rcParams['font.family'][0]})")


if __name__ == "__main__":
    main()
