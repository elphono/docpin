"""Carrying a conversation to the other machine (sync.py, vault.py, gdrive.py).

Two machines live in one test: each has its own data folder, its own Claude
Code projects folder and its own clone of one repository, at a different
path, as on the measure of 2026-10-12. The store between them is a folder
(FolderStore), or a fake Google that checks what the real one checks.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from annotate import claude, gdrive, server, sync, vault
from annotate.config import Config
from fakes import RecordingOpener, compact

SID = "f0202e40-dc93-4194-a3da-c0112e0dd7d3"
PASSPHRASE = "correct horse battery"


def git(*args: str, cwd: Path) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True, timeout=30)
    return done.stdout.strip()


class Machine:
    """One machine: entering it points docpin and `claude` at its folders."""

    def __init__(self, root: Path, name: str, clone: Path, monkeypatch) -> None:
        self.root, self.name, self.clone, self._mp = root, name, clone, monkeypatch
        self.projects = root / "projects"

    def __enter__(self) -> Machine:
        self._mp.setenv("ANNOTATE_DATA_DIR", str(self.root / "data"))
        self._mp.setenv("ANNOTATE_WORKSPACE", str(self.root / "workspace"))
        self._mp.setattr(claude, "projects_dir", lambda: self.projects)
        sync._version.clear()
        sync._stores.clear()
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def set_up(self, store: Path, passphrase: str = PASSPHRASE) -> None:
        with self:
            sync.update({"store": "folder", "folder": str(store), "passphrase": passphrase,
                         "machine_name": self.name, "enabled": True})
            sync.allow(str(self.clone))


class Pair:
    def __init__(self, a: Machine, b: Machine, store: Path, remote: Path) -> None:
        self.a, self.b, self.store, self.remote = a, b, store, remote


@pytest.fixture
def pair(tmp_path, monkeypatch) -> Pair:
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    a_clone = tmp_path / "A" / "work" / "proj"
    a_clone.parent.mkdir(parents=True)
    git("clone", "-q", str(remote), str(a_clone), cwd=tmp_path)
    (a_clone / "README.md").write_text("demo\n")
    git("add", ".", cwd=a_clone)
    git("commit", "-qm", "init", cwd=a_clone)
    git("push", "-q", "-u", "origin", "main", cwd=a_clone)
    # B finds its clone in its workspace; A, in the transcripts of its sessions.
    b_clone = tmp_path / "B" / "workspace" / "proj"
    b_clone.parent.mkdir(parents=True)
    git("clone", "-q", str(remote), str(b_clone), cwd=tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    a = Machine(tmp_path / "A", "laptop", a_clone, monkeypatch)
    b = Machine(tmp_path / "B", "mini-pc", b_clone, monkeypatch)
    a.set_up(store)
    b.set_up(store)
    with a:
        session_lines(a, "first message on A")
    return Pair(a, b, store, remote)


def session_lines(machine: Machine, text: str) -> Path:
    """Append one exchange to the session's transcript on `machine`, written
    in its own clone, as Claude Code would."""
    path = claude.transcript_of(SID) or \
        sync.project_folder(str(machine.clone)) / f"{SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for kind in ("user", "assistant"):
            handle.write(compact({"type": kind, "sessionId": SID, "cwd": str(machine.clone),
                                  "version": "2.1.296", "message": {"content": text}}) + "\n")
    return path


def stored(pair: Pair) -> list[str]:
    return sorted(p.name for p in pair.store.iterdir() if p.name.startswith(SID))


# ------------------------------------------------------------------ the trip

def test_a_session_goes_to_the_other_machine_and_back_three_times(pair):
    for trip in range(3):
        here, there = (pair.a, pair.b) if trip % 2 == 0 else (pair.b, pair.a)
        with here:
            path = session_lines(here, f"trip {trip} from {here.name}")
            sent = path.read_bytes()
            sync.send(SID)
            assert len(stored(pair)) == 1                       # the older copy is gone
            assert sync.poll_once()["received"] == []           # its own send stays
        with there:
            assert sync.poll_once()["received"] == [SID]
            placed = sync.project_folder(str(there.clone)) / f"{SID}.jsonl"
            assert placed.read_bytes() == sent
            assert claude.resumable(SID)
            [arrival] = sync.overview()["arrivals"]
            assert (arrival["status"], arrival["cwd"], arrival["from"]) == \
                ("ready", str(there.clone), here.name)
            assert arrival["sent_cwd"] == str(here.clone)
            assert sync.poll_once()["received"] == []           # nothing new


def test_the_store_never_holds_a_readable_transcript(pair):
    with pair.a:
        session_lines(pair.a, "SECRET-MARKER-1234")
        transcript = claude.transcript_of(SID)
        assert transcript is not None and b"SECRET-MARKER-1234" in transcript.read_bytes()
        sync.send(SID)
    [name] = stored(pair)
    blob = (pair.store / name).read_bytes()
    assert b"SECRET-MARKER" not in blob and str(pair.a.clone).encode() not in blob


def test_resume_here_opens_the_conversation_in_this_machines_clone(pair):
    with pair.a:
        sync.send(SID)
    with pair.b:
        sync.poll_once()
        opener = RecordingOpener()
        sync.resume(SID, Config(), opener=opener)
    [call] = opener.calls
    assert (call["resume"], call["cwd"]) == (SID, str(pair.b.clone))
    assert "moved from the machine laptop" in call["prompt"]
    assert str(pair.b.clone) in call["prompt"] and str(pair.a.clone) in call["prompt"]
    assert not str(call["prompt"]).startswith("-")


# --------------------------------------------------------- what may leave

def test_nothing_leaves_a_folder_the_user_did_not_allow(pair):
    with pair.a:
        sync.disallow(str(pair.a.clone))
        with pytest.raises(sync.SyncRefused, match="not in a folder allowed"):
            sync.send(SID)
        assert stored(pair) == []
        sync.allow(str(pair.a.clone.parent))                   # the witness
        sync.send(SID)
    assert len(stored(pair)) == 1


def test_sync_off_neither_sends_nor_polls(pair):
    with pair.a:
        sync.send(SID)
    with pair.b:
        sync.update({"enabled": False})
        assert sync.poll_once() == {"skipped": True}
        with pytest.raises(sync.SyncError, match="sync is off"):
            sync.send(SID)
        sync.update({"enabled": True})                          # the witness
        assert sync.poll_once()["received"] == [SID]


def test_sync_cannot_be_turned_on_before_it_is_set_up(tmp_path):
    with pytest.raises(sync.SyncError, match="passphrase.*Google Drive connected"):
        sync.update({"enabled": True})
    assert not sync.load_settings()["enabled"]


# ------------------------------------------------------- refusal or failure

class CountingStore(sync.FolderStore):
    def __init__(self, folder: Path, fail_lists: int = 0) -> None:
        super().__init__(folder)
        self.gets = 0
        self.fail_lists = fail_lists

    def list(self) -> list[dict[str, str]]:
        if self.fail_lists:
            self.fail_lists -= 1
            raise gdrive.DriveError("cannot reach Google: timed out")
        return super().list()

    def get(self, file_id: str) -> bytes:
        self.gets += 1
        return super().get(file_id)


def test_a_wrong_passphrase_is_reported_once_and_never_fetched_again(pair):
    with pair.a:
        sync.send(SID)
    with pair.b:
        sync.update({"passphrase": "another passphrase"})
        store = CountingStore(pair.store)
        for _ in range(3):
            sync.poll_once(store)
        assert store.gets == 1
        [arrival] = sync.overview()["arrivals"]
        assert arrival["status"] == "error" and "wrong passphrase" in arrival["error"]
        assert not (sync.project_folder(str(pair.b.clone)) / f"{SID}.jsonl").exists()


def test_an_unreachable_store_is_tried_again_at_the_next_pass(pair):
    with pair.a:
        sync.send(SID)
    with pair.b:
        store = CountingStore(pair.store, fail_lists=1)
        assert "timed out" in sync.poll_once(store)["error"]
        assert "timed out" in sync.overview()["last_poll"]["error"]
        assert sync.poll_once(store)["received"] == [SID]
        assert sync.overview()["last_poll"]["error"] == ""


def test_a_conversation_without_a_known_clone_waits_for_its_folder(pair, tmp_path):
    with pair.a:
        sync.send(SID)
    elsewhere = tmp_path / "B" / "elsewhere"
    elsewhere.mkdir()
    pair.b.clone.rename(elsewhere / "proj")                     # not in B's workspace any more
    with pair.b:
        sync.poll_once()
        [arrival] = sync.overview()["arrivals"]
        assert (arrival["status"], arrival["cwd"]) == ("needs-folder", "")
        with pytest.raises(sync.SyncError, match="choose the folder"):
            sync.resume(SID, Config(), opener=RecordingOpener())
        placed = sync.place(SID, str(elsewhere / "proj"))
        assert placed["status"] == "ready"
        assert claude.resumable(SID)
        opener = RecordingOpener()
        sync.resume(SID, Config(), opener=opener)
        assert opener.calls[0]["cwd"] == str(elsewhere / "proj")
    with pair.a:                                                # the next send goes there too
        session_lines(pair.a, "again")
        sync.send(SID)
    with pair.b:
        sync.poll_once()
        [arrival] = sync.overview()["arrivals"]
        assert (arrival["status"], arrival["cwd"]) == ("ready", str(elsewhere / "proj"))


def test_a_received_transcript_is_no_evidence_of_where_a_clone_lives(pair):
    """The first line of a received transcript names the OTHER machine's
    folder, which may exist here as well (same user name, same layout)."""
    other = "0e0e0e0e-1111-4222-8333-444444444444"
    with pair.a:
        sync.send(SID)
        path = sync.project_folder(str(pair.a.clone)) / f"{other}.jsonl"
        path.write_text(compact({"type": "user", "sessionId": other, "cwd": str(pair.a.clone),
                                 "message": {"content": "hi"}}) + "\n")
    with pair.b:
        sync.poll_once()                                        # SID, from A's folder
    with pair.a:
        sync.send(other)
    with pair.b:
        sync.poll_once()
        arrivals = {a["session_id"]: a for a in sync.overview()["arrivals"]}
    assert arrivals[other]["cwd"] == str(pair.b.clone)


def test_an_arrival_says_what_git_did_not_carry(pair):
    (pair.a.clone / "draft.txt").write_text("not committed\n")
    with pair.a:
        result = sync.send(SID)
    assert any("Uncommitted changes" in w for w in result["warnings"])
    git("commit", "-q", "--allow-empty", "-m", "later", cwd=pair.a.clone)  # not pushed
    with pair.a:
        result = sync.send(SID)
    assert any("1 commit(s) not pushed" in w for w in result["warnings"])
    with pair.b:
        sync.poll_once()
        [arrival] = sync.overview()["arrivals"]
    assert any("pull before resuming" in w for w in arrival["warnings"])
    assert any("not pushed" in w for w in arrival["warnings"])


# ----------------------------------------------------------------- secrets

def test_the_page_never_sees_a_secret_and_the_file_is_the_users_only(pair):
    with pair.a:
        sync.update({"client_id": "id.apps", "client_secret": "the-client-secret"})
        sync.connect_google("the-refresh-token")
        shown = json.dumps(sync.overview())
        on_disk = sync.settings_path().read_text()
        mode = stat.S_IMODE(sync.settings_path().stat().st_mode)
    for secret in (PASSPHRASE, "the-client-secret", "the-refresh-token"):
        assert secret in on_disk                                # the witness
        assert secret not in shown
    assert json.loads(shown)["settings"]["google_connected"] is True
    assert mode == 0o600


def test_a_new_client_id_drops_the_token_of_the_old_one(pair):
    with pair.a:
        sync.update({"client_id": "one"})
        sync.connect_google("token-of-one")
        sync.update({"client_id": "one"})
        assert sync.load_settings()["refresh_token"] == "token-of-one"
        sync.update({"client_id": "two"})
        assert sync.load_settings()["refresh_token"] == ""


# --------------------------------------------------------------- the pieces

def test_seal_round_trips_and_refuses_a_wrong_passphrase_or_a_damaged_byte():
    data = b"line one\n" * 1000
    sealed = vault.seal(data, PASSPHRASE)
    assert vault.unseal(sealed, PASSPHRASE) == data
    assert len(sealed) < len(data)                              # compressed first
    with pytest.raises(vault.VaultError, match="wrong passphrase"):
        vault.unseal(sealed, "not the passphrase")
    damaged = bytearray(sealed)
    damaged[-5] ^= 1
    with pytest.raises(vault.VaultError, match="damaged"):
        vault.unseal(bytes(damaged), PASSPHRASE)
    with pytest.raises(vault.VaultError, match="not something docpin sealed"):
        vault.unseal(b"plain text", PASSPHRASE)


def test_the_passphrase_never_reaches_a_command_line(monkeypatch):
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(argv, *args, **kwargs):
        seen.append([str(a) for a in argv])
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(vault.subprocess, "run", spy)
    vault.unseal(vault.seal(b"x", PASSPHRASE), PASSPHRASE)
    assert len(seen) == 2 and not any(PASSPHRASE in " ".join(argv) for argv in seen)


@pytest.mark.parametrize("url", ["git@github.com:me/docpin.git", "https://github.com/me/docpin",
                                 "ssh://git@GitHub.com/me/docpin.git",
                                 "https://user@github.com/me/docpin.git/"])
def test_every_spelling_of_a_remote_names_one_repository(url):
    assert sync.normalise_remote(url) == "github.com/me/docpin"


def test_a_different_repository_is_not_the_same_one():
    assert sync.normalise_remote("git@github.com:me/other.git") != \
        sync.normalise_remote("git@github.com:me/docpin.git")


def test_the_project_folder_is_named_as_claude_code_names_it(tmp_path):
    # Measured on 2026-10-12 with Claude Code itself (the session of the ROADMAP).
    cwd = "/tmp/claude-1001/-home-x-y/sonde/B/home/code/proj-parent/proj"
    assert sync.project_folder(cwd).name == \
        "-tmp-claude-1001--home-x-y-sonde-B-home-code-proj-parent-proj"
    with pytest.raises(sync.SyncError, match="does not document"):
        sync.project_folder("/" + "a" * 250)


def test_a_file_name_docpin_did_not_write_is_ignored():
    assert sync.parse_name(f"{SID}.1760000000000.0123abcd{sync.SUFFIX}") == \
        (SID, 1760000000000, "0123abcd")
    for name in ("notes.txt", f"{SID}.dpsync", f"../{SID}.1760000000000.0123abcd.dpsync"):
        assert sync.parse_name(name) is None


# ------------------------------------------------------------ Google Drive

class FakeGoogle(BaseHTTPRequestHandler):
    """What Google checks: the PKCE verifier against the challenge, the
    bearer token, the application data folder, the declared upload size."""

    def log_message(self, *args: object) -> None:
        pass

    def _reply(self, status: int, body: object = b"", headers: dict[str, str] | None = None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _authorised(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.server.access}":
            self._reply(401, {"error": {"message": "Invalid Credentials"}})
            return False
        return True

    def do_POST(self) -> None:  # noqa: N802
        g = self.server
        url = urlsplit(self.path)
        if url.path == "/token":
            form = {k: v[0] for k, v in parse_qs(self._body().decode()).items()}
            if form["grant_type"] == "authorization_code":
                digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
                if form["code"] != "the-code" or challenge != g.challenge:
                    return self._reply(400, {"error": "invalid_grant"})
                g.refreshes += 1
                g.access = f"access-{g.refreshes}"
                return self._reply(200, {"access_token": g.access, "expires_in": 3599,
                                         "refresh_token": "the-refresh"})
            if form.get("refresh_token") != "the-refresh":
                return self._reply(400, {"error": "invalid_grant",
                                         "error_description": "Token has been expired or revoked."})
            g.refreshes += 1
            g.access = f"access-{g.refreshes}"
            return self._reply(200, {"access_token": g.access, "expires_in": 3599})
        if url.path == "/upload/files" and self._authorised():
            meta = json.loads(self._body())
            assert parse_qs(url.query) == {"uploadType": ["resumable"]}
            assert meta["parents"] == ["appDataFolder"]
            g.uploads.append((meta["name"], int(self.headers["X-Upload-Content-Length"])))
            host = self.headers["Host"]
            return self._reply(200, headers={"Location": f"http://{host}/session/{len(g.uploads)}"})
        self._reply(404)

    def do_PUT(self) -> None:  # noqa: N802
        g = self.server
        if self.path.startswith("/session/") and self._authorised():
            name, size = g.uploads[int(self.path.rsplit("/", 1)[1]) - 1]
            data = self._body()
            assert len(data) == size
            g.next_id += 1
            g.files[f"id{g.next_id}"] = (name, data)
            return self._reply(200, {"id": f"id{g.next_id}", "name": name})
        self._reply(404)

    def do_GET(self) -> None:  # noqa: N802
        g = self.server
        url = urlsplit(self.path)
        if not self._authorised():
            return
        if url.path == "/files":
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            assert query["spaces"] == "appDataFolder"
            ids = sorted(g.files)
            start = int(query.get("pageToken") or 0)
            page = ids[start:start + 2]                         # two per page: paging is used
            body: dict[str, object] = {"files": [{"id": i, "name": g.files[i][0]} for i in page]}
            if start + 2 < len(ids):
                body["nextPageToken"] = str(start + 2)
            return self._reply(200, body)
        if url.path.startswith("/files/") and parse_qs(url.query) == {"alt": ["media"]}:
            found = g.files.get(url.path.rsplit("/", 1)[1])
            return self._reply(200, found[1]) if found else self._reply(404, {"error": {"message": "File not found"}})
        self._reply(404)

    def do_DELETE(self) -> None:  # noqa: N802
        g = self.server
        if self._authorised():
            gone = g.files.pop(self.path.rsplit("/", 1)[1], None)
            self._reply(204 if gone else 404)


@pytest.fixture
def google(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
    srv.access, srv.refreshes, srv.challenge = "", 0, ""           # type: ignore[attr-defined]
    srv.uploads, srv.files, srv.next_id = [], {}, 0               # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(gdrive, "AUTH_URL", f"{base}/auth")
    monkeypatch.setattr(gdrive, "TOKEN_URL", f"{base}/token")
    monkeypatch.setattr(gdrive, "API_URL", base)
    monkeypatch.setattr(gdrive, "UPLOAD_URL", f"{base}/upload")
    yield srv
    srv.shutdown()
    srv.server_close()


def consent(google, redirect: str = "http://127.0.0.1:1/") -> tuple[str, sync.GoogleLogins]:
    """Start a connection the way the page does, and play Google's consent page."""
    sync.update({"client_id": "id.apps", "client_secret": "secret"})
    logins = sync.GoogleLogins()
    url = logins.start(redirect)
    query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert (query["scope"], query["redirect_uri"]) == (gdrive.SCOPE, redirect)
    google.challenge = query["code_challenge"]
    return query["state"], logins


