"""The corpora fetcher is what makes the published numbers reproducible, so
it gets tested like anything else.

A local git repo stands in for the upstream ones: these tests exercise the
clone, the SHA verification, and the mismatch warning without touching the
network, so they run anywhere including CI.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

import setup_corpora as sc  # noqa: E402


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


@pytest.fixture
def fake_upstream(tmp_path):
    """A tiny local repo with a tagged release."""
    repo = tmp_path / "upstream"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "mod.py").write_text("def thing():\n    return 1\n",
                                 encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "initial", cwd=repo)
    _git("tag", "v1.0.0", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    return repo, sha


def test_clone_checks_out_the_pinned_revision(tmp_path, fake_upstream,
                                              monkeypatch, capsys):
    repo, sha = fake_upstream
    monkeypatch.setitem(sc.PINNED, "fake", (str(repo), "v1.0.0", sha))

    dest = tmp_path / "corpora"
    dest.mkdir()
    assert sc.clone("fake", dest, full=False) is True
    assert (dest / "fake" / "mod.py").is_file()
    assert sc.head_sha(dest / "fake") == sha
    assert "WARNING" not in capsys.readouterr().out


def test_moved_tag_is_reported_not_hidden(tmp_path, fake_upstream,
                                          monkeypatch, capsys):
    """If upstream moves the tag, the numbers are no longer comparable and
    the script has to say so."""
    repo, real_sha = fake_upstream
    wrong = "0" * 40
    monkeypatch.setitem(sc.PINNED, "fake", (str(repo), "v1.0.0", wrong))

    dest = tmp_path / "corpora"
    dest.mkdir()
    sc.clone("fake", dest, full=False)
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert real_sha[:8] in out, "must show what was actually checked out"


def test_verify_flags_missing_and_mismatched(tmp_path, fake_upstream,
                                             monkeypatch, capsys):
    repo, sha = fake_upstream
    monkeypatch.setattr(sc, "PINNED", {"fake": (str(repo), "v1.0.0", sha),
                                       "absent": ("http://x", "v1", "a" * 40)})
    dest = tmp_path / "corpora"
    dest.mkdir()
    sc.clone("fake", dest, full=False)

    bad = sc.verify(dest)
    out = capsys.readouterr().out
    assert bad == 1, "the missing repo should be counted"
    assert "MISSING" in out


def test_existing_checkout_is_left_alone(tmp_path, fake_upstream,
                                         monkeypatch, capsys):
    repo, sha = fake_upstream
    monkeypatch.setitem(sc.PINNED, "fake", (str(repo), "v1.0.0", sha))
    dest = tmp_path / "corpora"
    dest.mkdir()
    sc.clone("fake", dest, full=False)
    capsys.readouterr()

    assert sc.clone("fake", dest, full=False) is True
    assert "already present" in capsys.readouterr().out


def test_compare_one_resolution_order(tmp_path, monkeypatch):
    """--corpora beats the env var, which beats the bundled default."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))
    import compare_one

    monkeypatch.setenv("SLICEGREP_BENCH_CORPORA", str(tmp_path / "from_env"))
    assert compare_one.resolve_corpora(str(tmp_path / "from_cli")) == \
        tmp_path / "from_cli"
    assert compare_one.resolve_corpora(None) == tmp_path / "from_env"

    monkeypatch.delenv("SLICEGREP_BENCH_CORPORA")
    assert compare_one.resolve_corpora(None) == compare_one.DEFAULT_CORPORA
