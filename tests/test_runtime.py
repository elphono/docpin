"""`current` never goes back to a lower version (src/annotate/runtime.py).

Measured three times on 2026-10-19 and 20: a Claude Code session opened before
an update keeps the `bin/annotate` of the older version on its PATH, and one
`annotate status` there pointed `runtime/current` back at that version's copy;
the next restart of the systemd unit ran the older daemon.

The older launchers cannot be fixed: their code is frozen. So these tests run
THAT code, not a double of it: `tests/legacy/` holds, byte for byte, the
`runtime.py` and `bin/annotate` of every release up to 0.5.1 (three distinct
`runtime.py`, one `bin/annotate`), checked against their sha256. They are
stored as `.txt` so that nobody "fixes" them and no tool lints them.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

import annotate
from annotate import cli, client, config, runtime, server, service

ROOT = Path(__file__).resolve().parent.parent
LEGACY = Path(__file__).resolve().parent / "legacy"
# file: (a release that shipped it, sha256 of the released file)
LEGACY_RUNTIMES = {
    "runtime-0.3.3.py.txt": ("0.3.3", "273b168521c35e8d8d96e33a1581ced3bec8d5fa175c918002747fef5b1f4adf"),
    "runtime-0.3.4.py.txt": ("0.3.4", "6b7c942fbe94d9fb170347f855669f801e1d61cc971e454e514a3b4ae43db3ca"),
    "runtime-0.3.5-to-0.5.1.py.txt": (
        "0.4.0", "a6e518b0924296c3d7f3a45196594d5d62666924d7000191bdfb4b2ea61e0f25"),
}
LAST_LINK_RELEASE = "runtime-0.3.5-to-0.5.1.py.txt"     # 0.5.1: `current` was a link
LEGACY_BIN = ("bin-annotate-0.3.3-to-0.5.1.py.txt",
              "a8f9584f01f3a25cc6617aedadae51c1bf48168f791b97e0dc5e31199b61b2e3")


def test_the_frozen_files_are_the_released_ones():
    for name, (_, digest) in [*LEGACY_RUNTIMES.items(), (LEGACY_BIN[0], ("", LEGACY_BIN[1]))]:
        assert hashlib.sha256((LEGACY / name).read_bytes()).hexdigest() == digest, name


def legacy_runtime(name: str) -> types.ModuleType:
    """The frozen `runtime.py`, run as it ran: inside the `annotate` package,
    on this suite's `config` (whose `data_dir` is the same as then)."""
    module = types.ModuleType("annotate._legacy_" + re.sub(r"\W", "_", name))
    module.__package__ = "annotate"
    module.__file__ = str(LEGACY / name)
    exec(compile((LEGACY / name).read_text(encoding="utf-8"), str(LEGACY / name), "exec"),
         module.__dict__)
    return module


def release(tmp_path: Path, name: str, version: str, legacy: str | None = None) -> Path:
    """The `annotate` package of a plugin version folder: this one at
    `version`, its icon script marked with `name`; with `legacy`, the frozen
    `runtime.py` and `bin/annotate` in place of today's."""
    package = tmp_path / name / "src" / "annotate"
    shutil.copytree(ROOT / "src" / "annotate", package,
                    ignore=shutil.ignore_patterns("__pycache__"))
    init = package / "__init__.py"
    line = f'__version__ = "{annotate.__version__}"'
    assert init.read_text(encoding="utf-8").count(line) == 1
    init.write_text(init.read_text(encoding="utf-8").replace(line, f'__version__ = "{version}"'),
                    encoding="utf-8")
    tray = package / "windows" / "tray.ps1"
    tray.write_text(tray.read_text(encoding="utf-8") + f"\n# {name}\n", encoding="utf-8")
    launcher = tmp_path / name / "bin" / "annotate"
    launcher.parent.mkdir()
    if legacy:
        (package / "runtime.py").write_bytes((LEGACY / legacy).read_bytes())
        launcher.write_bytes((LEGACY / LEGACY_BIN[0]).read_bytes())
    else:
        shutil.copy2(ROOT / "bin" / "annotate", launcher)
    return package


