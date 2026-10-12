"""Configuration: where the data lives, which port the daemon listens on.

Nothing about the deployment is written in the code (no home directory, no
distribution name, no port): every value comes from the environment, from
`~/.config/annotate/config.toml`, or from a default that is not tied to a
machine. A test walks the AST of the package to keep it that way.

Precedence, highest first: environment variable, config file, default.

| Field          | Env variable             | Default                          |
|----------------|--------------------------|----------------------------------|
| port           | ANNOTATE_PORT            | 8765                             |
| claude_bin     | ANNOTATE_CLAUDE          | "claude" (resolved on the PATH)  |
| data_dir       | ANNOTATE_DATA_DIR        | ~/.local/share/annotate          |
| workspace      | ANNOTATE_WORKSPACE       | ~/workspace (the default tracked folder) |
| terminal       | (config file only)       | none: the platform's terminal (terminal.py) |
| (config file)  | ANNOTATE_CONFIG          | ~/.config/annotate/config.toml   |
| (platform)     | ANNOTATE_PLATFORM        | detected: wsl, linux, macos (`platform()`) |

`claude_bin` is what the terminal tab runs (resolved to an absolute path by
the daemon, see terminal.py); the tests point it at names that are not
`claude`, which the suite guard refuses.
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8765


class ConfigError(Exception):
    """The configuration file or an environment override is invalid."""


@dataclass(frozen=True)
class Config:
    port: int = DEFAULT_PORT
    claude_bin: str = "claude"
    terminal: tuple[str, ...] = ()


def config_path() -> Path:
    env = os.environ.get("ANNOTATE_CONFIG")
    if env:
        return Path(env)
    return Path.home() / ".config" / "annotate" / "config.toml"


def data_dir() -> Path:
    env = os.environ.get("ANNOTATE_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "annotate"


def workspace() -> Path:
    """The default tracked folder (folders.py): its `docs/` folders, until
    the user changes the list."""
    env = os.environ.get("ANNOTATE_WORKSPACE")
    if env:
        return Path(env)
    return Path.home() / "workspace"


PLATFORMS = ("wsl", "linux", "macos", "other")


def platform() -> str:
    """Which kind of machine this runs on. Three things depend on it, and
    nothing else: how a URL reaches the browser (browser.py), how a terminal
    tab opens (terminal.py), and whether the WSL distribution is needed.

    | Kind    | Detected by                                             |
    |---------|---------------------------------------------------------|
    | `wsl`   | `WSL_DISTRO_NAME`, or "microsoft" in the kernel release |
    | `macos` | `sys.platform == "darwin"` (not measured)               |
    | `linux` | any other Linux                                         |
    | `other` | anything else: no browser or terminal is guessed        |

    `ANNOTATE_PLATFORM` overrides the detection: the tests use it, and so can
    a user whose machine is detected wrongly."""
    forced = os.environ.get("ANNOTATE_PLATFORM", "").strip().lower()
    if forced in PLATFORMS:
        return forced
    if sys.platform == "darwin":
        return "macos"
    if not sys.platform.startswith("linux"):
        return "other"
    if os.environ.get("WSL_DISTRO_NAME"):
        return "wsl"
    return "wsl" if "microsoft" in kernel_release().lower() else "linux"


def kernel_release() -> str:
    try:
        return Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return ""


def annotate_command() -> str:
    """How a session calls `annotate`: the bare name when the plugin's
    `bin/annotate` launched this process (it is on the session's PATH, and
    the plugin's folder changes with every version), else the absolute path
    of the entry point of the running interpreter's environment (the
    project's virtualenv), else the bare name."""
    launcher = os.environ.get("ANNOTATE_LAUNCHER", "").strip()
    if launcher:
        return launcher
    entry = Path(sys.executable).parent / "annotate"
    return str(entry) if entry.is_file() else "annotate"


def _as_int(name: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer, got {value!r}")
    try:
        number = int(str(value).strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {number}")
    return number


def load_config() -> Config:
    """Read the config file (optional), then apply environment overrides."""
    raw: dict[str, object] = {}
    path = config_path()
    if path.is_file():
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc

    port: object = raw.get("port", DEFAULT_PORT)
    claude: object = raw.get("claude_bin", "claude")

    port = os.environ.get("ANNOTATE_PORT", port)
    claude = os.environ.get("ANNOTATE_CLAUDE", claude)

    if not isinstance(claude, str) or not claude.strip():
        raise ConfigError(f"claude_bin must be a non-empty string, got {claude!r}")
    terminal: object = raw.get("terminal", [])
    if not (isinstance(terminal, list) and all(isinstance(t, str) and t for t in terminal)):
        raise ConfigError(f"terminal must be a list of non-empty strings, got {terminal!r}")
    return Config(port=_as_int("port", port, 1, 65535),
                  claude_bin=claude.strip(), terminal=tuple(terminal))
