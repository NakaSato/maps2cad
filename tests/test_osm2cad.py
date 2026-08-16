"""Tests for the OSM file route (scripts/osm2cad.py).

The parser is the part with no equivalent elsewhere in the repo: a raw .osm
file carries node *references* where Overpass hands back coordinates, so the
geometry is stitched together here and everything downstream — the tag rules,
the layers, the label placement — is topo2cad.py's, shared rather than
copied. These tests pin the stitching, the file-format handling and the
import options, plus one end-to-end run that proves the drawing comes out
with the same NCS layers the other CAD routes write.
"""

import bz2
import gzip
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import osm2cad  # noqa: E402
from topo2cad import classify_elements  # noqa: E402


SAMPLE = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test">
  <bounds minlat="15.8300" minlon="104.3900"
          maxlat="15.8380" maxlon="104.3990"/>
  <node id="1" lat="15.8320" lon="104.3920"/>
  <node id="2" lat="15.8322" lon="104.3920"/>
  <node id="3" lat="15.8322" lon="104.3924"/>
  <node id="4" lat="15.8320" lon="104.3924"/>
  <node id="10" lat="15.8310" lon="104.3910"/>
  <node id="11" lat="15.8350" lon="104.3960"/>
  <node id="20" lat="15.8340" lon="104.3950">
    <tag k="amenity" v="place_of_worship"/>
    <tag k="name" v="วัดทดสอบ"/>
    <tag k="name:en" v="Test Temple"/>
  </node>
  <node id="21" lat="15.8341" lon="104.3951">
    <tag k="amenity" v="cafe"/>
    <tag k="name" v="Corner Cafe"/>
  </node>
  <way id="100">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="1"/>
    <tag k="building" v="yes"/>
    <tag k="name" v="อาคารเรียน"/>
  </way>
  <way id="200">
    <nd ref="10"/><nd ref="11"/>
    <tag k="highway" v="residential"/>
    <tag k="name" v="ถนนทดสอบ"/>
    <tag k="ref" v="202"/>
  </way>
  <way id="300">
    <nd ref="10"/><nd ref="11"/>
    <tag k="highway" v="footway"/>
  </way>
  <way id="400">
    <nd ref="1"/><nd ref="999"/>
    <tag k="building" v="yes"/>
  </way>
</osm>
"""

# A multipolygon: the outer and inner rings are untagged ways, and only the
# relation carries `building`. Drawing the member ways as well would double
# every wall, which is why untagged ways are geometry only.
MULTIPOLYGON = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
  <node id="1" lat="0.0000" lon="0.0000"/>
  <node id="2" lat="0.0010" lon="0.0000"/>
  <node id="3" lat="0.0010" lon="0.0010"/>
  <node id="4" lat="0.0000" lon="0.0010"/>
  <node id="5" lat="0.0004" lon="0.0004"/>
  <node id="6" lat="0.0006" lon="0.0004"/>
  <node id="7" lat="0.0006" lon="0.0006"/>
  <node id="8" lat="0.0004" lon="0.0006"/>
  <way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/>
    <nd ref="1"/></way>
  <way id="11"><nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/>
    <nd ref="5"/></way>
  <relation id="50">
    <member type="way" ref="10" role="outer"/>
    <member type="way" ref="11" role="inner"/>
    <tag k="type" v="multipolygon"/>
    <tag k="building" v="temple"/>
    <tag k="name" v="วัดมีลานกลาง"/>
  </relation>
</osm>
"""


