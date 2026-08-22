"""Tests for the web app's HTML layer (scripts/webui.py).

The pages are built by pure functions that take data and return bytes, so
they can be rendered here without a server, a database or a network.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sheet  # noqa: E402
import webui  # noqa: E402

SIZES = ("A4", "A3", "A2", "A1", "A0")
BASEMAPS = {"": "None", "osm": "OpenStreetMap"}
GOV = [("owner", "เจ้าของ / Owner"), ("scale", "มาตราส่วน / Scale")]


def render_form(values=None, error=""):
    return webui.form_page(values or {}, error, "<p>recent</p>",
                           GOV, BASEMAPS).decode()


# --------------------------------------------------------------- sheet rules
def test_viewport_matches_sheet_py():
    """webui restates sheet.py's arithmetic because serve.py cannot import
    ezdxf's side of the tree; if the two drift, the form quotes a scale the
    drawing will not plot at."""
    for size in SIZES:
        _, vp_w, vp_h = sheet.fitting_scale(1, 1, size)
        assert webui.viewport_mm(size) == (vp_w, vp_h), size


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("extent", [(200, 150), (500, 400), (770, 410),
                                    (1000, 750), (2000, 1500), (50, 40)])
def test_fitting_scale_matches_sheet_py(size, extent):
    mine = webui.fitting_scale(extent[0], extent[1], size)
    theirs, _, _ = sheet.fitting_scale(extent[0], extent[1], size)
    if mine is not None:
        assert mine == theirs


def test_fitting_scale_reports_no_fit_instead_of_clamping():
    """sheet.py returns its largest scale for an extent nothing holds; the
    form has to say it does not fit rather than quote a scale that crops."""
    assert webui.fitting_scale(100_000, 100_000, "A4") is None


def test_the_documented_default_lands_on_1_5000_on_a3():
    """The default extent is a locality map on the default sheet. That is a
    deliberate choice, and the form exists to say so before the run."""
    assert webui.fitting_scale(1000, 750, "A3") == 5000
    assert webui.fitting_scale(200, 150, "A3") == 1000


# --------------------------------------------------------------------- form
FORM_FIELDS = {"coords", "width", "height", "export", "profile", "title",
               "sheet_size", "cad_sheet", "cad_scale", "basemap", "codes",
               "final", "owner", "scale"}


def test_form_carries_every_field_the_server_reads():
    """Regrouping the form must never drop a control: a field that stops
    being rendered silently reverts to its default on every run."""
    names = set(re.findall(r'\bname="([^"]+)"', render_form()))
    assert FORM_FIELDS <= names, FORM_FIELDS - names


def test_form_repopulates_after_a_rejected_run():
    html = render_form({"coords": "15.83, 104.39", "width": 500,
                        "height": 400, "cad_sheet": "A1", "cad_scale": "1000",
                        "profile": "government", "basemap": "osm"},
                       "bad coordinate")
    for probe in ('value="15.83, 104.39"', 'value="500"', 'value="400"',
                  'value="A1" selected', 'value="1000" selected',
                  'value="osm" selected', "bad coordinate"):
        assert probe in html, probe


def test_advanced_opens_when_it_holds_a_chosen_value():
    """An error must not point at a control the page has folded away."""
    assert '<details class="adv" id="adv" open>' in render_form(
        {"cad_sheet": "A1"}, "something went wrong")
    assert '<details class="adv" id="adv">' in render_form({"coords": "1,1"})


def test_the_scale_readout_is_fed_the_same_numbers_as_python():
    html = render_form()
    for size in SIZES:
        vp_w, vp_h = webui.viewport_mm(size)
        assert f'"{size}":[{vp_w},{vp_h}]' in html, size
    assert str(webui.ROUND_SCALES).replace(" ", "") in html


# ------------------------------------------------------------- other pages
def test_pages_render_without_a_database():
    assert b"Nothing staged yet" in webui.projects_page(None)
    assert b"<form" in webui.import_page([], ("building",),
                                         {"building": "อาคาร / Buildings"},
                                         BASEMAPS, "", "")


def test_history_table_says_so_when_empty():
    assert "No maps generated yet" in webui.history_table([])


def test_every_page_binds_its_own_stylesheet():
    """page() is the only shell; a builder that hand-rolled <html> would
    render unstyled and theme-blind."""
    for out in (render_form().encode(),
                webui.projects_page(None),
                webui.import_page([], (), {}, BASEMAPS, "", "")):
        assert out.startswith(b"<!doctype html>")
        assert b"prefers-color-scheme" in out


# --------------------------------------------------------- the browser half
def _node():
    import shutil
    return shutil.which("node")


@pytest.mark.skipif(not _node(), reason="node is not installed")
def test_the_browser_scale_loop_agrees_with_python():
    """The page repeats one rule in JavaScript — the loop over the round
    scales — so run it and compare. Everything else it needs (the viewport
    of each sheet) is computed here and sent down already worked out."""
    import json
    import subprocess

    html = render_form()
    js = [b for b in re.findall(r"<script>(.*?)</script>", html, re.S)
          if "fittingScale" in b][0]
    core = js[:js.index("function formSync")]
    cases = [(w, h, s)
             for w in (50, 200, 500, 770, 1000, 2000, 9000, 60000)
             for h in (40, 150, 410, 750, 1500, 50000)
             for s in SIZES]
    prog = (core + "\nconst c = " + json.dumps(cases) + ";\n"
            "console.log(JSON.stringify(c.map(x => "
            "fittingScale(x[0], x[1], x[2]))));")
    out = subprocess.run([_node(), "-e", prog], capture_output=True,
                         text=True, check=True)
    for case, got in zip(cases, json.loads(out.stdout)):
        assert got == webui.fitting_scale(*case), case
