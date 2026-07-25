"""slicegrep core engine — token-frugal grep-and-extract for code.

`focused_read()` greps a file or directory for a pattern, extracts only the
surrounding slices, **ranks** them (co-occurrence, rare terms, definition vs.
usage), **dedupes** near-identical chunks, caps the total to a **token budget**,
and reports **negative evidence** (patterns/symbols it did *not* find).

That is the workflow you normally simulate with grep-then-read — but in one call
and a fraction of the tokens, which is exactly what an LLM coding agent wants
when it reads a codebase.

Everything here is standard-library only.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

__all__ = [
    "Chunk",
    "Result",
    "focused_read",
    "SKIP_DIRS",
    "SEARCHABLE_EXTS",
]

# Directories that are almost never what you want to read: VCS metadata, build
# output, dependency caches, editor state.
SKIP_DIRS = {
    ".git", ".svn", ".hg", ".idea", ".vs", ".vscode",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "build", "dist", "bin", "obj", "out", "target",
    "venv", ".venv", "env", ".tox", ".eggs", "site-packages",
}

# Extensions searched during a recursive walk.
SEARCHABLE_EXTS = {
    ".py", ".pyi", ".pyw",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx", ".hh", ".inl",
    ".cs", ".java", ".kt", ".kts", ".scala", ".swift", ".m", ".mm",
    ".go", ".rs", ".rb", ".php", ".lua", ".dart", ".ex", ".exs",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".sql", ".r", ".jl", ".hs", ".ml", ".clj", ".vim",
    ".txt", ".md", ".rst", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".xml", ".html", ".css", ".scss", ".less",
}

_MAX_FILE_BYTES = 1_000_000


# --------------------------------------------------------------------------- #
# Token estimation
# --------------------------------------------------------------------------- #

def estimate_tokens(text: str) -> int:
    """Cheap, model-agnostic token estimate (~4 chars/token)."""
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- #
# Language-aware block boundaries
# --------------------------------------------------------------------------- #

_LANG_PATTERNS = {
    "c_like": {
        "open": re.compile(
            r"^\s*(?:(?:static|virtual|const|inline|unsigned|signed|extern|template"
            r"|public|private|protected|friend|explicit|constexpr|noexcept"
            r"|decltype|auto|void|int|char|bool|float|double|size_t|uint\w+|int\w+"
            r"|class|struct|enum|union|namespace)\s+)*"
            r"(?:class|struct|enum|union|namespace|if|else|for|while|do|switch|case"
            r"|try|catch|finally|with|def|fn|func|function|proc|method|impl|trait"
            r"|interface|type|module|package)\b"
        ),
        "close": re.compile(r"^\s*[\}\)]"),
    },
    "python": {
        "open": re.compile(
            r"^(?:class|def|async\s+def|if|elif|else|for|while|try|except|finally"
            r"|with|match|case)\b"
        ),
        "close": re.compile(r"^\S"),
    },
    "brace": {
        "open": re.compile(r"\{"),
        "close": re.compile(r"\}"),
    },
}

_EXT_LANG = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".c": "c_like", ".h": "c_like", ".cpp": "c_like", ".hpp": "c_like",
    ".cc": "c_like", ".cxx": "c_like", ".hxx": "c_like", ".hh": "c_like",
    ".cs": "c_like", ".java": "c_like", ".kt": "c_like", ".scala": "c_like",
    ".go": "c_like", ".rs": "c_like", ".swift": "c_like", ".m": "c_like",
    ".mm": "c_like", ".inl": "c_like",
    ".js": "c_like", ".ts": "c_like", ".jsx": "c_like", ".tsx": "c_like",
    ".php": "c_like", ".rb": "c_like", ".lua": "c_like", ".dart": "c_like",
}


def _detect_lang(filepath: str) -> str:
    return _EXT_LANG.get(Path(filepath).suffix.lower(), "brace")


# --------------------------------------------------------------------------- #
# Pattern compilation
# --------------------------------------------------------------------------- #

def _split_pattern_top_level(pattern: str) -> List[str]:
    """Split a query on ``|`` ONLY at the top level of the regex.

    A grouped pattern like ``(retry|backoff)_delay`` is ONE pattern — a naive
    ``str.split('|')`` would shear it into ``(retry`` and ``backoff)_delay``,
    two broken fragments that crash compilation. Escapes and nesting inside
    parens/brackets are respected; a ``|`` inside them never splits.
    """
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            buf.append(pattern[i:i + 2])
            i += 2
            continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth = max(0, depth - 1)
        if c == "|" and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


# A quantified group whose body is itself quantified — (a+)+, (x*)*, (\d+)*
# and friends. Python's re engine backtracks exponentially on these: a 200
# character non-matching line against (a+)+$ pegs a core indefinitely, and
# re has no timeout, so the call never returns. Patterns here are written by
# an LLM as often as by a human, so this must be caught rather than trusted.
_REDOS_RE = re.compile(r"""
    \(                      # group open
      (?![?]P?[=!<])        # skip lookarounds / named-group prefixes
      [^()]*                # body without nesting
      (?:[+*]|\{\d+,\s*\}?) # ... containing an unbounded quantifier
      [^()]*
    \)
    \s*
    (?:[+*]|\{\d+,\s*\}?)   # ... and quantified again on the outside
