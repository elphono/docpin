"""The Claude Code plugin: its manifests, its launcher, the stable copy of the
code it runs, and the daemon it starts on demand.

The launcher is run for real (`bin/annotate`, a real `python3`), in the
test's data folder. Its on-demand start is turned off (`ANNOTATE_AUTOSTART=0`)
except in the one test that starts a real daemon, on a free port, from this
process, so that the suite can stop it.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

import annotate
from annotate import client, config, runtime, server, service

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "bin" / "annotate"


# ---------------------------------------------------------------- manifests

def test_every_manifest_carries_the_package_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    assert annotate.__version__ == version
    manifest = ROOT / ".claude-plugin" / "plugin.json"
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == version


def test_the_marketplace_lists_docpin_alone_from_this_repository():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
    entries = {e["name"]: e["source"] for e in market["plugins"]}
    assert entries == {"docpin": "./"}          # visual-first moved to eli on 2026-10-23
    for source in entries.values():
        assert (ROOT / source / ".claude-plugin" / "plugin.json").is_file()


def test_the_plugin_hook_runs_the_registering_script_of_the_plugin():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    [entry] = hooks["hooks"]["PostToolUse"]
    assert entry["matcher"] == "Write|Edit"
    [hook] = entry["hooks"]
    assert hook["command"] == '"${CLAUDE_PLUGIN_ROOT}"/hooks/register-on-write.py'
    assert hook["timeout"] > 20                 # the script bounds itself to 20 s
    for script in (ROOT / "hooks" / "register-on-write.py", LAUNCHER):
        assert os.access(script, os.X_OK), script


# ------------------------------------------------------------- the launcher

def launch(*args: str, launcher: Path = LAUNCHER, env: dict[str, str] | None = None
           ) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(launcher), *args], capture_output=True,
                          text=True, timeout=60,
                          env={**os.environ, "ANNOTATE_AUTOSTART": "0", **(env or {})})


def copies() -> list[str]:
    return sorted(p.name for p in runtime.runtime_dir().iterdir()
                  if p.is_dir() and not p.is_symlink() and p != runtime.current())


def test_the_launcher_runs_a_copy_of_the_code_outside_the_plugin_folder():
    done = launch("list")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "no document registered"
    stamp = runtime.fingerprint(ROOT / "src" / "annotate")
    assert copies() == [stamp]
    assert runtime.named() == stamp
    assert (runtime.current() / "annotate" / "static" / "annotate.js").is_file()
    launch("list")
    assert copies() == [stamp]                       # a second run reuses the copy
    version = launch("--version").stdout.replace("\n", "").strip()  # argparse wraps it
    assert version == f"docpin {annotate.__version__} ({runtime.runtime_dir() / stamp / 'annotate'})"


def plugin_version(tmp_path: Path, name: str, change: str) -> Path:
    """A copy of the plugin whose overlay differs by one comment: a version
    of its own, as an update or an edited clone gives."""
    root = tmp_path / name
    shutil.copytree(ROOT / "src" / "annotate", root / "src" / "annotate",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (root / "bin").mkdir()
    shutil.copy2(LAUNCHER, root / "bin" / "annotate")
    js = root / "src" / "annotate" / "static" / "annotate.js"
    js.write_text(js.read_text(encoding="utf-8") + f"\n// {change}\n", encoding="utf-8")
    return root / "bin" / "annotate"


def test_a_new_version_gets_a_copy_of_its_own_and_three_are_kept(tmp_path):
    seen = []
    for round_ in range(4):
        launcher = plugin_version(tmp_path, f"v{round_}", f"version {round_}")
        assert launch("list", launcher=launcher).returncode == 0
        stamp = runtime.fingerprint(launcher.parent.parent / "src" / "annotate")
        assert runtime.named() == stamp
        seen.append(stamp)
        time.sleep(0.02)                             # distinct folder dates
    assert len(set(seen)) == 4
    assert copies() == sorted(seen[1:])              # the oldest one is pruned


def test_a_copy_survives_the_plugin_folder_being_deleted(tmp_path):
    """What Claude Code does 14 days after an update."""
    launcher = plugin_version(tmp_path, "v1", "about to be deleted")
    assert launch("list", launcher=launcher).returncode == 0
    shutil.rmtree(tmp_path / "v1")
    assert (runtime.current() / "annotate" / "static" / "index.html").is_file()
    done = subprocess.run([sys.executable, "-P", "-m", "annotate", "list"], cwd=tmp_path,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(runtime.current())})
    assert done.returncode == 0 and "no document" in done.stdout


def hook_context(doc: Path, env: dict[str, str]) -> str:
    done = launch("register", str(doc), "--session", "sess-1", "--cwd", str(doc.parent),
                  "--hook", env={"ANNOTATE_PORT": str(free_port()), **env})
    assert done.returncode == 0, done.stderr
    return str(json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"])


def test_the_plugin_hook_tells_the_session_to_run_annotate_by_name(make_doc, tmp_path):
    doc = make_doc("<p>x</p>")
    context = hook_context(doc, {"CLAUDE_PLUGIN_ROOT": str(ROOT)})
    assert "`annotate wait " in context                # bin/ is on the session's PATH
    assert str(ROOT) not in context                    # never the plugin's own folder
    # The same launcher found on the PATH is called by name as well.
    path = f"{ROOT / 'bin'}:{os.environ['PATH']}"
    assert "`annotate wait " in hook_context(doc, {"PATH": path})


def test_a_launcher_off_the_path_is_called_back_by_its_own_path(make_doc):
    """A clone whose hook comes from the user's settings: no plugin, so its
    bin/ is not on the session's PATH, and a bare `annotate` would not run."""
    doc = make_doc("<p>x</p>")
    context = hook_context(doc, {})
    assert f"`{LAUNCHER} wait " in context


