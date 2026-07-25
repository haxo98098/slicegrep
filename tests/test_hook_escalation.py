"""Adaptive budget.

A fixed budget is a guess about how dense the file is. When the guess is
wrong the first pass returns a sliver, and the useful response is to spend
more tokens rather than to hand back 6% and hope. These tests pin that the
escalation happens, that it stops, and that whatever is still missing is
named with the exact offset/limit needed to fetch it.
"""
import io
import json
import sys

import pytest

from slicegrep import hook


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("SLICEGREP_HOOK_DISABLE", raising=False)
    monkeypatch.setattr(hook.tempfile, "gettempdir", lambda: str(tmp_path))


def _dense_file(tmp_path, n=120):
    """Many independent matching regions: far more than a small budget holds."""
    p = tmp_path / "dense.py"
    out = []
    for i in range(n):
        out.append(f"def handle_request_{i}(payload):")
        out.append(f'    """handle_request variant {i}."""')
        for j in range(8):
            out.append(f"    step_{j} = compute(payload, {i}, {j})")
        out.append(f"    return dispatch_{i}(payload)")
        out.append("")
    p.write_text("\n".join(out), encoding="utf-8")
    return p


def test_budget_escalates_when_coverage_is_low(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "BUDGET", 200)
    monkeypatch.setattr(hook, "MAX_BUDGET", 3200)
    monkeypatch.setattr(hook, "MIN_COVERAGE", 0.55)
    monkeypatch.setattr(hook, "TIMEOUT", 30.0)

    res = hook._slices(_dense_file(tmp_path), ["handle_request"])
    assert res, "expected slices"
    assert res["escalated"] >= 1, "low coverage should have raised the budget"
    assert res["budget"] > 200


def test_escalation_respects_the_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "BUDGET", 200)
    monkeypatch.setattr(hook, "MAX_BUDGET", 800)
    monkeypatch.setattr(hook, "MIN_COVERAGE", 0.99)   # never satisfiable here
    monkeypatch.setattr(hook, "TIMEOUT", 30.0)

    res = hook._slices(_dense_file(tmp_path), ["handle_request"])
    assert res["budget"] <= 800, "budget must not grow past the ceiling"


def test_no_escalation_when_coverage_is_already_good(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "BUDGET", 4000)
    monkeypatch.setattr(hook, "MAX_BUDGET", 8000)
    monkeypatch.setattr(hook, "TIMEOUT", 30.0)

    p = tmp_path / "small.py"
    p.write_text("def only_one(payload):\n    return payload\n" * 3,
                 encoding="utf-8")
    res = hook._slices(p, ["only_one"])
    if res:
        assert res["escalated"] == 0


def test_injected_text_names_omitted_ranges(tmp_path, monkeypatch):
    """The model must be told how to fetch what it did not get."""
    monkeypatch.setattr(hook, "BUDGET", 200)
    monkeypatch.setattr(hook, "MAX_BUDGET", 400)
    monkeypatch.setattr(hook, "MIN_COVERAGE", 0.99)
    monkeypatch.setattr(hook, "TIMEOUT", 30.0)
    monkeypatch.setattr(hook, "MIN_TOKENS", 100)

    f = _dense_file(tmp_path)
    # the hook derives its query from the session, so give it one
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"role": "user", "content":
                 "walk me through handle_request and dispatch"}}) + "\n",
                 encoding="utf-8")
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(f)},
               "session_id": "esc", "transcript_path": str(t)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = hook.main()
    assert rc == 0
    ctx = json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert "STILL NOT SHOWN" in ctx
    assert "offset=" in ctx and "limit=" in ctx, \
        "omitted regions must come with the exact read arguments"