""", re.VERBOSE)

# Minified bundles and generated data files carry single lines megabytes long.
# Regex cost is superlinear in line length for many patterns, so lines past
# this are skipped rather than matched.
_MAX_LINE_CHARS = 5000


def _reject_redos(parts: List[str]) -> None:
    """Raise only when EVERY fragment is unsafe.

    Individual unsafe fragments are defused to literals by the compilers
    below, matching the existing rule that one bad fragment must not kill
    the whole query: 'compute_total|(a+)+$' should still find the function.
    A query made of nothing but bombs would silently match nothing, so that
    one is worth an explicit error instead."""
    if parts and all(_REDOS_RE.search(p) for p in parts):
        raise ValueError(
            "pattern rejected: nested quantifier (e.g. '(a+)+') can cause "
            "catastrophic backtracking and hang the search. Rewrite without "
            "the inner quantifier, for example 'a+' instead of '(a+)+'."
        )


def _compile_or_escape(pattern: str, flags: int = re.IGNORECASE) -> re.Pattern:
    """Compile a fragment; an invalid regex degrades to a literal search rather
    than killing the whole query. Unsafe fragments degrade to literals too:
    a slow correct answer is fine, an infinite one is not."""
    if _REDOS_RE.search(pattern):
        return re.compile(re.escape(pattern), flags)
    try:
        return re.compile(pattern, flags)
    except re.error:
        return re.compile(re.escape(pattern), flags)


def _compile_multi_pattern(patterns: List[str], flags: int = re.IGNORECASE) -> re.Pattern:
    """Join pattern fragments into a single alternation so a line can be tested
    against all of them in one pass. One bad fragment must not kill the query."""
    if not patterns:
        return re.compile(r"(?!)", flags)  # matches nothing
    patterns = [re.escape(p) if _REDOS_RE.search(p) else p for p in patterns]
    parts = [f"(?:{p})" for p in patterns]
    try:
        return re.compile("|".join(parts), flags)
    except re.error:
        parts = [f"(?:{_compile_or_escape(p, flags).pattern})" for p in patterns]
        return re.compile("|".join(parts), flags)


_PY_HEADER_RE = re.compile(r"^(\s*)(?:async\s+def\s|def\s|class\s)")


def _extract_symbol(line: str) -> Optional[str]:
    m = re.search(
        r"(?:class|struct|enum|def|fn|func|function|proc|method|impl|trait|interface"
        r"|void|int|char|bool|auto|static|virtual|const)\s+(\w+)",
        line,
    )
    if m:
        return m.group(1)
    m = re.search(r"\b(\w+)\s*\(", line)
    if m:
        return m.group(1)
    return None


def _python_enclosing_block(lines: List[str], target: int) -> Optional[Tuple[int, int, Optional[str]]]:
    """Return ``(start, end, symbol)`` for the innermost def/class enclosing
    ``target``, or ``None`` if the match sits inside no def/class. ``end`` is
    exclusive and spans the whole block body by indentation."""
    m0 = _PY_HEADER_RE.match(lines[target])
    if m0:
        header = target
        header_indent = len(m0.group(1))
    else:
        target_indent = None
        for k in range(target, -1, -1):
            if lines[k].strip():
                target_indent = len(lines[k]) - len(lines[k].lstrip())
                break
        if target_indent is None:
            return None
        header = None
        header_indent = 0
        for i in range(target - 1, -1, -1):
            m = _PY_HEADER_RE.match(lines[i])
            if m and len(m.group(1)) < target_indent:
                header = i
                header_indent = len(m.group(1))
                break
        if header is None:
            return None

    end = len(lines)
    for j in range(header + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[j]) - len(lines[j].lstrip())
        if indent <= header_indent:
            end = j
            break
    return (header, end, _extract_symbol(lines[header]))


def _find_enclosing_boundary(lines: List[str], target: int, lang: str) -> Tuple[int, int, Optional[str]]:
    if lang == "python":
        block = _python_enclosing_block(lines, target)
        if block is not None:
            return block
        return (max(0, target - 20), min(len(lines), target + 21), None)

    patterns = _LANG_PATTERNS.get(lang, _LANG_PATTERNS["brace"])
    depth = 0
    for i in range(target, -1, -1):
        line = lines[i]
        opens = len(patterns["open"].findall(line))
        closes = len(patterns["close"].findall(line))
        depth += closes - opens
        if depth <= 0 and patterns["open"].search(line):
            end = i
            d = 0
            for j in range(i, min(len(lines), i + 500)):
                d += len(_LANG_PATTERNS["brace"]["open"].findall(lines[j]))
                d -= len(_LANG_PATTERNS["brace"]["close"].findall(lines[j]))
                if d <= 0 and j > i:
                    end = j
                    break
            return (i, min(end, target + 100), _extract_symbol(line))
    return (max(0, target - 20), min(len(lines), target + 21), None)


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

_COMMON_TERMS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "then",
    "return", "void", "int", "char", "bool", "float", "double", "auto",
    "class", "struct", "enum", "public", "private", "protected", "static",
    "virtual", "const", "inline", "true", "false", "null", "nullptr",
    "this", "self", "new", "delete", "using", "namespace", "import",
    "export", "def", "if", "else", "for", "while", "and", "or", "not",
    "none", "pass", "try", "except", "finally", "with", "lambda", "yield",
    "async", "await",
})


def _rarity(term: str) -> float:
    t = term.lower()
    if t in _COMMON_TERMS:
        return 0.1
    if len(t) <= 2:
        return 0.2
    if len(t) <= 4:
        return 0.4
    if t.isupper():
        return 0.8
    if "_" in t or "-" in t:
        return 0.7
    return 0.5


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Chunk:
    """A ranked slice of a file that matched the query."""

    file: str
    line_start: int
    line_end: int
    code: str
    patterns: List[str]
    matches: int
    symbol: str = ""
    score: float = 0.0
    rank_reason: List[str] = field(default_factory=list)
    hash: str = ""
    tokens: int = 0

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = hashlib.md5(self.code.encode("utf-8", "replace")).hexdigest()[:8]
        if not self.tokens:
            self.tokens = estimate_tokens(self.code)

    def header(self) -> str:
        sym = f" fn={self.symbol}" if self.symbol else ""
        pats = ",".join(self.patterns[:5])
        return (
            f"[{Path(self.file).name}:{self.line_start}-{self.line_end}"
            f"{sym} matches={self.matches} patterns={pats}"
            f" hash={self.hash} score={int(self.score)}]"
        )

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "symbol": self.symbol or None,
            "patterns": self.patterns,
            "matches": self.matches,
            "score": int(self.score),
            "rank_reason": self.rank_reason,
            "hash": self.hash,
            "tokens": self.tokens,
            "code": self.code,
        }


# --- v0.4: NL-query + subword semantic matching ---------------------------

_STEM_SUFFIXES = ("ation", "izing", "ized", "ing", "ies", "ion", "ers",
                  "ed", "es", "er", "s")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NL_STOP = frozenset("""
    the a an and or of to in for with when where how why what which does do
    did is are was were be been should could would can will my our this that
    these those it its on at by from into over under not no if then than so
    make makes made use uses using get gets sets set
