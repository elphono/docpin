"""Cleanup files: unmanage the documents without a known session, and delete
their files a month later if nobody touched them.

The group "Documents without a known session" fills with what nobody
claims: copies a subagent made, reports of a session long gone. "Cleanup
files" (decision of 2026-10-19), on the page or `annotate cleanup`,
unmanages in one batch every document of that group that a batch may take
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
          now: float | None = None) -> dict[str, list[dict[str, Any]]]:
    """Unmanage the documents without a known session that a batch may take,
    all of them or those of `ids` (what the page showed when the user
    confirmed); each file enters the deletion index. Nothing is deleted."""
    if ids is not None and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
        raise CleanupError(f"ids must be a list of document ids, got {ids!r}")
    chosen = None if ids is None else set(ids)
    delay = after_days()

    def unclaimed(doc: dict[str, Any]) -> bool:
        return not doc.get("session_id") and (chosen is None or doc["id"] in chosen)

    result = registry.forget_batch(unclaimed, listening, cleanup=True, now=now)
    for item in result["forgotten"]:
        log.info("cleanup: %s unmanaged; its file is deleted in %g day(s) unless touched",
                 item["path"], delay)
    return result


def message(gone: int, kept: int, now: float | None = None) -> str:
    """What the page and the command line say after a batch."""
    moment = time.time() if now is None else now
    text = f"{gone} document(s) without a known session unmanaged"
    if gone:
        text += (f"; their files are deleted on {day_of(moment + after_days() * DAY)} "
                 f"unless written again before")
    if kept:
        text += f"; {kept} kept, protected (a session on it, notes to send, or its file missing)"
    return text + "."


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