def version_named() -> tuple[int, ...] | None:
    return runtime.version_of(runtime.current() / "annotate")


def what_the_unit_runs() -> str:
    """The unit's `ExecStart`: `python3 -P -m annotate serve` on
    `PYTHONPATH=runtime/current`; here, the version that import gives."""
    done = subprocess.run([sys.executable, "-P", "-c", "import annotate; print(annotate.__version__)"],
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(runtime.current())})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def tray_of(name: str) -> bool:
    return runtime.tray_script().read_text(encoding="utf-8").endswith(f"\n# {name}\n")


# ------------------------------------------- the defect, with the frozen code

@pytest.mark.parametrize("legacy", sorted(LEGACY_RUNTIMES))
def test_while_current_was_a_link_an_older_launcher_took_it_back(tmp_path, legacy):
    """The witness: the frozen code does reproduce the defect, so its failure
    below is the folder's doing, not a loader that runs nothing."""
    legacy_runtime(LAST_LINK_RELEASE).install(release(tmp_path, "v051", "0.5.1", LAST_LINK_RELEASE))
    assert os.path.islink(runtime.current()) and version_named() == (0, 5, 1)
    old_version = LEGACY_RUNTIMES[legacy][0]
    legacy_runtime(legacy).install(release(tmp_path, "old", old_version, legacy))
    assert runtime.version_of(runtime.current() / "annotate") == tuple(
        int(p) for p in old_version.split("."))
    assert what_the_unit_runs() == old_version


# --------------------------------------------- the folder that stops them all

@pytest.mark.parametrize("legacy", sorted(LEGACY_RUNTIMES))
def test_an_older_launcher_cannot_take_current_back(tmp_path, legacy):
    # The machine as 0.5.1 left it: `current` a link.
    legacy_runtime(LAST_LINK_RELEASE).install(release(tmp_path, "v051", "0.5.1", LAST_LINK_RELEASE))
    new = runtime.install(release(tmp_path, "v052", "0.5.2"))
    assert runtime.current().is_dir() and not runtime.current().is_symlink()
    assert runtime.named() == new.name and what_the_unit_runs() == "0.5.2"
    watched = runtime.watched(new / "annotate")
    daemon_code = server.code_fingerprint(watched)
    assert tray_of("v052")
    old = legacy_runtime(legacy)
    with pytest.raises(old.LaunchError, match="Is a directory"):
        old.install(release(tmp_path, "old", LEGACY_RUNTIMES[legacy][0], legacy))
    assert runtime.named() == new.name and what_the_unit_runs() == "0.5.2"
    assert server.code_fingerprint(watched) == daemon_code      # the daemon is not stale
    assert tray_of("v052")                                      # nor the icon's script older
    assert (new / "annotate" / "cli.py").is_file()              # nor its copy pruned


@pytest.mark.parametrize("legacy", sorted(LEGACY_RUNTIMES))
def test_the_real_older_launcher_stops_with_an_error_and_changes_nothing(tmp_path, legacy):
    new = runtime.install(release(tmp_path, "v052", "0.5.2"))
    package = release(tmp_path, "old", LEGACY_RUNTIMES[legacy][0], legacy)
    done = subprocess.run([sys.executable, str(package.parent.parent / "bin" / "annotate"),
                           "--version"], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "ANNOTATE_AUTOSTART": "0"})
    assert done.returncode == 1, done.stdout + done.stderr
    assert done.stderr.startswith(f"error: cannot install the code under {runtime.runtime_dir()}: ")
    assert "Is a directory" in done.stderr and done.stdout == ""
    assert runtime.named() == new.name and what_the_unit_runs() == "0.5.2"
    # The link it made before giving up is swept by the next launcher of 0.5.2.
    assert list(runtime.runtime_dir().glob(".current-*"))
    runtime.install(release(tmp_path, "v052b", "0.5.2"))
    assert not list(runtime.runtime_dir().glob(".current-*"))