""".split())


def _stem(t: str) -> str:
    """Tiny suffix stemmer: maps invalidation/invalidate/invalidating to one
    stem so query vocabulary meets code vocabulary despite morphology."""
    for suf in _STEM_SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


def _sem_tokens(text: str) -> List[str]:
    """Tokens for the semantic passes: words plus snake_case/camelCase
    subwords, stemmed. 'CacheInvalidator' yields cache + invalid (+ itself),
    which is where lexical matching usually loses to embeddings."""
    out: List[str] = []
    for w in re.findall(r"[A-Za-z0-9_]{3,}", text):
        subs: List[str] = []
        for part in w.split("_"):
            subs.extend(_CAMEL_RE.split(part))
        kept = 0
        for s in subs:
            s = s.lower()
            if len(s) >= 3:
                out.append(_stem(s))
                kept += 1
        if kept > 1:
            out.append(w.lower())
    return out


def _expand_nl_query(pattern: str, patterns: List[str]) -> List[str]:
    """If the query reads as a natural-language phrase (spaces, no regex
    syntax), expand it into content-word patterns for the lexical pass, plus
    synthesized snake_case bigrams ('cache invalidation' also tries
    'cache_invalidation'). The semantic passes see the full phrase anyway."""
    if len(patterns) != 1 or " " not in pattern.strip():
        return patterns
    if re.search(r"[|\\^$()\[\]?*+{]", pattern):
        return patterns
    words = [w for w in re.findall(r"[A-Za-z0-9_]+", pattern)
             if len(w) >= 3 and w.lower() not in _NL_STOP]
    # Two-word queries ("class Context", "def score") are exact lookups, not
    # prose — shattering them into single common words floods the corpus with
    # noise (measured: v2 symbol family 82.5% -> 72.5%). Only expand real
    # sentences: three or more content words.
    if len(words) < 3:
        return patterns
    bigrams = [f"{words[i]}_{words[i+1]}" for i in range(len(words) - 1)]
    return words + bigrams


# A line that *defines* something (any supported language family).
_DEF_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:pub\s+)?(?:static\s+)?(?:async\s+)?"
    r"(?:def|class|fn|func|function|impl|trait|interface|struct|enum|type)\b"
)


class _Scorer:
    def __init__(self, patterns: List[str]) -> None:
        self.compiled = [_compile_or_escape(p) for p in patterns]
        self.pattern_strs = patterns
        self.combined = _compile_multi_pattern(patterns)

    def score(self, chunk: Chunk) -> Chunk:
        reasons: List[str] = []
        score = 0.0

        score += min(chunk.matches * 5, 25)
        if chunk.matches >= 3:
            reasons.append(f"multi_match({chunk.matches})")

        lines = chunk.code.splitlines()

        # One combined-regex prefilter per line; per-pattern work only on
        # matching lines (profiling: the naive per-pattern loop was ~50% of
        # warm-call latency on multi-pattern NL queries).
        match_lines = [l for l in lines
                       if len(l) <= _MAX_LINE_CHARS and self.combined.search(l)]

        matched = set()
        for line in match_lines:
            for i, pat in enumerate(self.compiled):
                if pat.search(line):
                    matched.add(i)
        if len(matched) >= 2:
            score += 15
            reasons.append("co_occurrence")
        if len(self.compiled) > 1 and len(matched) == len(self.compiled):
            score += 10
            reasons.append("all_patterns")

        rare = 0
        for line in match_lines:
            for term in re.findall(r"\w+", line):
                if _rarity(term) > 0.6:
                    rare += 1
        if rare:
            score += min(rare * 3, 15)
            reasons.append("rare_terms")

        # A query pattern matching ON a definition line is the strongest
        # possible signal: the user almost always wants the definition, and
        # usage-heavy chunks must not be able to crowd it out of the budget.
        # (This must not depend on chunk.symbol — that is only populated in
        # boundary="fn" mode, and the definition signal has to fire in the
        # default mode too.)
        for line in match_lines:
            if _DEF_LINE_RE.match(line):
                score += 25
                reasons.append("definition")
                break
        else:
            if chunk.symbol:
                for line in lines:
                    if chunk.symbol in line and any(kw in line for kw in (
                        "class", "struct", "def", "fn", "func", "function",
                        "proc", "void", "int", "char", "bool", "auto", "impl",
                        "trait",
                    )):
                        score += 12
                        reasons.append("definition")
                        break

        if chunk.symbol:
            has_body = any(("{" in ln or ":" in ln) for ln in lines)
            if not has_body and chunk.symbol in chunk.code:
                score -= 5
                reasons.append("declaration_only")

        joined = " ".join(self.pattern_strs).lower()
        # _is_test_path checks the file NAME and tests/ dirs — a naive
        # substring test on the full path let a parent dir named "testing"/
        # "contest"/pytest tmp dirs demote every file in the repo.
        if _is_test_path(chunk.file) and "test" not in joined:
            score -= 8
            reasons.append("test_demoted")

        if any(s in chunk.file.lower() for s in (
            "vendor", "node_modules", "__pycache__", ".git", "build", "dist",
            "generated", "auto-generated", ".min.js", ".bundle",
        )):
            score -= 15
            reasons.append("vendor_demoted")

        commentish = sum(
            1 for ln in lines if ln.strip().startswith(("//", "/*", "*", "#", "--"))
        )
        if lines and commentish / len(lines) > 0.7:
            score -= 10
            reasons.append("mostly_comments")

        chunk.score = max(0.0, score)
        chunk.rank_reason = reasons
        return chunk


class _Deduplicator:
    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold

    @staticmethod
    def _line_set(text: str) -> set:
        return {l.strip() for l in text.splitlines() if l.strip()}

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def dedupe(self, chunks: List[Chunk]) -> Tuple[List[Chunk], List[Tuple[Chunk, str]]]:
        kept: List[Chunk] = []
        kept_sets: List[set] = []
        removed: List[Tuple[Chunk, str]] = []
        seen_hashes: set = set()
        for c in chunks:
            if c.hash in seen_hashes:
                removed.append((c, f"exact_dup_of_{c.hash}"))
                continue
            lset = self._line_set(c.code)
            dup = False
            for i, kset in enumerate(kept_sets):
                sim = self._jaccard(lset, kset)
                if sim >= self.threshold:
                    removed.append((c, f"near_dup_{int(sim * 100)}%_of_{kept[i].hash}"))
                    dup = True
                    break
            if not dup:
                kept.append(c)
                kept_sets.append(lset)
                seen_hashes.add(c.hash)
        return kept, removed


class _NegativeEvidence:
    """Reports what was NOT found. An empty result is a real answer, not a
    failed search — negative evidence makes that explicit and about the FILE,
    not merely about which chunks survived budget/dedupe."""

    def __init__(self, patterns: List[str], search_path: str) -> None:
        self.patterns = patterns
        self.search_path = search_path
        self.absences: List[str] = []
        self._checked: set = set()

    def check_definition(self, symbol: str, chunks: List[Chunk]) -> None:
        if symbol in self._checked:
            return
        self._checked.add(symbol)
        for c in chunks:
            if c.symbol == symbol:
                for line in c.code.splitlines():
                    if symbol in line and any(kw in line for kw in (
                        "class", "struct", "def", "fn", "func", "function",
                        "proc", "void", "int", "char", "bool", "auto", "impl",
                        "trait",
                    )):
                        return
        self.absences.append(f"No definition found for '{symbol}' in {self.search_path}")

    def check_pattern(self, pattern: str, chunks: List[Chunk], full_text: str = "") -> None:
        rx = _compile_or_escape(pattern)
        if any(rx.search(c.code) for c in chunks):
            return
        if full_text and rx.search(full_text):
            self.absences.append(
                f"Pattern '{pattern}' IS in {self.search_path} but its match "
                f"fell outside the selected chunks (budget/dedupe)"
            )
        else:
            self.absences.append(f"Pattern '{pattern}' not found in {self.search_path}")


@dataclass
class Result:
    """The outcome of a :func:`focused_read` call."""

    query: List[str]
    chunks: List[Chunk]
    negative_evidence: List[str] = field(default_factory=list)
    files_searched: int = 1
    files_matched: int = 0
    budget: int = 0
    deduped: int = 0

    @property
    def total_tokens(self) -> int:
        return sum(c.tokens for c in self.chunks)

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "chunks": [c.to_dict() for c in self.chunks],
            "negative_evidence": self.negative_evidence,
            "total_tokens": self.total_tokens,
            "files_searched": self.files_searched,
            "files_matched": self.files_matched,
            "budget": self.budget,
            "deduped": self.deduped,
        }

    def to_json(self, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def render(self) -> str:
        """Render the human/LLM-facing text report."""
        parts: List[str] = []
        recursive = self.files_searched > 1
        head = (
            f"=== slicegrep{' recursive' if recursive else ''}: "
            f"{len(self.chunks)} chunk(s), ~{self.total_tokens} tokens"
            f"{f' / {self.budget} budget' if self.budget else ''}"
        )
        if recursive:
            head += f", {self.files_searched} files searched, {self.files_matched} matched"
        head += " ==="
        parts.append(head)
        parts.append(f"\nQUERY:\n{'|'.join(self.query)}")

        if len(self.chunks) > 1:
            parts.append("\nRANKING:")
            for i, c in enumerate(self.chunks[:10], 1):
                reason = ", ".join(c.rank_reason) if c.rank_reason else "direct_match"
                sym = f" fn={c.symbol}" if c.symbol else ""
                parts.append(f"  {i}. {Path(c.file).name}:{c.line_start}{sym} — {reason}")

        parts.append("\n---")
        for c in self.chunks:
            parts.append(f"\n{c.header()}\n{c.code}")

        if self.deduped:
            parts.append(f"\n[DEDUPED: {self.deduped} near-duplicate chunk(s) removed]")

        if self.negative_evidence:
            parts.append("\nNEGATIVE EVIDENCE:")
            parts.extend(f"  - {a}" for a in self.negative_evidence)

        return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

def _semantic_rerank(chunks: List[Chunk], patterns: List[str],
                     query_text: str = "") -> None:
    """Blend a lightweight TF-IDF cosine signal into lexical scores.

    Regex matching decides *candidacy*; this stage improves *ranking* for
    concept-style queries ("cache expiry refresh") where the best chunk is the
    one whose overall vocabulary matches the query, not the one with the most
    literal hits. IDF is computed over the candidate set itself — no corpus
    index, no dependencies, negligible cost.
    """
    # PRECISION pass: exact vocabulary only. Subword/stemmed tokens (see
    # _sem_tokens) measurably hurt here — common stems inflate similarity for
    # wrong chunks (v2 benchmark: 63.9% -> 57.7% when this pass used them).
    # The aggressive tokenizer belongs in the RECALL pass only.
    if len(chunks) < 2:
        return
    src_text = query_text or " ".join(patterns)
    qtokens = Counter(re.findall(r"[a-z0-9_]{3,}", src_text.lower()))
    if not qtokens:
        return
    docs = [Counter(re.findall(r"[a-z0-9_]{3,}", c.code.lower())) for c in chunks]
    df: Counter = Counter()
    for d in docs:
        df.update(d.keys())
    n = len(docs)
    idf = {t: math.log(1 + n / (1 + c)) for t, c in df.items()}
    qvec = {t: c * idf.get(t, math.log(1 + n)) for t, c in qtokens.items()}
    qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
    for chunk, d in zip(chunks, docs):
        dot = sum(qvec.get(t, 0.0) * c * idf.get(t, 0.0) for t, c in d.items())
        if dot <= 0:
            continue
        dnorm = math.sqrt(sum((c * idf.get(t, 0.0)) ** 2 for t, c in d.items())) or 1.0
        cos = dot / (qnorm * dnorm)
        bonus = int(round(12 * cos))
        if bonus:
            chunk.score += bonus
            if bonus >= 4 and "semantic" not in chunk.rank_reason:
                chunk.rank_reason.append("semantic")


# --- v0.5: corpus cache + block-aligned BM25 recall ------------------------
# Findings that drove this (12-baseline benchmark round):
#   * BM25 tied slicegrep on the controlled suite at ~1/1000th the latency
#     -> adopt its term-saturation scoring; cache the corpus in-process
#       (every baseline got an in-memory index; slicegrep re-read the tree
#       per call. The MCP server is long-lived, so caching is fair and real.)
#   * TF-IDF over ast-aligned chunks WON the real-sessions suite outright
#     -> real fixes modify whole functions; recall chunks now align to
#       definition boundaries instead of fixed 60-line windows.

_BM25_K1 = 1.2
_BM25_B = 0.75

# --- history priors: churn + co-change (novel signal) ----------------------
# Every retrieval strategy we benchmarked treats the repo as static text.
# But the question an agent actually asks is "what will this change touch?",
# and decades of defect-prediction research say the best predictor of change
# is prior change. When the corpus is a git repo, we mine two priors from
# `git log` (ancestors of HEAD only — no future leakage):
#   churn[f]        recency-weighted change frequency per file
#   cochange[a][b]  how often files changed in the same commit
# and fuse them with text evidence:  score = text * (1 + w * prior).

# Optional dense stage: model2vec static embeddings (potion-code-16M).
# STRICTLY additive and optional — stdlib-only behaviour is unchanged when
# model2vec is not installed or SLICEGREP_DENSE=off. Motivated by the n=289
# mistake-overlap analysis: dense and lexical retrieval make substantially
# DIFFERENT mistakes (oracle union 41.2% vs 30.4% best single), so fusion
# has real headroom; this is complementary evidence, not replacement.
_DENSE_STATE: Dict[str, object] = {"model": None, "tried": False}


def _dense_enabled() -> bool:
    import os
    return os.environ.get("SLICEGREP_DENSE", "on") != "off"


def _lex_share() -> float:
    import os
    try:
        return float(os.environ.get("SLICEGREP_LEX_SHARE", "0.65"))
    except ValueError:
        return 0.65


def _dense_weight() -> float:
    import os
    try:
        return float(os.environ.get("SLICEGREP_DENSE_W", "1.0"))
    except ValueError:
        return 1.0


def _dense_model():
    """Load the optional dense model, bounded by a deadline.

    from_pretrained may reach the network on a cold cache. A try/except
    catches a refused connection but not a stalled one, and a stalled load
    looks exactly like a frozen search to whatever is waiting on it. The
    load therefore runs on a daemon thread with a timeout: dense retrieval
    is an optional accuracy boost and is never worth blocking a query for.
    """
    if _DENSE_STATE["tried"]:
        return _DENSE_STATE["model"]
    _DENSE_STATE["tried"] = True
    if not _dense_enabled():
        return None

    import os
    import threading
    try:
        deadline = float(os.environ.get("SLICEGREP_DENSE_TIMEOUT", "15"))
    except ValueError:
        deadline = 15.0

    box: Dict[str, object] = {}

    def _load():
        try:
            from model2vec import StaticModel
            box["m"] = StaticModel.from_pretrained(
                "minishlab/potion-code-16M-v2")
        except Exception:
            box["m"] = None

    th = threading.Thread(target=_load, daemon=True)
    th.start()
    th.join(deadline)
    _DENSE_STATE["model"] = box.get("m")
    return _DENSE_STATE["model"]


_HISTORY_CACHE: Dict[str, Optional[dict]] = {}

# Fusion weights (log-space additive — history reshapes the candidate space,
# it never independently decides relevance):
#   S(f|q) = ALPHA*S_text + BETA*S_churn + GAMMA*S_cochange
_ALPHA_TEXT = 1.0
_BETA_CHURN = 0.3
_GAMMA_COCHANGE = 0.5
_CHURN_TAU_S = 90 * 86400          # 90-day temporal decay
_HIST_COMMITS = 600

# Ablation control for the benchmark table (env: SLICEGREP_HISTORY =
# full | off | churn-only | cochange-only | shuffled). "shuffled" preserves
# every file's activity level but destroys file RELATIONSHIPS — if the gain
# survives shuffling, it was popularity, not topology.
def _history_mode() -> str:
    import os
    return os.environ.get("SLICEGREP_HISTORY", "full")


def _history_priors(root: Path) -> Optional[dict]:
    """Temporally-decayed churn + co-change statistics from ancestor-only
    git history. w(commit,f) = log(1+changed_lines)/sqrt(files_in_commit),
    so a 300-file formatting sweep contributes ~nothing per file."""
    if _history_mode() == "off":
        return None
    key = str(root.resolve())
    if key in _HISTORY_CACHE:
        return _HISTORY_CACHE[key]
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "log", "--numstat",
             "--pretty=format:%ct", "-n", str(_HIST_COMMITS)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        if proc.returncode != 0:
            _HISTORY_CACHE[key] = None
            return None
        churn_raw: Dict[str, float] = {}
        df: Counter = Counter()          # commits containing f
        pair: Dict[str, Counter] = {}    # co-occurrence counts
        ncommits = 0
        newest_ts = None
        for block in proc.stdout.split(""):
            block = block.strip()
            if not block:
                continue
            lines = block.splitlines()
            try:
                ts = int(lines[0].strip())
            except ValueError:
                continue
            if newest_ts is None:
                newest_ts = ts           # evaluate "now" at repo head time
            files: Dict[str, int] = {}
            for l in lines[1:]:
                parts = l.split("	")
                if len(parts) != 3:
                    continue
                a, d, p = parts
                try:
                    changed = (0 if a == "-" else int(a)) +                               (0 if d == "-" else int(d))
                except ValueError:
                    continue
                files[p.replace("\\", "/")] = changed
            if not files:
                continue
            ncommits += 1
            decay = math.exp(-max(0, newest_ts - ts) / _CHURN_TAU_S)
            denom = math.sqrt(len(files))
            for f, changed in files.items():
                w = math.log(1 + changed) / denom
                churn_raw[f] = churn_raw.get(f, 0.0) + w * decay
                df[f] += 1
            if 2 <= len(files) <= 10:
                for a in files:
                    for b in files:
                        if a != b:
                            pair.setdefault(a, Counter())[b] += 1
        if not churn_raw or ncommits < 5:
            _HISTORY_CACHE[key] = None
            return None
        churn = {f: math.log(1 + v) for f, v in churn_raw.items()}
        mx = max(churn.values()) or 1.0
        churn = {f: v / mx for f, v in churn.items()}
        if _history_mode() == "shuffled":
            import random as _r
            rng = _r.Random(0)
            keys = list(churn)
            perm = keys[:]
            rng.shuffle(perm)
            remap = dict(zip(keys, perm))
            churn = {remap[f]: v for f, v in churn.items()}
            pair = {remap.get(a, a): Counter(
                {remap.get(b, b): c for b, c in cnts.items()})
                for a, cnts in pair.items()}
        entry = {"churn": churn, "pair": pair, "df": df,
                 "ncommits": ncommits, "root": key}
        entry.update(_mine_symbol_graph(root))
        _HISTORY_CACHE[key] = entry
        return entry
    except Exception:
        _HISTORY_CACHE[key] = None
        return None


_SYM_STOP = frozenset("""
    import return yield raise assert lambda global nonlocal print super
    self None True False class match case while elif else except finally
