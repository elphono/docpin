"""Carry a conversation to the user's other machine, through a store both can reach.

    machine A                        store (Google Drive, or a synced folder)       machine B
    Send ─► the transcript, sealed ─► <session>.<ms>.<machine>.dpsync ◄─ every 60 s: list
            (vault.py), with where                                       │ new from another machine?
            it ran: repository, branch, head                             ▼
                                                     unsealed, the transcript put where
                                                     `claude --resume <id>` finds it, in the
                                                     clone of the same repository; "Resume here"

Measured on 2026-10-12 (two isolated homes, the project at a different path on
each, ROADMAP): a transcript copied into the other machine's project folder
resumes there, intact, works in the new folder and never touches the old path;
the round trip back works too. Claude Code does not document it, and the format
is internal: both machines should run the same version (the arrival says so).

**What may leave this machine is the user's call, and nothing leaves by
default.** A session is sent only from a folder the user allowed (an
allow-list: on a machine that also holds an employer's repositories, a deny
list forgotten once leaks one), and only when the user clicks Send.

**No concurrency to handle, by decision of 2026-10-13**: a session is never
sent while it is being changed on the other side. A newer send of a session
replaces the older one, in the store and on arrival.

**A refusal and a failure are told apart** (the remarkable-sync lesson): a
file that cannot be opened (wrong passphrase, damaged, unknown format) is
recorded as an arrival in error and never fetched again, since it would fail
the same way; a store that cannot be reached is tried again at the next pass.

    sync.json        the settings, readable by the user only: the passphrase
                     and Google's refresh token live there
    sync-state.json  what was seen, sent and received
    sync/received/   the last transcript received for each session
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from . import claude, config, folders, gdrive, registry, terminal, vault
from .config import Config

log = logging.getLogger("annotate")

FORMAT = 1
SUFFIX = ".dpsync"
POLL_EVERY = 60.0
SEEN_KEPT = 500
STORES = ("gdrive", "folder")
NAME = re.compile(r"^([0-9A-Za-z][0-9A-Za-z-]{0,63})\.(\d{10,16})\.([0-9a-f]{8,32})"
                  + re.escape(SUFFIX) + r"$")
# Claude Code shortens longer project folder names with a hash it does not
# document: such a folder is refused rather than guessed.
PROJECT_NAME_MAX = 200
SECRET_KEYS = ("passphrase", "client_secret", "refresh_token")


class SyncError(Exception):
    """The sync is not set up for this, or the request makes no sense: the
    user has something to do first. Retrying gives the same answer."""


class SyncRefused(SyncError):
    """This session may not leave the machine."""


class Store(Protocol):
    def put(self, name: str, data: bytes) -> str: ...
    def list(self) -> list[dict[str, str]]: ...
    def get(self, file_id: str) -> bytes: ...
    def delete(self, file_id: str) -> None: ...


# A store that cannot be reached: the next pass tries again.
TRANSIENT = (gdrive.DriveError, OSError)


class FolderStore:
    """A folder another program keeps in step between the machines (Google
    Drive for desktop, Syncthing, a network share). The file name is its id."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def _path(self, file_id: str) -> Path:
        if not NAME.match(file_id):
            raise SyncError(f"{file_id!r} is not a file docpin wrote")
        return self.folder / file_id

    def put(self, name: str, data: bytes) -> str:
        target = self._path(name)
        temporary = self.folder / f".{name}.{os.getpid()}.tmp"
        temporary.write_bytes(data)
        os.replace(temporary, target)
        return name

    def list(self) -> list[dict[str, str]]:
        return [{"id": p.name, "name": p.name} for p in sorted(self.folder.iterdir())
                if NAME.match(p.name)]

    def get(self, file_id: str) -> bytes:
        return self._path(file_id).read_bytes()

    def delete(self, file_id: str) -> None:
        self._path(file_id).unlink(missing_ok=True)


# -- files --------------------------------------------------------------------

def settings_path() -> Path:
    return config.data_dir() / "sync.json"


def state_path() -> Path:
    return config.data_dir() / "sync-state.json"


def received_dir() -> Path:
    return config.data_dir() / "sync" / "received"


