"""Cleanup files: unmanage the documents of one group of the index page, and
delete their files a month later if nobody touched them.

The group "Documents without a known session" fills with what nobody
claims: copies a subagent made, reports of a session long gone; a session's
group, with what the session no longer needs. "Cleanup files" (decision of
2026-10-19, in every session group since 2026-10-20), on the page or
`annotate cleanup [--session ID]`, unmanages in one batch every document of
that group, and of that group only, that a batch may take
(registry.protection), and puts each file in a deletion index
(`cleanup.json`, read and written by registry.py under its lock):

    {"version": 1, "files": {path: {"cleaned_at": epoch, "mtime": epoch, "size": bytes}}}

A file still untouched `CLEANUP_AFTER_DAYS` days after its cleanup is deleted
from the disk by the daemon, which looks at most once an hour (`Sweeper`, in
the loop of scanner.run), or by `annotate cleanup --run`.

| Invariant                                   | Held by                          |
|---------------------------------------------|----------------------------------|
| the batch deletes nothing at once           | `forget_batch` only unmanages    |
| never a protected document in the batch     | registry.protection              |
| a missing file always goes, and never enters the index (nothing to delete) | registry.protection, `forget_batch` |
| only a file of the index is ever deleted    | `sweep_cleaned` walks the index, nothing else |
| touched means kept: date or size changed, or registered again (hook, scan, rescan, `register`) | `sweep_cleaned`, registry.register drops the entry |
| never before the delay                      | `sweep_cleaned`, `now` injected in the tests |

The delay is counted on the wall clock, as file dates are; the hourly
cadence on the monotonic one, since it is a rhythm of the daemon and not a
date. `ANNOTATE_CLEANUP_AFTER_DAYS` overrides the delay (the tests); no
other place holds it, the page reads it from /api/docs.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import registry

log = logging.getLogger("annotate")

CLEANUP_AFTER_DAYS = 30.0
ENV = "ANNOTATE_CLEANUP_AFTER_DAYS"
MAX_DAYS = 3650.0
DAY = 86400.0
SWEEP_EVERY = 3600.0


class CleanupError(Exception):
    """The delay before deletion, or a cleanup request, is invalid."""


def after_days() -> float:
    """How long a cleaned file waits before deletion: `CLEANUP_AFTER_DAYS`,
    unless the environment says otherwise."""
    raw = os.environ.get(ENV, "").strip()
    if not raw:
        return CLEANUP_AFTER_DAYS
    try:
        days = float(raw)
    except ValueError as exc:
        raise CleanupError(f"{ENV} must be a number of days, got {raw!r}") from exc
    if not 0 < days <= MAX_DAYS:
        raise CleanupError(f"{ENV} must be between 0 and {MAX_DAYS:g} days, got {days:g}")
    return days


def day_of(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def batch(listening: Callable[[str], bool], ids: object = None,
          now: float | None = None, session: object = "") -> dict[str, list[dict[str, Any]]]:
    """Unmanage the documents of one group that a batch may take: those of
    the session `session` ("" for the documents without a known session),
    all of them or those of `ids` (what the page showed when the user
    confirmed). A document of another group is never taken, whatever `ids`
    says. Each file enters the deletion index; nothing is deleted."""
    if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
        raise CleanupError(f"ids must be a list of document ids, got {ids!r}")
    if not isinstance(session, str):
        raise CleanupError(f"session must be a session id ('' for none), got {session!r}")
    chosen = None if ids is None else set(ids)
    delay = after_days()

    def in_group(doc: dict[str, Any]) -> bool:
        return doc.get("session_id", "") == session and (chosen is None or doc["id"] in chosen)

    result = registry.forget_batch(in_group, listening, cleanup=True, now=now)
    for item in result["forgotten"]:
        log.info("cleanup: %s unmanaged; its file is deleted in %g day(s) unless touched",
                 item["path"], delay)
    return result


def message(gone: int, kept: int, now: float | None = None, session: str = "",
            missing: int = 0) -> str:
    """What the page and the command line say after a batch; `missing` of the
    `gone` documents had no file any more, so nothing waits for deletion."""
    moment = time.time() if now is None else now
    group = f"of the session {session[:8]}" if session else "without a known session"
    text = f"{gone} document(s) {group} unmanaged"
    if gone > missing:
        text += (f"; their files are deleted on {day_of(moment + after_days() * DAY)} "
                 f"unless written again before")
    if missing:
        text += f"; {missing} had no file any more"
    if kept:
        text += f"; {kept} kept, protected (a session listens or is on it, or notes wait to be sent)"
    return text + "."


def missing(result: dict[str, list[dict[str, Any]]]) -> int:
    """How many documents a batch took had no file any more."""
    return sum(1 for item in result["forgotten"] if not item.get("exists"))


def listing() -> list[dict[str, Any]]:
    """The deletion index, soonest deletion first, with the date it is due."""
    delay = after_days() * DAY
    items = [{"path": path, **record, "due_at": record["cleaned_at"] + delay}
             for path, record in registry.cleanup_index().items()]
    return sorted(items, key=lambda item: (item["due_at"], item["path"]))


def sweep(now: float | None = None) -> dict[str, list[str]]:
    """Delete what is due now (registry.sweep_cleaned), and say so in the log."""
    moment = time.time() if now is None else now
    delay = after_days()
    result = registry.sweep_cleaned(moment, delay * DAY)
    for path in result["deleted"]:
        log.info("cleanup: %s deleted, untouched %g day(s) after its cleanup", path, delay)
    for path in result["released"]:
        log.info("cleanup: %s kept, touched or listed again since its cleanup (or gone)", path)
    return result


class Sweeper:
    """At most one sweep per `every` seconds: the daemon's scan loop ticks it
    every pass (30 s), and only the first tick of each hour sweeps."""

    def __init__(self, every: float = SWEEP_EVERY,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.every = every
        self.clock = clock
        self.next_at: float | None = None

    def tick(self, now: float | None = None) -> dict[str, list[str]] | None:
        moment = self.clock()
        if self.next_at is not None and moment < self.next_at:
            return None
        self.next_at = moment + self.every
        return sweep(now)
