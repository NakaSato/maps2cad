#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "shapely>=2.0",
#   "pyproj>=3.6",
# ]
# ///
"""SQLite staging layer between OSM extraction and CAD drafting.

Holds extracted features with their CAD label anchors already computed, so
the drawing step is plain SELECTs — no geometry work at draw time.

    uv run scripts/topo2cad.py --lat .. --lon .. --dem .. --outdir output/runs \\
        --db output/site.sqlite          # extract, draw, and stage in one run
    uv run scripts/stage_db.py --db output/site.sqlite --info
    uv run scripts/stage_db.py --db output/site.sqlite --labels 1

Design notes (these differ deliberately from a textbook PostGIS staging
schema, because the differences are what the real data demands):

* The projected SRID is stored **per project**, not fixed in the DDL.
  Thailand spans UTM 47N and 48N; pinning 32647 puts a site at 104.4°E
  1,078 km off-zone with +0.37% scale error.
* `osm_id` is **nullable** with a `source` discriminator. Microsoft ML
  footprints have no OSM id, and they are the overwhelming majority of
  buildings in rural extents (238 of 239 at the Yasothon test site).
* Label anchors use a **point guaranteed inside** the polygon, not the
  centroid: a centroid falls outside concave footprints (3 of 104 buildings
  in a dense Bangkok extent), which would strand the label in the street.
* Geometry is stored as WKB, so **multi-part** roads and buildings are
  representable. Clipping an extent produces MultiLineString roads.
* Roads carry a precomputed **label_rotation**, so text can be placed along
  the centreline without the CAD step recomputing azimuths.
"""


from __future__ import annotations


import argparse


import math


import re


import sqlite3


import sys


from datetime import datetime, timezone


from pathlib import Path

# The drawing rules live in cad_rules.py: everything deciding what a
# drawing looks like rather than what the database holds. They are
# re-exported here, in full and by name, because eight modules and the
# tests call them as stage_db.<name> and the documentation names them
# that way — the seam moved, the call sites did not. The dependency runs
# one way: this imports cad_rules, never the reverse.
from cad_rules import (  # noqa: F401
    ANNOTATION_REFERENCE_SCALE, ATTR_FIELDS, CHAR_ASPECT, CORNER_LABELS,
    CREDIT_WIDTH, DXF_SETUP, FLOW_ARROW_M, GIS_XDATA_APPID, GRID_STEPS,
    HATCH_PATTERNS, LANDMARK_LAYERS, MIN_EDGE_M, MS_XDATA_APPID,
    ONEWAY_ARROW_MAX_M, ONEWAY_ARROW_MIN_M, ONEWAY_MIN_RUN_M,
    ONEWAY_SPACING_M, POI_FIELDS, POI_LABEL_DX, POI_THAI,
    POI_THAI_PARTS, ROAD_FIELDS, SOURCE_CREDITS, THAI_PROBE, THAI_RE,
    XDATA_APPID, XDATA_MAX_TAGS, annotation_scale, arrow_positions,
    attribute_rows, azimuth_dms, bearing_text, carriageway_edges,
    check_fonts, clip_bytes, corner_label, corner_table, credit_lines,
    font_report, font_warnings, grid_spacing, grid_ticks, hatch_area,
    interior_point, is_thai, label_fits, levels_label,
    line_label_anchor, oneway_arrow_size, poi_kind_thai,
    polygon_corners, polygon_parts, repaired_polygon, ring_points,
    road_edges, set_drawing_extents, split_by_script, spot_grid,
    write_attribute_csv, write_corner_csv, write_font_note,
    write_poi_csv, write_road_csv, xdata_tags)


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    lat           REAL    NOT NULL,
    lon           REAL    NOT NULL,
    width_m       REAL    NOT NULL,
    height_m      REAL    NOT NULL,
    srid          INTEGER NOT NULL,   -- projected CRS for this site, in metres
    source_crs    TEXT    NOT NULL DEFAULT 'EPSG:4326',
    created_at    TEXT    NOT NULL,
    UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS staging_buildings (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    feature_id      TEXT    NOT NULL,   -- 'way/123', 'relation/9', 'ms/00042'
    osm_id          INTEGER,            -- NULL for non-OSM sources
    source          TEXT    NOT NULL,   -- openstreetmap | microsoft_ml
    building_type   TEXT,
    osm_name        TEXT,
    code            TEXT,               -- B### when the source has no name
    display_name    TEXT    NOT NULL,   -- what the drawing actually labels
    name_th         TEXT,               -- name:th, for C-ANNO-TEXT-TH
    name_en         TEXT,               -- name:en, for C-ANNO-TEXT-EN
    addr_house      TEXT,               -- addr:housenumber, drawn small on
                                        -- C-ANNO-ADDR beneath the name
    levels_label    TEXT,               -- "3F" or "12.0 m", already
                                        -- formatted: see levels_label()
    cad_layer       TEXT    NOT NULL DEFAULT 'C-BLDG-OUTL',
    geom_wkb        BLOB    NOT NULL,   -- (Multi)Polygon in the project SRID
    label_x         REAL    NOT NULL,   -- interior point, metres
    label_y         REAL    NOT NULL,
    label_rotation  REAL    NOT NULL DEFAULT 0,
    area_m2         REAL,
    latitude        REAL,               -- label anchor back in WGS 84
    longitude       REAL,
    minx REAL, miny REAL, maxx REAL, maxy REAL,
    UNIQUE (project_id, feature_id)
);

