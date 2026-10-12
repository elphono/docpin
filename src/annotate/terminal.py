"""Open an interactive Claude Code session in a terminal TAB of the user.

Used when the user sends notes and no open session listens for the document
(see listeners.py): the producing session is resumed where the user can see
it and keep talking to it, instead of running in the background
(`claude -p`), which is what the first real send did on 2026-10-05: the user
saw nothing at all.

    wt.exe -w 0 new-tab --title annotate-<id> -- wsl.exe -d <distro> -e bash -l <script>

Three measured constraints shape the call:

- **a TAB of the most recent window (`-w 0 new-tab`), never a new window**:
  the user's Windows Terminal is set to `launchMode: fullscreen`, which only
  applies to new windows (remarkable-sync, 2026-09-17);
- **no `;` anywhere in the arguments**: it is wt's command separator, and a
  `bash -c 'a; b'` was cut in two on 2026-10-05 (the first half ran, the file
  it wrote never appeared). Everything therefore goes into a script file, and
  the arguments are plain words;
- **`bash -l`**: `wsl.exe -e` starts no login shell, so `~/.local/bin`
  (where `claude` lives) is not on the PATH. The script also calls `claude`
  by the absolute path the daemon resolved.

Outside WSL (config.platform) there is no Windows Terminal, and the same script
runs in the first native terminal found:

| Platform | Terminal                                                        |
|----------|-----------------------------------------------------------------|
| WSL      | `wt.exe` as above, else `cmd.exe /c start`                      |
| Linux    | `x-terminal-emulator`, `gnome-terminal`, `konsole`, `xterm`     |
| macOS    | Terminal.app through `osascript` (not measured)                 |
| any      | `terminal = ["kitty", "bash", "-l", "{script}"]` in config.toml wins |

The prompt reaches `claude` as ONE argument read from a file
(`"$(cat …)"`): no quoting of user text ever happens on a command line. A
prompt starting with `-` would be read as an option, so the prompt never
starts with one (prompt.build starts with a word).
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import config

# Looked up on the PATH by name; a module constant so that the tests can point
# it at a fake (the suite guard refuses any program named wt.exe).
WT = "wt.exe"

# Native terminals of a Linux desktop, first found wins, with the arguments
# that come before the command they run.
LINUX_TERMINALS = (("x-terminal-emulator", ("-e",)), ("gnome-terminal", ("--",)),
                   ("konsole", ("-e",)), ("xterm", ("-e",)))


class TerminalError(Exception):
    """No terminal could be opened."""


def distro() -> str:
    """The WSL distribution this runs in: `WSL_DISTRO_NAME`, which systemd
    does not pass to the daemon, else the share name of `wslpath -w /`
    (`\\\\wsl.localhost\\<distro>\\`)."""
    name = os.environ.get("WSL_DISTRO_NAME", "").strip()
    if name:
        return name
    try:
        done = subprocess.run(["wslpath", "-w", "/"], capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TerminalError(f"cannot find the WSL distribution: {exc}") from exc
    parts = [p for p in done.stdout.strip().split("\\") if p]
    if done.returncode != 0 or len(parts) < 2:
        raise TerminalError(f"cannot find the WSL distribution from "
                            f"`wslpath -w /`: {done.stdout.strip()!r}")
    return parts[1]


def script_text(cwd: str, claude: str, resume: str | None, prompt_file: Path) -> str:
    command = [claude] + (["--resume", resume] if resume else [])
    return ("#!/bin/bash\n"
            "# Written by annotate (terminal.py): one session, then deleted.\n"
            f"cd {shlex.quote(cwd)} || exit 1\n"
            f"prompt=\"$(cat {shlex.quote(str(prompt_file))})\"\n"
            f"rm -f {shlex.quote(str(prompt_file))} \"$0\"\n"
            f"exec {shlex.join(command)} \"$prompt\"\n")


def write_launch(doc_id: str, cwd: str, claude: str, resume: str | None,
                 prompt: str) -> Path:
    """Write the prompt and the script under the data dir; return the script."""
    folder = config.data_dir() / "launch"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    prompt_file = folder / f"{doc_id}-{stamp}.prompt"
    script = folder / f"{doc_id}-{stamp}.sh"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")
        script.write_text(script_text(cwd, claude, resume, prompt_file), encoding="utf-8")
        script.chmod(0o700)
    except OSError as exc:
        raise TerminalError(f"cannot write the launch script: {exc}") from exc
    if any(c in str(script) for c in " ;\"'"):
        raise TerminalError(f"{script}: the data directory must not contain spaces, "
                            f"quotes or ';' (wt.exe would split the command)")
    return script


def command(doc_id: str, script: Path, wsl_distro: str | None,
            template: tuple[str, ...] = ()) -> list[str]:
    """The argv that opens the tab: the user's `terminal` template if set,
    else the platform's terminal (table above)."""
    if template:
        if not any("{script}" in part for part in template):
            raise TerminalError("the `terminal` setting must contain {script}, where "
                                "the launch script goes")
        return [part.replace("{script}", str(script)) for part in template]
    platform = config.platform()
    if platform == "linux":
        for name, before in LINUX_TERMINALS:
            found = shutil.which(name)
            if found:
                return [found, *before, "bash", "-l", str(script)]
        raise TerminalError("no terminal found on the PATH (x-terminal-emulator, "
                            "gnome-terminal, konsole, xterm): set `terminal` in "
                            f"{config.config_path()}")
    if platform == "macos":
        osascript = shutil.which("osascript")
        if not osascript:
            raise TerminalError("osascript not found: no terminal can be opened")
        # The script path holds no space, quote or ';' (write_launch refuses them).
        return [osascript, "-e", f'tell application "Terminal" to do script "bash -l {script}"',
                "-e", 'tell application "Terminal" to activate']
    if platform != "wsl" or not wsl_distro:
        raise TerminalError(f"no terminal is known for this platform ({platform}): set "
                            f"`terminal` in {config.config_path()}")
    wsl = ["wsl.exe", "-d", wsl_distro, "-e", "bash", "-l", str(script)]
    wt = shutil.which(WT)
    if wt:
        return [wt, "-w", "0", "new-tab", "--title", f"annotate-{doc_id}", "--", *wsl]
    cmd = shutil.which("cmd.exe")
    if cmd:
        # The empty "" is the window title `start` expects before the program.
        return [cmd, "/c", "start", "", *wsl]
    raise TerminalError("neither wt.exe nor cmd.exe is on the PATH: no terminal "
                        "can be opened (rewrite the unit with `annotate service`)")


def open_session(doc_id: str, cwd: str, claude_bin: str, resume: str | None,
                 prompt: str) -> str:
    """Open the tab; return the program used (`wt.exe`, `cmd.exe`, a native
    terminal, or the configured one)."""
    claude = shutil.which(claude_bin) or claude_bin
    if not Path(cwd).is_dir():
        raise TerminalError(f"the session folder {cwd} does not exist")
    try:
        template = config.load_config().terminal
    except config.ConfigError as exc:
        raise TerminalError(str(exc)) from exc
    script = write_launch(doc_id, cwd, claude, resume, prompt)
    on_wsl = not template and config.platform() == "wsl"
    argv = command(doc_id, script, distro() if on_wsl else None, template)
    # cmd.exe refuses a UNC working directory: run Windows programs from their own.
    run_in = os.path.dirname(argv[0]) if on_wsl else None
    try:
        done = subprocess.run(argv, cwd=run_in or None, capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TerminalError(f"cannot start {argv[0]}: {exc}") from exc
    if done.returncode != 0:
        raise TerminalError(f"{os.path.basename(argv[0])} exited {done.returncode}: "
                            f"{(done.stderr or done.stdout).strip()[:300]}")
    return os.path.basename(argv[0])
