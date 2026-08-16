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

`--arrows` draws direction arrows on one-way carriageways and `--basemap`
puts a tile backdrop under the linework, both opt-in and both **refused on
`--profile government`** — that sheet renders what its spec lists, and a
flag left on from an earlier run must not add a layer to a submission
drawing.

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

## An OpenStreetMap file → CAD

`topo2cad.py` asks Overpass for a box around a coordinate. When you already
have the data as a file — exported from openstreetmap.org, prepared by someone
else, or edited in JOSM and not uploaded yet — `osm2cad.py` draws it with no
network at all:

```bash
# openstreetmap.org → pan to the area → Export → map.osm
uv run scripts/osm2cad.py --input map.osm --outdir output/runs
uv run scripts/osm2cad.py --input map.osm --epsg 32647 --out output/site.dxf
uv run scripts/osm2cad.py --input area.osm.bz2 \
  --bbox 15.8300,104.3900,15.8380,104.3990 --types building,road \
  --layer-by highway --db output/staging.sqlite --project "wat-site"
```

Reads `.osm`/`.xml` plain, gzipped, bzipped, or inside a `.zip`. A `.osm.pbf`
is refused with the command that converts it (`osmium cat -o map.osm
map.osm.pbf`) rather than costing every route here a protobuf dependency.

It shares `topo2cad.py`'s tag rules, NCS layers and label placement — same
buildings, same road split, same bilingual annotation, same crop rectangle —
so a drawing made from a file and one made from a live fetch of the same
ground agree. What it does not do is add Microsoft ML footprints or contours:
the file is the source of truth, and terrain needs a DEM. Use `topo2cad.py`
when the deliverable needs either.

Options worth knowing:

| Option | What it does |
| --- | --- |
| `--types building,road,…` | Import only these feature types (`building`, `road`, `path`, `water`, `green`, `rail`, `barrier`, `landmark`) |
| `--epsg 32647` | Force a projected CRS instead of deriving the UTM zone from the data |
| `--bbox S,W,N,E` | Crop an extract that covers more than the site — whole features, never trimmed geometry |
| `--layer-by highway` | Split each layer by an OSM tag value: `C-ROAD-CNTR-RESIDENTIAL`, `C-ROAD-CNTR-SERVICE`, … |
| `--no-attributes` | Do not attach the OSM tags to each entity |

With `--db` the import is staged like any other run, so `db2dxf.py` re-issues
it after names are corrected — the two routes produce identical drawings
(`dxfdiff.py` reports IDENTICAL). Naming an existing project **merges**, so
you can bring in one feature type at a time and build up a single drawing:

```bash
uv run scripts/osm2cad.py --input map.osm --types building \
  --db output/staging.sqlite --project site --out output/step1.dxf
uv run scripts/osm2cad.py --input map.osm --types road,path \
  --db output/staging.sqlite --project site --out output/step2.dxf
uv run scripts/db2dxf.py --db output/staging.sqlite --project site \
  --out output/site.dxf          # both imports, one drawing
```

`--replace` clears the project instead. Re-running `topo2cad.py` on a
coordinate always replaces — that is a refresh of the same site, not an
addition to it.

### Source attributes

Every drawn entity carries its OSM tags as **extended data** (XDATA under the
application id `OSM`), on both CAD routes — select a building in AutoCAD, run
`LIST`, and the tags it was drawn from are there. The same rows are written to
`attributes.csv` beside the drawing, one row per (feature, tag):

```
feature_id,feature_type,cad_layer,display_name,key,value
way/1076374377,building,C-BLDG-OUTL,B004,addr:district,ปทุมวัน
way/1076374377,building,C-BLDG-OUTL,B004,building,retail
```

The CSV is the complete record — XDATA stops at 40 tags per entity — and the
web app renders it as a browsable grid. The tags are staged as well, so a
`db2dxf.py` re-issue re-attaches the same XDATA and rewrites the same table
rather than handing back a drawing stripped of its source data.
`--no-attributes` turns all of it off.

`gis2cad.py` does the same for your own files: a shapefile's DBF columns, a
GeoJSON's properties, land on the entity under the application id **`GIS`**
rather than `OSM`. A project holding both an extraction and a survey
re-issues with each feature under its own id, so nothing pretends a surveyed
plot came from OpenStreetMap.

## Background map under the linework

Bare linework gives a reviewer nothing to orient by. `--basemap` fetches the
map tiles covering the extent, reprojects them into the drawing's UTM CRS and
places them beneath everything else:

