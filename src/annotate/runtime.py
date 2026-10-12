"""The stable copy of the code that the plugin's `bin/annotate` runs.

Claude Code installs a plugin in a folder per version and deletes the
previous one 14 days after an update (documentation, plugins/loading,
"Cleanup of previous versions"). A daemon started from that folder would go on
serving `static/annotate.js` and the home page from files that disappear two
weeks later. So `bin/annotate` never runs the package where Claude Code put
it: it copies it once into the data folder, and runs that copy.

    <data>/runtime/<fingerprint>/annotate/   one copy per distinct content
    <data>/runtime/current -> <fingerprint>  the newest, what a restart runs

| Who                   | Runs                                           |
|-----------------------|------------------------------------------------|
| `bin/annotate` (CLI, hook) | the copy of its own version, made if missing |
| the daemon            | the copy it started from; `stale` once `current` points elsewhere |
| the systemd unit      | `current`, through `PYTHONPATH`                 |

The fingerprint covers every file of the package, the page and the overlay
included, so a change of `annotate.js` alone gives a copy of its own. It does
not depend on the version string: a plugin loaded in place from a clone (a
local marketplace, `--plugin-dir`) changes without one.

The copy is written next to its final name and renamed into place, so a
reader never sees half a package; two launchers racing make the same copy
and one rename wins. Old copies are pruned, the three newest kept: a daemon
that still runs an older one keeps its files until it is restarted.

Nothing here runs in a test unless the test asks: `bin/annotate` is the only
caller.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from . import config

KEEP = 3
SKIPPED = frozenset({"__pycache__"})


class LaunchError(Exception):
    """The stable copy cannot be made or found."""


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


def install(package: Path) -> Path:
    """Make sure the copy of `package` (the `annotate` folder of a plugin
    version) exists, point `current` at it, and return the folder to put on
    `sys.path`."""
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
        _point_current(stamp)
        _prune(keep=stamp)
    except OSError as exc:
        raise LaunchError(f"cannot install the code under {runtime_dir()}: {exc}") from exc
    return target


def _point_current(stamp: str) -> None:
    link = current()
    try:
        if os.readlink(link) == stamp:
            return
    except OSError:
        pass
    temporary = runtime_dir() / f".current-{uuid.uuid4().hex[:8]}"
    os.symlink(stamp, temporary)
    os.replace(temporary, link)


def _prune(keep: str) -> None:
    copies = [p for p in runtime_dir().iterdir()
              if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")]
    copies.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old in [p for p in copies if p.name != keep][KEEP - 1:]:
        shutil.rmtree(old, ignore_errors=True)


def watched(code_dir: Path) -> Path:
    """The folder whose content tells the daemon its code is stale: its own,
    or, when it runs a copy, the copy `current` points at now."""
    return stable(code_dir)


def stable(code_dir: Path) -> Path:
    """The name of `code_dir` that survives an update: `current/annotate`
    when it is a copy (whose dated folder is pruned KEEP versions later),
    else `code_dir` itself."""
    try:
        code_dir.resolve().relative_to(runtime_dir().resolve())
    except ValueError:
        return code_dir
    return current() / "annotate"
