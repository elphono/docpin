"""The folders the daemon tracks: where an .html file becomes a document.

Before 2026-10-07 there was one rule, written twice (here and in the hook):
an .html file with a `docs/` folder between the workspace and itself. It
missed what sessions produce elsewhere, measured that day: a deliverable
under `~/.claude/sync/docs/` (outside the workspace) and a report under a
`rapports/` folder of a repository (no `docs/` on the way). The user now picks
the folders from the index page (decision of 2026-10-07: "on peut choisir
n'importe quel dossier de ~/ [...] tout ce qui est en dessous du dossier
choisi est traqué").

    folders.json   {"version": 1, "folders": [{"path": "/abs", "docs_only": false}],
                    "excluded": [{"path": "/abs"}]}

| Folder                          | What is tracked under it                    |
|---------------------------------|---------------------------------------------|
| the default one, the workspace  | every .html with a `docs/` folder on the way (`docs_only`) |
| the default without a workspace | the same rule under the home directory, written by a session only (`scan: false`) |
| a folder the user added         | every .html, at any depth                   |
| an EXCLUDED folder              | nothing, whatever tracks it                 |

**The home directory is never walked** (`scan: false`): measured 2026-10-12,
one pass over a developer's home took 33.6 s, against 0.3 s for the
workspace, and the daemon walks every 30 s. Without a workspace, a document
is therefore found when a session writes it (the hook), or when the user adds
its folder; one written by Bash waits for that.

In both cases nothing under a `SKIPPED` folder is a document. The default
entry exists only while the `folders` list is absent: once the user adds or
removes a folder, the list on disk is the whole truth, the workspace included.

**Excluded folders** (decision of 2026-10-19): a session that builds a web
application writes `.html` files that are pages of the application, not
documents to read, and they were listed. An excluded folder wins over every
tracked one, the workspace's `docs/` rule included; excluding one unmanages
at once the documents already listed under it (`exclude`), except those a
batch never takes (registry.protection).

**One rule, in one place**: the hook does not decide any more, it hands every
.html it sees to `annotate register --hook`, which asks `wanted()`; the
daemon's pass and the rescan ask it too.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, registry

# Folders never entered: tool caches, dependencies, build output (coverage and
# lint reports are HTML too, by the hundred), `figures/`, where the HTML
# sources of a document's images live (docs/figures/<x>/tpl.html: inputs of a
# picture, not documents to read), and `.claude` (agent worktrees: temporary
# copies, deleted with the agent).
SKIPPED = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache",
                     ".pytest_cache", ".ruff_cache", "figures", ".claude",
                     "build", "_build", "dist", "target", "htmlcov", "site-packages"})


class FoldersError(Exception):
    """The list of folders cannot be read, written, or does not accept this one."""


def path() -> Path:
    return config.data_dir() / "folders.json"


def home() -> Path:
    return Path.home().resolve()


def _default() -> list[dict[str, Any]]:
    workspace = config.workspace().expanduser()
    if workspace.is_dir():
        return [{"path": str(workspace.resolve()), "docs_only": True}]
    return [{"path": str(home()), "docs_only": True, "scan": False}]


def _entry(item: dict[str, Any]) -> dict[str, Any]:
    entry = {"path": str(item["path"]), "docs_only": bool(item.get("docs_only"))}
    if item.get("scan") is False:
        entry["scan"] = False
    return entry


def _raw() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise FoldersError(f"cannot read {path()}: {exc}") from exc
    if not isinstance(data, dict):
        raise FoldersError(f"{path()}: not a JSON object")
    return data


def load() -> list[dict[str, Any]]:
    """The tracked folders. Without a `folders` list on disk (no file, or only
    exclusions in it), the default one."""
    data = _raw()
    if "folders" not in data:
        return _default()
    items = data["folders"]
    if not isinstance(items, list):
        raise FoldersError(f"{path()}: no 'folders' list")
    return [_entry(item) for item in items
            if isinstance(item, dict) and isinstance(item.get("path"), str)]


def load_excluded() -> list[Path]:
    """The excluded folders, absolute and resolved (`normalise`)."""
    items = _raw().get("excluded", [])
    if not isinstance(items, list):
        raise FoldersError(f"{path()}: 'excluded' is not a list")
    return [Path(item["path"]) for item in items
            if isinstance(item, dict) and isinstance(item.get("path"), str)]


def _save(**lists: list[Any]) -> None:
    """Rewrite `folders` and/or `excluded`, keeping the other list as it is."""
    data = {**_raw(), **lists, "version": 1}
    try:
        registry.atomic_write(path(), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise FoldersError(f"cannot write {path()}: {exc}") from exc


def normalise(raw: str) -> Path:
    """`raw` as an absolute, resolved folder under the home directory, or
    FoldersError. `~` is expanded; a relative path is refused rather than
    resolved against the daemon's own working directory."""
    text = (raw or "").strip()
    if not text:
        raise FoldersError("no folder given")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise FoldersError(f"{text!r} is not an absolute path (start it with / or ~)")
    folder = candidate.resolve()
    if not folder.is_relative_to(home()):
        raise FoldersError(f"{folder} is not under {home()}")
    if not folder.is_dir():
        raise FoldersError(f"{folder} is not a folder")
    return folder


