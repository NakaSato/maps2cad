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
import time
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
POSTER = BASE / "mapposter.py"
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
import gdrive                                                  # noqa: E402
# Every page the browser sees is built in webui.py. Only routing, jobs and
# the database live here, so the two can be read — and changed — apart.
import webui                                                   # noqa: E402

page = webui.page

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

# A coordinate reaches a person in whatever shape the thing that gave it to
# them uses: a Google Maps link, the app's share sheet, a GPS handset in
# degrees-minutes-seconds, a colleague's email. Refusing everything but
# "15.8, 104.4" makes the user the converter, so read them all. Order
# matters: a URL is tried first because its query string contains decimals
# that the plain-pair rule would otherwise pick up out of the wrong field.
_URL_LATLON = [
    # /maps/@lat,lon,17z  and  /maps/place/Name/@lat,lon,17z
    re.compile(r"/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)"),
    # ?q=lat,lon  ?ll=lat,lon  ?center=lat,lon  ?daddr=lat,lon
    re.compile(r"[?&](?:q|ll|center|daddr|sll)=(-?\d+(?:\.\d+)?),"
               r"(-?\d+(?:\.\d+)?)"),
    # openstreetmap.org/#map=17/lat/lon
    re.compile(r"#map=[\d.]+/(-?\d+(?:\.\d+)?)/(-?\d+(?:\.\d+)?)"),
    # openstreetmap.org/?mlat=..&mlon=..
    re.compile(r"[?&]mlat=(-?\d+(?:\.\d+)?).*?[?&]mlon=(-?\d+(?:\.\d+)?)"),
    # geo:lat,lon  (Android share sheet, RFC 5870)
    re.compile(r"^geo:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)"),
]

# 15°50'02.4"N   15 50 2.4 N   15°50.04'N   N15°50'02"
def _dms_part(n: str) -> str:
    """One half of a degrees-minutes-seconds pair, with numbered groups so
    the two halves can sit in the same expression."""
    return (rf"(?:(?P<pre{n}>[NSEWnsew])\s*)?"
            rf"(?P<deg{n}>-?\d+(?:\.\d+)?)\s*[°ºd]?\s*"
            rf"(?:(?P<min{n}>\d+(?:\.\d+)?)\s*['′m]\s*)?"
            rf"(?:(?P<sec{n}>\d+(?:\.\d+)?)\s*(?:\"|″|'')\s*)?"
            rf"(?P<post{n}>[NSEWnsew])?")


_DMS_PAIR = re.compile(r"^\s*" + _dms_part("1") + r"\s*[,; ]\s*"
                       + _dms_part("2") + r"\s*$")


def _dms_value(deg, minutes, seconds, hemi):
    v = float(deg) + float(minutes or 0) / 60 + float(seconds or 0) / 3600
    if hemi and hemi.upper() in ("S", "W"):
        v = -abs(v)
    return v


def parse_coords(text: str) -> tuple[float, float]:
    """Read a coordinate in whatever shape it arrived in.

    Accepts a decimal pair, hemisphere letters, degrees-minutes-seconds, and
    the URLs Google Maps, OpenStreetMap, Apple Maps and the Android share
    sheet hand out. Raises BadRequest naming what it does accept — a parser
    this permissive that still fails should say why rather than repeat the
    one format the user already tried.
    """
    raw = (text or "").strip()
    if not raw:
        raise BadRequest("Enter a latitude and longitude.")

    if "goo.gl" in raw or "maps.app" in raw:
        raise BadRequest(
            "That is a shortened Google Maps link, which only Google can "
            "expand. Open it in a browser and copy the address bar once it "
            "has loaded — that URL carries the coordinate — or right-click "
            "the map pin and copy the numbers.")

    if "://" in raw or raw.startswith("geo:"):
        for rx in _URL_LATLON:
            m = rx.search(raw)
            if m:
                return _check(float(m.group(1)), float(m.group(2)), raw)
        raise BadRequest(
            f"No coordinate found in that link. In Google Maps, right-click "
            f"the spot and click the latitude/longitude at the top of the "
            f"menu to copy it, then paste that here.")

    # Strip the labels people paste along with the numbers
    cleaned = re.sub(r"(?i)\b(lat|latitude|lon|lng|long|longitude)\b\s*[:=]?",
                     " ", raw).strip(" ,;")

    m = COORD_RE.match(cleaned)
    if m:
        return _check(float(m.group(1)), float(m.group(2)), raw)

    m = _DMS_PAIR.match(cleaned)
    if m:
        h1 = (m.group("pre1") or m.group("post1") or "").upper()
        h2 = (m.group("pre2") or m.group("post2") or "").upper()
        a = _dms_value(m.group("deg1"), m.group("min1"), m.group("sec1"), h1)
        b = _dms_value(m.group("deg2"), m.group("min2"), m.group("sec2"), h2)
        # Hemisphere letters beat position: "104°E, 15°N" is still lat 15.
        if h1 in ("E", "W") and h2 in ("N", "S"):
            a, b = b, a
        return _check(a, b, raw)

    raise BadRequest(
        f"Could not read “{raw}” as a coordinate. Any of these work: "
        "15.83384548, 104.39445555 · 15.8338 N, 104.3944 E · "
        "15°50'02\"N 104°23'40\"E · a Google Maps or OpenStreetMap link.")


