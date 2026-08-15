#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Local web front end for the Detailed Site Map Generator.

Start:  uv run scripts/serve.py              (http://127.0.0.1:8765)
        uv run scripts/serve.py --port 9000 --host 0.0.0.0

Stdlib only: the generator subprocess carries its own dependencies.

Serves a form, runs generate_detailed_site_map.py per request in a
subprocess, and returns the rendered sheet with download links. Binds to
localhost by default: the generator makes outbound Overpass calls and
writes files, so do not expose it to an untrusted network.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import html
import json
import math
import http.cookies
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
GEN = BASE / "generate_detailed_site_map.py"
CAD = BASE / "topo2cad.py"
DXF2PDF = BASE / "dxf2pdf.py"
DB2DXF = BASE / "db2dxf.py"
GIS2CAD = BASE / "gis2cad.py"
OSM2CAD = BASE / "osm2cad.py"
STAGE_DB = BASE / "stage_db.py"
# Staging database every CAD run is written into; browsable and editable
# from /projects. Overridable with --db.
STAGING_DB = BASE.parent / "output" / "staging.sqlite"
# Generated sheets live under the project's gitignored output/ directory
OUT = BASE.parent / "output" / "web"
DEM_DIR = BASE.parent / "dem"

# Google sign-in is optional: with the environment variables unset the app
# behaves exactly as before and no Drive button is rendered. Imported from
# the same directory, like sheet.py is by topo2cad.py.
sys.path.insert(0, str(BASE))
import gdrive                                              # noqa: E402

SESSIONS = None            # SessionStore, created in main() under data-dir
PENDING = {}               # oauth state -> (verifier, return_path)
PENDING_LOCK = threading.Lock()
DEM_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
           "Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM/"
           "Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM.tif")

# Each script declares its own dependencies (PEP 723), so run them through
# uv when it is available instead of this process's interpreter.
UV = shutil.which("uv")

# Background-map providers offered in the browser. The CLI also accepts a
# raw tile URL template; the web form deliberately does not, because a
# free-text URL would let anyone with the page point this server's fetcher
# at any host.
BASEMAP_CHOICES = {
    "": "None",
    "osm": "OpenStreetMap (standard)",
    "opentopomap": "OpenTopoMap (contours + hillshade)",
    "esri-topo": "Esri topographic",
    "esri-imagery": "Esri satellite imagery",
    "esri-street": "Esri street map",
    "carto-light": "Carto light (muted)",
    "carto-dark": "Carto dark",
    "carto-voyager": "Carto voyager",
    "osm-hot": "OSM humanitarian",
    "cyclosm": "CyclOSM",
}


def script_cmd(script: Path) -> list[str]:
    return [UV, "run", str(script)] if UV else [sys.executable, str(script)]

# Only these names may be fetched back from disk, keyed by job id.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

COORD_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$")

GOV_FIELDS = [
    ("project_name", "ชื่อโครงการ / Project name"),
    ("site_location", "สถานที่ตั้ง / Site location"),
    ("subdistrict", "ตำบล / Subdistrict"),
    ("district", "อำเภอ / District"),
    ("province", "จังหวัด / Province"),
    ("agency", "หน่วยงาน / Agency"),
    ("prepared_by", "ผู้จัดทำ / Prepared by"),
    ("checked_by", "ผู้ตรวจสอบ / Checked by"),
    ("approved_by", "ผู้อนุมัติ / Approved by"),
    ("drawing_no", "เลขที่แบบ / Drawing no."),
    ("sheet", "แผ่นที่ / Sheet"),
    ("revision", "ครั้งที่แก้ไข / Revision"),
]


class BadRequest(Exception):
    """Invalid form input, reported back to the browser."""


def dem_tile_for(lat: float, lon: float) -> tuple[Path, str]:
    """Copernicus tiles are 1°x1°, named by the floored integer degree."""
    la, lo = math.floor(lat), math.floor(lon)
    ns = f"{'N' if la >= 0 else 'S'}{abs(la):02d}"
    ew = f"{'E' if lo >= 0 else 'W'}{abs(lo):03d}"
    path = DEM_DIR / f"dem_{ns.lower()}_{ew.lower()}.tif"
    return path, DEM_URL.format(ns=ns, ew=ew)


def ensure_dem(lat: float, lon: float) -> Path:
    """Fetch the DEM tile covering the site if it isn't cached yet."""
    path, url = dem_tile_for(lat, lon)
    if path.is_file() and path.stat().st_size > 1_000_000:
        return path
    DEM_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  downloading DEM tile {path.name} (~40 MB, once per 1°x1° tile)")
    tmp = path.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=600) as r, \
                open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(path)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise BadRequest(
            f"Could not download the elevation tile for this location "
            f"({type(e).__name__}: {e}). The CAD export needs it for "
            "contours; the site map does not.")
    return path


# ----------------------------------------------------------------- rendering
def parse_form(form: dict[str, list[str]]) -> dict:
    def one(key, default=""):
        return (form.get(key) or [default])[0].strip()

    coords = one("coords")
    lat_s, lon_s = one("lat"), one("lon")
    if coords:
        m = COORD_RE.match(coords)
        if not m:
            raise BadRequest(
                f"Could not read “{coords}” as coordinates. Use "
                "“latitude, longitude”, for example 15.83384548, 104.39445555.")
        lat_s, lon_s = m.group(1), m.group(2)
    if not lat_s or not lon_s:
        raise BadRequest("Enter a latitude and longitude.")

    def number(value, label, lo, hi):
        try:
            n = float(value)
        except ValueError:
            raise BadRequest(f"{label} must be a number (got “{value}”).")
        if not (lo <= n <= hi):
            raise BadRequest(f"{label} must be between {lo} and {hi}.")
        return n

    lat = number(lat_s, "Latitude", -90, 90)
    lon = number(lon_s, "Longitude", -180, 180)
    # 1000 x 750 m — larger than A3 holds at 1:2000, so a sheet export
    # lands on 1:5000 (A3), 1:2500 (A2) or 1:2000 (A1)
    width = number(one("width", "1000"), "Width", 20, 20000)
    height = number(one("height", "750"), "Height", 20, 20000)

    export = one("export", "both")
    if export not in ("both", "cad", "map"):
        export = "both"

    profile = one("profile", "standard")
    if profile not in ("standard", "government"):
        profile = "standard"
    sheet_size = one("sheet_size", "A3")
    if sheet_size not in ("A4", "A3", "A2", "A1"):
        sheet_size = "A3"

    # Plottable paper-space sheet for the DXF (separate from the site map's
    # sheet size above, which is a raster/PDF page)
    # A3 now that the default extent fits it at 1:2000; it was A2 only
    # because 770 m of width overflowed the A3 viewport.
    cad_sheet = one("cad_sheet", "A3")
    if cad_sheet not in ("", "none", "A4", "A3", "A2", "A1", "A0"):
        cad_sheet = "A3"
    cad_scale = one("cad_scale", "fit")
    if cad_scale != "fit" and not cad_scale.isdigit():
        cad_scale = "fit"

    # Background map under the CAD linework. Only the named providers are
    # offered here — a free-text tile URL from a browser form would let the
    # server be pointed at any host, which the CLI may do and a web app
    # should not.
    basemap = one("basemap", "")
    if basemap not in BASEMAP_CHOICES:
        basemap = ""

    return {
        "lat": lat, "lon": lon, "width": width, "height": height,
        "export": export, "profile": profile, "sheet_size": sheet_size,
        "cad_sheet": cad_sheet, "cad_scale": cad_scale, "basemap": basemap,
        "title": one("title", "Detailed Site Map"),
        "codes": one("codes", "on") == "on",
        "final": one("final") == "on",
        "gov": {k: one(k) for k, _ in GOV_FIELDS},
    }


