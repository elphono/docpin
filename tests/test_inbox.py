"""Posting into the inbox of an open session (see inbox.py).

The receiving end is a real Unix socket in the test's folder, registered in
an isolated `~/.claude/sessions` (conftest): never one of the user's sessions.
"""
from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from annotate import inbox, registry, sender
from annotate.config import Config
from fakes import RecordingOpener, register_session


class Inbox:
    """A listening socket that keeps every line it receives."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lines: list[dict] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        self._srv.listen(5)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            data = b""
            with conn:
                while chunk := conn.recv(65536):
                    data += chunk
            self.lines += [json.loads(x) for x in data.decode().splitlines() if x]

    def wait(self, count: int = 1) -> None:
        for _ in range(300):
            if len(self.lines) >= count:
                return
            threading.Event().wait(0.01)

    def close(self) -> None:
        self._srv.close()


@pytest.fixture
def box(tmp_path):
    receiver = Inbox(tmp_path / "s.sock")
    yield receiver
    receiver.close()


def test_the_line_is_the_one_claude_code_writes_and_claims_no_permission_class():
    message = json.loads(inbox.line('notes with "quotes" and é'))
    assert (message["msgV"], message["type"], message["priority"]) == (1, "user", "next")
    content = message["message"]["content"]
    assert message["message"]["role"] == "user"
    assert content.startswith('<cross-session-message from="annotate" from-name="annotate">\n')
    assert content.endswith('\n</cross-session-message>')
    assert 'notes with "quotes" and é' in content
    assert "from-mode" not in content, "claiming a class would skip the approval dialog"


# What Claude Code 2.1.296 wrote into a witness inbox on 2026-10-12, from a
# real SendMessage (the socket path and session name are shortened).
CAPTURED_2_1_296 = (
    '{"msgV":1,"msg_id":"ab5d2124-4a9a-4d8c-a9f9-0057060def78","type":"user",'
    '"message":{"role":"user","content":"<cross-session-message from=\\"uds:/run/x.sock\\" '
    'from-name=\\"probe\\" from-mode=\\"bypass\\">\\nhello from the format check\\n'
    '</cross-session-message>"},"priority":"next","from":"uds:/run/x.sock"}')


def test_the_line_has_the_shape_claude_code_2_1_296_writes():
    real = json.loads(CAPTURED_2_1_296)
    ours = json.loads(inbox.line("hello from the format check"))
    assert set(ours) == set(real) and set(ours["message"]) == set(real["message"])
    assert {k: ours[k] for k in ("msgV", "type", "priority")} == \
        {k: real[k] for k in ("msgV", "type", "priority")}

    def body(content: str) -> str:
        return content.split(">\n", 1)[1]

    assert body(ours["message"]["content"]) == body(real["message"]["content"])
    assert real["message"]["content"].startswith("<cross-session-message from=")


@pytest.mark.parametrize("protocol", [2, None, "1"])
def test_a_session_on_an_unchecked_protocol_gets_nothing_and_the_notes_stay(
        box, make_doc, protocol):
    register_session("s-1", box.path, protocol=protocol, version="9.9.9")
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                        "note": "n"}])
    opener = RecordingOpener()
    send = sender.Sender(Config(), opener=opener)
    for _ in range(2):                                   # the next click too
        with pytest.raises(sender.SendError, match=r"9\.9\.9.*annotate.* wait "):
            send.dispatch(doc)
    assert box.lines == [] and opener.calls == []        # no post, never a tab
    assert len(registry.pending(registry.list_annotations(doc))) == 1
    assert "not one whose inbox format docpin checked" in registry.get(doc)["last_error"]


def test_resend_puts_the_last_batch_back_and_posts_it_again(box, make_doc):
    register_session("s-1", box.path)
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    send = sender.Sender(Config(), opener=RecordingOpener())
    with pytest.raises(sender.NothingToSend, match="nothing was sent"):
        send.resend(doc)
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                        "note": "first batch"}])
    send.dispatch(doc)
    items = registry.list_annotations(doc)
    registry.replace_annotations(doc, items + [{"id": "n2", "selector": "body",
                                                "quote": "", "note": "added later"}])
    send.dispatch(doc)                                   # batch 2: n2 alone
    for _ in range(100):
        if len(box.lines) == 2:
            break
        threading.Event().wait(0.02)
    result = send.resend(doc)
    assert result["target"] == "inbox" and result["count"] == 1   # the LAST batch only
    for _ in range(100):
        if len(box.lines) == 3:
            break
        threading.Event().wait(0.02)
    third = box.lines[2]["message"]["content"]
    assert "added later" in third and "first batch" not in third
    assert registry.pending(registry.list_annotations(doc)) == []


def test_only_a_live_process_of_that_session_counts(box, tmp_path):
    register_session("s-1", box.path, name="live")
    register_session("s-1", box.path, pid=2 ** 22 + 7, name="dead")          # no such pid
    register_session("s-1", box.path, proc_start="1", name="reused")         # pid reused
    register_session("s-1", tmp_path / "no.sock", name="no-socket")
    register_session("s-2", box.path, name="other-session")
    assert [e["name"] for e in inbox.open_sessions("s-1")] == ["live"]


def test_the_open_session_ids_pass_the_same_test_as_a_send(box, tmp_path):
    """What the index page calls "open" is what the sender would post into."""
    register_session("s-1", box.path, name="live")
    register_session("s-2", box.path, pid=2 ** 22 + 7, name="dead")          # no such pid
    register_session("s-3", box.path, proc_start="1", name="reused")         # pid reused
    register_session("s-4", tmp_path / "no.sock", name="no-socket")
    register_session("s-5", box.path, name="also-live")
    (inbox.sessions_dir() / "garbage.json").write_text("{not json")
    assert inbox.open_session_ids() == {"s-1", "s-5"}
    for session in ("s-1", "s-2", "s-3", "s-4", "s-5"):
        assert bool(inbox.open_sessions(session)) == (session in {"s-1", "s-5"})


def test_an_open_session_that_does_not_listen_gets_the_notes_in_its_inbox(box, make_doc):
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "x",
                                        "note": "fix the title"}])
    register_session("s-1", box.path)
    opener = RecordingOpener()
    result = sender.Sender(Config(), opener=opener).dispatch(doc)
    box.wait()
    assert result["target"] == "inbox" and result["name"] == "session-c1"
    assert opener.calls == [], "the session is open: never a second tab"
    assert "fix the title" in box.lines[0]["message"]["content"]
    assert registry.pending(registry.list_annotations(doc)) == []
    assert "approve the message" in sender.describe(result)


def test_the_most_recently_active_copy_of_a_twice_open_session_is_chosen(tmp_path, make_doc):
    old, recent = Inbox(tmp_path / "old.sock"), Inbox(tmp_path / "recent.sock")
    try:
        register_session("s-1", old.path, name="old", updated=100)
        register_session("s-1", recent.path, name="recent", updated=200)
        doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
        registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                            "note": "n"}])
        assert sender.Sender(Config(), opener=RecordingOpener()).dispatch(doc)["name"] == "recent"
        recent.wait()
        assert len(recent.lines) == 1 and old.lines == []
    finally:
        old.close()
        recent.close()


def test_a_refusing_inbox_keeps_the_notes_and_opens_no_tab(tmp_path, make_doc):
    dead = tmp_path / "dead.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(dead))
    srv.close()                              # the file exists, nobody listens
    register_session("s-1", dead)
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                        "note": "n"}])
    opener = RecordingOpener()
    with pytest.raises(sender.SendError, match="did not take"):
        sender.Sender(Config(), opener=opener).dispatch(doc)
    assert opener.calls == []
    assert len(registry.pending(registry.list_annotations(doc))) == 1


def test_a_listening_session_still_comes_first(box, make_doc):
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                        "note": "n"}])
    register_session("s-1", box.path)
    send = sender.Sender(Config(), opener=RecordingOpener())
    got: dict = {}
    waiter = threading.Thread(target=lambda: got.update(
        w=send.board.wait(doc, "s-1", "/", lambda: True, 10)), daemon=True)
    waiter.start()
    for _ in range(200):
        if send.board.listening(doc):
            break
        threading.Event().wait(0.01)
    assert send.dispatch(doc)["target"] == "session"
    waiter.join(5)
    assert box.lines == []


def test_new_session_never_posts_into_the_open_one(box, make_doc):
    doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
    registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                        "note": "n"}])
    register_session("s-1", box.path)
    opener = RecordingOpener()
    assert sender.Sender(Config(), opener=opener).dispatch(doc, fresh=True)["target"] == "terminal"
    assert box.lines == [] and len(opener.calls) == 1


def test_an_inbox_that_takes_the_line_and_ignores_it_counts_as_delivered(tmp_path, make_doc):
    # The limit inbox.py states, pinned: a socket that reads the line and
    # understands none of it looks, from the daemon, exactly like a session
    # that got the notes. The protocol check narrows it to formats Claude
    # Code was seen to read; "Resend" (test above) is how the user recovers.
    path = tmp_path / "drop.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    got = threading.Event()

    def drop() -> None:
        conn, _ = srv.accept()
        with conn:
            while conn.recv(65536):
                got.set()

    threading.Thread(target=drop, daemon=True).start()
    try:
        register_session("s-1", path)
        doc = registry.register(make_doc("<p>x</p>"), session="s-1")["id"]
        registry.replace_annotations(doc, [{"id": "n1", "selector": "body", "quote": "",
                                            "note": "n"}])
        result = sender.Sender(Config(), opener=RecordingOpener()).dispatch(doc)
        assert got.wait(3), "the line reached the socket"
        entry = registry.get(doc)
        assert result["target"] == "inbox" and entry["status"] == "delivered"
        assert registry.pending(registry.list_annotations(doc)) == [] and not entry["last_error"]
    finally:
        srv.close()