def add(raw: str) -> dict[str, Any]:
    """Track every .html under `raw`. Adding a folder already tracked makes it
    track everything (a `docs_only` entry becomes a full one)."""
    folder = normalise(raw)
    with registry.locked():
        items = [i for i in load() if i["path"] != str(folder)]
        entry = {"path": str(folder), "docs_only": False}
        _save(folders=[*items, entry])
    return entry


def remove(raw: str) -> dict[str, Any]:
    """Stop tracking a folder. Its documents stay registered: unmanaging them
    is a decision per document."""
    text = (raw or "").strip()
    with registry.locked():
        items = load()
        kept = [i for i in items if i["path"] != text]
        if len(kept) == len(items):
            raise FoldersError(f"{text!r} is not a tracked folder")
        _save(folders=kept)
    return next(i for i in items if i["path"] == text)


def excluded_by(path: Path, excluded: list[Path]) -> bool:
    """True if `path` (absolute, resolved) lies under one of `excluded`."""
    return any(path.is_relative_to(folder) for folder in excluded)


def exclude(raw: str, *, listening: Callable[[str], bool]) -> dict[str, Any]:
    """Never take an .html under `raw` again, and unmanage at once the
    documents listed under it, but those a batch never takes
    (registry.protection: `listening` says which ones a session waits for).
    Each unmanaged file is remembered as an Unmanage does (forgotten.json)."""
    folder = normalise(raw)
    with registry.locked():
        others = [str(p) for p in load_excluded() if p != folder]
        _save(excluded=[{"path": p} for p in [*others, str(folder)]])
    batch = registry.forget_batch(lambda doc: excluded_by(Path(doc["path"]), [folder]),
                                  listening)
    return {"folder": {"path": str(folder)}, **batch}


def unexclude(raw: str) -> dict[str, Any]:
    """Take the .html files under `raw` again, as the tracked folders say.
    What the exclusion unmanaged stays out until written again, as after an
    Unmanage."""
    text = (raw or "").strip()
    with registry.locked():
        items = [str(p) for p in load_excluded()]
        if text not in items:
            raise FoldersError(f"{text!r} is not an excluded folder")
        _save(excluded=[{"path": p} for p in items if p != text])
    return {"path": text}


def _matches(path: Path, folder: Path, docs_only: bool) -> bool:
    try:
        parts = path.relative_to(folder).parts[:-1]
    except ValueError:
        return False
    if SKIPPED.intersection(parts):
        return False
    return "docs" in parts if docs_only else True


def wanted(path: Path, items: list[dict[str, Any]] | None = None,
           excluded: list[Path] | None = None) -> bool:
    """True if `path` is a document: an .html file that one tracked folder
    claims and no excluded folder holds. The hook (through `annotate register
    --hook`), the daemon's pass and the rescan all ask this, and nothing else.
    `items` and `excluded` default to what is on disk."""
    if path.suffix.lower() != ".html":
        return False
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False
    if excluded_by(resolved, load_excluded() if excluded is None else excluded):
        return False
    return any(_matches(resolved, Path(i["path"]), i["docs_only"])
               for i in (load() if items is None else items))


def roots(items: list[dict[str, Any]]) -> list[Path]:
    """The folders to walk, never a `scan: false` one: a folder inside another one is already visited by
    that one's walk, unless a SKIPPED folder lies between them (`~` prunes
    `.claude`, so `~/.claude/sync/docs` must be walked on its own)."""
    paths = sorted({Path(i["path"]) for i in items if i.get("scan") is not False},
                   key=lambda p: len(p.parts))
    kept: list[Path] = []
    for folder in paths:
        covered = any(folder != outer and folder.is_relative_to(outer)
                      and not SKIPPED.intersection(folder.relative_to(outer).parts)
                      for outer in kept)
        if not covered:
            kept.append(folder)
    return kept


def subfolders(raw: str, limit: int = 50) -> dict[str, Any]:
    """What the index page proposes while the user types a folder: the
    subfolders of the deepest existing folder of `raw`, under the home
    directory only. Hidden folders included: `~/.claude/sync/docs` is one of
    the folders this exists for."""
    text = (raw or "~/").strip() or "~/"
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = home() / candidate
    base = candidate if text.endswith("/") else candidate.parent
    prefix = "" if text.endswith("/") else candidate.name
    try:
        base = base.resolve()
    except OSError:
        base = home()
    if not base.is_relative_to(home()) or not base.is_dir():
        base, prefix = home(), ""
    try:
        names = sorted(e.name for e in base.iterdir()
                       if e.is_dir() and e.name.startswith(prefix))
    except OSError:
        names = []
    return {"base": str(base), "folders": [str(base / n) for n in names[:limit]]}