def test_a_project_folder_named_annotate_cannot_stand_in_for_the_package(tmp_path):
    fake = tmp_path / "annotate"
    fake.mkdir()
    (fake / "__init__.py").write_text("", encoding="utf-8")
    (fake / "__main__.py").write_text("print('hijacked')\n", encoding="utf-8")
    done = subprocess.run([sys.executable, str(LAUNCHER), "list"], cwd=tmp_path,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "ANNOTATE_AUTOSTART": "0"})
    assert "hijacked" not in done.stdout and done.returncode == 0, done.stdout + done.stderr


# --------------------------------------------------- staleness of the daemon

def test_a_daemon_running_a_copy_turns_stale_when_a_new_version_is_installed(tmp_path):
    first = runtime.install(ROOT / "src" / "annotate")
    watched = runtime.watched(first / "annotate")
    assert watched == runtime.current() / "annotate"
    before = server.code_fingerprint(watched)
    launcher = plugin_version(tmp_path, "v2", "next release")
    (launcher.parent.parent / "src" / "annotate" / "server.py").write_text(
        (ROOT / "src" / "annotate" / "server.py").read_text(encoding="utf-8") + "\n# v2\n",
        encoding="utf-8")
    runtime.install(launcher.parent.parent / "src" / "annotate")
    assert server.code_fingerprint(watched) != before
    # A daemon run from a clone watches its own folder, as before the plugin.
    assert runtime.watched(ROOT / "src" / "annotate") == ROOT / "src" / "annotate"


def test_install_refuses_a_folder_that_is_not_the_package(tmp_path):
    with pytest.raises(runtime.LaunchError, match="not the annotate package"):
        runtime.install(tmp_path)


# --------------------------------------------------------- on-demand start

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_a_running_daemon_is_never_started_twice(monkeypatch):
    monkeypatch.setattr(client, "call", lambda *a, **k: {"docs": []})
    started = []
    assert client.ensure_daemon(1, lambda: started.append(1) or "process") == "running"
    assert started == []


def test_a_daemon_that_never_answers_is_said_not_waited_for_in_silence(monkeypatch):
    started = []
    with pytest.raises(client.DaemonUnreachable):
        client.ensure_daemon(free_port(), lambda: started.append(1) or "process", wait=0.3)
    assert started == [1]


def test_the_on_demand_start_runs_a_real_daemon_that_answers(tmp_path, monkeypatch, request):
    port = free_port()
    monkeypatch.setenv("ANNOTATE_PORT", str(port))
    monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "no-unit.service")
    spawned_before = set(request.node.spawned_pids)
    try:
        assert client.ensure_daemon(port, service.start_daemon, wait=20) == "started (process)"
        assert client.call(port, "GET", "/api/docs")["docs"] == []
    finally:
        for pid in set(request.node.spawned_pids) - spawned_before:
            os.kill(pid, 15)
    assert (config.data_dir() / "daemon.log").is_file()


