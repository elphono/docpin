# Roadmap

What is shipped, what is next, and what is deliberately not on the list. Items marked **(design
done)** have a written design in [docs/etude-reutilisation.html](docs/etude-reutilisation.html);
items marked **(proposal)** are ideas, not commitments.

## Shipped — v0.4

- **Carry a conversation to your other machine** (`sync.py`): **Send** seals the transcript on this
  machine (compressed, AES-256 through `openssl`, HMAC-checked) and puts it in your Google Drive's hidden
  application folder, or in a folder another program syncs; the other machine's daemon looks every
  minute, puts the conversation where `claude --resume` finds it, in its own clone of the same
  repository (found by its remote), and offers **Resume here**. Off by default; only the folders you
  allow may send. The route was measured first, on 2026-10-12: a transcript copied into the other
  machine's project folder resumes there intact, works in the new folder, and the trip back works.

## Shipped — v0.3

- **A Claude Code plugin, and its own marketplace**: `/plugin install docpin --marketplace
  elphono/docpin`. The plugin runs a stable copy of the code (never the version folder Claude Code
  deletes 14 days after an update), starts the daemon on demand, and puts `annotate` on the sessions'
  PATH. Installing, updating and the on-demand start were measured in an empty home directory.
- **The annotatable-documents skill**: what a session writes, where, how it hands it over and answers
  the notes. Its eval runs a session with and without the plugin (`claude plugin eval .`).
- **visual-first**, an optional second plugin: an output style that draws before it writes, and a
  check that sends a session back to a Markdown document, a page without a visual, or a page that
  loads from the network. What is a document is docpin's answer (`annotate is-document`). On
  2026-10-23 it moved out of docpin, into [eli](https://github.com/elphono/eli), which still asks
  `annotate is-document`.
- **Native Linux and macOS** (macOS untested): the browser through `xdg-open` / `open`, the terminal
  tab through the first desktop terminal found or a `terminal` template in `config.toml`.
- **The inbox route is guarded**: notes are posted only to a session whose peer protocol is one the
  line format was captured under (2.1.289 and 2.1.296, protocol 1); **Resend** puts the last batch
  back if it never arrived.

## Shipped — v0.1 and v0.2

- Hook on `Write|Edit` that registers a session's HTML documents with the session's id, and a daemon
  scan (every 30 s) for what Bash writes.
- Tracked folders chosen from the home page (any folder under `~`, every `.html` under it), and a
  rescan that catches up the files of the last N days without reviving unmanaged documents.
- Annotation layer injected by the daemon: Alt+click pins a numbered note, anchored by CSS selector +
  quote + offset, so it survives corrections. An “orphans” bar keeps notes whose anchor disappeared.
- Three delivery routes: the session that waits (`annotate wait`), the open session's inbox, a
  Windows Terminal tab that resumes the session. Never a background session.
- Home page and Windows tray icon, with per-document and per-daemon controls.
- What changed since the last send: every send keeps a copy of the document (outside the
  repository); the page compares it block by block with the file, marks what is new, changed or
  removed, folds the sections already read, and offers a navigator and **Mark as read**.
- Attach a document to any resumable conversation of the machine from the home page (open sessions
  first, filter as you type), or detach it; `annotate sessions` / `attach` / `detach` do the same.
- Local-only HTTP API, hardened against pages that merely target `localhost`.
- Test suite with guards that keep it from touching the real system, mutation harness, `ruff` and
  `mypy` clean, no runtime dependency.

## Next

1. **Carry a session in the same state** **(proposal, priority)**. v0.4 carries the conversation;
   what still stays behind is what runs (an `annotate wait`, a dev server), the uncommitted files, and
   docpin's own state for the session's documents (notes, references). Next: send the documents' state
   with the conversation, and offer to commit and push before sending. A public server, if there is an
   audience, keeps the same sealed format: the server only holds bytes it cannot read.
2. **First real run from Windows Chrome / Edge.** Alt+click has only been measured in headless
   Chromium on Linux, and Alt alone can give the focus to the browser menu on Windows. The tray icon
   and its menu have not been looked at on a screen either. Fix whatever this shows.
3. **More than pins: highlight, circle, strike-through** **(design done)**. Each annotation gets a
   `kind`; text kinds anchor on an exact quote with a 32-character prefix and suffix (the Web
   Annotation `TextQuoteSelector` idea), which survives block reordering where a selector does not.
   The tool is chosen in the bar, as in a screenshot tool — no gesture recognition. The prompt changes
   by one word per note.
4. **Watch the inbox route.** Posting into an open session's inbox relies on a line format that Claude
   Code does not document (captured on real messages, frozen by `tests/test_inbox.py`). Re-capture
   it when `peerProtocol` changes, and switch to a documented interface if one appears.
5. **A render check for documents** **(proposal)**: light, dark and phone-width captures of a document,
   its horizontal overflow and the smallest text of its figures, run by the skill instead of looked at
   by hand. Needs Playwright, declared by the plugin.
6. **PyPI.** For whoever does not use Claude Code plugins: `uvx` / `pipx` without a clone.

## Later

- **A menu-bar icon** on Linux and macOS **(proposal)**, as the tray icon does on Windows. Today
  `annotate open` opens the home page there.
- **A browser extension** **(design done, deferred)**, only for pages the daemon does not serve (an
  external site, a pull request). It would reuse the overlay script and the daemon API as they are.
  Not worth it until that need is real.
- **Other document kinds** **(proposal)**: Markdown rendered by the daemon, so that a session that
  writes `.md` can be annotated the same way.

## Not planned, on purpose

- **A background session answering the notes.** The first real send did exactly that: 2 min 45 s,
  $1.71, a correct fix — and nothing visible to the person waiting at the open session. Notes go to the
  conversation you can see, or to a visible tab.
- **Gesture recognition** (a drawn symbol that means “done”). A button cannot be misread.
- **Notes stored next to the document.** The document lives in a repository; the notes do not.
