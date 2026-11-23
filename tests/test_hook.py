"""The Claude Code hook, run as Claude Code runs it: a process, JSON on stdin.

A fake `annotate` on the PATH records its arguments; nothing real is
registered.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "register-on-write.py"


@pytest.fixture
def run_hook(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    subagents = tmp_path / "subagents.jsonl"   # ANNOTATE_SUBAGENT, per call
    fake = bin_dir / "annotate"
    answer = tmp_path / "answer.txt"     # what the fake prints, if present
    fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    f"open({str(subagents)!r}, 'a').write("
                    f"json.dumps(os.environ.get('ANNOTATE_SUBAGENT')) + '\\n')\n"
                    f"if os.path.exists({str(answer)!r}):\n"
                    f"    sys.stdout.write(open({str(answer)!r}).read())\n")
    fake.chmod(0o755)
    workspace = tmp_path / "workspace"
    outputs: list[str] = []
    # A copy of the hook, alone in its folder: next to the real one sits the
    # plugin's bin/annotate, which the hook prefers to the fake on the PATH.
    hook = tmp_path / "alone" / "hooks" / HOOK.name
    hook.parent.mkdir(parents=True)
    hook.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")

    def run(payload, extra_env=None, prints=None):
        if prints is not None:
            answer.write_text(prints)
        env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
               **(extra_env or {})}
        data = payload if isinstance(payload, str) else json.dumps(payload)
        done = subprocess.run([sys.executable, str(hook)], input=data, text=True,
                              capture_output=True, env=env, timeout=30)
        assert done.returncode == 0, done.stderr
        outputs.append(done.stdout)
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    def seen_subagents():
        if not subagents.exists():
            return []
        return [json.loads(line) for line in subagents.read_text().splitlines()]

    run.outputs = outputs  # type: ignore[attr-defined]
    run.subagents = seen_subagents  # type: ignore[attr-defined]
    run.bin_dir = bin_dir  # type: ignore[attr-defined]
    return run, workspace


def payload(path, session="sess-abc", cwd="/somewhere/repo"):
    return {"session_id": session, "cwd": cwd, "hook_event_name": "PostToolUse",
            "tool_name": "Write", "tool_input": {"file_path": str(path)}}


def test_an_html_doc_under_docs_is_registered_with_its_session(run_hook):
    run, ws = run_hook
    doc = ws / "misc" / "proj" / "docs" / "specs" / "design.html"
    assert run(payload(doc)) == [
        ["register", str(doc), "--session", "sess-abc", "--cwd", "/somewhere/repo",
         "--hook"]]


def test_only_html_files_reach_annotate(run_hook):
    run, ws = run_hook
    assert run(payload(ws / "proj" / "docs" / "notes.md")) == []
    assert run(payload(ws / "proj" / "docs" / "x.htm")) == []


def test_every_html_file_is_handed_over_and_annotate_decides(run_hook):
    """The tracked folders change from the index page: the hook keeps no copy
    of the rule (folders.wanted, asked by `register --hook`)."""
    run, ws = run_hook
    elsewhere = Path("/tmp/elsewhere/report.html")
    assert run(payload(elsewhere)) == [
        ["register", str(elsewhere), "--session", "sess-abc", "--cwd", "/somewhere/repo",
         "--hook"]]


def test_a_relative_path_is_taken_from_the_session_folder(run_hook):
    run, ws = run_hook
    assert run(payload(Path("docs/x.html"), cwd="/somewhere/repo"))[0][1] == \
        "/somewhere/repo/docs/x.html"


def test_the_context_annotate_prints_reaches_the_session(run_hook):
    run, ws = run_hook
    doc = ws / "proj" / "docs" / "x.html"
    context = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": "run annotate wait d1"}}
    run(payload(doc), prints=json.dumps(context))
    assert json.loads(run.outputs[-1]) == context


@pytest.mark.parametrize("printed", ["{}", "not json", "[1, 2]", ""])
def test_nothing_but_a_non_empty_object_is_forwarded(run_hook, printed):
    run, ws = run_hook
    run(payload(ws / "proj" / "docs" / "x.html"), prints=printed)
    assert run.outputs[-1] == ""


def test_garbage_on_stdin_never_fails_the_tool_call(run_hook):
    run, ws = run_hook
    assert run("not json at all") == []
    assert run({"tool_input": "nope"}) == []
    assert run({"session_id": "s"}) == []


def test_a_missing_session_does_not_clear_the_recorded_one(run_hook):
    run, ws = run_hook
    doc = ws / "proj" / "docs" / "x.html"
    assert run(payload(doc, session="")) == [
        ["register", str(doc), "--cwd", "/somewhere/repo", "--hook"]]


def subagent_payload(path, agent_id="agent-7f3a", agent_type="general-purpose"):
    """What Claude Code gives a hook that runs in a subagent: the parent's
    session id, plus `agent_id` and `agent_type`."""
    return {**payload(path), "agent_id": agent_id, "agent_type": agent_type}

def test_a_subagent_write_is_said_to_annotate_through_the_environment(run_hook):
    """The arguments stay those of a main session's write: only the variable
    differs, which an older `annotate` ignores."""
    run, ws = run_hook
    doc = ws / "proj" / "docs" / "x.html"
    assert run(subagent_payload(doc)) == [
        ["register", str(doc), "--session", "sess-abc", "--cwd", "/somewhere/repo",
         "--hook"]]
    assert run.subagents() == ["agent-7f3a"]

def test_a_main_session_write_says_nothing_of_a_subagent(run_hook):
    """Not even a variable left in the hook's own environment reaches annotate."""
    run, ws = run_hook
    doc = ws / "proj" / "docs" / "x.html"
    run(payload(doc))
    run(payload(doc), extra_env={"ANNOTATE_SUBAGENT": "stale"})
    run({**payload(doc), "agent_id": ""})
    run({**payload(doc), "agent_id": 12})
    assert run.subagents() == [None, None, None, None]

