"""Where each drawn entity says it came from (XDATA provenance).

A modelled footprint and a traced one are both black polylines on
C-BLDG-UNNM once they are in the drawing, and the difference is exactly
what a reviewer needs before treating either as survey. These cover the
rules that put the answer on the entity, and the check that keeps a
re-issue from quietly dropping it.
"""

import importlib.util as iu
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import stage_db  # noqa: E402
import topo2cad  # noqa: E402


def load(name):
    spec = iu.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = iu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------- the ML release parser
REAL_URL = ("https://minedbuildings.z5.web.core.windows.net/global-buildings/"
            "2026-02-03/global-buildings.geojsonl/RegionName=Thailand/"
            "quadkey=132203130/part-00116-4feead82.c000.csv.gz")


def test_the_release_and_region_come_out_of_the_tile_url():
    got = topo2cad.ms_release_from_url(REAL_URL)
    assert got["release"] == "2026-02-03"
    assert got["region"] == "Thailand"
    assert got["quadkey"] == "132203130"


def test_the_index_url_itself_reports_no_release():
    """dataset-links.csv sits at the same depth as a release date. Reading
    it as one would stamp every footprint with a release that is a
    filename."""
    got = topo2cad.ms_release_from_url(
        "https://minedbuildings.z5.web.core.windows.net/global-buildings/"
        "dataset-links.csv")
    assert "release" not in got


# ------------------------------------------------ what an ML outline says
def _links_csv(tmp_path):
    cache = tmp_path / "ms_cache"
    cache.mkdir()
    # the zoom-9 quadkey covering the Lopburi test site, from quadkey()
    (cache / "dataset-links.csv").write_text(
        "Location,QuadKey,Url,Size,UploadDate\n"
        f"Thailand,132203130,{REAL_URL},2.2MB,2026-02-23\n"
        # a row for other ground, which must not be read for this extent
        f"Laos,033333333,{REAL_URL.replace('2026-02-03', '2019-09-16')},"
        "1MB,2019-10-01\n")
    return cache


def test_an_ml_footprint_says_it_was_predicted(tmp_path):
    s, w, n, e = 14.8161, 100.5109, 14.8179, 100.5131
    tags = topo2cad.ms_source_tags(s, w, n, e, _links_csv(tmp_path))
    # The load-bearing word: this outline is not survey, and the drawing
    # has to be able to say so when someone selects it.
    assert "not surveyed" in tags["method"]
    assert tags["source"] == "microsoft_ml"
    assert tags["release"] == "2026-02-03"
    assert tags["region"] == "Thailand"


def test_a_tile_outside_the_extent_is_not_read(tmp_path):
    """The index lists 30,000 tiles worldwide; only the ones the bbox
    covers describe these footprints."""
    tags = topo2cad.ms_source_tags(14.8161, 100.5109, 14.8179, 100.5131,
                                   _links_csv(tmp_path))
    assert tags["release"] != "2019-09-16"


def test_a_missing_index_still_names_the_source(tmp_path):
    """A provenance lookup must never be what loses a drawing."""
    tags = topo2cad.ms_source_tags(14.8, 100.5, 14.9, 100.6,
                                   tmp_path / "absent")
    assert tags["source"] == "microsoft_ml"
    assert "release" not in tags


def test_the_quadkey_is_not_carried(tmp_path):
    """An extent can straddle four tiles, so one quadkey would be wrong for
    the footprints of the other three."""
    tags = topo2cad.ms_source_tags(14.8161, 100.5109, 14.8179, 100.5131,
                                   _links_csv(tmp_path))
    assert "quadkey" not in tags


def test_ml_footprints_are_not_filed_under_openstreetmap():
    """The appid is the answer to 'where did this come from' in the CAD
    attribute browser. Filing a model's output under OSM would be the same
    lie gis2cad.py's own id exists to avoid."""
    assert stage_db.MS_XDATA_APPID != stage_db.XDATA_APPID
    assert len({stage_db.XDATA_APPID, stage_db.GIS_XDATA_APPID,
                stage_db.MS_XDATA_APPID}) == 3


def test_a_gis_feature_with_no_columns_still_names_its_file():
    """Plenty of exports are bare geometry. Such a feature used to carry
    nothing and drop out of attributes.csv with it, so the table stopped
    describing the whole drawing."""
    src = (SCRIPTS / "gis2cad.py").read_text(encoding="utf-8")
    assert '"@source": f"user_gis:{path.name}"' in src
    # '@' marks it as assigned here, so a user column named "source" wins
    # its own name back. xdata_tags() lives in cad_rules.py — the drawing
    # rules, which is what it is.
    assert "@id=" in (SCRIPTS / "cad_rules.py").read_text(encoding="utf-8")


# ------------------------------------------------------ the parity check
def test_dxfdiff_compares_extended_data(tmp_path):
    """Nothing compared XDATA, so a re-issue could hand back a drawing
    stripped of its source data and this tool still said IDENTICAL."""
    ezdxf = pytest.importorskip("ezdxf")
    dxfdiff = load("dxfdiff")

    def write(path, tags):
        doc = ezdxf.new("R2010")
        doc.appids.add("MICROSOFT")
        line = doc.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 5)])
        if tags:
            line.set_xdata("MICROSOFT", tags)
        doc.saveas(path)
        return path

    full = write(tmp_path / "a.dxf",
                 [(1000, "@id=ms/00000"), (1000, "source=microsoft_ml")])
    bare = write(tmp_path / "b.dxf", None)
    mislabelled = write(tmp_path / "c.dxf",
                        [(1000, "@id=ms/00000"), (1000, "source=survey")])

    xa = dxfdiff.survey(str(full))[4]
    assert sum(xa.values()) == 1
    xb = dxfdiff.survey(str(bare))[4]
    assert sum(xb.values()) == 0
    xc = dxfdiff.survey(str(mislabelled))[4]

    # Same entity count, same layers, different provenance: the case the
    # counts alone cannot see.
    assert xa != xc
    assert sum(xc.values()) == 1


# ------------------------------------------- the fonts a reader will need
def test_a_missing_thai_font_is_reported_not_swallowed():
    """ezdxf writes UTF-8 whatever happens, so the Thai is in the file;
    what decides whether anyone sees it is the font the STYLE points at.
    The styles name THSarabunNew.ttf and arial.ttf by filename and nothing
    ever checked they exist — on the machine this was written on
    THSarabunNew is absent and ezdxf substitutes Arial Unicode, which
    happens to carry Thai, so every plot looked right by luck.
    """
    pytest.importorskip("ezdxf")
    report = stage_db.font_report(topo2cad.TEXT_STYLES)
    assert report, "no styles were examined"
    by_style = {r["style"]: r for r in report}
    assert by_style["TH_STYLE"]["needs_thai"] is True
    assert by_style["EN_STYLE"]["needs_thai"] is False
    # Whatever this machine has, a style that resolves to something other
    # than what it asked for has to say so.
    for row in report:
        if not row["present"]:
            assert stage_db.font_warnings([row]), row


def test_a_font_that_resolves_to_itself_says_nothing():
    """A warning that fires when everything is fine is read once and then
    ignored, which is worse than the silence it replaced."""
    ok = [{"style": "EN_STYLE", "declared": "arial.ttf", "present": True,
           "resolved": "arial.ttf", "has_thai": False, "needs_thai": False}]
    assert stage_db.font_warnings(ok) == []


def test_a_substitute_with_no_thai_is_the_loudest_case():
    bad = [{"style": "TH_STYLE", "declared": "THSarabunNew.ttf",
            "present": False, "resolved": "simplex.shx", "has_thai": False,
            "needs_thai": True}]
    line = stage_db.font_warnings(bad)[0]
    assert "???" in line and "simplex.shx" in line


def test_the_requirement_travels_beside_the_drawing(tmp_path):
    """A DXF names its fonts and cannot embed them, so a recipient missing
    THSarabunNew sees ??? and has nothing telling them why."""
    note = tmp_path / "fonts.txt"
    stage_db.write_font_note(note, [
        {"style": "TH_STYLE", "declared": "THSarabunNew.ttf",
         "present": False, "resolved": "x", "has_thai": True,
         "needs_thai": True}])
    body = note.read_text(encoding="utf-8")
    assert "THSarabunNew.ttf" in body
    assert "cannot embed" in body


def test_the_check_never_loses_a_drawing():
    """A font lookup is a courtesy; failing one must not fail a run."""
    assert stage_db.font_report({}) == []
    assert stage_db.font_warnings([]) == []


def test_the_zip_carries_the_font_note():
    src = (SCRIPTS / "serve.py").read_text(encoding="utf-8")
    assert '"sources.csv", "fonts.txt"' in src
