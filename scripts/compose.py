#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Many sources, one drawing — and a table saying which is which.

Everything needed to combine sources already existed here: `topo2cad.py`
pulls OpenStreetMap, Microsoft's ML footprints, a Copernicus DEM and
optionally Overture places; `osm2cad.py` reads an .osm export; `gis2cad.py`
reads a shapefile, GeoJSON, GeoPackage or KML; the SQLite staging layer
merges them under one project; and `db2dxf.py` issues one DXF from all of
it. What was missing is the single command, and the honesty that has to go
with it:

    uv run scripts/compose.py --lat 13.7455 --lon 100.5325 \\
        --width 500 --height 400 --dem dem/dem_n13_e100.tif \\
        --overture --basemap carto \\
        --add survey/boundary.geojson --add extract/soi.osm \\
        --outdir output/runs --sheet A3

The order is not arbitrary. The OSM step **replaces** what is staged for
the project — re-running a coordinate must not leave the previous run's
features behind — and every import after it **merges**, which is why the
imports come second and never the other way round.

Each step runs as its own `uv run` subprocess rather than an import. The
two OSM stacks in this repo have deliberately disjoint dependency sets, and
a conductor that imported both would be the thing that fused them.

What makes the result trustworthy is `sources.csv` and the provenance table
printed at the end: one row per (source, feature class), naming the file
each import came from. A combined drawing without that is not "combined",
it is "mixed" — and a reviewer asking where a boundary came from deserves
an answer better than "GIS".
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stage_db                                          # noqa: E402
from serve import BadRequest, import_kind, script_cmd     # noqa: E402

HERE = Path(__file__).resolve().parent
TOPO2CAD = HERE / "topo2cad.py"
OSM2CAD = HERE / "osm2cad.py"
GIS2CAD = HERE / "gis2cad.py"
DB2DXF = HERE / "db2dxf.py"


def run_step(cmd, what: str) -> None:
    """Run one converter, streaming its output under a heading."""
    print(f"\n── {what}\n   $ {' '.join(str(c) for c in cmd)}", flush=True)
    proc = subprocess.run([str(c) for c in cmd], text=True,
                          capture_output=True)
    for line in (proc.stdout or "").splitlines():
        if line.strip() and not line.startswith(
                ("Installed", "Resolved", "Built", "Downloading", "Prepared",
                 " Downloaded", "Updating", "Audited")):
            print(f"   {line}")
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise SystemExit(f"\n{what} failed:\n{detail or '(no output)'}")


def uv_run(script: Path) -> list:
    """`uv run` where uv is installed, plain python where it is not.

    Imported from serve.py rather than restated: the container image ships
    the union of every script's dependencies and no uv at all, and a
    conductor that insisted on uv would work on a laptop and fail on the
    deploy.
    """
    return script_cmd(script)


def step_name(index: int, path: Path) -> str:
    """A predictable per-step drawing name inside the run folder."""
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in path.stem)
    return f"step{index}_{stem[:40]}.dxf"


def plan_imports(paths):
    """[(path, 'osm'|'gis')] — routed by extension, exactly as the web
    upload routes it.

    `import_kind` is imported from serve.py rather than restated here: the
    browser and the CLI must never disagree about which converter draws a
    file, because the two produce different drawings from the same ground.
    """
    out = []
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            raise SystemExit(f"ERROR: no such file: {path}")
        out.append((path, import_kind([path])))
    return out


def project_srid(db, project):
    """The projected CRS this project is already staged in, if any."""
    if not Path(db).is_file():
        return None
    conn = stage_db.connect(db)
    row = conn.execute("SELECT srid FROM projects WHERE name = ?",
                       (project,)).fetchone()
    conn.close()
    return row["srid"] if row else None


