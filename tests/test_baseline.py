"""The reference of "what changed": the document as it was at the last send.

A send copies the file under the data folder (never next to the document);
the page compares that copy with the file on disk. A refused send keeps the
previous copy; a copy that cannot be written REMOVES it, because comparing
with an older version would mark as new what the reader already read.
"""
from __future__ import annotations

import pytest

from annotate import config, registry, sender, terminal
from annotate.config import Config
from fakes import RecordingOpener


def note(id_, text):
    return {"id": id_, "selector": "body > p:nth-of-type(1)", "quote": "q", "note": text,
            "offset_x": 0, "offset_y": 0}


def page(text):
    return f"<!doctype html><html><head><title>T</title></head><body><p>{text}</p></body></html>"


@pytest.fixture
def doc(make_doc):
    path = make_doc("<p>version one</p>")
    return path, registry.register(path, session="sess-1")["id"]


def add_note(doc_id, id_):
    items = registry.list_annotations(doc_id)
    registry.replace_annotations(doc_id, items + [note(id_, "fix " + id_)])


def send_note(send, doc_id, id_):
    add_note(doc_id, id_)
    return send.dispatch(doc_id)


def test_a_send_keeps_the_document_as_it_was_as_the_reference(doc):
    path, doc_id = doc
    sent_text = path.read_text()
    send_note(sender.Sender(Config(), opener=RecordingOpener()), doc_id, "n1")
    path.write_text(page("the session corrected this"))
    assert registry.read_baseline(doc_id) == sent_text
    entry = registry.get(doc_id)
    assert entry["baseline_at"] == entry["sent_at"] != ""


def test_three_sends_keep_one_reference_the_last(doc):
    path, doc_id = doc
    send = sender.Sender(Config(), opener=RecordingOpener())
    for round_ in range(3):
        path.write_text(page(f"version {round_}"))
        send_note(send, doc_id, f"n{round_}")
        assert registry.read_baseline(doc_id) == page(f"version {round_}")
    folder = registry.baseline_path(doc_id).parent
    assert [p.name for p in folder.iterdir()] == [f"{doc_id}.html"]


def test_a_refused_send_keeps_the_previous_reference(doc):
    path, doc_id = doc
    send_note(sender.Sender(Config(), opener=RecordingOpener()), doc_id, "n1")
    first = registry.read_baseline(doc_id)
    path.write_text(page("edited after the send"))
    failing = sender.Sender(Config(), opener=RecordingOpener(
        fail=terminal.TerminalError("wt.exe exited 1")))
    add_note(doc_id, "n2")
    for _ in range(2):                                   # the next cycle too
        with pytest.raises(sender.SendError):
            failing.dispatch(doc_id)
        assert registry.read_baseline(doc_id) == first
    registry.mark_sent(doc_id, ["n2"], "T-by-hand")
    with pytest.raises(sender.NothingToSend):
        sender.Sender(Config(), opener=RecordingOpener()).dispatch(doc_id)
    assert registry.read_baseline(doc_id) == first


def test_a_reference_that_cannot_be_written_is_removed_and_said(doc, monkeypatch):
    path, doc_id = doc
    send = sender.Sender(Config(), opener=RecordingOpener())
    send_note(send, doc_id, "n1")
    assert registry.read_baseline(doc_id) is not None
    real = registry.atomic_write

    def full_disk(target, text):
        assert isinstance(text, str)                     # the real one takes a str
        if target.parent.name == "baselines":
            raise OSError(28, "No space left on device")
        return real(target, text)

    monkeypatch.setattr(registry, "atomic_write", full_disk)
    for round_ in (2, 3):                                # twice: no crash, still nothing
        path.write_text(page(f"version {round_}"))
        result = send_note(send, doc_id, f"n{round_}")
        assert result["count"] == 1                      # the notes went anyway
        assert registry.read_baseline(doc_id) is None
        entry = registry.get(doc_id)
        assert entry["baseline_at"] == ""
        assert "reference" in entry["last_error"] and "No space" in entry["last_error"]


def test_the_reference_never_lives_next_to_the_document(doc):
    path, doc_id = doc
    send_note(sender.Sender(Config(), opener=RecordingOpener()), doc_id, "n1")
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]
    assert registry.baseline_path(doc_id).is_relative_to(config.data_dir())
    assert registry.baseline_path(doc_id).is_file()


def test_forgetting_a_document_forgets_its_reference(doc):
    _, doc_id = doc
    send_note(sender.Sender(Config(), opener=RecordingOpener()), doc_id, "n1")
    assert registry.baseline_path(doc_id).is_file()
    registry.forget(doc_id)
    assert not registry.baseline_path(doc_id).exists()