""".split())
_HUNK_SYM_RE = re.compile(r"^@@[^@]*@@\s*(.*)$")
_DIFF_FILE_RE = re.compile(r"^diff --git a/.* b/(.*)$")


def _mine_symbol_graph(root: Path) -> dict:
    """REGION-level temporal co-change: which SYMBOLS change together.

    Git hunk headers carry the enclosing function/class line (xfuncname), so
    the symbol graph mines straight from `git log -p` headers — no historical
    file reconstruction. Per commit we collect the set of changed symbols;
    lift-adjusted pair statistics then say things like "when parse_expression
    changes, ParseError and test_parser usually change too"."""
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "log", "-p", "-U0", "-n", "200",
             "--pretty=format:"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        if proc.returncode != 0:
            return {"symdf": Counter(), "sympair": {}, "nsymcommits": 0}
        symdf: Counter = Counter()
        sympair: Dict[str, Counter] = {}
        ncommits = 0
        for commit in proc.stdout.split(""):
            syms = set()
            for line in commit.splitlines():
                m = _HUNK_SYM_RE.match(line)
                if not m:
                    continue
                ctx = m.group(1).strip()
                if not ctx:
                    continue
                sym = _extract_symbol(ctx)
                if (sym and len(sym) >= 4 and not sym.startswith("_")
                        and sym.lower() not in _SYM_STOP):
                    syms.add(sym)
            if not syms:
                continue
            ncommits += 1
            for a in syms:
                symdf[a] += 1
            if 2 <= len(syms) <= 12:
                for a in syms:
                    for b in syms:
                        if a != b:
                            sympair.setdefault(a, Counter())[b] += 1
        return {"symdf": symdf, "sympair": sympair, "nsymcommits": ncommits}
    except Exception:
        return {"symdf": Counter(), "sympair": {}, "nsymcommits": 0}


