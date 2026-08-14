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
# ]
# ///
"""Topo + OSM (buildings w/ names, roads) around a GPS point -> DXF."""
import argparse
import math
import sys
import time

import numpy as np
import rasterio
from rasterio.windows import from_bounds
from rasterio.transform import xy as px2geo
from scipy.ndimage import gaussian_filter
from skimage import measure
from pyproj import Transformer
import requests
import ezdxf

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
HEADERS = {"User-Agent": "topo2cad/1.0 (personal CAD export script)"}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--radius", type=float, default=500.0, help="meters")
    p.add_argument("--dem", required=True)
    p.add_argument("--out", required=True)
    return p.parse_args()


def bbox_around(lat, lon, radius_m):
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * math.cos(math.radians(lat)))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon  # S, W, N, E


def fetch_osm(s, w, n, e):
    query = f"""
    [out:json][timeout:90];
    (
      way["building"]({s},{w},{n},{e});
      relation["building"]({s},{w},{n},{e});
      way["highway"]({s},{w},{n},{e});
      node["name"]({s},{w},{n},{e});
    );
    out tags geom;
    """
    last_err = None
    for attempt in range(3):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=180)
                r.raise_for_status()
                return r.json()["elements"]
            except Exception as exc:
                last_err = exc
                print(f"Overpass endpoint failed ({url}): {exc}")
        wait = 20 * (attempt + 1)
        print(f"All endpoints failed, retrying in {wait}s...")
        time.sleep(wait)
    raise last_err


def clip_runs(pts, s, w, n, e, margin=0.0005):
    """Split a lon/lat polyline into runs of points inside the bbox (+margin)."""
    runs, cur = [], []
    for lon, lat in pts:
        if (s - margin) <= lat <= (n + margin) and (w - margin) <= lon <= (e + margin):
            cur.append((lon, lat))
        else:
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
    if len(cur) >= 2:
        runs.append(cur)
    return runs


def best_name(tags):
    return tags.get("name") or tags.get("name:en") or tags.get("name:th")


def main():
    a = parse_args()
    s, w, n, e = bbox_around(a.lat, a.lon, a.radius)
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32647", always_xy=True)

    # ---- DEM -> contours -------------------------------------------------
    with rasterio.open(a.dem) as src:
        win = from_bounds(w, s, e, n, src.transform)
        dem = src.read(1, window=win).astype(float)
        wtrans = src.window_transform(win)
        nodata = src.nodata
    if nodata is not None:
        dem[dem == nodata] = np.nan
    print(f"DEM window: {dem.shape}, elev min/max = {np.nanmin(dem):.1f}/{np.nanmax(dem):.1f} m")

    smooth = gaussian_filter(dem, sigma=1.5)
    lo, hi = np.nanpercentile(smooth, [2, 98])
    span = max(hi - lo, 1.0)
    # pick a "nice" interval giving ~10 levels
    for interval in (0.5, 1, 2, 5, 10, 20, 50):
        if span / interval <= 12:
            break
    levels = np.arange(math.floor(lo / interval) * interval,
                       math.ceil(hi / interval) * interval + interval, interval)
    print(f"Contour interval: {interval} m ({len(levels)} levels)")

    contours = []  # (elev, [(x_utm, y_utm), ...])
    for lev in levels:
        for seg in measure.find_contours(smooth, lev):
            if len(seg) < 3:
                continue
            rows, cols = seg[:, 0], seg[:, 1]
            xs, ys = px2geo(wtrans, rows, cols)
            ux, uy = to_utm.transform(xs, ys)
            contours.append((float(lev), list(zip(ux, uy))))

    # ---- OSM buildings + roads ------------------------------------------
    print("Fetching OSM data (Overpass)...")
    elements = fetch_osm(s, w, n, e)
    buildings, roads, pois = [], [], []
    for el in elements:
        tags = el.get("tags", {})
        if el["type"] == "node" and best_name(tags):
            pois.append((best_name(tags), el["lon"], el["lat"]))
        elif el["type"] == "way" and "geometry" in el:
            pts = [(g["lon"], g["lat"]) for g in el["geometry"]]
            if "building" in tags:
                buildings.append((best_name(tags), pts))
            elif "highway" in tags:
                roads.append((best_name(tags), pts))
        elif el["type"] == "relation" and "building" in tags:
            for m in el.get("members", []):
                if m.get("role") == "outer" and "geometry" in m:
                    pts = [(g["lon"], g["lat"]) for g in m["geometry"]]
                    buildings.append((best_name(tags), pts))
                    break
    print(f"OSM: {len(buildings)} buildings, {len(roads)} road segments, {len(pois)} named POIs")

    # ---- DXF -------------------------------------------------------------
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    for name, color in [("CONTOURS", 8), ("CONTOUR_LABELS", 8),
                        ("BUILDINGS", 4), ("BUILDING_NAMES", 2),
                        ("ROADS", 5), ("ROAD_NAMES", 3),
                        ("POI", 6), ("POI_NAMES", 6), ("CENTER", 1)]:
        doc.layers.add(name, color=color)

    for lev, pts in contours:
        msp.add_polyline3d([(x, y, lev) for x, y in pts],
                           dxfattribs={"layer": "CONTOURS"})
        mx, my = pts[len(pts) // 2]
        msp.add_text(f"{lev:g}", height=2.5,
                     dxfattribs={"layer": "CONTOUR_LABELS"}).set_placement((mx, my))

    for name, pts in buildings:
        ux, uy = to_utm.transform(*zip(*pts))
        upts = list(zip(ux, uy))
        msp.add_lwpolyline(upts, close=True, dxfattribs={"layer": "BUILDINGS"})
        if name:
            cx, cy = float(np.mean(ux)), float(np.mean(uy))
            msp.add_text(name, height=4.0,
                         dxfattribs={"layer": "BUILDING_NAMES"}).set_placement((cx, cy))

    labeled_roads = set()
    for name, pts in roads:
        for run in clip_runs(pts, s, w, n, e):
            ux, uy = to_utm.transform(*zip(*run))
            upts = list(zip(ux, uy))
            msp.add_lwpolyline(upts, dxfattribs={"layer": "ROADS"})
            if name and name not in labeled_roads:
                labeled_roads.add(name)
                mid = len(upts) // 2
                msp.add_text(name, height=5.0,
                             dxfattribs={"layer": "ROAD_NAMES"}).set_placement(upts[mid])

    for name, plon, plat in pois:
        px, py = to_utm.transform(plon, plat)
        msp.add_circle((px, py), radius=2, dxfattribs={"layer": "POI"})
        msp.add_text(name, height=4.0,
                     dxfattribs={"layer": "POI_NAMES"}).set_placement((px + 3, py))

    cx, cy = to_utm.transform(a.lon, a.lat)
    msp.add_circle((cx, cy), radius=5, dxfattribs={"layer": "CENTER"})
    msp.add_text(f"GPS {a.lat},{a.lon}", height=5.0,
                 dxfattribs={"layer": "CENTER"}).set_placement((cx + 8, cy))

    doc.saveas(a.out)
    print(f"Saved: {a.out}")
    print(f"CRS: EPSG:32647 (UTM 47N), units = meters. Center at UTM ({cx:.1f}, {cy:.1f})")


if __name__ == "__main__":
    sys.exit(main())