def _write_private(path: Path, data: bytes) -> None:
    """Write `data` readable by the user only, from the first byte: a file
    created 0644 and then narrowed leaves a window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SyncError(f"cannot read {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    _write_private(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


# -- settings -----------------------------------------------------------------

def _defaults() -> dict[str, Any]:
    return {"version": 1, "enabled": False, "machine_name": socket.gethostname(),
            "machine_id": "", "store": "gdrive", "folder": "", "passphrase": "",
            "allowed": [], "client_id": "", "client_secret": "", "refresh_token": ""}


def load_settings() -> dict[str, Any]:
    raw = _read_json(settings_path())
    settings = {**_defaults(), **{k: v for k, v in raw.items() if k in _defaults()}}
    if not settings["machine_id"]:
        settings["machine_id"] = secrets.token_hex(8)
        _save(settings_path(), settings)
    return settings


def public(settings: dict[str, Any]) -> dict[str, Any]:
    """The settings the page may show: never a secret, only whether it is set."""
    shown = {k: v for k, v in settings.items() if k not in SECRET_KEYS}
    shown.update(passphrase_set=bool(settings["passphrase"]),
                 client_secret_set=bool(settings["client_secret"]),
                 google_connected=bool(settings["refresh_token"]))
    return shown


def _missing(settings: dict[str, Any]) -> list[str]:
    missing = []
    if not settings["passphrase"]:
        missing.append("a passphrase (the same on both machines)")
    if settings["store"] == "folder":
        if not settings["folder"] or not Path(settings["folder"]).is_dir():
            missing.append("an existing synced folder")
    elif not settings["refresh_token"]:
        missing.append("Google Drive connected")
    return missing


def update(changes: dict[str, Any]) -> dict[str, Any]:
    """Apply what the page sends; a field left out keeps its value, and an
    empty passphrase or client secret is no change."""
    with registry.locked():
        settings = load_settings()
        for key in ("machine_name", "client_id", "client_secret", "passphrase", "folder"):
            if key in changes:
                value = changes[key]
                if not isinstance(value, str):
                    raise SyncError(f"{key} must be text")
                value = value.strip()
                if key in ("client_secret", "passphrase") and not value:
                    continue
                if key == "passphrase" and len(value) < 8:
                    raise SyncError("the passphrase needs 8 characters at least")
                if key == "machine_name" and not 0 < len(value) <= 60:
                    raise SyncError("the machine name takes 1 to 60 characters")
                if key == "folder" and value:
                    folder = Path(value).expanduser()
                    if not folder.is_absolute() or not folder.is_dir():
                        raise SyncError(f"{value!r} is not an existing folder")
                    value = str(folder.resolve())
                if key == "client_id" and value != settings["client_id"]:
                    settings["refresh_token"] = ""      # a token belongs to its client
                settings[key] = value
        if "store" in changes:
            if changes["store"] not in STORES:
                raise SyncError(f"store must be one of {', '.join(STORES)}")
            settings["store"] = changes["store"]
        if "enabled" in changes:
            settings["enabled"] = bool(changes["enabled"])
        if settings["enabled"]:
            missing = _missing(settings)
            if missing:
                raise SyncError("cannot turn sync on yet, it needs " + " and ".join(missing))
            vault._openssl()
        _save(settings_path(), settings)
    _stores.clear()
    return settings


def allow(raw: str) -> str:
    folder = str(folders.normalise(raw))
    with registry.locked():
        settings = load_settings()
        settings["allowed"] = sorted({*settings["allowed"], folder})
        _save(settings_path(), settings)
    return folder


def disallow(raw: str) -> str:
    text = (raw or "").strip()
    with registry.locked():
        settings = load_settings()
        if text not in settings["allowed"]:
            raise SyncError(f"{text!r} is not an allowed folder")
        settings["allowed"] = [f for f in settings["allowed"] if f != text]
        _save(settings_path(), settings)
    return text


def allowed(cwd: str, settings: dict[str, Any]) -> bool:
    """True if the folder a session runs in lies in a folder the user allowed."""
    if not cwd:
        return False
    try:
        place = Path(cwd).resolve()
    except OSError:
        return False
    return any(place.is_relative_to(Path(f)) for f in settings["allowed"])


def connect_google(refresh_token: str) -> None:
    with registry.locked():
        settings = load_settings()
        settings["refresh_token"] = refresh_token
        _save(settings_path(), settings)
    _stores.clear()


# -- state --------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    state = _read_json(state_path())
    return {"seen": list(state.get("seen") or []), "arrivals": list(state.get("arrivals") or []),
            "sent": dict(state.get("sent") or {}), "last_poll": dict(state.get("last_poll") or {})}


def _save_state(state: dict[str, Any]) -> None:
    state["seen"] = state["seen"][-SEEN_KEPT:]
    _save(state_path(), {"version": 1, **state})


def _arrival(state: dict[str, Any], session: str) -> dict[str, Any]:
    for item in state["arrivals"]:
        if item["session_id"] == session:
            return item
    raise SyncError(f"nothing received for session {session}")


# -- the store ----------------------------------------------------------------

_stores: dict[str, Store] = {}


def store_for(settings: dict[str, Any]) -> Store:
    """The store the settings name. The Drive one is kept between passes:
    it holds the access token, renewed once an hour instead of every minute."""
    if settings["store"] == "folder":
        folder = Path(settings["folder"])
        if not settings["folder"] or not folder.is_dir():
            raise SyncError(f"the synced folder {settings['folder'] or '(none)'} does not exist")
        return FolderStore(folder)
    key = settings["refresh_token"]
    if key not in _stores:
        _stores[key] = gdrive.DriveStore(settings["client_id"], settings["client_secret"], key)
    return _stores[key]


def parse_name(name: str) -> tuple[str, int, str] | None:
    """(session, milliseconds, machine) of a file docpin wrote, or None."""
    found = NAME.match(name)
    return (found[1], int(found[2]), found[3]) if found else None


# -- transcripts and repositories ---------------------------------------------

def project_folder(cwd: str) -> Path:
    """Where Claude Code keeps the transcripts of a folder: its absolute path
    with every character but a letter or digit turned into `-` (measured on
    2026-10-12, and documented)."""
    name = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    if len(name) > PROJECT_NAME_MAX:
        raise SyncError(f"{cwd}: a path this long gets a folder name Claude Code does "
                        f"not document, docpin cannot place the conversation there")
    return claude.projects_dir() / name


def _latest(transcript: Path, key: str) -> str:
    """The value of `key` on the last line that carries it: a session that
    was resumed elsewhere carries the newer folder."""
    marker, value = f'"{key}":"'.encode(), ""
    with transcript.open("rb") as handle:
        for raw in handle:
            if marker not in raw:
                continue
            try:
                found = json.loads(raw).get(key)
            except (ValueError, AttributeError):
                continue
            if isinstance(found, str) and found:
                value = found
    return value


def _git(folder: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True,
                              text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def normalise_remote(url: str) -> str:
    """One spelling per repository: `git@host:a/b.git`, `ssh://git@host/a/b`
    and `https://host/a/b` name the same one."""
    text = url.strip().rstrip("/")
    text = text[:-4] if text.endswith(".git") else text
    found = re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", text)
    if found:
        return f"{found[1].lower()}/{found[2]}"
    found = re.match(r"^(?:[^@/]+@)?([^/:]+):(?!/)(.+)$", text)
    if found:
        return f"{found[1].lower()}/{found[2]}"
    return str(Path(text).expanduser().resolve()) if text.startswith(("/", "~")) else text


def repository_facts(cwd: str) -> dict[str, Any]:
    """Where the session runs, in terms the other machine can find: the
    repository's remote, the folder inside it, its branch and commit, and
    what git would not carry (uncommitted changes, unpushed commits)."""
    root, is_repo = registry.repository_of(Path(cwd))
    if not is_repo:
        return {}
    upstream = _git(root, "rev-list", "--count", "@{upstream}..HEAD")
    return {"remote": _git(root, "remote", "get-url", "origin") or "",
            "relative": str(Path(cwd).relative_to(root)) if Path(cwd) != root else "",
            "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD") or "",
            "head": _git(root, "rev-parse", "HEAD") or "",
            "dirty": bool(_git(root, "status", "--porcelain")),
            "unpushed": int(upstream) if upstream and upstream.isdigit() else None}


def send_warnings(facts: dict[str, Any]) -> list[str]:
    if not facts:
        return ["Not in a git repository: the other machine will ask where it goes."]
    warnings = []
    if not facts["remote"]:
        warnings.append("The repository has no remote `origin`: the other machine "
                        "will ask where its clone is.")
    if facts["dirty"]:
        warnings.append("Uncommitted changes stay on this machine: commit and push them.")
    if facts["unpushed"]:
        warnings.append(f"{facts['unpushed']} commit(s) not pushed: push them.")
    elif facts["unpushed"] is None and facts["remote"]:
        warnings.append(f"The branch {facts['branch']} tracks no remote branch: push it.")
    return warnings


def _is_clone_of(root: Path, wanted: str) -> bool:
    url = _git(root, "remote", "get-url", "origin")
    return bool(url) and normalise_remote(url or "") == wanted


def local_repository(remote: str, received: set[str] = frozenset(),  # type: ignore[assignment]
                     before: str = "") -> Path | None:
    """The clone of `remote` on this machine: where the session was placed
    here last time (`before`), else among the repositories where a
    conversation ran here, then those of the workspace (three levels deep).

    A transcript that came from the other machine starts in ITS folder, which
    may exist here too, as another clone or as nothing related: the sessions
    in `received` are no evidence of where a repository lives here."""
    wanted = normalise_remote(remote)
    if before and Path(before).is_dir():
        root, is_repo = registry.repository_of(Path(before))
        if is_repo and _is_clone_of(root, wanted):
            return root
    candidates: list[Path] = []
    for transcript in claude.projects_dir().glob("*/*.jsonl"):
        if transcript.stem in received:
            continue
        cwd = claude.cwd_of(transcript)
        if cwd and Path(cwd).is_dir():
            root, is_repo = registry.repository_of(Path(cwd))
            if is_repo and root not in candidates:
                candidates.append(root)
    workspace = config.workspace()
    if workspace.is_dir():
        for depth in ("*", "*/*", "*/*/*"):
            for marker in workspace.glob(f"{depth}/.git"):
                if marker.parent not in candidates:
                    candidates.append(marker.parent)
    for root in candidates:
        if _is_clone_of(root, wanted):
            return root
    return None


# -- send ---------------------------------------------------------------------

_lock = threading.Lock()


def _require(settings: dict[str, Any]) -> None:
    if not settings["enabled"]:
        raise SyncError("sync is off: turn it on in the Sync panel of the home page")
    missing = _missing(settings)
    if missing:
        raise SyncError("sync needs " + " and ".join(missing))


def send(session: str, store: Store | None = None) -> dict[str, Any]:
    """Seal the conversation and put it in the store; remove its older copies."""
    with _lock:
        settings = load_settings()
        _require(settings)
        transcript = claude.transcript_of(session)
        if transcript is None:
            raise SyncError(f"no conversation {session} on this machine")
        cwd = _latest(transcript, "cwd") or claude.cwd_of(transcript)
        if not allowed(cwd, settings):
            raise SyncRefused(f"{cwd or 'its folder'} is not in a folder allowed to leave "
                              f"this machine: add one under Sync, if it may")
        facts = repository_facts(cwd)
        warnings = send_warnings(facts)
        title = claude.title(session)
        header = {"format": FORMAT, "session_id": session, "title": title,
                  "machine_id": settings["machine_id"], "machine_name": settings["machine_name"],
                  "sent_at": registry.now_iso(), "cwd": cwd,
                  "claude_version": _latest(transcript, "version"), "repository": facts,
                  "warnings": warnings}
        sealed = vault.seal(json.dumps(header, ensure_ascii=False).encode() + b"\n"
                            + transcript.read_bytes(), settings["passphrase"])
        target = store or store_for(settings)
        name = f"{session}.{time.time_ns() // 1_000_000}.{settings['machine_id']}{SUFFIX}"
        target.put(name, sealed)
        for item in target.list():
            parsed = parse_name(item["name"])
            if parsed and parsed[0] == session and item["name"] != name:
                target.delete(item["id"])
        with registry.locked():
            state = load_state()
            state["seen"].append(name)
            state["sent"][session] = {"at": header["sent_at"], "title": title, "size": len(sealed)}
            _save_state(state)
    log.info("session %s sent (%d bytes sealed)", session, len(sealed))
    label = title or f"session {session[:8]}"
    message = f"{label} sent: the other machine takes it within a minute."
    return {"session_id": session, "title": title, "size": len(sealed),
            "warnings": warnings, "message": " ".join([message, *warnings])}


# -- receive ------------------------------------------------------------------

def _place(session: str, cwd: Path) -> None:
    source = received_dir() / f"{session}.jsonl"
    _write_private(project_folder(str(cwd)) / f"{session}.jsonl", source.read_bytes())


def _local_warnings(item: dict[str, Any], root: Path | None) -> list[str]:
    warnings = list(item.get("sent_warnings") or [])
    head, branch = item.get("head") or "", item.get("branch") or ""
    if root is not None and head:
        here = _git(root, "rev-parse", "HEAD") or ""
        if here != head:
            warnings.append(f"This clone is at {here[:7] or 'no commit'}, the session was sent "
                            f"at {head[:7]}" + (f" on {branch}" if branch else "")
                            + ": pull before resuming.")
    version = item.get("claude_version") or ""
    if version and version != claude_version():
        warnings.append(f"Sent from Claude Code {version}, this machine runs "
                        f"{claude_version() or 'an unknown version'}: the transcript format "
                        f"is internal, update one of them if the resume fails.")
    return warnings


_version: list[str] = []


def claude_version() -> str:
    """The version of the newest transcript here: what this machine's Claude
    Code writes (no process is started to ask it)."""
    if not _version:
        newest = sorted(claude.projects_dir().glob("*/*.jsonl"),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:1]
        _version.append(_latest(newest[0], "version") if newest else "")
    return _version[0]


def _receive(blob: bytes, name: str, settings: dict[str, Any],
             state: dict[str, Any]) -> dict[str, Any]:
    plain = vault.unseal(blob, settings["passphrase"])
    raw, _, transcript = plain.partition(b"\n")
    try:
        header = json.loads(raw)
    except ValueError as exc:
        raise SyncError(f"{name}: unreadable header") from exc
    parsed = parse_name(name)
    session = parsed[0] if parsed else ""
    if not isinstance(header, dict) or header.get("format") != FORMAT or \
            header.get("session_id") != session or not transcript:
        raise SyncError(f"{name}: not a session docpin {FORMAT} sent")
    _write_private(received_dir() / f"{session}.jsonl", transcript)
    facts = header.get("repository") or {}
    item: dict[str, Any] = {
        "session_id": session, "title": str(header.get("title") or ""),
        "from": str(header.get("machine_name") or ""), "sent_at": header.get("sent_at"),
        "received_at": registry.now_iso(), "remote": facts.get("remote") or "",
        "relative": facts.get("relative") or "", "branch": facts.get("branch") or "",
        "head": facts.get("head") or "", "claude_version": header.get("claude_version") or "",
        "sent_cwd": header.get("cwd") or "",
        "sent_warnings": [str(w) for w in header.get("warnings") or []],
        "status": "needs-folder", "cwd": "", "error": ""}
    received = {a["session_id"] for a in state["arrivals"]}
    before = next((a.get("cwd") or "" for a in state["arrivals"]
                   if a["session_id"] == session), "")
    root = local_repository(item["remote"], received, before) if item["remote"] else None
    if root is not None:
        inside = root / item["relative"]
        cwd = Path(before) if before and Path(before).is_relative_to(root) else \
            inside if inside.is_dir() else root
        _place(session, cwd)
        item.update(status="ready", cwd=str(cwd))
    item["warnings"] = _local_warnings(item, root)
    return item


def _keep(state: dict[str, Any], item: dict[str, Any]) -> None:
    state["arrivals"] = [a for a in state["arrivals"]
                         if a["session_id"] != item["session_id"]] + [item]


def poll_once(store: Store | None = None) -> dict[str, Any]:
    """One pass: fetch what another machine sent since the last one."""
    with _lock:
        settings = load_settings()
        if not settings["enabled"]:
            return {"skipped": True}
        received: list[str] = []
        error = ""
        target = store
        try:
            target = target or store_for(settings)
            files = target.list()
        except (*TRANSIENT, SyncError) as exc:
            files, error = [], str(exc)
        state = load_state()
        seen = set(state["seen"])
        newest: dict[str, tuple[int, dict[str, str]]] = {}
        for item in files:
            parsed = parse_name(item["name"])
            if not parsed or parsed[2] == settings["machine_id"] or item["name"] in seen:
                continue
            if parsed[0] not in newest or parsed[1] > newest[parsed[0]][0]:
                newest[parsed[0]] = (parsed[1], item)
        for session, (_, item) in newest.items():
            assert target is not None
            try:
                blob = target.get(item["id"])
            except TRANSIENT as exc:
                error = str(exc)
                continue
            try:
                arrival = _receive(blob, item["name"], settings, state)
            except (vault.VaultError, SyncError, OSError) as exc:
                arrival = {"session_id": session, "title": "", "from": "",
                           "received_at": registry.now_iso(), "status": "error",
                           "error": str(exc), "cwd": "", "warnings": []}
                log.error("sync: %s cannot be opened: %s", item["name"], exc)
            else:
                log.info("sync: session %s received from %s (%s)", session,
                         arrival["from"], arrival["status"])
            _keep(state, arrival)
            received.append(session)
            # Older copies of a session that arrives are superseded.
            state["seen"] += [i["name"] for i in files
                              if (parse_name(i["name"]) or ("",))[0] == session]
        state["last_poll"] = {"at": registry.now_iso(), "error": error,
                              "received": len(received)}
        with registry.locked():
            _save_state(state)
        return {"received": received, "error": error}


def place(session: str, raw_folder: str) -> dict[str, Any]:
    """Put a received conversation in the folder the user chose."""
    folder = folders.normalise(raw_folder)
    with _lock, registry.locked():
        state = load_state()
        item = _arrival(state, session)
        if item["status"] == "error":
            raise SyncError("this arrival could not be opened: nothing to place")
        _place(session, folder)
        root, is_repo = registry.repository_of(folder)
        item.update(status="ready", cwd=str(folder))
        item["warnings"] = _local_warnings(item, root if is_repo else None)
        _save_state(state)
    return item


def dismiss(session: str) -> None:
    with _lock, registry.locked():
        state = load_state()
        _arrival(state, session)
        state["arrivals"] = [a for a in state["arrivals"] if a["session_id"] != session]
        _save_state(state)


def resume_prompt(item: dict[str, Any], here: str) -> str:
    """The first message of the resumed session: where it is now. Its earlier
    messages name the other machine's paths."""
    lines = [f"This conversation was moved from the machine {item['from'] or '(unknown)'} "
             f"to this one ({here}). The project is now at {item['cwd']}"
             + (f", it was at {item['sent_cwd']}" if item.get("sent_cwd") else "")
             + ": paths named earlier in the conversation may differ here."]
    lines += item.get("warnings") or []
    lines.append("Check `git status`, then wait for my next message.")
    return "\n".join(lines)


