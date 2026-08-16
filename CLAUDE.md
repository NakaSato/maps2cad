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
uv run scripts/osm2cad.py --input map.osm --outdir output/runs  # .osm, no net
uv run scripts/basemap.py --bbox 15.83,104.39,15.84,104.40 \
  --epsg 32648 --out output/basemap.tif    # or --basemap on either CAD tool
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
   fetch_ms_buildings, best_name, utm_transformer`) and by `osm2cad.py`
   (the tag rules, layers, text styles and `stage_to_db`) — despite being a
   script.
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

**Four ways in, one drawing convention.** `topo2cad.py` (OSM + DEM),
`db2dxf.py` (staging database), `osm2cad.py` (an OSM *file*) and
`gis2cad.py` (user-supplied GeoJSON/SHP/GPKG/KML) all emit the same NCS
layers and MTEXT conventions. `gis2cad.py`
exists because large areas have no OSM or ML data at all — at 12.526,
102.15982 the nearest ML footprint is 3.19 km away, so an empty drawing there
is correct, not a bug. Check with a coverage query before hunting for one.

**`osm2cad.py` is the same stack with a different front door.** It reads an
.osm export (plain, .gz, .bz2 or inside a .zip) and hands the elements to
`topo2cad.classify_elements()` — the tag rules were lifted out of
`topo2cad.main()` into that function precisely so the file route and the
Overpass route cannot drift; change a tag rule there and both move. What a
raw .osm file needs and Overpass does not is geometry stitching: a way
carries node *references*, so coordinates are joined back on in
`parse_osm()`, and an untagged way is kept as material for the multipolygon
relations that use it rather than drawn (drawing them too doubles every
courtyard wall). Ways whose nodes fall outside the extract are counted and
skipped, never drawn short. It deliberately does **not** supplement with ML
footprints or draw contours: the file is the source of truth, and terrain
needs a DEM — use `topo2cad.py` when the deliverable needs either. `--db`
staging goes through `topo2cad.stage_to_db()`, so a re-issue via
`db2dxf.py` is byte-identical (`dxfdiff.py` reports IDENTICAL); the GPS tag
is written unformatted, like `topo2cad.py`, because db2dxf prints the
staged REAL back with `str()` and a `:.6f` here made the one non-geometry
label differ. Two options exist because an import dialog is expected to
offer them: `--types` filters feature types (roads split from paths on
`PATH_TYPES`), and `--layer-by TAG` suffixes the NCS layer with a tag value
(`C-ROAD-CNTR-RESIDENTIAL`) — a suffix, so `C-ROAD-CNTR*` still catches
every split layer. `--layer-by` affects the DXF only; staging keeps the
base layer, because `db2dxf.py` draws from a fixed layer table and would
otherwise re-issue onto a layer that table has no entry for. Every entity
also carries its source OSM tags as XDATA under app id `OSM` (group code
1000, clipped at 255 **bytes** — Thai is three bytes a character, so a
character-count clip still overruns); `--no-attributes` opts out. A
`.osm.pbf` is refused with `osmium cat -o map.osm map.osm.pbf` rather than
adding a protobuf dependency to a repo where nothing else needs one.

**A background map is a backdrop, not survey data.** `basemap.py` fetches
slippy-map tiles for the extent, mosaics them in Web Mercator, reprojects
to the drawing's CRS and hands the GeoTIFF to `underlay.py` — which is why
the reprojection is not optional: placing a 3857 image by its corners in a
UTM drawing stretches it by metres through the middle and still looks like
a map while doing it. It lands on `C-ANNO-BMAP`, deliberately not
`C-SITE-ORTH`, so a drafter can drop the fetched backdrop without dropping
imagery they own and traced from. Nothing is staged, so a `db2dxf.py`
re-issue draws linework alone — the same asymmetry `--underlay` already
has. The provider's attribution is written as MTEXT **on the basemap's own
layer**: freezing the backdrop must not leave a drawing crediting a map it
no longer shows. Two constraints are load-bearing. First, tile servers are
someone else's infrastructure: tiles cache to `cache/tiles/` (shared by
every run, not per-run, or a re-plot re-fetches), the fetch is sequential
with a real User-Agent, and `choose_zoom()` steps the zoom *down* until the
tile count fits the cap rather than clipping the extent or hammering the
server. Second, the DXF stores a path, not pixels, so `basemap.tif` must
keep its bare filename — `serve.py`'s download route special-cases it
against the usual `<name>_<lat>_<lon>.ext` renaming, which would otherwise
hand the user a drawing with a missing raster. `deg2tile()` clamps latitude
to ±85.05112878 before the log term, which divides by zero at the poles;
the tile arithmetic (`mosaic_origin`, `tile_range`, `choose_zoom`) is kept
free of rasterio so it stays testable without GDAL. The web form offers only
the named providers — the CLI takes a raw `{z}/{x}/{y}` URL, but accepting
one from a browser form would let anyone point the server's fetcher at any
host.

**ML footprints supplement OSM everywhere, not only where OSM is empty.**
`topo2cad.py` used to fetch them only when OSM returned fewer than 20
buildings; at Pathum Wan that drew 274 OSM buildings while 69 further ML
footprints sat on ground OSM has nothing for. A building missing from a site
plan is a worse error than one whose outline came from a model, and the
inventory CSV records `source` either way. `--no-ml` opts out.
`merge_ml_footprints()` deduplicates: a footprint is dropped when it
overlaps an existing building by more than half of **whichever of the two is
smaller**. Both weaker rules were tried and measured first — a
representative-point-inside test let 12 duplicate pairs through (a modelled
outline and a traced one disagree by a metre or two, so the point lands just
outside), and testing only what fraction of the ML footprint itself is
covered let 11 through (the ML layer merges rows of small buildings into one
blob that covers each entirely while they cover little of it). The
smaller-area rule leaves 0. Note the drawing still carries 7 near-duplicate
pairs at that site from OSM alone — `building` and `building:part` on the
same structure — which is upstream data, not this merge.

`mapposter.py` shares the rule through `new_ml_rings()` and reports the same
counts. `generate_detailed_site_map.py` deliberately adds nothing — its spec
forbids inventing features — so the site map still shows fewer buildings
than the DXF at the same coordinate, and that remains expected, not a bug. `generate_detailed_site_map.py`
deliberately does not — its spec forbids inventing features, so it renders only
what OSM has. On a rural site this is a large difference, not a rounding error:
at 15.8338, 104.3945 the DXF carries 155 buildings while the site map PDF shows
1. Expect the question "why does the PDF have fewer buildings than the CAD file"
and answer with this, not with a bug hunt.

**Default extent is 1000 × 750 m** across the CLI tools and the web form,
with A3 the default sheet. These do not combine at 1:2000: after the title
block and margins `sheet.py` leaves a 290 × 273 mm A3 viewport, which caps
1:2000 at 580 × 546 m, so 1000 × 750 plots at **1:5000 on A3**, 1:2500 on A2
and 1:2000 on A1. All are round scales a reviewer accepts, but the extent
and the sheet are now chosen independently — check `fitting_scale()` for the
combination you need rather than assuming 1:2000. `topo2cad.py` still
accepts `--radius` for a square box.

**The extent is drawn, on `C-ANNO-EXTN`, DASHED.** Both CAD routes close a rectangle
on the requested extent, derived from the centre and the nominal width and
height so they agree to the millimetre. It is a *crop line, not a clip*:
`clip_runs()` deliberately runs linework ~55 m past the boundary
(`margin=0.0005°`) so roads cross the border cleanly, and building
footprints are never cut, so a building straddling the edge stays a whole
footprint. At 14.8165, 100.5116 that is 84 of 609 entities crossing the
line, almost all of it roads. It is dashed so it cannot be mistaken for a
fence, a wall or a property boundary — it is a limit of extent, not
surveyed geometry. A drafter clips the viewport to this rectangle; do not "fix" the overhang by trimming geometry, or the DXF stops
carrying real footprints. `generate_detailed_site_map.py` needs none of
this — matplotlib clips to `set_xlim`/`set_ylim` and the axes spines already
draw the frame.

The previous default was 770 × 410 m, and its comment claimed the same
1:2000-on-A3 property. That was true only of a bare map frame: 770 m needs
385 mm against a 290 mm viewport, so `fitting_scale()` actually returned
1:5000, and the CAD sheet default was A2 to compensate. Do not restore it
without redoing the arithmetic.

**CAD output follows the NCS/AIA layer convention** (`C-BLDG-OUTL`,
`C-ROAD-CNTR`, `C-ROAD-EDGE`, `C-ANNO-TEXT`, …) — see `LAYERS` in
`topo2cad.py`. All annotation is MTEXT anchored Middle Center. Road names are
rotated along the centreline and the route `ref` is a separate MTEXT offset
perpendicular, so name and number never overprint.

**Road layers follow the NCS split, and a footway is not a carriageway.**
`C-ROAD-CNTR` carries centrelines with the **CENTER** linetype (with
`$LTSCALE` 5.0 — the pattern is in drawing units, i.e. metres, so unscaled
dashes are sub-millimetre on paper and read as continuous), `C-ROAD-EDGE`
the two offset edges of pavement, `C-ROAD-PATH` footways, cycleways and
steps as a *single* line with no offset — a 1.5 m path drawn with two kerb
lines reads as a road — and `C-ROAD-ROWY` is created empty with a PHANTOM
linetype for a drafter to draw the legal right-of-way onto, since OSM has no
source for one. `PATH_TYPES` decides the split; a path stages with
`carriageway_m = 0`, which is what tells `db2dxf.py` to skip its edges too.

**One-way direction arrows come from the `oneway` tag, and paths never get
them.** `oneway_dir()` reads yes/true/1 as +1, **-1/reverse as -1** — that
second family means "against the way as digitised", and treating it as
forward aims every arrow on a sliproad at oncoming traffic — with
`junction=roundabout` implying +1 unless `oneway=no` says otherwise. The
arrows are INSERTs of the `ONEWAY_ARROW` block on `C-ROAD-ARRW`, their own
layer so a sheet can plot without traffic direction on it. Placement is
`stage_db.arrow_positions()`, called by all three writers for the same
reason `line_label_anchor()` is: spacing is by distance along the run (60 m,
first at half that), because an OSM way carries a vertex every few metres
through a curve and per-vertex arrows would pile up on bends and vanish on
straights. A run under 12 m gets none; one under a full spacing gets one at
its midpoint. Size is `oneway_arrow_size()`, clamped to 3–10 m so a 14 m
motorway does not get a 14 m arrow. `staging_roads.oneway` carries it (a
`MIGRATIONS` entry, so older databases upgrade on open), and `db2dxf.py`
draws from that column — 39 arrows at Pathum Wan and 94 over 500 × 400 m,
identical in both routes by `dxfdiff.py`. `mapposter.py` calls the same
placement rule behind `--arrows`, so a poster and a drawing of one site
agree; it is opt-in there because a poster is a denser medium.
`generate_detailed_site_map.py` offers `--arrows`/`--basemap` too, and
**refuses both on `--profile government`** rather than ignoring them: that
layout implements a written spec, and a sheet that quietly gained a layer
because a flag was left on from an earlier run is what a reviewing officer
is entitled to reject. Two boundary notes there — it borrows exactly one
thing from the CAD side, `stage_db.arrow_positions()`, because sharing
where the arrows sit is the whole point and it is pure geometry; and it
restates the `oneway` tag reading locally instead of importing
`oneway_dir()`, so the two stacks keep their disjoint dependency sets. A
test asserts the two readings agree. `--basemap` is the one feature that
does pull `rasterio`/`pillow` into that stack, declared in its PEP 723
header: tiles arrive in Web Mercator and must be reprojected, not
corner-stretched.

**Detail added for CAD, and what keeps each half honest.** Four classes
were added to the extraction because a Thai site plan carries them and OSM
maps them: power lines with their pylons and poles (`C-UTIL-POWR`),
pipelines (`C-UTIL-PIPE`), individual trees (`C-LAND-TREE`) and house
numbers (`C-ANNO-ADDR`). The first three are geometry, so they ride the
existing tables — lines in `staging_context` under a new `kind`, pylons and
trees in `staging_pois` with an empty `display_name`, which `cad_labels`
already skips, so an unnamed mark cannot grow a label. House numbers needed
a column (`staging_buildings.addr_house`, a `MIGRATIONS` entry) and a row in
`cad_labels`, which is what lets `db2dxf.py` redraw them at the same offset.
Symbols and their sizes live in `blocks.py` keyed by layer
(`SYMBOL_FOR_LAYER`, `SIZE_FOR_LAYER`) because `db2dxf.py` knows only the
layer a point was staged on — a tree that came back pylon-sized would be a
difference nobody staged.

**Road width is measured where OSM measured it.** `carriageway_width()`
reads `width` (metres, or feet when the value ends `'`/`ft`), then `lanes`
× 3.5 m on the trunk classes and × 3.0 m elsewhere, and only then falls
back to `ROAD_WIDTH_M`. Every `residential` road used to be drawn 6.0 m
whether it was a 4 m soi or an 8 m avenue. A parsed width under a metre is
ignored as a mapping error, and lanes are capped at 40 m. `road_cad_layer()`
splits bridges and tunnels off the carriageway layers — a footbridge stays a
footway, and a way tagged both bridge and tunnel is drawn as a tunnel.

