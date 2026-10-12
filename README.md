# docpin

**React to the documents Claude Code writes. Pin a note on any passage, right in your browser — the notes
flow back to the session that wrote the document, and it fixes it.**

![How docpin works: a Claude Code session writes a document, the daemon serves it to your browser, and your notes travel back to the same conversation](docs/figures/flow.svg)

A long answer from Claude Code is hard to read in a terminal and awkward to answer. docpin gives every
HTML document the session writes a page in your browser, and a way to react to it in place.

A Claude Code session writes `docs/report.html`. You open it, Alt+click the sentence that is wrong,
type what you want instead, click **Send to session**. The session that wrote the document receives
your notes — each one carries the passage it points at — corrects the file on disk, and waits for the
next round. No copy-pasting of quotes into the terminal, no “which paragraph did I mean?”.

- **The notes go to the conversation that wrote the document**, not to a fresh one. If that session is
  still open, nothing else opens; if it is gone, a terminal tab resumes it.
- **Nothing to install in the browser.** The daemon serves your document with an annotation layer
  injected. Any browser works.
- **No runtime dependency.** The daemon is `http.server`, the configuration is `tomllib`, the registry
  is `json`. Python 3.12+ and nothing else.
- **Your repository stays clean.** Notes live under `~/.local/share/annotate`, never next to the
  document.
- **A note never gets lost.** If you edit the document and a note's anchor no longer resolves, the note
  moves to an “orphans” list and is still sent, with the passage it quoted.

## Status

v0.3, used daily by its author. The core loop (hook, daemon, overlay, the three delivery routes) is
covered by a test suite that never launches a real `claude`, `systemctl`, `wt.exe` or signal (guards
fail the test instead), and its central properties are checked by mutation. Installing as a plugin
from this repository, updating it, and the daemon it starts on demand were measured on 2026-10-12 in
an empty home directory. Not yet measured: Alt+click in a real Windows Chrome (it is tested in
headless Chromium on Linux), native Linux and macOS desktops, and the look of the tray icon. See
[ROADMAP.md](ROADMAP.md).

## Requirements