def resume(session: str, cfg: Config,
           opener: Callable[..., str] = terminal.open_session) -> dict[str, Any]:
    """Open a terminal tab that resumes the received conversation here."""
    state = load_state()
    item = _arrival(state, session)
    if item["status"] != "ready":
        raise SyncError("choose the folder it goes in first" if item["status"] == "needs-folder"
                        else "this arrival could not be opened")
    if not claude.resumable(session):
        raise SyncError("the conversation is not where Claude Code looks for it any more: "
                        "choose its folder again")
    program = opener(f"sync-{session[:8]}", item["cwd"], cfg.claude_bin, session,
                     resume_prompt(item, load_settings()["machine_name"]))
    log.info("sync: session %s resumed in a %s", session, program)
    return {"session_id": session, "message": f"{item['title'] or 'The session'} resumed "
                                              f"in a terminal tab, in {item['cwd']}."}


def overview() -> dict[str, Any]:
    """What the page shows."""
    settings = load_settings()
    state = load_state()
    return {"settings": public(settings), "arrivals": state["arrivals"], "sent": state["sent"],
            "last_poll": state["last_poll"], "poll_every": POLL_EVERY}


# -- Google's consent page, and back ------------------------------------------

class GoogleLogins:
    """The connections under way: the `state` Google sends back names one,
    once, for ten minutes. The callback is a plain GET from Google's page,
    without the `X-Annotate` header: this state is what proves the browser
    came from a consent the user started here."""

    LIFETIME = 600.0

    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()

    def start(self, redirect_uri: str) -> str:
        settings = load_settings()
        if not settings["client_id"] or not settings["client_secret"]:
            raise SyncError("enter the OAuth client id and secret of your Google Cloud "
                            "project first")
        url, state, verifier = gdrive.authorization(settings["client_id"], redirect_uri)
        with self._lock:
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
            self._pending[state] = (verifier, redirect_uri, now + self.LIFETIME)
        return url

    def finish(self, state: str, code: str) -> None:
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or pending[2] < time.time():
            raise SyncError("this Google answer matches no connection started here "
                            "(or it is older than ten minutes): connect again")
        settings = load_settings()
        tokens = gdrive.exchange(settings["client_id"], settings["client_secret"], code,
                                 pending[0], pending[1])
        connect_google(str(tokens["refresh_token"]))
        log.info("sync: Google Drive connected")


def run(stop: threading.Event, every: float = POLL_EVERY) -> None:
    """The daemon's thread: a pass, then a wait that `stop` interrupts. A
    failing pass is logged, never fatal."""
    while not stop.is_set():
        try:
            poll_once()
        except Exception as exc:  # noqa: BLE001 - the daemon must outlive a bad pass
            log.error("sync pass failed: %s", exc)
        stop.wait(every)