CREATE TABLE IF NOT EXISTS staging_roads (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    feature_id      TEXT    NOT NULL,
    osm_id          INTEGER,
    source          TEXT    NOT NULL DEFAULT 'openstreetmap',
    highway_type    TEXT,
    road_name       TEXT,
    road_ref        TEXT,               -- route number, kept separate
    official_name   TEXT,               -- official_name:th, the formal
                                        -- designation (ถนนพระรามที่ ๑),
                                        -- which is not the everyday name
    display_name    TEXT,
    name_th         TEXT,               -- name:th, for C-ANNO-TEXT-TH
    name_en         TEXT,               -- name:en, for C-ANNO-TEXT-EN
    cad_layer       TEXT    NOT NULL DEFAULT 'C-ROAD-CNTR',
    carriageway_m   REAL,               -- width used to offset the two edges
    oneway          INTEGER NOT NULL DEFAULT 0,   -- 1 with the geometry,
                                        -- -1 against it, 0 two-way. Drives
                                        -- the direction arrows on C-ROAD-ARRW
    geom_wkb        BLOB    NOT NULL,   -- (Multi)LineString in the project SRID
    label_x         REAL,
    label_y         REAL,
    label_rotation  REAL    NOT NULL DEFAULT 0,
    length_m        REAL,
    minx REAL, miny REAL, maxx REAL, maxy REAL,
    UNIQUE (project_id, feature_id)
);

-- Field-verified names outlive any single extraction. Re-running an OSM
-- pull replaces the staged features, but a name someone walked out and
-- confirmed must not be lost with them, so it is kept here by project name
-- and feature id and re-applied after every staging run.
CREATE TABLE IF NOT EXISTS verified_names (
    project_name  TEXT NOT NULL,
    feature_id    TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    verified_at   TEXT NOT NULL,
    PRIMARY KEY (project_name, feature_id)
);

-- Context linework: canals and ponds, parks and farmland, railways, walls
-- and fences. One row per OSM feature; the geometry is a MultiLineString of
-- the runs left after clipping, and a run whose first and last vertex
-- coincide is drawn as a closed polyline. Only water and green carry a
-- label, matching topo2cad.py, so label_x is NULL for the other kinds.
CREATE TABLE IF NOT EXISTS staging_context (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    feature_id      TEXT    NOT NULL,
    osm_id          INTEGER,
    source          TEXT    NOT NULL DEFAULT 'openstreetmap',
    kind            TEXT    NOT NULL,   -- water | green | rail | barrier
    display_name    TEXT,
    name_th         TEXT,
    name_en         TEXT,
    cad_layer       TEXT    NOT NULL,
    geom_wkb        BLOB    NOT NULL,   -- (Multi)LineString in the project SRID
    label_x         REAL,
    label_y         REAL,
    label_rotation  REAL    NOT NULL DEFAULT 0,
    length_m        REAL,
    UNIQUE (project_id, feature_id)
);

-- Landmarks mapped as a single node — a shrine, a monument, a viewpoint.
-- Landmarks mapped as an area live in staging_buildings with a cad_layer of
-- C-SITE-POI, because they already need a polygon, an interior label anchor
-- and an area, which is exactly that table.
CREATE TABLE IF NOT EXISTS staging_pois (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    feature_id      TEXT    NOT NULL,   -- 'node/123'
    osm_id          INTEGER,
    source          TEXT    NOT NULL DEFAULT 'openstreetmap',
    poi_key         TEXT,               -- amenity | tourism | historic
    poi_type        TEXT,               -- hospital, museum, monument, ...
    display_name    TEXT    NOT NULL,
    name_th         TEXT,
    name_en         TEXT,
    cad_layer       TEXT    NOT NULL DEFAULT 'C-ANNO-SYMB',
    geom_wkb        BLOB    NOT NULL,   -- Point in the project SRID: the
                                        -- symbol centre the circle is drawn on
    label_x         REAL    NOT NULL,   -- name anchor, already nudged clear
    label_y         REAL    NOT NULL,   -- of the symbol (see POI_LABEL_DX)
    latitude        REAL,
    longitude       REAL,
    UNIQUE (project_id, feature_id)
);

CREATE TABLE IF NOT EXISTS staging_contours (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    elevation_m     REAL    NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'copernicus_dem',
    cad_layer       TEXT    NOT NULL DEFAULT 'C-TOPO-CONT',
    geom_wkb        BLOB    NOT NULL,   -- LineString in the project SRID
    label_x         REAL,
    label_y         REAL,
    label_rotation  REAL    NOT NULL DEFAULT 0,
    length_m        REAL
);

-- Spot heights read off the DEM: the elevation at a point, which is what
-- a surveyor reads off a plan. Contours give the shape of the ground;
-- spot heights give a number you can level to.
--
-- Staged rather than treated as drawing furniture because db2dxf.py has no
-- DEM to sample — without this table a re-issue would come back with the
-- contours and no heights, which is the sort of silent loss this layer
-- exists to prevent.
CREATE TABLE IF NOT EXISTS staging_spots (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    elevation_m     REAL    NOT NULL,
    x               REAL    NOT NULL,   -- project SRID
    y               REAL    NOT NULL,
    source          TEXT    NOT NULL DEFAULT 'copernicus_dem',
    cad_layer       TEXT    NOT NULL DEFAULT 'C-TOPO-SPOT',
    UNIQUE (project_id, x, y)
);

-- The source OSM tags of every drawn feature, one row per tag.
--
-- Long rather than wide: OSM features carry wildly different tag sets — a
-- handful on a fence, forty on a mall — so a column per key would be a
-- sparse table hundreds of columns across, and choosing "the common keys"
-- would silently drop the rest.
--
-- This is what lets a re-issued drawing carry the same extended entity data
-- (XDATA) the extraction wrote. Without it, correcting one name and
-- redrawing would quietly strip the attributes off every entity in the
-- drawing, which is the sort of loss nobody notices until a reviewer asks
-- where the source data went.
CREATE TABLE IF NOT EXISTS staging_tags (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    feature_id      TEXT    NOT NULL,   -- 'way/123', 'relation/9/0', 'ms/00042'
    feature_type    TEXT    NOT NULL,   -- building | road | path | water | ...
    cad_layer       TEXT    NOT NULL,
    display_name    TEXT,
    -- XDATA application id. 'OSM' for OpenStreetMap tags, 'GIS' for the
    -- fields of a file the user supplied: one drawing can carry both when a
    -- survey is merged into an extraction, and labelling a shapefile's DBF
    -- columns "OSM" in the CAD attribute browser would be a lie.
    appid           TEXT    NOT NULL DEFAULT 'OSM',
    key             TEXT    NOT NULL,
    value           TEXT,
    UNIQUE (project_id, feature_id, key)
);
CREATE INDEX IF NOT EXISTS idx_tags_feature
    ON staging_tags (project_id, feature_id);