def job_id(p: dict) -> str:
    key = json.dumps(p, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def run_step(cmd: list[str], what: str, timeout=900) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    log = "\n".join(s for s in (proc.stdout, proc.stderr) if s.strip())
    # matplotlib font chatter and uv install noise are not useful here
    log = "\n".join(ln for ln in log.splitlines()
                    if "UserWarning" not in ln and "savefig" not in ln
                    and "fsSelection" not in ln
                    and not ln.startswith(("Installed", "Resolved", "Built",
                                           "Downloading", "Prepared",
                                           " Downloaded", "Updating"))
                    and ln.strip())
    if proc.returncode != 0:
        raise BadRequest(f"{what} failed:\n{log or '(no output)'}")
    return log


def run_generator(p: dict) -> dict:
    """Render the requested exports into one job folder."""
    jid = job_id(p)
    run = OUT / jid
    run.mkdir(parents=True, exist_ok=True)
    record = {"id": jid, "params": p, "dir": str(run),
              "when": datetime.now().strftime("%Y-%m-%d %H:%M")}
    logs = []

    if p["export"] in ("both", "map"):
        pdf = str(run / "site_map.pdf")
        png = str(run / "site_map.png")
        csv = str(run / "building_inventory.csv")
        cmd = script_cmd(GEN) + [
            "--lat", repr(p["lat"]), "--lon", repr(p["lon"]),
            "--width", repr(p["width"]), "--height", repr(p["height"]),
            "--output", pdf, "--png", png, "--inventory", csv,
            "--profile", p["profile"], "--sheet-size", p["sheet_size"],
            "--title", p["title"]]
        if not p["codes"]:
            cmd.append("--no-building-codes")
        if p["profile"] == "government":
            if p["final"]:
                cmd.append("--final")
            for key, _ in GOV_FIELDS:
                if p["gov"].get(key):
                    cmd += [f"--{key.replace('_', '-')}", p["gov"][key]]
        logs.append(run_step(cmd, "Site map"))
        record.update(pdf=pdf, png=png, csv=csv)

    if p["export"] in ("both", "cad"):
        dem = ensure_dem(p["lat"], p["lon"])
        dxf = str(run / "site.dxf")
        project = (p.get("gov", {}).get("project_name")
                   or f"{p['lat']:.6f}_{p['lon']:.6f}_"
                      f"{p['width']:.0f}x{p['height']:.0f}")
        cad_cmd = script_cmd(CAD) + [
            "--lat", repr(p["lat"]), "--lon", repr(p["lon"]),
            "--width", repr(p["width"]), "--height", repr(p["height"]),
            "--dem", str(dem), "--out", dxf,
            "--db", str(STAGING_DB), "--project", project]
        sheet = p.get("cad_sheet") or ""
        if sheet and sheet != "none":
            cad_cmd += ["--sheet", sheet,
                        "--scale", str(p.get("cad_scale", "fit"))]
        if p.get("basemap"):
            cad_cmd += ["--basemap", p["basemap"]]
        logs.append(run_step(cad_cmd, "CAD export"))
        record["project"] = project
        record["dxf"] = dxf
        # Files the CAD step writes beside the drawing, when it wrote them
        for kind in ("tif", "attrs"):
            if (run / KINDS[kind]).is_file():
                record[kind] = str(run / KINDS[kind])
        plot = str(run / "site_preview.pdf")
        try:
            # With a sheet, plot the titled layout at its own paper size
            pdf_cmd = script_cmd(DXF2PDF) + [dxf, "-o", plot]
            pdf_cmd += (["--layout", "SHEET", "--size", sheet]
                        if sheet and sheet != "none" else ["--size", "A3"])
            logs.append(run_step(pdf_cmd, "DXF plot preview"))
            record["plot"] = plot
        except BadRequest as e:      # preview is a convenience, not the export
            logs.append(f"NOTE: plot preview unavailable — {e}")

    record["log"] = "\n".join(logs)
    save_job(record)
    with JOBS_LOCK:
        JOBS[jid] = record
    return record


# ------------------------------------------------------------------ history
KINDS = {"pdf": "site_map.pdf", "png": "site_map.png",
         "csv": "building_inventory.csv", "dxf": "site.dxf",
         "plot": "site_preview.pdf",
         # The DXF stores a path to the background map, not its pixels, so
         # the GeoTIFF has to be downloadable beside it or the drawing opens
         # with a missing raster reference.
         "tif": "basemap.tif",
         # Every OSM tag of every drawn feature. The entities carry these as
         # XDATA too, but that needs AutoCAD to read; this opens anywhere.
         "attrs": "attributes.csv"}


def save_job(rec: dict) -> None:
    """Persist a run so history survives a restart."""
    meta = {"id": rec["id"], "params": rec["params"], "when": rec["when"],
            "log": rec.get("log", ""),
            "files": [k for k in KINDS if rec.get(k)]}
    try:
        (Path(rec["dir"]) / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"  could not save history entry: {e}")


def load_jobs() -> int:
    """Rebuild the job table from previous runs on disk."""
    if not OUT.is_dir():
        return 0
    found = 0
    for meta_file in OUT.glob("*/meta.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        folder = meta_file.parent
        rec = {"id": meta["id"], "params": meta["params"],
               "when": meta["when"], "log": meta.get("log", ""),
               "dir": str(folder)}
        for kind in meta.get("files", []):
            target = folder / KINDS[kind]
            if target.is_file():
                rec[kind] = str(target)
        with JOBS_LOCK:
            JOBS[rec["id"]] = rec
        found += 1
    return found


def history(limit: int | None = None) -> list[dict]:
    with JOBS_LOCK:
        rows = sorted(JOBS.values(), key=lambda r: r["when"], reverse=True)
    return rows[:limit] if limit else rows


def history_html(limit: int | None = None) -> str:
    rows = history(limit)
    if not rows:
        return ('<p class="note">No maps generated yet — your runs will be '
                "listed here.</p>")
    out = ['<table class="hist"><thead><tr><th>When</th><th>Location</th>'
           '<th>Area</th><th>Export</th><th>Files</th></tr></thead><tbody>']
    for r in rows:
        p = r["params"]
        # DXF downloads rather than previews: it has no browser viewer, so
        # /view substitutes the plot PDF, and clicking "DXF" to be handed a
        # PDF is not what the link says it does. The rest do preview.
        links = " ".join(
            (f'<a class="dl-dxf" href="/file/{r["id"]}/dxf" download '
             f'title="Download the DXF">⤓ DXF</a>' if k == "dxf" else
             f'<a href="/view/{r["id"]}/{k}" target="_blank" rel="noopener" '
             f'title="Preview {k.upper()}">{k.upper()}</a>')
            for k in ("dxf", "plot", "pdf", "png", "csv") if r.get(k))
        out.append(
            f'<tr><td>{html.escape(r["when"])}</td>'
            f'<td class="num">{p["lat"]:.6f}, {p["lon"]:.6f}</td>'
            f'<td class="num">{p["width"]:.0f} × {p["height"]:.0f} m</td>'
            f'<td>{html.escape(p.get("export", "map"))}</td>'
            f'<td class="dl">{links or "—"}</td></tr>')
    out.append("</tbody></table>")
    return "".join(out)


# --------------------------------------------------------------------- pages
CSS = """
:root{--paper:#FBFAF8;--sheet:#fff;--ink:#102A43;--soft:#486174;
--rule:#C9D6DF;--faint:#E4EBF0;--survey:#C45A00;--marker:#D90429;--teal:#00727C;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--paper:#0E1720;--sheet:#16222E;--ink:#E6EEF4;--soft:#9DB2C2;--rule:#2E4256;
--faint:#223140;--survey:#F08A2E;--marker:#FF5A6E;--teal:#38B0B9;}}
:root[data-theme="dark"]{--paper:#0E1720;--sheet:#16222E;--ink:#E6EEF4;
--soft:#9DB2C2;--rule:#2E4256;--faint:#223140;--survey:#F08A2E;
--marker:#FF5A6E;--teal:#38B0B9;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-size:16px;
line-height:1.6;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:clamp(18px,4vw,44px)}
.frame{border:1.5px solid var(--ink);background:var(--sheet);
padding:clamp(18px,3vw,36px)}
.eyebrow{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
letter-spacing:.16em;text-transform:uppercase;color:var(--soft);margin:0 0 10px}
h1{font-size:clamp(25px,4vw,38px);letter-spacing:-.02em;margin:0 0 8px;
line-height:1.1}
.lede{color:var(--soft);margin:0 0 26px;max-width:60ch}
label{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;
color:var(--soft);margin:0 0 5px}
input[type=text],input[type=number],select{width:100%;padding:9px 11px;
border:1px solid var(--rule);background:var(--paper);color:var(--ink);
font-size:15px;font-family:inherit;border-radius:0}
input:focus,select:focus{outline:2px solid var(--survey);outline-offset:1px}
.grid{display:grid;gap:16px}
.g2{grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.g3{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
fieldset{border:1px solid var(--rule);padding:18px;margin:22px 0 0}
legend{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
letter-spacing:.14em;text-transform:uppercase;color:var(--survey);padding:0 8px}
.check{display:flex;align-items:center;gap:9px;font-size:14.5px;color:var(--ink)}
.check input{width:16px;height:16px;accent-color:var(--survey)}
.checks{display:grid;gap:10px 18px;
grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
button{margin-top:22px;padding:12px 24px;border:1.5px solid var(--ink);
background:var(--ink);color:var(--paper);font-size:15px;font-weight:600;
cursor:pointer;font-family:inherit}
button:hover{background:var(--survey);border-color:var(--survey)}
button:focus-visible{outline:2px solid var(--survey);outline-offset:2px}
a{color:var(--survey)}
.err{border-left:3px solid var(--marker);background:var(--faint);
padding:12px 16px;margin:0 0 22px;white-space:pre-wrap;font-size:14.5px}
figure{margin:24px 0 0;border:1px solid var(--rule)}
figure img{display:block;width:100%;height:auto}
.files{display:flex;gap:10px;flex-wrap:wrap;margin-top:20px}
.card{display:inline-flex;border:1px solid var(--rule);align-items:stretch}
.card:hover{border-color:var(--survey)}
.card>a{display:inline-block;padding:9px 15px;text-decoration:none;
color:var(--ink);font-size:14px}
.card>a:hover{color:var(--survey)}
.card a b{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;
letter-spacing:.1em;color:var(--soft);display:block;font-weight:400}
.card .dlicon{border-left:1px solid var(--rule);display:flex;align-items:center;
padding:0 12px;font-size:16px;color:var(--soft)}
.card .dlicon:hover{color:var(--survey);background:var(--faint)}
/* The CAD export is what most runs are for, so it reads as the one action
   on the page rather than as one tile among five. */
.card.primary{border-color:var(--survey);border-width:1.5px}
.card.primary>a{color:var(--survey);font-weight:600}
.card.primary a b{color:var(--survey);opacity:.75;font-weight:400}
.card.primary>a:hover{background:var(--faint)}
.card.primary .dlicon{border-left-color:var(--survey)}
.files a:focus-visible,.hist a:focus-visible{outline:2px solid var(--survey);
outline-offset:1px}
dl.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
gap:1px;background:var(--rule);border:1px solid var(--rule);margin:22px 0 0}
dl.meta>div{background:var(--sheet);padding:13px 15px}
dl.meta dt{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10px;
letter-spacing:.12em;text-transform:uppercase;color:var(--soft);margin:0 0 4px}
dl.meta dd{margin:0;font-size:15px;font-variant-numeric:tabular-nums}
pre.log{background:var(--faint);border:1px solid var(--rule);padding:14px 16px;
overflow-x:auto;font-size:12.5px;line-height:1.65;margin:22px 0 0;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}
.back{display:inline-block;margin-top:26px;font-size:14.5px}
/* Generating takes 18-105 s depending on whether the DEM tile is cached,
   so the wait needs a real indicator, not a disabled button. */
#busy{display:none;margin-top:20px}
#busy.on{display:block}
.load{border:1px solid var(--rule);border-left:3px solid var(--survey);
padding:14px 16px;background:var(--faint)}
.load-head{display:flex;justify-content:space-between;align-items:baseline;
gap:12px}
.load-title{font-size:14.5px;color:var(--ink);font-weight:600}
.load-time{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:20px;color:var(--survey);font-variant-numeric:tabular-nums;
letter-spacing:.02em}
.load-note{font-size:13px;color:var(--soft);margin-top:6px}
/* Indeterminate: the server sends nothing until it is finished, so a
   percentage would be invented. A sweep says "working" without lying. */
.load-bar{position:relative;height:3px;background:var(--rule);margin-top:12px;
overflow:hidden}
.load-bar::after{content:"";position:absolute;inset:0 auto 0 0;width:40%;
background:var(--survey);animation:sweep 1.6s cubic-bezier(.4,0,.2,1) infinite}
@keyframes sweep{0%{left:-40%}100%{left:100%}}
.load.over .load-time{color:var(--marker)}
@media (prefers-reduced-motion:reduce){
.load-bar::after{animation:none;width:100%;opacity:.4}}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--rule);
font-size:12.5px;color:var(--soft);font-family:ui-monospace,SFMono-Regular,
Menlo,monospace}
h2{font-size:15px;letter-spacing:-.01em;margin:34px 0 12px;padding-bottom:9px;
border-bottom:1px solid var(--rule);display:flex;justify-content:space-between;
align-items:baseline;gap:12px}
h2 a{font-size:12.5px;text-decoration:none}
.hist{width:100%;border-collapse:collapse;font-size:13.5px}
.hist th{text-align:left;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--soft);
font-weight:400;padding:0 12px 7px 0;border-bottom:1px solid var(--rule)}
.hist td{padding:9px 12px 9px 0;border-bottom:1px solid var(--faint);
vertical-align:top}
.hist td.num{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.hist td.dl{white-space:nowrap}
/* The CAD file is the deliverable; make it obvious which link hands it over */
.hist a.dl-dxf{font-weight:600;color:var(--survey)}
.hist td.dl a{display:inline-block;font-family:ui-monospace,SFMono-Regular,
Menlo,monospace;font-size:10.5px;letter-spacing:.06em;padding:2px 7px;
margin:0 3px 3px 0;border:1px solid var(--rule);text-decoration:none;
color:var(--ink)}
.hist td.dl a:hover{border-color:var(--survey);color:var(--survey)}
.wide{overflow-x:auto}
.hist input[type=text]{width:100%;min-width:190px;padding:5px 8px;font-size:13px}
.ok{border-left:3px solid var(--teal);background:var(--faint);padding:11px 15px;
margin:0 0 20px;font-size:14.5px}
.filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 12px}
.filters input[type=text]{flex:1 1 260px;min-width:200px;padding:8px 11px}
.filters select{width:auto;min-width:150px;padding:8px 11px}
.filters button{margin-top:0;padding:8px 18px;font-size:14px}
.filters .clear{font-size:13.5px;text-decoration:none}
.pager{display:flex;gap:6px;flex-wrap:wrap;margin-top:16px;align-items:center}
.pg{display:inline-block;padding:6px 11px;border:1px solid var(--rule);
text-decoration:none;color:var(--ink);font-size:13.5px;
font-variant-numeric:tabular-nums}
.pg:hover{border-color:var(--survey);color:var(--survey)}
.pg.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.pg.off{color:var(--soft);border-color:transparent}
"""


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><div class="wrap"><div class="frame">{body}</div></div>
<script>
// Any form holding a #busy panel shows it on submit and runs a countdown
// from that form's estimate. The server streams nothing back until the
// whole job is done, so this is a timer, not progress — when the estimate
// runs out it says so and counts up rather than sitting at zero pretending.
(function(){{
  function pad(n){{return (n<10?'0':'')+n;}}
  function clock(s){{var m=Math.floor(Math.abs(s)/60);return (s<0?'+':'')+m+':'+pad(Math.abs(s)%60);}}
  document.querySelectorAll('form').forEach(function(form){{
    var busy = form.querySelector('#busy'); if(!busy) return;
    form.addEventListener('submit', function(){{
      var btn = form.querySelector('button[type=submit]') || form.querySelector('button');
      if(btn){{ btn.disabled = true; btn.textContent = btn.dataset.busy || 'Working…'; }}
      busy.classList.add('on');
      var panel = busy.querySelector('.load');
      var est = parseInt(panel && panel.dataset.estimate || '45', 10);
      var timeEl = busy.querySelector('.load-time');
      var noteEl = busy.querySelector('.load-over');
      var left = est, elapsed = 0;
      if(timeEl) timeEl.textContent = clock(left);
      setInterval(function(){{
        elapsed++; left--;
        if(timeEl) timeEl.textContent = clock(left);
        if(left < 0 && panel && !panel.classList.contains('over')){{
          panel.classList.add('over');
          if(noteEl) noteEl.style.display = 'block';
        }}
      }}, 1000);
    }});
  }});
  // Coming back via the back button must not leave a dead spinner up
  window.addEventListener('pageshow', function(){{
    document.querySelectorAll('#busy').forEach(function(b){{
      b.classList.remove('on');
      var p = b.querySelector('.load'); if(p) p.classList.remove('over');
    }});
    document.querySelectorAll('button[data-idle]').forEach(function(b){{
      b.disabled = false; b.textContent = b.dataset.idle;
    }});
  }});
}})();
</script>
</body></html>
""".encode()


def form_page(values: dict | None = None, error: str = "") -> bytes:
    v = values or {}
    g = v.get("gov", {})

    def val(key, default=""):
        return html.escape(str(v.get(key, default)))

    gov_inputs = "".join(
        f'<div><label for="{k}">{html.escape(lbl)}</label>'
        f'<input type="text" id="{k}" name="{k}" '
        f'value="{html.escape(str(g.get(k, "")))}"></div>'
        for k, lbl in GOV_FIELDS)

    err = f'<div class="err">{html.escape(error)}</div>' if error else ""
    sel = v.get("profile", "standard")
    sheet = v.get("sheet_size", "A3")

    def opts(names, chosen):
        return "".join(
            f'<option value="{n}"{" selected" if n == chosen else ""}>{n}'
            "</option>" for n in names)

    def opts_sel(names, chosen):
        return "".join(
            f'<option value="{n}"{" selected" if n == chosen else ""}>{n}'
            "</option>" for n in names)

    def opts_scale(values, chosen):
        return "".join(
            f'<option value="{n}"{" selected" if n == chosen else ""}>'
            f"1:{int(n):,}</option>" for n in values)

    basemap_opts = "".join(
        f'<option value="{k}"'
        f'{" selected" if v.get("basemap", "") == k else ""}>'
        f"{html.escape(label)}</option>"
        for k, label in BASEMAP_CHOICES.items())

    return page("maps2cad", f"""
<p class="eyebrow">GPS coordinate → CAD drawing + site map</p>
<h1>maps2cad</h1>
<p class="lede">Enter a WGS 84 coordinate and the ground area to cover. You get a
DXF in true UTM metres — double-line roads, contours, and every building labelled
at its centre — plus a print-ready site map sheet and the building inventory CSV
that resolves the B### codes.</p>
{err}
<form method="post" action="/generate">
  <div class="grid g2">
    <div><label for="coords">Coordinates (latitude, longitude)</label>
      <input type="text" id="coords" name="coords" placeholder="15.83384548, 104.39445555"
             value="{val('coords')}" autofocus></div>
    <div class="grid g2" style="gap:12px">
      <div><label for="width">Width (m)</label>
        <input type="number" id="width" name="width" step="any" min="20"
               value="{val('width', 1000)}"></div>
      <div><label for="height">Height (m)</label>
        <input type="number" id="height" name="height" step="any" min="20"
               value="{val('height', 750)}"></div>
    </div>
  </div>
  <div class="grid g3" style="margin-top:16px">
    <div><label for="export">Export</label>
      <select id="export" name="export" onchange="toggleGov()">
        <option value="both"{" selected" if v.get('export', 'both') == 'both' else ""}>CAD + site map</option>
        <option value="cad"{" selected" if v.get('export') == 'cad' else ""}>CAD only (DXF)</option>
        <option value="map"{" selected" if v.get('export') == 'map' else ""}>Site map only (PDF)</option>
      </select></div>
    <div><label for="profile">Layout</label>
      <select id="profile" name="profile" onchange="toggleGov()">
        <option value="standard"{" selected" if sel == "standard" else ""}>Standard</option>
        <option value="government"{" selected" if sel == "government" else ""}>Thai government submission</option>
      </select></div>
    <div><label for="sheet_size">Sheet</label>
      <select id="sheet_size" name="sheet_size">{opts(["A4","A3","A2","A1"], sheet)}</select></div>
    <div><label for="title">Map title</label>
      <input type="text" id="title" name="title" value="{val('title', 'Detailed Site Map')}"></div>
  </div>
  <div class="grid g3" style="margin-top:16px">
    <div><label for="cad_sheet">CAD sheet (paper space)</label>
      <select id="cad_sheet" name="cad_sheet">
        {opts_sel(["A3", "A2", "A1", "A0", "A4"], v.get('cad_sheet', 'A3'))}
        <option value="none"{" selected" if v.get('cad_sheet') == 'none' else ""}>No sheet — model space only</option>
      </select></div>
    <div><label for="cad_scale">Plot scale</label>
      <select id="cad_scale" name="cad_scale">
        <option value="fit"{" selected" if v.get('cad_scale', 'fit') == 'fit' else ""}>Fit the extent (recommended)</option>
        {opts_scale(["500", "1000", "1250", "2000", "2500", "5000"], str(v.get('cad_scale', '')))}
      </select></div>
    <div><label for="basemap">Background map (CAD)</label>
      <select id="basemap" name="basemap">{basemap_opts}</select></div>
  </div>
  <div style="display:flex;gap:22px;flex-wrap:wrap;margin-top:18px">
    <label class="check"><input type="checkbox" name="codes" checked> Show B### codes on unnamed buildings</label>
    <label class="check"><input type="checkbox" name="final"> Final (remove DRAFT watermark)</label>
  </div>
  <fieldset id="gov" style="display:{'block' if sel == 'government' else 'none'}">
    <legend>Title block</legend>
    <div class="grid g2">{gov_inputs}</div>
  </fieldset>
  <button type="submit" data-idle="Generate" data-busy="Generating…">
  Generate</button>
  <div id="busy"><div class="load" data-estimate="60">
    <div class="load-head"><span class="load-title">Generating…</span>
      <span class="load-time">1:00</span></div>
    <div class="load-bar"></div>
    <p class="load-note">Querying Overpass, reading the elevation tile,
    tracing contours and drawing. Measured runs take 18–105 s; the first
    export in a new 1°×1° square also downloads a ~40 MB elevation tile.</p>
    <p class="load-note load-over" style="display:none">Over the estimate —
    still working. A cold start or a fresh elevation tile adds a minute or
    so. Leave this tab open.</p>
  </div></div>
</form>
<h2>Staged projects <a href="/projects">Browse and edit names →</a></h2>
<p class="note">Correct building names on a staged project and re-issue the
drawing in under a second — no re-fetch from OpenStreetMap.</p>
<h2>Already have the data <a href="/import">Import a file →</a></h2>
<p class="note">An OpenStreetMap export (<code>.osm</code> from the Export
button on openstreetmap.org) is drawn on the same layers with no network
fetch at all — useful where Overpass is blocked, or when someone sent you the
extract. Your own survey files (GeoJSON, shapefile, KML) come in the same way,
for sites OpenStreetMap has nothing mapped at.</p>
<h2>Recent generations <a href="/history">See all →</a></h2>
<div class="wide">{history_html(8)}</div>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
<script>
function toggleGov(){{document.getElementById('gov').style.display =
  document.getElementById('profile').value === 'government' ? 'block' : 'none';}}
</script>""")


def result_page(rec: dict) -> bytes:
    p = rec["params"]
    jid = rec["id"]

    preview = ""
    if rec.get("png"):
        b64 = base64.b64encode(Path(rec["png"]).read_bytes()).decode()
        preview = (f'<figure><img src="data:image/png;base64,{b64}" '
                   'alt="Rendered site map"></figure>')

    def file_card(kind, tag, label):
        """Preview is the big target; the small icon downloads."""
        if not rec.get(kind):
            return ""
        return (f'<span class="card"><a href="/view/{jid}/{kind}" '
                f'target="_blank" rel="noopener"><b>{tag}</b>{label}</a>'
                f'<a class="dlicon" href="/file/{jid}/{kind}" download '
                f'title="Download {label}">⤓</a></span>')

    def plain_card(kind, tag, label):
        """A file with no browser viewer and no stand-in: click to download."""
        if not rec.get(kind):
            return ""
        return (f'<span class="card"><a href="/file/{jid}/{kind}" download>'
                f'<b>{tag}</b>⤓&nbsp; {label}</a></span>')

    def download_card(kind, tag, label, preview_title):
        """The other way round, for a file with no browser viewer: clicking
        the card downloads it, and the plot preview moves to the small
        icon. Clicking 'CAD' and getting a PDF back is the wrong default."""
        if not rec.get(kind):
            return ""
        preview = ""
        if rec.get("plot"):
            preview = (f'<a class="dlicon" href="/view/{jid}/{kind}" '
                       f'target="_blank" rel="noopener" '
                       f'title="{preview_title}">◱</a>')
        return (f'<span class="card primary">'
                f'<a href="/file/{jid}/{kind}" download>'
                f'<b>{tag}</b>⤓&nbsp; {label}</a>{preview}</span>')

    dxf_tag = (f'CAD · {Path(rec["dxf"]).stat().st_size / 1024:.0f} KB'
               if rec.get("dxf") else "CAD")
    links = [
        download_card("dxf", dxf_tag, "Download DXF",
                      "Preview the A3 plot instead"),
        # Kept so the plot PDF still has a download of its own, not only the
        # preview reachable from the DXF card
        file_card("plot", "A3 plot", "Plot preview"),
        file_card("pdf", "Vector", "Site map PDF"),
        file_card("png", "300 DPI", "Site map PNG"),
        file_card("csv", "Inventory", "Buildings CSV"),
        file_card("attrs", "Attributes", "OSM tags table"),
        # Download only — a browser has no GeoTIFF viewer. Keep it beside
        # the DXF: AutoCAD resolves the reference relative to the drawing,
        # so taking one without the other loses the map.
        plain_card("tif", "Backdrop", "Background map"),
    ]

    # Only offered when the server has Google credentials — an unconfigured
    # button that always errors is worse than no button.
    if gdrive.configured() and rec.get("dxf"):
        links.append(f'<span class="card"><a href="/drive/{jid}">'
                     f'<b>Google Drive</b>Save this run</a></span>')

    zone = utm_zone_label(p["lat"], p["lon"])
    heading = ("CAD export" if p["export"] == "cad"
               else p["title"] if p["profile"] == "standard"
               else "Project location sheet")
    return page("Export ready", f"""
<p class="eyebrow">Generated {html.escape(rec['when'])}</p>
<h1>{html.escape(heading)}</h1>
<dl class="meta">
  <div><dt>Latitude</dt><dd>{p['lat']:.8f}°</dd></div>
  <div><dt>Longitude</dt><dd>{p['lon']:.8f}°</dd></div>
  <div><dt>Coverage</dt><dd>{p['width']:.0f} × {p['height']:.0f} m</dd></div>
  <div><dt>Projection</dt><dd>UTM {zone}</dd></div>
</dl>
{preview}
<div class="files">{''.join(links)}</div>
<pre class="log">{html.escape(rec['log'])}</pre>
<a class="back" href="/">← Generate another</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
""")


def utm_zone_label(lat: float, lon: float) -> str:
    zone = min(max(int((lon + 180) // 6) + 1, 1), 60)
    epsg = (32600 if lat >= 0 else 32700) + zone
    return f"{zone}{'N' if lat >= 0 else 'S'} (EPSG:{epsg})"


# ----------------------------------------------------------------- projects
GIS_SUFFIXES = {".geojson", ".json", ".gpkg", ".kml", ".gml", ".zip"}
# OSM XML as www.openstreetmap.org's Export button hands it over, plus the
# shapes an extract is usually passed around in. `.pbf` is accepted here and
# refused by osm2cad.py, which answers with the one command that converts it
# — better than "not a GIS file" from a reader that never looked at it.
OSM_SUFFIXES = {".osm", ".xml", ".pbf", ".gz", ".bz2", ".zip"}
# A .zip could be either, so it is classified after expansion, by what it
# turned out to hold.
OSM_ONLY = OSM_SUFFIXES - {".zip"}
MAX_UPLOAD = 64 * 1024 * 1024        # 64 MB per request


def parse_multipart(content_type: str, body: bytes):
    """Minimal multipart/form-data reader.

    stdlib's `cgi` is deprecated and gone in 3.13, so the parts are split on
    the boundary directly. Returns (fields, files) where files is a list of
    (filename, bytes).
    """
    if "boundary=" not in content_type:
        raise BadRequest("Malformed upload: no multipart boundary.")
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"')
    sep = b"--" + boundary.encode()
    fields: dict[str, str] = {}
    files: list[tuple[str, bytes]] = []

    for chunk in body.split(sep):
        if chunk in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        head, _, data = chunk.partition(b"\r\n\r\n")
        if not _:
            continue
        data = data.rstrip(b"\r\n")
        headers = head.decode("utf-8", "replace")
        disp = next((h for h in headers.splitlines()
                     if h.lower().startswith("content-disposition")), "")
        name = filename = None
        for piece in disp.split(";"):
            piece = piece.strip()
            if piece.startswith("name="):
                name = piece[5:].strip('"')
            elif piece.startswith("filename="):
                filename = piece[9:].strip('"')
        if filename:
            if data:
                files.append((filename, data))
        elif name:
            fields[name] = data.decode("utf-8", "replace").strip()
    return fields, files


def save_uploads(files, dest: Path) -> list[Path]:
    """Write uploaded files, expanding a zipped shapefile set or OSM export."""
    import zipfile

    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, data in files:
        safe = Path(filename).name          # never trust a client path
        suffix = Path(safe).suffix.lower()
        if suffix not in GIS_SUFFIXES | OSM_SUFFIXES:
            raise BadRequest(
                f"“{safe}” is not a file this reads. Upload an OpenStreetMap "
                "export (.osm from the Export button on openstreetmap.org, "
                "or .gz/.bz2), or GIS data (GeoJSON, GeoPackage, KML, GML) "
                "— or a .zip holding either a shapefile set (.shp with its "
                ".dbf and .shx) or an .osm file.")
        target = dest / safe
        target.write_bytes(data)
        if suffix == ".zip":
            try:
                with zipfile.ZipFile(target) as z:
                    for member in z.namelist():
                        # Flatten; ignore anything trying to escape the folder
                        inner = Path(member).name
                        if inner and not member.endswith("/"):
                            (dest / inner).write_bytes(z.read(member))
            except zipfile.BadZipFile:
                raise BadRequest(f"“{safe}” is not a readable zip archive.")
            shp = sorted(dest.glob("*.shp"))
            osm = sorted(p for p in dest.iterdir()
                         if p.suffix.lower() in (".osm", ".xml"))
            if not shp and not osm:
                raise BadRequest(
                    f"“{safe}” holds no .shp file and no .osm file. Zip the "
                    "whole shapefile set — .shp, .dbf, .shx and ideally .prj "
                    "— or an OpenStreetMap export.")
            # A shapefile set wins: a zip carrying both is a survey with its
            # source extract alongside, and the survey is what was uploaded.
            written.extend(shp or osm)
        else:
            written.append(target)
    if not written:
        raise BadRequest("No file was uploaded.")
    return written


def import_kind(paths) -> str:
    """Which converter draws these files: 'osm' or 'gis'.

    Routed by extension, not by sniffing the contents: the two converters
    take different options and produce different drawings from the same
    ground, so a wrong guess is worse than a question. Mixing the two in one
    upload is refused for the same reason — there is no single command that
    would draw both.
    """
    kinds = {"osm" if Path(p).suffix.lower() in OSM_ONLY else "gis"
             for p in paths}
    if len(kinds) > 1:
        raise BadRequest(
            "Upload OpenStreetMap files and your own GIS files separately — "
            "they are drawn by different converters. Import one, then the "
            "other into the same project name, and they share a drawing.")
    return kinds.pop()


def parse_epsg(value: str):
    """Optional projected CRS code from the form."""
    value = (value or "").strip().upper().removeprefix("EPSG:")
    if not value:
        return None
    if not value.isdigit():
        raise BadRequest(f"“{value}” is not an EPSG code. Give a number, "
                         "e.g. 32647 for UTM zone 47N, or leave it blank to "
                         "derive the zone from the data.")
    return value


def stage_db_module():
    """Import the staging helpers lazily; serve.py itself stays stdlib-only."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("stage_db", STAGE_DB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def db_conn():
    if not STAGING_DB.is_file():
        return None
    # Go through stage_db.connect so the schema is applied: a database
    # created by an older version is missing newer tables otherwise.
    try:
        return stage_db_module().connect(STAGING_DB)
    except Exception as e:
        print(f"  staging schema check failed ({type(e).__name__}: {e})")
        conn = sqlite3.connect(str(STAGING_DB))
        conn.row_factory = sqlite3.Row
        return conn


def projects_page() -> bytes:
    conn = db_conn()
    if conn is None:
        return page("Projects", """
<p class="eyebrow">Staging database</p><h1>Projects</h1>
<p class="lede">Nothing staged yet. Generate a CAD export and the site will
appear here, where you can correct building names and re-issue the drawing
without re-fetching from OpenStreetMap.</p>
<a class="back" href="/">← Generate</a>""")
    rows = conn.execute(
        "SELECT p.*,"
        " (SELECT COUNT(*) FROM staging_buildings b WHERE b.project_id=p.id)"
        "   AS n_b,"
        " (SELECT COUNT(*) FROM staging_buildings b WHERE b.project_id=p.id"
        "    AND b.osm_name IS NOT NULL) AS n_named,"
        " (SELECT COUNT(*) FROM staging_roads r WHERE r.project_id=p.id)"
        "   AS n_r"
        " FROM projects p ORDER BY p.id DESC").fetchall()
    conn.close()
    body = ["<table class='hist'><thead><tr><th>Project</th><th>Centre</th>"
            "<th>Extent</th><th>CRS</th><th>Buildings</th><th>Roads</th>"
            "<th></th></tr></thead><tbody>"]
    for r in rows:
        body.append(
            f"<tr><td>{html.escape(r['name'])}</td>"
            f"<td class='num'>{r['lat']:.6f}, {r['lon']:.6f}</td>"
            f"<td class='num'>{r['width_m']:.0f} × {r['height_m']:.0f} m</td>"
            f"<td class='num'>EPSG:{r['srid']}</td>"
            f"<td class='num'>{r['n_b']} ({r['n_named']} named)</td>"
            f"<td class='num'>{r['n_r']}</td>"
            f"<td class='dl'><a href='/project/{r['id']}'>Open</a></td></tr>")
    body.append("</tbody></table>")
    return page("Projects", f"""
<p class="eyebrow">{len(rows)} staged project(s)</p>
<h1>Projects</h1>
<p class="lede">Every CAD export is staged here with its label anchors already
computed. Open a project to correct building names and re-issue the drawing —
that redraw takes under a second and never touches OpenStreetMap.</p>
<div class="wide">{''.join(body)}</div>
<p class="note"><a href="/import">Import a file →</a> — an OpenStreetMap
export or your own survey data, merged into a project by name so both share
one drawing.</p>
<a class="back" href="/">← Generate</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
""")


PER_PAGE = 100


def pager(pid, q, only, page_no, pages) -> str:
    if pages <= 1:
        return ""
    def link(n, label=None, disabled=False):
        if disabled:
            return f'<span class="pg off">{label or n}</span>'
        qs = urllib.parse.urlencode({"q": q, "only": only, "page": n})
        cls = "pg on" if n == page_no else "pg"
        return f'<a class="{cls}" href="/project/{pid}?{qs}">{label or n}</a>'

    window = [n for n in range(max(1, page_no - 2), min(pages, page_no + 2) + 1)]
    out = [link(page_no - 1, "‹ Prev", page_no == 1)]
    if window[0] > 1:
        out += [link(1), '<span class="pg off">…</span>']
    out += [link(n) for n in window]
    if window[-1] < pages:
        out += ['<span class="pg off">…</span>', link(pages)]
    out.append(link(page_no + 1, "Next ›", page_no == pages))
    return f'<div class="pager">{"".join(out)}</div>'


def project_page(pid: int, note: str = "", q: str = "", page_no: int = 1,
                 only: str = "all") -> bytes:
    conn = db_conn()
    if conn is None:
        return page("Not found", "<h1>No staging database</h1>"
                    '<a class="back" href="/">← Back</a>')
    proj = conn.execute("SELECT * FROM projects WHERE id = ?",
                        (pid,)).fetchone()
    if proj is None:
        conn.close()
        return page("Not found", "<h1>No such project</h1>"
                    '<a class="back" href="/projects">← Projects</a>')
    # A dense site can hold thousands of footprints, so the table is
    # searched, filtered and paged rather than rendered whole.
    # Landmark grounds share this table but are not buildings; they would
    # otherwise appear in the name editor as unnamed footprints to fill in.
    where = ["project_id = ?", "cad_layer = 'C-BLDG-OUTL'"]
    params: list = [pid]
    if only == "unnamed":
        where.append("code <> '' AND display_name = code")
    elif only == "named":
        where.append("(code = '' OR display_name <> code)")
    if q:
        where.append("(feature_id LIKE ? OR IFNULL(code,'') LIKE ?"
                     " OR IFNULL(display_name,'') LIKE ?"
                     " OR IFNULL(osm_name,'') LIKE ?)")
        params += [f"%{q}%"] * 4
    clause = " AND ".join(where)

    total_all = conn.execute("SELECT COUNT(*) FROM staging_buildings WHERE"
                             " project_id = ? AND cad_layer = 'C-BLDG-OUTL'",
                             (pid,)).fetchone()[0]
    unnamed_all = conn.execute(
        "SELECT COUNT(*) FROM staging_buildings WHERE project_id = ?"
        " AND cad_layer = 'C-BLDG-OUTL'"
        " AND code <> '' AND display_name = code", (pid,)).fetchone()[0]
    matched = conn.execute(f"SELECT COUNT(*) FROM staging_buildings WHERE"
                           f" {clause}", params).fetchone()[0]
    pages = max(1, (matched + PER_PAGE - 1) // PER_PAGE)
    page_no = min(max(1, page_no), pages)
    buildings = conn.execute(
        f"SELECT feature_id, code, osm_name, display_name, source, area_m2,"
        f" latitude, longitude FROM staging_buildings WHERE {clause}"
        f" ORDER BY (osm_name IS NULL), display_name LIMIT ? OFFSET ?",
        params + [PER_PAGE, (page_no - 1) * PER_PAGE]).fetchall()
    # Grouped by name the way the drawing labels it: a divided carriageway
    # is several ways sharing one name.
    roads = conn.execute(
        "SELECT road_name, road_ref, highway_type,"
        " MAX(carriageway_m) AS carriageway_m, SUM(length_m) AS length_m,"
        " COUNT(*) AS segments FROM staging_roads WHERE project_id = ?"
        " AND (road_name IS NOT NULL OR road_ref IS NOT NULL)"
        " GROUP BY road_name, road_ref, highway_type"
        " ORDER BY length_m DESC", (pid,)).fetchall()
    conn.close()

    rows = []
    for b in buildings:
        placeholder = b["code"] or ""
        current = b["display_name"] or ""
        is_code = current == placeholder
        rows.append(
            f"<tr><td class='num'>{html.escape(b['feature_id'])}</td>"
            f"<td class='num'>{html.escape(placeholder)}</td>"
            f"<td class='num'>{(b['area_m2'] or 0):,.0f} m²</td>"
            f"<td>{html.escape(b['source'] or '')}</td>"
            f"<td><input type='text' name='name::{html.escape(b['feature_id'])}'"
            f" value='{'' if is_code else html.escape(current)}'"
            f" placeholder='{html.escape(placeholder)}'></td></tr>")

    road_rows = "".join(
        f"<tr><td>{html.escape(r['road_name'] or '—')}</td>"
        f"<td class='num'>{html.escape(r['road_ref'] or '—')}</td>"
        f"<td>{html.escape(r['highway_type'] or '')}</td>"
        f"<td class='num'>{r['carriageway_m']:.1f} m</td>"
        f"<td class='num'>{r['length_m']:,.0f} m</td>"
        f"<td class='num'>{r['segments']}</td></tr>" for r in roads)

    banner = f'<div class="ok">{html.escape(note)}</div>' if note else ""
    return page(f"{proj['name']}", f"""
<p class="eyebrow">Project {pid} · EPSG:{proj['srid']}</p>
<h1>{html.escape(proj['name'])}</h1>
{banner}
<dl class="meta">
  <div><dt>Centre</dt><dd>{proj['lat']:.6f}, {proj['lon']:.6f}</dd></div>
  <div><dt>Extent</dt><dd>{proj['width_m']:.0f} × {proj['height_m']:.0f} m</dd></div>
  <div><dt>Buildings</dt><dd>{total_all}</dd></div>
  <div><dt>Named</dt><dd>{total_all - unnamed_all} verified</dd></div>
</dl>

<h2>Roads</h2>
<div class="wide"><table class="hist"><thead><tr><th>Name</th><th>Route no.</th>
<th>Class</th><th>Carriageway</th><th>Total length</th><th>Ways</th></tr></thead>
<tbody>{road_rows or '<tr><td>No named roads</td></tr>'}</tbody></table></div>

<h2>Buildings <span style="font-weight:400;font-size:13px;color:var(--soft)">
{total_all} total · {unnamed_all} still need a verified name</span></h2>

<form method="get" action="/project/{pid}" class="filters">
  <input type="text" name="q" value="{html.escape(q)}"
         placeholder="Search code, name or feature id…">
  <select name="only">
    <option value="all"{" selected" if only == "all" else ""}>All buildings</option>
    <option value="unnamed"{" selected" if only == "unnamed" else ""}>Needs a name</option>
    <option value="named"{" selected" if only == "named" else ""}>Already named</option>
  </select>
  <button type="submit">Filter</button>
  {'<a class="clear" href="/project/' + str(pid) + '">Clear</a>'
   if (q or only != "all") else ''}
</form>
<p class="note">Showing {len(buildings)} of {matched} matching
({(page_no - 1) * PER_PAGE + 1 if buildings else 0}–{(page_no - 1) * PER_PAGE + len(buildings)}).
Type a verified name to replace its code — blank keeps the code. Saving
applies to this page only.</p>

<form method="post" action="/project/{pid}/save?{urllib.parse.urlencode({'q': q, 'only': only, 'page': page_no})}">
  <div class="wide">
  <table class="hist"><thead><tr><th>Feature</th><th>Code</th><th>Area</th>
  <th>Source</th><th>Verified name</th></tr></thead>
  <tbody>{''.join(rows) or '<tr><td colspan="5">No buildings match.</td></tr>'}</tbody></table></div>
  <button type="submit">Save names on this page</button>
</form>
{pager(pid, q, only, page_no, pages)}

<h2>Re-issue drawing</h2>
<form method="post" action="/project/{pid}/redraw">
  <div class="grid g3">
    <div><label for="cad_sheet">Sheet</label>
      <select id="cad_sheet" name="cad_sheet">
        <option value="A3">A3</option><option value="A2">A2</option>
        <option value="A1">A1</option><option value="A0">A0</option>
        <option value="A4">A4</option>
        <option value="none">No sheet — model space only</option>
      </select></div>
    <div><label for="cad_scale">Plot scale</label>
      <select id="cad_scale" name="cad_scale">
        <option value="fit">Fit the extent (recommended)</option>
        <option value="500">1:500</option><option value="1000">1:1,000</option>
        <option value="1250">1:1,250</option>
        <option value="2000">1:2,000</option>
        <option value="2500">1:2,500</option>
        <option value="5000">1:5,000</option>
      </select></div>
    <div style="display:flex;align-items:flex-end">
      <button type="submit" style="margin-top:0" data-idle="Re-issue drawing"
        data-busy="Re-issuing…">Re-issue drawing</button>
    </div>
  </div>
  <div id="busy"><div class="load" data-estimate="15">
    <div class="load-head"><span class="load-title">Re-issuing…</span>
      <span class="load-time">0:15</span></div>
    <div class="load-bar"></div>
    <p class="load-note">Redrawing from the staging database — plain SELECTs,
    no Overpass and no elevation tile, so this is the fast path.</p>
    <p class="load-note load-over" style="display:none">Over the estimate —
    still working.</p>
  </div></div>
</form>
<a class="back" href="/projects">← Projects</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
""")


# Feature types osm2cad.py can import, in the order the form offers them.
# Must match TYPE_CHOICES there.
OSM_TYPES = ("building", "road", "path", "water", "green", "rail",
             "barrier", "landmark")
OSM_TYPE_LABELS = {
    "building": "อาคาร / Buildings", "road": "ถนน / Roads",
    "path": "ทางเดิน / Paths", "water": "แหล่งน้ำ / Water",
    "green": "พื้นที่สีเขียว / Parks", "rail": "ทางรถไฟ / Railways",
    "barrier": "รั้ว / Barriers", "landmark": "สถานที่สำคัญ / Landmarks",
}


def import_page(note: str = "", error: str = "") -> bytes:
    conn = db_conn()
    projects = conn.execute("SELECT id, name FROM projects ORDER BY name"
                            ).fetchall() if conn else []
    if conn:
        conn.close()
    options = "".join(
        f'<option value="{html.escape(p["name"])}">{html.escape(p["name"])}'
        f" (project {p['id']})</option>" for p in projects)
    banner = (f'<div class="err">{html.escape(error)}</div>' if error else
              f'<div class="ok">{html.escape(note)}</div>' if note else "")
    types = "".join(
        f'<label class="check"><input type="checkbox" name="t_{t}" '
        f'value="1" checked> {html.escape(OSM_TYPE_LABELS[t])}</label>'
        for t in OSM_TYPES)
    basemap_opts = "".join(f'<option value="{k}">{html.escape(label)}</option>'
                           for k, label in BASEMAP_CHOICES.items())
    return page("Import a file", f"""
<p class="eyebrow">An OSM export or your own survey → CAD</p>
<h1>Import a file</h1>
<p class="lede">Two kinds of file are drawn here, and the right converter is
picked from the extension. An <b>OpenStreetMap export</b> (.osm) becomes the
same NCS-layered drawing the generator makes, with no network fetch at all —
useful where Overpass is blocked or the area was exported for you. Your own
<b>GIS data</b> — plots, access roads, equipment pads — is drawn in true UTM
metres on the same layers. Either is merged into a project, so a survey and
an extract can share one drawing.</p>
{banner}
<form method="post" action="/import" enctype="multipart/form-data"
     >
  <div class="grid g2">
    <div><label for="files">File(s)</label>
      <input type="file" id="files" name="files" multiple
             accept=".osm,.xml,.gz,.bz2,.geojson,.json,.gpkg,.kml,.gml,.zip">
      </div>
    <div><label for="project">Merge into project</label>
      <input type="text" id="project" name="project" list="projects"
             placeholder="new or existing project name">
      <datalist id="projects">{options}</datalist></div>
  </div>
  <div class="grid g3" style="margin-top:16px">
    <div><label for="epsg">Coordinate system (optional)</label>
      <input type="text" id="epsg" name="epsg"
             placeholder="auto: UTM zone from the data"></div>
    <div><label for="name_field">Name attribute (GIS only)</label>
      <input type="text" id="name_field" name="name_field"
             placeholder="auto: name, PLOT_NAME, label…"></div>
    <div><label for="layer">CAD layer (GIS only)</label>
      <input type="text" id="layer" name="layer"
             placeholder="e.g. C-PROP-LINE"></div>
    <div><label for="width">Line width, m (GIS only)</label>
      <input type="number" id="width" name="width" step="any" min="0"
             value="6"></div>
  </div>
  <fieldset style="margin-top:16px">
    <legend>OpenStreetMap files</legend>
    <div class="checks">{types}</div>
    <div class="grid g3" style="margin-top:16px">
      <div><label for="bbox">Crop to box (optional)</label>
        <input type="text" id="bbox" name="bbox"
               placeholder="S,W,N,E in degrees"></div>
      <div><label for="layer_by">Split layers by tag</label>
        <input type="text" id="layer_by" name="layer_by"
               placeholder="e.g. highway, building"></div>
      <div><label for="basemap">Background map</label>
        <select id="basemap" name="basemap">{basemap_opts}</select></div>
    </div>
    <label class="check" style="margin-top:16px"><input type="checkbox"
      name="attributes" value="1" checked> Attach OSM tags as XDATA</label>
    <p class="note" style="margin-top:10px">Feature types to import, and
    whether each entity carries its source OSM tags — select a building in
    AutoCAD and LIST shows them. Ignored for GIS files.</p>
  </fieldset>
  <button type="submit" data-idle="Import and draw"
    data-busy="Importing…">Import and draw</button>
  <div id="busy"><div class="load" data-estimate="20">
    <div class="load-head"><span class="load-title">Importing…</span>
      <span class="load-time">0:20</span></div>
    <div class="load-bar"></div>
    <p class="load-note">Reading the file, reprojecting to UTM and drawing.
    No network fetch, so this is usually quick.</p>
    <p class="load-note load-over" style="display:none">Over the estimate —
    still working. A large export takes longer.</p>
  </div></div>
</form>
<p class="note">OpenStreetMap: .osm or .xml from the Export button on
openstreetmap.org, optionally .gz or .bz2. GIS: GeoJSON, GeoPackage, KML,
GML, or a .zip holding a shapefile set — files without a declared CRS are
assumed to be latitude/longitude. Upload the two kinds separately; a .osm.pbf
has to be converted first (<code>osmium cat -o map.osm map.osm.pbf</code>).</p>
<a class="back" href="/">← Generate from OpenStreetMap</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
<script>
</script>""")


# ------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "SiteMapServer/1.0"

    def log_message(self, fmt, *args):  # concise console output
        print(f"  {self.address_string()} {fmt % args}")

    def _send(self, body: bytes, status=200, ctype="text/html; charset=utf-8",
              extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------ Google session
    def session_id(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            return http.cookies.SimpleCookie(raw).get("m2c_sid").value
        except (AttributeError, http.cookies.CookieError):
            return None

    def google_session(self):
        """The signed-in Google session for this browser, or None."""
        if SESSIONS is None:
            return None
        return SESSIONS.get(self.session_id())

    def _redirect(self, to, extra=None):
        headers = {"Location": to}
        headers.update(extra or {})
        self._send(b"", 303, "text/plain", headers)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send(form_page())
        elif path == "/history":
            self._send(page("Generation history", f"""
<p class="eyebrow">{len(history())} run(s) on this machine</p>
<h1>Generation history</h1>
<p class="lede">Every run is kept in its own folder under
<code>output/web/</code>, so nothing overwrites anything else.</p>
<div class="wide">{history_html()}</div>
<a class="back" href="/">← Generate another</a>
<footer>Data © OpenStreetMap contributors (ODbL) · elevation © Copernicus</footer>
"""))
        elif path == "/import":
            self._send(import_page())
        elif path == "/projects":
            self._send(projects_page())
        elif path.startswith("/project/"):
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[1].isdigit():
                qs = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                q = (qs.get("q") or [""])[0].strip()
                only = (qs.get("only") or ["all"])[0]
                only = only if only in ("all", "unnamed", "named") else "all"
                try:
                    page_no = max(1, int((qs.get("page") or ["1"])[0]))
                except ValueError:
                    page_no = 1
                body = project_page(int(parts[1]), q=q, page_no=page_no,
                                    only=only)
                self._send(body, 200 if b"No such project" not in body
                           else 404)
            else:
                self._send(b"Not found", 404, "text/plain")
        elif path == "/auth/google":
            self.auth_start()
        elif path == "/auth/callback":
            self.auth_callback()
        elif path == "/auth/logout":
            sid = self.session_id()
            if SESSIONS is not None and sid:
                SESSIONS.drop(sid)
            self._redirect("/", {"Set-Cookie":
                                 "m2c_sid=; Path=/; Max-Age=0; HttpOnly"})
        elif path == "/health":
            self._send(b'{"ok":true}', ctype="application/json")
        elif path.startswith("/file/"):
            self.serve_file(path)
        elif path.startswith("/drive/"):
            self.drive_upload(path.strip("/").split("/")[-1])
        elif path.startswith("/view/"):
            self.serve_preview(path)
        else:
            self._send(page("Not found", "<h1>Not found</h1>"
                            '<a class="back" href="/">← Back</a>'), 404)

    # ------------------------------------------------------------ Google
    def auth_start(self):
        """Send the browser to Google, remembering where to come back to."""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        back = (q.get("next") or ["/"])[0]
        if not back.startswith("/"):
            back = "/"          # never bounce to another origin
        try:
            url, state, verifier = gdrive.start_login()
        except gdrive.DriveError as e:
            return self._send(page("Google sign-in", f"<h1>Not configured</h1>"
                                   f"<p class='lede'>{html.escape(str(e))}</p>"
                                   '<a class="back" href="/">← Back</a>'), 503)
        with PENDING_LOCK:
            PENDING[state] = (verifier, back)
        self._redirect(url)

    def auth_callback(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        err = (q.get("error") or [None])[0]
        state = (q.get("state") or [None])[0]
        code = (q.get("code") or [None])[0]
        with PENDING_LOCK:
            pending = PENDING.pop(state, None) if state else None
        if err or not code or pending is None:
            # A missing/unknown state means this callback did not start
            # here — the whole point of the parameter.
            why = err or ("that sign-in did not start from this page, or it "
                          "expired — try again")
            return self._send(page("Google sign-in", "<h1>Sign-in failed</h1>"
                                   f"<p class='lede'>{html.escape(why)}</p>"
                                   '<a class="back" href="/">← Back</a>'), 400)
        verifier, back = pending
        try:
            sess = gdrive.exchange_code(code, verifier)
            sess["email"] = gdrive.userinfo(sess["access_token"]).get("email", "")
        except gdrive.DriveError as e:
            return self._send(page("Google sign-in", "<h1>Sign-in failed</h1>"
                                   f"<p class='lede'>{html.escape(str(e))}</p>"
                                   '<a class="back" href="/">← Back</a>'), 502)
        sid = secrets.token_urlsafe(32)
        SESSIONS.put(sid, sess)
        # Secure only over https, or the cookie is dropped on a local http
        # run and sign-in silently never sticks.
        secure = "; Secure" if self.headers.get(
            "X-Forwarded-Proto", "").lower() == "https" else ""
        self._redirect(back, {"Set-Cookie":
                              f"m2c_sid={sid}; Path=/; HttpOnly; "
                              f"SameSite=Lax; Max-Age=2592000{secure}"})

    def drive_upload(self, jid):
        """Copy one run's outputs into the signed-in user's Drive."""
        sess = self.google_session()
        if sess is None:
            return self._redirect(f"/auth/google?next=/drive/{jid}")
        with JOBS_LOCK:
            rec = JOBS.get(jid)
        if not rec:
            return self._send(b"Unknown map id", 404, "text/plain")
        try:
            token, sess = gdrive.valid_token(sess)
            SESSIONS.put(self.session_id(), sess)
            p = rec["params"]
            project = (rec.get("project")
                       or f"{p['lat']:.6f}_{p['lon']:.6f}_"
                          f"{p['width']:.0f}x{p['height']:.0f}")
            wanted = [("dxf", "image/vnd.dxf"), ("plot", "application/pdf"),
                      ("pdf", "application/pdf"), ("png", "image/png"),
                      ("csv", "text/csv"), ("tif", "image/tiff")]
            files = [(Path(rec[k]), mime) for k, mime in wanted if rec.get(k)]
            result = gdrive.upload_project(token, project, files)
        except gdrive.DriveError as e:
            return self._send(page("Google Drive", "<h1>Upload failed</h1>"
                                   f"<p class='lede'>{html.escape(str(e))}</p>"
                                   f'<a class="back" href="/">← Back</a>'), 502)
        skipped = "".join(
            f"<li>{html.escape(n)} — {html.escape(why)}</li>"
            for n, why in result["skipped"])
        self._send(page("Saved to Google Drive", f"""
<p class="eyebrow">{html.escape(sess.get('email', ''))}</p>
<h1>Saved to Google Drive</h1>
<p class="lede">{len(result['uploaded'])} file(s) in
<code>{html.escape(gdrive.ROOT_FOLDER)}/{html.escape(project)}</code>.</p>
<div class="files"><span class="card primary">
<a href="{html.escape(result['link'])}" target="_blank" rel="noopener">
<b>Google Drive</b>Open the folder</a></span></div>
{f'<p class="note">Not uploaded:</p><ul>{skipped}</ul>' if skipped else ''}
<a class="back" href="/">← Generate another</a>"""))

    def serve_file(self, path):
        parts = path.strip("/").split("/")
        kinds = KINDS
        if len(parts) != 3 or parts[2] not in kinds:
            return self._send(b"Bad request", 400, "text/plain")
        with JOBS_LOCK:
            rec = JOBS.get(parts[1])
        if not rec or parts[2] not in rec:
            return self._send(b"Unknown map id", 404, "text/plain")
        target = Path(rec[parts[2]])
        if not target.is_file():
            return self._send(b"File missing", 404, "text/plain")
        ctype = (mimetypes.guess_type(target.name)[0]
                 or ("image/vnd.dxf" if parts[2] == "dxf"
                     else "application/octet-stream"))
        stem, ext = kinds[parts[2]].rsplit(".", 1)
        if parts[2] == "tif":
            # Keeps its bare name on purpose: the DXF references the raster
            # as "basemap.tif" relative to itself, so a per-coordinate name
            # would hand the user a drawing with a missing background map.
            name = kinds[parts[2]]
        else:
            name = (f"{stem}_{rec['params']['lat']:.6f}_"
                    f"{rec['params']['lon']:.6f}.{ext}")
        self._send(target.read_bytes(), ctype=ctype,
                   extra={"Content-Disposition":
                          f'attachment; filename="{name}"'})

    def serve_preview(self, path):
        """Open a file in the browser instead of downloading it. DXF has no
        native viewer, so its A3 plot PDF stands in; CSV renders as a table."""
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[2] not in KINDS:
            return self._send(b"Bad request", 400, "text/plain")
        with JOBS_LOCK:
            rec = JOBS.get(parts[1])
        if not rec:
            return self._send(b"Unknown map id", 404, "text/plain")
        kind = parts[2]
        if kind == "dxf":
            kind = "plot" if rec.get("plot") else "dxf"
        if kind not in rec:
            return self._send(page("Not available",
                                   "<h1>No preview</h1><p>That file was not "
                                   'generated for this run.</p>'
                                   '<a class="back" href="/">← Back</a>'), 404)
        target = Path(rec[kind])
        if not target.is_file():
            return self._send(b"File missing", 404, "text/plain")

        # Both CSVs render as a grid; only the words around them differ.
        if kind in ("csv", "attrs"):
            rows = list(csv.reader(
                target.read_text(encoding="utf-8").splitlines()))
            head = rows[0] if rows else []
            body = rows[1:]
            table = ["<table class='hist'><thead><tr>",
                     "".join(f"<th>{html.escape(c)}</th>" for c in head),
                     "</tr></thead><tbody>"]
            for r in body[:500]:
                table.append("<tr>" + "".join(
                    f"<td>{html.escape(c)}</td>" for c in r) + "</tr>")
            table.append("</tbody></table>")
            more = (f"<p class='note'>Showing the first 500 of {len(body)} "
                    "rows.</p>") if len(body) > 500 else ""
            if kind == "attrs":
                features = len({r[0] for r in body if r})
                return self._send(page("Attributes", f"""
<p class="eyebrow">{len(body)} tag(s) on {features} feature(s)</p>
<h1>Attributes</h1>
<p class="lede">Every OpenStreetMap tag carried by every feature in the
drawing. The entities hold the same tags as extended data — select one in
AutoCAD and <code>LIST</code> shows them — so this is the copy you can read
without opening the CAD file, and the complete one: the drawing stores the
first 40 tags per entity, this stores all of them.</p>
<div class="wide">{''.join(table)}</div>{more}
<div class="files"><a href="/file/{parts[1]}/attrs" download>
<b>Spreadsheet</b>Download CSV</a></div>
<a class="back" href="/">← Back</a>"""))
            return self._send(page("Building inventory", f"""
<p class="eyebrow">{len(body)} building(s)</p>
<h1>Building inventory</h1>
<p class="lede">Fill in <code>display_name</code> for the codes you verify in
the field, then regenerate with that file to put the real names on the
drawing.</p>
<div class="wide">{''.join(table)}</div>{more}
<div class="files"><a href="/file/{parts[1]}/csv" download>
<b>Spreadsheet</b>Download CSV</a></div>
<a class="back" href="/">← Back</a>"""))

        ctype = mimetypes.guess_type(target.name)[0] or "application/pdf"
        self._send(target.read_bytes(), ctype=ctype,
                   extra={"Content-Disposition": "inline"})

    def read_form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def save_names(self, pid: int):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        keep = {"q": (qs.get("q") or [""])[0],
                "only": (qs.get("only") or ["all"])[0],
                "page_no": int((qs.get("page") or ["1"])[0] or 1)}
        form = self.read_form()
        conn = db_conn()
        if conn is None:
            return self._send(b"No staging database", 404, "text/plain")
        applied = cleared = 0
        for key, values in form.items():
            if not key.startswith("name::"):
                continue
            fid = key[6:]
            name = (values[0] or "").strip()
            row = conn.execute(
                "SELECT code FROM staging_buildings WHERE project_id = ?"
                " AND feature_id = ?", (pid, fid)).fetchone()
            if row is None:
                continue
            if name:
                # File it by script as well, or the drawing would put a
                # typed Thai name on the language-neutral layer
                th, en = stage_db_module().split_by_script(name)
                cur = conn.execute(
                    "UPDATE staging_buildings SET display_name = ?,"
                    " name_th = ?, name_en = ? WHERE"
                    " project_id = ? AND feature_id = ? AND display_name <> ?",
                    (name, th, en, pid, fid, name))
                # Remember it so a later re-extraction does not lose the
                # field work that produced this name
                stage_db_module().record_verified(conn, pid, fid, name)
                applied += cur.rowcount
            elif row["code"]:
                # Blank means "no verified name yet" — fall back to the code
                cur = conn.execute(
                    "UPDATE staging_buildings SET display_name = ?,"
                    " name_th = NULL, name_en = NULL WHERE"
                    " project_id = ? AND feature_id = ? AND display_name <> ?",
                    (row["code"], pid, fid, row["code"]))
                if cur.rowcount:
                    stage_db_module().forget_verified(conn, pid, fid)
                cleared += cur.rowcount
        conn.commit()
        conn.close()
        note = (f"Saved {applied} name(s)"
                + (f", reverted {cleared} to their code" if cleared else "")
                + ". Re-issue the drawing to put them on the sheet.")
        self._send(project_page(pid, note, **keep))

    def redraw(self, pid: int):
        form = self.read_form()
        sheet = (form.get("cad_sheet") or ["A3"])[0].strip()
        scale = (form.get("cad_scale") or ["fit"])[0].strip()
        if sheet not in ("A4", "A3", "A2", "A1", "A0", "none"):
            sheet = "A3"
        if scale != "fit" and not scale.isdigit():
            scale = "fit"
        run = OUT / f"project{pid}-{datetime.now():%Y%m%d-%H%M%S}"
        run.mkdir(parents=True, exist_ok=True)
        dxf = str(run / "site.dxf")
        try:
            cmd = script_cmd(DB2DXF) + ["--db", str(STAGING_DB),
                                        "--project", str(pid), "--out", dxf]
            if sheet != "none":
                cmd += ["--sheet", sheet, "--scale", scale]
            log = run_step(cmd, "Re-issue")
            plot = str(run / "site_preview.pdf")
            try:
                pdf_cmd = script_cmd(DXF2PDF) + [dxf, "-o", plot]
                pdf_cmd += (["--layout", "SHEET", "--size", sheet]
                            if sheet != "none" else ["--size", "A3"])
                log += "\n" + run_step(pdf_cmd, "Plot preview")
            except BadRequest:
                plot = None
        except BadRequest as e:
            return self._send(project_page(pid, f"Re-issue failed: {e}"), 500)

        jid = hashlib.sha256(dxf.encode()).hexdigest()[:16]
        rec = {"id": jid, "dir": str(run), "dxf": dxf, "log": log,
               "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "params": {"lat": 0.0, "lon": 0.0, "width": 0.0, "height": 0.0,
                          "export": "cad", "profile": "standard",
                          "sheet_size": "A3", "title": f"Project {pid}"}}
        if plot:
            rec["plot"] = plot
        # db2dxf re-attaches the staged tags and rewrites the table, so a
        # re-issue offers the same attribute grid the first run did
        if (run / KINDS["attrs"]).is_file():
            rec["attrs"] = str(run / KINDS["attrs"])
        conn = db_conn()
        if conn is not None:
            p = conn.execute("SELECT * FROM projects WHERE id = ?",
                             (pid,)).fetchone()
            conn.close()
            if p:
                rec["params"].update(lat=p["lat"], lon=p["lon"],
                                     width=p["width_m"], height=p["height_m"],
                                     title=p["name"])
        save_job(rec)
        with JOBS_LOCK:
            JOBS[jid] = rec
        self._send(result_page(rec))

    def do_import(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            return self._send(import_page(
                error=f"Upload is larger than {MAX_UPLOAD // (1024*1024)} MB."),
                413)
        body = self.rfile.read(length)
        try:
            fields, files = parse_multipart(
                self.headers.get("Content-Type", ""), body)
            if not files:
                raise BadRequest("Choose at least one file to upload.")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            jid = hashlib.sha256(
                (stamp + files[0][0]).encode()).hexdigest()[:16]
            run = OUT / f"import-{stamp}"
            paths = save_uploads(files, run / "source")
            kind = import_kind(paths)

            project = fields.get("project", "").strip() \
                or Path(paths[0]).stem
            dxf = str(run / "site.dxf")
            epsg = parse_epsg(fields.get("epsg", ""))
            if kind == "osm":
                cmd = script_cmd(OSM2CAD)
                for p in paths:
                    cmd += ["--input", str(p)]
                cmd += ["--out", dxf, "--db", str(STAGING_DB),
                        "--project", project]
                types = [t for t in OSM_TYPES if fields.get(f"t_{t}")]
                # All boxes ticked is the same drawing as no filter at all,
                # so the flag is only passed when it actually drops something
                if types and len(types) < len(OSM_TYPES):
                    cmd += ["--types", ",".join(types)]
                if fields.get("bbox"):
                    cmd += ["--bbox", fields["bbox"]]
                if fields.get("layer_by"):
                    cmd += ["--layer-by", fields["layer_by"]]
                # Only the named providers, never a URL typed into the form
                if fields.get("basemap") in BASEMAP_CHOICES \
                        and fields.get("basemap"):
                    cmd += ["--basemap", fields["basemap"]]
                # An unticked checkbox is simply absent from the multipart
                # body, so the box carries the positive sense and its
                # absence is what turns the tags off.
                if not fields.get("attributes"):
                    cmd += ["--no-attributes"]
                if epsg:
                    cmd += ["--epsg", epsg]
                log = run_step(cmd, "OpenStreetMap import")
            else:
                cmd = script_cmd(GIS2CAD)
                for p in paths:
                    cmd += ["--input", str(p)]
                cmd += ["--out", dxf, "--db", str(STAGING_DB),
                        "--project", project]
                if fields.get("name_field"):
                    cmd += ["--name-field", fields["name_field"]]
                if fields.get("layer"):
                    cmd += ["--layer", fields["layer"]]
                if fields.get("width"):
                    cmd += ["--width", fields["width"]]
                if epsg:
                    cmd += ["--epsg", epsg]
                log = run_step(cmd, "GIS import")

            plot = str(run / "site_preview.pdf")
            try:
                log += "\n" + run_step(
                    script_cmd(DXF2PDF) + [dxf, "--size", "A3", "-o", plot],
                    "Plot preview")
            except BadRequest:
                plot = None
        except BadRequest as e:
            # Report the file the way they uploaded it, not our temp path
            msg = str(e).replace(str(run / "source") + "/", "") \
                if "run" in dir() else str(e)
            return self._send(import_page(error=msg), 400)

        rec = {"id": jid, "dir": str(run), "dxf": dxf, "log": log,
               "when": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "project": project,
               **{k: str(run / KINDS[k]) for k in ("tif", "attrs")
                  if (run / KINDS[k]).is_file()},
               "params": {"lat": 0.0, "lon": 0.0, "width": 0.0, "height": 0.0,
                          "export": kind, "profile": "standard",
                          "sheet_size": "A3", "title": project}}
        if plot:
            rec["plot"] = plot
        conn = db_conn()
        if conn is not None:
            p = conn.execute("SELECT * FROM projects WHERE name = ?",
                             (project,)).fetchone()
            conn.close()
            if p:
                rec["params"].update(lat=p["lat"], lon=p["lon"],
                                     width=p["width_m"], height=p["height_m"])
        save_job(rec)
        with JOBS_LOCK:
            JOBS[jid] = rec
        self._send(result_page(rec))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/import":
            return self.do_import()
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "project" and parts[1].isdigit():
            if parts[2] == "save":
                return self.save_names(int(parts[1]))
            if parts[2] == "redraw":
                return self.redraw(int(parts[1]))
        if path != "/generate":
            return self._send(b"Not found", 404, "text/plain")
        form = self.read_form()
        try:
            params = parse_form(form)
        except BadRequest as e:
            # Re-show the form with what they typed still in place
            flat = {k: v[0] for k, v in form.items()}
            flat["gov"] = {k: flat.get(k, "") for k, _ in GOV_FIELDS}
            return self._send(form_page(flat, str(e)), 400)
        print(f"  rendering {params['lat']}, {params['lon']} "
              f"({params['width']:.0f}x{params['height']:.0f} m, "
              f"{params['profile']})")
        try:
            rec = run_generator(params)
        except BadRequest as e:
            values = dict(params)
            values["coords"] = f"{params['lat']}, {params['lon']}"
            return self._send(form_page(values, str(e)), 400)
        except subprocess.TimeoutExpired:
            values = dict(params)
            values["coords"] = f"{params['lat']}, {params['lon']}"
            return self._send(form_page(
                values, "Timed out after 10 minutes. Try a smaller area."), 504)
        self._send(result_page(rec))


def main(argv=None):
    global STAGING_DB, OUT, DEM_DIR
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to accept connections from outside this "
                         "machine, which containers need")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT") or 8765),
                    help="defaults to $PORT when set, which is how most "
                         "hosts tell an app where to listen")
    ap.add_argument("--data-dir", metavar="DIR",
                    default=os.environ.get("MAPS2CAD_DATA"),
                    help="Put the DEM cache, run folders and staging "
                         "database under DIR instead of alongside the repo. "
                         "Point this at a mounted volume when deploying, or "
                         "every generated file and every field-verified name "
                         "dies with the container.")
    ap.add_argument("--db", default=None,
                    help="SQLite staging database to browse and write to "
                         "(default: <data-dir>/staging.sqlite)")
    a = ap.parse_args(argv)
    if a.data_dir:
        root = Path(a.data_dir).resolve()
        DEM_DIR = root / "dem"
        OUT = root / "web"
        STAGING_DB = root / "staging.sqlite"
        for d in (DEM_DIR, OUT):
            d.mkdir(parents=True, exist_ok=True)
    if a.db:
        STAGING_DB = Path(a.db).resolve()
    global SESSIONS
    SESSIONS = gdrive.SessionStore(
        (Path(a.data_dir) if a.data_dir else BASE.parent / "output")
        / "google_sessions.json")

    if not GEN.is_file():
        print(f"ERROR: {GEN} not found", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    restored = load_jobs()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"maps2cad — http://{a.host}:{a.port}")
    print(f"Output directory: {OUT}")
    print(f"History: {restored} previous run(s) restored"
          if restored else "History: no previous runs")
    print(f"Runner: {'uv run' if UV else sys.executable}")
    print("Google Drive: " + ("sign-in enabled" if gdrive.configured() else
                              "not configured (set GOOGLE_CLIENT_ID, "
                              "GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI)"))
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
