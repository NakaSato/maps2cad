"""The background run and its progress view (scripts/serve.py).

A generation takes 18–105 s. It runs on its own thread and reports what it
is doing, so the browser can show the plan and tick it off instead of
holding a POST open behind one indeterminate bar.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import serve  # noqa: E402
import webui  # noqa: E402


def params(export="both", poster=True):
    return {"lat": 14.8165, "lon": 100.5116, "width": 200.0, "height": 150.0,
            "export": export, "poster": poster, "profile": "standard",
            "title": "T", "sheet_size": "A3", "codes": True, "final": False,
            "gov": {}}


def test_the_plan_matches_what_was_asked_for():
    keys = lambda p: [s["key"] for s in serve.planned_steps(p)]
    assert keys(params("map", poster=False)) == ["map"]
    assert keys(params("cad", poster=False)) == ["dem", "cad", "plot"]
    assert keys(params("both")) == ["map", "dem", "cad", "plot", "poster"]


def test_every_step_starts_as_waiting():
    """The whole plan is shown from the first frame: a step still to come is
    as much information as the one running."""
    assert {s["state"] for s in serve.planned_steps(params())} == {"waiting"}


def test_progress_moves_a_step_through_its_states():
    jid = "test-progress"
    pr = serve.Progress(jid, params("map", poster=False))
    assert serve.run_state(jid)["steps"][0]["state"] == "waiting"

    say = pr.begin("map")
    assert serve.run_state(jid)["steps"][0]["state"] == "running"

    say("Retrieving OpenStreetMap features ...")
    assert serve.run_state(jid)["steps"][0]["detail"].startswith("Retrieving")

    pr.done("map", "sheet and CSV")
    step = serve.run_state(jid)["steps"][0]
    assert (step["state"], step["detail"]) == ("done", "sheet and CSV")

    pr.finish()
    assert serve.run_state(jid)["state"] == "done"


def test_a_failure_is_recorded_with_what_was_typed():
    """The form is re-shown with the values still in it, so the failure has
    to carry them: a user who mistyped one field should not lose the rest."""
    jid = "test-fail"
    pr = serve.Progress(jid, params("cad", poster=False))
    pr.begin("cad")
    pr.fail("CAD export failed: no DEM", {"coords": "14.8, 100.5"})
    state = serve.run_state(jid)
    assert state["state"] == "failed"
    assert state["values"] == {"coords": "14.8, 100.5"}
    # the step that was in flight is marked, not left spinning for ever
    assert [s["state"] for s in state["steps"]] == ["waiting", "failed",
                                                    "waiting"]


def test_run_state_hands_back_a_copy():
    """The worker thread writes while the browser reads; handing out the
    live dict would let a poll see a half-written step."""
    jid = "test-copy"
    serve.Progress(jid, params("map", poster=False))
    got = serve.run_state(jid)
    got["steps"][0]["state"] = "tampered"
    assert serve.run_state(jid)["steps"][0]["state"] == "waiting"


def test_unknown_run_is_none():
    assert serve.run_state("never-existed") is None


def test_the_run_table_stays_bounded():
    for n in range(serve.RUNS_KEPT + 20):
        pr = serve.Progress(f"bulk-{n}", params("map", poster=False))
        pr.finish()
    serve.prune_runs()
    assert len(serve.RUNS) <= serve.RUNS_KEPT


@pytest.mark.parametrize("line,keep", [
    ("Retrieving OpenStreetMap features ...", True),
    ("Spot heights: 25 sampled from the DEM", True),
    ("", False),
    ("   ", False),
    ("Installed 41 packages in 88ms", False),
    ("Resolved 30 packages in 1ms", False),
    ("UserWarning: Glyph missing from font", False),
    ("fsSelection bit 5 (bold) should match", False),
])
def test_install_noise_is_not_progress(line, keep):
    assert serve.useful_line(line) is keep


# ------------------------------------------------------------ the watch page
def test_the_watch_page_shows_the_plan_without_javascript():
    """Vue comes from a CDN. If it never loads, the page still has to say
    what is being made — the framework makes it live, not functional."""
    state = {"steps": serve.planned_steps(params()), "state": "running",
             "error": "", "values": {}, "started": 0}
    html = webui.run_page("abc123", state).decode()
    for step in state["steps"]:
        assert step["name"] in html
    assert "<noscript>" in html
    assert 'id="run" data-job="abc123"' in html


# ------------------------------------------- live preview while running
def test_a_run_reports_the_files_it_has_written_so_far(tmp_path, monkeypatch):
    """The run page narrated the steps while the sheets it was producing sat
    on disk unseen until the whole run finished. This is what lets the
    preview pane fill as the work happens."""
    monkeypatch.setattr(serve, "OUT", tmp_path)
    folder = tmp_path / "abc123"
    folder.mkdir()
    assert serve.preview_files("abc123") == []

    (folder / serve.KINDS["png"]).write_bytes(b"x" * 40)
    got = serve.preview_files("abc123")
    assert [p["kind"] for p in got] == ["png"]
    assert got[0]["bytes"] == 40 and got[0]["how"] == "image"

    (folder / serve.KINDS["plot"]).write_bytes(b"y" * 10)
    assert {p["kind"] for p in serve.preview_files("abc123")} == {"png", "plot"}


def test_a_half_written_file_is_not_offered(tmp_path, monkeypatch):
    """A step can be caught mid-write. An empty file would render as a
    broken image, which reads as a failure rather than as progress."""
    monkeypatch.setattr(serve, "OUT", tmp_path)
    folder = tmp_path / "abc123"
    folder.mkdir()
    (folder / serve.KINDS["png"]).write_bytes(b"")
    assert serve.preview_files("abc123") == []


def test_only_what_a_browser_can_render_is_previewed(tmp_path, monkeypatch):
    """A DXF has no viewer and a GeoTIFF is not a picture; offering either
    would put a download prompt in the middle of a progress page."""
    monkeypatch.setattr(serve, "OUT", tmp_path)
    folder = tmp_path / "abc123"
    folder.mkdir()
    for kind in ("dxf", "tif", "csv", "attrs"):
        (folder / serve.KINDS[kind]).write_bytes(b"z" * 10)
    assert serve.preview_files("abc123") == []
    offered = {k for k, _label, _how in serve.PREVIEW_KINDS}
    assert offered <= set(serve.KINDS)
    assert not offered & {"dxf", "tif", "csv", "attrs", "poster_pdf"}


def test_a_missing_run_folder_is_not_an_error(tmp_path, monkeypatch):
    """The status poll runs before the folder exists and must not raise."""
    monkeypatch.setattr(serve, "OUT", tmp_path)
    assert serve.preview_files("nothing-here") == []


def test_the_status_payload_carries_the_previews(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "OUT", tmp_path)
    folder = tmp_path / "deadbeef"
    folder.mkdir()
    (folder / serve.KINDS["poster"]).write_bytes(b"p" * 20)
    with serve.RUNS_LOCK:
        serve.RUNS["deadbeef"] = {"steps": [], "state": "running",
                                  "error": "", "values": {},
                                  "started": 0.0}
    try:
        state = serve.run_state("deadbeef")
        assert [p["kind"] for p in state["previews"]] == ["poster"]
    finally:
        with serve.RUNS_LOCK:
            serve.RUNS.pop("deadbeef", None)


def test_content_types_are_stated_not_guessed():
    """mimetypes.guess_type() reads the Windows registry, so a .png or .pdf
    can come back with a registry-specific type or none at all — and none
    fell through to application/octet-stream, which every browser downloads
    instead of showing. That turned the preview into an automatic download
    on Windows while it rendered fine everywhere else."""
    assert serve.content_type("site_map.png") == "image/png"
    assert serve.content_type("site_preview.pdf") == "application/pdf"
    assert serve.content_type("basemap.tif") == "image/tiff"
    assert serve.content_type("site.dxf") == "image/vnd.dxf"
    # Every file a run can produce has a stated type; none may fall through
    # to the octet-stream that forces a download.
    for kind, name in serve.KINDS.items():
        assert serve.content_type(name) != "application/octet-stream", kind


def test_the_preview_route_asks_the_browser_to_show_not_save():
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "serve.py").read_text(encoding="utf-8")
    # inline for the two preview routes, attachment only for downloads
    assert src.count('"Content-Disposition":\n                          f\'inline;') >= 1
    assert "attachment; filename=" in src
