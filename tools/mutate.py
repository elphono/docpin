"""Directed mutation campaign on the central properties of annotate-workflow.

    uv run python tools/mutate.py            # every mutation, full default suite
    uv run python tools/mutate.py orphan-js  # one of them

Each mutation breaks ONE property on purpose; the suite must go red. The
campaign never touches the working tree: it copies the repository into a
temporary folder, mutates the copy, runs the suite there with this
interpreter (the copy's `src` comes first on sys.path through the pytest
`pythonpath` setting), and bounds every run. A run that exceeds its bound
counts as a detection: a suite that does not return does not pass.

Before mutating, the UNMUTATED copy must be green, or every "kill" would be
a crash rather than a detection.

Mutations marked `browser` run the browser suite (`-m browser`), which needs
ANNOTATE_PLAYWRIGHT_DIR (see tests/test_browser.py).

Adapted from remarkable-sync's tools/mutate.py, reduced to directed
mutations: here the list is short and each line names what it breaks.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 600


class MutationError(Exception):
    """A mutation could not be applied (its anchor text is gone)."""


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    breaks: str
    browser: bool = False


MUTATIONS = (
    Mutation("path-escape", "src/annotate/server.py",
             "    if not target.is_relative_to(root):\n",
             "    if False:\n",
             "files outside the document folder are refused"),
    Mutation("send-all", "src/annotate/registry.py",
             '    return [a for a in items if not a.get("sent_at")]\n',
             "    return list(items)\n",
             "send only sends the unsent annotations"),
    Mutation("orphan-prompt", "src/annotate/prompt.py",
             "        node = htmldoc.resolve(doc, item.get(\"selector\", \"\"))\n",
             "        node = htmldoc.resolve(doc, item.get(\"selector\", \"\"))\n"
             "        if node is None:\n            continue\n",
             "an annotation whose selector no longer resolves is still sent"),
    Mutation("orphan-js", "src/annotate/static/annotate.js",
             "      if (!target) { orphans.push(item); continue; }\n",
             "      if (!target) { annotations = annotations.filter((a) => a !== item);"
             " continue; }\n",
             "an orphan stays in the model, so the next save keeps it",
             browser=True),
    Mutation("guard-claude", "tests/conftest.py",
             "            if name in FORBIDDEN_PROGRAMS:\n",
             "            if False:\n",
             "no test can launch a real claude"),
    Mutation("listener-dead", "src/annotate/listeners.py",
             "                if waiter.alive():\n",
             "                if True:\n",
             "a waiter whose client went away is never handed notes"),
    Mutation("listener-gives-up", "src/annotate/listeners.py",
             "            if time.monotonic() >= deadline or not alive():\n",
             "            if time.monotonic() >= deadline:\n",
             "a wait stops as soon as its client goes away"),
    Mutation("send-lock", "src/annotate/sender.py",
             "        with self._lock:\n            return self._dispatch(doc_id, fresh)\n",
             "        return self._dispatch(doc_id, fresh)\n",
             "a double click hands the notes over once"),
    Mutation("listener-first", "src/annotate/sender.py",
             "        if not fresh:\n",
             "        if False:\n",
             "an open session that listens gets the notes, no tab opens"),
    Mutation("fresh-to-listener", "src/annotate/sender.py",
             "        if not fresh:\n",
             "        if True:\n",
             "'New session' never reaches the listening session"),
    Mutation("resume-unchecked", "src/annotate/sender.py",
             "        resume = session if session and claude.resumable(session) else None\n",
             "        resume = session or None\n",
             "a session gone from this machine is not resumed"),
    Mutation("prompt-split", "src/annotate/terminal.py",
             "            f\"exec {shlex.join(command)} \\\"$prompt\\\"\\n\")\n",
             "            f\"exec {shlex.join(command)} $prompt\\n\")\n",
             "the prompt reaches claude as ONE argument"),
    Mutation("hook-repeats", "src/annotate/cli.py",
             "    if _listening(cfg, doc_id):\n        return {}\n",
             "",
             "the hook stays silent when a session already listens"),
    Mutation("answered", "src/annotate/registry.py",
             "        elif entry.get(\"status\") in (\"delivered\", \"answered\"):\n",
             "        elif False:\n",
             "a write after a delivery marks the document answered"),
    Mutation("wait-gives-up", "src/annotate/cli.py",
             "            time.sleep(args.retry)\n            continue\n",
             "            raise\n",
             "`annotate wait` survives a daemon that is away"),
    Mutation("scan-backfill", "src/annotate/scanner.py",
             "        if since is None:\n            write_since(start)\n",
             "        if since is None:\n            since = 0.0\n        if False:\n"
             "            write_since(start)\n",
             "nothing older than the first pass is taken"),
    Mutation("scan-forgets-progress", "src/annotate/scanner.py",
             "        write_since(start)\n        return added\n",
             "        return added\n",
             "each pass starts where the previous one did"),
    Mutation("rescan-everything", "src/annotate/scanner.py",
             "candidates(items, start - days * DAY)",
             "candidates(items, 0.0)",
             "a rescan takes only the files of the last N days"),
    Mutation("rescan-revives-unmanaged", "src/annotate/scanner.py",
             " or registry.dismissed(path.resolve(), mtime)",
             "",
             "a rescan never brings back what the user unmanaged"),
    Mutation("rescan-twice", "src/annotate/scanner.py",
             "            if self.running is not None:\n                return False\n",
             "",
             "one catch-up at a time"),
    Mutation("folder-outside-home", "src/annotate/folders.py",
             "    if not folder.is_relative_to(home()):\n",
             "    if False:\n",
             "a folder is tracked only under the home directory"),
    Mutation("added-folder-docs-only", "src/annotate/folders.py",
             '        entry = {"path": str(folder), "docs_only": False}\n',
             '        entry = {"path": str(folder), "docs_only": True}\n',
             "an added folder tracks every .html under it"),
    Mutation("default-folder-everything", "src/annotate/folders.py",
             '"docs_only": True}]\n',
             '"docs_only": False}]\n',
             "the workspace keeps its docs/ rule"),
    Mutation("nested-skipped-not-walked", "src/annotate/folders.py",
             "                      and not SKIPPED.intersection(folder.relative_to(outer).parts)\n",
             "\n",
             "a folder under a skipped one is walked on its own"),
    Mutation("hook-takes-everything", "src/annotate/cli.py",
             "    if args.hook and not folders.wanted(Path(args.path)):\n",
             "    if False:\n",
             "the hook registers only a document of a tracked folder"),
    Mutation("writer-earliest", "src/annotate/claude.py",
             "(best is None or when > best[0])",
             "(best is None or when < best[0])",
             "the writer is the LATEST call naming the file"),
    Mutation("writer-reader", "src/annotate/claude.py",
             "not mtime - WRITE_WINDOW <= when <= mtime + WRITE_SLACK",
             "not mtime - WRITE_WINDOW <= when",
             "a call made after the write only read the file"),
    Mutation("scan-figures", "src/annotate/folders.py",
             '".ruff_cache", "figures", ".claude",',
             '".ruff_cache", ".claude",',
             "figures/ sources are not documents"),
    Mutation("scan-build", "src/annotate/folders.py",
             '"build", "_build", ',
             "",
             "build output is not a document"),
    Mutation("title-ai-first", "src/annotate/claude.py",
             "        self._seen[path] = (offset + end, custom, ai)\n        return custom or ai\n",
             "        self._seen[path] = (offset + end, custom, ai)\n        return ai or custom\n",
             "a /rename title wins over the generated one"),
    Mutation("unknown-first", "src/annotate/registry.py",
             "    return known + ([groups[\"\"]] if \"\" in groups else [])\n",
             "    return ([groups[\"\"]] if \"\" in groups else []) + known\n",
             "documents without a known session come last"),
    Mutation("control-anywhere", "src/annotate/service.py",
             "    if not os.environ.get(\"INVOCATION_ID\"):\n",
             "    if False:\n",
             "the daemon buttons act only on a daemon systemd started"),
    Mutation("control-unguarded", "src/annotate/server.py",
             "            if method in (\"PUT\", \"POST\", \"DELETE\"):\n                self._check_client()\n",
             "            pass\n",
             "a page of another site cannot press the buttons"),
    Mutation("inbox-claims-bypass", "src/annotate/inbox.py",
             "f'from-name=\"{escape(SENDER_NAME)}\">\\n{text}",
             "f'from-name=\"{escape(SENDER_NAME)}\" from-mode=\"bypass\">\\n{text}",
             "the daemon never claims a permission class (the approval dialog stays)"),
    Mutation("inbox-pid-reuse", "src/annotate/inbox.py",
             "        if _proc_start(pid) != str(entry.get(\"procStart\")):\n",
             "        if _proc_start(pid) is None:\n",
             "a reused pid is not the session"),
    Mutation("inbox-skipped", "src/annotate/sender.py",
             "            if opened:\n                return self._post(doc_id, items, text, opened)\n",
             "",
             "an open session gets the notes, never a duplicate tab"),
    Mutation("inbox-oldest", "src/annotate/inbox.py",
             "key=lambda e: e.get(\"updatedAt\") or 0, reverse=True)",
             "key=lambda e: e.get(\"updatedAt\") or 0)",
             "the most recently active copy of a twice-open session"),
    Mutation("at-dropped", "src/annotate/static/annotate.js",
             "      at: selection ? '' : textAt(e),\n",
             "      at: '',\n",
             "the word under the cursor is captured",
             browser=True),
    Mutation("attach-no-write", "src/annotate/registry.py",
             "        entry[\"session_id\"] = session\n",
             "",
             "attaching a document writes its session_id"),
    Mutation("attach-unchecked", "src/annotate/registry.py",
             "        if not claude.resumable(session):\n",
             "        if False:\n",
             "a session this machine cannot resume is refused"),
    Mutation("attach-raw-cwd", "src/annotate/registry.py",
             "            entry[\"cwd\"] = str(usable_cwd(hint or entry.get(\"cwd\"), "
             "Path(entry[\"path\"])))\n",
             "            entry[\"cwd\"] = hint or entry.get(\"cwd\")\n",
             "an attach never keeps a deleted worktree as the session folder"),
    # No mutation of the pattern that keeps subagents out of the session list:
    # `resumable` keeps them out too, each guard covers the other (measured
    # 2026-10-08: either one removed, the test stays green; both, it goes red).
    Mutation("sessions-unresumable", "src/annotate/claude.py",
             "        if not resumable(session):\n            continue\n",
             "",
             "only conversations `claude --resume` would find are offered"),
    Mutation("sessions-open-not-first", "src/annotate/claude.py",
             "key=lambda kv: (kv[0] not in open_ids, -kv[1])",
             "key=lambda kv: -kv[1]",
             "open sessions come first in the list"),
    Mutation("sessions-unbounded", "src/annotate/claude.py",
             "        if len(found) >= limit and not is_open:\n            break\n",
             "",
             "the session list is bounded"),
    Mutation("sessions-cut-open", "src/annotate/claude.py",
             "        if len(found) >= limit and not is_open:\n",
             "        if len(found) >= limit:\n",
             "the bound never cuts an open session"),
    Mutation("groups-never-open", "src/annotate/server.py",
             "                group[\"open\"] = group[\"session_id\"] in open_ids\n",
             "                group[\"open\"] = False\n",
             "the listing says which session groups are open"),
    Mutation("picker-no-id-filter", "src/annotate/static/index.js",
             "short.includes(w) ||\n                              id.startsWith(w));",
             "short.includes(w));",
             "the picker filters on the start of a session id",
             browser=True),
    Mutation("picker-attaches-nothing", "src/annotate/static/index.js",
             "'/session', { session });",
             "'/session', { session: '' });",
             "choosing a session in the picker attaches the document to it",
             browser=True),
    Mutation("baseline-not-taken", "src/annotate/sender.py",
             "        missing = registry.take_baseline(doc_id, stamp)\n",
             "        missing = ''\n",
             "every send keeps the document as the reference"),
    Mutation("baseline-kept-on-failure", "src/annotate/registry.py",
             "            target.unlink(missing_ok=True)\n",
             "            pass\n",
             "a reference that cannot be written is removed"),
    Mutation("pairing-unchecked", "src/annotate/static/diff.js",
             "jaccard(old.words, unit.words) >= PAIR_MIN",
             "jaccard(old.words, unit.words) >= 0",
             "only a close block counts as changed", browser=True),
    Mutation("fold-hides-unsent", "src/annotate/static/changes.js",
             "        if (!changedHere && !unsent) fold(sec);\n",
             "        if (!changedHere) fold(sec);\n",
             "a section with an unsent note never folds", browser=True),
    Mutation("mark-unverified", "src/annotate/static/diff.js",
             "      unit.live = node && signature(node) === unit.sig ? node : null;\n",
             "      unit.live = node;\n",
             "a block is marked only where the page shows the same text", browser=True),
    Mutation("fold-shifts-anchors", "src/annotate/static/changes.js",
             "      const bar = document.createElement('annotate-fold');\n",
             "      const bar = document.createElement('p');\n",
             "folding never moves a pin anchor", browser=True),
    Mutation("witness-draws", "src/annotate/static/changes.js",
             "    if (!result.tooMany && !items.length) return null;\n",
             "",
             "nothing is drawn when nothing changed", browser=True),
    Mutation("nested-marks-return", "src/annotate/static/changes.js",
             "      const group = groupOf(item.target);\n"
             "      if (!group) { drawn.push(item); continue; }\n"
             "      if (shown.has(group) || under(group)) continue;\n",
             "      const group = item.target;\n",
             "a block inside a new block gets no mark of its own", browser=True),
    Mutation("new-list-split-per-item", "src/annotate/static/changes.js",
             "up && up !== document.body && allNew(up);",
             "up && up !== document.body && false;",
             "a whole new list (table, card) carries one mark, not one per item",
             browser=True),
    Mutation("grid-marked-whole", "src/annotate/static/changes.js",
             "      if (kids.filter(titled).length >= 2) return kids.flatMap(split);\n",
             "",
             "a grid of new cards marks each card, not the grid", browser=True),
    Mutation("wrapper-takes-mark", "src/annotate/static/changes.js",
             "      return [c.blocks === 1 ? c.one : el];",
             "      return [el];",
             "a lone new block keeps the mark, not its wrapper", browser=True),
    Mutation("absorbed-dimmed", "src/annotate/static/changes.js",
             "new Set(located.filter((i) => i.kind !== 'removed').concat(parts).map((i) => i.target));",
             "new Set(drawn.filter((i) => i.kind !== 'removed').map((i) => i.target));",
             "a block absorbed by a new one is never dimmed", browser=True),
    Mutation("tint-hides-background", "src/annotate/static/annotate.css",
             ".annotate-new { box-shadow: -6px 0 0 #16a34a, inset 0 0 0 9999px rgba(22, 163, 74, .12) !important; }",
             ".annotate-new { background-color: rgba(22, 163, 74, .12) !important; "
             "box-shadow: -6px 0 0 #16a34a !important; }",
             "a marked block keeps its own background under the tint", browser=True),
    Mutation("tag-over-line-above", "src/annotate/static/changes.js",
             "            if (covers(x) && right > x && !covers(right)) x = right;\n",
             "",
             "a tag with no room above goes to the right end, off the line above",
             browser=True),
    Mutation("tag-above-padding", "src/annotate/static/changes.js",
             " - 17 + Math.min(16, Math.max(0, room - 1));\n",
             " - 17;\n",
             "a padded block's tag is a tab on its top edge, not on the block above",
             browser=True),
    Mutation("tags-collide", "src/annotate/static/changes.js",
             "          const near = placed.slice(-8);\n",
             "          const near = [];\n",
             "a tag slides past the one placed just before, never on it", browser=True),
    Mutation("excluded-ignored", "src/annotate/folders.py",
             "    if excluded_by(resolved, load_excluded() if excluded is None else excluded):\n",
             "    if False:\n",
             "an excluded folder wins over every tracked one"),
    Mutation("exclude-forgets-nothing", "src/annotate/folders.py",
             "    batch = registry.forget_batch(lambda doc: excluded_by(Path(doc[\"path\"]), [folder]),\n",
             "    batch = registry.forget_batch(lambda doc: False,\n",
             "excluding a folder unmanages what is listed under it"),
    Mutation("exclude-forgets-everything", "src/annotate/folders.py",
             "    batch = registry.forget_batch(lambda doc: excluded_by(Path(doc[\"path\"]), [folder]),\n",
             "    batch = registry.forget_batch(lambda doc: True,\n",
             "excluding a folder unmanages nothing outside it"),
    Mutation("rescan-clears-nothing", "src/annotate/scanner.py",
             "        return doc[\"mtime\"] is None or doc[\"mtime\"] <= since\n",
             "        return False\n",
             "a rescan unmanages what is older than its window"),
    Mutation("rescan-window-edge", "src/annotate/scanner.py",
             "        return doc[\"mtime\"] is None or doc[\"mtime\"] <= since\n",
             "        return doc[\"mtime\"] is None or doc[\"mtime\"] < since\n",
             "the window's edge splits the files as the catch-up does"),
    Mutation("rescan-clears-recent", "src/annotate/scanner.py",
             "        return doc[\"mtime\"] is None or doc[\"mtime\"] <= since\n",
             "        return True\n",
             "a rescan keeps what was written in its window"),
    Mutation("rescan-clears-other-folders", "src/annotate/scanner.py",
             "        if folder is not None and not Path(doc[\"path\"]).is_relative_to(folder):\n",
             "        if False:\n",
             "a rescan of one folder unmanages only under it"),
    Mutation("add-folder-clears", "src/annotate/scanner.py",
             "            if prune:\n                result = rescan(",
             "            if True:\n                result = rescan(",
             "adding a folder never unmanages"),
    Mutation("page-rescan-adds-only", "src/annotate/server.py",
             "            started = self.server.catchups.start(days, prune=True)\n",
             "            started = self.server.catchups.start(days)\n",
             "Rescan from the page also clears"),
    Mutation("protect-not-listening", "src/annotate/registry.py",
             "    if doc.get(\"listening\", True):\n",
             "    if False:\n",
             "a batch keeps the document a session waits for"),
    Mutation("protect-not-delivered", "src/annotate/registry.py",
             "    if doc.get(\"status\") == \"delivered\":\n",
             "    if False:\n",
             "a batch keeps the document the session is on"),
    Mutation("protect-not-missing", "src/annotate/registry.py",
             "    if not doc.get(\"exists\", False):\n",
             "    if False:\n",
             "a batch keeps the document whose file is missing"),
    Mutation("protect-not-pending", "src/annotate/registry.py",
             "    if doc.get(\"pending\", 1):\n",
             "    if False:\n",
             "a batch keeps the document whose notes wait to be sent"),
    Mutation("fold-not-remembered", "src/annotate/static/index.js",
             "      try { stored = window.localStorage.getItem(FOLD_KEY + key) === '1'; } catch (err) { stored = false; }\n",
             "      stored = false;\n",
             "a folded section stays folded after a reload", browser=True),
    Mutation("fold-lost-on-redraw", "src/annotate/static/index.js",
             "    show(isFolded(key));\n",
             "    show(false);\n",
             "a redraw of the list keeps what is folded", browser=True),
    Mutation("fold-needs-storage", "src/annotate/static/index.js",
             "      try { stored = window.localStorage.getItem(FOLD_KEY + key) === '1'; } catch (err) { stored = false; }\n",
             "      stored = window.localStorage.getItem(FOLD_KEY + key) === '1';\n",
             "the page folds without storage", browser=True),
    Mutation("fold-shows-documents", "src/annotate/static/index.html",
             "  .group.folded > .doc, .repo.folded > .repo-sessions { display: none; }\n",
             "  .group.folded > .doc, .repo.folded > .repo-sessions { opacity: .99; }\n",
             "a folded section hides its documents", browser=True),
    # Cleanup files (2026-10-19): what may delete a file of the user.
    Mutation("cleanup-deletes-at-once", "src/annotate/registry.py",
             "            _unmanage(docs, doc_id, table)\n            if cleanup and info is not None:\n",
             "            _unmanage(docs, doc_id, table)\n            Path(entry[\"path\"]).unlink()\n"
             "            if cleanup and info is not None:\n",
             "the batch deletes nothing on the disk at once"),
    Mutation("rescan-schedules-deletion", "src/annotate/scanner.py",
             "    result = registry.forget_batch(older, listening, cleanup=False)\n",
             "    result = registry.forget_batch(older, listening, cleanup=True)\n",
             "only a file Cleanup files took is ever deleted (not a rescan's)"),
    Mutation("exclude-schedules-deletion", "src/annotate/folders.py",
             "                                  listening, cleanup=False)\n",
             "                                  listening, cleanup=True)\n",
             "only a file Cleanup files took is ever deleted (not an exclusion's)"),
    Mutation("cleanup-ignores-touch", "src/annotate/registry.py",
             "                       or (info.st_mtime, info.st_size) != (record[\"mtime\"], record[\"size\"]))\n",
             "                       or False)\n",
             "a file touched since its cleanup is never deleted"),
    Mutation("cleanup-ignores-size", "src/annotate/registry.py",
             "(info.st_mtime, info.st_size) != (record[\"mtime\"], record[\"size\"])",
             "info.st_mtime != record[\"mtime\"]",
             "a file grown or shrunk under the same date counts as touched"),
    Mutation("cleanup-too-early", "src/annotate/registry.py",
             "            if now - record[\"cleaned_at\"] < after:\n                continue\n",
             "",
             "nothing is deleted before the delay"),
    Mutation("cleanup-delay-edge", "src/annotate/registry.py",
             "            if now - record[\"cleaned_at\"] < after:\n",
             "            if now - record[\"cleaned_at\"] <= after:\n",
             "a file is deleted once the delay has passed, to the second"),
    Mutation("cleanup-deletes-registered", "src/annotate/registry.py",
             "            if path in registered or touched:\n",
             "            if touched:\n",
             "the sweep never deletes the file of a listed document"),
    Mutation("cleanup-survives-registration", "src/annotate/registry.py",
             "        index = _read_cleanup()\n        if index.pop(str(path), None) is not None:\n"
             "            _write_cleanup(index)\n",
             "",
             "a file listed again leaves the deletion index"),
    Mutation("cleanup-index-takes-bools", "src/annotate/registry.py",
             "        numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]\n",
             "        numbers = [v for v in values if isinstance(v, (int, float))]\n",
             "an index entry that cannot be read is never deleted"),
    Mutation("cleanup-takes-session-docs", "src/annotate/cleanup.py",
             "        return not doc.get(\"session_id\") and (chosen is None or doc[\"id\"] in chosen)\n",
             "        return chosen is None or doc[\"id\"] in chosen\n",
             "Cleanup files takes only documents without a known session"),
    Mutation("cleanup-ignores-ids", "src/annotate/cleanup.py",
             "        return not doc.get(\"session_id\") and (chosen is None or doc[\"id\"] in chosen)\n",
             "        return not doc.get(\"session_id\")\n",
             "Cleanup files takes only what the page showed"),
    Mutation("sweeper-every-tick", "src/annotate/cleanup.py",
             "        if self.next_at is not None and moment < self.next_at:\n            return None\n",
             "",
             "the daemon sweeps at most once an hour"),
    Mutation("scan-never-sweeps", "src/annotate/scanner.py",
             "            sweeper.tick()\n",
             "            pass\n",
             "the daemon's scan loop applies the deletions due"),
    Mutation("page-blind-to-protection", "src/annotate/server.py",
             "        live[\"protected\"] = registry.protection(live)\n",
             "        live[\"protected\"] = \"\"\n",
             "the page counts only what a batch may take"),
    Mutation("cleanup-button-everywhere", "src/annotate/static/index.js",
             "    if (!known) tools.append(cleanupButton(docs));\n",
             "    tools.append(cleanupButton(docs));\n",
             "Cleanup files lives in the header of the documents without a session",
             browser=True),
    Mutation("batch-unprotected", "src/annotate/registry.py",
             "            reason = protection(item)\n",
             "            reason = \"\"\n",
             "a batch never unmanages a protected document"),
    Mutation("home-walked", "src/annotate/folders.py",
             '    paths = sorted({Path(i["path"]) for i in items if i.get("scan") is not False},\n',
             '    paths = sorted({Path(i["path"]) for i in items},\n',
             "the home directory is never walked (33 s a pass)"),
    Mutation("native-uses-windows", "src/annotate/browser.py",
             "    native = NATIVE.get(config.platform())\n    if native:\n",
             "    native = NATIVE.get(config.platform())\n    if False:\n",
             "Linux and macOS open the URL with their own opener"),
    Mutation("terminal-template-ignored", "src/annotate/terminal.py",
             "    if template:\n        if not any(",
             "    if False:\n        if not any(",
             "the configured terminal wins over the platform's"),
    Mutation("launcher-runs-in-place", "bin/annotate",
             '{**os.environ, "PYTHONPATH": str(HOME),',
             '{**os.environ, "PYTHONPATH": str(PACKAGE.parent),',
             "the plugin never runs the code from its version folder"),
    Mutation("stale-ignores-new-copy", "src/annotate/runtime.py",
             "    return current() / \"annotate\"\n",
             "    return code_dir\n",
             "a daemon on a copy turns stale when a new version is installed"),
    Mutation("tray-shortcut-through-link", "src/annotate/tray.py",
             "    return runtime.tray_script()\n",
             "    return runtime.stable(code_dir) / \"windows\" / \"tray.ps1\"\n",
             "the Startup shortcut names a plain file Windows can open"),
    Mutation("tray-script-never-refreshed", "src/annotate/runtime.py",
             "    if target.is_file() and target.read_bytes() == data:\n",
             "    if target.is_file():\n",
             "every install refreshes the icon's script"),
    Mutation('sync-allow-all', "src/annotate/sync.py",
             '    return any(place.is_relative_to(Path(f)) for f in settings["allowed"])\n',
             '    return True\n',
             'nothing leaves a folder the user did not allow'),
    Mutation('sync-takes-own-send', "src/annotate/sync.py",
             '            if not parsed or parsed[2] == settings["machine_id"] or item["name"] in seen:\n',
             '            if not parsed or item["name"] in seen:\n',
             'a machine never receives its own send'),
    Mutation('sync-refetches-a-refusal', "src/annotate/sync.py",
             '                log.error("sync: %s cannot be opened: %s", item["name"], exc)\n',
             '                continue\n',
             'a file that cannot be opened is reported once, never fetched again'),
    Mutation('sync-keeps-older-copies', "src/annotate/sync.py",
             '                target.delete(item["id"])\n',
             '                pass\n',
             'a newer send of a session replaces the older one in the store'),
    Mutation('sync-shows-secrets', "src/annotate/sync.py",
             '    shown = {k: v for k, v in settings.items() if k not in SECRET_KEYS}\n',
             '    shown = dict(settings)\n',
             'the page never sees a secret'),
    Mutation('sync-forgets-placement', "src/annotate/sync.py",
             '    if before and Path(before).is_dir():\n',
             '    if False:\n',
             'a session goes where the user placed it last time'),
    Mutation('sync-trusts-received-cwd', "src/annotate/sync.py",
             '        if transcript.stem in received:\n            continue\n',
             '        pass\n',
             'a received transcript is no evidence of where a clone lives'),
    Mutation('vault-skips-hmac', "src/annotate/vault.py",
             '    if not hmac.compare_digest(tag, expected):\n',
             '    if False:\n',
             'a wrong passphrase or a damaged file is refused before decrypting'),
    Mutation("autostart-everywhere", "src/annotate/cli.py",
             'AUTOSTART = frozenset({"register", "wait", "open", "send", "new-session", "resend", "sync"})',
             'AUTOSTART = frozenset({"register", "wait", "open", "send", "new-session", "resend", "sync", "status"})',
             "status reports and never starts a daemon"),
    Mutation("prune-current", "src/annotate/runtime.py",
             "    for old in [p for p in copies if p.name != keep][KEEP - 1:]:\n",
             "    for old in copies[KEEP - 1:]:\n",
             "the copy in use is never pruned"),
    Mutation("inbox-unchecked-protocol", "src/annotate/sender.py",
             "            if not inbox.verified(target):\n",
             "            if False:\n",
             "nothing is posted to a session on an unchecked peer protocol"),
    Mutation("resend-everything", "src/annotate/registry.py",
             '            if item.get("sent_at") == stamp:\n',
             '            if item.get("sent_at"):\n',
             "Resend puts back the last batch only"),
    Mutation("docpin-not-asked", "plugins/visual-first/hooks/check-document.py",
             "    answer = _docpin_says(path)\n",
             "    answer = None\n",
             "what is a document is docpin's answer when it is installed"),
    Mutation("prose-limit-off-by-one", "plugins/visual-first/hooks/check-document.py",
             "    if paragraphs > PROSE_PER_VISUAL * figures:\n",
             "    if paragraphs >= PROSE_PER_VISUAL * figures:\n",
             "four paragraphs per visual are fine, five warn"),
    Mutation("markdown-let-through", "plugins/visual-first/hooks/check-document.py",
             '    if path.suffix.lower() != ".html":\n        return "block"',
             '    if False:\n        return "block"',
             "a Markdown deliverable is blocked"),
    Mutation("stable-copy-not-tried", "plugins/visual-first/hooks/check-document.py",
             '    if (copy / "annotate" / "cli.py").is_file():\n',
             '    if False:\n',
             "the check finds docpin through its stable copy (no bin/ on a hook's PATH)"),
    Mutation("external-ignored", "plugins/visual-first/hooks/check-document.py",
             "    if external:\n",
             "    if False:\n",
             "a page that loads from the network is not self-contained"),
)


def stale_anchors(root: Path = ROOT) -> dict[str, int]:
    """The mutations whose anchor is not found exactly once under `root`.

    Checked before the campaign starts, and by tests/test_mutate.py in the
    default suite: applied one by one, a stale anchor would stop the campaign
    only when its turn came, an hour of runs later (2026-10-14).
    """
    counts = {m.name: (root / m.path).read_text(encoding="utf-8").count(m.old) for m in MUTATIONS}
    return {name: n for name, n in counts.items() if n != 1}


def copy_repo(target: Path) -> None:
    ignore = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache",
                                    ".mypy_cache", ".ruff_cache")
    shutil.copytree(ROOT, target, ignore=ignore)


def run_suite(copy: Path, browser: bool) -> tuple[str, str]:
    """Run the suite in `copy`; return (verdict, last line)."""
    # The anchor test fails on ANY mutated copy (its anchor is gone): it would
    # kill every mutation without testing anything.
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--ignore=tests/test_mutate.py"]
    if browser:
        argv += ["-m", "browser"]
    try:
        done = subprocess.run(argv, cwd=copy, capture_output=True, text=True,
                              timeout=TIMEOUT, env={**os.environ})
    except subprocess.TimeoutExpired:
        return "timeout", f"no answer within {TIMEOUT}s"
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    last = lines[-1] if lines else done.stderr.strip()[-200:]
    failed = [ln.split(" - ")[0].replace("FAILED ", "") for ln in lines
              if ln.startswith("FAILED ")]
    if failed:
        last = last + "\n      red: " + "\n      red: ".join(failed)
    return ("green" if done.returncode == 0 else "red"), last


def apply(copy: Path, mutation: Mutation) -> None:
    path = copy / mutation.path
    text = path.read_text(encoding="utf-8")
    if text.count(mutation.old) != 1:
        raise MutationError(f"{mutation.name}: anchor found {text.count(mutation.old)} "
                            f"times in {mutation.path}, expected exactly once")
    path.write_text(text.replace(mutation.old, mutation.new), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="*", help="mutations to run (default: all)")
    args = parser.parse_args(argv)
    chosen = [m for m in MUTATIONS if not args.names or m.name in args.names]
    if not chosen:
        print(f"no mutation named {args.names}; known: {[m.name for m in MUTATIONS]}")
        return 2
    stale = {name: n for name, n in stale_anchors().items() if name in {m.name for m in chosen}}
    if stale:
        print(f"anchors not found exactly once (name: count): {stale}")
        return 2
    survivors = 0
    for browser in sorted({m.browser for m in chosen}):
        with tempfile.TemporaryDirectory(prefix="annotate-mutate-") as tmp:
            base = Path(tmp) / "baseline"
            copy_repo(base)
            verdict, last = run_suite(base, browser)
            label = "browser" if browser else "default"
            print(f"baseline ({label} suite, unmutated copy): {verdict} | {last}")
            if verdict != "green":
                print("the unmutated copy is not green: no mutation result would mean anything")
                return 2
        for mutation in (m for m in chosen if m.browser == browser):
            with tempfile.TemporaryDirectory(prefix="annotate-mutate-") as tmp:
                copy = Path(tmp) / "copy"
                copy_repo(copy)
                apply(copy, mutation)
                verdict, last = run_suite(copy, browser)
            killed = verdict in ("red", "timeout")
            survivors += 0 if killed else 1
            print(f"{'KILLED ' if killed else 'SURVIVED'} {mutation.name:<14} "
                  f"({mutation.breaks}) | {last}")
    print(f"{len(chosen)} mutation(s), {survivors} survivor(s)")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
