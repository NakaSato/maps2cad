# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "requests",
#   "pyproj",
#   "shapely>=2.0",
#   "matplotlib>=3.9",
# ]
# ///
"""Detailed Site Map Generator (spec v1.0).

Rectangular site map centred on a GPS coordinate: classified roads,
building footprints with verified names or B### codes, water, POIs,
map furniture, vector PDF + optional PNG, and a building inventory CSV
supporting a manual name-correction workflow.
"""
import argparse
import csv
import gzip
import json
import math
import sys
import time
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import box, LineString, Polygon
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as pe
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon as MplPolygon

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
HEADERS = {"User-Agent": "detailed-site-map/1.0 (site plan generator)"}
MS_LINKS_URL = ("https://minedbuildings.z5.web.core.windows.net/"
                "global-buildings/dataset-links.csv")

# FR-02 road classification
MAJOR = {"motorway", "trunk", "primary"}
MAIN = {"secondary", "tertiary"}
LOCAL = {"residential", "unclassified", "service"}
CLASS_STYLE = {  # (colour, casing lw, fill lw)
    "major": ("#C45A00", 9.0, 6.4),
    "main": ("#E07A00", 7.0, 4.8),
    "local": ("#8a8a8a", 5.0, 3.4),
    "minor": ("#a0a0a0", 2.2, 0.0),  # single dashed line
}
# 8.2 colours
C_BLD_FILL, C_BLD_EDGE = "#DCEFF2", "#008C99"
C_WATER_FILL, C_WATER_EDGE = "#D7EBFF", "#1D5DB8"
C_MARKER = "#D90429"
C_TEXT, C_TEXT2 = "#102A43", "#486174"
HALO = [pe.withStroke(linewidth=2.2, foreground="white")]


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--width", type=float, default=500.0, help="metres east-west")
    p.add_argument("--height", type=float, default=250.0, help="metres north-south")
    p.add_argument("--output", required=True, help="vector PDF path (.pdf)")
    p.add_argument("--png", help="optional 300 DPI PNG preview path")
    p.add_argument("--inventory", default="building_inventory.csv",
                   help="building inventory CSV to write")
    p.add_argument("--labels-csv", help="verified building-name overrides CSV")
    p.add_argument("--font", help="path to a Thai-capable TTF/OTF font")
    p.add_argument("--no-codes", action="store_true",
                   help="hide B### codes on unnamed buildings")
    p.add_argument("--title", default="DETAILED SITE MAP")
    a = p.parse_args()
    if not -90 <= a.lat <= 90:
        die(f"latitude {a.lat} outside valid range -90..90")
    if not -180 <= a.lon <= 180:
        die(f"longitude {a.lon} outside valid range -180..180")
    if not a.output.lower().endswith(".pdf"):
        die("--output must end in .pdf")
    if a.width <= 0 or a.height <= 0:
        die("--width/--height must be positive metres")
    return a


def setup_font(font_path):
    if font_path:
        fp = Path(font_path)
        if not fp.exists():
            die(f"font file not found: {font_path}")
        font_manager.fontManager.addfont(str(fp))
        name = font_manager.FontProperties(fname=str(fp)).get_name()
        plt.rcParams["font.family"] = name
        return name
    names = {f.name for f in font_manager.fontManager.ttflist}
    for n in ("Sarabun", "Noto Sans Thai", "Thonburi", "Ayuthaya"):
        if n in names:
            plt.rcParams["font.family"] = n
            return n
    return plt.rcParams["font.family"]


def fetch_osm(s, w, n, e):
    bbox = f"{s},{w},{n},{e}"
    query = f"""
    [out:json][timeout:90];
    (
      way["building"]({bbox}); relation["building"]({bbox});
      way["highway"]({bbox});
      way["waterway"]({bbox}); way["natural"="water"]({bbox});
      way["leisure"]({bbox}); way["amenity"]({bbox});
      way["shop"]({bbox}); way["office"]({bbox}); way["tourism"]({bbox});
      node["name"]({bbox});
    );
    out tags geom;
    """
    last = None
    for attempt in range(3):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS,
                                  timeout=180)
                r.raise_for_status()
                return r.json()["elements"]
            except Exception as exc:
                last = exc
                print(f"Overpass endpoint failed ({url}): {exc}")
        time.sleep(20 * (attempt + 1))
    die(f"OpenStreetMap request failed after retries: {last}")


