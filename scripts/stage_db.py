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
    -- Route number. Unnamed roads carry the Thai 'ทล.' prefix (matching
    -- topo2cad.py), which puts that form on the Thai layer; a bare number
    -- alongside a name is Latin. Offset clears the name stack above it.
    SELECT project_id, 'road_ref',
           CASE WHEN road_name IS NULL THEN 'ทล.' || road_ref
                ELSE road_ref END,
           label_x, label_y, label_rotation, 4.0,
           CASE WHEN road_name IS NULL THEN 'C-ANNO-TEXT-TH'
                ELSE 'C-ANNO-TEXT-EN' END,
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
                      ("oneway", "INTEGER NOT NULL DEFAULT 0")),
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


# Mirrors is_thai() in topo2cad.py — U+0E00–U+0E7F is the Thai block. Kept
# local so this module stays importable without the CAD/DEM stack.
THAI_RE = re.compile(r"[฀-๿]")


def is_thai(text) -> bool:
    """True if the string contains any Thai character."""
    return bool(text) and bool(THAI_RE.search(str(text)))


def split_by_script(name, name_th=None, name_en=None):
    """Resolve (name_th, name_en) for a staged feature.

    An explicit tag wins. Otherwise the single resolved name is filed by its
    own script, so a caller that knows nothing about languages — an older
    script, or a test — still gets its label onto the right layer instead of
    losing it.
    """
    th = name_th or None
    en = name_en or None
    if name and not (th or en):
        if is_thai(name):
            th = name
        else:
            en = name
    return th, en


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


def interior_point(geom):
    """A point guaranteed inside the polygon (centroids are not)."""
    pt = geom.representative_point()
    return pt.x, pt.y


# Coordinate grid. A survey sheet carries one so a reader can pick a
# northing and easting off the paper; this repo drew none, which is the
# sort of omission a reviewer notices before anything else.
GRID_STEPS = (10.0, 20.0, 25.0, 50.0, 100.0, 200.0, 250.0, 500.0, 1000.0,
              2000.0, 5000.0)


def grid_spacing(width_m, height_m, target=6):
    """A round grid interval giving roughly `target` lines across the
    extent — the same family of round numbers a scale bar uses, because a
    grid at 137 m is a grid nobody can read a coordinate off."""
    span = max(float(width_m), float(height_m))
    ideal = span / max(target, 1)
    for step in GRID_STEPS:
        if step >= ideal:
            return step
    return GRID_STEPS[-1]


def grid_ticks(centre_x, centre_y, width_m, height_m, spacing):
    """(eastings, northings) of the grid lines inside the extent.

    Round UTM values, not offsets from the centre: a grid line at
    665,700 E is a number a surveyor can use, one at 665,694.02 is not.
    Computed from the nominal extent so all three writers — which each know
    only the centre and the metres — agree without sharing geometry.
    """
    if spacing <= 0:
        return [], []
    west, east = centre_x - width_m / 2, centre_x + width_m / 2
    south, north = centre_y - height_m / 2, centre_y + height_m / 2

    def series(lo, hi):
        first = math.ceil(lo / spacing) * spacing
        out, value = [], first
        while value <= hi:
            out.append(round(value, 6))
            value += spacing
        return out

    return series(west, east), series(south, north)


# Hatch pattern per context kind, at a scale that reads between 1:500 and
# 1:5000. ANSI31 is the CAD convention for water and AR-SAND for ground
# cover, both in ezdxf's standard pattern table, so a drafter sees the fill
# AutoCAD would draw. It lives here rather than in topo2cad because
# db2dxf.py hatches the same rows and must not import the Overpass side to
# find out how.
HATCH_PATTERNS = {"water": ("ANSI31", 4.0), "green": ("AR-SAND", 0.6)}


def hatch_area(msp, points, kind, layer):
    """Fill one closed run with the pattern its kind uses.

    Not associative: the boundary is a separate polyline on its own layer,
    and a drafter editing the outline expects to refresh the hatch rather
    than have it silently follow.
    """
    pattern, scale = HATCH_PATTERNS[kind]
    hatch = msp.add_hatch(dxfattribs={"layer": layer})
    hatch.set_pattern_fill(pattern, scale=scale)
    hatch.paths.add_polyline_path(points, is_closed=True)
    return hatch


