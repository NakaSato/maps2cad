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