```bash
uv run scripts/osm2cad.py --input map.osm --basemap osm --out output/site.dxf
uv run scripts/topo2cad.py --lat 15.83384548 --lon 104.39445555 \
  --dem dem/dem_n15_e104.tif --basemap esri-imagery --outdir output/runs
uv run scripts/basemap.py --bbox 15.830,104.390,15.838,104.399 \
  --epsg 32648 --out output/basemap.tif      # the GeoTIFF on its own
```

Providers — `osm`, `opentopomap` (contours + hillshade, the closest thing to
a topographic sheet when the drawing has no contours of its own),
`esri-topo`, `esri-imagery`, `esri-street`, `carto-light`, `carto-dark`,
`carto-voyager`, `osm-hot`, `cyclosm` — or your own
`https://…/{z}/{x}/{y}.png` template for a WMTS service or an agency's tile
server. Each carries its provider's required attribution, and each declares
its own maximum zoom (OpenTopoMap renders to 17, so the fetch stops there). It lands as `basemap.tif` beside the drawing on layer `C-ANNO-BMAP`,
faded, with the provider's attribution on the same layer — freeze the layer
and the credit goes with the map it credits.

**Keep the pair together.** A DXF stores a *path* to a raster, not its
pixels, so the `.tif` has to travel with the `.dxf`; the web app offers it as
its own download for that reason. It is a backdrop, not survey data: nothing
is traced from it and nothing is staged, so a `db2dxf.py` re-issue draws the
linework alone — the same way `--underlay` already behaves.

**Tile servers are somebody else's infrastructure.** Every tile is cached in
`cache/tiles/` and reused across runs, the fetch is sequential with a real
User-Agent, and the tile count is capped (`--basemap-max-tiles`, default 128)
with the zoom stepped down until the extent fits. A 1,000 × 750 m site is
about 42 tiles at zoom 18. Do not point it at a province.

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

## Many sources, one drawing

Each converter here reads one kind of source. `compose.py` runs them in
order against a single staging project and issues one DXF from all of it:

```bash
uv run scripts/compose.py --lat 13.7455 --lon 100.5325 \
  --width 500 --height 400 --dem dem/dem_n13_e100.tif \
  --overture --basemap carto \
  --add survey/boundary.geojson --add extract/soi.osm \
  --outdir output/runs --sheet A3
```

The order is load-bearing: the OpenStreetMap step **replaces** what is
staged for the project (re-running a coordinate must not leave last run's
features behind) and every `--add` after it **merges**. `--no-osm` builds
from your own files alone. Every import is handed the CRS the project is
already staged in, so a survey file whose centroid falls the other side of
102°E cannot quietly land in UTM zone 48 inside a zone 47 drawing.

Each run prints — and writes to `sources.csv` — exactly what the drawing is
made of:

```
Sources in site.dxf:
  openstreetmap                   327   202 road, 56 point, 46 building, 23 context
  openstreetmap:soi.osm           147   67 road, 41 point, 23 building, 16 context
  copernicus_dem                   38   25 spot height, 13 contour
  overture                         29   29 point
  microsoft_ml                     21   21 building
  user_gis:boundary.geojson         2   1 building, 1 road
```

The submission sheet can carry the parcel too — the survey you imported,
over the OSM base, on either profile:

```bash
uv run scripts/generate_detailed_site_map.py --lat 13.7455 --lon 100.5325 \
  --width 500 --height 400 --profile government \
  --overlay-db output/runs/<run>/staging.sqlite --overlay-project <name>
```

Only the features you supplied are drawn (this stack fetches OSM itself),
and they are named three times over: heavier linework than anything OSM
contributes, a legend key, and the filename under DATA & ACCURACY.

The web app follows the same rules: uploading a file into an existing
project stages it in **that project's CRS** (an EPSG typed into the form
still wins), `/project/<id>` shows the same source table, and the DXF you
download after an import is the **combined** drawing — the uploaded file's
own drawing is offered beside it as `import.dxf`.

The sheet's title block credits every source that supplied a line, not a
fixed OpenStreetMap notice — and both CAD routes derive it the same way, so
a re-issue comes back with the same block.

The source names the *file*, not just the converter: a project can hold two
surveys and three extracts, and "user_gis" for all of them would not be a
provenance record. A combined drawing without one is not combined, it is
mixed — and a reviewer asking where a boundary line came from deserves a
better answer than "GIS".