def _symbol_expansions(priors: dict, anchor_syms: List[str],
                       k: int = 4) -> List[Tuple[str, float]]:
    """History-conditioned QUERY EXPANSION: given the symbols the current
    query's evidence points at, return the symbols that historically change
    with them (lift-adjusted), as NEW retrieval objectives the query never
    mentioned. This is history as a source of missing search objectives,
    not as a ranking feature."""
    n = max(1, priors.get("nsymcommits", 0))
    symdf = priors.get("symdf", Counter())
    sympair = priors.get("sympair", {})
    if not sympair or not anchor_syms:
        return []
    eps = 1.0 / (4 * n)
    out: Dict[str, float] = {}
    for a in anchor_syms:
        cnts = sympair.get(a)
        if not cnts:
            continue
        pa = symdf.get(a, 0) / n
        for b, c in cnts.items():
            if b in anchor_syms:
                continue
            r = math.log(((c / n) + eps) / ((symdf.get(b, 0) / n) * pa + eps))
            if r > 0:
                out[b] = max(out.get(b, 0.0), r)
    ranked = sorted(out.items(), key=lambda kv: kv[1], reverse=True)[:k]
    mx = ranked[0][1] if ranked else 1.0
    return [(sym, w / mx) for sym, w in ranked]


def _cochange_scores(priors: dict, anchors: List[Tuple[str, float]]) -> Dict[str, float]:
    """Significance-adjusted co-change: R(f,a) = log((P(f,a)+e)/(P(f)P(a)+e)),
    aggregated over textual anchors weighted by softmax of their text scores.
    Files rise because they have an UNUSUALLY strong relationship with files
    the current query supports — not because they are popular."""
    if not anchors:
        return {}
    n = max(1, priors["ncommits"])
    df = priors["df"]
    eps = 1.0 / (4 * n)
    mxs = max(sc for _f, sc in anchors) or 1.0
    exps = [math.exp((sc / mxs) * 2.0) for _f, sc in anchors]
    tot = sum(exps) or 1.0
    weights = [e / tot for e in exps]
    out: Dict[str, float] = {}
    for (a, _sc), wa in zip(anchors, weights):
        cnts = priors["pair"].get(a)
        if not cnts:
            continue
        pa = df.get(a, 0) / n
        for f, c in cnts.items():
            pfa = c / n
            pf = df.get(f, 0) / n
            r = math.log((pfa + eps) / (pf * pa + eps))
            if r > 0:
                out[f] = out.get(f, 0.0) + wa * r
    if out:
        mx = max(out.values())
        out = {f: v / mx for f, v in out.items()}
    return out


def _rel_posix(file: str, root_key: str) -> str:
    p = file.replace("\\", "/")
    r = root_key.replace("\\", "/").rstrip("/") + "/"
    return p[len(r):] if p.startswith(r) else p

_CORPUS_CACHE: Dict[str, dict] = {}


