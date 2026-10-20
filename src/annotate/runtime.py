"""The stable copy of the code that the plugin's `bin/annotate` runs.

Claude Code installs a plugin in a folder per version and deletes the
previous one 14 days after an update (documentation, plugins/loading,
"Cleanup of previous versions"). A daemon started from that folder would go on
serving `static/annotate.js` and the home page from files that disappear two
weeks later. So `bin/annotate` never runs the package where Claude Code put
it: it copies it once into the data folder, and runs that copy.

    <data>/runtime/<fingerprint>/annotate/               one copy per distinct content
    <data>/runtime/current/annotate -> ../<fp>/annotate  the copy a restart runs
    <data>/runtime/current/README.txt                    why `current` is a folder

| Who                   | Runs                                           |
|-----------------------|------------------------------------------------|
| `bin/annotate` (CLI, hook) | the copy of its own version, made if missing |
| the daemon            | the copy it started from; `stale` once `current` names another |
| the systemd unit      | `current`, through `PYTHONPATH`                 |

The fingerprint covers every file of the package, the page and the overlay
included, so a change of `annotate.js` alone gives a copy of its own. It does
not depend on the version string: a plugin loaded in place from a clone (a
local marketplace, `--plugin-dir`) changes without one.

**`current` never goes back to a lower version**, and two mechanisms hold it.

- It is a FOLDER since 0.5.2, holding the link that names the copy. Up to
  0.5.1 it was itself a link, which every launcher replaced by a link to its
  own copy, and the launcher of an older version stays on the PATH of every
  session opened before an update: one `annotate status` there, and the next
  restart of the unit ran the older daemon (measured three times on 2026-10-19
  and 20). That code is frozen. But it replaces `current` with `os.replace`,
  and `rename(2)` never puts a link in place of a directory (EISDIR): the
  older launcher stops with "Is a directory" before it moves `current`,
  rewrites the icon's script or prunes a copy.
- From 0.5.2 on, a launcher never points `current` at a version lower than
  the one it names (versions compared as numbers, never as dates or text): it
  runs its own copy and leaves `current`, the icon's script and that copy
  alone. An equal version does move it, for a clone loaded in place.

The copy is written next to its final name and renamed into place, so a
reader never sees half a package; two launchers racing make the same copy
and one rename wins. What `current` names is decided under a lock, so that two
launchers of different versions cannot both read it before either writes.
Old copies are pruned, three kept, the highest versions first, and never the
one `current` names: a daemon that still runs an older one keeps its files
until it is restarted.

Nothing here runs in a test unless the test asks: `bin/annotate` is the only
caller.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from . import config

KEEP = 3
SKIPPED = frozenset({"__pycache__"})
POINTER = "annotate"
NOTICE = "README.txt"
ATTEMPTS = 5
LOCK_WAIT = 10.0
VERSION = re.compile(r'^__version__ = "(\d+(?:\.\d+)*)"$', re.MULTILINE)

NOTICE_TEXT = """\
Why `current` is a folder (docpin 0.5.2 and later)

The systemd unit runs the copy that the `annotate` link of this folder names.
Up to 0.5.1, `current` was itself a link, and the `bin/annotate` of an older
version, still on the PATH of a Claude Code session opened before an update,
pointed it back at its own copy: the next restart ran the older daemon.

A launcher of docpin 0.5.1 or older cannot replace a folder: it stops with
"Is a directory" and changes nothing. It belongs to a session opened before
the update: restart that session, and it gets the launcher of the new version.

Do not delete or replace this folder. If it is gone, the next `annotate` of
0.5.2 or later makes it again, naming the highest version it knows.
"""


class LaunchError(Exception):
    """The stable copy cannot be made or found."""


class _Raced(Exception):
    """`current` changed between reading it and replacing it."""


def runtime_dir() -> Path:
    return config.data_dir() / "runtime"


def current() -> Path:
    return runtime_dir() / "current"


def _files(package: Path) -> list[Path]:
    return sorted(p for p in package.rglob("*")
                  if p.is_file() and not SKIPPED.intersection(p.relative_to(package).parts))


def fingerprint(package: Path) -> str:
    """12 hex characters that change whenever a file of `package` does."""
    digest = hashlib.sha256()
    for path in _files(package):
        digest.update(path.relative_to(package).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def version_of(package: Path) -> tuple[int, ...] | None:
    """The version of an `annotate` package, as numbers: 0.10.0 is above 0.9.0.
    None when it cannot be read."""
    try:
        text = (package / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return None
    found = VERSION.search(text)
    if found is None:
        return None
    return tuple(int(part) for part in found.group(1).split("."))


def named() -> str | None:
    """The fingerprint of the copy `current` names, whatever its form (the
    folder of 0.5.2 on, the link of 0.5.1 and before); None if it names none."""
    try:
        package = (current() / POINTER).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    copy = package.parent
    return copy.name if copy.parent == runtime_dir().resolve() else None


def install(package: Path) -> Path:
    """Make sure the copy of `package` (the `annotate` folder of a plugin
    version) exists, point `current` at it unless it names a higher version,
    and return the folder to put on `sys.path`: always this version's copy."""
    if not (package / "cli.py").is_file():
        raise LaunchError(f"{package} is not the annotate package")
    try:
        stamp = fingerprint(package)
        target = runtime_dir() / stamp
        if not (target / "annotate" / "cli.py").is_file():
            staging = runtime_dir() / f".{stamp}-{uuid.uuid4().hex[:8]}"
            shutil.copytree(package, staging / "annotate",
                            ignore=shutil.ignore_patterns(*SKIPPED))
            try:
                staging.rename(target)
            except OSError:
                shutil.rmtree(staging, ignore_errors=True)   # another launcher won
                if not (target / "annotate" / "cli.py").is_file():
                    raise
        with _locked() as held:
            if held:
                chosen = _point_current(stamp)
                _place_tray_script(runtime_dir() / chosen / "annotate")
                _prune(keep={stamp, chosen})
    except OSError as exc:
        raise LaunchError(f"cannot install the code under {runtime_dir()}: {exc}") from exc
    return target


