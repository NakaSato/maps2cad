# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Standalone scripts that turn a GPS coordinate into engineering deliverables around
that point: a DXF for CAD, poster-style PNG/PDF maps, and a print-ready site map
sheet (including a Thai government submission layout). Data comes from
OpenStreetMap, Copernicus 30 m DEM tiles, and Microsoft ML building footprints.

## Commands

Every script is a self-contained [PEP 723](https://peps.python.org/pep-0723/) file
declaring its own dependencies in a `# /// script` header, so `uv run` installs
them automatically. There is no package, no build step, and no lint config.

```bash
# DXF is the primary export. --outdir gives the run its own folder.
uv run scripts/topo2cad.py --lat 15.83384548 --lon 104.39445555 \
  --width 500 --height 400 --dem dem/dem_n15_e104.tif --outdir output/runs
uv run scripts/dxf2pdf.py output/runs/<run>/site.dxf --size A3   # check plot
uv run scripts/mapposter.py --lat 14.8165 --lon 100.5116 --radius 150 \
  --dem dem/dem_n14_e100.tif --out output/poster.png   # needs a DEM tile
uv run scripts/generate_detailed_site_map.py --lat 14.8165 --lon 100.5116 \
  --width 500 --height 250 --outdir output/runs --png   # no DEM needed
uv run scripts/serve.py                                # web UI on :8765
```

`topo2cad.py` and `mapposter.py` need the Copernicus DEM tile covering the
coordinate (integer degrees, floored). Download once per 1°×1° tile:

```bash
curl -o dem/dem_n15_e104.tif "https://copernicus-dem-30m.s3.amazonaws.com/\
Copernicus_DSM_COG_10_N15_00_E104_00_DEM/Copernicus_DSM_COG_10_N15_00_E104_00_DEM.tif"
```

Tests (`tests/test_generator.py` for the site map, `tests/test_topo2cad.py` for
the CAD path's geometry and CRS helpers):

```bash
uv run --with pytest --with pillow python -m pytest tests/ -q
uv run --with pytest --with pillow python -m pytest tests/ -q -k label_fits   # one test
RUN_NETWORK_TESTS=1 uv run --with pytest --with pillow python -m pytest tests/ -q
```

The network test is opt-in because it hits Overpass; everything else runs offline
against synthetic GeoDataFrames. A `.venv` + `requirements.txt` exist as an
alternative to `uv run` (`.venv/bin/python -m pytest tests/ -q`).

## Architecture

**Two independent OSM stacks. They share no code.**

1. `topo2cad.py` owns the raw-Overpass layer and is imported as a module by
   `mapposter.py` (`from topo2cad import bbox_around, fetch_osm, clip_runs,
   fetch_ms_buildings, best_name, utm_transformer`) — despite being a script.
   Editing `fetch_osm`, `clip_runs`, or `bbox_around` changes both tools. It
   POSTs a hand-written Overpass QL query, rotating through three endpoints with
   retries, and parses raw elements into per-category lists.
   Its heavy imports (rasterio, scipy, skimage, numpy, ezdxf) live **inside
   `main()` on purpose** so the pure helpers stay importable — and testable —
   without the DEM/CAD stack. Keep new pure helpers above `main()` and don't
   promote those imports back to module level.
2. `generate_detailed_site_map.py` (and `serve.py`, which shells out to it) uses
   **osmnx + geopandas** instead. It never touches the topo2cad helpers.

Consequences: the two stacks pull different tag sets, fail differently on network
errors, and must be fixed separately. The dependency sets are disjoint too —
rasterio/scikit-image/ezdxf on one side, osmnx/geopandas on the other.

**CRS is derived, never assumed.** Every tool picks the UTM zone from the
coordinate — `utm_epsg_for()` / `utm_transformer()` in `topo2cad.py` (imported by
`mapposter.py`), and an equivalent `utm_epsg_for()` in
`generate_detailed_site_map.py`. Do not reintroduce a hardcoded `EPSG:32647`:
Thailand spans two zones, and a site at 104.4°E forced into 47N lands at easting
1,078,000 (outside the zone's valid range) with +0.37% scale error — 3.75 m per
kilometre, which is not acceptable in a CAD deliverable.

**Three ways in, one drawing convention.** `topo2cad.py` (OSM + DEM),
`db2dxf.py` (staging database) and `gis2cad.py` (user-supplied GeoJSON/SHP/
GPKG/KML) all emit the same NCS layers and MTEXT conventions. `gis2cad.py`
exists because large areas have no OSM or ML data at all — at 12.526,
102.15982 the nearest ML footprint is 3.19 km away, so an empty drawing there
is correct, not a bug. Check with a coverage query before hunting for one.

**Sparse-data fallback, and why the two stacks disagree.** When OSM returns fewer
than 20 buildings, `topo2cad.py` and `mapposter.py` supplement with Microsoft ML
footprints (unnamed, cached in `dem/ms_cache/`). `generate_detailed_site_map.py`
deliberately does not — its spec forbids inventing features, so it renders only
what OSM has. On a rural site this is a large difference, not a rounding error:
at 15.8338, 104.3945 the DXF carries 155 buildings while the site map PDF shows
1. Expect the question "why does the PDF have fewer buildings than the CAD file"
and answer with this, not with a bug hunt.

**Default extent is 770 × 410 m** across the CLI tools and the web form, chosen
because it fills the A3 standard map frame at exactly 1:2000 — a round scale, so
the title block's มาตราส่วน is one a reviewing agency expects. Changing the frame
proportions or this default breaks that relationship; recompute before touching
either. `topo2cad.py` still accepts `--radius` for a square box.

**CAD output follows the NCS/AIA layer convention** (`C-BLDG-OUTL`,
`C-ROAD-CNTR`, `C-ROAD-EDGE`, `C-ANNO-TEXT`, …) — see `LAYERS` in
`topo2cad.py`. All annotation is MTEXT anchored Middle Center. Road names are
rotated along the centreline and the route `ref` is a separate MTEXT offset
perpendicular, so name and number never overprint.

**Annotation splits by script, onto three layers.** `C-ANNO-TEXT-TH` carries
Thai, `C-ANNO-TEXT-EN` carries Latin, and `C-ANNO-TEXT` keeps everything
language-neutral — B### codes, contour elevations, the GPS tag, the north
arrow. A drafter freezes one language layer to plot a single-language sheet;
the neutral layer must survive both, which is why codes are not "English".
At a rural site that distinction is the whole drawing: at 15.8338, 104.3945
all 155 building labels are codes, so filing codes as English would blank the
Thai-only plot entirely. `names_by_lang()` in `topo2cad.py` resolves
`(name:th, name:en)`, with a plain `name` filed by its own script — Thai
convention puts Thai in `name`, but a business trading under an English name
overrides that, so `name` alone is not a reliable Thai source. When a feature
carries both, English is stacked one line above Thai via
`offset_along_normal()`, which offsets square to the label's own rotation —
a plain -Y nudge would drift off a rotated road label.

**Landmarks come in two shapes, and the tag filter is deliberate.** A POI is
`amenity`/`tourism`/`historic` (`poi_kind()`); the query used to ask for
`node["name"]`, which over a 770 × 410 m extent at Pathum Wan returned 293
nodes of which 186 were mall floor markers, shop brands, benches and bus
stops — each drawing a symbol and a label. Points land on `C-ANNO-SYMB` with
the name offset `POI_LABEL_DX` (3 m) in x, and a name is required, so an
unnamed bicycle stand adds nothing. Areas *without* a `building` tag —
hospital and school grounds, temple precincts, car parks — go on
`C-SITE-POI`, not `C-BLDG-OUTL`, so a 3,000 m² car park does not read as a
structure; an area that *is* a building already came through the building
branch. Area POIs are staged in `staging_buildings` with their own
`cad_layer` (they need a polygon, an interior anchor and an area, which is
what that table stores) and are excluded from the building inventory CSV and
from `serve.py`'s name editor by that same column. Rural sites have none of
this: Yasothon and Lopburi return 0 area POIs, Pathum Wan returns 10.

**Thai needs a text style, not just UTF-8.** ezdxf writes UTF-8 regardless,
but AutoCAD renders Thai as `???` unless the MTEXT's style points at a font
carrying U+0E00–U+0E7F. All four writers register `TH_STYLE`
(`THSarabunNew.ttf`, the Thai government document font) and `EN_STYLE`
(`arial.ttf`) and bind every MTEXT to one of them — including `sheet.py`,
whose title block is Thai. Never emit MTEXT on the default `Standard` style.
The `TEXT_STYLES` dict is duplicated in `topo2cad.py`, `db2dxf.py` and
`gis2cad.py` (as `LAYER_STYLE` already is); keep the three in step.

Building labels fall back to the `B###` inventory code when OSM has no name.
This matters: at the Yasothon site 0 of 239 footprints carry an OSM name (238
come from Microsoft ML), so a names-only rule yields a completely unlabelled
drawing. `--names-only` opts into the stricter behaviour.

**Two ways to reach a DXF, and they must agree.** `topo2cad.py` draws during
extraction; `db2dxf.py` draws from the SQLite staging layer (`stage_db.py`)
using only SELECTs, because label anchors and rotations are computed at staging
time. Both emit the same NCS layers and the same entity counts — if you change
drawing rules in one, change the other, and check with a layer-count diff of the
two outputs. The language split lives in the `cad_labels` view: it emits one
row per language plus a `label_offset` in metres that `db2dxf.py` applies
perpendicular to `label_rotation`, so the anchor stored per feature stays the
feature's own point. SQLite has no regex, so any script test happens in Python
(`split_by_script()`) at staging time, never in the view. New `staging_*`
columns need an entry in `MIGRATIONS`; `connect()` runs `migrate()` on every
open because a view body is not resolved until it is queried, so an older file
would otherwise fail the moment something selects from `cad_labels`.
Anything that writes a name — `serve.py`'s editor, `stage_db.py --set-name`,
`apply_verified()` — must set `name_th`/`name_en` alongside `display_name`, or
a field-verified Thai name silently plots on the neutral layer. `stage_db.py --set-name/--import-names` then `db2dxf.py` is the
revision path: it re-issues a corrected drawing in ~0.4 s without touching
Overpass or the DEM.

**Sheets are paper space, drawings are model space.** `sheet.py` (imported by
`topo2cad.py` and `db2dxf.py`, like `topo2cad.py` is by `mapposter.py`) builds a
paper-space layout named `SHEET` with a viewport locked to a plot scale.
`fitting_scale()` accounts for the title-block strip, which is why 770 × 410 m
does not plot at 1:2000 on A3 — only A2 has the width. Never let a sheet crop
the extent silently; `add_sheet` warns with the scale that would fit. Render the
sheet with `dxf2pdf.py --layout SHEET`, not the default model-space plot.

**Field-verified names are not staged data.** They live in `verified_names`
(project name + feature id), survive the DELETE that a re-extraction performs on
`staging_buildings`, and are re-applied by `apply_verified()` at the end of
`stage_buildings()`. `create_project()` deliberately *updates* the existing
project row instead of deleting it, so `/project/<id>` links stay valid across
re-runs. Both properties are test-covered; losing either silently destroys field
work, which is the worst failure this tool has.

**Run folders.** `--outdir DIR` on `topo2cad.py` and
`generate_detailed_site_map.py` gives each generation its own timestamped folder
(`DIR/<lat>_<lon>_<extent>[_<profile>]_<stamp>/`) with predictable names inside
(`site.dxf`, `site_map.pdf`, `site_map.png`, `building_inventory.csv`), so repeat
runs never overwrite each other. `serve.py` groups by job id under `output/web/`.

### generate_detailed_site_map.py

Pipeline: validate args → build a rectangular extent in projected metres →
fetch OSM → clip and split into layers → build the building inventory → render →
write PDF/PNG/CSV. Implements a written spec; two layout profiles share one
`_draw_map_body` and differ in page furniture (`render_standard`,
`render_government`).

Invariants worth preserving, all of them load-bearing and test-covered:

- **Label placement is scale-aware.** `metres_per_point()` converts sheet points
  to ground metres; `label_fits()` drops or downgrades a label that cannot
  physically fit its footprint. Collision priority is deliberate: verified names
  outrank B### codes, and codes yield to road names (road names are required by
  the spec, codes are recoverable from the CSV). A label tries several interior
  positions before being dropped. Without this, dense extents become unreadable.
- **Font sizes in the government profile scale by sheet width** (`s` factor).
  They are absolute points against fractional layout positions, so unscaled type
  overruns the border on A4.
- **Codes are assigned by sorted `feature_id`**, so they are stable across runs
  but will shift if OSM adds or removes a building — hence the manual-labels CSV
  keys on `feature_id`, not on the code.
- **A curated labels file is never overwritten**: if `--labels-csv` and
  `--inventory` resolve to the same path, the inventory export is skipped.
- Polygons render through `polygon_patch()` with real interior rings; never
  cover holes with opaque patches, or features underneath disappear.

`serve.py` is stdlib-only on purpose (no deps in its header): it runs the
scripts as subprocesses through `uv run`, keyed by a hash of the request
parameters, and serves results from `output/web/` by job id rather than by
user-supplied path. It also browses and edits the staging database at
`/projects` → `/project/<id>`, where saved names are applied with
`UPDATE staging_buildings` and **Re-issue drawing** shells out to `db2dxf.py`.
A re-issue is registered as a normal job, so it appears in history with the
same download and preview routes.

## Conventions

- Generated output goes to `output/` (gitignored). DEM `.tif` tiles, `ms_cache/`,
  the osmnx `cache/`, and `.venv/` are gitignored too — only scripts, tests, and
  docs are tracked.
- Thai text is expected throughout. Scripts pick a Thai-capable system font
  (Sarabun / Noto Sans Thai / Thonburi) and fall back with a warning; labels use
  white halos for contrast over map geometry.
- Road names are labelled once per unique name, not once per OSM way — a divided
  carriageway is several ways sharing a name, so per-way labelling prints it two
  or four times. All three renderers dedupe; keep it that way when editing.
- Output is for engineering and government submission, so accuracy language
  matters: the site map carries an OpenStreetMap attribution and a data-accuracy
  statement, and marks unsigned sheets `DRAFT / FOR REVIEW` unless `--final`.
