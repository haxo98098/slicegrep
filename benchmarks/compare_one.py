#!/usr/bin/env python
"""One real task, two ways. The head-to-head an aggregate table cannot show.

Aggregate hit rates say slicegrep wins by N points. They do not show what the
difference feels like at the tool-call level, which is what actually spends
an agent's context. This runs a single realistic bug-investigation task both
ways and reports the same facts for each:

  tool calls, tokens delivered, and whether the three things you actually
  need to fix a bug were retrieved: the DEFINITION, a CALLER, and a TEST.

Baseline path (what an agent does without help):
    grep -> read a file -> read another file -> grep again
Each read is charged at its true whole-file cost, because that is what lands
in the context window.

slicegrep path:
    one call, budgeted.

The corpora are real upstream repositories at pinned revisions. Get them with:

    python benchmarks/setup_corpora.py

Usage:
    python benchmarks/compare_one.py                 # default task
    python benchmarks/compare_one.py --list
    python benchmarks/compare_one.py --task echo
    python benchmarks/compare_one.py --corpora /path/to/corpora

The corpora directory is resolved in this order: --corpora, then
$SLICEGREP_BENCH_CORPORA, then benchmarks/corpora next to this script.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slicegrep.core import focused_read  # noqa: E402

import os  # noqa: E402

DEFAULT_CORPORA = Path(__file__).resolve().parent / "corpora"


def resolve_corpora(cli_value: str | None) -> Path:
    """--corpora, else $SLICEGREP_BENCH_CORPORA, else benchmarks/corpora."""
    if cli_value:
        return Path(cli_value).expanduser()
    env = os.environ.get("SLICEGREP_BENCH_CORPORA")
    if env:
        return Path(env).expanduser()
    return DEFAULT_CORPORA


# Each task states a real bug-shaped question and what a correct answer must
# have seen: the symbol's definition, somewhere it is called from another
# file, and its test. Ground truth is expressed as regexes over retrieved
# text, so both strategies are judged by exactly the same standard.
TASKS = {
    "echo": {
        "repo": "click",
        "question": "echo() mangles unicode on Windows. Where is it defined, "
                    "who calls it, and what covers it?",
        "grep_term": "def echo",
        "pattern": "def echo|echo(",
        "definition": r"def echo\(",
        "caller": r"echo\(",
        "test": r"def test_\w*echo",
    },
    "url_for": {
        "repo": "flask",
        "question": "url_for builds the wrong path behind a proxy. Where is "
                    "it defined, who calls it, and what covers it?",
        "grep_term": "def url_for",
        "pattern": "def url_for|url_for(",
        "definition": r"def url_for\(",
        "caller": r"url_for\(",
        "test": r"def test_\w*url_for",
    },
    "session_request": {
        "repo": "requests",
        "question": "Session.request loses headers on redirect. Definition, "
                    "callers, tests?",
        "grep_term": "def request",
        "pattern": "def request|self.request(",
        "definition": r"def request\(",
        "caller": r"\.request\(",
        "test": r"def test_\w*(request|redirect)",
    },
}


def _tok(s: str) -> int:
    return max(1, len(s) // 4)


def _grep(root: Path, term: str, limit: int = 40):
    """Plain ripgrep-style hit list: file:line:text, no context."""
    try:
        p = subprocess.run(["rg", "-n", "--no-heading", term, str(root)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        lines = [l for l in p.stdout.splitlines() if l.strip()][:limit]
    except (OSError, subprocess.SubprocessError):
        lines = []
        for f in root.rglob("*.py"):
            try:
                for i, l in enumerate(
                        f.read_text(encoding="utf-8", errors="replace")
                        .splitlines(), 1):
                    if term in l:
                        lines.append(f"{f}:{i}:{l}")
                        if len(lines) >= limit:
                            return lines
            except OSError:
                continue
    return lines


def _covers(text: str, rx: str) -> bool:
    return re.search(rx, text) is not None


# "C:\repo\mod.py:12:code" must not split on the drive-letter colon. Getting
# this wrong makes the baseline's file reads fail silently, which flatters
# the baseline by charging it for grep output alone.
_HIT_RE = re.compile(r"^([A-Za-z]:[\\/][^:]*|[^:]+):(\d+):")


def _hit_path(hit: str):
    m = _HIT_RE.match(hit)
    return m.group(1) if m else None


def baseline(root: Path, task: dict) -> dict:
    """grep -> read -> read -> grep, charged at whole-file cost."""
    calls, delivered = 0, []

    hits = _grep(root, task["grep_term"])
    calls += 1
    delivered.append("\n".join(hits))

    # The agent opens the two most promising files whole. That is the habit
    # this whole project exists to replace.
    seen, opened = set(), []
    for h in hits:
        fp = _hit_path(h)
        if not fp or fp in seen:
            continue
        seen.add(fp)
        opened.append(fp)
        if len(opened) == 2:
            break
    for fp in opened:
        try:
            delivered.append(Path(fp).read_text(encoding="utf-8",
                                                errors="replace"))
            calls += 1
        except OSError as e:
            print(f"  [warn] baseline could not read {fp}: {e}",
                  file=sys.stderr)
    if len(opened) < 2:
        print(f"  [warn] baseline opened only {len(opened)} file(s); "
              f"grep hit parsing may be off", file=sys.stderr)

    # then a second grep, hunting the test
    hits2 = _grep(root, task["grep_term"].split()[-1] + "")
    calls += 1
    delivered.append("\n".join(hits2))

    blob = "\n".join(delivered)
    return {
        "name": "grep -> read -> read -> grep",
        "calls": calls,
        "tokens": _tok(blob),
        "definition": _covers(blob, task["definition"]),
        "caller": _covers(blob, task["caller"]),
        "test": _covers(blob, task["test"]),
    }


def slicegrep_path(root: Path, task: dict, budget: int = 2000) -> dict:
    r = focused_read(str(root), task["pattern"], budget=budget,
                     boundary="fn", objective="auto")
    blob = r.render()
    return {
        "name": "slicegrep (one call)",
        "calls": 1,
        "tokens": _tok(blob),
        "definition": _covers(blob, task["definition"]),
        "caller": _covers(blob, task["caller"]),
        "test": _covers(blob, task["test"]),
        "coverage": r.coverage,
        "omitted": len(r.omitted),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="echo", choices=sorted(TASKS))
    ap.add_argument("--budget", type=int, default=2000)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--corpora", default=None,
                    help="directory holding the cloned corpora "
                         "(default: $SLICEGREP_BENCH_CORPORA, else "
                         "benchmarks/corpora)")
    args = ap.parse_args()
    if args.list:
        for k, t in TASKS.items():
            print(f"{k:18s} [{t['repo']}] {t['question']}")
        return 0

    corpora = resolve_corpora(args.corpora)
    task = TASKS[args.task]
    root = corpora / task["repo"]
    if not root.is_dir():
        print(f"corpus not found: {root}\n\n"
              f"Fetch the pinned revisions the published numbers came from:\n"
              f"    python benchmarks/setup_corpora.py --only {task['repo']}\n"
              f"or point at an existing checkout:\n"
              f"    python benchmarks/compare_one.py --corpora /path/to/corpora\n"
              f"    (or set SLICEGREP_BENCH_CORPORA)", file=sys.stderr)
        return 2

    print(f"\nTASK ({task['repo']}): {task['question']}\n")
    rows = [baseline(root, task), slicegrep_path(root, task, args.budget)]

    print(f"{'strategy':32s} {'calls':>6s} {'tokens':>8s} "
          f"{'defn':>6s} {'caller':>7s} {'test':>6s}")
    print("-" * 70)
    for r in rows:
        tick = lambda b: " yes" if b else "  no"      # noqa: E731
        print(f"{r['name']:32s} {r['calls']:>6d} {r['tokens']:>8,} "
              f"{tick(r['definition']):>6s} {tick(r['caller']):>7s} "
              f"{tick(r['test']):>6s}")

    a, b = rows
    if a["tokens"] < 50:
        # the baseline found essentially nothing: comparing percentages here
        # would be noise dressed as a result
        print("\nbaseline retrieved almost nothing; percentage omitted "
              "(check the grep term for this task)")
    else:
        print(f"\n{100 * (1 - b['tokens'] / a['tokens']):.0f}% fewer tokens, "
              f"{a['calls']} calls -> {b['calls']}")
    if "coverage" in b:
        print(f"slicegrep coverage {b['coverage']:.0%} of matched material, "
              f"{b['omitted']} region(s) declared omitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