def _check(lat: float, lon: float, raw: str) -> tuple[float, float]:
    if not (-90 <= lat <= 90):
        # The one mistake worth naming: a swapped pair reads as a latitude
        # past the pole, and silently swapping it back would be a guess.
        extra = (" — that looks like longitude first; this field takes "
                 "latitude first.") if -90 <= lon <= 90 else "."
        raise BadRequest(f"Latitude {lat} is outside -90..90{extra}")
    if not (-180 <= lon <= 180):
        raise BadRequest(f"Longitude {lon} is outside -180..180.")
    return lat, lon

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

    def number(value, label, lo, hi):
        try:
            n = float(value)
        except ValueError:
            raise BadRequest(f"{label} must be a number (got “{value}”).")
        if not (lo <= n <= hi):
            raise BadRequest(f"{label} must be between {lo} and {hi}.")
        return n

    coords = one("coords")
    lat_s, lon_s = one("lat"), one("lon")
    if coords:
        lat, lon = parse_coords(coords)
    elif lat_s or lon_s:
        # Two separate fields, as the browser's own geolocation sends them.
        lat = number(lat_s, "Latitude", -90, 90)
        lon = number(lon_s, "Longitude", -180, 180)
    else:
        raise BadRequest("Enter a latitude and longitude.")
    # 1000 x 750 m — larger than A3 holds at 1:2000, so a sheet export
    # lands on 1:5000 (A3), 1:2500 (A2) or 1:2000 (A1).
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
        # Everything that *adds* to a run is on here, with no checkbox to
        # forget: the poster, direction arrows wherever they are allowed,
        # every mapped landmark rather than the curated civic set, and
        # colour plot previews. The two switches that *remove* something
        # are not defaulted on — --mono drops the layer colours, and
        # --final drops the DRAFT marking from an unsigned sheet, which is
        # a decision rather than a setting.
        "all_poi": True,
        # Overture's named places, on their own layer. It adds ~20 s to the
        # first run of an extent and nothing to a repeat, because the fetch
        # caches per extent — and a name a drafter can see the source of
        # beats a blank where OSM never had one.
        "overture": True,
        "poster": True,
        "poster_arrows": True,
        "map_arrows": True,
        "plot_colour": True,
        "mono": False,
        "final": one("final") == "on",
        "gov": {k: one(k) for k, _ in GOV_FIELDS},
    }


# What a job is doing right now, keyed by job id. Separate from JOBS,
# which holds finished runs and is rebuilt from disk at startup: a run in
# flight has no files yet and must not appear in history as though it did.
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()


def planned_steps(p: dict) -> list[dict]:
    """The whole plan up front, so the page can show what is still to come
    rather than one bar that says only "working"."""
    steps = []
    if p["export"] in ("both", "map"):
        steps.append({"key": "map", "name": "Site map sheet"})
    if p["export"] in ("both", "cad"):
        steps.append({"key": "dem", "name": "Elevation tile"})
        steps.append({"key": "cad", "name": "CAD drawing"})
        steps.append({"key": "plot", "name": "Plot preview"})
    if p.get("poster"):
        steps.append({"key": "poster", "name": "B&W poster"})
    for st in steps:
        st["state"] = "waiting"
        st["detail"] = ""
    return steps


