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
uv run scripts/overture.py --lat 13.7455 --lon 100.5325 --width 500 \
  --height 400        # named places from Overture; --overture draws them
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

**Default extent is 200 × 150 m** across the CLI tools and the web form,
with A3 the default sheet — the combination plots at **1:1000**, which is a
site-plan scale a reviewer expects. It was 1000 × 750, which fell to
**1:5000 on A3** and read as a locality map: after the title block and
margins `sheet.py` leaves a 290 × 273 mm A3 viewport, capping 1:2000 at
580 × 546 m. The extent and the sheet are chosen independently, so check
`fitting_scale()` for the combination you need rather than assuming any of
these; a wider extent still works and simply lands on a smaller scale.
`topo2cad.py` still accepts `--radius` for a square box.

The older numbers are worth keeping in mind when reading history: a run
before this default carries 1000 × 750 in its folder name and its staged
project, and the ML supplement covers a proportionally larger area at that
size — at a rural site 200 × 150 may hold no buildings at all where
1000 × 750 held a village edge.

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

**Storeys are stored formatted, and built-up land use is not planting.**
`stage_db.levels_label()` turns `building:levels` (or `height` when there is
no count) into the `3F` / `12.0 m` a drafter writes, and the *formatted*
string is the staged column. Two numeric columns would mean the rule in
Python for the extraction route and the same rule again in the `cad_labels`
view for the re-issue, and two spellings of one convention is the drift this
layer exists to prevent. The query also asks for the built-up land-use
classes it never fetched — residential, commercial, industrial and friends —
and they draw on `C-LAND-ZONE` rather than `C-LAND-VEGT`: a factory estate
is not a park, and a reviewer reads the two differently.

**The coordinate grid is computed, never stored.** `grid_spacing()` picks a
round interval — the same family of numbers the scale bar uses, because a
grid at 137 m is one nobody can read a coordinate off — and `grid_ticks()`
returns *round UTM values* rather than offsets from the centre: a line at
665,700 E is a number a surveyor can use, one at 665,694.02 is not. Both
live in `stage_db.py` and all three writers call them with the nominal
extent, which is the only geometry each of them shares, so the grids agree
without any of it being staged. `--contour-interval` forces the interval
where a deliverable specifies one; the automatic ~10 levels is for a first
look, and an interval that would draw hundreds of lines warns rather than
refuses.

**`--all-features` is the answer to "the map shows it and the drawing does
not".** The curated query stays the default because a submission drawing
wants what a reviewer reads, not every bench and bin — but nothing tagged
should be *silently* dropped, so this fetches `nwr[~"."~"."]` (every
element carrying at least one tag) and everything no rule claims lands on
`C-MISC-OTHR` / `C-MISC-SYMB` with a report of what that added, by tag. At
Pathum Wan it is 243 points and 10 lines against 2 curated landmarks. Three
things it must not do: keep *untagged* ways (they are multipolygon building
material, and drawing them traces every courtyard wall twice — test-covered);
draw its labels anywhere but the POI convention (`POI_LABEL_DX` across, 4.0
high, the offset `stage_pois` stores — getting this wrong put 195 labels up
to 2.19 m from where the re-issue drew them); or size its symbol locally
(`blocks.SIZE_FOR_LAYER` owns that, and a mismatch there is invisible to
`dxfdiff`, which does not compare block scale). `osm2cad.py` needs no
refetch for it: the file already holds everything.

**A plaza is an area, and water has a direction.** `highway=pedestrian`
with `area=yes` and a closed ring leaves the road bucket for `C-ROAD-PLAZ`
and draws closed: as a path it traced the outline as though the square were
a 2 m footway around its own edge. Waterways get flow arrows from
`arrow_positions()` — the same spacing rule the one-way arrows use — in the
direction the way was digitised, which is the only direction information a
canal carries and the convention `waterway=*` already relies on. A *closed*
run gets none: that is a pond, and water in a pond does not go anywhere.
Neither is staged beyond what already was — the arrows are derived in all
three writers from `staging_context`, so a re-issue draws them without a
column to carry them.

**Parking and gates are drawn regardless of the landmark filter.**
`amenity=parking` areas go to `C-SITE-PARK` before the POI branch sees
them, because a site plan needs the parking whether or not a car park
counts as a landmark worth curating — under the old rule they appeared only
with `--all-poi`, filed as landmark grounds. Gate nodes
(`gate|lift_gate|swing_gate|entrance`) draw as an open-leaf symbol on
`C-BNDY-BARR`: a gate is where you get in, and a plan that shows the fence
without the gate is missing the part a reviewer looks for.

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

**`compose.py` conducts, it does not convert.** One command puts several
sources in one drawing: `topo2cad.py` (OSM + Microsoft ML + DEM + optionally
Overture and a basemap) first, then every `--add` file through `osm2cad.py`
or `gis2cad.py`, then one `db2dxf.py` issue from the staging layer. Five
things are load-bearing:

*Order.* The OSM step **replaces** and every import **merges**, so the
imports come second and never the other way round — reversed, the OSM step
would delete every import that preceded it. This is the same
merge/replace asymmetry documented below, now with a command that depends
on it.

