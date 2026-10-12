"""The CLI's way of talking to the running daemon.

`send` and `new-session` go THROUGH the daemon rather than launching a session
from the CLI process: the daemon is the only actor that runs sessions, so its
"one session per document" rule holds whoever clicks. (remarkable-sync paid
for a second actor once: its "cycle" button launched a pull next to the
daemon, two sessions on the same ink.)
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .server import CLIENT_HEADER


class ClientError(Exception):
    """The daemon answered with an error."""


class DaemonUnreachable(ClientError):
    """Nothing answers on the configured port."""


def call(port: int, method: str, path: str, body: object = None,
         timeout: float = 10) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header(CLIENT_HEADER, "cli")
    request.add_header("Host", f"localhost:{port}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error", "")
        except (ValueError, AttributeError):
            message = ""
        raise ClientError(f"{method} {path}: HTTP {exc.code} {message}".strip()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DaemonUnreachable(
            f"no daemon on 127.0.0.1:{port} ({exc}); start it with "
            f"`systemctl --user start annotate` or `annotate serve`") from exc
    try:
        result = json.loads(payload) if payload else {}
    except json.JSONDecodeError as exc:
        raise ClientError(f"{method} {path}: unreadable answer") from exc
    return result if isinstance(result, dict) else {"value": result}


START_WAIT = 8.0


def ensure_daemon(port: int, start: Callable[[], str], wait: float = START_WAIT) -> str:
    """The plugin's on-demand start: `"running"` if a daemon answers, else
    start one with `start` and wait until it answers. Raise DaemonUnreachable
    if it never does: the caller says so instead of waiting in silence."""
    try:
        call(port, "GET", "/api/docs", timeout=2)
        return "running"
    except DaemonUnreachable:
        pass
    how = start()
    deadline = time.monotonic() + wait
    while True:
        try:
            call(port, "GET", "/api/docs", timeout=2)
            return f"started ({how})"
        except DaemonUnreachable:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
