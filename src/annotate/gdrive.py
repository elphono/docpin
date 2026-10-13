"""Google Drive as the place a session travels through: four calls, and OAuth.

The files live in the **application data folder** of the user's Drive
(`appDataFolder`, scope `drive.appdata`): a hidden folder only this OAuth
client can see, shared by every machine where the user connects it, and
counted in their Drive quota. docpin sees nothing else of the Drive, and the
Drive sees nothing of a session: what is put there is sealed first (vault.py).

    put(name, bytes)   resumable upload: one request for the metadata, one for the bytes
    list()             the files, every page of them
    get(id)            the bytes
    delete(id)         gone (a file already gone is not an error)

The OAuth client is the user's own ("Desktop app" type, created once in the
Google Cloud console): the authorization page sends the browser back to the
daemon (`http://127.0.0.1:<port>/sync/google/callback`), with PKCE, and the
refresh token is kept in the sync settings (sync.py), readable by the user only.

Every URL is a module constant: the tests point them at a fake server.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_URL = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.appdata"
FOLDER = "appDataFolder"
TIMEOUT = 60.0
# An access token is renewed this long before Google says it expires.
EXPIRY_MARGIN = 60.0


class DriveError(Exception):
    """Google Drive could not be reached, or refused the request."""


def _request(method: str, url: str, *, headers: dict[str, str] | None = None,
             data: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise DriveError(f"cannot reach Google ({urllib.parse.urlsplit(url).netloc}): "
                         f"{getattr(exc, 'reason', exc)}") from exc


def _reason(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace").strip()[:200]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    detail = data.get("error_description") if isinstance(data, dict) else None
    return f"{error}: {detail}" if detail else str(error or data)[:200]


# -- OAuth --------------------------------------------------------------------

def authorization(client_id: str, redirect_uri: str) -> tuple[str, str, str]:
    """(URL of Google's consent page, state, PKCE verifier). `prompt=consent`
    and `access_type=offline`: Google gives a refresh token every time."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge.decode().rstrip("="), "code_challenge_method": "S256"})
    return f"{AUTH_URL}?{query}", state, verifier


def _token(fields: dict[str, str]) -> dict[str, Any]:
    status, _, body = _request(
        "POST", TOKEN_URL, data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        raise DriveError(f"Google refused the authorization ({status}, {_reason(body)}): "
                         f"connect Google Drive again")
    data = json.loads(body)
    if not isinstance(data, dict) or not data.get("access_token"):
        raise DriveError("Google answered without an access token")
    return data


def exchange(client_id: str, client_secret: str, code: str, verifier: str,
             redirect_uri: str) -> dict[str, Any]:
    """The tokens for the code Google sent back to the callback."""
    data = _token({"client_id": client_id, "client_secret": client_secret, "code": code,
                   "code_verifier": verifier, "redirect_uri": redirect_uri,
                   "grant_type": "authorization_code"})
    if not data.get("refresh_token"):
        raise DriveError("Google gave no refresh token: remove docpin from your Google "
                         "account's third-party access, then connect again")
    return data


# -- the store ----------------------------------------------------------------

class DriveStore:
    """The application data folder, through one refresh token."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str) -> None:
        if not (client_id and client_secret and refresh_token):
            raise DriveError("Google Drive is not connected")
        self._client = (client_id, client_secret)
        self._refresh = refresh_token
        self._access = ""
        self._expires = 0.0

    def _authorization(self, renew: bool = False) -> str:
        if renew or not self._access or time.time() >= self._expires - EXPIRY_MARGIN:
            data = _token({"client_id": self._client[0], "client_secret": self._client[1],
                           "refresh_token": self._refresh, "grant_type": "refresh_token"})
            self._access = str(data["access_token"])
            self._expires = time.time() + float(data.get("expires_in") or 3600)
        return f"Bearer {self._access}"

    def _call(self, method: str, url: str, *, headers: dict[str, str] | None = None,
              data: bytes | None = None, ok: tuple[int, ...] = (200,)
              ) -> tuple[int, dict[str, str], bytes]:
        """One request; on a 401 the token is renewed and the request made once more."""
        for renew in (False, True):
            sent = {**(headers or {}), "Authorization": self._authorization(renew)}
            status, answer, body = _request(method, url, headers=sent, data=data)
            if status != 401:
                break
        if status not in ok:
            raise DriveError(f"Google Drive {method} failed ({status}): {_reason(body)}")
        return status, answer, body

    def put(self, name: str, data: bytes) -> str:
        """Upload `data` as a new file `name`; return its id."""
        meta = json.dumps({"name": name, "parents": [FOLDER]}).encode()
        _, answer, _ = self._call(
            "POST", f"{UPLOAD_URL}/files?uploadType=resumable", data=meta,
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": "application/octet-stream",
                     "X-Upload-Content-Length": str(len(data))})
        location = next((v for k, v in answer.items() if k.lower() == "location"), "")
        if not location:
            raise DriveError("Google Drive gave no upload address")
        _, _, body = self._call("PUT", location, data=data, ok=(200, 201),
                                headers={"Content-Type": "application/octet-stream"})
        file_id = json.loads(body).get("id")
        if not isinstance(file_id, str) or not file_id:
            raise DriveError("Google Drive stored the file without saying its id")
        return file_id

    def list(self) -> list[dict[str, str]]:
        """Every file of the folder: `{id, name}`."""
        found: list[dict[str, str]] = []
        token = ""
        while True:
            query = {"spaces": FOLDER, "pageSize": "1000",
                     "fields": "nextPageToken,files(id,name)"}
            if token:
                query["pageToken"] = token
            _, _, body = self._call("GET", f"{API_URL}/files?{urllib.parse.urlencode(query)}")
            data = json.loads(body)
            found += [{"id": str(f["id"]), "name": str(f["name"])}
                      for f in data.get("files", []) if f.get("id") and f.get("name")]
            token = str(data.get("nextPageToken") or "")
            if not token:
                return found

    def get(self, file_id: str) -> bytes:
        _, _, body = self._call("GET", f"{API_URL}/files/{urllib.parse.quote(file_id)}?alt=media")
        return body

    def delete(self, file_id: str) -> None:
        self._call("DELETE", f"{API_URL}/files/{urllib.parse.quote(file_id)}", ok=(200, 204, 404))
