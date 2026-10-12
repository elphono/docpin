"""The visual-first plugin's document check: what it covers, what it says,
and the hook run as Claude Code runs it (a process, JSON on stdin).

The hook asks docpin what is a document (`annotate is-document`); the tests
point it at a fake through `VISUAL_FIRST_ANNOTATE`, or at nothing at all for
the fallback rule, and once at the real command.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from annotate import cli, folders

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "plugins" / "visual-first" / "hooks" / "check-document.py"
_spec = importlib.util.spec_from_file_location("check_document", HOOK)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

SVG = '<svg viewBox="0 0 10 10"><rect width="5" height="5"/></svg>'


def page(body: str, head: str = "") -> str:
    return f"{head}<!doctype html><html><body>{body}</body></html>"


@pytest.fixture
def no_docpin(monkeypatch):
    monkeypatch.setenv("VISUAL_FIRST_ANNOTATE", "")
    monkeypatch.setattr(check.shutil, "which", lambda name: None)


def fake_docpin(tmp_path: Path, code: int) -> Path:
    fake = tmp_path / "fake-annotate"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path}/asked\nexit {code}\n",
                    encoding="utf-8")
    fake.chmod(0o755)
    return fake


# ------------------------------------------------------------------ coverage

@pytest.mark.parametrize("relative, expected", [
    ("docs/guide.html", True),
    ("docs/notes/state.md", True),
    ("sub/docs/x.html", True),
    ("src/page.html", False),
    ("docs/README.md", False),
    ("docs/plans/next.md", False),
    ("docs/figures/hero/tpl.html", False),
    ("node_modules/pkg/docs/a.html", False),
    ("docs/data.json", False),
])
def test_without_docpin_a_docs_folder_of_the_project_is_the_rule(tmp_path, no_docpin,
                                                                  relative, expected):
    assert check.covered(tmp_path / relative, tmp_path) is expected


def test_outside_the_project_nothing_is_covered_without_docpin(tmp_path, no_docpin):
    assert not check.covered(tmp_path / "elsewhere" / "docs" / "a.html", tmp_path / "proj")


@pytest.mark.parametrize("code, expected", [(0, True), (1, False)])
def test_docpin_decides_when_it_is_installed(tmp_path, monkeypatch, code, expected):
    monkeypatch.setenv("VISUAL_FIRST_ANNOTATE", str(fake_docpin(tmp_path, code)))
    target = tmp_path / "reports" / "weekly.html"            # no docs/ on the way
    assert check.covered(target, tmp_path) is expected
    assert (tmp_path / "asked").read_text().split() == ["is-document", str(target)]
    assert not check.covered(tmp_path / "docs" / "README.md", tmp_path)   # names stay out


def test_a_broken_docpin_falls_back_to_the_rule(tmp_path, monkeypatch):
    monkeypatch.setenv("VISUAL_FIRST_ANNOTATE", str(fake_docpin(tmp_path, 7)))
    assert check.covered(tmp_path / "docs" / "a.html", tmp_path)
    assert not check.covered(tmp_path / "reports" / "a.html", tmp_path)


def test_docpin_answers_from_its_tracked_folders_for_html_and_markdown(tmp_path, capsys):
    reports = tmp_path / "reports"
    reports.mkdir()
    folders.add(str(reports))
    assert cli.main(["is-document", str(reports / "weekly.md")]) == 0
    assert cli.main(["is-document", str(reports / "weekly.html")]) == 0
    assert cli.main(["is-document", str(tmp_path / "src" / "page.html")]) == 1
    assert capsys.readouterr().out.split("\n")[:3] == ["document", "document",
                                                       "not a document"]


# ------------------------------------------------------------------- verdicts

def test_markdown_is_blocked_and_html_without_a_visual_too():
    assert check.verdict(Path("a.md"), "# Title\n\ntext\n")[0] == "block"
    status, reason = check.verdict(Path("a.html"), page("<p>only words</p>"))
    assert status == "block" and "no visual" in reason


@pytest.mark.parametrize("visual", [SVG, '<img src="x.png" alt="">', "<table><tr><td>1</td></tr></table>",
                                    '<canvas id="c"></canvas>', '<pre class="mermaid">flowchart TD</pre>'])
def test_each_kind_of_visual_counts(visual):
    assert check.verdict(Path("a.html"), page(visual + "<p>one</p>"))[0] == "ok"


def test_more_than_four_paragraphs_per_visual_warns():
    assert check.verdict(Path("a.html"), page(SVG + "<p>x</p>" * 4))[0] == "ok"
    status, reason = check.verdict(Path("a.html"), page(SVG + "<p>x</p>" * 5))
    assert status == "warning" and "5 paragraphs for 1 visual" in reason


@pytest.mark.parametrize("marker", ["<!-- visual: exemption - raw data -->",
                                    "<!-- visuel : dérogation — transcription -->"])
def test_an_exemption_in_the_first_lines_disarms_it(marker):
    assert check.verdict(Path("a.md"), f"{marker}\n# Raw\n")[0] == "exempt"
    late = "\n".join(["line"] * 6) + f"\n{marker}\n"
    assert check.verdict(Path("a.md"), late)[0] == "block"


def test_a_heavy_image_warns_until_git_tracks_it(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "big.png").write_bytes(b"\0" * 1_200_000)
    (tmp_path / "small.png").write_bytes(b"\0" * 1000)       # the witness
    text = page('<img src="big.png" alt=""><img src="small.png" alt="">')
    status, reason = check.verdict(tmp_path / "a.html", text)
    assert status == "warning" and "big.png weighs 1.2 MB" in reason
    assert "small.png" not in reason
    subprocess.run(["git", "-C", str(tmp_path), "add", "big.png"], check=True)
    assert check.verdict(tmp_path / "a.html", text)[0] == "ok"


# --------------------------------------------------------- the hook, for real

def run_hook(payload: object, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK)], input=data, text=True,
                          capture_output=True, timeout=30,
                          env={**os.environ, "VISUAL_FIRST_ANNOTATE": "", **env})


def write_payload(path: Path, cwd: Path, tool: str = "Write") -> dict[str, object]:
    return {"tool_name": tool, "cwd": str(cwd), "tool_input": {"file_path": str(path)}}


def test_the_hook_blocks_warns_or_stays_silent(tmp_path):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    md = tmp_path / "docs" / "note.md"
    md.parent.mkdir()
    md.write_text("# Note\n", encoding="utf-8")
    done = run_hook(write_payload(md, tmp_path), env)
    assert done.returncode == 2 and "Markdown deliverable" in done.stderr and not done.stdout
    chatty = tmp_path / "docs" / "chatty.html"
    chatty.write_text(page(SVG + "<p>x</p>" * 9), encoding="utf-8")
    done = run_hook(write_payload(chatty, tmp_path, "Edit"), env)
    context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
    assert done.returncode == 0 and "9 paragraphs" in context
    fine = tmp_path / "docs" / "fine.html"
    fine.write_text(page(SVG + "<p>x</p>"), encoding="utf-8")
    assert run_hook(write_payload(fine, tmp_path), env).stdout == ""
    code = tmp_path / "src" / "notes.md"                     # not a deliverable
    code.parent.mkdir()
    code.write_text("# x\n", encoding="utf-8")
    assert run_hook(write_payload(code, tmp_path), env).returncode == 0


def test_the_hook_never_breaks_a_session(tmp_path):
    env = {"HOME": str(tmp_path)}
    for payload in ("not json", "[]", {"tool_name": "Read"},
                    write_payload(tmp_path / "docs" / "missing.md", tmp_path)):
        done = run_hook(payload, env)
        assert done.returncode == 0 and done.stdout == "" and done.stderr == ""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "visual-first-off").touch()
    md = tmp_path / "docs" / "note.md"
    md.parent.mkdir()
    md.write_text("# Note\n", encoding="utf-8")
    assert run_hook(write_payload(md, tmp_path), env).returncode == 0


def test_the_hook_asks_the_real_docpin(tmp_path):
    """The command docpin's plugin puts on the PATH, launched for real: a
    folder tracked in docpin is covered even without a docs/ on the way."""
    reports = tmp_path / "reports"
    reports.mkdir()
    folders.add(str(reports))
    md = reports / "weekly.md"
    md.write_text("# Weekly\n", encoding="utf-8")
    done = run_hook(write_payload(md, tmp_path),
                    {"VISUAL_FIRST_ANNOTATE": str(ROOT / "bin" / "annotate"),
                     "ANNOTATE_AUTOSTART": "0"})
    assert done.returncode == 2, done.stderr
