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
#   "shapely>=2.0",   # new_ml_rings(), shared with topo2cad.py
#   "pillow",         # basemap.py decodes map tiles with it
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
from scipy.ndimage import gaussian_filter, zoom
from skimage import measure
from pyproj import Transformer
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, patches

from topo2cad import (bbox_around, fetch_osm, fetch_ms_buildings,
                      new_ml_rings, clip_runs, oneway_dir,
                      best_name, utm_transformer)
import stage_db          # arrow spacing, shared with the CAD writers

ROAD_W = {"motorway": 3.0, "trunk": 3.0, "primary": 2.6, "secondary": 2.2,
          "tertiary": 1.8, "residential": 1.0, "unclassified": 1.0}

# B&W poster palette vs. the CAD-style color palette (สีเดิม)
STYLES = {
    "bw": {"contour": "0.8", "contour_lbl": "0.55", "road": "black",
           "road_name": "0.25", "water": "0.55", "water_fill": "0.88",
           "green_fill": "0.94", "path": "0.35", "barrier": "0.45",
           "bld_face": "0.2", "bld_edge": "black",
           "poi": "black", "pin": "black"},
    "color": {"contour": "0.75", "contour_lbl": "0.5", "road": "#e07b00",
              "road_name": "#b36200", "water": "#1f5fbf",
              "water_fill": "#cfe6fa", "green_fill": "#d9edd0",
              "path": "#8a6d4b", "barrier": "0.45",
              "bld_face": "#bfeef0", "bld_edge": "#00a0a8",
              "poi": "#c400c4", "pin": "red"},
}
PATHS = {"footway", "path", "track", "steps", "cycleway", "bridleway"}


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
    p.add_argument("--width", type=float, help="full box width in meters (overrides radius)")
    p.add_argument("--height", type=float, help="full box height in meters (overrides radius)")
    p.add_argument("--dem", required=True)
    p.add_argument("--out", default="poster.png")
    p.add_argument("--title", default="ผังบริเวณ / SITE MAP")
    p.add_argument("--no-ml", action="store_true",
                   help="Do not supplement with Microsoft ML building "
                        "footprints (same flag as topo2cad.py)")
    p.add_argument("--style", default="bw", choices=STYLES,
                   help="bw = black & white poster, color = CAD-style colors")
    p.add_argument("--arrows", action="store_true",
                   help="Draw direction arrows on one-way carriageways, "
                        "spaced by the same rule the CAD routes use. Opt-in: "
                        "a poster is a denser medium than a drawing, and on "
                        "a 150 m radius the arrows compete with the linework.")
    p.add_argument("--basemap", nargs="?", const="osm", metavar="PROVIDER",
                   help="Draw a map backdrop under everything: osm, "
                        "opentopomap, esri-imagery and the rest of "
                        "basemap.py's providers, or a {z}/{x}/{y} template. "
                        "Greyscaled under --style bw, where a colour map "
                        "would fight the black-and-white linework.")
    p.add_argument("--basemap-alpha", type=float, default=0.35, metavar="A",
                   help="How strongly the backdrop shows through (0-1, "
                        "default 0.35 — it is context, not content)")
    p.add_argument("--basemap-zoom", type=int, metavar="Z")
    p.add_argument("--basemap-max-tiles", type=int, default=128, metavar="N")
    args = p.parse_args()
    st = STYLES[args.style]

    s, w, n, e = bbox_around(args.lat, args.lon, args.radius, args.width, args.height)
    to_utm, utm_epsg, utm_label = utm_transformer(args.lat, args.lon)
    ux0, uy0 = to_utm.transform(w, s)
    ux1, uy1 = to_utm.transform(e, n)

    plt.rcParams["font.family"] = thai_font()
    fig, ax = plt.subplots(figsize=(14, 14))
    ax.set_facecolor("white")
    ax.set_aspect("equal")

    # Backdrop first, so every line and label sits over it.
    basemap_credit = ""
    if args.basemap:
        import tempfile

        import basemap as bm
        try:
            with tempfile.TemporaryDirectory() as tmp:
                info = bm.build((s, w, n, e), utm_epsg, Path(tmp) / "bm.tif",
                                provider=args.basemap, zoom=args.basemap_zoom,
                                max_tiles=args.basemap_max_tiles)
                with rasterio.open(info["path"]) as src:
                    rgb = np.transpose(src.read([1, 2, 3]), (1, 2, 0))
                    bounds = src.bounds
            if args.style == "bw":
                # Rec. 601 luma: a colour map under a black-and-white poster
                # fights the linework, while grey reads as texture behind it.
                grey = (rgb @ np.array([0.299, 0.587, 0.114])).astype("uint8")
                rgb = np.dstack([grey] * 3)
            ax.imshow(rgb, extent=(bounds.left, bounds.right,
                                   bounds.bottom, bounds.top),
                      zorder=0.4, alpha=args.basemap_alpha,
                      interpolation="bilinear")
            basemap_credit = info["attribution"]
            print(bm.describe(info))
        except bm.BasemapError as exc:
            # A poster without its backdrop is still a poster
            print(f"WARNING: no background map — {exc}")

    # contours from DEM
    with rasterio.open(args.dem) as src:
        win = from_bounds(w, s, e, n, src.transform)
        dem = src.read(1, window=win).astype(float)
        wtrans = src.window_transform(win)
    smooth = gaussian_filter(dem, sigma=1.5)
    # upsample so contours are smooth curves, not 30m-pixel steps
    ZF = 8
    fine = zoom(smooth, ZF, order=3)
    lo, hi = np.nanpercentile(smooth, [2, 98])
    for interval in (0.5, 1, 2, 5, 10, 20, 50):
        if (hi - lo) / interval <= 12:
            break
    for lev in np.arange(math.floor(lo), math.ceil(hi) + interval, interval):
        for seg in measure.find_contours(fine, lev):
            xs, ys = px2geo(wtrans, seg[:, 0] / ZF, seg[:, 1] / ZF)
            cx, cy = to_utm.transform(xs, ys)
            ax.plot(cx, cy, color=st["contour"], lw=0.5, zorder=1)
            if len(seg) > 40 * ZF:  # elevation label on long contours
                mi = len(cx) // 2
                ax.text(cx[mi], cy[mi], f"{lev:g}", fontsize=6.5,
                        color=st["contour_lbl"], ha="center", va="center",
                        zorder=1.2,
                        bbox=dict(facecolor="white", edgecolor="none",
                                  alpha=0.8, pad=0.3))

    # OSM + MS buildings
    elements = fetch_osm(s, w, n, e)
    buildings, pois, roads_draw = [], [], []
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "node" and best_name(tags):
            pois.append((best_name(tags), el["lon"], el["lat"]))
        elif el["type"] == "way" and "geometry" in el:
            pts = [(g["lon"], g["lat"]) for g in el["geometry"]]
            if "building" in tags:
                buildings.append(pts)
            elif "highway" in tags:
                runs = []
                for run in clip_runs(pts, s, w, n, e):
                    bx, by = to_utm.transform(*zip(*run))
                    runs.append((list(bx), list(by)))
                if not runs:
                    continue
                if tags["highway"] in PATHS:  # minor paths: dashed single line
                    for bx, by in runs:
                        ax.plot(bx, by, color=st["path"], lw=1.0, zorder=2.8,
                                linestyle=(0, (4, 2.5)))
                else:
                    lw = ROAD_W.get(tags["highway"], 0.7)
                    roads_draw.append((best_name(tags), lw, runs,
                                       oneway_dir(tags)))
            elif tags.get("natural") == "water":
                bx, by = to_utm.transform(*zip(*pts))
                ax.fill(bx, by, facecolor=st["water_fill"],
                        edgecolor=st["water"], lw=0.8, zorder=0.9)
            elif "waterway" in tags:
                for run in clip_runs(pts, s, w, n, e):
                    bx, by = to_utm.transform(*zip(*run))
                    ax.plot(bx, by, color=st["water"], lw=2.0, zorder=2)
            elif "leisure" in tags or "landuse" in tags:
                bx, by = to_utm.transform(*zip(*pts))
                ax.fill(bx, by, facecolor=st["green_fill"],
                        edgecolor="none", zorder=0.8)
            elif "barrier" in tags:
                for run in clip_runs(pts, s, w, n, e):
                    bx, by = to_utm.transform(*zip(*run))
                    ax.plot(bx, by, color=st["barrier"], lw=0.6, zorder=2.5)
    # roads as two parallel lines: colored casing with white fill between
    for _, lw, runs, _ow in roads_draw:
        for bx, by in runs:
            ax.plot(bx, by, color=st["road"], lw=lw * 2.5 + 2, zorder=3,
                    solid_capstyle="round", solid_joinstyle="round")
    for _, lw, runs, _ow in roads_draw:
        for bx, by in runs:
            ax.plot(bx, by, color="white", lw=lw * 2.5 - 0.2, zorder=3.1,
                    solid_capstyle="round", solid_joinstyle="round")

    # Each unique road name once, on its longest run anywhere in the extent.
    # A divided carriageway is several OSM ways sharing one name, so labeling
    # per-way prints the name two or four times over.
    longest_run = {}
    for name, lw, runs, _ow in roads_draw:
        if not name:
            continue
        best = max(runs, key=lambda r: len(r[0]))
        if name not in longest_run or len(best[0]) > len(longest_run[name][0]):
            longest_run[name] = best
    for name, (bx, by) in longest_run.items():
        if len(bx) < 2:
            continue
        i = len(bx) // 2
        j = max(i - 1, 0)
        ang = math.degrees(math.atan2(by[i] - by[j], bx[i] - bx[j]))
        if ang > 90:
            ang -= 180
        elif ang < -90:
            ang += 180
        ax.text((bx[j] + bx[i]) / 2, (by[j] + by[i]) / 2, name, fontsize=9,
                color=st["road_name"], rotation=ang, rotation_mode="anchor",
                ha="center", va="bottom", zorder=6,
                bbox=dict(facecolor="white", edgecolor="none", alpha=0.6,
                          pad=0.6))

    # Direction of travel, placed by stage_db.arrow_positions() — the same
    # call the three CAD writers make, so a poster and the drawing of the
    # same site put the arrows in the same places.
    if args.arrows:
        length = max(6.0, (ux1 - ux0) * 0.012)
        drawn_arrows = 0
        for _name, _lw, runs, oneway in roads_draw:
            if not oneway:
                continue
            for bx, by in runs:
                for x, y, rot in stage_db.arrow_positions(list(zip(bx, by))):
                    ang = math.radians(rot + (180.0 if oneway < 0 else 0.0))
                    dx, dy = math.cos(ang) * length / 2, math.sin(ang) * length / 2
                    ax.annotate("", xy=(x + dx, y + dy), xytext=(x - dx, y - dy),
                                arrowprops=dict(arrowstyle="-|>", lw=0.9,
                                                color=st["road_name"],
                                                shrinkA=0, shrinkB=0),
                                zorder=3.4)
                    drawn_arrows += 1
        print(f"One-way arrows: {drawn_arrows}")

    # Same rule as topo2cad.py: supplement everywhere, not only where OSM is
    # nearly empty, and drop footprints an OSM building already covers.
    if not args.no_ml:
        ms = fetch_ms_buildings(s, w, n, e, Path(args.dem).parent / "ms_cache")
        keep = new_ml_rings(buildings, ms)
        buildings += [ring for _i, ring in keep]
        print(f"MS footprints: {len(ms)} available, {len(keep)} added, "
              f"{len(ms) - len(keep)} already mapped in OSM")
    for ring in buildings:
        bx, by = to_utm.transform(*zip(*ring))
        ax.fill(bx, by, facecolor=st["bld_face"], edgecolor=st["bld_edge"],
                lw=0.4, zorder=2.6)  # below roads: road network stays on top
    for name, plon, plat in pois:
        px_, py_ = to_utm.transform(plon, plat)
        if ux0 < px_ < ux1 and uy0 < py_ < uy1:
            ax.plot(px_, py_, "o", ms=4, color=st["poi"], zorder=6)
            ax.annotate(name, (px_, py_), fontsize=10, color=st["poi"], zorder=6,
                        xytext=(4, 4), textcoords="offset points")

    # pin at GPS point
    gx, gy = to_utm.transform(args.lon, args.lat)
    ax.plot(gx, gy, marker="v", ms=16, color=st["pin"], zorder=7)
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
    extent = (f"พื้นที่ {args.width:g}×{args.height:g} m"
              if args.width and args.height else f"รัศมี {args.radius:g} m")
    ax.set_xlabel(f"GPS {args.lat}, {args.lon}   |   UTM {utm_label} "
                  f"(EPSG:{utm_epsg})   |   {extent}", fontsize=11, labelpad=10)

    if basemap_credit:
        # On the figure, not the axes: the backdrop is credited wherever the
        # poster is cropped to, and a licence line is not optional.
        fig.text(0.01, 0.005, basemap_credit, fontsize=7, color="0.35")

    out = Path(args.out)
    fig.savefig(out, dpi=450, bbox_inches="tight", facecolor="white")
    fig.savefig(out.with_suffix(".pdf"), dpi=450, bbox_inches="tight",
                facecolor="white")
    print(f"Saved: {out} and {out.with_suffix('.pdf')} (font: {plt.rcParams['font.family'][0]})")


if __name__ == "__main__":
    main()