class Progress:
    """Handle a running job reports through. Every mutation takes the lock:
    the worker thread writes while the browser's poll reads."""

    def __init__(self, jid: str, p: dict):
        self.jid = jid
        with RUNS_LOCK:
            RUNS[jid] = {"steps": planned_steps(p), "state": "running",
                         "error": "", "values": {}, "started": time.time()}

    def _edit(self, fn):
        with RUNS_LOCK:
            run = RUNS.get(self.jid)
            if run:
                fn(run)

    def begin(self, key: str):
        def go(run):
            for st in run["steps"]:
                if st["key"] == key:
                    st["state"] = "running"
        self._edit(go)
        return lambda line: self.say(key, line)

    def say(self, key: str, line: str):
        self._edit(lambda run: [st.update(detail=line[:160])
                                for st in run["steps"] if st["key"] == key])

    def done(self, key: str, detail: str = ""):
        def go(run):
            for st in run["steps"]:
                if st["key"] == key:
                    st["state"] = "done"
                    if detail:
                        st["detail"] = detail
        self._edit(go)

    def skipped(self, key: str, why: str):
        def go(run):
            for st in run["steps"]:
                if st["key"] == key:
                    st["state"] = "skipped"
                    st["detail"] = why
        self._edit(go)

    def finish(self):
        self._edit(lambda run: run.update(state="done"))

    def fail(self, message: str, values: dict):
        def go(run):
            run["state"] = "failed"
            run["error"] = message
            run["values"] = values
            for st in run["steps"]:
                if st["state"] == "running":
                    st["state"] = "failed"
        self._edit(go)


RUNS_KEPT = 50


def prune_runs():
    """Keep the table bounded. A finished run's files are on disk and its
    record is in JOBS, so dropping the oldest progress entries loses only
    the step-by-step view of a run nobody is watching any more."""
    with RUNS_LOCK:
        finished = sorted(((r["started"], jid) for jid, r in RUNS.items()
                           if r["state"] != "running"))
        for _, jid in finished[:max(0, len(RUNS) - RUNS_KEPT)]:
            RUNS.pop(jid, None)


def start_run(p: dict) -> str:
    """Begin a generation on its own thread and return its job id."""
    jid = job_id(p)
    prune_runs()
    progress = Progress(jid, p)
    values = dict(p)
    values["coords"] = f"{p['lat']}, {p['lon']}"

    def work():
        try:
            run_generator(p, progress)
            progress.finish()
        except BadRequest as e:
            progress.fail(str(e), values)
        except subprocess.TimeoutExpired:
            progress.fail("Timed out after 10 minutes. Try a smaller area.",
                          values)
        except Exception as e:                          # noqa: BLE001
            # A crash must reach the page. Losing it to a dead thread would
            # leave the browser polling a run that is never going to move.
            progress.fail(f"{type(e).__name__}: {e}", values)

    threading.Thread(target=work, daemon=True).start()
    return jid


# What a run can show while it is still going, newest-looking first. Only
# the kinds a browser renders on its own: a DXF has no viewer and a GeoTIFF
# is not a picture, so neither belongs in a preview pane.
PREVIEW_KINDS = (("plot", "Plot preview", "pdf"),
                 ("png", "Site map", "image"),
                 ("poster", "Poster", "image"),
                 ("pdf", "Site map (PDF)", "pdf"))


# Content types are stated here, not asked of the operating system.
# mimetypes.guess_type() reads the Windows registry, so on Windows a .png
# or .pdf can come back with a registry-specific type or with none at all —
# and "none" fell through to application/octet-stream, which every browser
# downloads instead of showing. That is what turned the preview into an
# automatic download there while it rendered fine everywhere else. These are
# our own files with known formats; there is nothing to guess.
CONTENT_TYPES = {".png": "image/png", ".pdf": "application/pdf",
                 ".csv": "text/csv; charset=utf-8", ".tif": "image/tiff",
                 ".tiff": "image/tiff", ".dxf": "image/vnd.dxf",
                 ".zip": "application/zip", ".txt": "text/plain; charset=utf-8",
                 ".json": "application/json", ".geojson": "application/json"}


def content_type(name: str, fallback: str = "application/octet-stream") -> str:
    """The MIME type for one of our own output files."""
    dot = str(name).rfind(".")
    return CONTENT_TYPES.get(str(name)[dot:].lower(), fallback) if dot >= 0 \
        else fallback


