"""Tests for the Google sign-in and Drive helpers (scripts/gdrive.py).

Everything here runs offline. The parts that talk to Google are thin
wrappers over urllib; what is worth testing is the logic around them —
PKCE, token refresh bookkeeping, query escaping, the multipart body, and
that an unconfigured server degrades instead of breaking.
"""

import base64
import hashlib
import json
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gdrive  # noqa: E402


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://host/auth/callback")


# ------------------------------------------------------------- config
def test_unconfigured_server_reports_rather_than_crashes(monkeypatch):
    """With no credentials the app must still run — the Drive button is
    simply not offered — so configured() is what the UI branches on."""
    for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
              "GOOGLE_REDIRECT_URI"):
        monkeypatch.delenv(k, raising=False)
    assert gdrive.configured() is False
    with pytest.raises(gdrive.DriveError):
        gdrive.start_login()


def test_partial_configuration_is_not_configured(monkeypatch, configured):
    monkeypatch.delenv("GOOGLE_REDIRECT_URI")
    assert gdrive.configured() is False


# -------------------------------------------------------------- PKCE
def test_start_login_builds_a_valid_pkce_challenge(configured):
    url, state, verifier = gdrive.start_login()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == [expected]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] == [state]
    assert "=" not in q["code_challenge"][0]      # base64url, unpadded


def test_start_login_asks_for_a_refresh_token(configured):
    """Without access_type=offline and prompt=consent, Google hands back no
    refresh token and every session dies an hour later, unrenewable."""
    url, _, _ = gdrive.start_login()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]


def test_start_login_requests_only_drive_file(configured):
    """drive.file reaches only files this app created. Asking for `drive`
    would grant read access to everything the user owns."""
    url, _, _ = gdrive.start_login()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    scopes = q["scope"][0].split()
    assert "https://www.googleapis.com/auth/drive.file" in scopes
    assert not any(s.endswith("/auth/drive") or s.endswith("drive.readonly")
                   for s in scopes)


def test_each_login_is_unique(configured):
    a = gdrive.start_login()
    b = gdrive.start_login()
    assert a[1] != b[1] and a[2] != b[2]


# ------------------------------------------------------------ tokens
def test_refresh_response_keeps_the_existing_refresh_token():
    """Google omits refresh_token when refreshing. Dropping it would make
    the second refresh impossible."""
    previous = {"refresh_token": "keep-me", "email": "a@b.c"}
    sess = gdrive._session_from_token(
        {"access_token": "new", "expires_in": 3600}, previous=previous)
    assert sess["refresh_token"] == "keep-me"
    assert sess["email"] == "a@b.c"


def test_a_fresh_token_overrides_the_old_one():
    sess = gdrive._session_from_token(
        {"access_token": "a", "refresh_token": "brand-new", "expires_in": 60},
        previous={"refresh_token": "old"})
    assert sess["refresh_token"] == "brand-new"


def test_valid_token_passes_through_a_live_token(configured):
    sess = {"access_token": "live", "expires_at": time.time() + 3600,
            "refresh_token": "r"}
    token, out = gdrive.valid_token(sess)
    assert token == "live" and out is sess


def test_valid_token_refreshes_just_before_expiry(configured, monkeypatch):
    """A token valid for another 10 seconds is not good enough — the
    request it is used for can outlive it."""
    calls = []
    monkeypatch.setattr(gdrive, "_post_form",
                        lambda url, fields: calls.append(fields) or
                        {"access_token": "renewed", "expires_in": 3600})
    sess = {"access_token": "stale", "expires_at": time.time() + 10,
            "refresh_token": "r"}
    token, out = gdrive.valid_token(sess)
    assert token == "renewed"
    assert calls[0]["grant_type"] == "refresh_token"
    assert out["refresh_token"] == "r"


def test_refresh_without_a_refresh_token_is_a_clear_error():
    with pytest.raises(gdrive.DriveError, match="[Ss]ign in again"):
        gdrive.refresh({"access_token": "x"})


# ------------------------------------------------------------- Drive
def test_folder_query_escapes_apostrophes():
    """A project named "Wat Pho's site" would otherwise terminate the query
    string early and change what it matches."""
    assert gdrive._escape("Wat Pho's site") == "Wat Pho\\'s site"
    assert gdrive._escape("back\\slash") == "back\\\\slash"


def test_multipart_body_carries_metadata_then_bytes():
    body, ctype = gdrive._multipart({"name": "site.dxf"}, b"\x00DXF",
                                    "image/vnd.dxf")
    boundary = ctype.split("boundary=")[1]
    assert body.count(boundary.encode()) == 3      # two parts + terminator
    assert b'"name": "site.dxf"' in body
    assert b"\x00DXF" in body
    assert body.rstrip().endswith(f"--{boundary}--".encode())


def test_upload_project_reports_missing_files_instead_of_aborting(monkeypatch,
                                                                 tmp_path):
    """A run without a PNG must still upload its DXF; a partial upload the
    user can see beats an all-or-nothing failure."""
    real = tmp_path / "site.dxf"
    real.write_bytes(b"dxf")
    monkeypatch.setattr(gdrive, "find_or_create_folder",
                        lambda *a, **k: "folder-id")
    monkeypatch.setattr(gdrive, "upload_file",
                        lambda *a, **k: {"name": "site.dxf"})
    out = gdrive.upload_project("tok", "proj", [
        (real, "image/vnd.dxf"),
        (tmp_path / "missing.png", "image/png"),
    ])
    assert out["uploaded"] == ["site.dxf"]
    assert out["skipped"] == [("missing.png", "not generated for this run")]
    assert out["link"].endswith("folder-id")


def test_upload_project_survives_one_failing_file(monkeypatch, tmp_path):
    a, b = tmp_path / "a.dxf", tmp_path / "b.csv"
    a.write_bytes(b"1")
    b.write_bytes(b"2")

    def flaky(token, path, parent, **k):
        if Path(path).name == "a.dxf":
            raise gdrive.DriveError("quota exceeded")
        return {"name": Path(path).name}

    monkeypatch.setattr(gdrive, "find_or_create_folder",
                        lambda *a_, **k: "f")
    monkeypatch.setattr(gdrive, "upload_file", flaky)
    out = gdrive.upload_project("tok", "proj",
                                [(a, "image/vnd.dxf"), (b, "text/csv")])
    assert out["uploaded"] == ["b.csv"]
    assert out["skipped"][0][0] == "a.dxf"
    assert "quota" in out["skipped"][0][1]


# ------------------------------------------------------ session store
def test_session_store_round_trips_and_is_private(tmp_path):
    store = gdrive.SessionStore(tmp_path / "s.json")
    store.put("sid1", {"access_token": "t", "email": "a@b.c"})
    assert store.get("sid1")["email"] == "a@b.c"
    assert store.get("nope") is None
    assert store.get(None) is None
    # refresh tokens must not be world-readable
    assert (tmp_path / "s.json").stat().st_mode & 0o077 == 0


def test_session_store_drop_removes_only_that_session(tmp_path):
    store = gdrive.SessionStore(tmp_path / "s.json")
    store.put("a", {"access_token": "1"})
    store.put("b", {"access_token": "2"})
    store.drop("a")
    assert store.get("a") is None
    assert store.get("b")["access_token"] == "2"


def test_session_store_tolerates_a_corrupt_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    store = gdrive.SessionStore(path)
    assert store.get("x") is None
    store.put("x", {"access_token": "t"})      # recovers by overwriting
    assert store.get("x")["access_token"] == "t"
    assert json.loads(path.read_text())["x"]["access_token"] == "t"