def quadkey(lat, lon, z=9):
    sl = math.sin(math.radians(lat))
    x = (lon + 180) / 360
    y = 0.5 - math.log((1 + sl) / (1 - sl)) / (4 * math.pi)
    tx, ty = int(x * 2**z), int(y * 2**z)
    return "".join(str((((ty >> (z - 1 - i)) & 1) << 1) | ((tx >> (z - 1 - i)) & 1))
                   for i in range(z))


def fetch_ms_buildings(s, w, n, e, cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    links = cache_dir / "dataset-links.csv"
    try:
        if not links.exists():
            print("Downloading MS buildings index...")
            links.write_bytes(requests.get(MS_LINKS_URL, headers=HEADERS,
                                           timeout=300).content)
        keys = {quadkey(la, lo) for la in (s, n) for lo in (w, e)}
        urls = [ln.split(",")[2] for ln in links.read_text().splitlines()
                if ln.split(",")[1:2] and ln.split(",")[1] in keys]
        rings = []
        for url in urls:
            tile = cache_dir / url.rsplit("/quadkey=", 1)[1].replace("/", "_")
            if not tile.exists():
                print(f"Downloading MS buildings tile {tile.name}...")
                tile.write_bytes(requests.get(url, headers=HEADERS,
                                              timeout=600).content)
            with gzip.open(tile, "rt") as f:
                for line in f:
                    geom = json.loads(line)["geometry"]
                    if geom["type"] != "Polygon":
                        continue
                    ring = geom["coordinates"][0]
                    if any(s <= la <= n and w <= lo <= e for lo, la in ring):
                        rings.append(ring)
        return rings
    except Exception as exc:
        print(f"WARNING: Microsoft footprints unavailable ({exc}); "
              "continuing with OSM buildings only.")
        return []


def load_label_overrides(path):
    overrides = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            cols = set(reader.fieldnames or [])
            if not {"feature_id", "display_name"} <= cols:
                die(f"labels CSV {path} must contain 'feature_id' and "
                    f"'display_name' columns (found: {sorted(cols)})")
            for row in reader:
                name = (row.get("display_name") or "").strip()
                if name:
                    overrides[row["feature_id"].strip()] = name
    except FileNotFoundError:
        die(f"labels CSV not found: {path}")
    except csv.Error as exc:
        die(f"labels CSV {path} is not valid CSV: {exc}")
    return overrides


def road_class(hw):
    if hw in MAJOR:
        return "major"
    if hw in MAIN:
        return "main"
    if hw in LOCAL:
        return "local"
    return "minor"


def main():
    a = parse_args()
    font = setup_font(a.font)

    # 6.3 exact rectangle in projected metres, centred on the site
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:32647", always_xy=True)
    gx, gy = to_utm.transform(a.lon, a.lat)
    ux0, ux1 = gx - a.width / 2, gx + a.width / 2
    uy0, uy1 = gy - a.height / 2, gy + a.height / 2
    extent = box(ux0, uy0, ux1, uy1)
    to_wgs = Transformer.from_crs("EPSG:32647", "EPSG:4326", always_xy=True)
    # geographic bbox (slightly padded) for the data request
    pad = 1.0002
    w, s = to_wgs.transform(gx - a.width / 2 * pad, gy - a.height / 2 * pad)
    e, n = to_wgs.transform(gx + a.width / 2 * pad, gy + a.height / 2 * pad)

    elements = fetch_osm(s, w, n, e)

    roads, water_areas, water_lines = [], [], []
    buildings, pois, area_labels = [], [], []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("name:en") or tags.get("name:th")
        if el["type"] == "node" and name:
            pois.append((name, *to_utm.transform(el["lon"], el["lat"])))
            continue
        if "geometry" not in el and el["type"] != "relation":
            continue
        if el["type"] == "way":
            pts = [to_utm.transform(g["lon"], g["lat"]) for g in el["geometry"]]
        if "building" in tags and el["type"] == "way" and len(pts) >= 4:
            buildings.append((f"way/{el['id']}", tags, Polygon(pts)))
        elif el["type"] == "relation" and "building" in tags:
            for m in el.get("members", []):
                if m.get("role") == "outer" and "geometry" in m:
                    pts = [to_utm.transform(g["lon"], g["lat"])
                           for g in m["geometry"]]
                    if len(pts) >= 4:
                        buildings.append((f"relation/{el['id']}", tags,
                                          Polygon(pts)))
                    break
        elif "highway" in tags and len(pts) >= 2:
            roads.append((road_class(tags["highway"]), name, LineString(pts)))
        elif tags.get("natural") == "water" and len(pts) >= 4:
            water_areas.append(Polygon(pts))
        elif "waterway" in tags and len(pts) >= 2:
            water_lines.append(LineString(pts))
        elif name and len(pts) >= 3 and any(
                k in tags for k in ("amenity", "leisure", "shop", "office",
                                    "tourism")):
            rp = Polygon(pts).representative_point()
            area_labels.append((name, rp.x, rp.y))

    n_osm_bld = len(buildings)
    ms_used = False
    if n_osm_bld < 20:
        print("Few OSM buildings — supplementing with Microsoft ML footprints...")
        rings = fetch_ms_buildings(s, w, n, e,
                                   Path(__file__).resolve().parent.parent
                                   / "dem" / "ms_cache")
        for i, ring in enumerate(rings):
            poly = Polygon([to_utm.transform(lo, la) for lo, la in ring])
            if poly.is_valid and poly.intersects(extent):
                buildings.append((f"ms/{i}", {"building": "ms_footprint"}, poly))
        ms_used = len(buildings) > n_osm_bld

    # clip buildings to extent, drop slivers
    clipped = []
    for fid, tags, poly in buildings:
        try:
            inter = poly.buffer(0).intersection(extent)
        except Exception:
            continue
        if inter.is_empty or inter.area < 1.0:
            continue
        geoms = list(inter.geoms) if inter.geom_type == "MultiPolygon" else [inter]
        for g in geoms:
            if g.geom_type == "Polygon" and g.area >= 1.0:
                clipped.append((fid, tags, g))
    # 6.4/FR-05: deterministic B### codes (north->south, west->east)
    clipped.sort(key=lambda b: (-b[2].representative_point().y,
                                b[2].representative_point().x))
    overrides = load_label_overrides(a.labels_csv) if a.labels_csv else {}

    inventory, seen_fid = [], set()
    for i, (fid, tags, poly) in enumerate(clipped, 1):
        code = f"B{i:03d}"
        rp = poly.representative_point()
        rlon, rlat = to_wgs.transform(rp.x, rp.y)
        osm_name = (tags.get("name") or tags.get("name:en")
                    or tags.get("name:th") or "")
        display = overrides.get(fid, "") or osm_name or code
        inventory.append({"feature_id": fid, "code": code,
                          "osm_name": osm_name, "display_name": display,
                          "building_type": tags.get("building", ""),
                          "latitude": f"{rlat:.7f}", "longitude": f"{rlon:.7f}",
                          "_poly": poly, "_rp": rp})

    if not roads and not clipped:
        print("WARNING: empty map result — no roads or buildings found "
              "in the requested extent.")

    # ---- render --------------------------------------------------------
    # exact layout in inches: map panel sized to the extent aspect ratio,
    # furniture strip below, title strip above — nothing overlaps
    map_w_in = 16 * 0.93
    map_h_in = map_w_in * (a.height / a.width)
    strip_in, title_in = 2.55, 0.65
    fig_h = map_h_in + strip_in + title_in
    fig = plt.figure(figsize=(16, fig_h))
    ax = fig.add_axes([0.035, strip_in / fig_h, 0.93, map_h_in / fig_h])
    ax.set_facecolor("white")
    ax.set_aspect("equal")
    ax.set_xlim(ux0, ux1)
    ax.set_ylim(uy0, uy1)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_linewidth(2)
        spine.set_color(C_TEXT)

    for poly in water_areas:
        inter = poly.buffer(0).intersection(extent)
        if inter.is_empty:
            continue
        geoms = (inter.geoms if inter.geom_type == "MultiPolygon" else [inter])
        for g in geoms:
            if g.geom_type == "Polygon":
                ax.add_patch(MplPolygon(list(g.exterior.coords), closed=True,
                                        facecolor=C_WATER_FILL,
                                        edgecolor=C_WATER_EDGE, lw=1.0,
                                        zorder=1))
    for line in water_lines:
        inter = line.intersection(extent)
        geoms = (inter.geoms if inter.geom_type.startswith("Multi") else [inter])
        for g in geoms:
            if g.geom_type == "LineString":
                ax.plot(*g.xy, color=C_WATER_EDGE, lw=2.2, zorder=1.5)

    for fid, tags, poly in [(r["feature_id"], None, r["_poly"])
                            for r in inventory]:
        ax.add_patch(MplPolygon(list(poly.exterior.coords), closed=True,
                                facecolor=C_BLD_FILL, edgecolor=C_BLD_EDGE,
                                lw=0.6, zorder=2))

    # roads: casing pass then fill pass, minor drawn as dashed single lines
    def road_segments(geom):
        inter = geom.intersection(extent)
        geoms = (inter.geoms if inter.geom_type.startswith("Multi") else [inter])
        return [g for g in geoms if g.geom_type == "LineString"]

    order = ["minor", "local", "main", "major"]
    for cls in order:
        colour, cw, fw = CLASS_STYLE[cls]
        for rcls, _, geom in roads:
            if rcls != cls:
                continue
            for g in road_segments(geom):
                if cls == "minor":
                    ax.plot(*g.xy, color=colour, lw=1.2, zorder=3,
                            linestyle=(0, (4, 2.5)))
                else:
                    ax.plot(*g.xy, color=colour, lw=cw, zorder=3.0 + 0.01 *
                            order.index(cls), solid_capstyle="round",
                            solid_joinstyle="round")
    for cls in order:
        colour, cw, fw = CLASS_STYLE[cls]
        if fw <= 0:
            continue
        for rcls, _, geom in roads:
            if rcls != cls:
                continue
            for g in road_segments(geom):
                ax.plot(*g.xy, color="white", lw=fw, zorder=3.5,
                        solid_capstyle="round", solid_joinstyle="round")

    # FR-03: one label per unique named road, along its longest segment
    labeled = set()
    for rcls, name, geom in sorted(
            roads, key=lambda r: -r[2].intersection(extent).length):
        if not name or name in labeled:
            continue
        segs = road_segments(geom)
        if not segs:
            continue
        seg = max(segs, key=lambda g: g.length)
        if seg.length < 25:
            continue
        labeled.add(name)
        mid = seg.interpolate(0.5, normalized=True)
        p1 = seg.interpolate(0.45, normalized=True)
        p2 = seg.interpolate(0.55, normalized=True)
        ang = math.degrees(math.atan2(p2.y - p1.y, p2.x - p1.x))
        if ang > 90:
            ang -= 180
        elif ang < -90:
            ang += 180
        ax.text(mid.x, mid.y, name, fontsize=10, color=C_TEXT, rotation=ang,
                rotation_mode="anchor", ha="center", va="bottom", zorder=6,
                path_effects=HALO)

    # FR-05 building labels at interior representative points
    for row in inventory:
        poly, rp = row["_poly"], row["_rp"]
        has_name = row["display_name"] != row["code"]
        small = math.sqrt(poly.area) < 12
        if has_name and not (small or len(row["display_name"]) > 24):
            ax.text(rp.x, rp.y, row["display_name"], fontsize=6.5,
                    color=C_TEXT, ha="center", va="center", zorder=5,
                    path_effects=HALO)
        elif not a.no_codes:
            ax.text(rp.x, rp.y, row["code"], fontsize=5.2, color=C_TEXT2,
                    ha="center", va="center", zorder=5, path_effects=HALO)

    for name, px, py in pois + area_labels:
        if ux0 < px < ux1 and uy0 < py < uy1:
            ax.plot(px, py, "o", ms=4, color=C_TEXT, zorder=6)
            ax.annotate(name, (px, py), fontsize=9, color=C_TEXT, zorder=6,
                        xytext=(4, 4), textcoords="offset points",
                        path_effects=HALO)

    # FR-09 site marker above everything
    ax.plot(gx, gy, marker="v", ms=18, color=C_MARKER, zorder=10)
    ax.plot(gx, gy, marker=".", ms=5, color="white", zorder=11)

    # FR-10 north arrow + scale bar
    nx = ux1 - a.width * 0.035
    ny = uy1 - a.height * 0.09
    asz = min(a.width, a.height) * 0.035
    ax.annotate("N", (nx, ny + asz * 1.3), fontsize=17, ha="center",
                weight="bold", color=C_TEXT, zorder=9, path_effects=HALO)
    ax.add_patch(MplPolygon([(nx - asz * 0.35, ny - asz), (nx, ny + asz),
                             (nx + asz * 0.35, ny - asz),
                             (nx, ny - asz * 0.5)],
                            closed=True, facecolor=C_TEXT, zorder=9))
    bar = 10 ** math.floor(math.log10(a.width / 5))
    if a.width / 5 / bar >= 5:
        bar *= 5
    elif a.width / 5 / bar >= 2:
        bar *= 2
    bx0, by0 = ux0 + a.width * 0.04, uy0 + a.height * 0.06
    ax.plot([bx0, bx0 + bar], [by0, by0], color=C_TEXT, lw=3.5, zorder=9,
            path_effects=HALO)
    ax.text(bx0 + bar / 2, by0, f"{bar:g} m", fontsize=11, ha="center",
            va="bottom", color=C_TEXT, zorder=9, path_effects=HALO)

    # FR-10 title, legend, metadata, attribution, feature summary
    fig.suptitle(a.title, fontsize=22, weight="bold", color=C_TEXT,
                 y=(strip_in + map_h_in + 0.22) / fig_h, va="bottom")
    handles = [
        Line2D([], [], color=CLASS_STYLE["major"][0], lw=4, label="Major road"),
        Line2D([], [], color=CLASS_STYLE["main"][0], lw=3, label="Main local road"),
        Line2D([], [], color=CLASS_STYLE["local"][0], lw=2.4, label="Local road"),
        Line2D([], [], color=CLASS_STYLE["minor"][0], lw=1.5,
               linestyle="--", label="Minor access"),
        Patch(facecolor=C_BLD_FILL, edgecolor=C_BLD_EDGE, label="Building"),
        Patch(facecolor=C_WATER_FILL, edgecolor=C_WATER_EDGE, label="Water"),
        Line2D([], [], color=C_MARKER, marker="v", linestyle="",
               markersize=10, label="Site location"),
    ]
    fig.legend(handles=handles, loc="lower left", ncol=4, frameon=False,
               bbox_to_anchor=(0.03, 1.05 / fig_h), fontsize=10)
    n_named = sum(1 for r in inventory if r["display_name"] != r["code"])
    meta = [
        f"Site: {a.lat}, {a.lon} (WGS 84)   |   CRS: WGS 84 / UTM Zone 47N "
        f"(EPSG:32647)   |   Coverage: {a.width:g} m × {a.height:g} m",
        f"Features: {len(roads)} road segments ({len(labeled)} named roads "
        f"labeled), {len(inventory)} buildings ({n_named} named, "
        f"{len(inventory) - n_named} coded), {len(water_areas)} water areas, "
        f"{len(water_lines)} waterways",
        "Map data © OpenStreetMap contributors"
        + ("   |   Building footprints © Microsoft (ODbL), AI-detected"
           if ms_used else ""),
    ]
    for i, line in enumerate(meta):
        fig.text(0.035, (0.82 - i * 0.26) / fig_h, line, fontsize=9.5,
                 color=C_TEXT2 if i else C_TEXT)

    # FR-11 outputs
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.savefig(out, facecolor="white")
    except OSError as exc:
        die(f"cannot write output PDF {out}: {exc}")
    print(f"Saved PDF: {out}")
    if a.png:
        png = Path(a.png)
        png.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(png, dpi=300, facecolor="white")
        print(f"Saved PNG (300 dpi): {png}")

    # FR-06 inventory CSV
    inv = Path(a.inventory)
    inv.parent.mkdir(parents=True, exist_ok=True)
    with open(inv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "feature_id", "code", "osm_name", "display_name",
            "building_type", "latitude", "longitude"])
        writer.writeheader()
        for row in inventory:
            writer.writerow({k: v for k, v in row.items()
                             if not k.startswith("_")})
    print(f"Saved inventory: {inv} ({len(inventory)} buildings)")
    print(f"Font: {font}")


if __name__ == "__main__":
    main()