| | Needed for | Notes |
|---|---|---|
| [Claude Code](https://claude.com/claude-code) 2.1.275 or later | everything | the hook and `annotate wait` run inside its sessions |
| Python ≥ 3.12 (`python3`) | everything | no package is installed: the plugin runs the code with the system's `python3` |
| a terminal: Windows Terminal on WSL, any desktop terminal on Linux | route C: resuming a session that is gone | or set `terminal = ["kitty", "bash", "-l", "{script}"]` in `~/.config/annotate/config.toml` |
| systemd user services | the daemon as a service | optional: the plugin starts the daemon on demand |
| Windows | the tray icon | optional |

| Platform | State |
|---|---|
| WSL2 + Windows | developed and used daily |
| native Linux | supported: `xdg-open`, the first desktop terminal found; not tried on a desktop yet |
| macOS | written from the documentation (`open`, Terminal.app through `osascript`), not tested |

## Install

**As a Claude Code plugin** (recommended). In a session:

```text
/plugin install docpin --marketplace elphono/docpin
```

or, in two steps, `/plugin marketplace add elphono/docpin` then `/plugin install docpin@docpin`.
Start a **new** session: hooks load when a session starts. That is all. The first document a session
writes starts the daemon on `127.0.0.1:8765`, and `annotate` is on the PATH of your sessions
(`! annotate open` opens the home page).

The plugin brings:

- the hook that registers the documents a session writes, and tells it how to get your notes back;
- the skill **annotatable-documents**, which tells a session what to write, where, and how to answer
  your notes;
- the `annotate` command. It never runs from the plugin's own folder, which Claude Code deletes 14 days
  after an update: it copies the code once per version under `~/.local/share/annotate/runtime/` and
  runs that copy. After an update, the page offers **Restart daemon**.

Optional, from the same marketplace: **visual-first** (`/plugin install visual-first@docpin`), an
output style that draws before it writes (pick it in `/output-style`, it is never imposed) and a check
that sends a session back to a document that is Markdown, has no visual, or loads from the network.

To keep the daemon running across logins instead of on demand:

```bash
annotate service                              # writes ~/.config/systemd/user/annotate.service
loginctl enable-linger $USER
systemctl --user daemon-reload
systemctl --user enable --now annotate.service
annotate tray --start                         # optional, Windows: tray icon + Startup shortcut
```

**By hand, from a clone** (to work on docpin itself):

```bash
git clone https://github.com/elphono/docpin.git && cd docpin && uv sync
claude --plugin-dir .                         # this session loads the clone as the plugin
```

`--plugin-dir` replaces an installed docpin for that session, without registering anything twice.

**Tracked folders.** Out of the box: the `docs/` folders of your workspace (`~/workspace` unless you
set `ANNOTATE_WORKSPACE`), or, without a workspace, every `docs/` folder under your home directory,
found when a session writes there (your home directory is never walked: it is far too big). Add any
folder under your home directory from the home page (or `annotate folders add <folder>`): every
`.html` file under it is tracked, except in `build/`, `node_modules/`, `figures/` and the like. The
daemon also walks the tracked folders every 30 seconds, which catches documents written by Bash
(`cp` from a scratchpad, subagents) that never go through the hook. Adding a folder takes its files of
the last days; **Rescan all folders** does the same everywhere, for 1, 7, 30 or 90 days. A document you
unmanaged stays out until it is written again.

## Use

1. In a Claude Code session, ask for an HTML document in a tracked folder. The hook registers it and
   tells the session to run `annotate wait <id>` in the background.
2. Open the home page (`http://127.0.0.1:8765/`, or a left click on the tray icon), then the document.
3. **Alt+click** anywhere to pin a note. Select text first and the note quotes exactly that selection.
4. **Send to session** (the bar, the home page, the tray, or `annotate send <id>`). Only notes that were
   never sent go out.
5. When the session has corrected the document, reopen it: what changed since your send is marked
   (new, changed, removed), sections you already read fold into one line, and ▲ ▼ walk the changes.
   **Mark as read** makes the version on screen the new reference.

The home page groups documents by session — its title, then its folder — and the sessions by the
git repository they run in; it carries every control:
Open, Send to session, New session, Unmanage, Delete file; the tracked folders and the rescan; and
Restart / Stop for the daemon. **Attach to session…** ties a document to any conversation of the
machine that `claude --resume` can find (open ones first, filter as you type); **Detach** unties it.

```bash
annotate list                        # one line per document
annotate open [<id>]                 # a document, or the home page
annotate send <id>                   # same as the button
annotate resend <id>                 # the last batch again, if it never reached the session
annotate new-session <id>            # the unsent notes go to a NEW session (a terminal tab)
annotate forget <id> [--delete]
annotate sessions                    # the conversations a document can be attached to
annotate attach <id> <session>       # or: annotate detach <id>
annotate folders [add|remove <dir>]  # the tracked folders
annotate rescan [--days 7] [--folder F]  # register what was written recently
annotate status
```

Configuration is optional: `~/.config/annotate/config.toml` (`port`, `claude_bin`, `terminal`), overridden by
`ANNOTATE_PORT` and `ANNOTATE_CLAUDE`. Data lives under `ANNOTATE_DATA_DIR`
(default `~/.local/share/annotate`).

## Security

The daemon listens on `127.0.0.1` only, and `send` starts a session that is allowed to edit files, so a
web page you merely visit must not be able to trigger it: every request that changes anything needs the
custom `X-Annotate` header (which forces a CORS preflight the daemon never grants), a local `Origin`,
and a `Host` that names localhost (DNS rebinding). `/docs/<id>/files/` serves nothing outside the
document's folder, symbolic links included.

## Develop

```bash
uv run pytest -q                          # default suite
uv run ruff check src tests tools hooks bin/annotate plugins
uv run mypy
uv run python tools/mutate.py             # directed mutation campaign, run on a copy of the repo
claude plugin validate --json . > report.json && python3 tools/check_plugin_report.py report.json
claude plugin eval . --runs 1 --allow-tools Write Edit   # the skill, with and without the plugin
```

The invariants and the lessons behind them are in [CLAUDE.md](CLAUDE.md) (in French). The design
study that fixed the main choices is [docs/etude-reutilisation.html](docs/etude-reutilisation.html)
(French; GitHub shows HTML as source, so open it from a clone).

## License

[MIT](LICENSE).

## Name

**docpin**: pin a note on a doc. The command line is still `annotate`, and so is the Python package, so that nothing you have set up breaks.