def preview_files(jid: str) -> list[dict]:
    """The previewable files a run has produced so far.

    Read off disk rather than tracked through Progress: the scripts write
    when they write, and a file that exists is one the browser can show —
    there is no state to keep in step. Size is reported so the page can
    tell a file being written from one that is finished, and mtime busts
    the browser cache when a step overwrites its own output.
    """
    folder = OUT / jid
    out = []
    for kind, label, how in PREVIEW_KINDS:
        target = folder / KINDS[kind]
        try:
            stat = target.stat()
        except OSError:
            continue
        if stat.st_size <= 0:
            continue
        out.append({"kind": kind, "label": label, "how": how,
                    "bytes": stat.st_size, "ts": int(stat.st_mtime)})
    return out


def run_state(jid: str) -> dict | None:
    with RUNS_LOCK:
        run = RUNS.get(jid)
        state = json.loads(json.dumps(run)) if run else None
    if state is not None:
        state["previews"] = preview_files(jid)
    return state


def job_id(p: dict) -> str:
    key = json.dumps(p, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def useful_line(line: str) -> bool:
    """matplotlib font chatter and uv install noise are not progress."""
    return bool(line.strip()) and "UserWarning" not in line \
        and "savefig" not in line and "fsSelection" not in line \
        and not line.startswith(("Installed", "Resolved", "Built",
                                 "Downloading", "Prepared", " Downloaded",
                                 "Updating"))


def run_step(cmd: list[str], what: str, timeout=900, on_line=None) -> str:
    """Run one script, handing each line of its output to `on_line` as it
    arrives.

    Read line by line rather than collected at the end because these steps
    take 18-105 s and the scripts already narrate themselves — "Retrieving
    OpenStreetMap features", "Reading the elevation tile". Waiting for the
    process to exit before showing any of it is what left the browser with
    an indeterminate bar and nothing to say.
    """
    # Python block-buffers stdout when it is a pipe rather than a terminal,
    # so without this the child's narration arrives in one lump at exit and
    # the live view stays blank for the whole of the slowest step.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            env=env)
    timed_out = threading.Event()

    def give_up():
        timed_out.set()
        proc.kill()

    killer = threading.Timer(timeout, give_up)
    killer.start()
    kept = []
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            if not useful_line(line):
                continue
            kept.append(line)
            if on_line:
                on_line(line)
    finally:
        proc.stdout.close()
        code = proc.wait()
        killer.cancel()
    log = "\n".join(kept)
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout)
    if code != 0:
        raise BadRequest(f"{what} failed:\n{log or '(no output)'}")
    return log