**Spot heights are staged because `db2dxf.py` has no DEM.** `topo2cad.py`
samples the DEM on `stage_db.spot_grid()`'s inset 5 × 5 grid and writes both
the mark and the level; the grid is inset so the numbers do not land on the
crop line. Without `staging_spots` a re-issue would come back with contours
and no levels. The label format is `{:+.1f}` rather than `"+" + value`,
because a point below datum was printing as `+-0.3`. Hatching
(`HATCH_PATTERNS`, `hatch_area`) lives in `stage_db.py` for the same reason
the arrow rule does: `db2dxf.py` fills the same rows and must not import the
Overpass side to learn how. It is opt-in (`--hatch`) — a hatch at 1:5000 on
a dense site is a lot of ink — and only closed runs are filled, since an
open canal centreline has no area.

**A repaired polygon is what gets drawn *and* what gets staged.**
`stage_db.repaired_polygon()` / `polygon_parts()` exist because OSM carries
self-intersecting rings — จุฬาลงกรณ์มหาวิทยาลัย is one — and `buffer(0)`
splits those into two polygons. The extraction routes used to draw the raw
ring while staging the repaired one, so the drawing had one outline where
its re-issue had two, with the label 97 m away in the other lobe;
`dxfdiff.py` caught it only once a real extract contained such a ring. Label
anchors now come from `stage_db.interior_point()` on that same repaired
shape in every route. Note a bow-tie ring repairs to a *single* polygon of
half the original area — that is GEOS, not a bug here, and it is identical
on both sides.

