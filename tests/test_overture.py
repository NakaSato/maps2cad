"""Overture places: the curation, the cache key and the OSM dedupe.

Everything here is offline. The fetch itself needs S3 and about a minute,
which is exactly why `fetch_places` caches per extent rather than per
confidence floor — a test for that would be a test of DuckDB.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import overture  # noqa: E402


def test_landmark_categories_are_kept():
    for category in ("school", "hospital", "buddhist_temple", "art_museum",
                     "police_station", "post_office", "train_station",
                     "public_and_government_association", "gas_station",
                     "university", "library"):
        assert overture.keep_place(category), category


def test_shop_and_restaurant_categories_are_dropped():
    # The 500 x 400 m box at Siam Square returns 22 japanese_restaurant and
    # 20 clothing_store above 0.95 confidence. None of them locate a parcel.
    for category in ("japanese_restaurant", "clothing_store", "jewelry_store",
                     "beauty_salon", "coffee_shop", "sushi_restaurant",
                     "mobile_phone_store", "nail_salon", "bar", ""):
        assert not overture.keep_place(category), category


def test_a_shop_named_after_a_landmark_is_still_a_shop():
    # The reject words are applied before the keep words on purpose: a
    # school supply store is a stationer, not a school.
    assert not overture.keep_place("school_supply_store")
    assert not overture.keep_place("hospital_equipment_store")


def test_the_two_mall_anchors_survive_the_retail_reject():
    # A mall is how a Thai address describes where a parcel is, so these two
    # outrank the "store"/"shopping" reject that removes their tenants.
    assert overture.keep_place("shopping_center")
    assert overture.keep_place("department_store")
    assert not overture.keep_place("mens_clothing_store")


def test_filter_places_applies_confidence_then_curation():
    places = [{"name": "A", "category": "school", "confidence": 0.95},
              {"name": "B", "category": "school", "confidence": 0.60},
              {"name": "C", "category": "cafe", "confidence": 0.99}]
    kept = overture.filter_places(places, 0.9)
    assert [p["name"] for p in kept] == ["A"]
    # --all-places keeps the cafe but never the low-confidence row
    kept = overture.filter_places(places, 0.9, curated=False)
    assert [p["name"] for p in kept] == ["A", "C"]


def test_cache_key_ignores_the_confidence_floor():
    """One query serves every floor, or trying 0.9 then 0.8 costs a minute
    each time."""
    box = (13.7437, 100.53019, 13.7473, 100.53481)
    assert overture.cache_key(box, "2026-07-22.0") == overture.cache_key(
        box, "2026-07-22.0")
    assert "c0.9" not in overture.cache_key(box, "2026-07-22.0")
    # ...but a different extent or release is a different file
    assert (overture.cache_key(box, "2026-07-22.0")
            != overture.cache_key(box, "2026-06-17.0"))
    assert (overture.cache_key(box, "2026-07-22.0")
            != overture.cache_key((13.75, 100.53019, 13.7473, 100.53481),
                                  "2026-07-22.0"))


def test_drop_known_needs_both_the_name_and_the_place():
    place = {"name": "โรงเรียนวัดปทุมวนาราม", "lon": 100.5325, "lat": 13.7455}
    same = [("โรงเรียนวัดปทุมวนาราม", 100.53251, 13.74551)]   # ~1 m away
    far = [("โรงเรียนวัดปทุมวนาราม", 100.5425, 13.7455)]      # ~1 km away
    other = [("วัดปทุมวนาราม", 100.53251, 13.74551)]
    assert overture.drop_known([place], same) == []
    # A chain has branches: the same name a kilometre away is another place
    assert overture.drop_known([place], far) == [place]
    assert overture.drop_known([place], other) == [place]


def test_drop_known_ignores_spacing_and_case():
    place = {"name": "Siam Paragon", "lon": 100.5325, "lat": 13.7455}
    assert overture.drop_known(
        [place], [("siam  paragon", 100.5325, 13.7455)]) == []


def test_place_tags_carry_the_source_and_the_confidence():
    tags = overture.place_tags({"name": "Bacc", "category": "art_museum",
                                "source": "meta", "confidence": 0.9876})
    assert tags["source"] == "meta"
    assert tags["confidence"] == "0.99"     # formatted, not a float repr
    assert tags["dataset"] == "overture"


def test_the_appid_is_not_osm():
    """A Meta record filed under OSM in the CAD attribute browser would be a
    lie, the same reason gis2cad.py has its own id."""
    import stage_db

    assert overture.XDATA_APPID not in (stage_db.XDATA_APPID,
                                        stage_db.GIS_XDATA_APPID)


def test_latest_release_reads_the_bucket_listing(monkeypatch):
    xml = b"""<?xml version="1.0"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <CommonPrefixes><Prefix>release/2026-06-17.0/</Prefix></CommonPrefixes>
      <CommonPrefixes><Prefix>release/2026-07-22.0/</Prefix></CommonPrefixes>
    </ListBucketResult>"""

    class FakeResponse:
        def read(self):
            return xml

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(overture.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse())
    assert overture.latest_release() == "2026-07-22.0"


def test_a_failed_listing_says_so(monkeypatch):
    def boom(*a, **k):
        raise OSError("no route to host")

    monkeypatch.setattr(overture.urllib.request, "urlopen", boom)
    with pytest.raises(overture.OvertureError):
        overture.latest_release()


def test_fetch_places_serves_the_cache_without_duckdb(tmp_path):
    box = (13.0, 100.0, 13.01, 100.01)
    path = tmp_path / overture.cache_key(box, "2026-07-22.0")
    path.write_text(json.dumps([{"name": "วัดโพธิ์", "category": "temple",
                                 "confidence": 0.97, "source": "meta",
                                 "lon": 100.005, "lat": 13.005, "id": "x"}]),
                    encoding="utf-8")
    places, cached = overture.fetch_places(box, release="2026-07-22.0",
                                           cache_dir=tmp_path)
    assert cached and places[0]["name"] == "วัดโพธิ์"


def test_overture_places_label_on_their_own_layer_family():
    """Freezing the third-party source has to take its names with it, or the
    drawing keeps a label pointing at nothing."""
    import stage_db

    conn = stage_db.connect(":memory:")
    pid = stage_db.create_project(conn, "t", 13.7455, 100.5325, 500, 400,
                                  32647)
    stage_db.stage_pois(conn, pid, [
        {"feature_id": "overture/abc", "x": 100.0, "y": 200.0,
         "source": "overture", "poi_key": "place", "poi_type": "art_museum",
         "display_name": "Bacc", "cad_layer": "C-ANNO-OVTR"},
        {"feature_id": "node/1", "x": 0.0, "y": 0.0, "poi_key": "amenity",
         "poi_type": "school", "display_name": "โรงเรียนวัดปทุมวนาราม"},
    ])
    layers = {row["text"]: row["cad_layer"] for row in conn.execute(
        "SELECT text, cad_layer FROM cad_labels WHERE project_id = ?", (pid,))}
    assert layers["Bacc"] == "C-ANNO-OVTR-EN"
    assert layers["โรงเรียนวัดปทุมวนาราม"] == "C-ANNO-TEXT-TH"
