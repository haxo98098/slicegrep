"""Hang regressions.

A retrieval tool that returns the wrong slices is annoying. One that never
returns freezes the agent calling it, and Python's `re` has no timeout, so
a single bad pattern pegs a core forever. These patterns were measured
hanging (197s of CPU on a 200-char line) before the guard existed; each test
has a wall-clock assertion so a regression fails instead of hanging CI.
"""
import time

import pytest

from slicegrep.core import _MAX_LINE_CHARS, focused_read


# (a+)+ is the classic; the others are the shapes an LLM actually writes.
REDOS_PATTERNS = [
    r"(a+)+$",
    r"(a*)*b",
    r"(\d+)*x",
    r"(\w+\s?)+!",
    r"([a-z]+)+@",
]


@pytest.fixture
def bomb(tmp_path):
    """A file whose content maximises backtracking: long run, no match."""
    p = tmp_path / "bomb.py"
    p.write_text("x = '" + "a" * 200 + "!'\n" * 5, encoding="utf-8")
    return p


@pytest.mark.parametrize("pattern", REDOS_PATTERNS)
def test_redos_patterns_rejected_fast(bomb, pattern):
    t0 = time.time()
    with pytest.raises(ValueError, match="nested quantifier"):
        focused_read(str(bomb), pattern, budget=800)
    assert time.time() - t0 < 2.0, "rejection must be immediate, not attempted"


def test_safe_quantifiers_still_work(bomb):
    """The guard must not reject ordinary patterns."""
    for ok in [r"a+", r"x\s*=", r"\d{2,4}", r"(foo|bar)", r"def\s+\w+"]:
        focused_read(str(bomb), ok, budget=800)      # must not raise


def test_nested_quantifier_inside_alternation_is_defused(tmp_path):
    """A dangerous fragment joined with a safe one must not hang the query."""
    p = tmp_path / "m.py"
    p.write_text("def compute_total():\n    return 1\n" +
                 "y = '" + "a" * 200 + "!'\n", encoding="utf-8")
    t0 = time.time()
    r = focused_read(str(p), r"compute_total|(a+)+$", budget=800)
    assert time.time() - t0 < 5.0
    assert any("compute_total" in c.code for c in r.chunks), \
        "the safe half of the query must still return its match"


def test_pathological_long_line_skipped(tmp_path):
    """Minified bundles: one enormous line must not dominate match time."""
    p = tmp_path / "bundle.js"
    huge = "a" * (_MAX_LINE_CHARS * 4)
    p.write_text(f"function realTarget() {{ return 1 }}\nvar x='{huge}';\n",
                 encoding="utf-8")
    t0 = time.time()
    r = focused_read(str(p), r"realTarget|a{3,}", budget=800)
    assert time.time() - t0 < 5.0
    assert any("realTarget" in c.code for c in r.chunks)


def test_hook_times_out_instead_of_blocking(tmp_path, monkeypatch):
    """The hook must abandon slow retrieval rather than hold up the Read."""
    from slicegrep import hook

    monkeypatch.setattr(hook, "TIMEOUT", 0.4)

    def slow(*a, **k):
        time.sleep(30)

    import slicegrep.core as core
    monkeypatch.setattr(core, "focused_read", slow)

    f = tmp_path / "x.py"
    f.write_text("def a():\n    return 1\n" * 500, encoding="utf-8")
    t0 = time.time()
    out = hook._slices(f, ["something"])
    elapsed = time.time() - t0
    assert out == {}
    assert elapsed < 3.0, f"hook waited {elapsed:.1f}s on a hung retrieval"