def test_with_a_unit_the_daemon_is_started_by_systemd(tmp_path, monkeypatch):
    unit = tmp_path / "annotate.service"
    unit.write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    calls = []

    class Recorder:
        def __init__(self, argv, **kwargs):
            calls.append(argv)

    monkeypatch.setattr(service.subprocess, "Popen", Recorder)
    assert service.start_daemon() == "systemd"
    assert calls == [["systemctl", "--user", "--no-block", "start", service.UNIT_NAME]]


def test_the_unit_written_from_the_plugin_runs_the_stable_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("ANNOTATE_LAUNCHER", "annotate")
    monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "annotate.service")
    text = service.write_unit().read_text(encoding="utf-8")
    assert f"ExecStart={sys.executable} -P -m annotate serve" in text
    assert f"Environment=PYTHONPATH={runtime.current()}" in text
    # Under the plugin, the interpreter is the system's; here it is the suite's.
    rest = text.replace(sys.executable, "<python3>")
    assert "/.claude/plugins/cache/" not in rest and str(ROOT) not in rest
    monkeypatch.delenv("ANNOTATE_LAUNCHER")
    text = service.write_unit().read_text(encoding="utf-8")
    assert "PYTHONPATH" not in text and " serve" in text   # a clone keeps its venv


def test_the_session_command_is_the_bare_name_under_the_launcher(monkeypatch):
    monkeypatch.setenv("ANNOTATE_LAUNCHER", "annotate")
    assert config.annotate_command() == "annotate"
    monkeypatch.delenv("ANNOTATE_LAUNCHER")
    assert config.annotate_command().endswith("annotate")


@pytest.mark.parametrize("argv, starts", [
    (["wait", "DOC"], True), (["open"], True), (["send", "DOC"], True),
    (["register", "DOC", "--hook"], True),
    (["register", "/elsewhere/page.html", "--hook"], False),   # not a document
    (["status"], False), (["list"], False)])
def test_the_launcher_starts_the_daemon_only_for_commands_that_need_it(
        make_doc, monkeypatch, argv, starts):
    from annotate import cli
    doc = make_doc("<p>x</p>")
    argv = [str(doc) if a == "DOC" else a for a in argv]
    started = []
    monkeypatch.setattr(client, "ensure_daemon",
                        lambda port, start: started.append(port) or "running")
    for command in ("cmd_wait", "cmd_open", "cmd_send", "cmd_register", "cmd_status",
                    "cmd_list"):
        monkeypatch.setattr(cli, command, lambda args, cfg: 0)
    cli.main(argv)
    assert started == []                             # off unless the launcher says so
    monkeypatch.setenv("ANNOTATE_AUTOSTART", "1")
    cli.main(argv)
    assert started == ([config.DEFAULT_PORT] if starts else [])


def test_the_tray_shortcut_names_a_plain_file_that_every_install_refreshes(tmp_path,
                                                                          monkeypatch):
    """The Startup shortcut outlives every update. A dated copy is pruned
    three versions later, and Windows does not traverse `current` (measured:
    a reparse point through wsl.localhost): the shortcut names a plain file."""
    from annotate import tray
    runtime.install(ROOT / "src" / "annotate")
    dated = (runtime.current() / "annotate").resolve()          # what __file__ resolves to
    script = tray.script(dated)
    assert script == runtime.tray_script()
    assert script.resolve() == script                          # no symlink on the way
    assert script.read_bytes() == (ROOT / "src" / "annotate" / "windows" / "tray.ps1").read_bytes()
    named: list[str] = []

    def windows_path(path: Path) -> str:                        # wslpath -w resolves links
        assert isinstance(path, Path)
        named.append(str(path.resolve()))
        return r"\\wsl.localhost\D\tray.ps1"
    monkeypatch.setattr(tray, "CODE_DIR", dated)
    monkeypatch.setattr(tray, "windows_path", windows_path)
    monkeypatch.setenv("WSL_DISTRO_NAME", "D")
    tray.shortcut_script(8765)
    assert named == [str(script)]
    # A new version brings its own script: the same file carries it.
    launcher = plugin_version(tmp_path, "v2", "next release")
    new_ps1 = launcher.parent.parent / "src" / "annotate" / "windows" / "tray.ps1"
    new_ps1.write_text(new_ps1.read_text(encoding="utf-8") + "\n# v2\n", encoding="utf-8")
    runtime.install(launcher.parent.parent / "src" / "annotate")
    assert script.read_text(encoding="utf-8").endswith("# v2\n")
    # A clone names its own file, as before the plugin.
    clone = ROOT / "src" / "annotate"
    assert tray.script(clone) == clone / "windows" / "tray.ps1"