*Subprocesses, not imports.* Each step runs as its own `uv run`. The two
OSM stacks here keep deliberately disjoint dependency sets, and a conductor
that imported both would be the thing that fused them; `compose.py` itself
declares no dependencies and imports only `stage_db` (stdlib at module
level) and `serve.import_kind`.

*One routing rule.* `import_kind()` is imported from `serve.py` rather than
restated, so the browser upload and the CLI cannot disagree about which
converter draws a file — they produce different drawings from the same
ground, which is why that function refuses a mixed batch instead of
guessing.

*One CRS.* Every import is passed the srid the project already carries
(`project_srid()`), because each converter otherwise derives a UTM zone from
its own data: a survey file whose centroid falls the other side of 102°E
would stage in zone 48 inside a zone 47 project, a kilometre-scale error
that looks like nothing until the drawing opens. The extent is written back
from the request (`set_extent()`) because an import carries features, not an
extent, and the crop line, dimensions and grid come from the project row.

*An import hands back the drawing it joined, not the file it drew.*
`/import` writes the upload's own drawing as `import.dxf` and then re-issues
`site.dxf` from the staging layer, exactly as `compose.py` finishes a run.
Before this, importing a survey into an existing site downloaded two
boundary lines named `site.dxf` — the drawing was right in the database and
misleading in the hand. Both are offered, `site.dxf` first, and the plot
preview follows the combined one. A re-issue failure keeps the import
rather than losing the run.

*The web has the same two rules.* `/import` passes the target project's
srid as `--epsg` unless the form states one — an EPSG typed in is someone
saying what their file is in, and it wins — and `/project/<id>` shows the
same provenance table the CLI prints, through the existing lazy
`stage_db_module()` loader rather than a module-level import, because
`serve.py` staying stdlib-only is what lets it run anywhere Python does.
Verified live: `far.geojson` alone stages in EPSG:32648, and merged into a
zone 47 project through the browser it stages in 32647.

*Provenance names the file.* `stage_db.provenance()` reports one row per
(source, feature class) and the run writes `sources.csv` beside the
drawing. Getting there closed a real hole: `stage_roads()` never wrote the
`source` column at all, so a survey centreline merged from a shapefile read
as OpenStreetMap in the staging layer, and `staging_contours` /
`staging_spots` had no such column (added via `MIGRATIONS`, defaulting to
`copernicus_dem`). `gis2cad.py` now stages `user_gis:<file>` and
`osm2cad.py` `openstreetmap:<file>`, because a project can hold two surveys
and three extracts and one label for all of them is not a provenance
record. Nothing branches on these strings — they are descriptive — so a new
source only has to name itself honestly. A real six-source run at Pathum
Wan: 327 openstreetmap, 147 openstreetmap:soi.osm, 38 copernicus_dem, 29
overture, 21 microsoft_ml, 2 user_gis:boundary.geojson.

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

**`gisqa.py` uses the ML layer as a check on OSM, not as a fix.** The two
sources are independent — people tracing versus a model predicting — so
disagreement localises where the data is worth checking before a drawing
goes out. It reports and never repairs, for the same reason `underlay.py`
refuses to auto-trace: a corrected outline carries metres of error and
looks as authoritative in a DXF as a surveyed one. Two things it got wrong
first and must keep right: an OSM building is compared against the
**union** of every ML footprint under it, because a mall is one OSM polygon
and five ML pieces of roof and scoring against the largest piece flagged
every large building at Pathum Wan; and `poor_overlap` is worded as "worth
an eye" because ML traces roofs while OSM is drawn at the wall, a
systematic difference in a dense city rather than an error. Regularising
the ML geometry was considered and dropped after measuring it — median 4
vertices and 0.999 rectangularity at Yasothon, so Microsoft has already
done it.

**A second source of names is drawn on its own layer, or it is not worth
having.** `overture.py` reads Overture Maps' places theme — a conflation of
Meta, Microsoft, Esri, PinMeTo and OSM, each place scored — through DuckDB
over its public S3 parquet, and `topo2cad.py --overture` draws them on
`C-ANNO-OVTR` with their labels on `C-ANNO-OVTR-TH` / `C-ANNO-OVTR-EN`.
Four rules hold this together:

*Separable.* The names never touch `C-ANNO-TEXT*`, and the label layers are
in the same family as the symbol layer, so freezing `C-ANNO-OVTR*` returns
the drawing to what OSM says and never leaves a label pointing at nothing.
That split lives in the `cad_labels` view as a CASE on `staging_pois
.cad_layer`, so the re-issue route splits the same way — proved IDENTICAL
by `dxfdiff` with 13 Thai and 16 Latin Overture labels in both drawings.
XDATA rides under the appid `OVERTURE`, never `OSM`: filing a Meta record
as OpenStreetMap in the CAD attribute browser would be the same lie
`gis2cad.py`'s `GIS` id exists to avoid, and every place carries its
`source` dataset and `confidence` so nobody mistakes it for survey.

