"""The daemon's own pass over the tracked folders: what the hook cannot see.

The hook (hooks/register-on-write.py) fires on `Write` and `Edit` only. A
document a session produces with Bash (a script, a `cp` from its scratchpad,
the usual way of subagents) never reaches it: measured 2026-10-05,
`<repo>/docs/<name>.html` was composed by a subagent
in its scratchpad and copied into `docs/` at 16:08, an hour after the hook
existed, and never appeared in the tray.

Every `SCAN_EVERY` seconds the daemon walks the tracked folders (folders.py;
0.3 s for 10 577 folders of the workspace, measured) and registers the
documents written SINCE ITS PREVIOUS PASS; the session that wrote each one is
looked up in the transcripts (claude.writer_of). The hook stays: it knows the
session for certain, and it is what tells that session to run `annotate wait`.

**The regular pass takes nothing older than its first pass** (decision of
2026-10-05: "on rattrape pas l'existant"). The start of each pass is kept on
disk (`scan.json`): a file written while the daemon was down is found at the
next start.

**The catch-up is the user's, and bounded** (decision of 2026-10-07): the
"Rescan" button, and adding a folder, take what was written in the last
`days` days, minus what the user unmanaged and nobody wrote since
(registry.dismissed). A full catch-up would have brought back 100 documents
of the workspace at once, most of them weeks old. It runs in a thread of its
own: finding the writers of a hundred files reads the transcripts a hundred
times (0.2 s each, measured).

**"Rescan" also clears** (decision of 2026-10-19): the list then holds the
documents of the window, and those older are unmanaged (`prune`), as an
Unmanage does, never one that registry.protection keeps. Adding a folder
only catches up.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import claude, cleanup, config, folders, registry

log = logging.getLogger("annotate")

SCAN_EVERY = 30.0
DEFAULT_DAYS = 7
MAX_DAYS = 3650
DAY = 86400.0

# One pass at a time: the regular pass and a catch-up would otherwise look up
# the same writer twice and log the same registration twice.
_PASS = threading.Lock()


class ScanError(Exception):
    """The scan state cannot be read or written."""


def state_path() -> Path:
    return config.data_dir() / "scan.json"


def read_since() -> float | None:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ScanError(f"cannot read {state_path()}: {exc}") from exc
    since = data.get("since") if isinstance(data, dict) else None
    return float(since) if isinstance(since, (int, float)) else None


def write_since(since: float) -> None:
    try:
        registry.atomic_write(state_path(), json.dumps({"since": since}) + "\n")
    except OSError as exc:
        raise ScanError(f"cannot write {state_path()}: {exc}") from exc


def candidates(items: list[dict[str, Any]], since: float) -> list[tuple[Path, float]]:
    """Every document of the tracked folders `items` written after `since`.
    An excluded folder is not entered: what is a document is still
    `folders.wanted`'s answer, the pruning only spares the walk."""
    found: dict[Path, float] = {}
    excluded = folders.load_excluded()
    for root in folders.roots(items):
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in folders.SKIPPED
                       and not folders.excluded_by(Path(folder) / d, excluded)]
            for name in files:
                if not name.lower().endswith(".html"):
                    continue
                path = Path(folder) / name
                if path in found or not folders.wanted(path, items, excluded):
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime > since:
                    found[path] = mtime
    return list(found.items())


def _register_new(found: list[tuple[Path, float]], reason: str) -> list[str]:
    known = {entry["path"] for entry in registry.all_docs().values()}
    added = []
    for path, mtime in sorted(found, key=lambda c: c[1]):
        if str(path.resolve()) in known or registry.dismissed(path.resolve(), mtime):
            continue
        writer = claude.writer_of(path, mtime)
        session, cwd = writer if writer else ("", "")
        try:
            entry = registry.register(path, session=session,
                                      cwd=Path(cwd) if cwd and Path(cwd).is_dir() else None)
        except registry.RegistryError as exc:
            log.warning("%s: %s not registered: %s", reason, path, exc)
            continue
        added.append(entry["id"])
        log.info("%s: %s registered as %s (session %s)", reason, path, entry["id"],
                 session or "unknown")
    return added


def scan_once(now: float | None = None) -> list[str]:
    """One regular pass; return the ids it registered. The first pass
    registers nothing: it only sets the starting point."""
    start = time.time() if now is None else now
    with _PASS:
        since = read_since()
        if since is None:
            write_since(start)
            log.info("scan: first pass, documents written from now on will be listed")
            return []
        added = _register_new(candidates(folders.load(), since), "scan")
        write_since(start)
        return added


