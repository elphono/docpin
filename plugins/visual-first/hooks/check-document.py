#!/usr/bin/env python3
"""PostToolUse hook: send the session back to a document it just wrote, when
the document is not what the visual-first style asks for.

The file is already on disk when this runs: a block does not prevent it, it
makes the session rework it BEFORE moving on, which prose instructions alone
did not obtain (the author's own sessions, 2026-09).

| The document                                   | Verdict |
|------------------------------------------------|---------|
| a Markdown deliverable                          | block: a document is a self-contained HTML page |
| an HTML page without any visual (svg, img, canvas, table, mermaid) | block |
| more than PROSE_PER_VISUAL paragraphs per visual | warning |
| a local image over 1 MB that git does not track yet | warning |
| a script, stylesheet or image loaded from the network | warning: not self-contained |
| a `class="mermaid"` block | not a visual: it draws nothing without its library, which a self-contained page does not load; inline SVG instead |
| `<!-- visual: exemption - reason -->` in its first five lines | nothing |

**What is a document is decided by docpin when it is installed**
(`annotate is-document <path>`, through `annotate` on the PATH or docpin's
stable copy: the folders the user tracks there, one rule in one place);
without it, by a `docs/` folder between the project and the
file. READMEs, `CLAUDE.md`, skills, dependencies and working notes (`plans/`,
`drafts/`) are never covered.

It never exits other than 0 or 2: a failure of this hook must not break a
session. `~/.claude/visual-first-off` turns it off for the machine.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TOOLS = {"Write", "Edit", "MultiEdit"}
SUFFIXES = {".md", ".html"}
EXCLUDED_NAMES = {"README.md", "CHANGELOG.md", "CONTRIBUTING.md", "CLAUDE.md", "AGENTS.md",
                  "LICENSE.md", "SKILL.md"}
# Working notes are Markdown on purpose ("brouillons": the same, in French).
EXCLUDED_FOLDERS = {"node_modules", ".venv", "venv", "site-packages", ".git", "__pycache__",
                    "plans", "drafts", "brouillons", "figures"}
PROSE_PER_VISUAL = 4
IMAGE_MAX_BYTES = 1_000_000

# English, and the French marker of the style this one comes from.
_EXEMPTION = re.compile(r"^\s*<!--\s*(?:visual\s*:\s*exemption|visuel\s*:\s*d[ée]rogation)"
                        r"\s*[—–-]\s*(.+?)\s*-->\s*$", re.I)
_FIGURE = re.compile(r"<(?:svg|img|canvas|table)\b", re.I)
_MERMAID = re.compile(r"class\s*=\s*[\"'][^\"']*\bmermaid\b", re.I)
_EXTERNAL = re.compile(r"<(?:script|img|link|iframe)\b[^>]*?\b(?:src|href)\s*=\s*[\"']"
                       r"((?:https?:)?//[^\"']+)", re.I)
_PARAGRAPH = re.compile(r"<p\b", re.I)
_LOCAL_IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"'#?:]+)[\"']", re.I)

FIX = "write it as a self-contained HTML page and draw what can be drawn (SVG, diagram, table)"
EXEMPT = ("If the form is justified (a transcript, raw data, the source of a figure), put "
          "<!-- visual: exemption - reason --> in its first five lines")


class CheckError(Exception):
    """The document cannot be checked (never surfaces: the hook then says nothing)."""


def exemption(text: str) -> str | None:
    for line in text.splitlines()[:5]:
        found = _EXEMPTION.match(line)
        if found:
            return found.group(1)
    return None


def _docpin_command() -> tuple[list[str], dict[str, str]] | None:
    """How to ask docpin: `annotate` on the PATH, else the stable copy docpin's
    plugin keeps in its data folder (src/annotate/runtime.py). Measured
    2026-10-12: a plugin hook does not get another plugin's bin/ on its PATH,
    so the copy is what this hook finds when both plugins are installed."""
    env = dict(os.environ)
    command = os.environ.get("VISUAL_FIRST_ANNOTATE")
    if command is not None:
        return ([command], env) if command else None
    found = shutil.which("annotate")
    if found:
        return [found], env
    data = Path(os.environ.get("ANNOTATE_DATA_DIR")
                or Path.home() / ".local" / "share" / "annotate")
    copy = data / "runtime" / "current"
    if (copy / "annotate" / "cli.py").is_file():
        return [sys.executable, "-P", "-m", "annotate"], {**env, "PYTHONPATH": str(copy)}
    return None


def _docpin_says(path: Path) -> bool | None:
    found = _docpin_command()
    if found is None:
        return None
    command, env = found
    try:
        done = subprocess.run([*command, "is-document", str(path)], capture_output=True,
                              text=True, timeout=10, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(done.returncode)


def covered(path: Path, project: Path) -> bool:
    if path.suffix.lower() not in SUFFIXES or path.name in EXCLUDED_NAMES:
        return False
    if EXCLUDED_FOLDERS.intersection(path.parts):
        return False
    answer = _docpin_says(path)
    if answer is not None:
        return answer
    try:
        inside = path.resolve().relative_to(project.resolve()).parts[:-1]
    except ValueError:
        return False
    return "docs" in inside


def _tracked_by_git(path: Path) -> bool:
    try:
        return subprocess.run(["git", "-C", str(path.parent), "ls-files", "--error-unmatch",
                               "--", path.name], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def heavy_images(text: str, folder: Path) -> list[tuple[str, int]]:
    """Local images over IMAGE_MAX_BYTES that git does not track yet: a new
    one is said when the document references it, a committed one is left."""
    found = []
    for src in dict.fromkeys(_LOCAL_IMAGE.findall(text)):
        image = folder / src
        try:
            size = image.stat().st_size
        except OSError:
            continue
        if size > IMAGE_MAX_BYTES and not _tracked_by_git(image):
            found.append((src, size))
    return found


def verdict(path: Path, text: str) -> tuple[str, str]:
    """("ok" | "warning" | "block" | "exempt", reason) for a covered document."""
    reason = exemption(text)
    if reason:
        return "exempt", reason
    if path.suffix.lower() != ".html":
        return "block", ("a Markdown deliverable: a document is a self-contained HTML page, "
                         "with as many visuals as it has ideas to show")
    figures = len(_FIGURE.findall(text))
    if figures == 0:
        mermaid = (" (a mermaid block draws nothing without its library: draw it as inline "
                   "SVG)") if _MERMAID.search(text) else ""
        return "block", ("no visual in this HTML page: a document is drawn (svg, img, table, "
                         f"canvas) before it is written{mermaid}")
    reasons = []
    external = list(dict.fromkeys(_EXTERNAL.findall(text)))
    if external:
        shown = ", ".join(external[:3]) + (" ..." if len(external) > 3 else "")
        reasons.append(f"not self-contained, it loads from the network: {shown}")
    paragraphs = len(_PARAGRAPH.findall(text))
    if paragraphs > PROSE_PER_VISUAL * figures:
        reasons.append(f"{paragraphs} paragraphs for {figures} visual(s), more than "
                       f"{PROSE_PER_VISUAL} per visual: draw the rest")
    for src, size in heavy_images(text, path.parent):
        reasons.append(f"{src} weighs {size / 1e6:.1f} MB, over {IMAGE_MAX_BYTES / 1e6:.0f} MB: "
                       f"render it smaller (no grain, scale 1)")
    return ("warning", "; ".join(reasons)) if reasons else ("ok", "")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or payload.get("tool_name") not in TOOLS:
            return 0
        if (Path.home() / ".claude" / "visual-first-off").exists():
            return 0
        raw = (payload.get("tool_input") or {}).get("file_path")
        if not isinstance(raw, str) or not raw:
            return 0
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
        project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd or ".")
        path = Path(raw) if Path(raw).is_absolute() else project / raw
        if not covered(path, project):
            return 0
        status, reason = verdict(path, path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - a broken check must never break the session
        return 0
    if status == "block":
        print(f"{path.name}: {reason}.\nRework it now, before anything else: {FIX}.\n"
              f"{EXEMPT}.", file=sys.stderr)
        return 2
    if status == "warning":
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"{path.name}: {reason}. Rework it before moving on: {FIX}.",
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