-- Bounding-box columns stand in for a spatial index: SQLite has no GiST,
-- but a window query on these is index-assisted and plenty for one site.
CREATE INDEX IF NOT EXISTS idx_bldg_bbox
    ON staging_buildings (project_id, minx, maxx, miny, maxy);
CREATE INDEX IF NOT EXISTS idx_road_bbox
    ON staging_roads (project_id, minx, maxx, miny, maxy);

-- Everything a CAD writer needs for annotation, already resolved.
-- Road names are deduplicated to one label per unique name, placed on the
-- longest segment: a divided carriageway is several OSM ways sharing a
-- name, so labelling per row prints it two or four times over.
--
-- Annotation is split by script so a drafter can freeze one language:
-- name_th -> C-ANNO-TEXT-TH, name_en -> C-ANNO-TEXT-EN, and anything
-- language-neutral (a B### code) stays on the base C-ANNO-TEXT layer so
-- freezing either language never blanks the drawing. label_offset is a
-- distance in metres applied perpendicular to label_rotation by the CAD
-- writer, which stacks the English label above the Thai one.
DROP VIEW IF EXISTS cad_labels;
CREATE VIEW cad_labels AS
    -- Thai building name
    SELECT project_id, 'building' AS feature_class, name_th AS text,
           label_x, label_y, label_rotation, 3.5 AS text_height,
           'C-ANNO-TEXT-TH' AS cad_layer, 0.0 AS label_offset
      FROM staging_buildings
     WHERE name_th IS NOT NULL AND name_th <> ''
    UNION ALL
    -- English building name, stacked above the Thai one when both exist
    SELECT project_id, 'building', name_en,
           label_x, label_y, label_rotation, 3.5, 'C-ANNO-TEXT-EN',
           CASE WHEN name_th IS NOT NULL AND name_th <> ''
                THEN 3.5 * 1.3 ELSE 0.0 END
      FROM staging_buildings
     WHERE name_en IS NOT NULL AND name_en <> ''
    UNION ALL
    -- Neutral label — a title like "school" or, on older data, a B###
    -- code — and only when there is no name at all
    SELECT project_id, 'building', display_name,
           label_x, label_y, label_rotation, 3.5, 'C-ANNO-TEXT', 0.0
      FROM staging_buildings
     WHERE display_name <> ''
       AND COALESCE(name_th, '') = '' AND COALESCE(name_en, '') = ''
    UNION ALL
    -- The B### inventory code, when the source gave the footprint no name
    -- at all. Its own feature_class, because it is the one label a writer
    -- is allowed to suppress (--names-only) and it must be suppressible
    -- without touching the names. It stays on the neutral layer: at a
    -- rural site every building label is a code, so filing them as
    -- English would blank a Thai-only plot entirely.
    SELECT project_id, 'building_code', code,
           label_x, label_y, label_rotation, 3.5, 'C-ANNO-TEXT', 0.0
      FROM staging_buildings
     WHERE code IS NOT NULL AND code <> '' AND display_name = ''
       AND COALESCE(name_th, '') = '' AND COALESCE(name_en, '') = ''
    UNION ALL
    -- Context features are labelled once per unique name within their own
    -- kind, the way topo2cad.py dedupes per layer: one canal mapped as
    -- several ways still gets one name.
    SELECT project_id, 'context', name_th,
           label_x, label_y, label_rotation, 4.0, 'C-ANNO-TEXT-TH', 0.0
      FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY project_id, kind,
                                                      display_name
                                         ORDER BY length_m DESC) AS rn
              FROM staging_context
             WHERE display_name IS NOT NULL AND display_name <> ''
               AND label_x IS NOT NULL)
     WHERE rn = 1 AND name_th IS NOT NULL AND name_th <> ''
    UNION ALL
    SELECT project_id, 'context', name_en,
           label_x, label_y, label_rotation, 4.0, 'C-ANNO-TEXT-EN',
           CASE WHEN name_th IS NOT NULL AND name_th <> ''
                THEN 4.0 * 1.3 ELSE 0.0 END
      FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY project_id, kind,
                                                      display_name
                                         ORDER BY length_m DESC) AS rn
              FROM staging_context
             WHERE display_name IS NOT NULL AND display_name <> ''
               AND label_x IS NOT NULL)
     WHERE rn = 1 AND name_en IS NOT NULL AND name_en <> ''
    UNION ALL
    SELECT project_id, 'context', display_name,
           label_x, label_y, label_rotation, 4.0, 'C-ANNO-TEXT', 0.0
      FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY project_id, kind,
                                                      display_name
                                         ORDER BY length_m DESC) AS rn
              FROM staging_context
             WHERE display_name IS NOT NULL AND display_name <> ''
               AND label_x IS NOT NULL)
     WHERE rn = 1 AND COALESCE(name_th, '') = ''
                  AND COALESCE(name_en, '') = ''
    UNION ALL
    -- House numbers sit under whatever label the building already carries,
    -- small and language-neutral: a number is a number in either script.
    SELECT project_id, 'building_addr', addr_house,
           label_x, label_y, 0.0, 2.2, 'C-ANNO-ADDR', -3.0
      FROM staging_buildings
     WHERE addr_house IS NOT NULL AND addr_house <> ''
    UNION ALL
    -- Storeys under the house number: "3F" is read the same in either
    -- script, so it stays off the language layers like the codes do.
    SELECT project_id, 'building_levels', levels_label,
           label_x, label_y, 0.0, 2.2, 'C-ANNO-ADDR', -5.4
      FROM staging_buildings
     WHERE levels_label IS NOT NULL AND levels_label <> ''
    UNION ALL
    -- Landmark points. label_x/label_y is already clear of the symbol, so
    -- these rows need only the same language stacking as everything else.
    -- A place from a third-party source keeps its name inside its own layer
    -- family (C-ANNO-OVTR-TH/-EN): freezing that source has to take the
    -- names with the symbols, or the drawing keeps a label pointing at
    -- nothing. The language split survives inside it.
    SELECT project_id, 'poi', name_th,
           label_x, label_y, 0.0, 4.0,
           CASE WHEN cad_layer = 'C-ANNO-OVTR' THEN 'C-ANNO-OVTR-TH'
                ELSE 'C-ANNO-TEXT-TH' END, 0.0
      FROM staging_pois WHERE name_th IS NOT NULL AND name_th <> ''
    UNION ALL
    SELECT project_id, 'poi', name_en,
           label_x, label_y, 0.0, 4.0,
           CASE WHEN cad_layer = 'C-ANNO-OVTR' THEN 'C-ANNO-OVTR-EN'
                ELSE 'C-ANNO-TEXT-EN' END,
           CASE WHEN name_th IS NOT NULL AND name_th <> ''
                THEN 4.0 * 1.3 ELSE 0.0 END
      FROM staging_pois WHERE name_en IS NOT NULL AND name_en <> ''
    UNION ALL
    SELECT project_id, 'poi', display_name,
           label_x, label_y, 0.0, 4.0,
           CASE WHEN cad_layer = 'C-ANNO-OVTR' THEN 'C-ANNO-OVTR'
                ELSE 'C-ANNO-TEXT' END, 0.0
      FROM staging_pois
     WHERE display_name <> ''
       AND COALESCE(name_th, '') = '' AND COALESCE(name_en, '') = ''
    UNION ALL
    SELECT project_id, 'road_name', name_th,
           label_x, label_y, label_rotation, 5.0, 'C-ANNO-TEXT-TH', 0.0
      FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY project_id, road_name
                                         ORDER BY length_m DESC) AS rn
              FROM staging_roads
             WHERE road_name IS NOT NULL AND label_x IS NOT NULL)
     WHERE rn = 1 AND name_th IS NOT NULL AND name_th <> ''
    UNION ALL
    SELECT project_id, 'road_name', name_en,
           label_x, label_y, label_rotation, 5.0, 'C-ANNO-TEXT-EN',
           CASE WHEN name_th IS NOT NULL AND name_th <> ''
                THEN 5.0 * 1.3 ELSE 0.0 END
      FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY project_id, road_name
                                         ORDER BY length_m DESC) AS rn
              FROM staging_roads
             WHERE road_name IS NOT NULL AND label_x IS NOT NULL)
     WHERE rn = 1 AND name_en IS NOT NULL AND name_en <> ''
    UNION ALL
    -- A named road with neither language column filled would otherwise fall
    -- out of the view entirely; buildings have the same neutral fallback.
    SELECT project_id, 'road_name', road_name,
           label_x, label_y, label_rotation, 5.0, 'C-ANNO-TEXT', 0.0
      FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY project_id, road_name
                                         ORDER BY length_m DESC) AS rn
              FROM staging_roads
             WHERE road_name IS NOT NULL AND label_x IS NOT NULL)
     WHERE rn = 1 AND COALESCE(name_th, '') = ''
                  AND COALESCE(name_en, '') = ''
    UNION ALL
    -- Route number, always with the Thai 'ทล.' prefix, so it sits on the
    -- Thai layer. Offset clears the name stack above it.
    -- A bare "311" beside a road name reads as a distance,
    -- a lane count or a house number. ทล.311 is what the number is.
    SELECT project_id, 'road_ref', 'ทล.' || road_ref,
           label_x, label_y, label_rotation, 4.0,
           -- Thai now that it always carries a Thai prefix.
           'C-ANNO-TEXT-TH',
           CASE WHEN road_name IS NULL THEN 0.0
                WHEN COALESCE(name_th, '') <> '' AND
                     COALESCE(name_en, '') <> '' THEN 6.0 + 5.0 * 1.3
                ELSE 6.0 END
      FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY project_id, road_ref
                                         ORDER BY length_m DESC) AS rn
              FROM staging_roads
             WHERE road_ref IS NOT NULL AND label_x IS NOT NULL)
     WHERE rn = 1;