def run_generator(p: dict, progress: "Progress | None" = None) -> dict:
    """Render the requested exports into one job folder."""
    jid = job_id(p)
    run = OUT / jid
    run.mkdir(parents=True, exist_ok=True)
    record = {"id": jid, "params": p, "dir": str(run),
              "when": datetime.now().strftime("%Y-%m-%d %H:%M")}
    logs = []

    # One project name for both exports: the site map overlays the same
    # staged survey data the DXF carries, so a user who imported a parcel
    # gets it on the submission sheet too rather than only in CAD.
    project = (p.get("gov", {}).get("project_name")
               or f"{p['lat']:.6f}_{p['lon']:.6f}_"
                  f"{p['width']:.0f}x{p['height']:.0f}")

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
        # The generator refuses these on the government profile, so the
        # form must not send them there — the sheet renders its spec.
        if p["profile"] != "government":
            if p.get("map_arrows"):
                cmd.append("--arrows")
            if p.get("basemap"):
                cmd += ["--basemap", p["basemap"]]
        # Only when that project is actually staged: on a first run nothing
        # is, and pointing the renderer at a project that does not exist
        # would fail the whole export for the sake of an overlay.
        if project_srid(project):
            cmd += ["--overlay-db", str(STAGING_DB),
                    "--overlay-project", project]
        if p["profile"] == "government":
            if p["final"]:
                cmd.append("--final")
            for key, _ in GOV_FIELDS:
                if p["gov"].get(key):
                    cmd += [f"--{key.replace('_', '-')}", p["gov"][key]]
        on = progress.begin("map") if progress else None
        logs.append(run_step(cmd, "Site map", on_line=on))
        if progress:
            progress.done("map", "sheet, 300 DPI PNG and inventory CSV")
        record.update(pdf=pdf, png=png, csv=csv)

    if p["export"] in ("both", "cad"):
        if progress:
            progress.begin("dem")
            progress.say("dem", "checking the Copernicus tile for this square")
        cached = dem_tile_for(p["lat"], p["lon"])[0].is_file()
        dem = ensure_dem(p["lat"], p["lon"])
        if progress:
            progress.done("dem", "already downloaded" if cached
                          else f"downloaded {Path(dem).name}")
        dxf = str(run / "site.dxf")
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
        # The B### checkbox drives both exports: it said "show codes on
        # unnamed buildings" while only the site map listened to it, so the
        # DXF labelled them either way.
        if not p["codes"]:
            cad_cmd.append("--names-only")
        if p.get("all_poi"):
            cad_cmd.append("--all-poi")
        if p.get("overture"):
            cad_cmd.append("--overture")
        if p.get("mono"):
            cad_cmd.append("--mono")
        on = progress.begin("cad") if progress else None
        logs.append(run_step(cad_cmd, "CAD export", on_line=on))
        if progress:
            progress.done("cad")
        record["project"] = project
        record["dxf"] = dxf
        # Files the CAD step writes beside the drawing, when it wrote them
        # "csv" included: topo2cad.py writes building_inventory.csv beside
        # the drawing, and a CAD-only run offered every other table it
        # produced but not the one the B### codes on the sheet are keyed
        # on — 15.9 KB of it sitting in the folder, unreachable.
        for kind in ("tif", "attrs", "roads", "landmarks", "csv",
                     "sources"):
            if (run / KINDS[kind]).is_file():
                record[kind] = str(run / KINDS[kind])
        plot = str(run / "site_preview.pdf")
        try:
            # With a sheet, plot the titled layout at its own paper size
            pdf_cmd = script_cmd(DXF2PDF) + [dxf, "-o", plot]
            pdf_cmd += (["--layout", "SHEET", "--size", sheet]
                        if sheet and sheet != "none" else ["--size", "A3"])
            # A preview is read on a screen, where the NCS layer colours are
            # the fastest way to see what is on which layer. dxf2pdf keeps
            # black as *its* default, which is right for a plotted sheet.
            if p.get("plot_colour", True):
                pdf_cmd.append("--color")
            on = progress.begin("plot") if progress else None
            logs.append(run_step(pdf_cmd, "DXF plot preview", on_line=on))
            if progress:
                progress.done("plot")
            record["plot"] = plot
        except BadRequest as e:      # preview is a convenience, not the export
            logs.append(f"NOTE: plot preview unavailable — {e}")
            if progress:
                progress.skipped("plot", "unavailable — the drawing is fine")

    if p.get("poster"):
        # Its own deliverable, not a variant of the others: a B&W print map
        # for a wall or a submission cover, from the same coordinate.
        dem = ensure_dem(p["lat"], p["lon"])
        png = str(run / "poster.png")
        cmd = script_cmd(POSTER) + [
            "--lat", repr(p["lat"]), "--lon", repr(p["lon"]),
            "--width", repr(p["width"]), "--height", repr(p["height"]),
            "--dem", str(dem), "--out", png, "--title", p["title"]]
        if p.get("poster_arrows"):
            cmd.append("--arrows")
        if p.get("basemap"):
            cmd += ["--basemap", p["basemap"]]
        on = progress.begin("poster") if progress else None
        logs.append(run_step(cmd, "Poster", on_line=on))
        if progress:
            progress.done("poster")
        record["poster"] = png
        if (run / "poster.pdf").is_file():
            record["poster_pdf"] = str(run / "poster.pdf")

    record["log"] = "\n".join(logs)
    save_job(record)
    with JOBS_LOCK:
        JOBS[jid] = record
    return record


# ------------------------------------------------------------------ history
KINDS = {"pdf": "site_map.pdf", "png": "site_map.png",
         "csv": "building_inventory.csv", "dxf": "site.dxf",
         # Which road is which and what number it carries. Buildings have
         # had an inventory from the start; roads had none, so the most
         # asked-for fact on a site plan meant opening the DXF.
         "roads": "road_inventory.csv",
         # สถานที่สำคัญใกล้เคียง — what is nearby, how far, and which way.
         # The sheet has always carried the symbols; this is the list a
         # ผังบริเวณ is read alongside.
         "landmarks": "landmark_inventory.csv",
         # Which source supplied which feature class. Every route writes
         # one now, not only compose.py.
         "sources": "sources.csv",
         "plot": "site_preview.pdf",
         # The DXF stores a path to the background map, not its pixels, so
         # the GeoTIFF has to be downloadable beside it or the drawing opens
         # with a missing raster reference.
         "tif": "basemap.tif",
         # Every OSM tag of every drawn feature. The entities carry these as
         # XDATA too, but that needs AutoCAD to read; this opens anywhere.
         "attrs": "attributes.csv",
         # mapposter writes the pair; the PNG previews in a browser, the PDF
         # is what goes to a printer.
         "poster": "poster.png", "poster_pdf": "poster.pdf",
         # An import merged into a site produces two drawings: what the
         # upload itself holds, and the project it joined. "site.dxf" is
         # always the second, because a user who imports a survey into
         # their site and downloads "the drawing" means the site.
         "import_dxf": "import.dxf"}