def write(tmp_path, text=SAMPLE, name="map.osm"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def read(path):
    with osm2cad.osm_stream(Path(path)) as (head, stream):
        return osm2cad.parse_osm(head, stream)


# ------------------------------------------------------------------ parsing
def test_parse_resolves_way_geometry_from_node_refs(tmp_path):
    elements, stats = read(write(tmp_path))
    ways = {el["id"]: el for el in elements if el["type"] == "way"}
    assert [(g["lon"], g["lat"]) for g in ways[100]["geometry"]][0] == \
        (104.3920, 15.8320)
    assert len(ways[100]["geometry"]) == 5          # closed ring
    assert stats["bounds"] == (15.8300, 104.3900, 15.8380, 104.3990)


def test_parse_skips_ways_whose_nodes_are_not_in_the_extract(tmp_path):
    """An export cuts through ways at the box edge; a way left with one
    coordinate is dropped and counted, never drawn short."""
    elements, stats = read(write(tmp_path))
    assert 400 not in {el["id"] for el in elements if el["type"] == "way"}
    assert stats["incomplete_ways"] == 1


def test_parse_keeps_untagged_ways_out_of_the_elements(tmp_path):
    """Untagged ways are a multipolygon's building material, not features."""
    elements, _ = read(write(tmp_path, MULTIPOLYGON))
    assert [el["type"] for el in elements] == ["relation"]
    rel = elements[0]
    assert [m["role"] for m in rel["members"]] == ["outer", "inner"]
    assert len(rel["members"][0]["geometry"]) == 5


def test_multipolygon_courtyard_survives_classification(tmp_path):
    """The inner ring has to reach classify_elements() as a hole, or a
    temple draws with its courtyard filled in solid."""
    elements, _ = read(write(tmp_path, MULTIPOLYGON))
    buildings = classify_elements(elements)["buildings"]
    (th, en), (outer, holes), fid = buildings[0]
    assert th == "วัดมีลานกลาง" and fid == "relation/50"
    assert len(outer) == 5 and len(holes) == 1


def test_classification_matches_the_overpass_route(tmp_path):
    """Same tags, same buckets: the file route and fetch_osm() share the
    rules, including the curated POI filter that drops the cafe."""
    elements, _ = read(write(tmp_path))
    f = classify_elements(elements)
    assert [b[2] for b in f["buildings"]] == ["way/100"]
    assert {r[3] for r in f["roads"]} == {"residential", "footway"}
    assert [p[0] for p in f["pois"]] == [("วัดทดสอบ", "Test Temple")]
    assert [th or en for (th, en), *_ in classify_elements(
        elements, curated=False)["pois"]] == ["วัดทดสอบ", "Corner Cafe"]


def test_parse_reads_the_osmosis_bound_element(tmp_path):
    path = write(tmp_path, SAMPLE.replace(
        '<bounds minlat="15.8300" minlon="104.3900"\n'
        '          maxlat="15.8380" maxlon="104.3990"/>',
        '<bound box="15.83,104.39,15.838,104.399"/>'))
    _, stats = read(path)
    assert stats["bounds"] == (15.83, 104.39, 15.838, 104.399)


# ------------------------------------------------------------- file formats
@pytest.mark.parametrize("suffix,pack", [
    (".osm.gz", lambda p, d: p.write_bytes(gzip.compress(d))),
    (".osm.bz2", lambda p, d: p.write_bytes(bz2.compress(d))),
])
def test_compressed_files_read_the_same(tmp_path, suffix, pack):
    path = tmp_path / f"map{suffix}"
    pack(path, SAMPLE.encode())
    elements, _ = read(path)
    assert any(el["id"] == 100 for el in elements)


def test_zip_archive_reads_its_first_osm_member(tmp_path):
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("readme.txt", "ignore me")
        z.writestr("area.osm", SAMPLE)
    elements, _ = read(path)
    assert any(el["id"] == 100 for el in elements)


def test_zip_without_an_osm_member_is_refused(tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("readme.txt", "nothing")
    with pytest.raises(osm2cad.OsmFileError, match="no .osm"):
        read(path)


def test_pbf_is_refused_with_the_conversion_command(tmp_path):
    """Protobuf would cost a dependency; say how to convert instead."""
    path = tmp_path / "thailand-latest.osm.pbf"
    path.write_bytes(b"\x00\x00\x00\x0d\n\x09OSMHeader")
    with pytest.raises(osm2cad.OsmFileError, match="osmium cat"):
        read(path)


def test_pbf_content_under_an_osm_name_is_still_caught(tmp_path):
    path = tmp_path / "renamed.osm"
    path.write_bytes(b"\x00\x00\x00\x0d\n\x09OSMHeader" + b"\x00" * 40)
    with pytest.raises(osm2cad.OsmFileError, match="osmium cat"):
        read(path)


def test_a_file_that_is_not_osm_says_so(tmp_path):
    path = tmp_path / "notes.osm"
    path.write_text("just some text")
    with pytest.raises(osm2cad.OsmFileError, match="OSM XML"):
        read(path)


def test_several_files_merge_and_deduplicate_by_id(tmp_path):
    """Overlapping exports share OSM ids; the same way is one feature."""
    a = write(tmp_path, SAMPLE, "a.osm")
    b = write(tmp_path, SAMPLE, "b.osm")
    elements, stats = osm2cad.read_osm_files([a, b])
    assert sum(1 for el in elements if el["id"] == 100) == 1
    assert stats["bounds"] == (15.8300, 104.3900, 15.8380, 104.3990)


# ------------------------------------------------------------------ extents
def test_element_bounds_covers_nodes_ways_and_relations(tmp_path):
    elements, _ = read(write(tmp_path))
    s, w, n, e = osm2cad.element_bounds(elements)
    assert (round(s, 4), round(w, 4)) == (15.8310, 104.3910)
    assert (round(n, 4), round(e, 4)) == (15.8350, 104.3960)


def test_nominal_extent_matches_bbox_around(tmp_path):
    """The crop rectangle, the staged extent and db2dxf.py's re-issue are all
    sized from this, so it has to be bbox_around()'s arithmetic run
    backwards — not a more exact figure that lands 2 m away."""
    from topo2cad import bbox_around

    box = bbox_around(15.8338, 104.3945, None, 1000.0, 750.0)
    width, height = osm2cad.nominal_extent(box)
    assert width == pytest.approx(1000.0, abs=0.5)
    assert height == pytest.approx(750.0, abs=0.5)


def test_parse_bbox_rejects_an_inside_out_box():
    assert osm2cad.parse_bbox("15.83,104.39,15.84,104.40") == \
        (15.83, 104.39, 15.84, 104.40)
    with pytest.raises(osm2cad.OsmFileError, match="inside out"):
        osm2cad.parse_bbox("15.84,104.39,15.83,104.40")
    with pytest.raises(osm2cad.OsmFileError, match="four numbers"):
        osm2cad.parse_bbox("15.84,104.39")


def test_intersects_box_keeps_a_footprint_straddling_the_line():
    box = (15.83, 104.39, 15.84, 104.40)
    straddling = [(104.3999, 15.8395), (104.4009, 15.8395)]
    assert osm2cad.intersects_box(straddling, box)
    assert not osm2cad.intersects_box([(104.5, 15.9)], box)


# ------------------------------------------------------------ import options
def test_select_types_splits_carriageways_from_paths(tmp_path):
    elements, _ = read(write(tmp_path))
    f = classify_elements(elements)
    roads = osm2cad.select_types(f, {"road"})["roads"]
    assert [r[3] for r in roads] == ["residential"]
    paths = osm2cad.select_types(f, {"path"})["roads"]
    assert [r[3] for r in paths] == ["footway"]
    assert osm2cad.select_types(f, {"road"})["buildings"] == []
    # None means "import everything", the default
    assert len(osm2cad.select_types(f, None)["roads"]) == 2


def test_select_types_landmarks_covers_points_and_grounds(tmp_path):
    elements, _ = read(write(tmp_path))
    f = classify_elements(elements)
    assert osm2cad.select_types(f, {"building"})["pois"] == []
    assert len(osm2cad.select_types(f, {"landmark"})["pois"]) == 1


@pytest.mark.parametrize("value,expected", [
    ("residential", "C-ROAD-CNTR-RESIDENTIAL"),
    ("place_of_worship", "C-ROAD-CNTR-PLACE_OF_WORSHIP"),
    ("motorway link", "C-ROAD-CNTR-MOTORWAY-LINK"),
    ('bad<>:"/\\|?*name', "C-ROAD-CNTR-BAD-NAME"),
    (None, "C-ROAD-CNTR"),
    ("", "C-ROAD-CNTR"),
])
def test_layer_variant_stays_a_usable_dxf_layer_name(value, expected):
    assert osm2cad.layer_variant("C-ROAD-CNTR", value) == expected


def test_layer_variant_keeps_the_ncs_prefix():
    """A suffix, not a name of its own: freezing C-ROAD-CNTR* still catches
    every split layer."""
    assert osm2cad.layer_variant("C-ROAD-CNTR", "x").startswith("C-ROAD-CNTR")


# -------------------------------------------------------------------- XDATA
def test_xdata_carries_the_id_first_then_sorted_tags():
    data = osm2cad.xdata_tags("way/100", {"name": "A", "building": "yes"})
    assert data == [(1000, "@id=way/100"), (1000, "building=yes"),
                    (1000, "name=A")]


def test_xdata_clips_on_bytes_not_characters():
    """Group code 1000 caps at 255 bytes and Thai is three bytes a
    character, so a character-count clip would still overrun."""
    long_thai = "ก" * 300
    value = osm2cad.xdata_tags("n/1", {"name": long_thai})[1][1]
    assert len(value.encode("utf-8")) <= 255
    assert value.startswith("name=ก")


def test_xdata_truncation_is_reported_not_silent():
    tags = {f"k{i:03d}": "v" for i in range(60)}
    data = osm2cad.xdata_tags("way/1", tags, max_tags=10)
    assert data[-1][1] == "@truncated=50 more tags"
    assert len(data) == 12                      # id + 10 tags + the marker


def test_tags_for_falls_back_past_a_relation_part_suffix():
    index = {"relation/50": {"building": "yes"}}
    assert osm2cad.tags_for(index, "relation/50/1") == {"building": "yes"}
    assert osm2cad.tags_for(index, "way/9") == {}


# ---------------------------------------------------------- attribute table
def test_attribute_rows_are_one_row_per_tag_sorted():
    """Long format, so a fence with three tags and a mall with forty share
    one table instead of a sparse sheet hundreds of columns wide."""
    drawn = [{"feature_id": "way/2", "feature_type": "road",
              "cad_layer": "C-ROAD-CNTR", "display_name": "ถนน"},
             {"feature_id": "way/1", "feature_type": "building",
              "cad_layer": "C-BLDG-OUTL", "display_name": "B001"}]
    index = {"way/1": {"building": "yes", "amenity": "school"},
             "way/2": {"highway": "residential"}}
    rows = osm2cad.attribute_rows(drawn, index)
    assert [(r["feature_id"], r["key"]) for r in rows] == [
        ("way/1", "amenity"), ("way/1", "building"), ("way/2", "highway")]
    assert rows[0]["cad_layer"] == "C-BLDG-OUTL"
    assert set(rows[0]) == set(osm2cad.ATTR_FIELDS)


def test_attribute_rows_describe_the_drawing_not_the_file():
    """A feature dropped by --types or --bbox was not drawn, so it has no
    business in a table of what the drawing contains."""
    index = {"way/1": {"building": "yes"}, "way/9": {"highway": "service"}}
    rows = osm2cad.attribute_rows(
        [{"feature_id": "way/1", "feature_type": "building",
          "cad_layer": "C-BLDG-OUTL", "display_name": ""}], index)
    assert {r["feature_id"] for r in rows} == {"way/1"}


def test_attribute_table_is_complete_where_xdata_is_capped(tmp_path):
    """XDATA stops at XDATA_MAX_TAGS per entity; the CSV must not."""
    tags = {f"k{i:03d}": "v" for i in range(osm2cad.XDATA_MAX_TAGS + 5)}
    rows = osm2cad.attribute_rows(
        [{"feature_id": "way/1", "feature_type": "building",
          "cad_layer": "C-BLDG-OUTL", "display_name": ""}], {"way/1": tags})
    assert len(rows) == len(tags)
    assert len(osm2cad.xdata_tags("way/1", tags)) < len(tags)


def test_main_writes_the_attribute_table(tmp_path):
    import csv as csv_mod

    pytest.importorskip("ezdxf")
    out = tmp_path / "site.dxf"
    assert osm2cad.main(["--input", str(write(tmp_path)),
                         "--out", str(out)]) == 0
    with open(tmp_path / "attributes.csv", encoding="utf-8") as f:
        rows = list(csv_mod.DictReader(f))
    assert [r for r in rows if r["feature_id"] == "way/100"
            and r["key"] == "building"][0]["value"] == "yes"
    kinds = {r["feature_type"] for r in rows}
    assert {"building", "road", "path", "landmark"} <= kinds
    # The cafe was filtered out of the drawing, so it is not in the table
    assert not [r for r in rows if r["value"] == "Corner Cafe"]


# --------------------------------------------------------------- end to end
def test_main_writes_a_drawing_on_the_shared_ncs_layers(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    out = tmp_path / "site.dxf"
    assert osm2cad.main(["--input", str(write(tmp_path)), "--out", str(out),
                         "--layer-by", "highway"]) == 0
    doc = ezdxf.readfile(out)
    layers = {layer.dxf.name for layer in doc.layers}
    assert {"C-BLDG-OUTL", "C-ROAD-CNTR", "C-ROAD-EDGE", "C-ROAD-PATH",
            "C-ANNO-TEXT-TH", "C-ANNO-EXTN"} <= layers
    # --layer-by splits by tag value, inheriting the parent's style
    assert "C-ROAD-CNTR-RESIDENTIAL" in layers
    assert doc.layers.get("C-ROAD-CNTR-RESIDENTIAL").dxf.color == \
        doc.layers.get("C-ROAD-CNTR").dxf.color

    msp = doc.modelspace()
    # Drawn in UTM metres, not degrees: a 4-node building is metres across
    building = [e for e in msp
                if e.dxf.layer == "C-BLDG-OUTL"][0]
    xs = [p[0] for p in building.get_points()]
    assert 1.0 < max(xs) - min(xs) < 100.0
    # The OSM tags ride along as extended data
    assert building.get_xdata("OSM")[1].value == "building=yes"
    assert (tmp_path / "building_inventory.csv").is_file()


ONEWAY_SAMPLE = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
  <bounds minlat="13.7440" minlon="100.5310"
          maxlat="13.7470" maxlon="100.5340"/>
  <node id="1" lat="13.7445" lon="100.5315"/>
  <node id="2" lat="13.7445" lon="100.5335"/>
  <node id="3" lat="13.7460" lon="100.5315"/>
  <node id="4" lat="13.7460" lon="100.5335"/>
  <way id="10"><nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="primary"/><tag k="oneway" v="yes"/></way>
  <way id="20"><nd ref="3"/><nd ref="4"/>
    <tag k="highway" v="primary"/><tag k="oneway" v="-1"/></way>
</osm>
"""


def test_oneway_arrows_point_with_and_against_the_geometry(tmp_path):
    """Both ways run west to east; the second is tagged oneway=-1, so its
    arrows must point the other way. Getting this backwards aims every
    arrow on a sliproad at oncoming traffic."""
    ezdxf = pytest.importorskip("ezdxf")
    out = tmp_path / "oneway.dxf"
    assert osm2cad.main([
        "--input", str(write(tmp_path, ONEWAY_SAMPLE, "ow.osm")),
        "--out", str(out)]) == 0
    doc = ezdxf.readfile(out)
    arrows = [e for e in doc.modelspace()
              if e.dxftype() == "INSERT" and e.dxf.layer == "C-ROAD-ARRW"]
    assert arrows, "a one-way road drew no direction arrows"
    rotations = {round(e.dxf.rotation) % 360 for e in arrows}
    assert rotations == {0, 180}
    assert all(e.dxf.name == "ONEWAY_ARROW" for e in arrows)
    # Sized from the carriageway, not left at unit scale
    assert all(e.dxf.xscale > 1 for e in arrows)


def test_two_way_roads_get_no_arrows(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    out = tmp_path / "plain.dxf"
    assert osm2cad.main(["--input", str(write(tmp_path)), "--out",
                         str(out)]) == 0
    doc = ezdxf.readfile(out)
    assert not [e for e in doc.modelspace()
                if e.dxf.layer == "C-ROAD-ARRW"]


def test_main_bbox_drops_features_outside_the_box(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    out = tmp_path / "cropped.dxf"
    assert osm2cad.main([
        "--input", str(write(tmp_path)), "--out", str(out),
        "--bbox", "15.8300,104.3900,15.8325,104.3930"]) == 0
    doc = ezdxf.readfile(out)
    # The temple node sits at 15.8340,104.3950 — outside the crop
    texts = {e.text for e in doc.modelspace()
             if e.dxftype() == "MTEXT"}
    assert not any("วัดทดสอบ" in t for t in texts)
    assert any("อาคารเรียน" in t for t in texts)


def test_main_without_attributes_writes_no_xdata(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    out = tmp_path / "plain.dxf"
    assert osm2cad.main(["--input", str(write(tmp_path)), "--out", str(out),
                         "--no-attributes"]) == 0
    doc = ezdxf.readfile(out)
    building = [e for e in doc.modelspace()
                if e.dxf.layer == "C-BLDG-OUTL"][0]
    # has_xdata, not get_xdata: ezdxf raises for an appid that was never
    # attached rather than returning None
    assert not building.has_xdata("OSM")
    assert "OSM" not in doc.appids


def test_main_stages_so_db2dxf_can_reissue(tmp_path):
    pytest.importorskip("ezdxf")
    db = tmp_path / "staging.sqlite"
    assert osm2cad.main(["--input", str(write(tmp_path)),
                         "--out", str(tmp_path / "site.dxf"),
                         "--db", str(db), "--project", "file-import"]) == 0
    import stage_db
    conn = stage_db.connect(db)
    pid = conn.execute("SELECT id FROM projects WHERE name = 'file-import'"
                       ).fetchone()[0]
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE"
                            " project_id = ?", (pid,)).fetchone()[0]
        for table in ("staging_buildings", "staging_roads", "staging_pois")}
    conn.close()
    assert counts == {"staging_buildings": 1, "staging_roads": 2,
                      "staging_pois": 1}


# --------------------------------------------------------- staging a project
def test_repeated_imports_merge_into_one_project(tmp_path):
    """Step 6 of the usual import workflow — bring in one feature type at a
    time from the same file. Replacing on the second run silently threw away
    the first, which is what this used to do."""
    pytest.importorskip("ezdxf")
    import stage_db

    src, db = write(tmp_path), tmp_path / "s.sqlite"
    for types, out in (("building", "a.dxf"), ("road,path", "b.dxf")):
        assert osm2cad.main([
            "--input", str(src), "--types", types,
            "--out", str(tmp_path / out), "--db", str(db),
            "--project", "site"]) == 0
    conn = stage_db.connect(db)
    pid = conn.execute("SELECT id FROM projects").fetchone()[0]
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE project_id = ?",
                              (pid,)).fetchone()[0]
              for t in ("staging_buildings", "staging_roads")}
    conn.close()
    assert counts == {"staging_buildings": 1, "staging_roads": 2}


def test_replace_clears_the_project_first(tmp_path):
    pytest.importorskip("ezdxf")
    import stage_db

    src, db = write(tmp_path), tmp_path / "s.sqlite"
    assert osm2cad.main(["--input", str(src), "--types", "building",
                         "--out", str(tmp_path / "a.dxf"), "--db", str(db),
                         "--project", "site"]) == 0
    assert osm2cad.main(["--input", str(src), "--types", "road", "--replace",
                         "--out", str(tmp_path / "b.dxf"), "--db", str(db),
                         "--project", "site"]) == 0
    conn = stage_db.connect(db)
    pid = conn.execute("SELECT id FROM projects").fetchone()[0]
    n_b = conn.execute("SELECT COUNT(*) FROM staging_buildings WHERE"
                       " project_id = ?", (pid,)).fetchone()[0]
    conn.close()
    assert n_b == 0                      # the building import was cleared


def test_merging_keeps_one_project_row_and_its_id(tmp_path):
    """/project/<id> links have to survive a second import."""
    pytest.importorskip("ezdxf")
    import stage_db

    src, db = write(tmp_path), tmp_path / "s.sqlite"
    ids = []
    for out in ("a.dxf", "b.dxf"):
        assert osm2cad.main(["--input", str(src), "--out", str(tmp_path / out),
                             "--db", str(db), "--project", "site"]) == 0
        conn = stage_db.connect(db)
        ids.append([r[0] for r in conn.execute("SELECT id FROM projects")])
        conn.close()
    assert ids[0] == ids[1] == [1]


# ------------------------------------------------- utilities and planting
UTILITIES = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
  <bounds minlat="15.8300" minlon="104.3900"
          maxlat="15.8380" maxlon="104.3990"/>
  <node id="1" lat="15.8320" lon="104.3920"/>
  <node id="2" lat="15.8322" lon="104.3920"/>
  <node id="3" lat="15.8322" lon="104.3924"/>
  <node id="4" lat="15.8320" lon="104.3924"/>
  <node id="12" lat="15.8330" lon="104.3940"/>
  <node id="13" lat="15.8350" lon="104.3960"/>
  <node id="20" lat="15.8332" lon="104.3942"><tag k="power" v="tower"/></node>
  <node id="22" lat="15.8325" lon="104.3930">
    <tag k="natural" v="tree"/></node>
  <way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/>
    <nd ref="1"/><tag k="building" v="yes"/>
    <tag k="addr:housenumber" v="99/1"/></way>
  <way id="200"><nd ref="12"/><nd ref="13"/><tag k="power" v="line"/></way>
  <way id="300"><nd ref="12"/><nd ref="13"/>
    <tag k="man_made" v="pipeline"/></way>
</osm>
"""


def test_utilities_and_planting_reach_their_own_layers(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    out = tmp_path / "utils.dxf"
    assert osm2cad.main(["--input", str(write(tmp_path, UTILITIES, "u.osm")),
                         "--out", str(out)]) == 0
    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    layers = {e.dxf.layer for e in msp}
    assert {"C-UTIL-POWR", "C-UTIL-PIPE", "C-LAND-TREE", "C-ANNO-ADDR"} <= layers
    # A pylon and a tree are different marks, not two circles
    blocks = {e.dxf.name for e in msp if e.dxftype() == "INSERT"}
    assert {"PYLON_SYMB", "TREE_SYMB"} <= blocks
    # ...and the tree is drawn smaller than the pylon
    size = {e.dxf.name: e.dxf.xscale for e in msp if e.dxftype() == "INSERT"}
    assert size["TREE_SYMB"] < size["PYLON_SYMB"]
    house = [e.text for e in msp
             if e.dxftype() == "MTEXT" and e.dxf.layer == "C-ANNO-ADDR"]
    assert house == ["99/1"]


def test_types_can_drop_utilities_and_trees(tmp_path):
    """--types building alone must not smuggle a pylon in."""
    elements, _ = read(write(tmp_path, UTILITIES, "u.osm"))
    f = classify_elements(elements)
    assert len(f["power"]) == 1 and len(f["pipelines"]) == 1
    assert {m[0] for m in f["points"]} == {"power", "tree"}

    only_power = osm2cad.select_types(f, {"power"})
    assert only_power["buildings"] == []
    assert {m[0] for m in only_power["points"]} == {"power"}
    no_utils = osm2cad.select_types(f, {"building"})
    assert no_utils["power"] == [] and no_utils["pipelines"] == []
    assert no_utils["points"] == []