"""


# Columns added after the first release. SQLite takes one ADD COLUMN per
# statement and has no IF NOT EXISTS for them, so an existing staging file
# is migrated by comparing against PRAGMA table_info.
# Everything a re-extraction of the same project must clear. Add a
# staging_* table here in the same commit that creates it, or the second run
# of a site silently keeps the first run's features.
STAGED_TABLES = ("staging_buildings", "staging_roads", "staging_contours",
                 "staging_pois", "staging_context", "staging_tags",
                 "staging_spots")


MIGRATIONS = {
    "staging_buildings": (("name_th", "TEXT"), ("name_en", "TEXT"),
                          ("addr_house", "TEXT"),
                          ("levels_label", "TEXT")),
    "staging_roads": (("name_th", "TEXT"), ("name_en", "TEXT"),
                      ("oneway", "INTEGER NOT NULL DEFAULT 0"),
                      ("official_name", "TEXT")),
    "staging_tags": (("appid", "TEXT NOT NULL DEFAULT 'OSM'"),),
    # Provenance on the DEM-derived tables: a project can now hold features
    # from several sources at once, and a report that cannot name where a
    # row came from is not a report.
    "staging_contours": (("source", "TEXT NOT NULL DEFAULT 'copernicus_dem'"),),
    "staging_spots": (("source", "TEXT NOT NULL DEFAULT 'copernicus_dem'"),),
}


def migrate(conn) -> list[str]:
    """Bring an existing staging database up to the current schema.
    Returns the list of columns added, so callers can report the upgrade."""
    added = []
    for table, columns in MIGRATIONS.items():
        have = {r["name"] for r in
                conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue            # table not created yet; SCHEMA_SQL covers it
        for column, decl in columns:
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN"
                             f" {column} {decl}")
                added.append(f"{table}.{column}")
    if added:
        _backfill_languages(conn)
        conn.commit()
    return added


def _backfill_languages(conn) -> None:
    """File already-staged names onto a language layer. Without this, every
    feature in a pre-existing staging database has NULL name_th/name_en and
    cad_labels would drop its label entirely."""
    for table, source in (("staging_buildings", "osm_name"),
                          ("staging_roads", "road_name")):
        rows = conn.execute(
            f"SELECT id, {source} AS nm FROM {table}"
            f" WHERE {source} IS NOT NULL AND {source} <> ''"
            " AND COALESCE(name_th, '') = '' AND COALESCE(name_en, '') = ''"
        ).fetchall()
        conn.executemany(
            f"UPDATE {table} SET name_th = ?, name_en = ? WHERE id = ?",
            [(*split_by_script(r["nm"]), r["id"]) for r in rows])


def connect(path: str | Path) -> sqlite3.Connection:
    # `--db output/runs/site.sqlite` into a folder that does not exist yet
    # otherwise fails with a bare "unable to open database file", which says
    # nothing about the missing directory. ':memory:' has no parent.
    parent = Path(path).parent
    if str(path) != ":memory:" and str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    # SQLite does not resolve a view body until it is queried, so cad_labels
    # above can reference columns an older file has yet to gain. Adding them
    # here, right after, keeps that window closed.
    added = migrate(conn)
    if added:
        print(f"  migrated staging schema: added {', '.join(added)}")
    return conn


def create_project(conn, name, lat, lon, width_m, height_m, srid) -> int:
    """Register a site, replacing any previous staging for the same name."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row = conn.execute("SELECT id FROM projects WHERE name = ?",
                       (name,)).fetchone()
    if row:
        # Clear the staged features but keep the project row, so its id — and
        # therefore any /project/<id> link or bookmark — stays stable across
        # re-extractions. Verified names live in their own table and survive.
        pid = row["id"]
        # Every staged table, not only the three that existed when this was
        # written: staging_pois, staging_context and staging_tags were added
        # later and were never cleared, so re-running a site at a smaller
        # extent left landmarks and canals from the old one in the database —
        # and a db2dxf.py re-issue drew them, outside the new extent.
        for table in STAGED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE project_id = ?", (pid,))
        conn.execute(
            "UPDATE projects SET lat = ?, lon = ?, width_m = ?, height_m = ?,"
            " srid = ?, created_at = ? WHERE id = ?",
            (lat, lon, width_m, height_m, srid, stamp, pid))
        conn.commit()
        return pid
    cur = conn.execute(
        "INSERT INTO projects (name, lat, lon, width_m, height_m, srid,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (name, lat, lon, width_m, height_m, srid, stamp))
    conn.commit()
    return cur.lastrowid


def get_or_create_project(conn, name, lat, lon, width_m, height_m,
                          srid) -> tuple[int, bool]:
    """Return (project_id, existed). Unlike create_project this keeps what is
    already staged, so a survey layer can be merged into an OSM extraction."""
    row = conn.execute("SELECT id FROM projects WHERE name = ?",
                       (name,)).fetchone()
    if row:
        return row["id"], True
    return create_project(conn, name, lat, lon, width_m, height_m, srid), False


def record_verified(conn, project_id: int, feature_id: str,
                    display_name: str) -> None:
    """Remember a field-verified name so it survives re-extraction."""
    row = conn.execute("SELECT name FROM projects WHERE id = ?",
                       (project_id,)).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT INTO verified_names (project_name, feature_id, display_name,"
        " verified_at) VALUES (?,?,?,?) ON CONFLICT(project_name, feature_id)"
        " DO UPDATE SET display_name = excluded.display_name,"
        " verified_at = excluded.verified_at",
        (row["name"], feature_id, display_name,
         datetime.now(timezone.utc).isoformat(timespec="seconds")))


def forget_verified(conn, project_id: int, feature_id: str) -> None:
    row = conn.execute("SELECT name FROM projects WHERE id = ?",
                       (project_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM verified_names WHERE project_name = ?"
                     " AND feature_id = ?", (row["name"], feature_id))


def apply_verified(conn, project_id: int) -> int:
    """Re-apply remembered names to the freshly staged features.

    A verified name also re-routes the feature's annotation layer: someone
    who walks out and confirms 'ศาลาประชาคม' expects it on the Thai layer,
    not on the neutral one the B### code was using. SQLite has no regex, so
    the script test happens here rather than in the cad_labels view.
    """
    row = conn.execute("SELECT name FROM projects WHERE id = ?",
                       (project_id,)).fetchone()
    if row is None:
        return 0
    verified = conn.execute(
        "SELECT v.feature_id, v.display_name FROM verified_names v"
        " WHERE v.project_name = ? AND v.feature_id IN ("
        "   SELECT feature_id FROM staging_buildings WHERE project_id = ?)",
        (row["name"], project_id)).fetchall()
    if not verified:
        return 0
    updates = [(r["display_name"],
                r["display_name"] if is_thai(r["display_name"]) else None,
                None if is_thai(r["display_name"]) else r["display_name"],
                project_id, r["feature_id"]) for r in verified]
    conn.executemany(
        "UPDATE staging_buildings SET display_name = ?, name_th = ?,"
        " name_en = ? WHERE project_id = ? AND feature_id = ?", updates)
    conn.commit()
    return len(updates)


def _osm_id(feature_id: str):
    """'way/123' -> 123; 'ms/00042' -> None (no OSM identity).

    A relation with several `outer` rings is staged as one row per ring,
    with ids like 'relation/123/0' — the OSM identity is still 123, and
    int()-ing the whole tail raised ValueError on the first real extract
    that carried one.
    """
    if "/" not in feature_id:
        return None
    kind, _, num = feature_id.partition("/")
    num = num.split("/", 1)[0]
    return int(num) if kind in ("way", "relation", "node") \
        and num.isdigit() else None


def stage_spots(conn, project_id, rows) -> int:
    """records: dicts with x, y (project SRID) and elevation_m."""
    conn.executemany(
        "INSERT OR REPLACE INTO staging_spots (project_id, x, y,"
        " elevation_m, source, cad_layer) VALUES (?,?,?,?,?,?)",
        [(project_id, float(r["x"]), float(r["y"]),
          float(r["elevation_m"]), r.get("source", "copernicus_dem"),
          r.get("cad_layer", "C-TOPO-SPOT"))
         for r in rows])
    conn.commit()
    return len(rows)


def stage_tags(conn, project_id, rows, appid=XDATA_APPID) -> int:
    """Store the attribute rows; `rows` is attribute_rows() output.

    `appid` records which XDATA application id these belong under, so a
    re-issue puts a survey's fields back under GIS and OSM tags under OSM
    even when one project holds both.
    """
    conn.executemany(
        "INSERT OR REPLACE INTO staging_tags (project_id, feature_id,"
        " feature_type, cad_layer, display_name, appid, key, value)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [(project_id, r["feature_id"], r["feature_type"], r["cad_layer"],
          r.get("display_name") or "", r.get("appid") or appid,
          r["key"], r["value"]) for r in rows])
    conn.commit()
    return len(rows)


