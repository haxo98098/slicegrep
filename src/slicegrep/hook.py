"""PreToolUse hook: intercept whole-file reads and return ranked slices.

Registering slicegrep as an MCP tool makes it *available*. The agent still has
to choose it, and agents reach for the plain file read out of habit. This hook
makes budgeted retrieval the default path instead of an option: it sits in
front of the Read tool, and when a read is large enough to be worth it, the
whole-file read is replaced with a file map plus the slices that match what
the session is actually working on.

Contract (Claude Code PreToolUse):
  stdin   {"tool_name": "Read", "tool_input": {...}, "transcript_path": ...,
           "session_id": ..., "cwd": ...}
  stdout  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
           "permissionDecision": "deny", "permissionDecisionReason": ...,
           "additionalContext": "<the slices>"}}

Design rules, in priority order:

1. FAIL OPEN. Any error, missing dependency, unreadable path, or empty result
   exits 0 with no output, which defers to the normal permission flow and the
   full read happens. A retrieval optimizer must never be able to break a
   session.
2. NEVER TRAP THE MODEL. The second read of the same path in a session passes
   through untouched, and the injected text says so. If the slices were not
   enough, asking again gets the whole file. Without this rule an agent that
   genuinely needs the full file can never get it.
3. ONLY WHEN IT PAYS. Small files, ranged reads (offset/limit), and non-code
   files pass through. The median real-world read is ~600 tokens, where
   slicing saves nothing; the cost lives in the tail.

Environment:
  SLICEGREP_HOOK_DISABLE=1     turn the hook off without unregistering it
  SLICEGREP_HOOK_MIN_TOKENS=N  only intercept reads estimated above N (2000)
  SLICEGREP_HOOK_BUDGET=N      token budget for the returned slices (1200)
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

MIN_TOKENS = int(os.environ.get("SLICEGREP_HOOK_MIN_TOKENS", "2000"))
BUDGET = int(os.environ.get("SLICEGREP_HOOK_BUDGET", "1200"))

# Slicing only makes sense for source. Prose and data files are read whole.
_CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".rs", ".go",
    ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php",
    ".swift", ".scala", ".sh", ".bash", ".lua", ".pl", ".r", ".m", ".mm",
    ".vue", ".svelte", ".sql", ".zig", ".dart", ".ex", ".exs", ".hs",
}

_DEF_RE = re.compile(
    r"^\s*(?:@|(?:async\s+)?def\s|class\s|func\s|fn\s|function\s|"
    r"(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s|"
    r"(?:public|private|protected|static|final)\s|"
    r"(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?\(|"
    r"(?:pub\s+)?(?:struct|enum|trait|impl|interface|type)\s)"
)

_STOP = {
    "the", "and", "for", "that", "this", "with", "from", "what", "when",
    "where", "which", "have", "has", "was", "were", "are", "you", "your",
    "can", "could", "would", "should", "please", "make", "just", "like",
    "into", "then", "than", "them", "they", "there", "here", "how", "why",
    "does", "did", "not", "but", "all", "any", "its", "it's", "use", "using",
    "file", "files", "code", "line", "lines", "read", "look", "see", "run",
    "add", "fix", "check", "need", "want", "get", "let", "one", "two", "new",
    "now", "out", "off", "our", "own", "way", "who", "will", "also", "some",
}


def _est_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _state_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id))[:64] or "nosession"
    return Path(tempfile.gettempdir()) / f"slicegrep_hook_{safe}.json"


def _seen(session_id: str, path: str) -> bool:
    """True if this path was already intercepted in this session.

    Rule 2: one interception per file per session. If the agent asks again it
    means the slices were not enough, and the full read must go through.
    """
    sp = _state_path(session_id)
    try:
        seen = set(json.loads(sp.read_text(encoding="utf-8")))
    except Exception:
        seen = set()
    key = str(path).replace("\\", "/").lower()
    if key in seen:
        return True
    seen.add(key)
    try:
        sp.write_text(json.dumps(sorted(seen)[-500:]), encoding="utf-8")
    except Exception:
        pass
    return False


def _tail_text(transcript_path: str, limit: int = 200_000) -> list:
    """Recent user+assistant text from the transcript, newest first."""
    p = Path(transcript_path)
    if not p.is_file():
        return []
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                fh.readline()          # drop the partial line
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            out.append((role, content))
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    out.append((role, item.get("text", "")))
    out.reverse()
    return out


def _query_terms(messages: list, path: str) -> list:
    """Terms describing what the session is working on.

    Weighted toward the most recent user message: a read is nearly always in
    service of the current request, not the conversation as a whole.
    """
    scored: dict = {}
    budget_msgs = 0
    for role, text in messages:
        if not text:
            continue
        budget_msgs += 1
        if budget_msgs > 6:
            break
        weight = 3.0 if role == "user" else 1.0
        weight /= budget_msgs                      # recency decay
        # identifiers first: snake_case, camelCase, dotted, CONSTANTS
        for m in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text[:4000]):
            low = m.lower()
            if low in _STOP:
                continue
            bonus = 2.0 if ("_" in m or not m.islower()) else 1.0
            scored[m] = scored.get(m, 0.0) + weight * bonus
    # the filename's own stem is a strong hint
    stem = Path(path).stem
    if len(stem) >= 4:
        scored[stem] = scored.get(stem, 0.0) + 1.0
    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    return [t for t, _ in ranked[:8]]


def _file_map(text: str, max_lines: int = 60) -> str:
    """Definition skeleton: what is in the file and where."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 200 or not _DEF_RE.match(line):
            continue
        stripped = line.rstrip()
        if len(stripped) > 110:
            stripped = stripped[:110] + " ..."
        out.append(f"{i:6d}  {stripped}")
        if len(out) >= max_lines:
            out.append("       ... (definition list truncated)")
            break
    return "\n".join(out)


