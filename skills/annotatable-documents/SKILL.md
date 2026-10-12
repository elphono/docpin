---
name: annotatable-documents
description: Use when writing a document for the user to read and review rather than code - a spec, design note, plan, report, comparison, procedure or summary - and when the user's docpin notes on such a document come back to the session. Covers where to write it, its form, giving the link, waiting for notes, and answering them.
---

# Annotatable documents

docpin lets the user pin notes on an HTML document in their browser and send them back to
**this** conversation. It works on its own once the document exists in the right place;
this skill is about producing documents that are worth annotating, and closing the loop.

## Write it

- **One self-contained `.html` file** under the project's `docs/` folder
  (`docs/<yyyy-mm-dd>-<topic>.html`), not Markdown: the user reads it in a browser and
  pins notes on its blocks. Inline the CSS; no build step, no external script it cannot
  live without. Images it uses sit next to it, referenced by relative path.
- **Show before telling.** A mechanism, a flow or an architecture becomes an inline SVG
  diagram (not Mermaid: it needs a library the page does not load); a comparison becomes
  a table; a number that matters becomes a tile. Prose keeps what does not draw: a
  trade-off, a doubt, a decision to take.
- **Readable in light and dark**: colours as CSS variables on `:root`, redefined under
  `@media (prefers-color-scheme: dark)`, and an explicit `body` background. Check it at
  phone width: no horizontal scroll.
- **Layered when it will be read again**: a reference document (a spec, a procedure) is
  nested, so the reader picks a depth and never wades through detail to reach the end:
  the claim, then the reasoning, then the proof (commands, outputs, excerpts). A level
  selector at the top, or `<details>` blocks. A one-off note does not need it.
- **Blocks the user can point at**: short paragraphs, list items, table rows and headings.
  A note anchors on the block it was pinned to; a wall of text gets one vague note.
- End with what the user has to decide or do, if anything.

## Hand it over

After the `Write`, docpin's hook answers with the document's address and a command such as
`annotate wait <id>`.

1. Start that command **once, in the background** (`run_in_background`), and do not wait
   for it. It ends when the user clicks "Send to session"; its output is their notes.
2. Give the user the link (`http://127.0.0.1:<port>/docs/<id>`) in your reply, with one
   line on what to look at first.
3. If the hook said nothing, the file is outside the folders docpin tracks: say so and
   give the path, or ask the user to add the folder from docpin's home page
   (`annotate open`).

## When the notes come back

The background command's output lists each note with its number, the text it was pinned
on, and what the user wrote.

- **Answer every note**, by number, in your reply: done, done differently (and why), or
  not done (and why). A note left unanswered reads as ignored.
- **Edit the document in place** (`Edit`, not a full rewrite): notes and docpin's
  "what changed since the last send" view both anchor on the blocks that stay. Keep
  headings and the order of sections unless a note asks to change them.
- A note whose anchor is reported **lost** was on a block that no longer exists: it still
  carries its quote, so handle it from that.
- Then start `annotate wait <id>` in the background again: the user may annotate the new
  version.