**All annotation carries a background mask** (`set_bg_color("canvas",
scale=1.1)`), so a label crossing a building outline or a road edge cuts
through it instead of overprinting. Note `set_bg_color(None)` *removes* a
mask rather than adding one — passing None is the easy mistake, and it fails
silently because the text still draws. All three writers do this, including
`db2dxf.py`'s contour elevations, north arrow and GPS tag, which sit outside
its label loop and were missed on the first pass.

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

**Source attributes ride with the drawing, and they are staged too.** Every
drawn entity carries its OSM tags as XDATA under the appid `OSM` (select it
in AutoCAD, `LIST`, read the tags), and the same rows are written to
`attributes.csv` beside the drawing — long format, one row per (feature,
tag), because OSM features carry wildly different tag sets and a column per
key would be a sparse sheet hundreds of columns wide. The rules live in
`stage_db.py` (`xdata_tags`, `attribute_rows`, `write_attribute_csv`) because
all three writers apply them; group code 1000 clips at 255 **bytes**, and
Thai is three bytes a character, so the clip is applied after encoding.
XDATA stops at `XDATA_MAX_TAGS` (40) per entity with a `@truncated=` marker —
the CSV carries the full set. `staging_tags` is what lets `db2dxf.py`
re-attach the same XDATA and rewrite the same table: without it, correcting
one name and re-issuing would silently hand back a drawing stripped of its
source data. `--no-attributes` opts out on every route. The table describes
the *drawing*, not the source, so a feature dropped by `--types` or `--bbox`
is absent from both.