def _passthrough() -> int:
    """Exit 0 with no output: normal permission flow, full read proceeds."""
    return 0


def main(argv=None) -> int:
    if os.environ.get("SLICEGREP_HOOK_DISABLE"):
        return _passthrough()
    try:
        # utf-8-sig: some shells prepend a BOM when piping. Without this the
        # hook silently fails open on every call, which looks like "the hook
        # does nothing" rather than an error.
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="replace")
        payload = json.loads(raw)
    except Exception:
        try:                       # stdin already consumed/replaced (tests)
            payload = json.loads(sys.stdin.read())
        except Exception:
            return _passthrough()

    if payload.get("tool_name") != "Read":
        return _passthrough()

    tin = payload.get("tool_input") or {}
    path = tin.get("file_path") or tin.get("path")
    if not path:
        return _passthrough()

    # An explicit range is already a budgeted read; leave it alone.
    if tin.get("offset") or tin.get("limit"):
        return _passthrough()

    p = Path(path)
    if p.suffix.lower() not in _CODE_SUFFIXES:
        return _passthrough()
    try:
        if not p.is_file():
            return _passthrough()
        raw_size = p.stat().st_size
    except OSError:
        return _passthrough()

    if raw_size // 4 < MIN_TOKENS:          # rule 3: not worth intercepting
        return _passthrough()

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _passthrough()

    est = _est_tokens(text)
    if est < MIN_TOKENS:
        return _passthrough()

    # Rule 2: the second ask always wins.
    if _seen(payload.get("session_id", ""), path):
        return _passthrough()

    terms = _query_terms(_tail_text(payload.get("transcript_path", "")), path)
    file_map = _file_map(text)

    slices = ""
    if terms:
        try:
            from .core import focused_read
            pattern = "|".join(re.escape(t) for t in terms)
            result = focused_read(str(p), pattern, budget=BUDGET,
                                  boundary="fn", objective="auto")
            if result.chunks:
                slices = result.render()
        except Exception:
            slices = ""

    if not slices and not file_map:
        return _passthrough()               # rule 1: nothing useful, fail open

    n_lines = text.count("\n") + 1
    parts = [
        f"slicegrep replaced a whole-file read of {p.name} "
        f"({n_lines:,} lines, ~{est:,} tokens) with a map and ranked slices "
        f"(~{BUDGET} token budget).",
        "",
    ]
    if file_map:
        parts += [f"FILE MAP — {path}", file_map, ""]
    if slices:
        parts += [f"SLICES matching this session's focus ({', '.join(terms[:5])}):",
                  slices, ""]
    parts.append(
        "If this is not enough: read the file again and the full contents "
        "will be returned (the next read of this path is not intercepted), "
        "or read a specific range with offset/limit."
    )
    context = "\n".join(parts)

    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"slicegrep: returned ranked slices instead of "
                f"~{est:,} tokens of whole file"
            ),
            "additionalContext": context,
        }
    }, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