def tags_by_feature(conn, project_id) -> dict:
    """feature_id -> (appid, {key: value}), for a writer re-attaching XDATA."""
    out: dict[str, tuple[str, dict]] = {}
    for row in conn.execute(
            "SELECT feature_id, appid, key, value FROM staging_tags"
            " WHERE project_id = ? ORDER BY feature_id, key", (project_id,)):
        appid, tags = out.setdefault(row["feature_id"], (row["appid"], {}))
        tags[row["key"]] = row["value"]
    return out


def stage_buildings(conn, project_id, records, to_wgs=None) -> int:
    """records: dicts with feature_id, source, geom (shapely, project SRID),
    osm_name, code, display_name, building_type, addr_house."""
    from shapely import wkb as shp_wkb

    rows = []
    for r in records:
        geom = r["geom"]
        lx, ly = interior_point(geom)
        lon = lat = None
        if to_wgs is not None:
            lon, lat = to_wgs.transform(lx, ly)
        minx, miny, maxx, maxy = geom.bounds
        rows.append((
            project_id, r["feature_id"], _osm_id(r["feature_id"]),
            r.get("source", "openstreetmap"), r.get("building_type"),
            r.get("osm_name") or None, r.get("code") or None,
            # display_name holds a *name* and nothing else; the B###
            # code stays in its own column. Folding the code in here
            # instead is what a name editor then reads back as the
            # building's name, and what apply_verified() would compare a
            # field correction against. The code still reaches the drawing
            # — cad_labels emits it as its own 'building_code' row, which
            # is also what lets --names-only drop it without touching a
            # single name.
            r.get("display_name") or "",
            *split_by_script(r.get("osm_name"), r.get("name_th"),
                             r.get("name_en")),
            r.get("addr_house") or None, r.get("levels_label") or None,
            r.get("cad_layer", "C-BLDG-OUTL"),
            shp_wkb.dumps(geom), lx, ly, 0.0, geom.area, lat, lon,
            minx, miny, maxx, maxy))
    conn.executemany(
        "INSERT OR REPLACE INTO staging_buildings (project_id, feature_id,"
        " osm_id, source, building_type, osm_name, code, display_name,"
        " name_th, name_en, addr_house, levels_label, cad_layer,"
        " geom_wkb, label_x, label_y, label_rotation, area_m2, latitude,"
        " longitude, minx, miny, maxx, maxy)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    restored = apply_verified(conn, project_id)
    if restored:
        print(f"  restored {restored} field-verified name(s) from a previous "
              "run")
    return len(rows)


def stage_context(conn, project_id, records) -> int:
    """records: dicts with feature_id, kind, cad_layer, runs (lists of
    (x, y) in the project SRID), display_name/name_th/name_en and a
    `labelled` flag. Rail and barrier are drawn but never labelled, so they
    stage with a NULL anchor and drop out of cad_labels on their own."""
    from shapely import wkb as shp_wkb
    from shapely.geometry import LineString, MultiLineString

    rows = []
    for r in records:
        runs = [LineString(run) for run in r["runs"] if len(run) >= 2]
        if not runs:
            continue
        geom = runs[0] if len(runs) == 1 else MultiLineString(runs)
        if r.get("labelled") and (r.get("display_name") or "").strip():
            lx, ly, rot = line_label_anchor(geom)
        else:
            lx, ly, rot = None, None, 0.0
        th, en = split_by_script(r.get("display_name"), r.get("name_th"),
                                 r.get("name_en"))
        rows.append((
            project_id, r["feature_id"], _osm_id(r["feature_id"]),
            r.get("source", "openstreetmap"), r["kind"],
            r.get("display_name") or None, th, en, r["cad_layer"],
            shp_wkb.dumps(geom), lx, ly, rot, geom.length))
    conn.executemany(
        "INSERT OR REPLACE INTO staging_context (project_id, feature_id,"
        " osm_id, source, kind, display_name, name_th, name_en, cad_layer,"
        " geom_wkb, label_x, label_y, label_rotation, length_m)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def stage_pois(conn, project_id, records, label_dx=None) -> int:
    """records: dicts with feature_id, x, y (project SRID), poi_key,
    poi_type, display_name and optionally name_th/name_en.

    `label_dx` is how far the name sits from the symbol, in metres of
    ground — POI_LABEL_DX at 1:1000, scaled up with the sheet, because a
    3 m gap is 0.6 mm at 1:5000 and the name lands on the symbol it
    belongs to. The anchor is stored already offset (db2dxf.py draws the
    staged point as it finds it), so a re-issue onto a very different sheet
    keeps the gap the extraction chose while its text resizes: the label
    stays clear of the symbol either way, and no anchor moves between a
    drawing and its own re-issue.
    """
    from shapely import wkb as shp_wkb
    from shapely.geometry import Point

    rows = []
    for r in records:
        x, y = float(r["x"]), float(r["y"])
        th, en = split_by_script(r.get("display_name"), r.get("name_th"),
                                 r.get("name_en"))
        rows.append((
            project_id, r["feature_id"], _osm_id(r["feature_id"]),
            r.get("source", "openstreetmap"), r.get("poi_key"),
            r.get("poi_type"), r.get("display_name") or "", th, en,
            r.get("cad_layer", "C-ANNO-SYMB"),
            shp_wkb.dumps(Point(x, y)),
            x + (POI_LABEL_DX if label_dx is None else float(label_dx)), y,
            r.get("latitude"), r.get("longitude")))
    conn.executemany(
        "INSERT OR REPLACE INTO staging_pois (project_id, feature_id, osm_id,"
        " source, poi_key, poi_type, display_name, name_th, name_en,"
        " cad_layer, geom_wkb, label_x, label_y, latitude, longitude)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def stage_contours(conn, project_id, records) -> int:
    """records: dicts with elevation_m and geom (shapely LineString)."""
    from shapely import wkb as shp_wkb

    rows = []
    for r in records:
        geom = r["geom"]
        lx, ly, rot = line_label_anchor(geom)
        rows.append((project_id, r["elevation_m"],
                     r.get("source", "copernicus_dem"),
                     r.get("cad_layer", "C-TOPO-MINR"), shp_wkb.dumps(geom),
                     lx, ly, rot, geom.length))
    conn.executemany(
        "INSERT INTO staging_contours (project_id, elevation_m, source,"
        " cad_layer, geom_wkb, label_x, label_y, label_rotation, length_m)"
        " VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def stage_roads(conn, project_id, records) -> int:
    """records: dicts with feature_id, geom (shapely, project SRID),
    highway_type, road_name, road_ref, carriageway_m, oneway.

    `source` defaults to openstreetmap and is written, not assumed: a
    centreline merged in from a survey file used to land in this table
    reading as OpenStreetMap, because the column existed and nothing filled
    it. One project can hold several sources at once.
    """
    from shapely import wkb as shp_wkb

    rows = []
    for r in records:
        geom = r["geom"]
        lx, ly, rot = line_label_anchor(geom)
        minx, miny, maxx, maxy = geom.bounds
        name, ref = r.get("road_name"), r.get("road_ref")
        display = f"{name} ({ref})" if name and ref else (name or ref)
        th, en = split_by_script(name, r.get("name_th"), r.get("name_en"))
        rows.append((
            project_id, r["feature_id"], _osm_id(r["feature_id"]),
            r.get("source", "openstreetmap"),
            r.get("highway_type"), name, ref, display, th, en,
            r.get("official_name") or None,
            r.get("cad_layer", "C-ROAD-CNTR"),
            r.get("carriageway_m"), int(r.get("oneway") or 0),
            shp_wkb.dumps(geom),
            lx, ly, rot, geom.length, minx, miny, maxx, maxy))
    conn.executemany(
        "INSERT OR REPLACE INTO staging_roads (project_id, feature_id, osm_id,"
        " source, highway_type, road_name, road_ref, display_name,"
        " name_th, name_en, official_name,"
        " cad_layer, carriageway_m, oneway,"
        " geom_wkb, label_x, label_y, label_rotation, length_m,"
        " minx, miny, maxx, maxy)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


def road_inventory_rows(conn, project_id) -> list[dict]:
    """One row per staged road, longest first.

    Buildings have had an inventory from the start and roads never did, so
    the one thing a reader most often wants off a site plan — which road is
    which, and what number it carries — could only be had by opening the
    DXF and clicking a line. `official_name` is in here even though it is
    rarely drawn: ถนนพระรามที่ ๑ is the formal designation and a submission
    may need to quote it.
    """
    cols = ", ".join(ROAD_FIELDS)
    return [dict(r) for r in conn.execute(
        f"SELECT {cols} FROM staging_roads WHERE project_id = ?"
        " ORDER BY length_m DESC, feature_id", (project_id,))]


def poi_inventory_rows(conn, project_id, centre=None) -> list[dict]:
    """Nearby landmarks, nearest first, with how far and which way.

    The drawing has always carried these — วัด, โรงเรียน, โรงพยาบาล,
    สถานีตำรวจ are exactly what POI_SUBMISSION curates for — but only as
    symbols on C-ANNO-SYMB. A ผังบริเวณ is read alongside a list of
    สถานที่สำคัญใกล้เคียง, and there was no list: you could see a symbol on
    the sheet and had no way to say what it was, how far off, or in which
    direction without measuring it yourself.

    `centre` is the project's own (x, y) in its SRID. Without it the
    distance and bearing columns are left empty rather than guessed.
    """
    from shapely import wkb as shp_wkb

    out = []
    for row in conn.execute(
            "SELECT feature_id, poi_key, poi_type, display_name, name_th,"
            " name_en, cad_layer, geom_wkb, latitude, longitude, source"
            " FROM staging_pois WHERE project_id = ?"
            " AND display_name <> '' AND cad_layer IN "
            "('" + "','".join(LANDMARK_LAYERS) + "')", (project_id,)):
        rec = {k: row[k] for k in
               ("feature_id", "poi_key", "poi_type", "display_name",
                "name_th", "name_en", "cad_layer", "latitude", "longitude",
                "source")}
        rec["kind_th"] = poi_kind_thai(row["poi_type"])
        rec["distance_m"] = ""
        rec["bearing"] = ""
        if centre is not None:
            try:
                pt = shp_wkb.loads(row["geom_wkb"])
                de, dn = pt.x - centre[0], pt.y - centre[1]
                rec["distance_m"] = round(math.hypot(de, dn), 1)
                rec["bearing"] = bearing_text(de, dn)
            except Exception:
                pass
        out.append(rec)
    out.sort(key=lambda r: (r["distance_m"] if isinstance(r["distance_m"],
                                                          float) else 1e9,
                            r["feature_id"]))
    return out


# ---------------------------------------------------------------- sources
# Which staged table holds what, for the provenance report. A drawing built
# from several sources at once is only trustworthy if it can say which rows
# came from where — "combined" without provenance is just "mixed".
PROVENANCE_TABLES = (("staging_buildings", "building"),
                     ("staging_roads", "road"),
                     ("staging_context", "context"),
                     ("staging_pois", "point"),
                     ("staging_contours", "contour"),
                     ("staging_spots", "spot height"))


def provenance(conn, project_id) -> list[dict]:
    """[{source, feature_class, count}], one row per (source, class)."""
    rows = []
    for table, label in PROVENANCE_TABLES:
        for r in conn.execute(
                f"SELECT source, COUNT(*) n FROM {table}"
                f" WHERE project_id = ? GROUP BY source ORDER BY source",
                (project_id,)):
            rows.append({"source": r["source"], "feature_class": label,
                         "count": r["n"]})
    return sorted(rows, key=lambda r: (-r["count"], r["source"],
                                       r["feature_class"]))


def write_provenance_csv(path, rows) -> int:
    """The source table beside the drawing: what came from where."""
    import csv

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["source", "feature_class",
                                               "count"])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def format_provenance(rows) -> str:
    """The same table as text, for a run's console output."""
    if not rows:
        return "  (nothing staged)"
    by_source: dict[str, list] = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)
    out = []
    for source in sorted(by_source, key=lambda s: -sum(
            r["count"] for r in by_source[s])):
        total = sum(r["count"] for r in by_source[source])
        detail = ", ".join(f"{r['count']} {r['feature_class']}"
                           for r in sorted(by_source[source],
                                           key=lambda r: -r["count"]))
        out.append(f"  {source:<28} {total:>6}   {detail}")
    return "\n".join(out)


# ------------------------------------------------------------------ CLI
def show_info(conn) -> None:
    projects = conn.execute("SELECT * FROM projects ORDER BY id").fetchall()
    if not projects:
        print("No projects staged yet.")
        return
    for p in projects:
        print(f"\n[{p['id']}] {p['name']}")
        print(f"  centre {p['lat']:.8f}, {p['lon']:.8f}   "
              f"{p['width_m']:.0f} x {p['height_m']:.0f} m   "
              f"EPSG:{p['srid']}   staged {p['created_at']}")
        b = conn.execute(
            "SELECT COUNT(*) n, SUM(osm_name IS NOT NULL) named,"
            " SUM(source='microsoft_ml') ml, SUM(osm_id IS NULL) noid,"
            " ROUND(SUM(area_m2)) area FROM staging_buildings"
            " WHERE project_id = ?", (p["id"],)).fetchone()
        print(f"  buildings {b['n']}: {b['named'] or 0} named, "
              f"{b['ml'] or 0} from Microsoft ML, {b['noid'] or 0} without an "
              f"OSM id, {b['area'] or 0:,.0f} m2 footprint")
        r = conn.execute(
            "SELECT COUNT(*) n, SUM(road_name IS NOT NULL) named,"
            " SUM(road_ref IS NOT NULL) refs, ROUND(SUM(length_m)) len"
            " FROM staging_roads WHERE project_id = ?", (p["id"],)).fetchone()
        print(f"  roads {r['n']}: {r['named'] or 0} named, {r['refs'] or 0} "
              f"with a route number, {r['len'] or 0:,.0f} m centreline")
        lab = conn.execute("SELECT COUNT(*) n FROM cad_labels"
                           " WHERE project_id = ?", (p["id"],)).fetchone()
        print(f"  cad_labels view: {lab['n']} annotation objects ready")


def show_labels(conn, project_id, limit) -> None:
    print(f"-- the whole CAD annotation step is this one query --")
    print("SELECT text, label_x, label_y, label_rotation, cad_layer"
          f"\n  FROM cad_labels WHERE project_id = {project_id};\n")
    rows = conn.execute(
        "SELECT feature_class, text, label_x, label_y, label_rotation,"
        " cad_layer FROM cad_labels WHERE project_id = ?"
        " ORDER BY feature_class, text LIMIT ?",
        (project_id, limit)).fetchall()
    for r in rows:
        print(f"  {r['feature_class']:9s} {str(r['text'])[:28]:30s} "
              f"E {r['label_x']:11,.2f}  N {r['label_y']:13,.2f}  "
              f"{r['label_rotation']:6.1f}°  {r['cad_layer']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="SQLite staging database")
    ap.add_argument("--info", action="store_true", help="summarise projects")
    ap.add_argument("--labels", type=int, metavar="PROJECT_ID",
                    help="show the CAD annotation query for a project")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--set-name", action="append", metavar="FEATURE_ID=NAME",
                    help="Set a verified building name, e.g. "
                         "--set-name ms/00042=ศาลาประชาคม (repeatable)")
    ap.add_argument("--import-names", metavar="CSV",
                    help="Apply display_name values from an inventory CSV, "
                         "matched on feature_id")
    ap.add_argument("--project", metavar="ID", type=int,
                    help="Project id for --set-name / --import-names")
    a = ap.parse_args(argv)

    if not Path(a.db).exists() and not a.info:
        print(f"ERROR: {a.db} does not exist", file=sys.stderr)
        return 1
    conn = connect(a.db)
    if a.set_name or a.import_names:
        pid = a.project or conn.execute(
            "SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()["id"]
        updates = {}
        for pair in (a.set_name or []):
            fid, _, name = pair.partition("=")
            if not name:
                print(f"ERROR: expected FEATURE_ID=NAME, got '{pair}'",
                      file=sys.stderr)
                return 1
            updates[fid.strip()] = name.strip()
        if a.import_names:
            import csv
            with open(a.import_names, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                cols = set(reader.fieldnames or [])
                if not {"feature_id", "display_name"} <= cols:
                    print("ERROR: CSV needs feature_id and display_name "
                          "columns", file=sys.stderr)
                    return 1
                for row in reader:
                    fid = (row["feature_id"] or "").strip()
                    name = (row["display_name"] or "").strip()
                    # Codes are placeholders, not verified names
                    if fid and name and not re.fullmatch(r"B\d{3,}", name):
                        updates[fid] = name
        applied = missed = 0
        for fid, name in updates.items():
            th, en = split_by_script(name)
            cur = conn.execute(
                "UPDATE staging_buildings SET display_name = ?, osm_name ="
                " COALESCE(osm_name, ?), name_th = ?, name_en = ?"
                " WHERE project_id = ? AND feature_id = ?",
                (name, name, th, en, pid, fid))
            if cur.rowcount:
                record_verified(conn, pid, fid, name)
            applied += cur.rowcount
            missed += (cur.rowcount == 0)
        conn.commit()
        print(f"Applied {applied} name(s) to project {pid}"
              + (f"; {missed} feature_id(s) matched nothing" if missed else ""))
        return 0
    if a.labels is not None:
        show_labels(conn, a.labels, a.limit)
    else:
        show_info(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