*Curated, or it is a mall directory.* 500 × 400 m at Siam Square holds
3,103 places raw and 1,797 above the 0.5 fetch floor; at confidence 0.9 the
survivors are still 22 japanese_restaurant, 20 clothing_store, 13
jewelry_store. `keep_place()` curates the same way `POI_SUBMISSION` does on
the OSM side and for the same reason — an officer locates a parcel by วัด,
โรงเรียน, โรงพยาบาล — leaving 29. It matches **substrings**, because
Overture adds taxonomy leaves between releases and an exact list silently
drops `buddhist_temple` the day it appears; the retail words are rejected
*first*, or `school_supply_store` reads as a school. `shopping_center` and
`department_store` are the two deliberate exceptions: a mall is how a Thai
address describes where a parcel is, even though its tenants are not.

*Cached per extent, not per floor.* The query costs about a minute, so
`cache_key()` deliberately excludes `min_confidence` and the cache holds
everything above `FETCH_FLOOR` — a drafter trying 0.9 then 0.8 pays it
once. The release is discovered from the bucket listing rather than pinned,
so a new Overture release is picked up instead of quietly serving stale
data. Overture's **buildings** theme was measured at 268 s for the same box
against ~6 MB Microsoft quadkey tiles, so it is not used; Google Open
Buildings was rejected outright at 1,016 MB for the tile covering Bangkok.

*It fetches without joining the dependency set.* `topo2cad.py` deliberately
does not declare `duckdb` — a 20 MB parquet engine has no business in every
run for one opt-in flag — so when the import is missing `fetch_places()`
re-runs `overture.py` under its own `uv run` and reads the cache file back.
Without that hand-off the first extent a site asks for fails on the import
while every cached one works, which is the worst shape a bug can take. The
cache honours `MAPS2CAD_DATA`, like `basemap.py`'s, so a Render deploy
writes it to the mounted disk and not into the image.

*It is on by default in the web app and named in the legend.* `serve.py`
defaults `overture` on with the rest of what *adds* to a run, since the
cost lands only on the first run of an extent; and `sheet.py`'s
`LEGEND_LABELS` names it "สถานที่ (ข้อมูล Overture) / Place (Overture)"
rather than lumping it in with the landmarks — a reviewer reading the sheet
is entitled to know which names nobody here surveyed. At 14.8165, 100.5116
this is the whole argument for the feature: OSM returns **0** landmark
points and Overture returns สถานีตำรวจภูธรท่าวุ้ง, สำนักงานสาธารณสุขอำเภอ
and the kindergarten — which is what an officer locates that parcel by.

*OSM wins a tie.* `drop_known()` drops a place whose name matches an OSM
feature within 25 m — name *and* proximity, because two branches of one
chain are two places while the same shop mapped twice is one. `dxfaudit.py`
counts only `C-ANNO-SYMB` as landmark symbols, which is correct rather than
an oversight: it audits the drawing against its OSM source, and a place
from another source is not OSM's to account for.

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
produce a drawing with no direction on it at all. It counts **both** building layers: an unnamed footprint draws on
`C-BLDG-UNNM`, and counting only `C-BLDG-OUTL` had it report a SHORTFALL
against a drawing carrying every building the source held — 23 of 77 at
Pathum Wan. An audit that cries wolf is read once and then ignored, so a
false alarm here is a worse defect than the silent loss it looks for; when
this tool disagrees with a drawing, check the tool's assumptions before the
drawing's. Counting entities is not enough on its own: every label can be
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

**The extent is dimensioned with real DIMENSION entities.**
`blocks.add_extent_dimensions()` writes two linear dimensions on
`C-ANNO-DIMS` under a `MAPS2CAD` dimension style, not lines with a number
beside them: a drafter expects to select one and have it behave like a
dimension. `render()` is called, because an unrendered dimension has no
geometry block and viewers draw nothing at all. The text height scales with
the extent — the same style has to read on a 200 m site plan and an 8 km
locality map — and the style formats with `dimpost = "<> m"` at zero
decimals, because this dimensions an extent, not a setting-out. All three
writers place them from the nominal extent, so they agree without staging.
Note they are invisible in a whole-drawing plot preview at A3: 3.6 m of text
across 500 m of ground is four pixels. That is scale, not a defect — the
geometry is there, and the isolated render shows extension lines,
arrowheads and `100 m`.

**A sheet carries a legend and a graphic scale bar.** `used_layers()`
lists only the layers that actually carry an entity, because a key to an
empty layer is noise and this drawing set creates several empty ones on
purpose (`C-PROP-LINE`, `C-ROAD-ROWY`). Neither sits behind a filled box:
a `SOLID` in "white" is ACI 255, which is white only in AutoCAD's palette
and plots as a black rectangle over the legend everywhere else — and a
`WIPEOUT` renders the same way in the plot preview. Each label carries its
own background mask instead, the convention the model-space annotation
already uses. The bar is four segments, and `nice_bar_length()` bounds the
*whole* bar rather than one segment: bounding the segment is how it first
came out 200 mm wide on an A3 sheet. Both are drawn over the viewport
corner rather than by shrinking the viewport, which would quietly change
the plot scale `fitting_scale()` promised for every existing sheet.

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
