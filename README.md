# slicegrep

**grep that returns ranked, token-budgeted code slices — built for LLMs and coding agents.**

Plain `grep` gives you matching lines with no context. "Read the whole file" gives
you context but burns thousands of tokens on code the model doesn't need. `slicegrep`
sits in between: it greps a file or directory, extracts only the **relevant slices**,
**ranks** them, **dedupes** near-duplicates, caps the total to a **token budget**, and
tells you what it did **not** find.

That's the grep-then-read loop an LLM agent runs dozens of times per task — collapsed
into one call that returns a fraction of the tokens.

```bash
pip install git+https://github.com/haxo98098/slicegerp
```

## Make it automatic (Claude Code plugin)

Offering an agent a better retrieval tool is not enough. It has to *choose*
the tool, and agents reach for the plain file read out of habit. The plugin
removes the choice: a `PreToolUse` hook sits in front of `Read`, and when a
read is large enough to be worth it, the whole file is replaced with a file
map plus the slices matching what the session is actually working on.

```
/plugin marketplace add haxo98098/slicegerp
/plugin install slicegrep@slicegrep
```

No pip install needed; the plugin runs the bundled source directly. Measured
on this repo's own `core.py`: **17,849 tokens of whole file became 2,195
tokens of map plus slices, an 88% cut on a single read.**

Three rules keep it safe to leave on:

1. **Fail open.** Any error, unreadable path, or empty result lets the normal
   read happen. A retrieval optimizer must never be able to break a session.
2. **Never trap the model.** The second read of the same path in a session
   passes through untouched, and the injected text says so. If the slices
   were not enough, asking again returns the whole file.
3. **Only when it pays.** Small files, ranged reads (`offset`/`limit`), and
   non-code files pass through. The median real-world read is ~600 tokens,
   where slicing saves nothing. The cost lives in the tail.

Tune with `SLICEGREP_HOOK_MIN_TOKENS` (default 2000), `SLICEGREP_HOOK_BUDGET`
(1200), or `SLICEGREP_HOOK_DISABLE=1`. If `python` is not on your PATH as
`python`, edit the command in `hooks/hooks.json`.

- **Zero dependencies** for the core (standard library only). Python 3.8+ (the
  optional MCP server needs 3.10+).
- **CLI + library + MCP server.** Use it from a shell, import it, or plug it into
  Claude Desktop / Claude Code / Cursor / Windsurf over the Model Context Protocol.
- **Regex or natural language.** `"def score|budget"` works; so does
  `"how does budget packing guarantee definitions"` — phrases with 3+ content
  words expand automatically (subword + stemmed matching closes the
  vocabulary gap on vague queries).

---

## Why

An LLM reading code doesn't want the file — it wants the *five slices that matter*,
ordered by relevance, small enough to fit its context. `slicegrep` is that primitive:

| | plain `grep` | read whole files | **slicegrep** |
|---|---|---|---|
| Context around matches | ✗ (lines only) | ✓ (all of it) | ✓ (just enough) |
| Ranked by relevance | ✗ | ✗ | ✓ |
| Near-duplicates collapsed | ✗ | ✗ | ✓ |
| Fits a token budget | ✗ | ✗ | ✓ |
| Tells you what's absent | ✗ | ✗ | ✓ (negative evidence) |

### The point, in tokens

Reading one 660-line source file to answer "how does scoring and dedup work?":

```
whole file  : ~6600 tokens
slicegrep   :  ~375 tokens   →  94% fewer tokens, only the slices that matter
```

```bash
slicegrep src/core.py "class Scorer|def score|dedupe|rare" --budget 600
```

Multiply that by every file an agent reads per task.

## Benchmarks

Evaluated under a three-tier protocol: tuning and validation seeds are burned
during development; published numbers come from CONFIRMATION runs on virgin
data (every previously-touched session excluded) against the frozen engine,
run once. Two router defects were caught by confirmation runs and fixed; the
seeds they consumed are documented in the CHANGELOG.

### Real-change retrieval (v3, primary) — confirmation, n=286 virgin sessions

Real commits mined from click/flask/requests/rich history; repo reconstructed
at the parent commit (git worktree, ancestor-only history — no future
leakage); query from the commit message only; ground truth = the regions the
real fix touched. Session hit = ≥50% of those regions retrieved under an
8k-token cap.

| strategy | hit rate | 95% CI | mean coverage |
|---|---|---|---|
| dense embeddings (potion-code) | 28.3% | [23.1, 33.5] | 25.0% |
| **slicegrep 0.5** | **26.6%** | [21.5, 31.7] | 24.2% |
| tf-idf windows | 23.4% | [18.5, 28.3] | 21.6% |
| grep + file ranking | 23.4% | [18.5, 28.3] | 21.6% |
| ast-chunk tf-idf | 22.0% | [17.2, 26.8] | 20.5% |
| bm25 windows | 21.7% | [16.9, 26.5] | 20.2% |

Statistical tie for first with the dense-only retriever; both clear of the
rest. slicegrep is the only method in the top cluster that also returns
line-attributed slices, negative evidence, and objective-guaranteed context
(definition + caller + test), and the only one that wins the suite below.

### Controlled retrieval suite (v2) — confirmation, fresh seed, 240 tasks

Six task families (symbol, docstring-concept, cross-file call-chain, bug
localization from error strings, config/data-flow, test+implementation),
twelve strategies, 8k cap.

