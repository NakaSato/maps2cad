"""Tests for the background-map fetcher (scripts/basemap.py).

Tile arithmetic is the part that fails silently: an off-by-one in the tile
range or a wrong mosaic origin produces a picture that still looks like a
map, placed metres from where it belongs. Everything here runs offline —
the one test that exercises the fetch loop passes a fake session, because
these tests must never hit a tile server.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import basemap  # noqa: E402


def mercator(lat, lon):
    """Lon/lat -> EPSG:3857 metres, independently of the module under test."""
    x = lon / 180.0 * basemap.MERCATOR_ORIGIN
    y = (math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
         / math.pi * basemap.MERCATOR_ORIGIN)
    return x, y


def png_bytes(colour=(200, 100, 50), size=basemap.TILE_PX):
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------- tile maths
@pytest.mark.parametrize("lat,lon,zoom,tile", [
    (0.0, 0.0, 0, (0, 0)),
    (0.0, 0.0, 1, (1, 1)),           # equator/meridian is the grid centre
    (85.0, -179.9, 1, (0, 0)),       # top-left of the world
    (-85.0, 179.9, 1, (1, 1)),       # bottom-right
])
def test_deg2tile_known_corners(lat, lon, zoom, tile):
    assert basemap.deg2tile(lat, lon, zoom) == tile


def test_deg2tile_never_indexes_off_the_grid():
    """A coordinate past the Mercator limit must clamp, not overflow."""
    for zoom in (0, 5, 18):
        n = 2 ** zoom
        for lat, lon in ((90.0, 180.0), (-90.0, -180.0)):
            x, y = basemap.deg2tile(lat, lon, zoom)
            assert 0 <= x < n and 0 <= y < n


@pytest.mark.parametrize("lat,lon,zoom", [
    (13.7455, 100.5325, 18), (15.8338, 104.3945, 16), (-33.87, 151.21, 14),
])
def test_tile_contains_its_own_coordinate(lat, lon, zoom):
    """The tile a point maps to must actually cover that point once the
    mosaic transform places it — this is what pins the two together."""
    x, y = basemap.deg2tile(lat, lon, zoom)
    west, north, _res = basemap.mosaic_origin(x, y, zoom)
    size = basemap.tile_size_m(zoom)
    mx, my = mercator(lat, lon)
    assert west <= mx <= west + size
    assert north - size <= my <= north


def test_tile_range_covers_the_whole_box():
    box = (13.744, 100.531, 13.747, 100.534)          # S, W, N, E
    x0, y0, x1, y1 = basemap.tile_range(box, 18)
    # North gives the smaller y: the grid counts down from the pole
    assert basemap.deg2tile(box[2], box[1], 18) == (x0, y0)
    assert basemap.deg2tile(box[0], box[3], 18) == (x1, y1)
    assert x1 >= x0 and y1 >= y0
    assert basemap.tile_count(box, 18) == (x1 - x0 + 1) * (y1 - y0 + 1)


def test_tile_count_quadruples_per_zoom_level():
    """Each level halves the tile size in both axes. The bound is loose
    because a box that straddles a tile edge picks up a whole extra row."""
    box = (13.70, 100.50, 13.80, 100.60)
    counts = [basemap.tile_count(box, z) for z in (14, 15, 16)]
    assert counts[1] >= counts[0] * 2.5 and counts[2] >= counts[1] * 2.5


def test_choose_zoom_steps_down_until_the_extent_fits():
    """The cap exists because tile servers are shared infrastructure; the
    answer to a big extent is a coarser zoom, not hundreds of requests."""
    box = (13.70, 100.50, 13.80, 100.60)             # ~11 km
    zoom = basemap.choose_zoom(box, max_tiles=64, max_zoom=19)
    assert basemap.tile_count(box, zoom) <= 64
    assert basemap.tile_count(box, zoom + 1) > 64     # and it is the sharpest


def test_choose_zoom_respects_a_providers_maximum():
    tiny = (13.7455, 100.5325, 13.7456, 100.5326)
    assert basemap.choose_zoom(tiny, max_tiles=128, max_zoom=17) == 17


def test_mosaic_origin_places_tile_zero_at_the_world_corner():
    west, north, res = basemap.mosaic_origin(0, 0, 0)
    assert west == pytest.approx(-basemap.MERCATOR_ORIGIN)
    assert north == pytest.approx(basemap.MERCATOR_ORIGIN)
    assert res == pytest.approx(2 * basemap.MERCATOR_ORIGIN / basemap.TILE_PX)


# ---------------------------------------------------------------- providers
def test_resolve_provider_by_name_carries_an_attribution():
    spec = basemap.resolve_provider("osm")
    assert "OpenStreetMap" in spec["attribution"]
    assert spec["name"] == "osm"


def test_resolve_provider_accepts_a_custom_template():
    spec = basemap.resolve_provider("https://tiles.example/{z}/{x}/{y}.png")
    assert spec["name"] == "custom"
    assert "replace this credit" in spec["attribution"]


def test_resolve_provider_rejects_a_name_it_does_not_know():
    with pytest.raises(basemap.BasemapError, match="Unknown basemap"):
        basemap.resolve_provider("google")


def test_tile_url_honours_each_providers_path_order():
    """Esri orders the path z/y/x; a positional template would swap them and
    fetch a tile from the wrong side of the world."""
    assert basemap.tile_url(basemap.PROVIDERS["osm"]["url"], 5, 9, 18) \
        == "https://tile.openstreetmap.org/18/5/9.png"
    assert basemap.tile_url(
        basemap.PROVIDERS["esri-imagery"]["url"], 5, 9, 18).endswith("/18/9/5")


# ------------------------------------------------------------------ mosaics
def test_mosaic_places_each_tile_at_its_own_offset():
    np = pytest.importorskip("numpy")
    px = basemap.TILE_PX
    tiles = {
        (10, 20): np.full((px, px, 3), 10, dtype="uint8"),
        (11, 20): np.full((px, px, 3), 20, dtype="uint8"),
        (10, 21): np.full((px, px, 3), 30, dtype="uint8"),
    }
    out = basemap.mosaic(tiles, 10, 20, 2, 2)
    assert out.shape == (3, 2 * px, 2 * px)
    assert out[0, 0, 0] == 10                    # top-left tile
    assert out[0, 0, px] == 20                   # one tile east
    assert out[0, px, 0] == 30                   # one tile south
    assert out[0, px, px] == 255                 # missing tile stays white


def test_mosaic_leaves_a_failed_tile_white_not_black():
    """A gap should read as blank paper. Black would burn a hole through the
    drawing that looks deliberate."""
    out = basemap.mosaic({(0, 0): None}, 0, 0, 1, 1)
    assert (out == 255).all()


def test_decode_tile_normalises_to_rgb():
    pytest.importorskip("PIL")
    arr = basemap.decode_tile(png_bytes((1, 2, 3)))
    assert arr.shape == (basemap.TILE_PX, basemap.TILE_PX, 3)
    assert tuple(arr[0, 0]) == (1, 2, 3)


def test_decode_tile_returns_none_for_an_error_page():
    """A 502 arrives as HTML with a 200 body often enough to matter."""
    pytest.importorskip("PIL")
    assert basemap.decode_tile(b"<html>rate limited</html>") is None


# ------------------------------------------------------------- fetch + build
class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class FakeSession:
    """Stands in for requests.Session: these tests never touch a network."""

    def __init__(self, content):
        self.content = content
        self.urls = []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        return FakeResponse(self.content)


def test_fetch_tiles_caches_so_a_second_run_costs_no_requests(tmp_path):
    pytest.importorskip("PIL")
    spec = basemap.resolve_provider("osm")
    box = (13.7440, 100.5310, 13.7445, 100.5315)
    session = FakeSession(png_bytes())

    tiles, stats = basemap.fetch_tiles(spec, box, 18, tmp_path, session)
    assert stats["fetched"] == len(tiles) and stats["cached"] == 0
    assert len(session.urls) == len(tiles)

    again, stats2 = basemap.fetch_tiles(spec, box, 18, tmp_path, session)
    assert stats2["cached"] == len(again) and stats2["fetched"] == 0
    assert len(session.urls) == len(tiles)        # no further requests


def test_fetch_tiles_survives_a_provider_returning_junk(tmp_path):
    spec = basemap.resolve_provider("osm")
    box = (13.7440, 100.5310, 13.7445, 100.5315)
    tiles, stats = basemap.fetch_tiles(spec, box, 18, tmp_path,
                                       FakeSession(b"<html>nope</html>"))
    assert tiles == {} and stats["failed"] >= 1


def test_build_writes_a_geotiff_in_the_drawings_crs(tmp_path):
    pytest.importorskip("rasterio")
    pytest.importorskip("PIL")
    rasterio = pytest.importorskip("rasterio")
    box = (13.7440, 100.5310, 13.7470, 100.5340)
    out = tmp_path / "basemap.tif"
    info = basemap.build(box, 32647, out, zoom=18, cache_dir=tmp_path / "c",
                         session=FakeSession(png_bytes((120, 130, 140))))
    assert info["path"] == out and info["zoom"] == 18
    with rasterio.open(out) as src:
        assert src.crs.to_epsg() == 32647        # not Web Mercator
        assert src.count == 3
        # The site's UTM easting/northing must fall inside the raster
        assert src.bounds.left < 665694 < src.bounds.right
        assert src.bounds.bottom < 1520106 < src.bounds.top
        # Reprojected from 3857 to UTM, so the resolution is metres/px
        assert 0.05 < src.res[0] < 5.0


def test_build_refuses_to_blow_the_tile_budget(tmp_path):
    """An explicit zoom over a wide box is the one path that could hammer a
    tile server, so it is refused rather than throttled."""
    wide = (13.0, 100.0, 14.0, 101.0)
    with pytest.raises(basemap.BasemapError, match="tile cap"):
        basemap.build(wide, 32647, tmp_path / "x.tif", zoom=18,
                      max_tiles=64, session=FakeSession(png_bytes()))


def test_build_reports_when_every_tile_failed(tmp_path):
    box = (13.7440, 100.5310, 13.7445, 100.5315)
    with pytest.raises(basemap.BasemapError, match="No tiles"):
        basemap.build(box, 32647, tmp_path / "x.tif", zoom=18,
                      cache_dir=tmp_path / "c",
                      session=FakeSession(b"not an image"))
