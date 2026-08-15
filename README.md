# topo2cad

Generate a CAD file (DXF) around a GPS point: topographic contours (Copernicus 30m DEM)
plus OpenStreetMap roads, buildings, and named places, in UTM 47N meters.

## Usage

Requires only [uv](https://docs.astral.sh/uv/) — dependencies install automatically.

```bash
# 1. Download the DEM tile covering your latitude/longitude (once per 1°x1° tile).
#    Tile naming: N<lat>_00_E<lon>_00 (integer degrees, floor of your coordinate).
#    Tiles N13 and N14 (Bangkok / Lopburi) are already in dem/.
curl -o dem/dem_n14_e100.tif "https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N14_00_E100_00_DEM/Copernicus_DSM_COG_10_N14_00_E100_00_DEM.tif"

# 2. Generate the DXF.
uv run scripts/topo2cad.py \
  --lat 14.8164876968956 --lon 100.511644184589 \
  --radius 1000 \
  --dem dem/dem_n14_e100.tif \
  --out output/topo_gps_1km.dxf

# Or give each run its own timestamped folder instead of naming the file:
uv run scripts/topo2cad.py --lat 15.83384548 --lon 104.39445555 \
  --width 500 --height 400 --dem dem/dem_n15_e104.tif --outdir output/runs
```

The UTM zone is derived from the coordinate, so sites east of 102°E use
EPSG:32648 automatically — forcing them into 47N would introduce a +0.37%
scale error. `--outdir` also works on `generate_detailed_site_map.py`.

## Detailed site map

Print-ready vector PDF with classified roads, building footprints labeled by
name or B### code, water, legend, attribution — plus a building inventory CSV
for the manual name-correction workflow:

```bash
# 1. Generate map + inventory
uv run scripts/generate_detailed_site_map.py \
  --lat 14.8164876968956 --lon 100.511644184589 --width 500 --height 250 \
  --output output/site_map_detailed.pdf --png output/site_map_detailed.png \
  --inventory output/building_inventory.csv

# 2. Edit display_name values in the inventory CSV (field-verified names)

# 3. Regenerate with verified names
uv run scripts/generate_detailed_site_map.py \
  --lat 14.8164876968956 --lon 100.511644184589 --width 500 --height 250 \
  --labels-csv output/building_inventory.csv \
  --inventory output/building_inventory_v2.csv \
  --output output/site_map_named.pdf
```

The UTM zone is chosen from the coordinate, so sites east of 102°E get
EPSG:32648 automatically — no flag needed. Your curated labels file is never
overwritten: pass a different `--inventory` path to export an updated copy, and
overrides whose `feature_id` no longer matches are reported rather than dropped
silently.

Options: `--no-building-codes` hides B### labels, `--font path.ttf` supplies a
custom Thai-capable font, `--title` sets the map title, `--sheet-size A4|A3|A2|A1`
picks the sheet (portrait is chosen automatically when the extent is taller than
it is wide).

### Thai government submission sheet

```bash
uv run scripts/generate_detailed_site_map.py \
  --lat 15.83384548 --lon 104.39445555 --width 500 --height 400 \
  --profile government --sheet-size A3 \
  --project-name "ชื่อโครงการ" --site-location "ที่ตั้ง" \
  --subdistrict "ตำบล" --district "อำเภอ" --province "จังหวัด" \
  --agency "หน่วยงาน" --prepared-by "ผู้จัดทำ" --drawing-no "SM-001" \
  --output output/gov_site_map.pdf
```

Formal layout: sheet border, bilingual title (แผนผังแสดงที่ตั้งโครงการ /
PROJECT LOCATION AND SITE MAP), project and administrative fields, geographic
plus computed UTM coordinates, legend, certification block with stamp space, and
the data-accuracy statement. Marked `DRAFT / FOR REVIEW` until you pass
`--final`. If the receiving agency issues its own mandatory form, that form wins.

## Your own GIS data → CAD

OpenStreetMap and the ML footprint layer have nothing in many places — new
plots, plantations, land your team surveyed. Feed your own files in instead:

```bash
uv run scripts/gis2cad.py --input plots.geojson --input access.geojson \
  --out output/site.dxf
uv run scripts/gis2cad.py --input survey.shp --name-field PLOT_NAME \
  --layer C-PROP-LINE --out output/parcels.dxf
```

Add `--db` to merge your data into a staged project so one drawing carries
both — the OSM roads and terrain the site does have, plus the plots only your
survey knows about:

```bash
uv run scripts/topo2cad.py --lat 12.526 --lon 102.15982 \
  --dem dem/dem_n12_e102.tif --db output/staging.sqlite --project "site-a" \
  --out output/site-a.dxf
uv run scripts/gis2cad.py --input solar_plots.geojson --input access.geojson \
  --db output/staging.sqlite --project "site-a" --out output/survey.dxf
uv run scripts/db2dxf.py --db output/staging.sqlite --project 2 \
  --out output/combined.dxf     # roads + contours + your plots, one sheet
```

Naming an existing project merges; re-running `topo2cad.py` on the same name
replaces its extraction. Reads GeoJSON, Shapefile, GeoPackage, KML and GML. The UTM zone is derived
from the data, so files arrive in metres whatever CRS they were saved in — a
GeoPackage stored in 47N for a 48N site is reprojected, not trusted. Geometry
type picks the default layer (polygons → `C-BLDG-OUTL`, lines →
`C-ROAD-CNTR` plus carriageway edges, points → `C-ANNO-SYMB`); `--layer`
overrides per input. Labels come from a `name`-like attribute, or name one
with `--name-field`.

## SQLite staging layer

Stage extracted features with their CAD label anchors already computed, so the
drawing step is plain `SELECT`s:

```bash
uv run scripts/topo2cad.py --lat 15.83384548 --lon 104.39445555 \
  --dem dem/dem_n15_e104.tif --outdir output/runs \
  --db output/staging.sqlite --project "yasothon-solar-site"

uv run scripts/stage_db.py --db output/staging.sqlite --info
uv run scripts/stage_db.py --db output/staging.sqlite --labels 1
```

Tables: `projects`, `staging_buildings`, `staging_roads`, `staging_contours`,
plus a `cad_labels` view that resolves every annotation object (text, insertion
point, rotation, layer) in one query. Geometry is WKB in the project's UTM
metres.

### Re-issue a drawing without re-fetching

Correct names in the database, then redraw straight from it — no Overpass, no
DEM, about a third of a second instead of ~20 s:

```bash
uv run scripts/stage_db.py --db output/staging.sqlite --project 1 \
  --set-name "ms/00042=ศาลาประชาคมบ้านหนองแวง"
uv run scripts/stage_db.py --db output/staging.sqlite --project 1 \
  --import-names output/runs/<run>/building_inventory.csv   # bulk, from field work
uv run scripts/db2dxf.py --db output/staging.sqlite --project 1 \
  --out output/revised.dxf
```

`db2dxf.py` produces an entity-for-entity identical drawing to the extraction
path (239 outlines, 21 centrelines, 42 edges, 9 contours, 253 MTEXT on the test
site), so a revision is a redraw, not a re-survey. `--import-names` ignores
`B###` placeholders, so only verified names land on the sheet.

Four things differ from a textbook PostGIS staging schema, each because the
real data demands it:

| Choice | Why |
|---|---|
| SRID stored **per project** | Thailand spans 47N and 48N; a fixed 32647 puts a 104.4°E site 1,078 km off-zone with +0.37% scale error |
| `osm_id` **nullable** + `source` column | Microsoft ML footprints have no OSM id, and they are 238 of 239 buildings at the test site |
| Label anchor = **interior point**, not centroid | A centroid falls outside concave footprints — 3 of 104 buildings in a dense Bangkok extent |
| **Rotation precomputed**, road names deduped in the view | Otherwise the CAD step recomputes azimuths, and a divided carriageway prints its name four times |

## Web interface

```bash
uv run scripts/serve.py                  # http://127.0.0.1:8765
uv run scripts/serve.py --port 9000
```

Paste a coordinate, set the area, choose the export (CAD + site map, CAD only, or
site map only) and download the DXF, its A3 plot preview, the site map PDF/PNG,
and the inventory CSV. Each run gets its own folder under `output/web/`. The
elevation tile the CAD export needs is downloaded automatically the first time
you use a new 1°×1° area (~40 MB).

The form carries the same sheet controls as the CLI: pick a **CAD sheet** paper
size and a **plot scale** (default "fit the extent"), and the DXF comes with the
titled paper-space layout, with its plot preview rendered from that layout
rather than raw model space. Asking for a scale that would crop shows the
warning right in the result. The project page offers the same two controls on
**Re-issue drawing**. Its building table is searchable (by code, name or
feature id), filterable to "needs a name" or "already named", and paged 100 at
a time, so a dense site with thousands of footprints stays workable.

**Verified names survive re-extraction.** A name you confirm is kept in a
`verified_names` table keyed by project and feature id, separate from the
staged geometry. Re-running an OSM pull replaces the features and then re-applies
those names, reporting how many it restored — and the project keeps its id, so
`/project/<id>` links stay valid.

**`/import` takes your own GIS files in the browser** — GeoJSON, GeoPackage,
KML, GML, or a `.zip` holding a shapefile set (the form a survey office
usually sends). Name an existing project and the upload merges into it, so your
plots share a drawing with the OSM roads and terrain. Useful in the field over
the tunnel, where the site has nothing mapped.

CAD exports are staged automatically, so `/projects` lists every site you have
generated. Open one to see its roads and all its building footprints, type
verified names over the `B###` codes, and hit **Re-issue drawing** — the DXF is
rebuilt from the database in about two seconds without re-fetching from
OpenStreetMap, and lands in your download list like any other run. Point the
app at a different staging database with `--db`.

Stdlib only — it shells out to the scripts through `uv run`, so each carries its
own dependencies.

### Sharing it over ngrok

```bash
./scripts/tunnel.sh          # starts the app if needed, prints the public URL
```

`ngrok.yml` defines the tunnel (port 8765, https); the authtoken stays in
ngrok's own global config and never lands in the repo. The file is gitignored.

The tunnel is open — there is no login on the tunnel or in the app, so anyone
with the link can generate maps, edit staged building names, and download the
outputs. Stop it with Ctrl-C when you are done. To require a password, add to
`ngrok.yml` under the tunnel:

```yaml
    basic_auth:
      - "maps2cad:choose-a-strong-password"
``` Binds to localhost by default; it fetches from Overpass and
writes files, so don't expose it to an untrusted network.

### Choosing width × height

The printed scale follows from the extent and the sheet. On A3 the standard
profile's map frame is 387 × 206 mm (so a 1.87:1 extent fills it) while the
government profile's frame is nearly square at 265 × 255 mm (1.04:1). Extents
that land on a round scale:

| Scale | Standard A3 | Government A3 |
|---|---|---|
| 1:500 | 190 × 100 m | 130 × 130 m |
| 1:1000 | 390 × 210 m | 260 × 260 m |
| 1:1250 | 480 × 260 m | 330 × 320 m |
| 1:2000 | 770 × 410 m | 530 × 510 m |

On the government sheet a wide extent wastes vertical space: 500 × 250 and
500 × 400 both print at 1:1889, so the taller one is free coverage.

**770 × 410 m is the default** for `topo2cad.py`, `generate_detailed_site_map.py`
and the web form — it fills the A3 standard frame at exactly 1:2000. Pass
`--radius` to `topo2cad.py` for a square box instead.

## Tests

```bash
uv run --with pytest --with pillow python -m pytest tests/ -q   # offline
RUN_NETWORK_TESTS=1 uv run --with pytest --with pillow python -m pytest tests/ -q
```

Covers UTM zone selection, extent geometry, road classification, label fitting
and collision rules, inventory determinism, and CSV validation. The network test
is opt-in so the suite doesn't hit Overpass on every run.

## B&W poster-style map (PNG + PDF)

```bash
uv run scripts/mapposter.py --lat 14.8165 --lon 100.5116 --radius 150 \
  --dem dem/dem_n14_e100.tif --out output/poster_150m.png --title "ผังบริเวณ / SITE MAP"
```

Black-and-white print map: buildings filled black, road widths by class,
contours light gray, Thai labels (Sarabun/Noto Sans Thai), north arrow,
GPS pin, scale bar, and frame. Writes both .png (300 dpi) and .pdf.

## Plottable sheet (paper space + title block)

Model space holds the survey in real metres. `--sheet` adds the sheet you
actually plot — a paper-space layout with a border, a viewport locked to a
scale, and a bilingual title block carrying the project identity, WGS 84 and
UTM coordinates, CRS, extent, drawing/sheet/revision/date and signature boxes:

```bash
uv run scripts/topo2cad.py --lat 15.83384548 --lon 104.39445555 \
  --dem dem/dem_n15_e104.tif --sheet A2 --scale 2000 --out output/site.dxf
uv run scripts/db2dxf.py --db output/staging.sqlite --project 1 \
  --sheet A3 --out output/revised.dxf          # --scale defaults to 'fit'
uv run scripts/dxf2pdf.py output/site.dxf --layout SHEET --size A2
```

`--scale fit` (the default) picks the largest round scale that shows the whole
extent on that sheet. Naming a scale that would crop the site prints exactly
what gets lost and what to use instead:

```
WARNING: 770 × 410 m does not fit A3 at 1:2,000 — the viewport shows
580 × 546 m and the rest is cropped. Use --scale 5000 on A3, or a larger sheet.
```

The usable width is the sheet minus the title block, which is why the default
770 × 410 m extent needs **A2** to plot at a true 1:2000.

## Convert DXF to PDF

```bash
uv run scripts/dxf2pdf.py output/topo_gps_1km.dxf            # A3, black linework
uv run scripts/dxf2pdf.py output/topo_gps_1km.dxf --color    # keep layer colors
uv run scripts/dxf2pdf.py output/topo_gps_1km.dxf --size A1 --dpi 600 -o print.pdf
```

Paper sizes: A4–A0 (landscape). Default is A3 at 300 dpi with all-black linework
for clean printing; `--color` keeps the CAD layer colors on a white background.

## Output layers

| Layer | Content |
|---|---|
| CONTOURS / CONTOUR_LABELS | Contour lines as 3D polylines at true elevation, auto interval (~10 levels) |
| BUILDINGS / BUILDING_NAMES | OSM building footprints (closed polylines) + name labels |
| ROADS / ROAD_NAMES | OSM roads clipped to the area + name labels |
| POI / POI_NAMES | Named OSM point features (temples, shops, schools...) |
| CENTER | Circle + label at the input GPS point |

Coordinates are meters in EPSG:32647 (WGS84 / UTM zone 47N — correct for Thailand
between 96°E and 102°E; use zone 48N / EPSG:32648 east of 102°E).

When OSM has fewer than 20 buildings in the area, the script automatically
supplements with Microsoft Global ML Building Footprints (AI-detected from
satellite imagery, unnamed outlines). Tiles are cached in dem/ms_cache/.

Notes: OSM coverage varies — rural Thai areas often lack building footprints.
The Copernicus DEM is a ~30m surface model (includes vegetation/buildings), fine for
site context but not survey-grade.