def catch_up(days: float, only: str | None = None, now: float | None = None) -> list[str]:
    """Register the documents written in the last `days` days, in every
    tracked folder or in the folder `only`."""
    start = time.time() if now is None else now
    items = folders.load()
    if only is not None:
        items = [i for i in items if i["path"] == only]
        if not items:
            raise ScanError(f"{only} is not a tracked folder")
    with _PASS:
        return _register_new(candidates(items, start - days * DAY), "rescan")


def prune(days: float, only: str | None = None, now: float | None = None, *,
          listening: Callable[[str], bool]) -> dict[str, list[dict[str, Any]]]:
    """Unmanage the listed documents whose file is older than the window of
    the last `days` days, all of them or those under the folder `only`; never
    a protected one (registry.protection, `listening` naming the documents a
    session waits for). The window's edge splits the files exactly as
    `catch_up` does: newer than `since` is taken, the rest may go."""
    start = time.time() if now is None else now
    since = start - days * DAY
    folder = Path(only) if only is not None else None

    def older(doc: dict[str, Any]) -> bool:
        if folder is not None and not Path(doc["path"]).is_relative_to(folder):
            return False
        return doc["mtime"] is None or doc["mtime"] <= since

    result = registry.forget_batch(older, listening, cleanup=False)
    for item in result["forgotten"]:
        log.info("rescan: %s unmanaged, written more than %g day(s) ago", item["path"], days)
    return result


def rescan(days: float, only: str | None = None, now: float | None = None, *,
           listening: Callable[[str], bool]) -> dict[str, list[Any]]:
    """"Rescan" (decision of 2026-10-19): the list becomes the documents of
    the last `days` days. What was written in the window is registered
    (`catch_up`), what is older is unmanaged (`prune`), and comes back only
    when written again (registry.dismissed). Adding a folder only catches up:
    it never unmanages."""
    added = catch_up(days, only, now)
    pruned = prune(days, only, now, listening=listening)
    return {"added": added, "removed": [d["id"] for d in pruned["forgotten"]],
            "kept": pruned["kept"]}


def check_days(raw: object) -> float:
    """The window of a catch-up, from a request: a number of days, 7 if absent."""
    if raw is None or raw == "":
        return float(DEFAULT_DAYS)
    if isinstance(raw, bool):
        raise ScanError(f"days must be a number, got {raw!r}")
    try:
        days = float(str(raw))
    except ValueError as exc:
        raise ScanError(f"days must be a number, got {raw!r}") from exc
    if not 0 < days <= MAX_DAYS:
        raise ScanError(f"days must be between 0 and {MAX_DAYS}, got {days:g}")
    return days


class CatchUps:
    """The catch-ups the index page starts, one at a time, in a thread; what
    the last one did is kept for the page to show. `listening` names the
    documents a session waits for (the daemon's board): a rescan never
    unmanages them."""

    def __init__(self, listening: Callable[[str], bool]) -> None:
        self._lock = threading.Lock()
        self.listening = listening
        self.running: dict[str, Any] | None = None
        self.last: dict[str, Any] | None = None

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.running, "last": self.last}

    def start(self, days: float, only: str | None = None, prune: bool = False) -> bool:
        """False if a catch-up already runs: the user clicked twice. `prune`
        makes it a rescan, which also unmanages what is older than the
        window; adding a folder never does."""
        with self._lock:
            if self.running is not None:
                return False
            self.running = {"days": days, "folder": only, "prune": prune,
                            "started_at": registry.now_iso()}
        threading.Thread(target=self._run, args=(days, only, prune), name="rescan",
                         daemon=True).start()
        return True

    def _run(self, days: float, only: str | None, prune: bool) -> None:
        error, added, removed, kept = "", [], [], []
        try:
            if prune:
                result = rescan(days, only, listening=self.listening)
                added, removed, kept = result["added"], result["removed"], result["kept"]
            else:
                added = catch_up(days, only)
        except Exception as exc:  # noqa: BLE001 - reported to the page, never fatal
            log.error("rescan failed: %s", exc)
            error = str(exc)
        with self._lock:
            self.last = {**(self.running or {}), "ended_at": registry.now_iso(),
                         "added": len(added), "removed": len(removed), "kept": len(kept),
                         "error": error}
            self.running = None


def run(stop: threading.Event, every: float = SCAN_EVERY,
        sweeper: cleanup.Sweeper | None = None) -> None:
    """The daemon's thread: a pass, the deletions "Cleanup files" made due
    (at most once an hour: cleanup.Sweeper), then a wait that `stop`
    interrupts. A failing pass or sweep is logged and the next one is tried:
    neither may take the daemon down."""
    sweeper = cleanup.Sweeper() if sweeper is None else sweeper
    while not stop.is_set():
        try:
            scan_once()
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.error("scan failed: %s", exc)
        try:
            sweeper.tick()
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.error("cleanup failed: %s", exc)
        stop.wait(every)
