"""Omission accounting.

A budgeted retriever that silently drops matches is asking to be trusted on
faith. These tests pin the promise that every matching region is either
returned or named: nothing disappears without a line in the report saying
where it went and why.
"""
from slicegrep import focused_read


def _many_matches(tmp_path, n=40):
    """A file with far more matching regions than a small budget can hold."""
    p = tmp_path / "wide.py"
    body = []
    for i in range(n):
        body.append(f"def handle_request_{i}(payload):")
        body.append(f"    # handle_request variant {i}, deliberately padded")
        body.append("    " + ("x = compute(payload)  # filler\n    " * 6))
        body.append(f"    return dispatch_{i}(payload)")
        body.append("")
    p.write_text("\n".join(body), encoding="utf-8")
    return p


def test_omissions_are_reported(tmp_path):
    p = _many_matches(tmp_path)
    r = focused_read(str(p), "def handle_request", budget=400)
    assert r.omitted, "regions were dropped but nothing was reported"
    assert r.omitted_tokens > 0
    for o in r.omitted:
        assert o["line_start"] >= 1
        assert o["line_end"] >= o["line_start"]
        assert o["tokens"] > 0
        # two ways to lose material: a whole region dropped, or a kept
        # region cut short. Both must be declared.
        assert o["why"] in ("budget", "truncated")


def test_nothing_omitted_when_budget_is_ample(tmp_path):
    p = _many_matches(tmp_path, n=2)
    r = focused_read(str(p), "def handle_request", budget=0)
    assert r.omitted == []
    assert r.coverage == 1.0
    assert "OMITTED" not in r.render()


def test_coverage_is_honest(tmp_path):
    """Coverage must reflect what was actually withheld, not just chunk count."""
    p = _many_matches(tmp_path)
    r = focused_read(str(p), "def handle_request", budget=400)
    total = r.total_tokens + r.omitted_tokens
    assert abs(r.coverage - r.total_tokens / total) < 1e-6
    assert 0.0 < r.coverage < 1.0


def test_render_names_locations_not_just_counts(tmp_path):
    """The report must say WHERE, so the caller can go get it."""
    p = _many_matches(tmp_path)
    r = focused_read(str(p), "def handle_request", budget=400)
    text = r.render()
    assert "OMITTED" in text
    assert "wide.py:" in text.split("OMITTED", 1)[1]
    assert "raise --budget" in text
    first = r.omitted[0]
    assert f"{first['line_start']}-{first['line_end']}" in text


def test_truncated_chunk_header_does_not_overstate(tmp_path):
    """A chunk cut to fit must not keep claiming the lines it dropped."""
    p = _many_matches(tmp_path)
    r = focused_read(str(p), "def handle_request", budget=400)
    trunc = [o for o in r.omitted if o["why"] == "truncated"]
    if not trunc:
        return                      # budget was enough; nothing to check
    for c in r.chunks:
        shown = c.code.count("\n") + 1
        claimed = c.line_end - c.line_start + 1
        assert claimed <= shown + 2, (
            f"header claims {claimed} lines but only {shown} were returned")


def test_omissions_in_structured_output(tmp_path):
    p = _many_matches(tmp_path)
    d = focused_read(str(p), "def handle_request", budget=400).to_dict()
    assert d["omitted"] and d["omitted_tokens"] > 0
    assert 0.0 < d["coverage"] < 1.0
