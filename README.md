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
```

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