def levels_label(tags) -> str:
    """Storeys or height as a drafter writes it: "3F", or "12.0 m".

    Stored formatted rather than as two numeric columns, deliberately. The
    alternative is a rule in Python for the extraction route and the same
    rule again in the cad_labels view for the re-issue, and two spellings
    of one convention is exactly the drift this layer exists to prevent.

    `building:levels` wins over `height`: a storey count is what a site
    plan annotates, and it survives a mapper who guessed the metres.
    """
    levels = str(tags.get("building:levels", "")).strip()
    if levels:
        try:
            count = float(levels)
        except ValueError:
            count = 0.0
        if 0 < count < 200:
            return f"{count:g}F"
    raw = str(tags.get("height", "")).strip().lower().removesuffix("m").strip()
    try:
        metres = float(raw)
    except ValueError:
        return ""
    return f"{metres:.1f} m" if 0 < metres < 1000 else ""


def repaired_polygon(exterior, holes=()):
    """The polygon a writer should draw *and* stage.

    OSM carries self-intersecting rings — a university boundary that crosses
    itself, a building traced in a bow tie — and `buffer(0)` splits those
    into two polygons. The extraction routes used to draw the raw ring while
    staging the repaired one, so the drawing had one outline where its
    re-issue had two, with the label 97 m away in a different lobe. Both go
    through this, so what is drawn is what is stored.
    """
    from shapely.geometry import Polygon

    poly = Polygon(exterior, list(holes) if holes else None)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly


def polygon_parts(geom):
    """The Polygon pieces of a repaired shape, in db2dxf.py's draw order."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", ())
            if g.geom_type == "Polygon" and not g.is_empty]


def line_label_anchor(geom):
    """Midpoint of the longest part, plus an upright rotation in degrees."""
    parts = [g for g in (geom.geoms if geom.geom_type.startswith("Multi")
                         else [geom]) if not g.is_empty]
    if not parts:
        return None, None, 0.0
    line = max(parts, key=lambda g: g.length)
    if line.length <= 0:
        return None, None, 0.0
    mid = line.interpolate(0.5, normalized=True)
    a = line.interpolate(max(0.0, 0.5 - 0.05), normalized=True)
    b = line.interpolate(min(1.0, 0.5 + 0.05), normalized=True)
    ang = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
    if ang > 90:
        ang -= 180
    elif ang < -90:
        ang += 180
    return mid.x, mid.y, ang


# Direction-of-travel arrows on one-way carriageways. The rule lives here,
# beside line_label_anchor(), for the same reason: all three CAD writers
# place them, and a drawing whose re-issue puts the arrows somewhere else is
# a drawing nobody can check against its own revision.
ONEWAY_SPACING_M = 60.0        # along the run, between arrows
ONEWAY_MIN_RUN_M = 12.0        # shorter than this, a run gets none
ONEWAY_ARROW_MIN_M = 3.0
ONEWAY_ARROW_MAX_M = 10.0


# Water flows the way the OSM way is digitised — that is the convention
# waterway=* relies on, and it is the only direction information a canal
# carries. Drawn with the same arrow at a fixed size, because a canal has
# no carriageway width to scale from.
FLOW_ARROW_M = 5.0


def oneway_arrow_size(carriageway_m) -> float:
    """Arrow length for a carriageway width, clamped so a 14 m motorway
    does not get a 14 m arrow and a 3 m alley does not get an invisible one."""
    width = carriageway_m or 0.0
    return max(ONEWAY_ARROW_MIN_M, min(float(width), ONEWAY_ARROW_MAX_M))


def arrow_positions(coords, spacing=ONEWAY_SPACING_M,
                    min_length=ONEWAY_MIN_RUN_M):
    """[(x, y, bearing_degrees), ...] along a polyline, in its own direction.

    Arrows are spaced by distance along the line rather than one per vertex:
    an OSM way carries a vertex every few metres through a curve, so
    per-vertex arrows would pile up on bends and vanish on straights. The
    first sits half a spacing in, so a run never opens with an arrow sitting
    on the junction it starts at. A run shorter than `min_length` gets none
    — there is no room to read one — and anything between that and a full
    spacing gets exactly one, at its midpoint.

    The bearing is the direction of travel *as digitised*; a caller with
    `oneway=-1` adds 180.
    """
    pts = [(float(x), float(y)) for x, y in coords]
    if len(pts) < 2:
        return []
    segs, total = [], 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        if length <= 0:
            continue
        segs.append((total, length, x1, y1, x2, y2))
        total += length
    if not segs or total < min_length:
        return []
    if total < spacing:
        marks = [total / 2]
    else:
        marks = []
        d = spacing / 2
        while d < total:
            marks.append(d)
            d += spacing
    out = []
    for mark in marks:
        for start, length, x1, y1, x2, y2 in segs:
            if mark <= start + length or (start, length) == segs[-1][:2]:
                t = min(max((mark - start) / length, 0.0), 1.0)
                out.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t,
                            math.degrees(math.atan2(y2 - y1, x2 - x1))))
                break
    return out


# ------------------------------------------------------- source attributes
# Extended entity data, the AutoCAD mechanism an OSM importer uses to hang
# the source tags off each entity: select a building, LIST it, and the tags
# are there. Group code 1000 is a string capped at 255 *bytes* — Thai is
# three bytes a character, so the cap is applied after encoding.
XDATA_APPID = "OSM"          # OpenStreetMap tags
GIS_XDATA_APPID = "GIS"      # fields of a file the user supplied
XDATA_MAX_TAGS = 40
ATTR_FIELDS = ["feature_id", "feature_type", "cad_layer", "display_name",
               "key", "value"]


def clip_bytes(text, limit: int = 255) -> str:
    raw = str(text).encode("utf-8")
    if len(raw) <= limit:
        return str(text)
    return raw[:limit].decode("utf-8", "ignore")


def xdata_tags(feature_id: str, tags: dict, max_tags: int = XDATA_MAX_TAGS):
    """[(1000, 'key=value'), ...] for one feature, id first.

    Sorted, so two runs of the same source produce byte-identical drawings.
    A long tag list is truncated with a marker rather than silently: AutoCAD
    caps XDATA at 16 KB per entity, and a machine-generated `source:...`
    history can approach it. The attribute CSV carries the full set.
    """
    out = [(1000, clip_bytes(f"@id={feature_id}"))]
    items = sorted(tags.items())
    for key, value in items[:max_tags]:
        out.append((1000, clip_bytes(f"{key}={value}")))
    if len(items) > max_tags:
        out.append((1000, f"@truncated={len(items) - max_tags} more tags"))
    return out


def attribute_rows(drawn, tags_by_id):
    """One row per (drawn feature, tag), sorted — the attribute table.

    `drawn` describes what reached the drawing, so a feature dropped by a
    type filter or a crop is absent from the table too: it documents the
    DXF, not the source it came from.
    """
    rows = []
    for rec in drawn:
        for key, value in sorted(tags_by_id.get(rec["feature_id"], {}).items()):
            rows.append({**rec, "key": key, "value": value})
    return sorted(rows, key=lambda r: (r["feature_id"], r["key"]))


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


def spot_grid(west, south, east, north, columns=5, rows_n=5):
    """Sample points for spot heights, inset from the extent.

    A grid rather than a scatter: a reviewer reads levels across a site by
    comparing neighbours, and the inset keeps the numbers off the crop line
    where the frame or the title block would sit on them.
    """
    if columns < 1 or rows_n < 1:
        return []
    span_x, span_y = east - west, north - south
    step_x, step_y = span_x / (columns + 1), span_y / (rows_n + 1)
    return [(west + step_x * (i + 1), south + step_y * (j + 1))
            for j in range(rows_n) for i in range(columns)]


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


def write_attribute_csv(path, rows) -> int:
    """The attribute table beside the drawing, for review outside CAD."""
    import csv

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ATTR_FIELDS)
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in ATTR_FIELDS} for r in rows)
    return len(rows)


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
            # display_name is what a writer draws; the B### code is a
            # handle for field work and stays in its own column. Falling
            # back to the code here put "B001" on the re-issue of a
            # drawing whose extraction had drawn nothing.
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


# How far the landmark name sits from its symbol, in metres. Must match the
# `px + 3` in topo2cad.py, or the two CAD routes place the label differently.
POI_LABEL_DX = 3.0


def stage_pois(conn, project_id, records) -> int:
    """records: dicts with feature_id, x, y (project SRID), poi_key,
    poi_type, display_name and optionally name_th/name_en."""
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
            shp_wkb.dumps(Point(x, y)), x + POI_LABEL_DX, y,
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
            r.get("cad_layer", "C-ROAD-CNTR"),
            r.get("carriageway_m"), int(r.get("oneway") or 0),
            shp_wkb.dumps(geom),
            lx, ly, rot, geom.length, minx, miny, maxx, maxy))
    conn.executemany(
        "INSERT OR REPLACE INTO staging_roads (project_id, feature_id, osm_id,"
        " source, highway_type, road_name, road_ref, display_name,"
        " name_th, name_en,"
        " cad_layer, carriageway_m, oneway,"
        " geom_wkb, label_x, label_y, label_rotation, length_m,"
        " minx, miny, maxx, maxy)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return len(rows)


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


# What each staged source is called on a printed sheet. The title block
# credits the data, and a sheet carrying a survey boundary, Microsoft
# footprints and Overture places while crediting only OpenStreetMap is
# wrong in both directions: it credits a source that did not supply the
# line, and it fails to credit the ones that did.
SOURCE_CREDITS = {
    "openstreetmap": "OpenStreetMap contributors (ODbL)",
    "microsoft_ml": "Microsoft ML footprints (ODbL)",
    "overture": "Overture Maps (ODbL/CDLA)",
    "copernicus_dem": "Copernicus DEM (ESA)",
}
# Characters that fit one line of the title block's smallest text at the
# narrowest sheet. An honest credit that overruns the frame is not on the
# sheet at all, so the lines are wrapped rather than trusted to fit.
CREDIT_WIDTH = 46


def credit_lines(sources, max_files: int = 2,
                 width: int = CREDIT_WIDTH) -> list[str]:
    """Attribution lines for a title block, from staged source names.

    Sources are recorded per file (`user_gis:boundary.geojson`), so the
    prefix decides the credit and the file names are listed after it — up
    to `max_files`, because a title block is 55 mm wide and an honest line
    that overruns the frame is not on the sheet at all.
    """
    order = list(SOURCE_CREDITS)
    known, files, osm_files = [], [], []
    for source in sorted(set(s for s in sources if s),
                         key=lambda s: (order.index(s.partition(":")[0])
                                        if s.partition(":")[0] in order
                                        else len(order), s)):
        head, _, rest = source.partition(":")
        credit = SOURCE_CREDITS.get(head)
        if head == "user_gis":
            files.append(rest or "supplied file")
        elif credit:
            if rest:
                osm_files.append(rest)
            if credit not in known:
                known.append(credit)
        else:                       # a source added later, named honestly
            known.append(head)
    import textwrap

    lines = []
    if known:
        lines += textwrap.wrap("Data © " + "; ".join(known), width,
                               subsequent_indent="   ")
    if osm_files:
        shown = osm_files[:max_files]
        more = len(osm_files) - len(shown)
        lines += textwrap.wrap("OSM extract: " + ", ".join(shown)
                               + (f" +{more} more" if more else ""), width,
                               subsequent_indent="   ")
    if files:
        shown = files[:max_files]
        more = len(files) - len(shown)
        lines += textwrap.wrap("Supplied survey data: " + ", ".join(shown)
                               + (f" +{more} more" if more else ""), width,
                               subsequent_indent="   ")
    return lines


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