def test_connecting_google_keeps_the_refresh_token_once_the_verifier_matches(google):
    state, logins = consent(google)
    with pytest.raises(sync.SyncError, match="matches no connection"):
        logins.finish("another-state", "the-code")
    assert not sync.load_settings()["refresh_token"]
    logins.finish(state, "the-code")
    assert sync.load_settings()["refresh_token"] == "the-refresh"
    with pytest.raises(sync.SyncError, match="matches no connection"):
        logins.finish(state, "the-code")                         # a state serves once


def test_the_drive_store_puts_lists_gets_and_deletes_in_the_app_folder(google):
    store = gdrive.DriveStore("id.apps", "secret", "the-refresh")
    ids = [store.put(f"{SID}.{1760000000000 + n}.0123abcd.dpsync", bytes([n]) * (n + 1))
           for n in range(5)]
    assert [f["id"] for f in store.list()] == sorted(ids)       # three pages
    assert store.get(ids[3]) == b"\x03" * 4
    store.delete(ids[3])
    store.delete(ids[3])                                         # already gone: no error
    assert len(store.list()) == 4
    assert google.refreshes == 1                                 # one token for all of it


def test_a_rejected_token_is_renewed_once_and_a_revoked_one_says_so(google):
    store = gdrive.DriveStore("id.apps", "secret", "the-refresh")
    store.list()
    google.access = "rotated-by-google"
    store.list()
    assert google.refreshes == 2
    revoked = gdrive.DriveStore("id.apps", "secret", "revoked")
    with pytest.raises(gdrive.DriveError, match="connect Google Drive again"):
        revoked.list()