def test_the_folder_says_why_it_is_one():
    runtime.install(ROOT / "src" / "annotate")
    text = (runtime.current() / runtime.NOTICE).read_text(encoding="utf-8")
    assert "Is a directory" in text and "restart that session" in text


def test_a_deleted_folder_is_made_again_on_the_highest_version(tmp_path):
    """The weak spot, said in the notice: with the folder gone, an older
    launcher makes its link again; the next launcher of 0.5.2 takes it back."""
    new = runtime.install(release(tmp_path, "v052", "0.5.2"))
    shutil.rmtree(runtime.current())
    old = legacy_runtime(LAST_LINK_RELEASE)
    old.install(release(tmp_path, "old", "0.4.0", LAST_LINK_RELEASE))
    assert what_the_unit_runs() == "0.4.0"
    runtime.install(release(tmp_path, "v052b", "0.5.2"))
    assert runtime.current().is_dir() and not runtime.current().is_symlink()
    assert what_the_unit_runs() == "0.5.2" and runtime.named() != new.name


# ------------------------------------------- 0.5.2 on: never a lower version

def test_a_lower_version_runs_its_own_copy_and_leaves_current_alone(tmp_path):
    high = runtime.install(release(tmp_path, "v060", "0.6.0"))
    low = runtime.install(release(tmp_path, "v052", "0.5.2"))
    assert low != high and (low / "annotate" / "cli.py").is_file()   # the CLI runs its own
    assert runtime.named() == high.name and what_the_unit_runs() == "0.6.0"
    assert tray_of("v060")


def test_the_copy_current_names_is_never_pruned(tmp_path):
    high = runtime.install(release(tmp_path, "v060", "0.6.0"))
    lows = []
    for round_ in range(4):                      # four lower copies, all newer on disk
        time.sleep(0.02)
        lows.append(runtime.install(release(tmp_path, f"low{round_}", "0.5.2")))
    assert runtime.named() == high.name and (high / "annotate" / "cli.py").is_file()
    assert not lows[0].exists()                  # the pruning did happen, on the others
    assert what_the_unit_runs() == "0.6.0"


def test_the_copy_current_names_is_kept_even_ranked_below_copies_of_its_version(tmp_path):
    """The ranking by version alone protects the highest copy, not the named
    one: a clone of 0.6.0 flipped back to its oldest content leaves two 0.6.0
    copies above the one `current` names, then a lower launcher prunes
    (mutation `prune-takes-the-named-copy` survived the test above once the
    ranking by version made it protect the named copy by itself)."""
    clones = [release(tmp_path, name, "0.6.0") for name in ("a", "b", "c")]
    for package in clones:
        runtime.install(package)
        time.sleep(0.02)                         # distinct folder dates
    named = runtime.install(clones[0])           # back to the oldest on disk
    assert runtime.named() == named.name
    runtime.install(release(tmp_path, "low", "0.5.2"))
    assert runtime.named() == named.name and (named / "annotate" / "cli.py").is_file()
    assert what_the_unit_runs() == "0.6.0"


def test_the_copies_older_launchers_leave_go_before_the_one_a_daemon_still_runs(tmp_path):
    """Each older launcher makes its copy, then the folder refuses it: those
    copies are the newest on disk, and nothing will ever run them."""
    running = runtime.install(release(tmp_path, "v051", "0.5.1"))  # until the restart
    new = release(tmp_path, "v052", "0.5.2")
    runtime.install(new)
    for legacy, (version, _) in sorted(LEGACY_RUNTIMES.items()):
        time.sleep(0.02)
        old = legacy_runtime(legacy)
        with pytest.raises(old.LaunchError):
            old.install(release(tmp_path, f"old-{version}", version, legacy))
    assert len(copies()) == 5                    # 0.5.1, 0.5.2 and three left behind
    runtime.install(new)                         # the next launch of 0.5.2 prunes
    assert (running / "annotate" / "cli.py").is_file()
    assert runtime.named() == runtime.fingerprint(new)
    assert len(copies()) == runtime.KEEP


def copies() -> list[Path]:
    return [p for p in runtime.runtime_dir().iterdir()
            if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
            and p != runtime.current()]


