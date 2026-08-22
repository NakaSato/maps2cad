"""Reading a coordinate in whatever shape it arrived in (scripts/serve.py).

A coordinate reaches a person in the form the thing that gave it to them
used — a Maps link, a share sheet, a GPS handset in degrees and minutes.
Accepting only one of those makes the user do the conversion.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from serve import BadRequest, parse_coords, parse_form  # noqa: E402

SITE = (15.83384548, 104.39445555)


@pytest.mark.parametrize("text,want", [
    ("15.83384548, 104.39445555", SITE),
    ("15.83384548 104.39445555", SITE),
    ("  15.83384548,104.39445555  ", SITE),
    ("lat: 15.83384548, lon: 104.39445555", SITE),
    ("latitude 15.83384548 longitude 104.39445555", SITE),
    ("15.83384548 N, 104.39445555 E", SITE),
    ("15.83384548N 104.39445555E", SITE),
    ("N15.83384548 E104.39445555", SITE),
    ("geo:15.83384548,104.39445555", SITE),
    ("https://www.google.com/maps/@15.83384548,104.39445555,17z", SITE),
    ("https://www.google.com/maps/place/X/@15.83384548,104.39445555,18z/d",
     SITE),
    ("https://maps.google.com/?q=15.83384548,104.39445555", SITE),
    ("https://www.google.com/maps?ll=15.83384548,104.39445555&z=17", SITE),
    ("https://www.openstreetmap.org/#map=17/15.83384548/104.39445555", SITE),
    ("https://www.openstreetmap.org/?mlat=15.83384548&mlon=104.39445555",
     SITE),
])
def test_reads_every_shape_a_coordinate_arrives_in(text, want):
    lat, lon = parse_coords(text)
    assert lat == pytest.approx(want[0], abs=1e-6)
    assert lon == pytest.approx(want[1], abs=1e-6)


@pytest.mark.parametrize("text,want", [
    ("15°50'02\"N 104°23'40\"E", (15.833889, 104.394444)),
    ("15°50.04'N, 104°23.67'E", (15.834, 104.3945)),
    ("13°45'27\"S 100°30'06\"W", (-13.7575, -100.501667)),
])
def test_reads_degrees_minutes_seconds(text, want):
    lat, lon = parse_coords(text)
    assert lat == pytest.approx(want[0], abs=1e-4)
    assert lon == pytest.approx(want[1], abs=1e-4)


def test_hemisphere_letters_beat_the_order_they_were_written_in():
    """A GPS handset that prints longitude first is still unambiguous: the
    E says which number it is. Ordering by position would put the site a
    hemisphere away and it would still look like a coordinate."""
    lat, lon = parse_coords("104°23'40\"E 15°50'02\"N")
    assert lat == pytest.approx(15.8339, abs=1e-3)
    assert lon == pytest.approx(104.3944, abs=1e-3)


def test_a_shortened_maps_link_says_why_it_cannot_be_read():
    """Only Google can expand goo.gl, and this app does not call out to
    resolve one. Saying so beats "could not read that"."""
    with pytest.raises(BadRequest) as e:
        parse_coords("https://maps.app.goo.gl/aBcD1234")
    assert "shorten" in str(e.value).lower()


def test_a_swapped_pair_is_named_rather_than_quietly_fixed():
    """Silently swapping would be a guess about what the user meant."""
    with pytest.raises(BadRequest) as e:
        parse_coords("104.39445555, 15.83384548")
    assert "latitude first" in str(e.value)


@pytest.mark.parametrize("text", [
    "", "   ", "nonsense", "https://example.com/no-coordinate-here",
    "15.8, 400", "200, 104.4",
])
def test_rejects_what_it_cannot_read(text):
    with pytest.raises(BadRequest):
        parse_coords(text)


def test_the_error_lists_the_formats_that_do_work():
    with pytest.raises(BadRequest) as e:
        parse_coords("somewhere near the temple")
    msg = str(e.value)
    assert "15.83384548, 104.39445555" in msg and "Google Maps" in msg


def test_the_form_accepts_a_link_end_to_end():
    got = parse_form({"coords":
                      ["https://www.google.com/maps/@14.8165,100.5116,18z"]})
    assert (got["lat"], got["lon"]) == (14.8165, 100.5116)


def test_separate_lat_lon_fields_still_work():
    """What the browser's own geolocation posts, and what older links use."""
    got = parse_form({"lat": ["15.5"], "lon": ["104.5"]})
    assert (got["lat"], got["lon"]) == (15.5, 104.5)