def test_two_machines_meet_through_google_drive(pair, google):
    for machine in (pair.a, pair.b):
        with machine:
            sync.update({"enabled": False, "store": "gdrive", "client_id": "id.apps",
                         "client_secret": "secret"})
            sync.connect_google("the-refresh")
            sync.update({"enabled": True})
    with pair.a:
        sync.send(SID)
        sync.send(SID)
    assert len(google.files) == 1
    with pair.b:
        assert sync.poll_once()["received"] == [SID]
        assert sync.overview()["arrivals"][0]["status"] == "ready"


# ------------------------------------------------------------- the daemon

@pytest.fixture
def daemon():
    srv = server.make_server(Config(), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def ask(srv, method: str, path: str, body: object = None, client: bool = True):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    headers = {"Host": f"localhost:{srv.port}"}
    if client:
        headers["X-Annotate"] = "test"
    conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, dict(response.getheaders()), data


def test_the_settings_change_only_with_the_client_header(daemon):
    status, _, _ = ask(daemon, "POST", "/api/sync", {"machine_name": "evil"}, client=False)
    assert status == 403
    status, _, _ = ask(daemon, "POST", "/api/sync", {"machine_name": "desk"})
    assert status == 200
    status, _, body = ask(daemon, "GET", "/api/sync")
    assert json.loads(body)["settings"]["machine_name"] == "desk"


def test_googles_answer_comes_back_to_the_home_page(daemon, google):
    status, _, body = ask(daemon, "POST", "/api/sync/google")
    assert status == 409 and b"client id" in body                # not set up yet
    sync.update({"client_id": "id.apps", "client_secret": "secret"})
    status, _, body = ask(daemon, "POST", "/api/sync/google")
    query = {k: v[0] for k, v in parse_qs(urlsplit(json.loads(body)["url"]).query).items()}
    assert query["redirect_uri"] == f"http://127.0.0.1:{daemon.port}/"
    google.challenge = query["code_challenge"]
    status, headers, _ = ask(daemon, "GET", "/?state=forged&code=the-code", client=False)
    assert status == 303 and headers["Location"].startswith("/#sync-error=")
    assert not sync.load_settings()["refresh_token"]
    status, headers, _ = ask(daemon, "GET", f"/?state={query['state']}&code=the-code",
                             client=False)
    assert (status, headers["Location"]) == (303, "/#sync-connected")
    assert sync.load_settings()["refresh_token"] == "the-refresh"


def test_a_session_that_may_not_leave_is_a_403(daemon, pair):
    with pair.a:
        sync.disallow(str(pair.a.clone))
        status, _, body = ask(daemon, "POST", "/api/sync/send", {"session": SID})
    assert status == 403 and b"not in a folder allowed" in body
    assert os.listdir(pair.store) == []


def test_sync_send_typed_in_a_session_sends_that_session(daemon, pair, monkeypatch, capsys):
    from annotate import cli
    monkeypatch.setenv("ANNOTATE_PORT", str(daemon.port))
    with pair.a:
        assert cli.main(["sync", "send"]) == 1
        assert "which session" in capsys.readouterr().err
        assert stored(pair) == []
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID)
        assert cli.main(["sync", "send"]) == 0
    assert "sent: the other machine takes it" in capsys.readouterr().out
    assert len(stored(pair)) == 1


def test_a_machine_never_receives_its_own_send_even_after_losing_its_state(pair):
    with pair.a:
        sync.send(SID)
        sync.state_path().unlink()                              # what was seen is gone
        assert sync.poll_once()["received"] == []
    with pair.b:                                                # the witness
        assert sync.poll_once()["received"] == [SID]