def test_a_higher_version_takes_current_each_time(tmp_path):
    """The witness of the guard, three times, an older launcher refused in
    between each: 0.10.0 is above 0.9.0 (numbers, not text)."""
    old = legacy_runtime(LAST_LINK_RELEASE)
    for round_, version in enumerate(("0.5.2", "0.9.0", "0.10.0")):
        copy = runtime.install(release(tmp_path, f"v{round_}", version))
        assert runtime.named() == copy.name and what_the_unit_runs() == version
        assert tray_of(f"v{round_}")
        with pytest.raises(old.LaunchError):
            old.install(release(tmp_path, f"old{round_}", "0.4.0", LAST_LINK_RELEASE))
        assert runtime.named() == copy.name


def test_the_same_version_with_another_content_takes_current(tmp_path):
    """A clone loaded in place (a local marketplace, `--plugin-dir`) changes
    without a new version."""
    runtime.install(release(tmp_path, "a", "0.5.2"))
    second = runtime.install(release(tmp_path, "b", "0.5.2"))
    assert runtime.named() == second.name and tray_of("b")


def test_the_on_demand_start_runs_the_copy_current_names_not_the_launchers(tmp_path,
                                                                           monkeypatch):
    """Without a unit (macOS, no systemd), the launcher starts the daemon
    itself: a launcher of 0.5.2 must not start a 0.5.2 daemon over a 0.6.0."""
    runtime.install(release(tmp_path, "v060", "0.6.0"))
    low = runtime.install(release(tmp_path, "v052", "0.5.2"))
    monkeypatch.setenv("ANNOTATE_LAUNCHER", "annotate")
    monkeypatch.setenv("PYTHONPATH", str(low))             # what bin/annotate of 0.5.2 sets
    monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "no-unit.service")
    calls = []

    class Recorder:
        def __init__(self, argv, **kwargs):
            assert isinstance(argv, list) and "env" in kwargs
            calls.append((argv, kwargs))

    with monkeypatch.context() as patched:
        patched.setattr(service.subprocess, "Popen", Recorder)
        assert service.start_daemon() == "process"
    [(argv, kwargs)] = calls
    assert argv[-1] == "serve"
    done = subprocess.run([*argv[:-1], "--version"], capture_output=True, text=True,
                          timeout=60, env=kwargs["env"])
    assert done.stdout.startswith("docpin 0.6.0 "), done.stdout + done.stderr


def test_a_held_lock_leaves_current_alone_and_the_launcher_still_runs(tmp_path, monkeypatch):
    first = runtime.install(release(tmp_path, "v052", "0.5.2"))
    higher = release(tmp_path, "v060", "0.6.0")
    monkeypatch.setattr(runtime, "LOCK_WAIT", 0.2)
    with open(runtime.runtime_dir() / ".lock", "a", encoding="utf-8") as other:
        fcntl.flock(other, fcntl.LOCK_EX)        # a launcher frozen while it held it
        copy = runtime.install(higher)
        assert (copy / "annotate" / "cli.py").is_file()
        assert runtime.named() == first.name
    assert runtime.install(higher) == copy and runtime.named() == copy.name


# --------------------------------------------------------- what status says

def test_status_says_which_docpin_the_daemon_runs_and_whether_it_is_stale(monkeypatch, capsys):
    answers = iter([{"docs": [], "version": "0.5.2", "stale": False},
                    {"docs": [], "version": "0.5.2", "stale": True},
                    {"docs": []}])                     # a daemon of 0.5.1 says neither
    monkeypatch.setattr(client, "call", lambda *a, **k: next(answers))
    cfg = config.load_config()
    lines = []
    for _ in range(3):
        assert cli.cmd_status(None, cfg) == 0  # type: ignore[arg-type]
        lines.append(capsys.readouterr().out.strip())
    assert lines[0] == f"daemon up on :{cfg.port}, docpin 0.5.2, 0 document(s)"
    assert lines[1].startswith(lines[0] + "; stale: ") and "restart it" in lines[1]
    assert lines[2] == f"daemon up on :{cfg.port}, 0 document(s)"
