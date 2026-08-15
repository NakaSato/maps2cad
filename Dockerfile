# maps2cad — the web UI plus every script it shells out to.
#
# serve.py runs the generators as subprocesses. With uv present it uses
# `uv run` and each script installs its own PEP 723 dependencies; without
# uv it falls back to sys.executable, so this image installs the union from
# requirements.txt and skips uv entirely.
#
# rasterio, geopandas/pyogrio and shapely all ship manylinux wheels with
# GDAL/GEOS/PROJ bundled, so no system GDAL is needed — only a C runtime
# and CA certificates for the Overpass, Copernicus and Microsoft fetches.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg \
    MAPS2CAD_DATA=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    # Thai-capable fonts. Without one the site map falls back with a
    # warning and Thai labels render as boxes; the DXF text style names
    # THSarabunNew, which AutoCAD resolves on the client, not here.
        fonts-thai-tlwg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ ./scripts/

# Everything that must outlive a deploy lives here: the DEM tile cache
# (~40 MB per 1x1 degree tile), the Microsoft footprint cache, the run
# folders, and staging.sqlite with its verified_names table. Mount a
# volume at /data or all of it is lost on restart.
VOLUME ["/data"]

EXPOSE 8765
CMD ["sh", "-c", "python scripts/serve.py --host 0.0.0.0 --port ${PORT:-8765} --data-dir /data"]
