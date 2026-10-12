"""Outside WSL: native Linux and macOS open the browser and the terminal tab
their own way, and a machine without a workspace still finds its documents.

The suite runs as WSL unless a test says otherwise (conftest sets
`ANNOTATE_PLATFORM`); every test here sets the platform it describes, so
that the answers are the same on WSL, on a Linux CI runner and on macOS.
"""
from __future__ import annotations

import sys
import time

import pytest

from annotate import browser, cli, config, folders, scanner, terminal
from fakes import read_log, write_fake_claude


# ---------------------------------------------------------------- detection

def test_the_platform_is_detected_and_can_be_forced(monkeypatch):
    monkeypatch.delenv("ANNOTATE_PLATFORM")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(config, "kernel_release", lambda: "6.6.87.2-microsoft-standard-WSL2")
    assert config.platform() == "wsl"
    monkeypatch.setattr(config, "kernel_release", lambda: "6.8.0-45-generic")
    assert config.platform() == "linux"
    monkeypatch.setenv("WSL_DISTRO_NAME", "Some-Distro")     # systemd drops it, a shell has it
    assert config.platform() == "wsl"
    monkeypatch.delenv("WSL_DISTRO_NAME")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert config.platform() == "macos"
    monkeypatch.setattr(sys, "platform", "win32")
    assert config.platform() == "other"
    monkeypatch.setenv("ANNOTATE_PLATFORM", "LINUX")
    assert config.platform() == "linux"
    monkeypatch.setenv("ANNOTATE_PLATFORM", "beos")          # unknown: detected again
    assert config.platform() == "other"


# ------------------------------------------------------------------ browser

def which_of(found: dict[str, str]):
    return lambda name: found.get(name)


@pytest.mark.parametrize("platform, opener", [("linux", "xdg-open"), ("macos", "open")])
def test_a_native_platform_opens_the_url_with_its_own_opener_only(monkeypatch, platform,
                                                                  opener):
    monkeypatch.setenv("ANNOTATE_PLATFORM", platform)
    # The Windows openers may be on the PATH of a WSL-like machine: never used here.
    monkeypatch.setattr(browser.shutil, "which", which_of(
        {opener: f"/usr/bin/{opener}", "cmd.exe": "/w/cmd.exe", "wslview": "/u/wslview"}))
    assert browser.openers("http://x") == [[f"/usr/bin/{opener}", "http://x"]]
    monkeypatch.setattr(browser.shutil, "which", which_of({}))
    with pytest.raises(browser.OpenError, match=opener):
        browser.open_url("http://x")


