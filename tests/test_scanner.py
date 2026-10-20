"""The daemon's pass over the tracked folders: documents the hook did not see,
and the catch-up the user asks for ("Rescan", adding a folder)."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from annotate import claude, folders, registry, scanner
from fakes import tool_call, transcript

HTML = "<!doctype html><title>{}</title><p>x</p>"
NOW = 100 * scanner.DAY


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def ws(tmp_path):
    """The workspace, the default tracked folder (conftest points
    ANNOTATE_WORKSPACE at it)."""
    root = tmp_path / "workspace"
    (root / "repo" / ".git").mkdir(parents=True)
    return root


def write(root: Path, relative: str, mtime: float, title: str = "Doc") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HTML.format(title))
    os.utime(path, (mtime, mtime))
    return path


def names(ids: list[str]) -> set[str]:
    return {Path(registry.get(i)["path"]).name for i in ids}


# -- the regular pass ----------------------------------------------------------

def test_nothing_older_than_the_first_pass_is_taken(ws):
    write(ws, "repo/docs/old.html", 1000.0)
    assert scanner.scan_once(now=2000.0) == []          # first pass: a starting point
    assert scanner.scan_once(now=3000.0) == []          # the old file stays out
    assert registry.all_docs() == {}


def test_a_document_written_after_the_first_pass_is_registered_with_its_writer(ws):
    scanner.scan_once(now=2000.0)
    path = write(ws, "repo/docs/sub/new.html", 2500.0, title="Timeline")
    transcript(claude.projects_dir(), "parent-1", folder="-home-x-repo/parent-1/subagents",
               lines=(tool_call("parent-1", iso(2499.0), f"cp /tmp/s/new.html {path}",
                                cwd=str(ws / "repo")),))
    [doc_id] = scanner.scan_once(now=3000.0)
    entry = registry.get(doc_id)
    assert entry["path"] == str(path) and entry["title"] == "Timeline"
    assert entry["session_id"] == "parent-1" and entry["cwd"] == str(ws / "repo")


def test_a_document_nobody_claims_is_registered_without_a_session(ws):
    scanner.scan_once(now=2000.0)
    write(ws, "repo/docs/orphan.html", 2500.0)
    [doc_id] = scanner.scan_once(now=3000.0)
    assert registry.get(doc_id)["session_id"] == ""


def test_the_daemon_being_down_loses_nothing(ws):
    scanner.scan_once(now=2000.0)
    scanner.scan_once(now=3000.0)
    write(ws, "repo/docs/while-down.html", 3500.0)     # after the last pass
    assert len(scanner.scan_once(now=9000.0)) == 1     # the next start finds it


def test_each_pass_starts_where_the_previous_one_did(ws):
    """Since 2026-10-07 an unmanaged document is also kept out by
    registry.dismissed: the starting point needs its own test."""
    for now in (2000.0, 3000.0, 4000.0):
        scanner.scan_once(now=now)
        assert scanner.read_since() == now


def test_three_passes_register_each_document_once(ws):
    scanner.scan_once(now=2000.0)
    write(ws, "repo/docs/a.html", 2500.0)
    assert len(scanner.scan_once(now=3000.0)) == 1
    write(ws, "repo/docs/b.html", 3500.0)
    assert len(scanner.scan_once(now=4000.0)) == 1
    assert scanner.scan_once(now=5000.0) == []
    assert len(registry.all_docs()) == 2


def test_a_document_the_hook_registered_keeps_its_session(ws):
    scanner.scan_once(now=2000.0)
    path = write(ws, "repo/docs/hooked.html", 2500.0)
    doc_id = registry.register(path, session="from-the-hook")["id"]
    assert scanner.scan_once(now=3000.0) == []
    assert registry.get(doc_id)["session_id"] == "from-the-hook"


def test_a_forgotten_document_comes_back_only_if_written_again(ws):
    scanner.scan_once(now=2000.0)
    path = write(ws, "repo/docs/f.html", 2500.0)
    [doc_id] = scanner.scan_once(now=3000.0)
    registry.forget(doc_id)
    assert scanner.scan_once(now=4000.0) == []
    os.utime(path, (4500.0, 4500.0))
    assert len(scanner.scan_once(now=5000.0)) == 1


def test_figures_build_output_and_tool_folders_are_never_documents(ws):
    scanner.scan_once(now=2000.0)
    for relative in ("repo/docs/figures/x/tpl.html", "repo/docs/node_modules/p/i.html",
                     "repo/src/page.html", "repo/docs/notes.md",
                     "repo/docs/_build/html/index.html"):
        write(ws, relative, 2500.0)
    assert scanner.scan_once(now=3000.0) == []


def test_a_document_inside_an_agent_worktree_is_not_registered(ws):
    """A temporary copy, deleted with the agent (2026-10-06: it stayed listed as
    'file missing')."""
    scanner.scan_once(now=2000.0)
    write(ws, "repo/.claude/worktrees/agent-a9/docs/install.html", 2500.0)
    assert scanner.scan_once(now=3000.0) == []


def test_a_failing_pass_does_not_stop_the_scan(ws, monkeypatch):
    calls = []
    stop = threading.Event()

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk hiccup")
        if len(calls) == 3:
            stop.set()

    monkeypatch.setattr(scanner, "scan_once", flaky)
    thread = threading.Thread(target=scanner.run, args=(stop, 0.01), daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and len(calls) == 3


def test_an_unreadable_state_is_an_error_not_a_backfill(ws):
    scanner.state_path().parent.mkdir(parents=True, exist_ok=True)
    scanner.state_path().write_text("{broken")
    with pytest.raises(scanner.ScanError):
        scanner.scan_once(now=2000.0)


# -- the tracked folders -------------------------------------------------------

@pytest.mark.parametrize(("relative", "expected"), [
    ("repo/docs/a.html", True), ("repo/docs/specs/b.html", True), ("docs/f.html", True),
    ("repo/docs/figures/x/tpl.html", False), ("repo/docs/.git/c.html", False),
    ("repo/src/d.html", False), ("repo/docs.html", False), ("repo/docs/e.md", False),
    ("repo/doc/g.html", False), ("repo/docs/node_modules/h.html", False),
    ("repo/.claude/worktrees/agent-1/docs/i.html", False),
    ("repo/build/reports/j.html", False),
])
def test_the_default_folder_is_the_workspace_and_its_docs_folders(ws, relative, expected):
    assert folders.load() == [{"path": str(ws.resolve()), "docs_only": True}]
    assert folders.wanted(write(ws, relative, 2500.0)) is expected


def test_a_folder_the_user_adds_tracks_every_html_under_it(ws, tmp_path):
    reports = tmp_path / "other" / "reports"
    reports.mkdir(parents=True)
    scanner.scan_once(now=2000.0)
    folders.add(str(reports))
    write(reports, "2026/run-1/report.html", 2500.0)       # no docs/ on the way
    write(reports, "build/coverage/index.html", 2500.0)    # build output stays out
    write(ws, "repo/src/page.html", 2500.0)                # the workspace keeps its rule
    assert names(scanner.scan_once(now=3000.0)) == {"report.html"}


def test_adding_a_folder_twice_keeps_one_entry_that_tracks_everything(ws):
    folders.add(str(ws))
    folders.add(str(ws) + "/")
    assert folders.load() == [{"path": str(ws.resolve()), "docs_only": False}]


def test_a_removed_folder_is_no_longer_walked_and_its_documents_stay(ws):
    scanner.scan_once(now=2000.0)
    kept = write(ws, "repo/docs/kept.html", 2500.0)
    assert len(scanner.scan_once(now=3000.0)) == 1
    folders.remove(str(ws.resolve()))
    assert folders.load() == []                       # the default does not come back
    write(ws, "repo/docs/later.html", 3500.0)
    assert scanner.scan_once(now=4000.0) == []
    assert [d["path"] for d in registry.all_docs().values()] == [str(kept)]


def test_removing_a_folder_that_is_not_tracked_is_an_error(ws):
    with pytest.raises(folders.FoldersError, match="not a tracked folder"):
        folders.remove("/nowhere")


@pytest.mark.parametrize(("raw", "message"), [
    ("", "no folder"), ("relative/path", "not an absolute path"),
    ("/usr", "is not under"), ("~/../..", "is not under"),
])
def test_a_folder_outside_the_home_directory_or_not_absolute_is_refused(ws, raw, message):
    with pytest.raises(folders.FoldersError, match=message):
        folders.add(raw)
    assert not folders.path().exists()


def test_a_file_or_a_missing_folder_is_refused(ws, tmp_path):
    (tmp_path / "a-file").write_text("x")
    for raw in (str(tmp_path / "a-file"), str(tmp_path / "missing")):
        with pytest.raises(folders.FoldersError, match="not a folder"):
            folders.add(raw)


def test_a_tilde_is_the_home_directory(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "notes").mkdir()
    assert folders.add("~/notes")["path"] == str((tmp_path / "notes").resolve())


def test_a_folder_inside_a_skipped_one_is_walked_on_its_own(ws, tmp_path):
    """`~` prunes `.claude`; `~/.claude/sync/docs`, tracked too, is walked
    separately, and a document under both is registered once."""
    sync_docs = tmp_path / ".claude" / "sync" / "docs"
    sync_docs.mkdir(parents=True)
    folders.add(str(tmp_path))
    folders.add(str(sync_docs))
    assert folders.roots(folders.load()) == [tmp_path.resolve(), sync_docs.resolve()]
    scanner.scan_once(now=2000.0)
    write(sync_docs, "patchnotes/v1.html", 2500.0)
    write(tmp_path, ".claude/plugins/template.html", 2500.0)   # still pruned
    write(ws, "repo/src/page.html", 2500.0)                     # ~ takes everything
    assert names(scanner.scan_once(now=3000.0)) == {"v1.html", "page.html"}


def test_a_folder_inside_a_tracked_one_is_not_walked_twice(ws, tmp_path):
    inner = ws / "repo"
    folders.add(str(ws))
    folders.add(str(inner))
    assert folders.roots(folders.load()) == [ws.resolve()]


# -- the excluded folders (2026-10-19) -------------------------------------------

def nobody(doc_id: str) -> bool:
    return False


def test_a_document_under_an_excluded_folder_is_never_wanted_even_in_a_tracked_one(ws, tmp_path):
    """The pages of an application a session builds are not documents, under
    a tracked folder or a `docs/` folder of the workspace."""
    app = tmp_path / "app"
    folders.add(str(app.parent))                            # the home: every .html
    recent = NOW - scanner.DAY                              # in the window of a rescan
    page = write(app, "public/docs/index.html", recent)     # a docs/ on the way, too
    docs_page = write(ws, "repo/docs/app/page.html", recent)
    beside = write(ws, "repo/docs/report.html", recent)
    scanner.scan_once(now=2000.0)
    assert all(folders.wanted(p) for p in (page, docs_page, beside))   # before: documents
    for folder in (app, ws / "repo" / "docs" / "app"):
        folders.exclude(str(folder), listening=nobody)
    assert [folders.wanted(p) for p in (page, docs_page, beside)] == [False, False, True]
    assert names(scanner.scan_once(now=3000.0)) == {"report.html"}
    assert names(scanner.catch_up(7, now=NOW)) == set()     # the rescan asks the same rule
    assert folders.load_excluded() == [app.resolve(), (ws / "repo" / "docs" / "app").resolve()]


def test_excluding_a_folder_unmanages_the_documents_listed_under_it(ws, tmp_path):
    inside = write(ws, "repo/docs/app/page.html", NOW - scanner.DAY)
    deeper = write(ws, "repo/docs/app/sub/other.html", NOW - scanner.DAY)
    outside = write(ws, "repo/docs/report.html", NOW - scanner.DAY)
    ids = {Path(p).name: registry.register(p)["id"] for p in (inside, deeper, outside)}
    result = folders.exclude(str(ws / "repo" / "docs" / "app"), listening=nobody)
    assert {Path(d["path"]).name for d in result["forgotten"]} == {"page.html", "other.html"}
    assert result["kept"] == []
    assert list(registry.all_docs()) == [ids["report.html"]]
    for path in (inside, deeper):                           # unmanaged: the files stay
        assert path.exists() and registry.dismissed(path, path.stat().st_mtime)


def test_excluding_a_folder_keeps_a_document_a_batch_never_takes(ws):
    path = write(ws, "repo/docs/app/listened.html", NOW - scanner.DAY)
    gone = write(ws, "repo/docs/app/gone.html", NOW - scanner.DAY)
    doc_id = registry.register(path)["id"]
    gone_id = registry.register(gone)["id"]
    gone.unlink()                                  # listened too: a missing file still goes
    result = folders.exclude(str(ws / "repo" / "docs" / "app"),
                             listening={doc_id, gone_id}.__contains__)
    assert [d["id"] for d in result["forgotten"]] == [gone_id]
    assert list(registry.all_docs()) == [doc_id]
    assert result["kept"] == [{"id": doc_id, "path": str(path),
                               "reason": "a session listens for its notes"}]


def test_an_exclusion_survives_the_tracked_list_changing_and_back(ws, tmp_path):
    (tmp_path / "other").mkdir()
    write(ws, "repo/docs/app/x.html", 2500.0)
    folders.exclude(str(ws / "repo" / "docs" / "app"), listening=nobody)
    # Only an exclusion on disk: the default tracked folder is still the default.
    assert folders.load() == [{"path": str(ws.resolve()), "docs_only": True}]
    folders.add(str(tmp_path / "other"))
    folders.remove(str(ws.resolve()))
    assert folders.load_excluded() == [(ws / "repo" / "docs" / "app").resolve()]
    folders.exclude(str(ws / "repo" / "docs" / "app") + "/", listening=nobody)   # once only
    assert len(folders.load_excluded()) == 1
    assert folders.unexclude(str((ws / "repo" / "docs" / "app").resolve())) == {
        "path": str((ws / "repo" / "docs" / "app").resolve())}
    assert folders.load_excluded() == []
    assert [f["path"] for f in folders.load()] == [str((tmp_path / "other").resolve())]
    with pytest.raises(folders.FoldersError, match="not an excluded folder"):
        folders.unexclude(str(ws))


@pytest.mark.parametrize(("raw", "message"), [
    ("", "no folder"), ("relative/path", "not an absolute path"), ("/usr", "is not under"),
])
def test_an_excluded_folder_follows_the_rules_of_a_tracked_one(ws, raw, message):
    with pytest.raises(folders.FoldersError, match=message):
        folders.exclude(raw, listening=nobody)
    assert not folders.path().exists()


def test_subfolders_are_proposed_under_the_home_directory_only(ws, tmp_path):
    for name in ("alpha", "alps", "beta", ".hidden"):
        (tmp_path / "pick" / name).mkdir(parents=True)
    listing = folders.subfolders(str(tmp_path / "pick" / "al"))
    assert [Path(p).name for p in listing["folders"]] == ["alpha", "alps"]
    listing = folders.subfolders(str(tmp_path / "pick") + "/")
    assert [Path(p).name for p in listing["folders"]] == [".hidden", "alpha", "alps", "beta"]
    assert folders.subfolders("/usr/")["base"] == str(tmp_path.resolve())


# -- the catch-up --------------------------------------------------------------

def test_a_catch_up_takes_the_recent_window_only(ws):
    write(ws, "repo/docs/yesterday.html", NOW - 1 * scanner.DAY)
    write(ws, "repo/docs/last-week.html", NOW - 6 * scanner.DAY)
    write(ws, "repo/docs/last-month.html", NOW - 30 * scanner.DAY)
    assert names(scanner.catch_up(7, now=NOW)) == {"yesterday.html", "last-week.html"}
    assert scanner.catch_up(7, now=NOW) == []                       # once only
    assert names(scanner.catch_up(31, now=NOW)) == {"last-month.html"}


def test_a_catch_up_does_not_move_the_regular_pass(ws):
    scanner.scan_once(now=NOW)
    since = scanner.read_since()
    write(ws, "repo/docs/a.html", NOW - scanner.DAY)
    scanner.catch_up(7, now=NOW + 10)
    assert scanner.read_since() == since


def test_an_unmanaged_document_stays_out_of_a_catch_up_until_written_again(ws):
    path = write(ws, "repo/docs/dismissed.html", NOW - scanner.DAY)
    [doc_id] = scanner.catch_up(7, now=NOW)
    registry.forget(doc_id)
    assert scanner.catch_up(7, now=NOW) == []
    assert scanner.catch_up(7, now=NOW + 1) == []                   # and again
    os.utime(path, (NOW - 10, NOW - 10))                            # written again
    assert len(scanner.catch_up(7, now=NOW)) == 1
    assert not registry.dismissed(path, NOW - 10)                  # the record is gone


def test_a_deleted_document_leaves_no_record(ws):
    path = write(ws, "repo/docs/gone.html", NOW - scanner.DAY)
    [doc_id] = scanner.catch_up(7, now=NOW)
    registry.forget(doc_id, delete_file=True)
    assert not registry.forgotten_path().exists() or str(path) not in \
        registry.forgotten_path().read_text()


def test_a_catch_up_of_one_folder_leaves_the_others_alone(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    folders.add(str(other))
    write(ws, "repo/docs/ws.html", NOW - scanner.DAY)
    write(other, "o.html", NOW - scanner.DAY)
    assert names(scanner.catch_up(7, only=str(other.resolve()), now=NOW)) == {"o.html"}
    with pytest.raises(scanner.ScanError, match="not a tracked folder"):
        scanner.catch_up(7, only="/nowhere", now=NOW)


# -- the rescan also clears (2026-10-19) ------------------------------------------

def test_a_catch_up_forgets_the_documents_older_than_its_window(ws):
    since = NOW - 7 * scanner.DAY
    fresh = write(ws, "repo/docs/fresh.html", NOW - scanner.DAY)
    edge = write(ws, "repo/docs/edge.html", since + 1)            # just inside the window
    at_edge = write(ws, "repo/docs/at-edge.html", since)          # where catch_up stops
    old = write(ws, "repo/docs/old.html", NOW - 30 * scanner.DAY)
    ids = {p.name: registry.register(p)["id"] for p in (fresh, edge, at_edge, old)}
    result = scanner.rescan(7, now=NOW, listening=nobody)
    assert result["added"] == [] and result["kept"] == []
    assert sorted(result["removed"]) == sorted([ids["at-edge.html"], ids["old.html"]])
    assert set(registry.all_docs()) == {ids["fresh.html"], ids["edge.html"]}
    for path in (at_edge, old):                                   # unmanaged: remembered
        assert path.exists() and registry.dismissed(path, path.stat().st_mtime)
    assert scanner.rescan(7, now=NOW, listening=nobody)["removed"] == []    # once only
    assert scanner.rescan(31, now=NOW, listening=nobody)["added"] == []     # not brought back
    os.utime(old, (NOW - 10, NOW - 10))                           # written again
    assert scanner.rescan(7, now=NOW, listening=nobody)["added"] == [ids["old.html"]]


def test_a_catch_up_of_one_folder_forgets_only_under_it(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    folders.add(str(other))
    old_ws = registry.register(write(ws, "repo/docs/ws.html", NOW - 30 * scanner.DAY))["id"]
    old_other = registry.register(write(other, "o.html", NOW - 30 * scanner.DAY))["id"]
    result = scanner.rescan(7, only=str(other.resolve()), now=NOW, listening=nobody)
    assert result["removed"] == [old_other] and list(registry.all_docs()) == [old_ws]
    assert scanner.rescan(7, now=NOW, listening=nobody)["removed"] == [old_ws]


def note(text: str) -> list[dict[str, str]]:
    return [{"id": "n1", "selector": "body", "quote": "x", "note": text}]


def test_a_catch_up_never_forgets_a_protected_document(ws):
    """Each protection alone keeps its document; the unprotected one beside
    them goes, so that nothing here passes by forgetting nothing."""
    old = NOW - 30 * scanner.DAY
    paths = {name: write(ws, f"repo/docs/{name}.html", old)
             for name in ("listened", "delivered", "notes", "plain")}
    ids = {name: registry.register(path)["id"] for name, path in paths.items()}
    registry.update(ids["delivered"], status="delivered")
    registry.replace_annotations(ids["notes"], note("not sent yet"))
    result = scanner.rescan(7, now=NOW, listening=lambda i: i == ids["listened"])
    assert result["removed"] == [ids["plain"]]
    reasons = {item["id"]: item["reason"] for item in result["kept"]}
    assert reasons == {ids["listened"]: "a session listens for its notes",
                       ids["delivered"]: "the session is on it",
                       ids["notes"]: "notes wait to be sent"}
    assert set(registry.all_docs()) == set(reasons)
    assert registry.list_annotations(ids["notes"])[0]["note"] == "not sent yet"
    assert registry.protection({"listening": False, "status": "answered", "exists": True,
                                "pending": 0}) == ""


def test_a_missing_file_is_always_taken_even_with_every_protection(ws):
    """Decision of 2026-10-20: "file missing" wins over every other label. A
    document whose file is gone goes at the rescan, whatever its window (it
    has no date), even when a session listens, it is delivered, and notes
    wait. Its twin with the file still there stays: the protections hold."""
    fresh = NOW - scanner.DAY                               # inside any window
    gone = write(ws, "repo/docs/gone.html", fresh)
    twin = write(ws, "repo/docs/twin.html", fresh)
    ids = {p.name: registry.register(p)["id"] for p in (gone, twin)}
    for doc_id in ids.values():
        registry.update(doc_id, status="delivered")
        registry.replace_annotations(doc_id, note("not sent yet"))
    gone.unlink()
    result = scanner.rescan(7, now=NOW, listening=set(ids.values()).__contains__)
    assert result["removed"] == [ids["gone.html"]] and result["kept"] == []
    assert list(registry.all_docs()) == [ids["twin.html"]]
    assert registry.protection({"exists": False, "listening": True, "status": "delivered",
                                "pending": 3}) == ""
    assert registry.protection({"exists": True, "listening": True}) != ""


def test_adding_a_folder_never_unmanages_an_older_document(ws, tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    old = registry.register(write(reports, "old.html", NOW - 30 * scanner.DAY))["id"]
    write(reports, "new.html", NOW - scanner.DAY)
    monkeypatch.setattr(scanner.time, "time", lambda: NOW)
    folders.add(str(reports))
    catchups = scanner.CatchUps(nobody)
    assert catchups.start(7, only=str(reports.resolve())) is True      # as the page adds one
    last = _settle(catchups)["last"]
    assert (last["added"], last["removed"], last["prune"]) == (1, 0, False)
    assert old in registry.all_docs()
    assert catchups.start(7, prune=True) is True                        # as Rescan does
    last = _settle(catchups)["last"]
    assert (last["added"], last["removed"], last["prune"]) == (0, 1, True)
    assert old not in registry.all_docs()


@pytest.mark.parametrize(("raw", "days"), [(None, 7.0), ("", 7.0), (1, 1.0), ("30", 30.0)])
def test_the_window_of_a_catch_up(raw, days):
    assert scanner.check_days(raw) == days


@pytest.mark.parametrize("raw", [0, -1, "x", True, scanner.MAX_DAYS + 1])
def test_a_bad_window_is_refused(raw):
    with pytest.raises(scanner.ScanError):
        scanner.check_days(raw)


def _settle(catchups: scanner.CatchUps) -> dict:
    deadline = time.monotonic() + 10
    while catchups.state()["running"] is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    return catchups.state()


def test_one_catch_up_at_a_time_and_the_last_one_is_reported(ws, monkeypatch):
    gate = threading.Event()
    real = scanner.catch_up

    def held(days, only=None, now=None):
        gate.wait(10)
        return real(days, only, now=NOW)

    monkeypatch.setattr(scanner, "catch_up", held)
    write(ws, "repo/docs/a.html", NOW - scanner.DAY)
    write(ws, "repo/docs/b.html", NOW - 2 * scanner.DAY)
    catchups = scanner.CatchUps(nobody)
    assert catchups.start(7) is True
    assert catchups.start(7) is False                 # a second click
    assert catchups.state()["running"]["days"] == 7
    gate.set()
    state = _settle(catchups)
    assert state["running"] is None and state["last"]["added"] == 2
    assert state["last"]["error"] == ""
    for added in (0, 0):                              # third and later runs
        assert catchups.start(7) is True
        assert _settle(catchups)["last"]["added"] == added


def test_a_failing_catch_up_is_reported_not_raised(ws, monkeypatch):
    def broken(days, only=None, now=None):
        raise OSError("disk hiccup")

    monkeypatch.setattr(scanner, "catch_up", broken)
    catchups = scanner.CatchUps(nobody)
    catchups.start(7)
    assert _settle(catchups)["last"]["error"] == "disk hiccup"
    assert catchups.start(7) is True                  # not stuck "running"
