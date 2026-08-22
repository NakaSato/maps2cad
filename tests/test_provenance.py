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
    # its own name back.
    assert "@id=" in (SCRIPTS / "stage_db.py").read_text(encoding="utf-8")


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

    _c, _l, _s, _y, xa = dxfdiff.survey(str(full))
    assert sum(xa.values()) == 1
    _c, _l, _s, _y, xb = dxfdiff.survey(str(bare))
    assert sum(xb.values()) == 0
    _c, _l, _s, _y, xc = dxfdiff.survey(str(mislabelled))

    # Same entity count, same layers, different provenance: the case the
    # counts alone cannot see.
    assert xa != xc
    assert sum(xc.values()) == 1