@contextlib.contextmanager
def _locked() -> Iterator[bool]:
    """Hold `runtime/.lock` while deciding what `current` names: True once
    held. Bounded: a launcher frozen while it held it (a paused session) never
    blocks the others; they run their own copy and leave `current` as it is
    (False), which can never take it back to a lower version."""
    with open(runtime_dir() / ".lock", "a", encoding="utf-8") as handle:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _kept(stamp: str) -> str:
    """The copy `current` must name: `stamp`, unless it names a copy of a
    strictly higher version, which stays. A copy whose version cannot be
    read gives way; one of the same version too (a clone loaded in place)."""
    there = named()
    if there is None or there == stamp:
        return stamp
    theirs = version_of(runtime_dir() / there / "annotate")
    if theirs is None:
        return stamp
    ours = version_of(runtime_dir() / stamp / "annotate")
    if ours is None or ours < theirs:
        return there
    return stamp


def _point_current(stamp: str) -> str:
    """Point `current` at the copy `stamp`, or keep the higher one it names,
    in the folder form; return the copy it names afterwards."""
    for _ in range(ATTEMPTS):
        chosen = _kept(stamp)
        folder = current()
        try:
            if folder.is_dir() and not folder.is_symlink():
                _aim(folder, chosen)
            else:
                _make_folder(chosen)
            return chosen
        except _Raced:
            continue
    raise OSError(f"{current()} kept changing while this launcher pointed it")


def _aim(folder: Path, stamp: str) -> None:
    """Replace the link inside the folder, atomically."""
    link = folder / POINTER
    aim = f"../{stamp}/annotate"
    try:
        if os.readlink(link) == aim:
            return
    except OSError:
        pass
    temporary = folder / f".{POINTER}-{uuid.uuid4().hex[:8]}"
    try:
        os.symlink(aim, temporary)
        os.replace(temporary, link)
    except FileNotFoundError as exc:                 # the folder went away meanwhile
        raise _Raced from exc


def _make_folder(stamp: str) -> None:
    """Put the folder form in place of the link of 0.5.1 and before (or of
    nothing). Not atomic, unlike `_aim`: a directory cannot be renamed over a
    link, so the link goes first; once, at the first launch of 0.5.2."""
    staging = runtime_dir() / f".pointer-{uuid.uuid4().hex[:8]}"
    staging.mkdir()
    os.symlink(f"../{stamp}/annotate", staging / POINTER)
    (staging / NOTICE).write_text(NOTICE_TEXT, encoding="utf-8")
    try:
        try:
            os.unlink(current())
        except FileNotFoundError:
            pass
        os.rename(staging, current())
    except OSError as exc:                           # `current` came back meanwhile
        shutil.rmtree(staging, ignore_errors=True)
        raise _Raced from exc


def tray_script() -> Path:
    """The Windows icon's script as a plain file: the Startup shortcut keeps
    its path for good. Measured on 2026-10-12: Windows sees `current` through
    `wsl.localhost` as a reparse point and does not traverse it, and a
    dated copy is pruned KEEP versions later."""
    return runtime_dir() / "tray.ps1"


def _place_tray_script(package: Path) -> None:
    source = package / "windows" / "tray.ps1"
    target = tray_script()
    data = source.read_bytes()
    if target.is_file() and target.read_bytes() == data:
        return
    temporary = runtime_dir() / f".tray-{uuid.uuid4().hex[:8]}.ps1"
    temporary.write_bytes(data)
    os.replace(temporary, target)


def _prune(keep: set[str]) -> None:
    # The links an older launcher leaves when the folder refuses them.
    for leftover in runtime_dir().glob(".current-*"):
        if leftover.is_symlink():
            leftover.unlink(missing_ok=True)
    copies = [p for p in runtime_dir().iterdir()
              if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
              and p.name != current().name]
    # The highest versions first, then the newest. An older launcher makes its
    # copy just before the folder refuses it: by date alone, that copy, which
    # nothing will ever run, would push out the one a daemon not yet
    # restarted still serves its page from.
    copies.sort(key=lambda p: (version_of(p / "annotate") or (), p.stat().st_mtime),
                reverse=True)
    for old in [p for p in copies if p.name not in keep][KEEP - 1:]:
        shutil.rmtree(old, ignore_errors=True)


def watched(code_dir: Path) -> Path:
    """The folder whose content tells the daemon its code is stale: its own,
    or, when it runs a copy, the copy `current` points at now."""
    return stable(code_dir)


def stable(code_dir: Path) -> Path:
    """The name of `code_dir` that survives an update, for Linux: `current/
    annotate` when it is a copy (whose dated folder is pruned KEEP versions
    later), else `code_dir` itself. Windows does not follow it (tray_script)."""
    try:
        code_dir.resolve().relative_to(runtime_dir().resolve())
    except ValueError:
        return code_dir
    return current() / "annotate"