STRICT_OLD_ANNOTATE = """\
import argparse, json, os, sys
# The `register` arguments of docpin 0.9.4, and nothing else: argparse exits
# with 2 on anything unknown, as the real older version would.
top = argparse.ArgumentParser(prog="annotate")
sub = top.add_subparsers(dest="command", required=True)
reg = sub.add_parser("register")
reg.add_argument("path")
reg.add_argument("--session")
reg.add_argument("--cwd")
reg.add_argument("--hook", action="store_true")
args = top.parse_args()
open(os.environ["STRICT_MARK"], "a").write(args.path + "\\n")
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                  "additionalContext": "registered by the older version"}}))
"""

def test_an_older_annotate_still_registers_a_subagents_document(run_hook, tmp_path):
    """The launcher runs `runtime/current`, which may be older than the hook:
    a strict 0.9.4-like `annotate` must register the subagent's document and
    its answer reach the subagent."""
    run, ws = run_hook
    strict = run.bin_dir / "annotate"
    strict.write_text(f"#!{sys.executable}\n{STRICT_OLD_ANNOTATE}")
    strict.chmod(0o755)
    mark = tmp_path / "registered.txt"
    env = {**os.environ, "STRICT_MARK": str(mark)}
    # The witness: this fake does refuse what it does not know.
    refused = subprocess.run([str(strict), "register", "x.html", "--subagent"], env=env,
                             capture_output=True, text=True, timeout=30)
    assert refused.returncode == 2 and not mark.exists()
    doc = ws / "proj" / "docs" / "x.html"
    run(subagent_payload(doc), extra_env={"STRICT_MARK": str(mark)})
    assert mark.read_text().splitlines() == [str(doc)]
    assert json.loads(run.outputs[-1])["hookSpecificOutput"]["additionalContext"] == \
        "registered by the older version"