def set_extent(db, project, lat, lon, width_m, height_m) -> bool:
    """Write the requested extent onto the project row."""
    conn = stage_db.connect(db)
    cur = conn.execute(
        "UPDATE projects SET lat = ?, lon = ?, width_m = ?, height_m = ?"
        " WHERE name = ?", (lat, lon, width_m, height_m, project))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--width", type=float, default=200.0)
    ap.add_argument("--height", type=float, default=150.0)
    ap.add_argument("--dem", help="Copernicus tile covering the coordinate; "
                                  "required unless --no-osm")
    ap.add_argument("--add", action="append", default=[], metavar="FILE",
                    help="Merge a file into the same drawing: .osm/.xml/"
                         ".gz/.bz2 through osm2cad.py, everything else "
                         "(.geojson, .shp, .gpkg, .kml, .zip) through "
                         "gis2cad.py. Repeatable.")
    ap.add_argument("--no-osm", action="store_true",
                    help="Skip the OpenStreetMap/DEM step and build the "
                         "drawing from --add files alone.")
    ap.add_argument("--project", help="staging project name "
                                      "(default: the coordinate and extent)")
    ap.add_argument("--db", help="staging database "
                                 "(default: staging.sqlite in the run folder)")
    ap.add_argument("--outdir", default="output/runs")
    ap.add_argument("--sheet", help="also build a paper-space sheet: A4..A0")
    ap.add_argument("--epsg", help="projected CRS for the imports; default "
                                   "is the UTM zone of the coordinate")
    # Passed straight through to topo2cad.py, so one command can ask for
    # everything this repo knows how to draw.
    ap.add_argument("--overture", action="store_true")
    ap.add_argument("--basemap", metavar="PROVIDER")
    ap.add_argument("--all-features", action="store_true")
    ap.add_argument("--all-poi", action="store_true")
    ap.add_argument("--hatch", action="store_true")
    ap.add_argument("--grid", nargs="?", const="auto", metavar="SPACING")
    ap.add_argument("--contour-interval", type=float, metavar="M")
    ap.add_argument("--no-ml", action="store_true")
    a = ap.parse_args(argv)

    if not a.no_osm and not a.dem:
        print("ERROR: --dem is required for the OpenStreetMap step (it draws "
              "contours and spot heights). Use --no-osm to build from --add "
              "files alone.", file=sys.stderr)
        return 1
    if a.no_osm and not a.add:
        print("ERROR: --no-osm with nothing to --add would compose nothing.",
              file=sys.stderr)
        return 1

    try:
        imports = plan_imports(a.add)
    except BadRequest as exc:                  # mixed kinds in one upload
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    extent = f"{a.width:.0f}x{a.height:.0f}"
    run = Path(a.outdir) / f"{a.lat:.6f}_{a.lon:.6f}_{extent}_{stamp}"
    run.mkdir(parents=True, exist_ok=True)
    db = Path(a.db) if a.db else run / "staging.sqlite"
    project = a.project or f"{a.lat:.6f}_{a.lon:.6f}_{extent}"

    print(f"Composing {len(imports) + (0 if a.no_osm else 1)} source step(s) "
          f"into one drawing")
    print(f"  project : {project}")
    print(f"  staging : {db}")
    print(f"  run     : {run}")

    if not a.no_osm:
        cmd = uv_run(TOPO2CAD) + [
            "--lat", repr(a.lat), "--lon", repr(a.lon),
            "--width", repr(a.width), "--height", repr(a.height),
            "--dem", a.dem, "--out", str(run / "step1_openstreetmap.dxf"),
            "--db", str(db), "--project", project]
        for flag in ("overture", "all_features", "all_poi", "hatch", "no_ml"):
            if getattr(a, flag):
                cmd.append("--" + flag.replace("_", "-"))
        if a.basemap:
            cmd += ["--basemap", a.basemap]
        if a.grid:
            cmd += ["--grid", a.grid]
        if a.contour_interval:
            cmd += ["--contour-interval", repr(a.contour_interval)]
        run_step(cmd, "OpenStreetMap + Microsoft ML + DEM")

    for i, (path, kind) in enumerate(imports, start=2):
        out = run / step_name(i, path)
        script = OSM2CAD if kind == "osm" else GIS2CAD
        cmd = uv_run(script) + ["--input", str(path), "--out", str(out),
                                "--db", str(db), "--project", project]
        # One CRS for every source. Each converter derives a UTM zone from
        # its own data, and a survey file whose centroid falls the other
        # side of 102°E would otherwise stage in zone 48 inside a zone 47
        # project — a kilometre-scale error that looks like nothing until
        # the drawing opens. The zone the project already carries wins.
        # (A merge is also the point of composing: --replace here would make
        # each import destroy the one before it.)
        srid = a.epsg or project_srid(db, project)
        if srid:
            cmd += ["--epsg", str(srid)]
        run_step(cmd, f"Import {path.name} ({kind})")

    # An import carries features, not an extent, so the drawing furniture —
    # crop line, dimensions, grid, north arrow — comes from what was asked
    # for here. Harmless when the OSM step already wrote the same numbers.
    set_extent(db, project, a.lat, a.lon, a.width, a.height)

    final = run / "site.dxf"
    cmd = uv_run(DB2DXF) + ["--db", str(db), "--project", project,
                            "--out", str(final)]
    if a.sheet:
        cmd += ["--sheet", a.sheet]
    run_step(cmd, "Combined drawing from the staging layer")

    conn = stage_db.connect(db)
    row = conn.execute("SELECT id FROM projects WHERE name = ?",
                       (project,)).fetchone()
    rows = stage_db.provenance(conn, row["id"]) if row else []
    conn.close()
    sources_csv = run / "sources.csv"
    stage_db.write_provenance_csv(sources_csv, rows)

    print(f"\nSources in {final.name}:")
    print(stage_db.format_provenance(rows))
    print(f"\n  {sources_csv}")
    print("Re-issue any time without re-fetching:")
    print(f"  uv run scripts/db2dxf.py --db {db} --project '{project}' "
          f"--out {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
