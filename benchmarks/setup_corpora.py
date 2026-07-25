#!/usr/bin/env python
"""Fetch the exact repository revisions the published benchmarks were run on.

Every number in the README came from these commits. Tags can be moved by
their maintainers, so each entry pins a full SHA and the script verifies what
it actually checked out. A mismatch is reported loudly rather than silently
producing numbers that cannot be compared to the published ones.

    python benchmarks/setup_corpora.py                 # -> benchmarks/corpora
    python benchmarks/setup_corpora.py --dest /tmp/c   # somewhere else
    python benchmarks/setup_corpora.py --full          # + full-history clones
    python benchmarks/setup_corpora.py --only click,flask
    python benchmarks/setup_corpora.py --verify        # check, clone nothing

Then:

    python benchmarks/compare_one.py --corpora benchmarks/corpora
    export SLICEGREP_BENCH_CORPORA=benchmarks/corpora   # or set it once
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# name -> (url, tag, exact SHA that tag pointed at when the benchmarks ran)
PINNED = {
    "click":    ("https://github.com/pallets/click", "8.1.7",
                 "874ca2bc1c30d93a4ac6e36a15ed685eafe89097"),
    "flask":    ("https://github.com/pallets/flask", "3.0.0",
                 "735a4701d6d5e848241e7d7535db898efb62d400"),
    "requests": ("https://github.com/psf/requests", "v2.31.0",
                 "147c8511ddbfa5e8f71bbf5c18ede0c4ceb3bba4"),
    "rich":     ("https://github.com/Textualize/rich", "v13.7.0",
                 "fd981823644ccf50d685ac9c0cfe8e1e56c9dd35"),
    "django":   ("https://github.com/django/django", "5.0.6",
                 "2719a7f8c161233f45d34b624a9df9392c86cc1b"),
    "zod":      ("https://github.com/colinhacks/zod", "v3.23.8",
                 "ca42965df46b2f7e2747db29c40a26bcb32a51d5"),
    "serde":    ("https://github.com/serde-rs/serde", "v1.0.203",
                 "d5bc546ca53be0b31984a06a8ad587cbea4ca5ce"),
}

# bench3 mines commit history, so it needs full clones rather than snapshots.
FULL_HISTORY = ["click", "flask", "requests", "rich"]

# Which suites need what, so a reader knows why a repo is in the list.
USED_BY = {
    "click": "compare_one, bench2, bench3",
    "flask": "compare_one, bench2, bench3",
    "requests": "compare_one, bench2, bench3",
    "rich": "bench2, bench3",
    "django": "bench5 (monorepo scale)",
    "zod": "bench5 (TypeScript)",
    "serde": "bench5 (Rust)",
}


def _git(*args, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def head_sha(path: Path):
    p = _git("rev-parse", "HEAD", cwd=str(path))
    return p.stdout.strip() if p.returncode == 0 else None


def clone(name: str, dest: Path, full: bool) -> bool:
    url, tag, sha = PINNED[name]
    target = dest / name
    if target.exists():
        got = head_sha(target)
        if got == sha or full:
            print(f"  {name:9s} already present ({(got or '?')[:8]})")
            return True
        print(f"  {name:9s} present but at {got[:8] if got else '?'}, "
              f"expected {sha[:8]}; remove it to re-clone")
        return False

    args = ["clone", "--quiet"]
    if not full:
        args += ["--depth", "1", "--branch", tag]
    args += [url, str(target)]
    print(f"  {name:9s} cloning {tag} ...", flush=True)
    p = _git(*args)
    if p.returncode != 0:
        print(f"  {name:9s} FAILED: {p.stderr.strip()[:160]}")
        return False

    got = head_sha(target)
    if not full and got != sha:
        # The tag still resolved, but not to the revision that produced the
        # published numbers. Say so rather than pretending it is comparable.
        print(f"  {name:9s} WARNING: tag {tag} now points at {got[:8]}, "
              f"benchmarks were run on {sha[:8]}")
    else:
        print(f"  {name:9s} ok ({(got or '?')[:8]})")
    return True


def verify(dest: Path) -> int:
    bad = 0
    for name, (_url, tag, sha) in PINNED.items():
        target = dest / name
        if not target.is_dir():
            print(f"  {name:9s} MISSING")
            bad += 1
            continue
        got = head_sha(target)
        mark = "ok  " if got == sha else "DIFF"
        if got != sha:
            bad += 1
        print(f"  {name:9s} {mark} have={(got or '?')[:8]} "
              f"want={sha[:8]} ({tag})")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    default = Path(__file__).resolve().parent / "corpora"
    ap.add_argument("--dest", default=str(default),
                    help=f"where to put the corpora (default: {default})")
    ap.add_argument("--only", help="comma-separated subset, e.g. click,flask")
    ap.add_argument("--full", action="store_true",
                    help="also make full-history clones in <dest>-full "
                         "(needed by bench3, which mines commit history)")
    ap.add_argument("--verify", action="store_true",
                    help="report what is checked out; clone nothing")
    args = ap.parse_args()

    dest = Path(args.dest)
    if args.verify:
        print(f"verifying {dest}")
        bad = verify(dest)
        print("all pinned revisions present" if bad == 0
              else f"{bad} repo(s) missing or at a different revision")
        return 1 if bad else 0

    names = [n.strip() for n in args.only.split(",")] if args.only \
        else list(PINNED)
    unknown = [n for n in names if n not in PINNED]
    if unknown:
        print(f"unknown: {', '.join(unknown)}", file=sys.stderr)
        return 2

    dest.mkdir(parents=True, exist_ok=True)
    print(f"fetching {len(names)} repo(s) into {dest}")
    ok = sum(clone(n, dest, full=False) for n in names)

    if args.full:
        fdest = Path(str(dest) + "-full")
        fdest.mkdir(parents=True, exist_ok=True)
        print(f"\nfull-history clones into {fdest} (slower)")
        for n in [n for n in names if n in FULL_HISTORY]:
            clone(n, fdest, full=True)

    print(f"\n{ok}/{len(names)} ready. Now:")
    print(f"  python benchmarks/compare_one.py --corpora {dest}")
    print(f"  (or set SLICEGREP_BENCH_CORPORA={dest})")
    return 0 if ok == len(names) else 1


if __name__ == "__main__":
    raise SystemExit(main())