def test_open_without_a_document_opens_the_home_page(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(browser, "open_url", lambda url: opened.append(url) or "xdg-open")
    assert cli.main(["open"]) == 0
    assert opened == [f"http://127.0.0.1:{config.DEFAULT_PORT}/"]
    assert cli.main(["open", "nosuchdoc"]) == 1             # an id still has to exist
    assert "nosuchdoc" in capsys.readouterr().err


# ----------------------------------------------------------------- terminal

def test_linux_opens_the_first_native_terminal_found(tmp_path, monkeypatch):
    monkeypatch.setenv("ANNOTATE_PLATFORM", "linux")
    script = terminal.write_launch("d1", str(tmp_path), "claude", None, "x")
    monkeypatch.setattr(terminal.shutil, "which", which_of(
        {"konsole": "/usr/bin/konsole", "xterm": "/usr/bin/xterm", "wt.exe": "/w/wt.exe"}))
    assert terminal.command("d1", script, None) == [
        "/usr/bin/konsole", "-e", "bash", "-l", str(script)]
    monkeypatch.setattr(terminal.shutil, "which", which_of(
        {"gnome-terminal": "/usr/bin/gnome-terminal", "xterm": "/usr/bin/xterm"}))
    assert terminal.command("d1", script, None) == [
        "/usr/bin/gnome-terminal", "--", "bash", "-l", str(script)]
    monkeypatch.setattr(terminal.shutil, "which", which_of({"wt.exe": "/w/wt.exe"}))
    with pytest.raises(terminal.TerminalError, match="terminal"):
        terminal.command("d1", script, None)


def test_macos_runs_the_script_in_terminal_app(tmp_path, monkeypatch):
    monkeypatch.setenv("ANNOTATE_PLATFORM", "macos")
    script = terminal.write_launch("d1", str(tmp_path), "claude", None, "x")
    monkeypatch.setattr(terminal.shutil, "which", which_of({"osascript": "/usr/bin/osascript"}))
    argv = terminal.command("d1", script, None)
    assert argv[0] == "/usr/bin/osascript"
    assert f'do script "bash -l {script}"' in argv[2]


def test_an_unknown_platform_needs_the_terminal_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("ANNOTATE_PLATFORM", "other")
    script = terminal.write_launch("d1", str(tmp_path), "claude", None, "x")
    with pytest.raises(terminal.TerminalError, match="set `terminal`"):
        terminal.command("d1", script, None)
    assert terminal.command("d1", script, None, ("my-term", "-x", "{script}")) == [
        "my-term", "-x", str(script)]
    with pytest.raises(terminal.TerminalError, match="{script}"):
        terminal.command("d1", script, None, ("my-term",))


@pytest.mark.parametrize("value", ['"kitty"', '["kitty", 3]', '["", "{script}"]'])
def test_a_malformed_terminal_setting_is_refused(tmp_path, monkeypatch, value):
    cfg = tmp_path / "annotate-config.toml"
    cfg.write_text(f"terminal = {value}\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="terminal"):
        config.load_config()


def test_the_configured_terminal_runs_the_session_for_real(tmp_path, monkeypatch):
    """End to end on a native platform: the template from config.toml opens
    a real program, which runs the real script, which starts the (fake)
    claude with the prompt as one argument."""
    monkeypatch.setenv("ANNOTATE_PLATFORM", "linux")
    fake, log = write_fake_claude(tmp_path / "bin")
    term = tmp_path / "bin" / "fake-term"
    term.write_text("#!/bin/sh\nexec bash \"$1\"\n", encoding="utf-8")
    term.chmod(0o755)
    (tmp_path / "annotate-config.toml").write_text(
        f'terminal = ["{term}", "{{script}}"]\n', encoding="utf-8")
    used = terminal.open_session("d1", str(tmp_path), str(fake), "sess-3", "fix it")
    assert used == "fake-term"
    assert read_log(log) == [{"argv": ["--resume", "sess-3", "fix it"], "cwd": str(tmp_path)}]


# ---------------------------------------------------------- tracked folders

def test_without_a_workspace_the_home_docs_are_documents_but_never_walked(tmp_path):
    assert not config.workspace().exists()
    home = folders.home()
    assert folders.load() == [{"path": str(home), "docs_only": True, "scan": False}]
    doc = home / "project" / "docs" / "note.html"
    doc.parent.mkdir(parents=True)
    doc.write_text("<p>x</p>", encoding="utf-8")
    assert folders.wanted(doc)                                # the hook takes it
    assert not folders.wanted(home / "project" / "src" / "page.html")
    assert folders.roots(folders.load()) == []                # the pass walks nothing
    assert scanner.candidates(folders.load(), time.time() - 3600) == []


def test_a_never_walked_entry_survives_the_list_being_saved(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    folders.add(str(reports))
    assert folders.load() == [{"path": str(folders.home()), "docs_only": True, "scan": False},
                              {"path": str(reports.resolve()), "docs_only": False}]
    assert folders.roots(folders.load()) == [reports.resolve()]


def test_with_a_workspace_the_default_is_unchanged(tmp_path):
    config.workspace().mkdir()
    assert folders.load() == [{"path": str(config.workspace().resolve()), "docs_only": True}]