def _scan_tree(root: Path) -> List[Tuple[str, int, float]]:
    """One fast os.scandir walk: (path, size, mtime) for searchable files.
    (pathlib rglob + stat was ~25% of warm-call latency on Windows.)"""
    import os
    out: List[Tuple[str, int, float]] = []
    stack = [str(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    name = e.name
                    if e.is_dir(follow_symlinks=False):
                        if name not in SKIP_DIRS:
                            stack.append(e.path)
                        continue
                    dot = name.rfind(".")
                    if dot < 0 or name[dot:].lower() not in SEARCHABLE_EXTS:
                        continue
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_size > _MAX_FILE_BYTES:
                        continue
                    out.append((e.path, st.st_size, st.st_mtime))
        except OSError:
            continue
    out.sort()
    return out


def _corpus(root: Path) -> dict:
    """In-process, mtime-validated cache of file text + the semantic index."""
    key = str(root.resolve())
    scan = _scan_tree(root)
    sig = (len(scan), sum(s for _p, s, _m in scan),
           max((m for _p, _s, m in scan), default=0.0))
    entry = _CORPUS_CACHE.get(key)
    if entry is not None and entry["sig"] == sig:
        return entry
    files = []
    for fpath, _size, _mtime in scan:
        try:
            text = _read_text(Path(fpath))
        except OSError:
            continue
        files.append((fpath, text, text.splitlines(keepends=True)))
    entry = {"sig": sig, "files": files, "sem": None}
    _CORPUS_CACHE[key] = entry
    return entry


def _block_spans(lines: List[str]) -> List[Tuple[int, int]]:
    """Split a file at shallow definition boundaries (indent <= 4), so recall
    chunks align with the functions/classes a change actually touches. Long
    blocks are windowed; a leading preamble span is kept."""
    bounds = [
        i for i, l in enumerate(lines)
        if _DEF_LINE_RE.match(l) and (len(l) - len(l.lstrip())) <= 4
    ]
    if not bounds:
        return [(lo, min(len(lines), lo + 60))
                for lo in range(0, max(1, len(lines)), 40)]
    spans = []
    if bounds[0] > 0:
        spans.append((0, bounds[0]))
    for j, lo in enumerate(bounds):
        hi = bounds[j + 1] if j + 1 < len(bounds) else len(lines)
        while hi - lo > 90:                     # window long blocks
            spans.append((lo, lo + 70))
            lo += 60
        spans.append((lo, hi))
    return [(lo, hi) for lo, hi in spans if hi > lo]


def _sem_index(entry: dict) -> dict:
    """Block-aligned BM25 index over the cached corpus, built once."""
    if entry["sem"] is not None:
        return entry["sem"]
    chunks = []            # (path, lo, hi, tf Counter, doclen)
    df: Counter = Counter()
    total_len = 0
    for path, _text, lines in entry["files"]:
        for lo, hi in _block_spans(lines):
            tf = Counter(_sem_tokens("".join(lines[lo:hi])))
            if not tf:
                continue
            dl = sum(tf.values())
            chunks.append((path, lo, hi, tf, dl))
            df.update(tf.keys())
            total_len += dl
    n = max(1, len(chunks))
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    postings: Dict[str, List[int]] = {}
    for ci, (_p, _lo, _hi, tf, _dl) in enumerate(chunks):
        for t in tf:
            postings.setdefault(t, []).append(ci)
    entry["sem"] = {
        "chunks": chunks, "idf": idf, "postings": postings,
        "avgdl": (total_len / n) if n else 1.0, "embs": None,
    }
    model = _dense_model()
    if model is not None and chunks:
        try:
            import numpy as np
            lines_of = {p: ls for p, _t, ls in entry["files"]}
            texts = ["".join(lines_of[p][lo:hi]) for p, lo, hi, _tf, _dl in chunks]
            embs = model.encode(texts)
            embs = embs / (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9)
            entry["sem"]["embs"] = embs
        except Exception:
            entry["sem"]["embs"] = None
    return entry["sem"]


def _semantic_candidates_bm25(entry: dict, patterns: List[str],
                              query_text: str = "",
                              max_chunks: int = 40,
                              use_dense: bool = True) -> List[Chunk]:
    """BM25-scored, definition-aligned recall candidates."""
    sem = _sem_index(entry)
    src_text = query_text or " ".join(patterns)
    qtokens = Counter(_sem_tokens(src_text))
    for t in re.findall(r"[a-z0-9_]{3,}", src_text.lower()):
        qtokens[_stem(t)] += 2                  # whole words outrank fragments
    if not qtokens or not sem["chunks"]:
        return []
    idf, avgdl = sem["idf"], sem["avgdl"]
    # inverted index: only score chunks containing at least one query token
    cand_ids = set()
    for t in qtokens:
        cand_ids.update(sem["postings"].get(t, ()))
    scored = []
    chunks_arr = sem["chunks"]
    for ci in cand_ids:
        path, lo, hi, tf, dl = chunks_arr[ci]
        s = 0.0
        norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl)
        for t, qw in qtokens.items():
            f = tf.get(t)
            if not f:
                continue
            s += qw * idf.get(t, 0.0) * (f * (_BM25_K1 + 1)) / (f + norm)
        if s > 0:
            scored.append((s, path, lo, hi))
    # Dense fusion: cosine over ALL chunks (dense finds evidence with zero
    # token overlap — that is its whole value), normalized and added to the
    # BM25 signal. Weight via SLICEGREP_DENSE_W (dev-tunable, frozen before
    # any confirmation run).
    dense_sims = None
    if use_dense and sem.get("embs") is not None:
        try:
            import numpy as np
            model = _dense_model()
            qv = model.encode([src_text.replace("\\", " ").replace("|", " ")])[0]
            qv = qv / (np.linalg.norm(qv) + 1e-9)
            dense_sims = sem["embs"] @ qv
        except Exception:
            dense_sims = None
    if dense_sims is not None:
        wd = _dense_weight()
        # rebuild scored with fused values over the union of candidates
        pos = {(chunks_arr[ci][0], chunks_arr[ci][1]): ci for ci in cand_ids}
        bm_map = {}
        for sc, path, lo, hi in scored:
            ci = pos.get((path, lo))
            if ci is not None:
                bm_map[ci] = sc
        bmax = max(bm_map.values()) if bm_map else 1.0
        import numpy as np
        top_dense = np.argsort(dense_sims)[::-1][:40]
        union_ids = set(bm_map) | {int(i) for i in top_dense
                                   if dense_sims[int(i)] > 0}
        dmax = float(dense_sims.max()) or 1.0
        scored = []
        dense_of = {}
        for ci in union_ids:
            path, lo, hi, tf, dl = chunks_arr[ci]
            fb = (bm_map.get(ci, 0.0) / bmax) if bmax else 0.0
            fd = max(0.0, float(dense_sims[ci]) / dmax) if dmax else 0.0
            fused = (fb + wd * fd) / (1.0 + wd)
            if fused > 0:
                scored.append((fused, path, lo, hi))
                dense_of[(path, lo)] = fd
    if not scored:
        return []
    scored.sort(reverse=True)
    # Keep top-N by FUSED score AND top-N by pure dense: the vague-query
    # router needs dense-strong chunks even when their BM25 term overlap is
    # zero — truncating on fused rank alone silently discards them.
    keep = list(scored[:max_chunks])
    if dense_sims is not None and dense_of:
        have = {(p, lo) for _s, p, lo, _hi in keep}
        by_dense = sorted(scored, key=lambda t: dense_of.get((t[1], t[2]), 0.0),
                          reverse=True)
        for t in by_dense[:max_chunks]:
            if (t[1], t[2]) not in have:
                keep.append(t)
                have.add((t[1], t[2]))
    top = scored[0][0] or 1.0
    out = []
    lines_of = {path: lines for path, _t, lines in entry["files"]}
    for s, path, lo, hi in keep:
        chunk = Chunk(
            file=path, line_start=lo + 1, line_end=hi,
            code="".join(lines_of[path][lo:hi]),
            patterns=[], matches=0, symbol="",
        )
        chunk.score = 5 + int(30 * (s / top))
        chunk.rank_reason = ["semantic-recall"]
        try:
            chunk.dense_sim = dense_of.get((path, lo), 0.0)
        except Exception:
            chunk.dense_sim = 0.0
        out.append(chunk)
    return out


def _semantic_candidates(
    file_data: List[Tuple[str, List[str]]],
    patterns: List[str],
    max_chunks: int = 40,
    query_text: str = "",
) -> List[Chunk]:
    """TF-IDF recall pass: windows whose *vocabulary* matches the query.

    Regex decides precision candidates; this decides RECALL. On vague queries
    ("handle prompt suffix when default rejected") the code that matters often
    contains none of the query words on any single line — but a 60-line window
    around it usually shares vocabulary. Benchmark v3 (real sessions mined
    from git history) showed pure TF-IDF retrieval beating regex-gated
    slicegrep 22.5% to 16.2% for exactly this reason.
    """
    src_text = query_text or " ".join(patterns)
    qtokens = Counter(_sem_tokens(src_text))
    # Whole-word matches outrank fragment matches: subword stems are the
    # recall floor (they close the morphology gap on vague queries), but a
    # window sharing the query's exact vocabulary should win over one that
    # merely shares stems (measured: without this, multi-span families pay
    # for the subword dilution).
    for t in re.findall(r"[a-z0-9_]{3,}", src_text.lower()):
        qtokens[_stem(t)] += 2
    if not qtokens or not file_data:
        return []
    windows: List[Tuple[str, int, int, Counter]] = []
    df: Counter = Counter()
    for fpath, lines in file_data:
        for lo in range(0, max(1, len(lines)), 40):
            hi = min(len(lines), lo + 60)
            toks = Counter(_sem_tokens("".join(lines[lo:hi])))
            if toks:
                windows.append((fpath, lo, hi, toks))
                df.update(toks.keys())
            if hi >= len(lines):
                break
    n = len(windows)
    if n < 2:
        return []
    idf = {t: math.log(1 + n / (1 + c)) for t, c in df.items()}
    qvec = {t: c * idf.get(t, math.log(1 + n)) for t, c in qtokens.items()}
    qnorm = math.sqrt(sum(v * v for v in qvec.values())) or 1.0
    scored = []
    for fpath, lo, hi, toks in windows:
        dot = sum(qvec.get(t, 0.0) * c * idf.get(t, 0.0) for t, c in toks.items())
        if dot <= 0:
            continue
        dnorm = math.sqrt(sum((c * idf.get(t, 0.0)) ** 2
                              for t, c in toks.items())) or 1.0
        scored.append((dot / (qnorm * dnorm), fpath, lo, hi))
    scored.sort(reverse=True)
    out = []
    for cos, fpath, lo, hi in scored[:max_chunks]:
        lines = next(ls for fp, ls in file_data if fp == fpath)
        chunk = Chunk(
            file=fpath,
            line_start=lo + 1,
            line_end=hi,
            code="".join(lines[lo:hi]),
            patterns=[],
            matches=0,
            symbol="",
        )
        chunk.score = 5 + int(30 * cos)
        chunk.rank_reason = ["semantic-recall"]
        out.append(chunk)
    return out


def _overlaps(c: Chunk, picked: List[Chunk]) -> bool:
    for p in picked:
        if p.file == c.file and c.line_start <= p.line_end and p.line_start <= c.line_end:
            return True
    return False


def _pack_hybrid(lex: List[Chunk], sem: List[Chunk], budget: int,
                 objective: str = "auto",
                 priors: Optional[dict] = None) -> List[Chunk]:
    """Lexical chunks get first claim on ~65% of the budget; semantic-recall
    chunks fill whatever remains without overlapping what's already in."""
    if not sem:
        return _apply_budget(lex, budget, objective)
    if not lex:
        return _apply_budget(sem, budget, "single")
    # File-first allocation: aggregate evidence per FILE across both lists.
    # When one file dominates (vague queries usually resolve to one or two
    # files, and real fixes concentrate there), give it a contiguous majority
    # share of the budget instead of scattering slices across the repo.
    # (Held-out real-session benchmark: file-level whole-reads beat
    # slice-scatter exactly in this regime.)
    fscore: Dict[str, float] = {}
    for c in lex + sem:
        fscore[c.file] = fscore.get(c.file, 0.0) + c.score
    # History fusion, log-space additive: S = a*text + b*churn + g*cochange.
    # History never independently decides relevance — it reshapes the
    # candidate space the CURRENT query created. Anchors = top text files;
    # co-change is significance-adjusted (lift), so a file rises only when
    # its coupling to the anchors is stronger than chance, not because it is
    # a popular hotspot.
    if priors is not None and fscore:
        mode = _history_mode()
        rk = priors["root"]
        tmax = max(fscore.values()) or 1.0
        text_n = {f: v / tmax for f, v in fscore.items()}
        anchors = [(_rel_posix(f, rk), fscore[f])
                   for f in sorted(fscore, key=lambda f: fscore[f],
                                   reverse=True)[:3]]
        co = (_cochange_scores(priors, anchors)
              if mode in ("full", "cochange-only", "shuffled") else {})
        churn = priors["churn"] if mode in ("full", "churn-only",
                                            "shuffled") else {}
        fused = {}
        for f in fscore:
            rel = _rel_posix(f, rk)
            fused[f] = (_ALPHA_TEXT * text_n[f]
                        + _BETA_CHURN * churn.get(rel, 0.0)
                        + _GAMMA_COCHANGE * co.get(rel, 0.0))
        fscore = {f: v * tmax for f, v in fused.items()}
    ranked_files = sorted(fscore, key=lambda f: fscore[f], reverse=True)
    dominant = (len(ranked_files) == 1 or
                (len(ranked_files) > 1 and
                 fscore[ranked_files[0]] >= 1.8 * fscore[ranked_files[1]]))
    picked: List[Chunk] = []
    used = 0
    if dominant:
        f0 = ranked_files[0]
        f0_chunks = sorted((c for c in lex + sem if c.file == f0),
                           key=lambda c: c.score, reverse=True)
        f0_budget = int(budget * 0.55)
        for c in f0_chunks:
            if _overlaps(c, picked):
                continue
            if used + c.tokens <= f0_budget:
                picked.append(c)
                used += c.tokens
    lex_rest = [c for c in lex if c not in picked]
    part = _apply_budget(lex_rest, max(1, int((budget - used) * 0.65)),
                         objective)
    for c in part:
        if not _overlaps(c, picked) and used + c.tokens <= budget:
            picked.append(c)
            used += c.tokens
    for c in sorted(sem, key=lambda c: c.score, reverse=True):
        if c in picked or _overlaps(c, picked):
            continue
        if used + c.tokens <= budget:
            picked.append(c)
            used += c.tokens
    picked.sort(key=lambda c: c.score, reverse=True)
    return picked


def _is_test_path(file: str) -> bool:
    low = file.replace("\\", "/").lower()
    return "test" in Path(low).name or "/tests/" in low or low.startswith("tests/")


def _apply_budget(chunks: List[Chunk], budget: int,
                  objective: str = "auto") -> List[Chunk]:
    if budget <= 0 or not chunks:
        return chunks

    # --- Retrieval objectives: reserve budget slots for span *kinds* -------
    # A lookup rarely needs one span: understanding a symbol usually takes its
    # definition PLUS how it's called elsewhere PLUS how it's tested. Greedy
    # score-order packing floods the budget with same-file chunks. So before
    # greedy filling, guarantee (when they exist among candidates):
    #   definition    — best chunk ranked as a definition        (all modes)
    #   cross-file    — best chunk from a different, non-test file ("auto",
    #                   "def+caller")
    #   test          — best chunk from a test file               ("auto",
    #                   "def+test")
    # "single" restores pure score-order packing (v0.1 behaviour).
    guaranteed: List[Chunk] = []
    if objective != "single":
        # prefer a NON-test definition: "def test_foo" also matches a query
        # for foo and must not claim the definition slot when the real
        # definition exists
        best_def = next((c for c in chunks if "definition" in c.rank_reason
                         and not _is_test_path(c.file)), None)
        if best_def is None:
            best_def = next((c for c in chunks
                             if "definition" in c.rank_reason), None)
        if best_def is not None:
            guaranteed.append(best_def)
        anchor_file = guaranteed[0].file if guaranteed else chunks[0].file
        if objective in ("auto", "def+caller"):
            caller = next(
                (c for c in chunks
                 if c.file != anchor_file and not _is_test_path(c.file)
                 and c not in guaranteed),
                None,
            )
            if caller is not None:
                guaranteed.append(caller)
        if objective in ("auto", "def+test"):
            test = next(
                (c for c in chunks if _is_test_path(c.file)
                 and c not in guaranteed),
                None,
            )
            if test is not None:
                guaranteed.append(test)

    fitted: List[Chunk] = []
    used = 0
    for c in guaranteed:
        if used + c.tokens <= budget:
            fitted.append(c)
            used += c.tokens

    # --- Diversity-aware greedy fill ---------------------------------------
    # Prefer the highest score, but discount chunks from files already
    # represented so one hot file cannot consume the whole budget.
    remaining = [c for c in chunks if c not in fitted]
    while remaining:
        file_counts: Dict[str, int] = {}
        for c in fitted:
            file_counts[c.file] = file_counts.get(c.file, 0) + 1
        best, best_eff = None, None
        for c in remaining:
            if used + c.tokens > budget:
                continue
            eff = c.score - 6 * file_counts.get(c.file, 0)
            if best_eff is None or eff > best_eff:
                best, best_eff = c, eff
        if best is None:
            break
        fitted.append(best)
        used += best.tokens
        remaining.remove(best)

    # Render in rank order regardless of packing order.
    fitted.sort(key=lambda c: c.score, reverse=True)
    if not fitted:
        # The best chunk alone exceeds the budget. Zero chunks is the worst
        # possible answer — truncate the best one to fit instead.
        top = chunks[0]
        keep = max(200, budget * 4)
        top.code = top.code[:keep] + "\n... (truncated to budget)"
        top.tokens = estimate_tokens(top.code)
        fitted = [top]
    return fitted


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_file(
    filepath: str,
    patterns: List[str],
    before: int,
    after: int,
    boundary: str,
    dedupe_threshold: float,
    text: Optional[str] = None,
) -> Tuple[List[Chunk], Optional[_NegativeEvidence], str]:
    path = Path(filepath)
    if text is None:
        if not path.is_file():
            return [], None, ""
        text = _read_text(path)
    lines = text.splitlines(keepends=True)

    compiled = [_compile_or_escape(p) for p in patterns]
    combined = _compile_multi_pattern(patterns)

    matches_by_line: Dict[int, List[str]] = {}
    for i, line in enumerate(lines):
        if len(line) > _MAX_LINE_CHARS or not combined.search(line):
            continue
        for pat in compiled:
            if pat.search(line):
                matches_by_line.setdefault(i, []).append(pat.pattern)

    neg = _NegativeEvidence(patterns, filepath)
    if not matches_by_line:
        return [], neg, text

    lang = _detect_lang(filepath)
    chunks: List[Chunk] = []
    # Merge match lines whose windows would overlap into ONE chunk up front.
    # Overlapping windows are near-duplicates by construction; building them
    # all and letting the O(n^2) Jaccard dedupe collapse them was the single
    # biggest cost on match-heavy queries.
    targets = sorted(matches_by_line)
    groups: List[List[int]] = []
    for t in targets:
        if groups and t - groups[-1][-1] <= before + after:
            groups[-1].append(t)
        else:
            groups.append([t])
    for group in groups:
        if boundary == "fn":
            start, end, symbol = _find_enclosing_boundary(lines, group[0], lang)
            for t in group[1:]:
                s2, e2, _ = _find_enclosing_boundary(lines, t, lang)
                start, end = min(start, s2), max(end, e2)
        else:  # "auto" / "none" both use a fixed window
            start = max(0, group[0] - before)
            end = min(len(lines), group[-1] + after + 1)
            symbol = None
        pats: List[str] = []
        for t in group:
            for p in matches_by_line[t]:
                if p not in pats:
                    pats.append(p)
        chunks.append(Chunk(
            file=filepath,
            line_start=start + 1,
            line_end=end,
            code="".join(lines[start:end]),
            patterns=pats,
            matches=len(pats),
            symbol=symbol or "",
        ))

    scorer = _Scorer(patterns)
    chunks = [scorer.score(c) for c in chunks]
    chunks.sort(key=lambda c: c.score, reverse=True)

    chunks, _ = _Deduplicator(dedupe_threshold).dedupe(chunks)
    return chunks, neg, text


def _iter_files(root: Path):
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if f.suffix.lower() not in SEARCHABLE_EXTS:
            continue
        try:
            if f.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield f


def focused_read(
    path: str,
    pattern: str,
    *,
    before: int = 40,
    after: int = 40,
    budget: int = 0,
    boundary: str = "auto",
    recursive: bool = False,
    dedupe: bool = True,
    objective: str = "auto",
    semantic: bool = True,
) -> Result:
    """Grep ``path`` for ``pattern`` and return ranked, budget-capped slices.

    Parameters
    ----------
    path:
        A file or a directory. A directory (or ``recursive=True``) triggers a
        recursive walk of source files, skipping vendored/build dirs and files
        over 1 MB.
    pattern:
        A regex, case-insensitive. Join alternatives with ``|``; a chunk that
        matches *more* of them ranks higher (co-occurrence scoring). Grouped
        alternations like ``(a|b)_c`` are treated as one pattern.
    before, after:
        Context lines each side of a match (ignored when ``boundary="fn"``).
    budget:
        Keep only the highest-ranked chunks fitting ~this many tokens. ``0``
        means no cap. This is the token lever.
    boundary:
        ``"auto"`` (fixed window) or ``"fn"`` (snap each chunk to its whole
        enclosing function/class).
    recursive:
        Force a directory walk even when ``path`` is a single file's directory.
    dedupe:
        Collapse near-duplicate chunks (exact duplicates always collapse).
    objective:
        What the budget must cover. ``"auto"`` (default) guarantees, when
        candidates exist: the best definition chunk, the best cross-file
        usage chunk, and the best test-file chunk — then fills the rest by
        score with a same-file diversity discount. ``"def+caller"`` and
        ``"def+test"`` guarantee only that pair; ``"single"`` restores pure
        score-order packing.

    Returns
    -------
    Result
        Ranked :class:`Chunk` list plus negative evidence. Call
        :meth:`Result.render` for the text report or :meth:`Result.to_dict`
        for structured data.
    """
    patterns = _split_pattern_top_level(pattern)
    if not patterns:
        raise ValueError("pattern is empty after parsing")
    _reject_redos(patterns)         # fail fast rather than hang; see above
    patterns = _expand_nl_query(pattern, patterns)

    target = Path(path)
    threshold = 2.0 if not dedupe else 0.7

    if recursive or target.is_dir():
        root = target if target.is_dir() else target.parent
        all_chunks: List[Chunk] = []
        entry = _corpus(root)
        searched = 0
        matched = 0
        for fpath, text, _lines in entry["files"]:
            searched += 1
            try:
                chunks, _neg, _text = _extract_file(
                    fpath, patterns, before, after, boundary, threshold,
                    text=text,
                )
            except Exception:
                continue
            if chunks:
                matched += 1
                all_chunks.extend(chunks)
        # rerank only the plausible head — reranking thousands of low-scored
        # usage chunks costs latency and cannot change what gets packed
        all_chunks.sort(key=lambda c: c.score, reverse=True)
        head = all_chunks[:250]
        _semantic_rerank(head, patterns, query_text=pattern)
        head.sort(key=lambda c: c.score, reverse=True)
        all_chunks = head + all_chunks[250:]
        if semantic and budget > 0:
            import os as _os
            def _plain_word_q(p):
                return bool(re.fullmatch(r"[a-z][a-z0-9]{3,}", p))
            _router_mode = _os.environ.get("SLICEGREP_ROUTER", "on")
            if _router_mode == "learned":
                # logistic router trained on 519 burned benchmark outcomes
                # (train acc 92.3%; see benchmarks/train_router.py)
                _W = [-2.0264, 1.4161, 0.3591, -1.2606, -1.2075, -0.0575,
                      -0.8742]
                _n = len(patterns)
                _fx = [1.0, float(_n),
                       (sum(1 for p in patterns if _plain_word_q(p)) / _n)
                       if _n else 0.0,
                       1.0 if any("_" in p for p in patterns) else 0.0,
                       1.0 if any(re.search(r"[A-Z]", p) for p in patterns)
                       else 0.0,
                       1.0 if any("\\" in p for p in patterns) else 0.0,
                       (sum(len(p) for p in patterns) / (4.0 * _n))
                       if _n else 0.0]
                vague_q = sum(w * x for w, x in zip(_W, _fx)) > 0
            else:
                vague_q = (len(patterns) >= 3
                           and all(_plain_word_q(p) for p in patterns)
                           and _router_mode != "off")
            # dense participates ONLY on the vague route: on precise queries
            # it measurably dilutes packing in every configuration tested
            # (v2 confirmation ablation: router-off 62.6 < router-on 63.4,
            # both < dense-free 69.6-era engine).
            sem = _semantic_candidates_bm25(entry, patterns,
                                            query_text=pattern,
                                            use_dense=vague_q)
            priors = _history_priors(root)
            if priors is not None and _history_mode() == "full":
                # history-conditioned query expansion: anchor symbols from
                # the top lexical evidence -> historically coupled symbols
                # become NEW retrieval objectives
                anchor_syms: List[str] = []
                for c in all_chunks[:5]:
                    for line in c.code.splitlines():
                        if _DEF_LINE_RE.match(line):
                            sym = _extract_symbol(line)
                            if sym and len(sym) >= 4:
                                anchor_syms.append(sym)
                anchor_list = list(dict.fromkeys(anchor_syms))
                # REGION-LEVEL history scoring: candidate chunks whose
                # enclosing definition is historically coupled to the query's
                # anchor symbols get a lift-scaled boost. This is the
                # symbol-granularity version of the co-change prior — the
                # level at which changes actually cluster.
                sym_lift = dict(_symbol_expansions(priors, anchor_list, k=16))
                if sym_lift:
                    for c in all_chunks + sem:
                        first = c.code.split("\n", 1)[0] if c.code else ""
                        if not _DEF_LINE_RE.match(first):
                            continue
                        csym = _extract_symbol(first)
                        w = sym_lift.get(csym or "")
                        if w:
                            c.score += int(round(8 * w))
                            if "region-history" not in c.rank_reason:
                                c.rank_reason.append("region-history")
                exps = [(sy, w) for sy, w in
                        sorted(sym_lift.items(), key=lambda kv: kv[1],
                               reverse=True)[:4]]
                if exps:
                    have = {(c.file, c.line_start) for c in all_chunks + sem}
                    for sym, w in exps:
                        rx = re.compile(
                            r"^\s*(?:async\s+)?(?:def|class|fn|func|function)\s+"
                            + re.escape(sym) + r"")
                        for fpath, _text, lines in entry["files"]:
                            for i, line in enumerate(lines):
                                if rx.match(line):
                                    lo = i
                                    hi = min(len(lines), i + 30)
                                    if (fpath, lo + 1) in have:
                                        break
                                    ch = Chunk(
                                        file=fpath, line_start=lo + 1,
                                        line_end=hi,
                                        code="".join(lines[lo:hi]),
                                        patterns=[], matches=0, symbol=sym,
                                    )
                                    ch.score = 12 + int(14 * w)
                                    ch.rank_reason = ["history-expansion"]
                                    sem.append(ch)
                                    break
                            else:
                                continue
                            break
            # Query router: the two regimes have different winners and the
            # dev-set overlap analysis showed fusion cannot serve both.
            #   precise query (top lexical chunk is a strong definition hit)
            #     -> lexical-first packing with objective guarantees
            #   vague query (no strong lexical anchor)
            #     -> semantic list (dense+BM25-ranked blocks) gets the FULL
            #        budget; lexical only back-fills leftover space
            vague = vague_q
            if not vague:
                all_chunks = _pack_hybrid(all_chunks, sem, budget, objective,
                                          priors=priors)
            else:
                # vague mode: objective guarantees FIRST (definition/caller/
                # test slots are regime-independent), then pure-dense fill.
                picked = _apply_budget(all_chunks,
                                       max(1, int(budget * 0.25)), objective)
                used = sum(c.tokens for c in picked)
                # fused ordering, not pure dense: word-soup queries whose
                # words DO appear in the corpus (docstring-style) are won by
                # BM25; those whose words do NOT are won by dense. The fused
                # score covers both; pure dense measurably loses the first
                # kind (v2 comprehend family).
                for c in sorted(sem, key=lambda c: c.score, reverse=True):
                    if _overlaps(c, picked):
                        continue
                    if used + c.tokens <= budget:
                        picked.append(c)
                        used += c.tokens
                picked.sort(key=lambda c: c.score, reverse=True)
                all_chunks = picked
        else:
            all_chunks = _apply_budget(all_chunks, budget, objective)
        return Result(
            query=patterns,
            chunks=all_chunks,
            negative_evidence=[],
            files_searched=max(searched, 1),
            files_matched=matched,
            budget=budget,
        )

    chunks, neg, text = _extract_file(
        str(target), patterns, before, after, boundary, threshold
    )
    pre_budget = len(chunks)
    _semantic_rerank(chunks, patterns, query_text=pattern)
    chunks.sort(key=lambda c: c.score, reverse=True)
    chunks = _apply_budget(chunks, budget, objective)

    absences: List[str] = []
    if neg is not None:
        for c in chunks:
            if c.symbol:
                neg.check_definition(c.symbol, chunks)
        for pat in patterns:
            neg.check_pattern(pat, chunks, full_text=text)
        absences = neg.absences

    return Result(
        query=patterns,
        chunks=chunks,
        negative_evidence=absences,
        files_searched=1,
        files_matched=1 if chunks else 0,
        budget=budget,
        deduped=max(0, pre_budget - len(chunks)) if budget else 0,
    )
