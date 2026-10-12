# docpin

**React to the documents Claude Code writes. Pin a note on any passage, right in your browser — the notes
flow back to the session that wrote the document, and it fixes it.**

![How docpin works: a Claude Code session writes a document, the daemon serves it to your browser, and your notes travel back to the same conversation](docs/figures/flow.svg)

A long answer from Claude Code is hard to read in a terminal and awkward to answer. With docpin, the
session writes a document instead; you read it in your browser, Alt+click the sentence that is wrong,
type what you want, and click **Send to session**. The session that wrote the document gets your
notes, each with the passage it points at, corrects the document, and waits for the next round.

- **Your notes go back to the same conversation**, not to a new one.
- **Nothing to install in the browser**, any browser works.
- **Your projects stay clean**: your notes are kept on your machine, never next to the document.
- **After a correction, the page shows what changed** since you sent your notes, and folds what you
  already read.

## What you get

| | What it does | Needed? |
|---|---|---|
| **docpin** | the loop above: your sessions write documents you can annotate, and your notes come back to them | yes |
| **visual-first** | sessions draw diagrams and tables instead of walls of text, and every document they write is checked: a session that writes plain text where a page with pictures was expected is sent back to it | optional, recommended |
| [epic-infographics](https://github.com/OrRon/EpicInfographics) | richer poster-like figures, as images | optional, another author's plugin: `/plugin install epic-infographics --marketplace OrRon/EpicInfographics` |

That is exactly the author's setup, minus his own preferences (he writes in French).

## Where it works

| Your machine | Works? |
|---|---|
| Windows, with Claude Code running in WSL2 | ✓ developed and used every day |
| Linux | ✓ should work: built for it, not yet tried on a desktop |
| macOS | ⚠ written for it, never tested |
| Windows without WSL | ✗ not supported |

## Before you start

- **Claude Code** 2.1.275 or later.
- **Python 3.12 or newer**, available as `python3`. Ubuntu 24.04 and recent distributions have it; on
  Ubuntu 22.04 or macOS, install a newer Python first. docpin installs no Python package.
- A browser.

## Install

1. In a Claude Code session, type:

   ```text
   /plugin install docpin --marketplace elphono/docpin
   ```

2. Optional, recommended: `/plugin install visual-first@docpin`. To also make your sessions answer in
   its style, pick **visual-first** in `/output-style`.
3. **Start a new session.** That is all: there is nothing to configure.

To try it, ask that new session for something to review, for instance *"write up how our login
works, as a document I can comment on"*. It answers with a link: open it, Alt+click a sentence, write
a note, click **Send to session**. The session receives the note and fixes the document.

The first time, Claude Code asks your permission to run `annotate wait`: that is the session waiting
for your notes. Allow it.

## What it puts on your machine

| What | Where |
|---|---|
| the two plugins | in Claude Code's own plugin folder, like any plugin |
| your notes, and a copy of docpin's code | `~/.local/share/annotate` |
| docpin's daemon: the small background program that serves the pages and carries your notes | **included in the docpin plugin**, nothing else to install; it only answers your own machine (`http://127.0.0.1:8765`) |

Nothing is written into your projects, nothing is installed system-wide, nothing goes to the network.

**When the daemon runs.** It is not started with Claude Code: the first time a session writes a
document, docpin sees that no daemon answers and starts it in the background. It then keeps running
until the machine (or WSL) restarts, and the next document starts it again. The only thing it does
not do on its own is start at login, so that the home page answers before any session wrote anything.

### Optional: start the daemon at login

This uses systemd, which recent Linux distributions and WSL Ubuntu have switched on (not macOS).

1. In a Claude Code session, type `! annotate service`. (`annotate` is on the PATH of Claude Code's
   sessions, not of an ordinary terminal; the `!` runs it from the session.) It writes the service file.
2. In any terminal:

   ```bash
   loginctl enable-linger $USER          # keep it running when no terminal is open
   systemctl --user daemon-reload
   systemctl --user enable annotate.service
   ```

3. The service takes over at the next start of your machine (on Windows: the next start of WSL, for
   instance after `wsl --shutdown`). If docpin has not started its daemon yet, start the service right
   away instead: `systemctl --user start annotate.service`. Do not start it while docpin's own daemon
   runs: both would want the same port.

To check: `systemctl --user status annotate.service`. To undo:
`systemctl --user disable --now annotate.service`.

### Other extras

Type them in a Claude Code session:

| You want | Type |
|---|---|
| the list of your documents | `! annotate open`, or open `http://127.0.0.1:8765/` |
| an icon in the Windows notification area | `! annotate tray --start` |

## Every day

- Ask a session for a document (a spec, a report, a plan to review). It gives you the link.
- **Alt+click** to pin a note; select text first to quote exactly that.
- **Send to session**. If the session is still open, the notes arrive there (Claude Code may show an
  *Approve / Deny* dialog first); if you closed it, a terminal tab reopens it.
- Reopen the document after the correction: the changes are marked. **Mark as read** when you are done.

**Which documents?** Out of the box, the HTML files a session writes in a `docs/` folder. To follow
another folder, add it on the home page (`! annotate open` in a session). Documents also show up when
a script or a helper agent wrote them, within 30 seconds.

## Update, uninstall

In a terminal:

- Update: `claude plugin update docpin@docpin` (and `visual-first@docpin`), then start a new session.
  If the home page says **Restart daemon**, click it.
- Uninstall: `claude plugin uninstall docpin@docpin` (and `visual-first@docpin`). Your notes stay in
  `~/.local/share/annotate`; delete that folder to remove them too.

## Status

v0.3, used every day by its author. Installing from this page, updating, and the whole loop up to a
note sent from Chrome on Windows were checked on 2026-10-12. Not yet checked: a Linux or macOS
desktop, and the tray icon on screen. What comes next is in [ROADMAP.md](ROADMAP.md).

## For the curious

Configuration is optional: `~/.config/annotate/config.toml` takes `port`, `claude_bin` (how to start
Claude Code) and `terminal` (which terminal reopens a closed session, for instance
`["kitty", "bash", "-l", "{script}"]`). The command line does what the pages do; type each command in a
Claude Code session with a leading `!`:

```bash
annotate list                        # one line per document
annotate open [<id>]                 # a document, or the home page
annotate send <id>                   # same as the button
annotate resend <id>                 # the last batch again, if it never reached the session
annotate new-session <id>            # the unsent notes go to a NEW session
annotate forget <id> [--delete]
annotate attach <id> <session>       # tie a document to another conversation (or: detach)
annotate folders [add|remove <dir>]  # the followed folders
annotate rescan [--days 7]           # take what was written recently
annotate status
```

## Security

The daemon listens on `127.0.0.1` only, and `send` starts a session that is allowed to edit files, so a
web page you merely visit must not be able to trigger it: every request that changes anything needs the
custom `X-Annotate` header (which forces a CORS preflight the daemon never grants), a local `Origin`,
and a `Host` that names localhost (DNS rebinding). `/docs/<id>/files/` serves nothing outside the
document's folder, symbolic links included.

## Develop

From a clone (`git clone https://github.com/elphono/docpin.git && cd docpin && uv sync`),
`claude --plugin-dir .` starts a session that uses the clone instead of the installed plugin.

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
