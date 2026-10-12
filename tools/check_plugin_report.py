"""Read the JSON report of `claude plugin validate --json .` and fail on any
error, or on any warning but the one this repository accepts: its CLAUDE.md
sits at the plugin root because it is the guide of whoever changes the code,
not context for the plugin's users.

    claude plugin validate --json . > report.json; python3 tools/check_plugin_report.py report.json
"""
from __future__ import annotations

import json
import sys

ACCEPTED = ("CLAUDE.md at the plugin root is not loaded as project context",)


class ReportError(Exception):
    """The validation report says something is wrong."""


def problems(report: dict) -> list[str]:
    found = []
    sections = [report.get("manifest") or {}, *(report.get("contents") or [])]
    if not report.get("success"):
        found.append("validation failed")
    for section in sections:
        where = section.get("file", "?")
        for error in section.get("errors") or []:
            found.append(f"error in {where}: {error.get('message')}")
        for warning in section.get("warnings") or []:
            message = str(warning.get("message", ""))
            if not message.startswith(ACCEPTED):
                found.append(f"warning in {where}: {message}")
    return found


def main(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        found = problems(json.load(handle))
    for line in found:
        print(line, file=sys.stderr)
    print("plugin report: " + ("ok" if not found else f"{len(found)} problem(s)"))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