### Corner coordinates for a parcel

A boundary is only useful with its numbers. `--corners` marks and labels
every corner and writes the setting-out table beside the drawing:

```bash
uv run scripts/gis2cad.py --input survey/boundary.geojson --corners
```

```
parcel,corner,easting,northing,bearing,distance_m
แปลงที่ดิน A,A1,665532.293,1520028.31,089°38'10",86.516
แปลงที่ดิน A,A2,665618.808,1520028.859,359°38'09",66.378
```

Bearings are grid bearings, north-based and clockwise — so a rectangle
drawn in WGS 84 tables as 089°38′ rather than 090°00′ at Bangkok. That is
meridian convergence, and it is what the drawing is in. `db2dxf.py
--corners` writes the same table for parcels staged in a project.

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

**`/import` takes a file in the browser** — either an OpenStreetMap export
(`.osm`, `.xml`, `.gz`, `.bz2`) or your own GIS data (GeoJSON, GeoPackage,
KML, GML, or a `.zip` holding a shapefile set, the form a survey office
usually sends). The converter is picked from the extension; an OSM upload also
offers the feature types to import, a coordinate system, a crop box, the tag
to split layers by, a background map, and the same switches the CLI has —
every amenity instead of the curated landmarks, named buildings only,
monochrome, and replace-instead-of-merge. Name an existing project and the upload merges into
it, so an export and a survey share one drawing. Useful in the field over the
tunnel, where the site has nothing mapped — or where Overpass is unreachable
and someone can send you the extract.

CAD exports are staged automatically, so `/projects` lists every site you have
generated. Open one to see its roads and all its building footprints, type
verified names over the `B###` codes, and hit **Re-issue drawing** — the DXF is
rebuilt from the database in about two seconds without re-fetching from
OpenStreetMap, and lands in your download list like any other run. Point the
app at a different staging database with `--db`.

Stdlib only — it shells out to the scripts through `uv run`, so each carries its
own dependencies.

Each result page also offers the whole run as one zip. It keeps the files
under the names the drawing expects — `basemap.tif` in particular, which
the DXF references relative to itself — so the package extracts to a
drawing that still finds its backdrop.

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

The default is **200 × 150 m**, which plots at 1:1000 on A3 — a site plan.
Widen it for context and the scale drops accordingly (1000 × 750 lands on
1:5000, a locality map).

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

## When Overpass is down

Every Overpass response is cached under `cache/overpass/` for a day, keyed
on the query. A repeat run at the same coordinate skips the network
entirely, and if every endpoint is failing — they do — the run falls back
to an expired copy and says how old it is rather than dying:

```
WARNING: Overpass is unreachable — drawing from a cached response
2.3 h old (1462 elements). Re-run when it is back for current data.
```

`--refresh-osm` ignores the cache. `dxfaudit.py` never reads it: an audit
that checks a drawing against the snapshot it was made from proves only
that the file matches itself.

## Checking the data quality before you draw

OpenStreetMap is traced by people; Microsoft's footprints are predicted from
imagery. Two independent sources see the same ground, so where they disagree
something is worth a look:

```bash
uv run scripts/gisqa.py --lat 15.83384548 --lon 104.39445555 \
  --width 500 --height 400 --out output/gis_quality.csv
```

It flags OSM buildings the ML layer sees nothing at, outlines the two
sources disagree about, near-duplicate footprints (`building` and
`building:part` on one structure), slivers and self-intersecting rings — and
counts how much of your drawing is modelled rather than surveyed. It
**reports, it does not repair**: an auto-corrected outline carries metres of
boundary error and looks exactly as authoritative in a DXF as a surveyed
one.

Read `poor_overlap` as "worth an eye", not "wrong": ML traces roofs and OSM
outlines are drawn at the wall, so in a dense city with overhangs the two
differ systematically. At Pathum Wan that is 23 of 56 buildings; on rural
ground at Yasothon the two sources agree everywhere.

## A second source of names: Overture Maps

