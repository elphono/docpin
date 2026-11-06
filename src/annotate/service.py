"""The systemd user unit of the daemon, generated from this machine's environment.

Pattern taken from remarkable-sync, `rmpapier/cli.py::_cmd_service`, with the
lesson of 2026-09-15 applied from the start: **the unit runs the entry point
of the project's virtualenv DIRECTLY, never `uv run`**. Under systemd,
`KillMode=control-group` sends SIGTERM to the whole group, and `uv run`
forwards its own to the child: Python received two (10 times out of 15), and
the duplicate ended in exit 143 or in a deadlock then SIGKILL.

Every path comes from the environment at generation time: the interpreter
running this command (hence the venv, or for the plugin the system's
`python3` on the stable copy of runtime.py), `claude`, `cmd.exe`, `explorer.exe` and
`wt.exe` on the PATH. Nothing is written in the code.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

UNIT_NAME = "annotate.service"


class ServiceError(Exception):
    """The unit cannot be generated on this machine."""


def from_plugin() -> bool:
    """True when the plugin's `bin/annotate` launched this process: the
    code then lives in the stable copy (runtime.py), never in a virtualenv."""
    return bool(os.environ.get("ANNOTATE_LAUNCHER"))


def entry_point() -> Path:
    entry = Path(sys.executable).parent / "annotate"
    if not entry.is_file():
        raise ServiceError(
            f"{entry} not found: `service` must run with the project's "
            f"interpreter (`uv run annotate service`), otherwise the unit "
            f"would start a missing file")
    return entry


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def search_path() -> str:
    """PATH of the unit: where `claude`, the Windows openers and Windows
    Terminal live, then the system directories. `wt.exe` sits in
    `WindowsApps`, which no other tool shares: without it the daemon falls
    back to a plain console window (terminal.py)."""
    dirs: list[str] = []
    for tool in ("claude", "wslview", "cmd.exe", "explorer.exe", "wt.exe"):
        found = shutil.which(tool)
        if found and str(Path(found).parent) not in dirs:
            dirs.append(str(Path(found).parent))
    for system in ("/usr/local/bin", "/usr/bin", "/bin"):
        if system not in dirs:
            dirs.append(system)
    return ":".join(dirs)


def unit_text(entry: Path, project: Path, path_env: str,
              python_path: Path | None = None) -> str:
    """The unit. `entry` is the virtualenv's `annotate`, or, for the plugin
    (`python_path` set), the system's interpreter, run on the stable copy
    that `runtime.current()` names: never on the plugin's version folder."""
    if python_path is None:
        start, extra = f"{entry} serve", ""
    else:
        start = f"{entry} -P -m annotate serve"
        extra = f"Environment=PYTHONPATH={python_path}\nEnvironment=ANNOTATE_LAUNCHER=annotate\n"
    return f"""[Unit]
Description=docpin daemon (annotate HTML documents, send notes to Claude Code)
Documentation=https://github.com/elphono/docpin
After=default.target

[Service]
Type=simple
WorkingDirectory={project}
Environment=PATH={path_env}
{extra}# The daemon DIRECTLY, never under `uv run`: uv forwards the SIGTERM systemd
# already sends to the whole group (remarkable-sync, measured 2026-09-15).
ExecStart={start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


CONTROL_VERBS = ("restart", "stop")


def control(verb: str) -> None:
    """Ask systemd to restart or stop THIS daemon (the web page's buttons).

    `--no-block`: systemd takes the job and the command returns at once. The
    job then kills this process, and the command with it if it were still
    waiting; started in its own session, it does not even share our signals.
    Refused when the daemon was not started by systemd (`INVOCATION_ID`
    unset): `systemctl` would act on a unit that is not this process.
    `start` is not offered: a stopped daemon serves no page to click it on.
    """
    if verb not in CONTROL_VERBS:
        raise ServiceError(f"unknown daemon action {verb!r}")
    if not os.environ.get("INVOCATION_ID"):
        raise ServiceError("this daemon was not started by systemd (`annotate serve` "
                           "by hand?): stop it where it runs")
    try:
        subprocess.Popen(["systemctl", "--user", "--no-block", verb, UNIT_NAME],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise ServiceError(f"cannot run systemctl: {exc}") from exc


def write_unit() -> Path:
    target = unit_path()
    if from_plugin():
        from . import config, runtime
        text = unit_text(Path(sys.executable), config.data_dir(), search_path(),
                         python_path=runtime.current())
    else:
        project = Path(__file__).resolve().parent.parent.parent
        text = unit_text(entry_point(), project, search_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def start_daemon() -> str:
    """Start the daemon in the background, for the plugin's on-demand start
    (decision Q5): through systemd when the unit exists, else as a process of
    its own session, logging to `daemon.log` of the data folder. Returns how.

    Under the plugin, the process runs the copy `runtime.current()` names,
    as the unit does, never the launcher's own: a launcher of a lower version
    (runtime.py) would otherwise start a lower daemon."""
    from . import config, runtime
    env = {**os.environ, "PYTHONPATH": str(runtime.current())} if from_plugin() else None
    if unit_path().is_file():
        try:
            subprocess.Popen(["systemctl", "--user", "--no-block", "start", UNIT_NAME],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            raise ServiceError(f"cannot run systemctl: {exc}") from exc
        return "systemd"
    folder = config.data_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "daemon.log", "ab") as log:
            subprocess.Popen([sys.executable, "-P", "-m", "annotate", "serve"],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             cwd=folder, start_new_session=True, env=env)
    except OSError as exc:
        raise ServiceError(f"cannot start the daemon: {exc}") from exc
    return "process"


# ------------------------------------------- the daemon of the on-demand start

PROC_TCP = Path("/proc/net/tcp")
STOP_WAIT = 10.0


def on_demand_daemon(port: int) -> int | None:
    """The pid of the daemon `start_daemon` started as a plain process on
    `port`: one of this user's, listening on 127.0.0.1:`port`, whose command is
    `<python> [-P] -m annotate serve`, and not under systemd. None for anything
    else: a daemon started by hand (`annotate serve`, `uv run annotate serve`)
    is the user's, and a session start must never stop it."""
    for pid in _listeners(port):
        if pid > 1 and pid != os.getpid() and _on_demand_command(_command(pid)) \
                and not _under_systemd(pid):
            return pid
    return None


def stop_daemon_process(pid: int, port: int, wait: float = STOP_WAIT) -> None:
    """SIGTERM to that daemon, then wait until nothing listens on `port`. Never
    a group (`killpg`), never pid 0 or 1, never this process."""
    if pid <= 1 or pid == os.getpid():
        raise ServiceError(f"refusing to signal pid {pid}")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait
    while _answers(port):
        if time.monotonic() > deadline:
            raise ServiceError(f"the daemon (pid {pid}) still listens on :{port} "
                               f"after {wait:.0f} s")
        time.sleep(0.1)


def _answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _on_demand_command(argv: list[str]) -> bool:
    return (len(argv) >= 4 and argv[-3:] == ["-m", "annotate", "serve"]
            and Path(argv[0]).name.startswith("python"))


def _listeners(port: int) -> list[int]:
    """The pids of this user's processes listening on 127.0.0.1:`port`: from
    /proc on Linux, from `lsof` elsewhere (macOS)."""
    if PROC_TCP.is_file():
        return _proc_listeners(port)
    try:
        done = subprocess.run(
            ["lsof", "-nP", "-t", "-a", "-u", str(os.getuid()),
             f"-iTCP@127.0.0.1:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [int(word) for word in done.stdout.split() if word.isdigit()]


def _proc_listeners(port: int) -> list[int]:
    sockets = set()
    for line in PROC_TCP.read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A" or fields[7] != str(os.getuid()):
            continue                                   # 0A: LISTEN
        address, _, hex_port = fields[1].partition(":")
        host = socket.inet_ntoa(int(address, 16).to_bytes(4, sys.byteorder))
        if host == "127.0.0.1" and int(hex_port, 16) == port:
            sockets.add(f"socket:[{fields[9]}]")
    pids: list[int] = []
    if not sockets:
        return pids
    proc = PROC_TCP.parent.parent
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            links = [os.readlink(fd) for fd in (entry / "fd").iterdir()]
        except OSError:                                # another user's, or gone
            continue
        if sockets.intersection(links):
            pids.append(int(entry.name))
    return pids


def _command(pid: int) -> list[str]:
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        if PROC_TCP.is_file():
            return [part for part in cmdline.read_bytes().decode(errors="replace")
                    .split("\0") if part]
        done = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5)
        return done.stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return []


def _under_systemd(pid: int) -> bool:
    """systemd marks what it starts with INVOCATION_ID; its daemon is restarted
    through systemd, never signalled from here. No /proc, no systemd."""
    try:
        return b"\0INVOCATION_ID=" in b"\0" + Path(f"/proc/{pid}/environ").read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return True                                    # unreadable: not ours to stop
