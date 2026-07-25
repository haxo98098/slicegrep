"""The hook sits in front of every Read, so its failure modes matter more
than its features. These tests pin the three rules: fail open, never trap
the model, only intercept when it pays."""
import io
import json
import os
import sys

import pytest

from slicegrep import hook


def _run(payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = hook.main()
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def _big_py(tmp_path, name="big.py", n=400):
    p = tmp_path / name
    body = "\n".join(
        f"class Widget{i}:\n"
        f"    def render_widget_{i}(self, payload):\n"
        f"        # some explanatory filler to give the file real bulk\n"
        f"        return compute_layout(payload, mode={i})\n"
        for i in range(n)
    )
    p.write_text(body, encoding="utf-8")
    return p


def _payload(path, session="s1", transcript="", **tin):
    inp = {"file_path": str(path)}
    inp.update(tin)
    return {"tool_name": "Read", "tool_input": inp, "session_id": session,
            "transcript_path": transcript}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SLICEGREP_HOOK_DISABLE", raising=False)
    # isolate the per-session state file
    monkeypatch.setattr(hook.tempfile, "gettempdir", lambda: str(tmp_path))


def test_intercepts_large_code_file(tmp_path, monkeypatch, capsys):
    big = _big_py(tmp_path)
    rc, out = _run(_payload(big), monkeypatch, capsys)
    assert rc == 0
    assert out is not None, "a large code read should be intercepted"
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    ctx = hso["additionalContext"]
    assert "FILE MAP" in ctx
    assert "render_widget_0" in ctx          # the map lists real definitions
    assert "read the file again" in ctx      # escape hatch is advertised


def test_second_read_passes_through(tmp_path, monkeypatch, capsys):
    """Rule 2: if the slices were not enough, asking again gets the file."""
    big = _big_py(tmp_path)
    rc1, out1 = _run(_payload(big), monkeypatch, capsys)
    rc2, out2 = _run(_payload(big), monkeypatch, capsys)
    assert out1 is not None
    assert rc2 == 0 and out2 is None, "second read must not be intercepted"


def test_small_file_passes_through(tmp_path, monkeypatch, capsys):
    small = tmp_path / "tiny.py"
    small.write_text("def f():\n    return 1\n", encoding="utf-8")
    rc, out = _run(_payload(small), monkeypatch, capsys)
    assert rc == 0 and out is None


def test_ranged_read_passes_through(tmp_path, monkeypatch, capsys):
    big = _big_py(tmp_path)
    rc, out = _run(_payload(big, offset=100, limit=50), monkeypatch, capsys)
    assert rc == 0 and out is None, "an explicit range is already budgeted"


def test_non_code_passes_through(tmp_path, monkeypatch, capsys):
    doc = tmp_path / "notes.md"
    doc.write_text("# heading\n" + ("prose paragraph. " * 3000), encoding="utf-8")
    rc, out = _run(_payload(doc), monkeypatch, capsys)
    assert rc == 0 and out is None


def test_missing_file_fails_open(tmp_path, monkeypatch, capsys):
    rc, out = _run(_payload(tmp_path / "nope.py"), monkeypatch, capsys)
    assert rc == 0 and out is None


def test_other_tools_ignored(tmp_path, monkeypatch, capsys):
    big = _big_py(tmp_path)
    p = _payload(big)
    p["tool_name"] = "Edit"
    rc, out = _run(p, monkeypatch, capsys)
    assert rc == 0 and out is None


def test_malformed_stdin_fails_open(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))
    assert hook.main() == 0
    assert capsys.readouterr().out.strip() == ""


def test_disable_env_var(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SLICEGREP_HOOK_DISABLE", "1")
    big = _big_py(tmp_path)
    rc, out = _run(_payload(big), monkeypatch, capsys)
    assert rc == 0 and out is None


def test_engine_failure_still_fails_open(tmp_path, monkeypatch, capsys):
    """If the retrieval engine itself explodes, the read must still work."""
    big = _big_py(tmp_path)
    import slicegrep.core as core
    monkeypatch.setattr(core, "focused_read",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    rc, out = _run(_payload(big), monkeypatch, capsys)
    assert rc == 0
    # the file map alone is still useful, but nothing may crash
    if out is not None:
        assert "FILE MAP" in out["hookSpecificOutput"]["additionalContext"]


def test_query_terms_prefer_recent_user_message(tmp_path):
    msgs = [("user", "fix the retry_backoff logic"),
            ("assistant", "looking at unrelated_helper now"),
            ("user", "older message about parser_state")]
    terms = hook._query_terms(msgs, str(tmp_path / "mod.py"))
    assert "retry_backoff" in terms
    assert terms.index("retry_backoff") < terms.index("parser_state")


def test_transcript_query_drives_slices(tmp_path, monkeypatch, capsys):
    """End to end: the session's focus should steer which slices come back."""
    big = _big_py(tmp_path)
    t = tmp_path / "t.jsonl"
    with open(t, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"message": {"role": "user", "content":
                 "walk me through render_widget_7 please"}}) + "\n")
    rc, out = _run(_payload(big, transcript=str(t)), monkeypatch, capsys)
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "SLICES" in ctx
    assert "render_widget_7" in ctx