OpenStreetMap is one community's view of a site. [Overture
Maps](https://overturemaps.org) publishes a conflation of several — Meta,
Microsoft, Esri, PinMeTo and OSM itself — and scores each place, so it
carries names OSM never had:

```bash
uv run scripts/overture.py --lat 13.7455 --lon 100.5325 \
  --width 500 --height 400            # look first
uv run scripts/topo2cad.py --lat 13.7455 --lon 100.5325 \
  --width 500 --height 400 --dem dem/dem_n13_e100.tif --overture
```

They land on `C-ANNO-OVTR` with their labels on `C-ANNO-OVTR-TH` /
`C-ANNO-OVTR-EN`, deliberately away from the OSM annotation layers: a name
nobody here can trace to a survey or to OSM has to be separable in one
click, and each entity carries its dataset and confidence as XDATA under the
application id `OVERTURE` (select it in AutoCAD and `LIST`). Anything OSM
already names at the same spot is dropped — the drawing must not carry one
place twice under two sources.

**Curated, or it is a mall directory.** The 500 × 400 m box at Siam Square
holds 1,797 places above the fetch floor; at confidence 0.9 and the landmark
filter that is 29 — museums, schools, a hospital, the government convention
bureau — where the rest is 22 japanese\_restaurant, 20 clothing\_store and
13 jewelry\_store. `--overture-confidence` moves the floor and
`--all-places` keeps every category.

On rural ground this is the whole argument for it: at 14.8165, 100.5116 OSM
has **no** landmark points at all, and Overture supplies สถานีตำรวจภูธร,
สำนักงานสาธารณสุขอำเภอ and the village kindergarten — which is exactly what
an officer locates that parcel by. It is on by default in the web app, and
the plottable sheet lists it in the legend under its own source name.

The query reads Overture's public S3 parquet through DuckDB and takes about
20–60 s, so each extent is cached under `cache/overture/` — keyed on the
extent and release but **not** on the confidence floor, so trying 0.9 then
0.8 costs one query, not two. Overture's buildings theme was measured at 4½
minutes for the same box and is not used: Microsoft's quadkey tiles already
supply footprints.

## Checking a drawing before you issue it

Two different questions, two tools:

```bash
uv run scripts/dxfdiff.py a.dxf b.dxf                      # do the routes agree?
uv run scripts/dxfaudit.py output/site.dxf \
  --db output/staging.sqlite --project 1                   # is it complete?
uv run scripts/dxfaudit.py output/site.dxf --osm-file map.osm   # same, from a file
```

`dxfdiff` compares the extraction route against a `db2dxf.py` re-issue —
entity counts, layer table, and label positions to the millimetre. It proves
the two agree, **not** that either is right: it has reported IDENTICAL while
both routes dropped the same courtyards.

`dxfaudit` asks the other question — does the drawing contain what the source
had? It re-queries Overpass for the extent, or reads the `.osm` export the
drawing came from, and counts buildings, courtyards, landmarks and one-way
roads against what was actually drawn. Exit status 0 means complete, 1 means
a shortfall, so it works as a pre-submission gate.

## B&W poster-style map (PNG + PDF)

```bash
uv run scripts/mapposter.py --lat 14.8165 --lon 100.5116 --radius 150 \
  --dem dem/dem_n14_e100.tif --out output/poster_150m.png --title "ผังบริเวณ / SITE MAP"
```

Black-and-white print map: buildings filled black, road widths by class,
contours light gray, Thai labels (Sarabun/Noto Sans Thai), north arrow,
GPS pin, scale bar, and frame. Writes both .png (300 dpi) and .pdf.

`--arrows` adds direction arrows on one-way carriageways, placed by the same
`stage_db.arrow_positions()` the CAD writers call, so a poster and the drawing
of the same site put them in the same places. `--basemap` puts a map backdrop
under everything — greyscaled under `--style bw`, where a colour map would
fight the linework — with the provider's credit printed under the frame. Both
are opt-in: a poster is a denser medium than a drawing.

## Plottable sheet (paper space + title block, legend, scale bar)

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

Layer names follow the NCS/AIA convention (discipline–major–minor), so the
DXF drops straight into an engineering drawing set.

| Layer | Content |
|---|---|
| `C-BLDG-OUTL` | Building footprints, closed polylines; courtyards as separate inner rings |
| `C-ROAD-CNTR` | Road centrelines, CENTER linetype |
| `C-ROAD-EDGE` | Both edges of pavement, offset by carriageway class |
| `C-ROAD-PATH` | Footways, cycleways, steps — one line, no kerbs |
| `C-ROAD-PLAZ` | Pedestrian areas and plazas, drawn closed as surface |
| `C-ROAD-ARRW` | One-way direction arrows, from the OSM `oneway` tag |
| `C-ROAD-BRDG` / `C-ROAD-TUNL` | Bridges; tunnels, HIDDEN — under the ground the plan describes |
| `C-ROAD-ROWY` | Empty, PHANTOM — for a drafter to draw the legal right-of-way |
| `C-TOPO-MAJR` / `C-TOPO-MINR` | Contours as 3D polylines at true elevation; every 5th is an index contour, labelled |
| `C-HYDR-WATR` / `C-LAND-VEGT` | Canals with flow arrows, ponds; parks, farmland, cemeteries |
| `C-RAIL-TRAK` / `C-BNDY-BARR` | Railways; walls, fences, and gates as access-point symbols |
| `C-ANNO-SYMB` / `C-SITE-POI` | Landmark point symbols; landmark grounds with no building tag |
| `C-UTIL-POWR` / `C-UTIL-PIPE` | Power lines with their pylons and poles; pipelines |
| `C-UTIL-LAMP` | Street lamps |
| `C-LAND-TREE` | Individual trees (`natural=tree`), drawn as their own symbol |
| `C-ANNO-ADDR` | House numbers (`addr:housenumber`) and storeys (`3F`), under the building label |
| `C-LAND-ZONE` | Built-up land use — residential, commercial, industrial |
| `C-SITE-PARK` | Parking areas, drawn whatever the landmark filter says |
| `C-TOPO-SPOT` | Spot heights sampled from the DEM on a 5 × 5 grid |
| `C-ANNO-GRID` | UTM coordinate grid (`--grid`), crosses on round eastings and northings |
| `C-ANNO-DIMS` | Extent dimensions as real DIMENSION entities |
| `C-MISC-OTHR` / `C-MISC-SYMB` | `--all-features`: everything no other rule claimed |
| `C-ANNO-TEXT` | Language-neutral text: B### codes, contour elevations, the GPS tag |
| `C-ANNO-TEXT-TH` / `C-ANNO-TEXT-EN` | Thai and Latin labels — freeze one to plot a single-language sheet |
| `C-ANNO-OVTR` (`-TH` / `-EN`) | `--overture`: named places from Overture Maps, with their labels — freeze `C-ANNO-OVTR*` and the drawing is back to what OSM says |
| `C-ANNO-EXTN` | The requested extent, DASHED. A crop line, not a clip: linework runs ~55 m past it and footprints are never cut |
| `C-ANNO-NORT` / `C-ANNO-GPSP` | North arrow block; circle and label at the input coordinate |
| `C-PROP-LINE` / `C-PROP-SETB` | Empty, ready for parcel boundaries and setbacks (OSM has no source for either) |
| `C-ANNO-BMAP` / `C-SITE-ORTH` | `--basemap` backdrop with its attribution; `--underlay` imagery you own |

Carriageway width comes from the OSM `width` or `lanes` tags where a mapper
supplied them (`width=4` draws 4 m, not the 6 m the class would guess);
`ROAD_WIDTH_M` is the fallback. `--hatch` fills water and vegetation with the
CAD patterns a drafter expects, and `--no-spots` turns off the levels.
`--all-features` draws everything OpenStreetMap has in the extent rather
than the curated tag list — at Pathum Wan that is 243 extra points and 10
extra lines (benches, shops, entrances, crossings, bus stops) which the
default discards, and the run reports what it added by tag.
`--grid` adds the UTM coordinate grid a survey sheet carries, and
`--contour-interval 0.5` forces an interval where a deliverable specifies
one instead of letting the DEM's range choose.

Coordinates are metres in the UTM zone **derived from the coordinate** —
EPSG:32647 west of 102°E, EPSG:32648 east of it. Nothing is hardcoded:
Thailand spans two zones, and forcing a site at 104.4°E into 47N lands it
outside the zone's valid range with +0.37% scale error, 3.75 m per kilometre.

Microsoft Global ML footprints supplement OSM **everywhere**, not only where
OSM is sparse — at Pathum Wan that adds 69 buildings OSM has nothing for.
Duplicates are dropped by overlap against the smaller of the two footprints;
the inventory CSV records the source of each. `--no-ml` opts out. Tiles are
cached in `dem/ms_cache/`.

Notes: OSM coverage varies — rural Thai areas often lack building footprints
entirely, which is what `gis2cad.py` and `--underlay` are for. The Copernicus
DEM is a ~30 m surface model (it includes vegetation and buildings), fine for
site context but not survey-grade.
