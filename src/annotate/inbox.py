"""Post the notes into a Claude Code session that is OPEN but does not listen.

Before this, a send to such a session opened a terminal tab that resumed the
same conversation a second time (2026-10-06: "ça m'a ouvert un onglet avec ma
session en double ; l'ancienne n'a pas vu le truc"). A document found by the
daemon's scan is the usual case: its session never got the hook's
`annotate wait` instruction.

Claude Code gives each session an inbox, a Unix socket, for cross-session
messages, and documents that "a script or hook" may post into it
(https://code.claude.com/docs/en/cross-session-messaging, "The session's
inbox socket"). A message wakes an idle session, which starts a turn with it.

- **Which sessions are open**: `~/.claude/sessions/<pid>.json`, the registry
  `claude agents --json` reads (`sessionId`, `name`, `messagingSocketPath`).
  A file outlives its process, and a pid is reused: an entry counts only if
  `/proc/<pid>/stat` still carries its `procStart` (field 22, the start time
  in ticks). Never a signal to find out.
- **The line format is NOT documented**, only the auth line is. It was
  captured on 2026-10-06 (Claude Code 2.1.289) from a real `SendMessage` into
  a probe socket, and `tests/test_inbox.py` fixes it. If an update changes it,
  two things can happen, and only the first one shows: the socket REFUSES
  the post (the notes stay pending, never a tab), or it ACCEPTS the bytes and
  Claude Code ignores them. The second is invisible from here: a socket that
  took the line is all the daemon can know, so the notes count as delivered
  and nothing reaches the session (reproduced 2026-10-12 with a socket that
  reads and drops; `test_an_inbox_that_takes_the_line_and_ignores_it_counts_as_delivered`).
  Two mechanisms close it (decision T2, 2026-10-09). **The daemon posts only
  to a session whose `peerProtocol` is one the format was checked under**
  (`VERIFIED_PROTOCOLS`): a real `SendMessage` was captured into a witness
  socket under 2.1.289 (2026-10-06) and again under 2.1.296 (2026-10-12),
  same line, both protocol 1. The registry field, not the version, is the
  gate: the version changed seven times in those six days, the protocol never
  (2.1.121 to 2.1.296, every entry on the author's machine). And **"Resend"**
  puts the last batch back to send (`registry.unmark_sent`) when it never
  arrived.
- **No `from-mode`**: that attribute is how a sender claims its permission
  class. The daemon claims none, so a session that bypasses permission
  prompts HOLDS the message behind an Approve / Deny dialog. That is the
  user's decision of 2026-10-06 ("dialogue à chaque envoi"): nothing reaches
  a session without being seen.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from html import escape
from pathlib import Path
from typing import Any

SENDER_NAME = "annotate"
CONNECT_TIMEOUT = 5.0
# The `peerProtocol` values of ~/.claude/sessions/<pid>.json under which the
# line format of `line()` was captured from a real Claude Code (see above).
VERIFIED_PROTOCOLS = frozenset({1})


class InboxError(Exception):
    """The message could not be posted."""


def sessions_dir() -> Path:
    return Path.home() / ".claude" / "sessions"


def _proc_start(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Field 2 (the command) may hold spaces and parentheses: cut after the
    # LAST ')' (remarkable-sync, etat.identifier). Field 22 is index 19 there.
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def _live_entries() -> list[dict[str, Any]]:
    """Every entry of the sessions registry whose process is alive and has an
    inbox: the one place that decides what "open" means."""
    found = []
    for path in sessions_dir().glob("*.json"):
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("sessionId"), str):
            continue
        pid, socket_path = entry.get("pid"), entry.get("messagingSocketPath")
        if not isinstance(pid, int) or pid <= 1 or not isinstance(socket_path, str):
            continue
        if _proc_start(pid) != str(entry.get("procStart")):
            continue                          # gone, or a reused pid
        if not Path(socket_path).exists():
            continue
        found.append(entry)
    return found


def open_sessions(session_id: str) -> list[dict[str, Any]]:
    """The live processes of `session_id` that have an inbox, most recently
    active first (a conversation can be open twice: the duplicate tab)."""
    found = [e for e in _live_entries() if e["sessionId"] == session_id]
    return sorted(found, key=lambda e: e.get("updatedAt") or 0, reverse=True)


def open_session_ids() -> set[str]:
    """The sessions that are open right now, by the same test as
    `open_sessions`: open on the index page means a send would post into it."""
    return {e["sessionId"] for e in _live_entries()}


def verified(entry: dict[str, Any]) -> bool:
    """True if docpin knows how this session's inbox reads a message."""
    protocol = entry.get("peerProtocol")
    return isinstance(protocol, int) and protocol in VERIFIED_PROTOCOLS


def line(text: str) -> bytes:
    """One message, in the format Claude Code's own `SendMessage` writes."""
    content = (f'<cross-session-message from="{escape(SENDER_NAME)}" '
               f'from-name="{escape(SENDER_NAME)}">\n{text}\n</cross-session-message>')
    message = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user",
               "message": {"role": "user", "content": content},
               "priority": "next", "from": SENDER_NAME}
    return (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")


def post(socket_path: str, text: str) -> None:
    """Write one message to an inbox socket; raise InboxError if it fails."""
    if not os.path.isabs(socket_path):
        raise InboxError(f"not an inbox socket: {socket_path!r}")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(socket_path)
            sock.sendall(line(text))
    except OSError as exc:
        raise InboxError(f"cannot post to {socket_path}: {exc}") from exc