The appid says where the data came from: `OSM` for OpenStreetMap tags,
`GIS` for the fields of a file the user supplied (`gis2cad.py` — a
shapefile's DBF columns are the same thing to a drafter). One project can
hold both when a survey is merged into an extraction, so `staging_tags`
carries an `appid` column per row and `db2dxf.py` registers each id it finds
and attaches accordingly; at a mixed site that is 33 entities under OSM and
2 under GIS in one re-issued drawing. Labelling a survey's columns "OSM" in
the CAD attribute browser would be a lie, which is the whole reason for the
column.

**Import merges, extraction replaces.** `stage_to_db(merge=True)` keeps what
is already staged under a project name; `merge=False` clears it first.
`topo2cad.py` replaces, because re-running a coordinate refreshes that site
and last run's features must not linger. `osm2cad.py` and `gis2cad.py`
merge, because an import is how you bring in one feature type at a time —
`--types building`, then `--types road` from the same file — and the
workflow every OSM importer documents ("repeat the import for other feature
types") silently destroyed the first pass when this replaced. `--replace`
opts back into clearing. A merge prints "Merged into" and the project's new
totals, because a `db2dxf.py` re-issue draws everything staged, not just the
import you just ran.

**A new `staging_*` table goes in `STAGED_TABLES` in the same commit.**
`create_project()` clears that list when a project is re-extracted.
`staging_pois` and `staging_context` were added after the list was written
and were never added to it, so re-running a site at a *smaller* extent kept
the previous run's landmarks and canals in the database — and `db2dxf.py`
drew them, outside the new extent. Test-covered now; the equivalent for a
new column is a `MIGRATIONS` entry.

**Every feature topo2cad draws, it also stages.** Buildings, roads and
contours were staged from the start; landmark points (`staging_pois`),
landmark areas (`staging_buildings` with a `C-SITE-POI` cad_layer) and
context linework — water, vegetation, rail, barrier (`staging_context`) —
were added later, each because `db2dxf.py` cannot draw what the staging
layer does not hold, and a re-issued drawing that silently loses features is
worse than no re-issue path at all. If you teach `topo2cad.py` to draw
something new, stage it in the same commit and prove it with a layer-count
diff of the two DXFs. A `staging_context` row keeps its runs as a
MultiLineString and recovers the closed flag from `coords[0] == coords[-1]`,
so a pond stays a closed polyline; rail and barrier stage with a NULL
`label_x` because `topo2cad.py` never labels them, which drops them out of
`cad_labels` without a special case.

**Landmarks come in two shapes, and the tag filter is deliberate — twice
over.** A POI is `amenity`/`tourism`/`historic` (`poi_kind()`); the query
used to ask for `node["name"]`, which over a 770 × 410 m extent at Pathum Wan
returned 293 nodes of which 186 were mall floor markers, shop brands, benches
and bus stops — each drawing a symbol and a label.

Those three keys are then curated down to `POI_SUBMISSION`, because the keys
alone still return the wrong thing: of the 144 landmark nodes left at that
extent, 105 were restaurants, cafés, ATMs and money changers. A ผังบริเวณ is
read by an officer locating a parcel, and they locate it by วัด, โรงเรียน,
โรงพยาบาล, สถานีตำรวจ — civic fixtures that outlast any tenant — so the
default keeps worship, education, health, civil authority and public
fixtures, plus `fuel` (ปั๊มน้ำมัน is genuine wayfinding here) and all of
`historic`, which is small and inherently relevant. That is 144 → 9 points
and 10 → 2 areas at Pathum Wan, the survivors being the Royal Thai Police
headquarters and โรงเรียนวัดปทุมวนาราม. `--all-poi` restores the unfiltered
behaviour; adding a value is a one-line edit. Note `poi_kind()` falls
*through* a rejected value rather than returning None, so a feature tagged
`amenity=restaurant` + `tourism=museum` is still drawn as the museum.
The Overpass query stays broad on purpose, so `--all-poi` needs no refetch. Points land on `C-ANNO-SYMB` with
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
time. Both emit the same NCS layers, the same entity counts **and the same
label positions** — if you change drawing rules in one, change the other, and
prove it with `dxfdiff.py a.dxf b.dxf`, which exits non-zero on any
difference. **`dxfdiff` proves the two routes agree, not that either is
right** — it reported IDENTICAL while both dropped building courtyards, and
again while both skipped 69 ML footprints per site, because two
implementations of one mistake look like agreement. `dxfaudit.py` is the
other half: it re-queries Overpass for the same extent — or reads the .osm
export with `--osm-file`, which is the only audit the file route has — and
compares the drawing against the source, so a silent loss shows as a
shortfall and exits non-zero. Run it before a submission, and after any
change to what gets drawn. Its counting is deliberately **not**
`classify_elements()`: an audit that asks the drawing's own classifier what
the source contained cannot catch a bug in that classifier, which is the
exact failure it exists to cover, so the tag rules are restated there. It
earns its keep — the `--osm-file` pass immediately found a courtyard the
drawing was dropping, and the fix (`assign_inner_rings()`) now gives each
inner ring to the outer that contains it instead of dropping every hole on
a multi-outer relation. Its expectations are geometric, not naive: an inner
ring that straddles two outers, or touches its shell, cannot become a closed
polyline — `buffer(0)` bites a notch instead — so those are reported as a
note rather than counted as missing. One-way direction is checked the same
careful way: arrow *count* is not a source count (spacing decides how many a
run gets), so what it asserts is that a source with one-way roads did not
produce a drawing with no direction on it at all. Counting entities is not enough on its own: every label can be
present in both drawings and still sit up to 287 m apart, which is what that
tool's position pass exists to catch. Linear labels (roads, canals, parks,
contours) are therefore anchored by calling `stage_db.line_label_anchor()`
from *both* routes, and deduped by the same "longest feature carrying this
name" rule the `cad_labels` view applies with `ROW_NUMBER() ... ORDER BY
length_m DESC`. Do not reintroduce a "label the first clipped run" shortcut
in `topo2cad.py`; that is precisely the 287 m. Drawing furniture is sized
from the *nominal* extent in metres, not the projected bbox corners, because
`db2dxf.py` has only the nominal figure and `bbox_around()` approximates a
degree as 111,320 m — worth ~2 m on a 770 m extent. The language split lives in the `cad_labels` view: it emits one
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
`fitting_scale()` accounts for the title-block strip, which is why the usable
A3 viewport is 290 × 273 mm rather than the full 420 × 297 mm — and why the
old 770 × 410 m default fell to 1:5000 there. Never let a sheet crop the
extent silently; `add_sheet` warns with the scale that would fit. Render the
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

**Google sign-in and Drive upload are optional and stdlib-only.**
`gdrive.py` speaks OAuth 2.0 and Drive v3 over `urllib` rather than pulling
in `google-api-python-client`, because `serve.py` having no third-party
dependencies is what lets it run anywhere Python does. With
`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`/`GOOGLE_REDIRECT_URI` unset the
app behaves exactly as before and no Drive card is rendered — never show a
button that can only error. Sign-in gates *only* the upload; it is not an
access control, so `serve.py`'s warning about untrusted networks still
stands. Scope is `drive.file`, which reaches only files this app created:
do not widen it to `drive`, which would grant read access to everything the
user owns for no gain. The flow uses PKCE plus a `state` the callback
requires (an unknown state is a 400), `access_type=offline` and
`prompt=consent` so a refresh token actually arrives, and a refresh response
keeps the existing refresh token because Google omits it. Sessions live in
`<data-dir>/google_sessions.json` at 0600, deliberately *not* in
`staging.sqlite` — that file holds survey data and gets copied around, and
refresh tokens have no business travelling with it. Files land in
`maps2cad/<project>/`, folders reused rather than duplicated.

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
  cover holes with opaque patches,`serve.py` is stdlib-only on purpose (no deps in its header): it runs the
scripts as subprocesses through `uv run`, keyed by a hash of the request
parameters, and serves results from `output/web/` by job id rather than by
user-supplied path. It also browses and edits the staging database at
`/projects` → `/project/<id>`, where saved names are applied with
`UPDATE staging_buildings` and **Re-issue drawing** shells out to `db2dxf.py`.
A re-issue is registered as a normal job, so it appears in history with the
same download and preview routes. `/import` serves two converters from one
form and picks between them in `import_kind()` **by extension, never by
sniffing**: `.osm`/`.xml`/`.gz`/`.bz2`/`.pbf` go to `osm2cad.py`, everything
else to `gis2cad.py`, and a mixed upload is refused rather than guessed at —
the two draw different drawings from the same ground. A `.zip` is classified
after expansion by what it turned out to hold (a shapefile set wins over an
.osm beside it). `.pbf` is accepted by the upload and refused by
`osm2cad.py`, which answers with the conversion command; rejecting it at the
door would say "not a GIS file" about a file that is plainly OSM data.

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
