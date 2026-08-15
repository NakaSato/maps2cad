#!/usr/bin/env python3
"""Google sign-in and Drive upload, over the standard library only.

serve.py has no third-party dependencies on purpose, and this keeps that
true: OAuth 2.0 and the Drive v3 API are both plain HTTPS and JSON, so
google-api-python-client would buy convenience at the cost of the one
property that lets serve.py run anywhere Python does.

Scope is `drive.file`, which grants access *only to files this app itself
creates*. It cannot read or touch anything else in someone's Drive, which
is the right level for a tool that writes deliverables and never reads them
back. Sign-in is per user: each engineer's drawings land in their own Drive.

Configure with three environment variables, from a Google Cloud project
with an OAuth 2.0 Web application client:

    GOOGLE_CLIENT_ID       xxxxxxxx.apps.googleusercontent.com
    GOOGLE_CLIENT_SECRET   GOCSPX-xxxxxxxx
    GOOGLE_REDIRECT_URI    https://your-host/auth/callback

The redirect URI must match one registered on the client exactly, scheme
and all. With none of these set the app runs exactly as before and the
Drive button simply does not appear.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"

# drive.file only — files this app created. Deliberately not drive or
# drive.readonly: this tool writes deliverables and never reads a user's
# existing files, so asking for more would be asking for trust it does not
# need (and would drag the OAuth consent screen into Google verification).
SCOPES = "openid email https://www.googleapis.com/auth/drive.file"

ROOT_FOLDER = "maps2cad"

# Tokens are refreshed a minute early: a request that starts valid can
# still arrive expired.
EXPIRY_SKEW_S = 60


class DriveError(Exception):
    """Something went wrong talking to Google, phrased for a user."""


def configured() -> bool:
    """True when the three environment variables are all present."""
    return all(os.environ.get(k) for k in
               ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                "GOOGLE_REDIRECT_URI"))


def _client():
    if not configured():
        raise DriveError(
            "Google sign-in is not configured on this server. Set "
            "GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
            "GOOGLE_REDIRECT_URI to enable it.")
    return (os.environ["GOOGLE_CLIENT_ID"],
            os.environ["GOOGLE_CLIENT_SECRET"],
            os.environ["GOOGLE_REDIRECT_URI"])


# ----------------------------------------------------------------- OAuth
def start_login() -> tuple[str, str, str]:
    """Begin the authorization code flow.

    Returns (auth_url, state, verifier). Keep state and verifier against the
    browser session: state is what stops another site from replaying a code
    at our callback, and the PKCE verifier is what stops an intercepted code
    being exchanged by anyone but us.
    """
    client_id, _, redirect_uri = _client()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # offline + consent so we are actually given a refresh token;
        # without both, Google returns one only on the very first consent
        # and the session dies an hour later with no way to renew.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", state, verifier


def _post_form(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise DriveError(f"Google rejected the request ({e.code}): {detail}")
    except urllib.error.URLError as e:
        raise DriveError(f"Could not reach Google: {e.reason}")


def exchange_code(code: str, verifier: str) -> dict:
    """Swap an authorization code for tokens. Returns a session dict."""
    client_id, client_secret, redirect_uri = _client()
    tok = _post_form(TOKEN_URL, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    })
    return _session_from_token(tok)


def _session_from_token(tok: dict, previous: dict | None = None) -> dict:
    sess = {
        "access_token": tok["access_token"],
        "expires_at": time.time() + float(tok.get("expires_in", 3600)),
        # A refresh response omits refresh_token — keep the one we hold, or
        # the second refresh has nothing to refresh with.
        "refresh_token": tok.get("refresh_token")
        or (previous or {}).get("refresh_token"),
        "email": (previous or {}).get("email", ""),
    }
    return sess


def refresh(session: dict) -> dict:
    if not session.get("refresh_token"):
        raise DriveError("This sign-in has expired. Sign in again.")
    client_id, client_secret, _ = _client()
    tok = _post_form(TOKEN_URL, {
        "refresh_token": session["refresh_token"],
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    })
    return _session_from_token(tok, previous=session)


def valid_token(session: dict) -> tuple[str, dict]:
    """Access token for this session, refreshing first if it is near expiry.
    Returns (token, session) — the session may have been replaced."""
    if session.get("expires_at", 0) - EXPIRY_SKEW_S > time.time():
        return session["access_token"], session
    session = refresh(session)
    return session["access_token"], session


# ------------------------------------------------------------- Drive API
def _api(token: str, url: str, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        if e.code in (401, 403):
            raise DriveError(
                "Google refused the upload — the sign-in may have been "
                f"revoked. Sign in again. ({e.code}: {detail})")
        raise DriveError(f"Drive returned {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise DriveError(f"Could not reach Drive: {e.reason}")


def userinfo(token: str) -> dict:
    return _api(token, USERINFO_URL)


def _escape(name: str) -> str:
    """Quote a name for a Drive query string. A project named with an
    apostrophe would otherwise break the query, or worse, change it."""
    return name.replace("\\", "\\\\").replace("'", "\\'")


def find_or_create_folder(token: str, name: str, parent: str | None = None
                          ) -> str:
    """Folder id for `name`, created under `parent` if it does not exist.
    Reused rather than duplicated, so re-running a project adds to its
    existing folder instead of littering Drive with copies."""
    q = (f"name = '{_escape(name)}' and mimeType = '{FOLDER_MIME}' "
         "and trashed = false")
    q += f" and '{_escape(parent)}' in parents" if parent else \
         " and 'root' in parents"
    found = _api(token, f"{FILES_URL}?"
                 + urllib.parse.urlencode({"q": q, "fields": "files(id,name)",
                                           "pageSize": "1"}))
    files = found.get("files") or []
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": FOLDER_MIME}
    if parent:
        meta["parents"] = [parent]
    made = _api(token, f"{FILES_URL}?fields=id", method="POST",
                body=json.dumps(meta).encode(),
                headers={"Content-Type": "application/json"})
    return made["id"]


def _multipart(meta: dict, data: bytes, mime: str) -> tuple[bytes, str]:
    """A Drive multipart/related upload body: JSON metadata, then bytes."""
    boundary = "----maps2cad" + secrets.token_hex(16)
    parts = [
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8"
        f"\r\n\r\n{json.dumps(meta)}\r\n".encode(),
        f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/related; boundary={boundary}"


def upload_file(token: str, path: Path, parent: str, name=None,
                mime="application/octet-stream") -> dict:
    """Upload one file into `parent`. Returns the created file resource."""
    data = Path(path).read_bytes()
    meta = {"name": name or Path(path).name, "parents": [parent]}
    body, ctype = _multipart(meta, data, mime)
    return _api(token, f"{UPLOAD_URL}?uploadType=multipart"
                       "&fields=id,name,webViewLink",
                method="POST", body=body, headers={"Content-Type": ctype})


def upload_project(token: str, project: str, files: list[tuple[Path, str]]
                   ) -> dict:
    """Put a run's outputs in Drive under maps2cad/<project>/.

    `files` is (path, mime). Returns {'folder': id, 'link': url,
    'uploaded': [names], 'skipped': [(name, why)]} — a file that fails is
    reported rather than aborting the rest, because a partial upload the
    user can see beats an all-or-nothing failure they cannot.
    """
    root = find_or_create_folder(token, ROOT_FOLDER)
    folder = find_or_create_folder(token, project, parent=root)
    uploaded, skipped = [], []
    for path, mime in files:
        p = Path(path)
        if not p.is_file():
            skipped.append((p.name, "not generated for this run"))
            continue
        try:
            res = upload_file(token, p, folder, mime=mime)
            uploaded.append(res.get("name", p.name))
        except DriveError as e:
            skipped.append((p.name, str(e)))
    return {
        "folder": folder,
        "link": f"https://drive.google.com/drive/folders/{folder}",
        "uploaded": uploaded,
        "skipped": skipped,
    }


# ------------------------------------------------- session persistence
class SessionStore:
    """Browser session id -> Google tokens, in a JSON file.

    Kept out of the staging database on purpose: that file holds survey
    data and gets copied, inspected and handed around, and OAuth refresh
    tokens have no business travelling with it. Written 0600.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            return self._read().get(sid)

    def put(self, sid, session) -> None:
        with self._lock:
            data = self._read()
            data[sid] = session
            self._write(data)

    def drop(self, sid) -> None:
        with self._lock:
            data = self._read()
            if data.pop(sid, None) is not None:
                self._write(data)
