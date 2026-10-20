"""The overlay in a real browser (headless Chromium through Playwright).

Excluded from the default run, like remarkable-sync's `device` tests: it needs
Node and a Playwright install the repository does not ship. Run it with

    ANNOTATE_PLAYWRIGHT_DIR=<dir whose node_modules holds playwright> \\
        uv run pytest -m browser -v

Without that variable, it FAILS rather than skips: a browser run that skips
leaves a green line having seen nothing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from annotate import config, registry, sender, server
from fakes import RecordingOpener

SCRIPT = Path(__file__).resolve().parent / "browser" / "overlay_check.mjs"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
       b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01"
       b"\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.mark.browser
def test_overlay_in_a_real_browser(tmp_path, monkeypatch, make_doc):
    pw_dir = os.environ.get("ANNOTATE_PLAYWRIGHT_DIR", "")
    node = shutil.which("node")
    assert pw_dir and node, ("set ANNOTATE_PLAYWRIGHT_DIR to a directory whose "
                             "node_modules holds playwright, and put node on the PATH")
    path = make_doc(
        "<h1>Overlay check</h1><nav><a href='#second'>to the second part</a></nav>"
        "<div id='body-text'><p>First paragraph, a long enough line of text.</p>"
        "<p>Second paragraph, which the test annotates with Alt+click.</p></div>"
        "<img src='figures/dot.png' alt='dot'>"
        "<svg id='fig' width='400' height='120' viewBox='0 0 400 120'>"
        "<rect x='10' y='10' width='120' height='60' fill='#ddd'/>"
        "<text x='20' y='45'>Session</text>"
        "<rect x='250' y='10' width='120' height='60' fill='#ddd'/>"
        "<text x='260' y='45'>Browser</text></svg>"
        "<div style='height:1200px'></div><h2 id='second'>Second part</h2><p>End.</p>",
        title="Overlay check")
    (path.parent / "figures").mkdir()
    (path.parent / "figures" / "dot.png").write_bytes(PNG)
    doc_id = registry.register(path, session="sess-browser")["id"]
    registry.replace_annotations(doc_id, [{
        "id": "orphan1", "number": 1, "selector": "#deleted-section > p:nth-of-type(3)",
        "quote": "A sentence that was deleted", "note": "Keep this idea somewhere",
        "offset_x": 0, "offset_y": 0}])

    cfg = config.load_config()
    opener = RecordingOpener()
    srv = server.make_server(cfg, port=0, send=sender.Sender(cfg, opener=opener))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # An open session listens, as `annotate wait` would make it.
    box: dict[str, object] = {}
    listener = threading.Thread(target=lambda: box.update(
        waiter=srv.sender.board.wait(doc_id, "sess-open", "/", lambda: True, 110)),
        daemon=True)
    listener.start()
    out = Path(os.environ.get("ANNOTATE_BROWSER_OUT", tmp_path))
    out.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run([node, str(SCRIPT), pw_dir, f"http://localhost:{srv.port}",
                               doc_id, str(out), str(path)],
                              capture_output=True, text=True, timeout=120)
    finally:
        srv.shutdown()
        srv.server_close()
    print(done.stdout)
    assert done.returncode == 0, done.stdout + done.stderr

    listener.join(5)
    items = {a["id"]: a for a in registry.list_annotations(doc_id)}
    assert items["orphan1"]["note"] == "Keep this idea somewhere"   # never dropped
    by_number = {a["number"]: a for a in items.values()}
    para, fig = by_number[2], by_number[3]
    assert para["selector"] == "#body-text > p:nth-of-type(2)"
    assert para["quote"].startswith("Second paragraph, which the test annotates")
    assert para["note"] == "Rephrase this, it is unclear." and para["sent_at"]
    # The words under the cursor, not the whole paragraph (user note 2, 2026-10-05).
    assert para["at"].startswith("[[Second]] paragraph, which the test")
    assert para["at"].endswith(" …") and len(para["at"]) < 100
    assert fig["selector"] == "#fig" and fig["at"] == 'in the figure, next to the label "Browser"'
    prompt = box["waiter"].prompt  # type: ignore[union-attr]
    assert "Rephrase this, it is unclear." in prompt and "[[Second]]" in prompt
    assert "Keep this idea somewhere" in prompt and "Anchor: lost" in prompt
    assert "Section: Overlay check" in prompt
    assert opener.calls == [], "a session listened: no terminal tab"


INDEX_SCRIPT = Path(__file__).resolve().parent / "browser" / "index_check.mjs"
CHANGES_SCRIPT = Path(__file__).resolve().parent / "browser" / "changes_check.mjs"


@pytest.mark.browser
def test_the_index_page_offers_every_tray_control(tmp_path, monkeypatch, make_doc):
    import time

    from annotate import claude
    from fakes import compact, conversation, register_session, transcript
    pw_dir = os.environ.get("ANNOTATE_PLAYWRIGHT_DIR", "")
    node = shutil.which("node")
    assert pw_dir and node, "see test_overlay_in_a_real_browser"
    # The conversations of this machine: two titled ones, the first OPEN, an
    # untitled one; and two the picker must not offer (a subagent's, and a
    # transcript `claude --resume` would refuse).
    now, projects, ws = time.time(), claude.projects_dir(), tmp_path / "workspace"
    conversation(projects, "s-live", cwd=str(ws / "repo"), title="Live session", when=now - 120)
    conversation(projects, "s-other", cwd=str(ws / "other"), title="Other session",
                 when=now - 3 * 3600)
    (ws / "other").mkdir(parents=True)                # a folder no git repository holds
    conversation(projects, "s-untitled", cwd=str(ws / "third"), when=now - 2 * 86400)
    conversation(projects, "agent-sub1", folder="-x/s-live/subagents", title="A subagent",
                 when=now)
    transcript(projects, "s-title-only", lines=(
        compact({"type": "ai-title", "aiTitle": "Never resumable"}),))
    (tmp_path / "live.sock").write_text("")
    register_session("s-live", tmp_path / "live.sock")
    alpha = registry.register(make_doc("<p>a</p>", name="a.html", title="Alpha"),
                              session="s-live")["id"]
    bravo_path = make_doc("<p>b</p>", name="b.html", title="Bravo")
    bravo = registry.register(bravo_path, session="")["id"]
    charlie_path = make_doc("<p>c</p>", name="c.html", title="Charlie")
    charlie = registry.register(charlie_path, session="s-live")["id"]
    echo_path = make_doc("<p>e</p>", name="e.html", title="Echo")
    os.utime(echo_path, (now - 40 * 86400,) * 2)      # old, but delivered: a rescan keeps it
    echo = registry.register(echo_path, session="s-other", cwd=ws / "other")["id"]
    registry.replace_annotations(echo, [{"id": "e1", "selector": "body", "quote": "e",
                                         "note": "already sent"}])
    registry.mark_sent(echo, ["e1"], registry.now_iso())
    registry.update(echo, status="delivered")
    foxtrot_path = make_doc("<p>f</p>", name="f.html", title="Foxtrot")
    (ws / "repo" / "sub").mkdir(parents=True)        # a second session of the repository `repo`
    foxtrot = registry.register(foxtrot_path, session="s-untitled", cwd=ws / "repo" / "sub")["id"]
    foxtrot_path.unlink()
    juliet_path = make_doc("<p>j</p>", name="j.html", title="Juliet")   # 40 days old
    os.utime(juliet_path, (now - 40 * 86400,) * 2)
    juliet = registry.register(juliet_path, session="s-other", cwd=ws / "other")["id"]
    # Without a session: Golf, which "Cleanup files" takes, and Hotel, delivered
    # (the session is on it), which it keeps.
    golf_path = make_doc("<p>g</p>", name="g.html", title="Golf")
    golf = registry.register(golf_path, session="")["id"]
    hotel = registry.register(make_doc("<p>h</p>", name="h.html", title="Hotel"),
                              session="")["id"]
    registry.update(hotel, status="delivered")
    registry.replace_annotations(alpha, [{"id": "n1", "selector": "body", "quote": "a",
                                          "note": "from the index page"}])
    reports = tmp_path / "pick" / "reports"          # no docs/ on the way
    reports.mkdir(parents=True)
    (reports / "delta.html").write_text("<title>Delta</title><p>d</p>")
    asked: list[str] = []
    monkeypatch.setattr(server.service, "control", asked.append)
    cfg = config.load_config()
    opener = RecordingOpener()
    srv = server.make_server(cfg, port=0, send=sender.Sender(cfg, opener=opener))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    box: dict[str, object] = {}
    listener = threading.Thread(target=lambda: box.update(
        waiter=srv.sender.board.wait(alpha, "s-live", "/", lambda: True, 110)), daemon=True)
    listener.start()
    out = Path(os.environ.get("ANNOTATE_BROWSER_OUT", tmp_path))
    out.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run([node, str(INDEX_SCRIPT), pw_dir, f"http://localhost:{srv.port}",
                               str(out), str(reports.resolve())],
                              capture_output=True, text=True, timeout=120)
    finally:
        srv.shutdown()
        srv.server_close()
    print(done.stdout)
    assert done.returncode == 0, done.stdout + done.stderr
    listener.join(5)
    assert "from the index page" in box["waiter"].prompt  # type: ignore[union-attr]
    assert opener.calls == []
    from annotate import folders
    delta = (reports / "delta.html").resolve()
    # Foxtrot went with its session's cleanup; its file was missing: not indexed.
    assert set(registry.all_docs()) == {alpha, echo, hotel} and golf and foxtrot
    assert golf_path.exists() and set(registry.cleanup_index()) == {str(golf_path)}
    assert registry.dismissed(juliet_path, juliet_path.stat().st_mtime) and juliet  # rescan
    assert registry.dismissed(delta, delta.stat().st_mtime)     # unmanaged by the exclusion
    assert [f["path"] for f in folders.load()] == [str((tmp_path / "workspace").resolve())]
    assert folders.load_excluded() == []                        # and the exclusion removed
    assert bravo_path.exists() and not charlie_path.exists()
    assert bravo and charlie and asked == ["restart", "stop"]


CHANGES_A = (
    "<h1>Changes check</h1><p>Intro paragraph stays. <a href='#ctx-two'>to context two</a></p>"
    "<h2 id='s1'>Context</h2><p>Context one.</p><p id='ctx-two'>Context two, already read.</p>"
    "<h2 id='s2'>Options</h2><p>Three options were compared.</p>"
    "<p>The migration takes a week, measured on the test copy.</p>"
    "<p>Option C needs one more server.</p>"
    "<h2 id='s3'>Notes</h2><p>A paragraph with an unsent note.</p>"
    "<h2 id='s4'>Decision</h2><p>Decision text.</p>"
    "<p id='clock'>Clock reads ten past nine</p>"
    "<script>document.getElementById('clock').textContent = 'Set by a script'</script>")
# B keeps eight <p> at the same ranks as A for the unsent note: "fourth
# option" takes rank 5, the migration rank 6, and "Option C" goes away.
CHANGES_B = CHANGES_A.replace(
    "<p>The migration takes a week, measured on the test copy.</p>"
    "<p>Option C needs one more server.</p>",
    "<p>A fourth option keeps the old format read only for a month.</p>"
    "<p>The migration takes two days, measured on the test copy.</p>")

# Blocks inside blocks (2026-10-14: "ugly and too busy"). Cards laid out like
# the document of that day: padded, rounded, in a grid, light and dark.
NESTED_STYLE = (
    "<style>:root{--paper:#fff;--ink:#1f2937;--card:#f8fafc;--line:#d8dee6;--muted:#5b6472}"
    "@media (prefers-color-scheme: dark){:root{--paper:#0f1115;--ink:#e5e7eb;"
    "--card:#1b1f27;--line:#2c3340;--muted:#9ca3af}}"
    "body{margin:0;background:var(--paper);color:var(--ink);"
    "font:17px/1.5 system-ui,sans-serif}"
    "main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}"
    "h2{font-size:22px;margin:40px 0 12px}"
    ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}"
    ".card,.box{background:var(--card);border:1px solid var(--line);border-radius:16px;"
    "padding:14px 16px 16px}"
    ".card header{display:flex;align-items:center;gap:10px}.card h3{margin:0;font-size:19px}"
    ".num{flex:none;width:34px;height:34px;border-radius:50%;background:#0e7490;color:#fff;"
    "font-weight:700;display:grid;place-items:center}.dot{width:28px;height:28px}"
    ".card p{margin:6px 0 0}.card p+p{font-size:14.5px;color:var(--muted)}"
    ".box h3{margin:0 0 6px}table{border-collapse:collapse}td,th{padding:4px 10px}</style>")
NESTED_A = (
    NESTED_STYLE + "<main><h1>Nested check</h1><p>An intro paragraph that stays.</p>"
    "<h2>Context</h2><p>Context paragraph one.</p><p>Context paragraph two.</p>"
    "<h2>What stays manual</h2><p>Manual step one.</p><p>Manual step two.</p></main>")
# A whole new section: a new h2, then three new cards, each a block of its own
# (the number is its own text) holding a new h3 and two new paragraphs.
CARD = ("<div class='card'><header><span class='num'>{0}</span><h3>{1}</h3></header>"
        "<p>{2}</p><p>{3}</p></div>")
NESTED_B = NESTED_A.replace("<h2>What stays manual</h2>", (
    "<h2>Planned improvements</h2><div class='grid'>"
    + CARD.format(1, "Offline reading", "Pages stay readable without a network.",
                  "Not done: the cache is filled by hand today.")
    + CARD.format(2, "Shared highlights", "Two readers see each other's marks.",
                  "Later: compare the marks of two readers side by side.")
    + CARD.format(3, "Print layout", "A page that prints the way it reads.",
                  "Still planned for the first version, nothing started yet.")
    + "</div><h2>What stays manual</h2>"))
# Under a CHANGED block, a new or changed block is news of its own; under a NEW
# block, a changed one is not. The Owner card's title is its first child: its
# tag and the card's meet at the top edge.
MIXED_A = (
    NESTED_STYLE + "<main><h1>Mixed check</h1><p>An intro paragraph that stays.</p>"
    "<h2>Status</h2><div class='box'>Status: draft for review"
    "<p>The plan holds for the quarter.</p></div>"
    "<h2>Owner</h2><div class='box'><h3>Budget: ten units per week</h3>"
    "Owner: team alpha, since May</div>"
    "<h2>Scope</h2><div class='box'><p>Scope covers the north and south sites.</p></div>"
    "<h2>Notes</h2><p>Notes stay as they were.</p></main>")
MIXED_B = (MIXED_A
           .replace("Status: draft for review<p>The plan holds for the quarter.</p>",
                    "Status: final for review<p>The plan holds for the quarter.</p>"
                    "<p>A paragraph added inside a changed block.</p>")
           .replace("<h3>Budget: ten units per week</h3>Owner: team alpha, since May",
                    "<h3>Budget: twelve units per week</h3>Owner: team beta, since May")
           .replace("<div class='box'><p>Scope covers the north and south sites.</p>",
                    "<div class='box'>Extended this week"
                    "<p>Scope covers the north and west sites.</p>"))
# The cards of the capture: SIBLING blocks in a card that is no block (an
# <article>), a round numbered dot next to the title, two paragraphs under.
# A new card is one part: one mark on the article, none inside.
SIBLING_CARD = ("<article class='card'><header><div class='num dot'>{0}</div>"
                "<h3>{1}</h3></header><p>{2}</p><p>{3}</p></article>")
SIBLINGS_B = NESTED_A.replace("<h2>What stays manual</h2>", (
    "<h2>Planned improvements</h2><div class='grid'>"
    + SIBLING_CARD.format(1, "Offline reading, without a network", "Pages stay readable.",
                          "Not done: the cache is filled by hand today.")
    + SIBLING_CARD.format(2, "Shared highlights", "Two readers see each other's marks.",
                          "Later: compare the marks of two readers side by side.")
    + "</div><h2>What stays manual</h2>"))
# Lists and tables: a whole new one is one part, a new item in an old one is
# its own; a lone new paragraph in a new wrapper keeps the mark on itself; a
# new <section> whose h2 sits beside a grid of cards splits into them.
LISTS_A = (
    NESTED_STYLE + "<main><h1>Lists check</h1><p>An intro paragraph that stays.</p>"
    "<h2>Tasks</h2><p>Tasks stay as they were.</p>"
    "<h2>Steps</h2><ul><li>First step, kept.</li><li>Second step, kept.</li></ul>"
    "<h2>Figures</h2><p>Figures stay as they were.</p>"
    "<h2>Notes</h2><p>Notes stay as they were.</p></main>")
LISTS_B = (LISTS_A
           .replace("<p>Tasks stay as they were.</p>",
                    "<p>Tasks stay as they were.</p><ul><li>Alpha item to do.</li>"
                    "<li>Beta item to do.</li><li>Gamma item to do.</li></ul>")
           .replace("<li>Second step, kept.</li>",
                    "<li>Second step, kept.</li><li>Third step, added.</li>")
           .replace("<p>Figures stay as they were.</p>",
                    "<p>Figures stay as they were.</p><table><thead><tr><th>Name</th>"
                    "<th>Value</th></tr></thead><tbody><tr><td>Speed</td><td>12</td></tr>"
                    "<tr><td>Load</td><td>3</td></tr></tbody></table>")
           .replace("<p>Notes stay as they were.</p>",
                    "<p>Notes stay as they were.</p><div class='note'>"
                    "<p>A lone new paragraph in a new wrapper.</p></div>"
                    "<section><h2>Wrapped part</h2><div class='grid'>"
                    + CARD.format(1, "Export", "Pages leave as files.", "Planned for later.")
                    + CARD.format(2, "Import", "Files come back as pages.", "Not started.")
                    + "</div></section>"))


@pytest.mark.browser
def test_changes_in_a_real_browser(tmp_path, make_doc):
    """What changed since the last send: marked, folded, walked, then read."""
    pw_dir = os.environ.get("ANNOTATE_PLAYWRIGHT_DIR", "")
    node = shutil.which("node")
    assert pw_dir and node, "see test_overlay_in_a_real_browser"
    assert CHANGES_B != CHANGES_A
    path = make_doc(CHANGES_A, title="Changes check")
    doc_id = registry.register(path, session="sess-browser")["id"]
    cfg = config.load_config()
    srv = server.make_server(cfg, port=0, send=sender.Sender(cfg, opener=RecordingOpener()))
    registry.replace_annotations(doc_id, [{
        "id": "n1", "number": 1, "selector": "body > p:nth-of-type(2)", "quote": "Context one.",
        "note": "Already handled", "offset_x": 4, "offset_y": 4}])
    srv.sender.dispatch(doc_id)                      # the reference is A
    assert registry.read_baseline(doc_id) == path.read_text()
    registry.replace_annotations(doc_id, registry.list_annotations(doc_id) + [{
        "id": "n2", "number": 2, "selector": "body > p:nth-of-type(7)",
        "quote": "A paragraph with an unsent note.", "note": "Still to send",
        "offset_x": 4, "offset_y": 4}])
    path.write_text(path.read_text().replace(CHANGES_A, CHANGES_B))
    nested_ids = []
    for name, before, after in (("nested.html", NESTED_A, NESTED_B),
                                ("mixed.html", MIXED_A, MIXED_B),
                                ("siblings.html", NESTED_A, SIBLINGS_B),
                                ("lists.html", LISTS_A, LISTS_B)):
        assert after != before
        other = make_doc(before, name=name, title=name)
        other_id = registry.register(other, session="sess-browser")["id"]
        assert registry.take_baseline(other_id, registry.now_iso()) == ""
        other.write_text(other.read_text().replace(before, after))
        nested_ids.append(other_id)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = Path(os.environ.get("ANNOTATE_BROWSER_OUT", tmp_path))
    out.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run([node, str(CHANGES_SCRIPT), pw_dir, f"http://localhost:{srv.port}",
                               doc_id, str(out), str(path), *nested_ids],
                              capture_output=True, text=True, timeout=180)
    finally:
        srv.shutdown()
        srv.server_close()
    print(done.stdout)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ten past ten" in (registry.read_baseline(doc_id) or "")   # Mark as read took C
    third = [a for a in registry.list_annotations(doc_id) if a["note"] == "Third note, while folded."]
    assert [a["selector"] for a in third] == ["body > p:nth-of-type(7)"]