| strategy | tokens → model | hit rate | tool calls |
|---|---|---|---|
| **slicegrep 0.5** | 2,304 | **71.4%** | 1 |
| bm25 windows | 2,213 | 66.1% | 1 |
| ast-chunk tf-idf | 2,296 | 58.6% | 1 |
| grep + window reads | 5,693 | 60.4% | 7 |
| dense embeddings | 2,262 | 35.2% | 1 |
| semble (embeddings+BM25) | 2,094 | 44.5% | 1 |

First by 5.3 points at ~2.3k tokens and one call. Warm latency ~35-60ms
(in-process corpus cache).

### How: a query-shape router

Precise queries (identifiers, error strings, 1-2 terms) run the lexical
pipeline — BM25-scored definition-aligned blocks, objective guarantees,
diversity packing; dense is fully gated out (it measurably dilutes precise
packing). Vague queries (3+ plain words) keep the guarantees, then fill the
budget by fused dense+BM25 ranking. Optional extras: `model2vec` for the
dense stage; git history priors (temporally safe, ablation-switchable) —
both off gracefully when unavailable, keeping the stdlib-only core.

### Other suites (earlier engines; see RESULTS files)

- **Cross-language (v5):** zod (TS) 77.5% vs next-best 60.0; serde (Rust)
  67.5% vs 50.0; django at ~2,800 files: 60.0% holding first.
- **Multi-turn (v4):** with one mechanical refinement round for every
  strategy, slicegrep led on coverage (27.3%) and tied the best hit rate.
- **End-to-end (v6, real Claude calls):** best mean file recall (66.7%)
  among the three strategies tested; answer-correct within noise of the
  leader at n=15.
- **Historical (v1):** definition lookups, 84.7% vs 76.0 (grep+windows);
  this suite caught the v0.1 ranking bug (71.7% before the fix).

---

## Quick start

```bash
# find a function
slicegrep src/app.py "def handle_request"

# whole enclosing blocks, searched recursively, under a token budget
slicegrep src/ "Scorer|def score" --boundary fn --budget 800

# co-occurring concepts — a chunk matching more of them ranks higher
slicegrep . "retry|timeout|backoff" --budget 1500

# raw JSON for tooling
slicegrep src/ "TODO" 2 2 --json
```

`fr` is installed as a shorter alias for `slicegrep` (focused read).

### As a library

```python
from slicegrep import focused_read

result = focused_read("src/", "class Scorer|def score", budget=800, boundary="fn")

print(result.render())          # ranked text report (what an LLM reads)
print(result.total_tokens)      # e.g. 612
for chunk in result.chunks:
    print(chunk.file, chunk.line_start, chunk.score, chunk.rank_reason)

data = result.to_dict()         # structured output for your own pipeline
```

---

## MCP server

Expose `focused_read` to any MCP client so the model can pull ranked code context
on its own:

```bash
pip install "slicegrep[mcp] @ git+https://github.com/haxo98098/slicegerp"
```

**Claude Desktop / Claude Code** — add to your MCP config:

```json
{
  "mcpServers": {
    "slicegrep": {
      "command": "slicegrep-mcp"
    }
  }
}
```

Or, with Claude Code's CLI:

```bash
claude mcp add slicegrep -- slicegrep-mcp
```

The model then calls a `focused_read` tool with `path`, `pattern`, and an optional
`budget` / `boundary`, and gets back the same ranked, budget-capped report — instead
of reading whole files into its context window.

---

## How the ranking works

Every candidate slice is scored, then the list is sorted, deduped, and trimmed to the
budget. Signals that **raise** a chunk's score:

- **co_occurrence / all_patterns** — the slice matches several of your `|` patterns.
- **rare_terms** — it contains distinctive identifiers, not just boilerplate.
- **definition** — the match is where a symbol is *defined*, not just used.
- **multi_match** — several hits in the same slice.

Signals that **lower** it: `declaration_only`, `test_demoted` (unless you searched for
tests), `vendor_demoted` (generated/vendored paths), `mostly_comments`.

### Negative evidence

An empty result is a real answer. `slicegrep` reports it explicitly, and distinguishes
"the pattern isn't in the file" from "it's there but fell outside the budgeted chunks":

```
NEGATIVE EVIDENCE:
  - No definition found for 'Scorer' in src/
  - Pattern 'deprecated_api' not found in src/
```

---

## CLI reference

```
slicegrep <path> <pattern> [before] [after] [options]

  <path>       file OR directory (a directory implies a recursive walk)
  <pattern>    case-insensitive regex; join alternatives with '|'
  before after context lines each side of a match (default 40 40)

options:
  --budget N        keep only the highest-ranked chunks fitting ~N tokens
  --boundary MODE   auto (fixed window) | fn (snap to enclosing function/class) | none
  --recursive, -r   force a directory walk even for a file path
  --no-dedupe       keep near-duplicate chunks (exact dups still collapse)
  --json            print raw JSON instead of the rendered report
  --version
```

Exit code is `0` when at least one chunk matched, `1` when nothing did — so shell
scripts and CI can branch on it.

---

## Development

```bash
git clone https://github.com/haxo98098/slicegerp
cd slicegrep
pip install -e ".[dev,mcp]"
pytest
```

## License

[MIT](LICENSE)
