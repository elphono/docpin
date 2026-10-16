"""The mutation campaign's anchors, checked by the default suite.

tools/mutate.py applies each mutation only when its turn comes: a stale anchor
stops the whole campaign there, after every run before it. The campaign checks
them first too (`stale_anchors`), and never runs this file on a mutated copy. Measured on
2026-10-14: the 0.4.0 picker code had moved under `picker-attaches-nothing`,
and the campaign stopped after an hour, the browser mutations behind it never
run.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _mutate_module():
    spec = importlib.util.spec_from_file_location("mutate_tool", ROOT / "tools" / "mutate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["mutate_tool"] = module          # the dataclasses look it up there
    spec.loader.exec_module(module)
    return module


def test_every_mutation_anchor_is_found_exactly_once():
    mutate = _mutate_module()
    assert len(mutate.MUTATIONS) > 50
    assert mutate.stale_anchors(ROOT) == {}
    assert len({m.name for m in mutate.MUTATIONS}) == len(mutate.MUTATIONS)


def test_the_campaign_never_runs_the_anchor_test_on_a_mutated_copy():
    # Every mutated copy has lost its anchor: this file would kill them all.
    assert "--ignore=tests/test_mutate.py" in (ROOT / "tools" / "mutate.py").read_text(encoding="utf-8")