def job_zip(rec: dict) -> tuple[bytes, str]:
    """Every file of one run, as a zip, under their on-disk names.

    The names matter more here than anywhere else: the DXF references its
    background map as "basemap.tif" relative to itself, so a package that
    renamed either would extract to a drawing with a missing raster. The
    per-file download route renames on purpose (a folder of "site.dxf" is
    useless); a package is the opposite case — it travels together.
    """
    import io
    import zipfile

    params = rec.get("params") or {}
    lat, lon = params.get("lat", 0.0), params.get("lon", 0.0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        packed = set()
        for kind, name in KINDS.items():
            path = rec.get(kind)
            if path and Path(path).is_file():
                z.write(path, name)
                packed.add(name)
        # Then anything else the run left in its folder that a reader needs
        # and the record happens not to name — a run restored from an older
        # session, or written by a route that never registered it. The
        # promise here is "every file of this run", so the folder is the
        # authority, not the bookkeeping.
        folder = Path(rec["dir"]) if rec.get("dir") else None
        for extra in ("sources.csv", "fonts.txt"):
            if extra in packed or folder is None:
                continue
            side = folder / extra
            if side.is_file():
                z.write(side, extra)
    stem = (f"maps2cad_{lat:.6f}_{lon:.6f}" if (lat or lon)
            else f"maps2cad_{rec['id']}")
    return buf.getvalue(), f"{stem}.zip"


def save_job(rec: dict) -> None:
    """Persist a run so history survives a restart."""
    # The staged project name rides along: without it a restart leaves every
    # run in history with no way back to the project it wrote into, which is
    # the whole path to correcting a name and re-issuing.
    meta = {"id": rec["id"], "params": rec["params"], "when": rec["when"],
            "log": rec.get("log", ""), "project": rec.get("project", ""),
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
               "project": meta.get("project", ""), "dir": str(folder)}
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
    return webui.history_table(history(limit), project_ids())


# --------------------------------------------------------------------- pages




def form_page(values: dict | None = None, error: str = "") -> bytes:
    return webui.form_page(values or {}, error, history_html(8),
                           GOV_FIELDS, BASEMAP_CHOICES)


def result_page(rec: dict) -> bytes:
    p = rec["params"]
    return webui.result_page(rec, KINDS,
                             utm_zone_label(p["lat"], p["lon"]),
                             gdrive.configured(),
                             project_ids().get(rec.get("project") or ""))


def run_page(jid: str, state: dict) -> bytes:
    return webui.run_page(jid, state)


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
        return webui.projects_page(None)
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
    return webui.projects_page(rows)


PER_PAGE = 100




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
    # What this drawing is made of. A project can hold an OSM extraction,
    # Microsoft footprints, Overture places, DEM levels and any number of
    # imported files at once, and the only honest answer to "where did this
    # line come from" is the source column of the row it was drawn from.
    sources = stage_db_module().provenance(conn, pid)
    conn.close()

    # The runs that wrote into this project. A staged project and the files
    # it came from are one site; listing them apart is how yesterday's run
    # gets lost.
    runs = [r for r in history() if r.get("project") == proj["name"]]
    return webui.project_page(
        {"pid": pid, "proj": proj, "buildings": buildings,
         "roads": roads, "sources": sources, "total_all": total_all,
         "unnamed_all": unnamed_all, "matched": matched,
         "pages": pages, "runs": runs},
        note, q, page_no, only, PER_PAGE)


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


def project_ids() -> dict[str, int]:
    """Staged project name -> id, for linking a run to what it staged.

    A CAD run records the project name it wrote into; the browser needs the
    id to link to it. Without this the two halves of one site sit on
    separate pages with nothing joining them, which is exactly how someone
    loses the run they did yesterday.
    """
    conn = db_conn()
    if conn is None:
        return {}
    try:
        return {r["name"]: r["id"]
                for r in conn.execute("SELECT id, name FROM projects")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def project_srid(name: str):
    """The projected CRS a staged project is already in, or None."""
    conn = db_conn()
    if conn is None:
        return None
    row = conn.execute("SELECT srid FROM projects WHERE name = ?",
                       (name,)).fetchone()
    conn.close()
    return str(row["srid"]) if row else None


def import_page(note: str = "", error: str = "") -> bytes:
    conn = db_conn()
    projects = conn.execute("SELECT id, name FROM projects ORDER BY name"
                            ).fetchall() if conn else []
    if conn:
        conn.close()
    return webui.import_page(projects, OSM_TYPES, OSM_TYPE_LABELS,
                             BASEMAP_CHOICES, note, error)


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
        elif path.startswith("/run/"):
            parts = path.strip("/").split("/")
            jid = parts[1] if len(parts) > 1 else ""
            if len(parts) == 4 and parts[2] == "file":
                return self.serve_run_file(jid, parts[3])
            if len(parts) == 3 and parts[2] == "status":
                state = run_state(jid)
                if state is None:
                    return self._send(b'{"state":"unknown"}', 404,
                                      "application/json")
                return self._send(json.dumps(state).encode(), 200,
                                  "application/json")
            with JOBS_LOCK:
                rec = JOBS.get(jid)
            state = run_state(jid)
            if state and state["state"] == "failed":
                return self._send(
                    form_page(state["values"], state["error"]), 400)
            # A finished run is its result page; a run still going gets the
            # watcher. Reloading either one is safe.
            if state and state["state"] == "running":
                return self._send(run_page(jid, state))
            if rec:
                return self._send(result_page(rec))
            self._send(b"No such run", 404, "text/plain")
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
        elif path.startswith("/zip/"):
            self.serve_zip(path.strip("/").split("/")[-1])
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

    def serve_zip(self, jid):
        with JOBS_LOCK:
            rec = JOBS.get(jid)
        if not rec:
            return self._send(b"Unknown map id", 404, "text/plain")
        try:
            data, name = job_zip(rec)
        except OSError as e:
            return self._send(f"Could not package this run: {e}".encode(),
                              500, "text/plain")
        self._send(data, ctype="application/zip",
                   extra={"Content-Disposition":
                          f'attachment; filename="{name}"'})

    def serve_run_file(self, jid, kind):
        """A file from a run still in flight, for the live preview.

        /file/ and /view/ both read the JOBS record, which only exists once
        the run has finished — the whole point here is to show the work
        before then. The folder is addressed by job id and the filename
        comes from the KINDS table, never from the URL, so there is nothing
        for a caller to traverse with.
        """
        if not re.fullmatch(r"[0-9a-f]{8,32}", jid or ""):
            return self._send(b"Bad request", 400, "text/plain")
        if kind not in dict((k, l) for k, l, _h in PREVIEW_KINDS):
            return self._send(b"Bad request", 400, "text/plain")
        target = OUT / jid / KINDS[kind]
        if not target.is_file():
            return self._send(b"Not yet", 404, "text/plain")
        ctype = content_type(target.name, "application/pdf")
        try:
            body = target.read_bytes()
        except OSError:
            # Being written this instant: the poll comes round again.
            return self._send(b"Not yet", 404, "text/plain")
        # Shown inline, and never cached: a step can overwrite its own
        # output and the page has to see the new one.
        self._send(body, ctype=ctype,
                   extra={"Content-Disposition":
                          f'inline; filename="{KINDS[kind]}"',
                          "Cache-Control": "no-store"})

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
        # A download says what it is too: a browser that has to guess at a
        # .dxf or a .tif is a browser that may rename or mangle it.
        ctype = content_type(target.name)
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
        if kind in ("csv", "attrs", "roads", "landmarks", "sources"):
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
            if kind == "landmarks":
                return self._send(page("Landmarks", f"""
<p class="eyebrow">{len(body)} place(s) near the site</p>
<h1>สถานที่สำคัญใกล้เคียง / Nearby landmarks</h1>
<p class="lede">Every landmark the drawing carries, nearest first, with how
far it is from the site coordinate and the bearing to it — north-based and
clockwise, the way a surveyor reads one. The sheet shows these as symbols;
this is the list a ผังบริเวณ is read alongside.</p>
{"".join(table)}
{more}
<a class="back" href="/run/{html.escape(parts[1])}">← Back to the run</a>
"""))
            if kind == "roads":
                return self._send(page("Roads", f"""
<p class="eyebrow">{len(body)} road(s) in the extent</p>
<h1>Roads</h1>
<p class="lede">Every centreline the drawing carries, longest first: its
route number, its name in Thai and Latin, the formal
<code>official_name</code> where OpenStreetMap records one, the carriageway
width the edges were offset by, and its length. The sheet labels a road
once per name; this lists every way.</p>
{"".join(table)}
{more}
<a class="back" href="/run/{html.escape(parts[1])}">← Back to the run</a>
"""))
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

        ctype = content_type(target.name, "application/pdf")
        # Named as well as inline: a browser that does decide to save it
        # then writes a sensible filename instead of the route's last path
        # segment ("plot", "png").
        self._send(target.read_bytes(), ctype=ctype,
                   extra={"Content-Disposition":
                          f'inline; filename="{KINDS[kind]}"'})

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
        # An unticked box is absent from the body, so the positive sense
        # lives on the checkbox and its absence is what suppresses the
        # codes. A re-issue cannot inherit the choice from the run that
        # staged the project — the flag is a drawing option and was never
        # staged — so the form asks rather than guessing.
        codes = "codes" in form
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
            if not codes:
                cmd.append("--names-only")
            if sheet != "none":
                cmd += ["--sheet", sheet, "--scale", scale]
            log = run_step(cmd, "Re-issue")
            plot = str(run / "site_preview.pdf")
            try:
                pdf_cmd = script_cmd(DXF2PDF) + [dxf, "-o", plot, "--color"]
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
        for kind in ("attrs", "roads", "landmarks", "sources"):
            if (run / KINDS[kind]).is_file():
                rec[kind] = str(run / KINDS[kind])
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
            # Merging into a project that already exists means adopting its
            # CRS. Each converter otherwise derives a UTM zone from its own
            # data, so a survey file whose centroid falls the other side of
            # 102 degrees East would stage in zone 48 inside a zone 47
            # project — a kilometre-scale error that looks like nothing
            # until the drawing opens. An EPSG typed into the form still
            # wins: that is someone stating what their file is in.
            if not epsg:
                epsg = project_srid(project)
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
                if fields.get("all_poi"):
                    cmd += ["--all-poi"]
                if fields.get("names_only"):
                    cmd += ["--names-only"]
                if fields.get("mono"):
                    cmd += ["--mono"]
                if fields.get("replace"):
                    cmd += ["--replace"]
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
                if fields.get("mono"):
                    cmd += ["--mono"]
                log = run_step(cmd, "GIS import")

            # The import drew only what was uploaded. If that merged into a
            # project holding anything else — an OSM extraction, an earlier
            # import — then the drawing a user means by "the DXF" is the
            # combined one, and handing them two survey lines labelled
            # site.dxf is a misleading deliverable. So re-issue from the
            # staging layer, exactly as compose.py finishes a run.
            import_only = run / KINDS["import_dxf"]
            try:
                Path(dxf).replace(import_only)
                log += "\n" + run_step(
                    script_cmd(DB2DXF) + ["--db", str(STAGING_DB),
                                          "--project", project,
                                          "--out", dxf],
                    "Combined drawing from the staging layer")
            except (BadRequest, OSError) as e:
                # The import itself succeeded; keep it rather than lose the
                # run to a re-issue problem.
                if import_only.is_file() and not Path(dxf).is_file():
                    import_only.replace(Path(dxf))
                    import_only = None
                log += f"\nNOTE: combined drawing unavailable — {e}"

            plot = str(run / "site_preview.pdf")
            try:
                log += "\n" + run_step(
                    script_cmd(DXF2PDF) + [dxf, "--size", "A3", "-o", plot,
                                           "--color"],
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
               **{k: str(run / KINDS[k]) for k in ("tif", "attrs",
                                                   "import_dxf")
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
        # The run happens on its own thread and the browser is sent to a page
        # that watches it. Holding the POST open for 18-105 s is what made
        # the wait a blank bar: the steps narrate themselves, but nothing
        # could show them until the response finally arrived.
        jid = start_run(params)
        self._redirect(f"/run/{jid}")


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
        # The scripts run as subprocesses and find their own caches through
        # this, so --data-dir has to reach them too: without it a tile cache
        # lands beside the code and dies with the container, re-fetching
        # tiles a usage policy asks us not to re-fetch.
        os.environ["MAPS2CAD_DATA"] = str(root)
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
