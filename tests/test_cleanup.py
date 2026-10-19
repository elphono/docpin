"""Cleanup files (2026-10-19): the documents without a known session are
unmanaged in one batch, and their files deleted a month later if nobody
touched them. Every deletion test checks the disk, not only the index."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from annotate import cleanup, registry, scanner

T = 1_000_000_000.0                 # the moment of the cleanup, injected
DUE = T + 30 * cleanup.DAY          # thirty days later


def nobody(doc_id: str) -> bool:
    return False


@pytest.fixture
def unclaimed(make_doc):
    """A document without a session, its file dated before the cleanup."""
    def make(name: str = "orphan.html", session: str = "") -> Path:
        path = make_doc(f"<p>{name}</p>", name=name)
        os.utime(path, (T - 3600, T - 3600))
        registry.register(path, session=session)
        return path
    return make


def test_cleanup_files_unmanages_the_documents_without_a_session_and_deletes_nothing(unclaimed):
    plain = unclaimed("plain.html")
    owned = unclaimed("owned.html", session="s-1")
    noted = unclaimed("noted.html")
    delivered = unclaimed("delivered.html")
    listened = unclaimed("listened.html")
    ids = {Path(d["path"]).name: i for i, d in registry.all_docs().items()}
    registry.replace_annotations(ids["noted.html"], [{"id": "n1", "selector": "body",
                                                      "quote": "x", "note": "to send"}])
    registry.update(ids["delivered.html"], status="delivered")
    listening = ids["listened.html"].__eq__
    result = cleanup.batch(listening, now=T)
    assert [Path(d["path"]).name for d in result["forgotten"]] == ["plain.html"]
    assert {Path(k["path"]).name: k["reason"] for k in result["kept"]} == {
        "noted.html": "notes wait to be sent", "delivered.html": "the session is on it",
        "listened.html": "a session listens for its notes"}
    assert set(registry.all_docs()) == {ids[n] for n in ("owned.html", "noted.html",
                                                          "delivered.html", "listened.html")}
    for path in (plain, owned, noted, delivered, listened):    # nothing deleted at the click
        assert path.exists()
    info = plain.stat()
    assert registry.cleanup_index() == {str(plain): {"cleaned_at": T, "mtime": info.st_mtime,
                                                     "size": info.st_size}}
    assert registry.dismissed(plain, info.st_mtime)            # unmanaged, as Unmanage does
    assert cleanup.batch(listening, now=T)["forgotten"] == []  # once only


def test_a_cleaned_file_untouched_for_thirty_days_is_deleted_and_not_before(unclaimed, caplog):
    path = unclaimed()
    cleanup.batch(nobody, now=T)
    for moment in (T, T + cleanup.DAY, DUE - 1):                 # three sweeps too early
        assert cleanup.sweep(now=moment) == {"deleted": [], "released": []}
        assert path.exists() and str(path) in registry.cleanup_index()
    caplog.set_level("INFO", logger="annotate")
    assert cleanup.sweep(now=DUE) == {"deleted": [str(path)], "released": []}
    assert not path.exists() and registry.cleanup_index() == {}
    assert f"cleanup: {path} deleted, untouched 30 day(s) after its cleanup" in caplog.text
    assert cleanup.sweep(now=DUE + cleanup.DAY) == {"deleted": [], "released": []}


@pytest.mark.parametrize("touch", ["date", "size"])
def test_a_cleaned_file_touched_since_is_kept_and_leaves_the_index(unclaimed, touch):
    path = unclaimed()
    cleanup.batch(nobody, now=T)
    before = path.stat()
    if touch == "date":
        os.utime(path, (T + 60, T + 60))
    else:                                       # longer, under the same date
        path.write_text(path.read_text() + "<p>more</p>")
        os.utime(path, (before.st_atime, before.st_mtime))
        assert path.stat().st_mtime == before.st_mtime
    assert cleanup.sweep(now=DUE + cleanup.DAY) == {"deleted": [], "released": [str(path)]}
    assert path.exists() and registry.cleanup_index() == {}


def test_a_cleaned_file_listed_again_is_never_deleted(unclaimed):
    path = unclaimed()
    cleanup.batch(nobody, now=T)
    doc_id = registry.register(path)["id"]          # by hand, the hook, the scan: register
    assert registry.cleanup_index() == {}
    registry.forget(doc_id)                         # then a plain Unmanage
    assert cleanup.sweep(now=DUE + cleanup.DAY) == {"deleted": [], "released": []}
    assert path.exists()


def test_a_stale_index_entry_of_a_registered_document_is_never_deleted(unclaimed):
    """The sweep's own check, apart from register's: an index that still
    names a listed document (restored from a backup, say) deletes nothing."""
    path = unclaimed()
    info = path.stat()
    registry.cleanup_path().write_text(json.dumps({"version": 1, "files": {str(path): {
        "cleaned_at": T, "mtime": info.st_mtime, "size": info.st_size}}}))
    assert cleanup.sweep(now=DUE + cleanup.DAY) == {"deleted": [], "released": [str(path)]}
    assert path.exists() and len(registry.all_docs()) == 1


def test_only_a_file_of_the_index_is_ever_deleted(unclaimed, ws_folder):
    cleaned = unclaimed("cleaned.html")
    unmanaged = unclaimed("unmanaged.html")
    old = unclaimed("old.html")
    excluded = ws_folder / "app" / "page.html"
    excluded.parent.mkdir()
    excluded.write_text("<title>App</title>")
    os.utime(excluded, (T - 3600, T - 3600))
    registry.register(excluded)
    os.utime(old, (T - 30 * cleanup.DAY,) * 2)
    ids = {Path(d["path"]).name: i for i, d in registry.all_docs().items()}
    registry.forget(ids["unmanaged.html"])                                  # Unmanage
    from annotate import folders
    folders.exclude(str(excluded.parent), listening=nobody)                  # an exclusion
    scanner.prune(7, now=T, listening=nobody)                                # a rescan
    assert set(registry.all_docs()) == {ids["cleaned.html"]}
    cleanup.batch(nobody, ids=[ids["cleaned.html"]], now=T)
    assert set(registry.cleanup_index()) == {str(cleaned)}
    assert cleanup.sweep(now=DUE + 100 * cleanup.DAY)["deleted"] == [str(cleaned)]
    assert not cleaned.exists()
    assert unmanaged.exists() and old.exists() and excluded.exists()


def test_a_file_already_gone_leaves_the_index_without_an_error(unclaimed):
    path = unclaimed()
    cleanup.batch(nobody, now=T)
    path.unlink()
    assert cleanup.sweep(now=DUE) == {"deleted": [], "released": [str(path)]}
    assert registry.cleanup_index() == {}


def test_an_index_entry_that_cannot_be_read_is_never_deleted(unclaimed):
    path = unclaimed()
    info = path.stat()
    registry.forget(next(iter(registry.all_docs())))
    for record in ({"cleaned_at": T, "mtime": info.st_mtime},            # no size
                   {"cleaned_at": "long ago", "mtime": info.st_mtime, "size": info.st_size},
                   {"cleaned_at": True, "mtime": info.st_mtime, "size": info.st_size}):
        registry.cleanup_path().write_text(json.dumps({"version": 1,
                                                       "files": {str(path): record}}))
        assert cleanup.sweep(now=DUE + cleanup.DAY)["deleted"] == []
        assert path.exists()
    registry.cleanup_path().write_text(json.dumps({"version": 1, "files": {"relative.html": {
        "cleaned_at": T, "mtime": 0, "size": 0}}}))
    assert registry.cleanup_index() == {}


def test_the_delay_is_a_named_constant_the_environment_can_change(unclaimed, monkeypatch):
    assert cleanup.after_days() == cleanup.CLEANUP_AFTER_DAYS == 30.0
    path = unclaimed()
    cleanup.batch(nobody, now=T)
    monkeypatch.setenv(cleanup.ENV, "2")
    assert cleanup.after_days() == 2.0
    assert cleanup.sweep(now=T + 2 * cleanup.DAY - 1)["deleted"] == []
    assert cleanup.listing() == [{"path": str(path), **registry.cleanup_index()[str(path)],
                                  "due_at": T + 2 * cleanup.DAY}]
    assert cleanup.sweep(now=T + 2 * cleanup.DAY)["deleted"] == [str(path)]
    for bad in ("x", "0", "-1", str(cleanup.MAX_DAYS + 1)):
        monkeypatch.setenv(cleanup.ENV, bad)
        with pytest.raises(cleanup.CleanupError):
            cleanup.after_days()


def test_a_bad_delay_deletes_nothing(unclaimed, monkeypatch):
    path = unclaimed()
    cleanup.batch(nobody, now=T)
    monkeypatch.setenv(cleanup.ENV, "soon")
    with pytest.raises(cleanup.CleanupError):
        cleanup.sweep(now=DUE + 100 * cleanup.DAY)
    assert path.exists() and str(path) in registry.cleanup_index()


def test_a_cleanup_of_chosen_ids_takes_only_those(unclaimed):
    first, second = unclaimed("first.html"), unclaimed("second.html")
    ids = {Path(d["path"]).name: i for i, d in registry.all_docs().items()}
    result = cleanup.batch(nobody, ids=[ids["first.html"], "deadbeef"], now=T)
    assert [d["path"] for d in result["forgotten"]] == [str(first)]
    assert list(registry.all_docs()) == [ids["second.html"]] and second.exists()
    for bad in ("all", [1, 2], {"ids": []}):
        with pytest.raises(cleanup.CleanupError):
            cleanup.batch(nobody, ids=bad)


def test_the_sweeper_sweeps_at_most_once_an_hour(monkeypatch):
    clock = {"now": 5000.0}
    swept: list[float | None] = []
    monkeypatch.setattr(cleanup, "sweep", lambda now=None: swept.append(now) or {})
    sweeper = cleanup.Sweeper(clock=lambda: clock["now"])
    answers = []
    for moment in (5000.0, 5010.0, 8599.0, 8600.0, 8601.0, 12199.0, 12200.0):
        clock["now"] = moment
        answers.append(sweeper.tick() is not None)
    assert answers == [True, False, False, True, False, False, True]
    assert len(swept) == 3


def test_the_scan_thread_sweeps_and_a_failing_sweep_does_not_stop_it(monkeypatch):
    passes, ticks = [], []
    stop = threading.Event()

    def counted():
        passes.append(1)
        if len(passes) == 3:
            stop.set()

    class Flaky:
        def tick(self, now=None):
            ticks.append(1)
            if len(ticks) == 1:
                raise OSError("disk hiccup")

    monkeypatch.setattr(scanner, "scan_once", counted)
    thread = threading.Thread(target=scanner.run, args=(stop, 0.01, Flaky()), daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and len(passes) == 3 and len(ticks) == 3


def test_the_message_names_the_day_of_deletion():
    assert cleanup.message(2, 1, now=T) == (
        f"2 document(s) without a known session unmanaged; their files are deleted on "
        f"{cleanup.day_of(DUE)} unless written again before; 1 kept, protected "
        f"(a session on it, notes to send, or its file missing).")
    assert cleanup.message(0, 0, now=T) == "0 document(s) without a known session unmanaged."


@pytest.fixture
def ws_folder(tmp_path):
    folder = tmp_path / "workspace" / "repo" / "docs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder
