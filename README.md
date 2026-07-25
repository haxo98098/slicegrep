# slicegrep

grep that gives back ranked, token-budgeted code slices instead of whole files.

![slicegrep demo](docs/demo-short.gif)

[Longer demo](docs/demo.gif) covering directory ranking, plain-English
queries, negative evidence, and the hook.

## The problem I kept hitting

Watch a coding agent work and you see the same loop over and over. It greps,
gets a list of line numbers with no context, then reads three whole files to
understand them, then does it again for the next question. Most of what lands
in the context window is code nobody asked about, and once it is in there it
stays, pushing out the things that actually mattered.

Plain grep gives you matches with no context. Reading the file gives you
context and a few thousand tokens of noise with it. I wanted the middle: the
handful of slices that answer the question, ordered by relevance, capped at a
size I choose.

That is what this does. It greps a file or a directory, pulls out the relevant
slices, ranks them, collapses near-duplicates, trims the result to a token
budget, and tells you what it could not find.

```bash
pip install git+https://github.com/haxo98098/slicegrep
```

Standard library only for the core. Python 3.8 and up.

## Make it automatic

Giving an agent a better tool is not enough. It has to remember to pick it,
and agents reach for the plain file read out of habit. So there is a plugin
that takes the choice away. A hook sits in front of the Read tool, and when a
read is big enough to be worth it, the whole file gets swapped for a map of
the file plus the slices that match whatever the session is working on.

```
/plugin marketplace add haxo98098/slicegrep
/plugin install slicegrep@slicegrep
```

Nothing to pip install, the plugin runs the bundled source. On this repo's own
`core.py` that turns a 17,849 token read into 2,195 tokens of map plus slices.

I run this on my own machine, so it is built to be safe to leave switched on:

1. **It fails open.** Any error, any unreadable path, any empty result, and
   the normal read just happens. A retrieval tool should never be able to
   break your session.
2. **It never traps the model.** Read the same file twice and the second one
   passes straight through, and the injected text says so. If the slices were
   not enough, asking again gets you the whole thing.
3. **It only fires when it pays.** Small files, ranged reads, and non-code
   files pass through untouched. I measured the median real-world read at
   about 600 tokens, where slicing saves nothing worth having. The money is
   in the tail.

It also adjusts itself. A fixed budget is a guess about how dense the file
is, and when the guess is wrong the first pass returns a sliver. So the hook
checks its own coverage: if it returned less than 55% of what matched, it
raises the budget and retries, up to a ceiling. A hook that grew without
bound would just be a whole-file read with extra steps, so the ceiling is the
point.

Whatever is still missing gets named with the exact arguments to fetch it:

```
STILL NOT SHOWN (38% of matched material above).
To see any of these, Read core.py with offset/limit:
  offset=394  limit=104   (~1028 tok, score=32)
  offset=524  limit=219   (~2185 tok, score=31)
```

So the next step is a precise ranged read, not a full re-read of the file.

Tune it with `SLICEGREP_HOOK_MIN_TOKENS` (default 2000),
`SLICEGREP_HOOK_BUDGET` (1200), `SLICEGREP_HOOK_MIN_COVERAGE` (0.55),
`SLICEGREP_HOOK_MAX_BUDGET` (4000), `SLICEGREP_HOOK_TIMEOUT` (5s), or turn it
off with `SLICEGREP_HOOK_DISABLE=1`. If your Python is not on PATH as
`python`, edit the command in `hooks/hooks.json`.

## Using it directly

```bash
# find a function
slicegrep src/app.py "def handle_request"

# whole enclosing blocks, searched recursively, under a token budget
slicegrep src/ "Scorer|def score" --boundary fn --budget 800

# co-occurring concepts, a chunk matching more of them ranks higher
slicegrep . "retry|timeout|backoff" --budget 1500

# raw JSON for tooling
slicegrep src/ "TODO" 2 2 --json
```

`fr` is installed as a shorter alias, for focused read.

Natural language works too. `"def score|budget"` is fine, and so is
`"how does budget packing guarantee definitions"`. Anything with three or
more content words gets expanded automatically with stemming and subword
matching, which is what closes the vocabulary gap on vague questions.

As a library:

```python
from slicegrep import focused_read

result = focused_read("src/", "class Scorer|def score", budget=800, boundary="fn")

print(result.render())          # the ranked text report an LLM reads
print(result.total_tokens)      # e.g. 612
for chunk in result.chunks:
    print(chunk.file, chunk.line_start, chunk.score, chunk.rank_reason)

data = result.to_dict()         # structured output for your own pipeline
```

## One task, both ways

Aggregate hit rates hide what the difference feels like. Here is a single
realistic bug investigation run both ways, scored on the three things you
actually need to fix a bug: the definition, a caller in another file, and the
test. `benchmarks/compare_one.py` runs this, so you can check it.

**"echo() mangles unicode on Windows. Where is it defined, who calls it, and
what covers it?"** (click)

| | tool calls | tokens | definition | caller | test |
|---|---|---|---|---|---|
| grep → read → read → grep | 4 | 14,233 | yes | yes | **no** |
| slicegrep | **1** | **2,614** | yes | yes | **yes** |

Same question against two other repos:

| task | baseline | slicegrep | saved |
|---|---|---|---|
| `url_for` (flask) | 4 calls, 22,815 tok, all three | 1 call, 2,763 tok, all three | 88% |
| `Session.request` (requests) | 4 calls, 8,826 tok, **no test** | 1 call, 2,710 tok, all three | 69% |

Two things worth noticing, including the one that is not flattering:

- **The baseline usually finds the definition and a caller, and misses the
  test.** That is not bad luck. Reading the two most promising files gets you
  the implementation; nothing in that loop goes looking for coverage. The
  budget packer reserves a slot for a test chunk, which is why it lands one
  in a single call.
- **The token gap is mostly about whole files.** The baseline pays full price
  for two files to use a few regions of each. That is the entire thesis, and
  it is why the win shrinks on small files and grows on big ones.

The honest caveat: slicegrep is showing 4% to 36% of matched material in
these runs, and says so. It gets the three things that matter because of the
objective guarantees, not because it saw everything.

## Does it actually work

I got tired of retrieval projects that report numbers from the same data they
were tuned on, so this one holds data out. Tuning and validation seeds get
burned during development, and published numbers come from confirmation runs
on virgin data with every previously touched session excluded, against a
frozen engine, run once. Two router bugs were caught this way, and the seeds
they consumed are written down in the CHANGELOG rather than quietly reused.

**Real-change retrieval, 286 virgin sessions.** Real commits mined from
click, flask, requests and rich. The repo is reconstructed at the parent
commit so no future information leaks in, the query is the commit message
alone, and a hit means retrieving at least half the regions the real fix
touched under an 8k cap.

| strategy | hit rate | 95% CI | mean coverage |
|---|---|---|---|
| dense embeddings (potion-code) | 28.3% | [23.1, 33.5] | 25.0% |
| **slicegrep 0.5** | **26.6%** | [21.5, 31.7] | 24.2% |
| tf-idf windows | 23.4% | [18.5, 28.3] | 21.6% |
| grep + file ranking | 23.4% | [18.5, 28.3] | 21.6% |
| ast-chunk tf-idf | 22.0% | [17.2, 26.8] | 20.5% |
| bm25 windows | 21.7% | [16.9, 26.5] | 20.2% |

That is a statistical tie for first, not a win, and I would rather say so.
Both are clear of the rest. slicegrep is the only one in the top group that
also returns line-attributed slices, negative evidence, and guaranteed
coverage of definition plus caller plus test, and it is the one that wins the
suite below.

**Controlled retrieval suite, fresh seed, 240 tasks.** Six task families
(symbol lookup, docstring concepts, cross-file call chains, bug localization
from error strings, config data flow, test plus implementation) against
twelve strategies.

| strategy | tokens to model | hit rate | tool calls |
|---|---|---|---|
| **slicegrep 0.5** | 2,304 | **71.4%** | 1 |
| bm25 windows | 2,213 | 66.1% | 1 |
| ast-chunk tf-idf | 2,296 | 58.6% | 1 |
| grep + window reads | 5,693 | 60.4% | 7 |
| semble (embeddings+BM25) | 2,094 | 44.5% | 1 |
| dense embeddings | 2,262 | 35.2% | 1 |

First by 5.3 points, at about 2.3k tokens and a single call.

Other suites, run against earlier engines, are in the RESULTS files:
cross-language (zod 77.5% against a next-best 60.0, serde 67.5% against 50.0,
django at ~2,800 files holding first at 60.0), multi-turn, and an end-to-end
run with real model calls where it had the best mean file recall.

## How the ranking works

Precise queries (identifiers, error strings, one or two terms) run the lexical
pipeline: BM25 over definition-aligned blocks, guaranteed objectives, and
diversity packing so one file cannot hog the budget. Dense retrieval is gated
out of that path entirely, because measuring it showed it dilutes precise
packing. Vague queries keep the guarantees and then fill the remaining budget
with a fused dense and BM25 ranking.

Things that raise a slice's score: it matches several of your patterns, it
holds distinctive identifiers rather than boilerplate, it is where a symbol is
defined rather than merely used, it has several hits in one place. Things that
lower it: declaration-only matches, test files (unless you searched for
tests), vendored or generated paths, and slices that are mostly comments.

The optional extras (model2vec for dense, git history priors) degrade quietly
when they are not available, which is what keeps the core dependency free.

### How do you know it did not cut something you needed

This is the obvious objection to any tool that hands back 350 tokens where
there were 18,000, and "trust the ranking" is not an answer. So nothing is
allowed to disappear quietly. Every region that matched your query is either
returned, or named in the report with its location, size, score and the
reason it lost:

```
OMITTED — 12 matching region(s), ~8610 tokens not returned (6% of matched material shown):
  core.py:102-192  ~925 tok  score=47.0  (multi_match(3), co_occurrence, all_patterns)
  cli.py:1-60      ~642 tok  score=41.0  (multi_match(3), co_occurrence)
  hook.py:219-235  ~127 tok  score=13    (semantic-recall)
  ... and 4 more
  -> raise --budget, or read these ranges directly.
```

Three things follow from that:

- **Coverage is stated, not implied.** "6% of matched material shown" is the
  honest reading of a 700-token budget against this query. If that number is
  too low for what you are doing, raise the budget. The tool will not pretend
  the other 94% did not exist.
- **You can always go get it.** Omissions carry exact line ranges, so the
  next step is a normal read of those lines, not a fishing trip.
- **Truncation counts as loss too.** When a single region is bigger than the
  whole budget it gets cut to fit, and in that case the header used to claim
  the full line range while showing part of it. It now reports the range it
  actually returned and lists the remainder as omitted. That was a real bug,
  found by writing the test for this section.

`result.coverage`, `result.omitted` and `result.omitted_tokens` are on the
Python object and in `--json`, so a harness can act on them: raise the budget
and retry, or fetch the omitted ranges, instead of guessing.

### An empty result is a real answer

Most search tools return nothing and leave you guessing whether the thing does
not exist or you just missed it. This one says which:

```
NEGATIVE EVIDENCE:
  - No definition found for 'Scorer' in src/
  - Pattern 'deprecated_api' not found in src/
```

It also distinguishes "not in the file" from "in the file but it fell outside
the budget", which matters when you are deciding whether to raise the budget
or change the query.

## MCP server

If you would rather the model call it as a tool:

```bash
pip install "slicegrep[mcp] @ git+https://github.com/haxo98098/slicegrep"
claude mcp add slicegrep -- slicegrep-mcp
```

Or in an MCP config file:

```json
{
  "mcpServers": {
    "slicegrep": {
      "command": "slicegrep-mcp"
    }
  }
}
```

Works with Claude Desktop, Claude Code, Cursor, Windsurf, or anything else
that speaks MCP. Needs Python 3.10 or newer.

## If a search seems to hang

Both of these were real, both are fixed in 0.5.1, and both are worth knowing
about because they will bite any regex-based retrieval tool.

**Catastrophic backtracking.** A pattern with a nested quantifier like
`(a+)+$` makes Python's regex engine backtrack exponentially. I measured 197
seconds of CPU against one 200-character line, and it was still going when I
killed it, because `re` has no timeout and simply never returns. Patterns are
screened now. A query made only of those raises straight away and tells you
how to rewrite it, and a bad fragment inside a bigger query degrades to a
literal so the rest still works. Lines over 5,000 characters, which in
practice means minified bundles, get skipped instead of matched.

**The dense model reaching the network.** Loading it can fetch from
HuggingFace on a cold cache. A refused connection raises an error, but a
stalled one just sits there, and from the outside that is indistinguishable
from a frozen search. It is bounded now by `SLICEGREP_DENSE_TIMEOUT` (15s) and
falls back to lexical only.

For speed, `SLICEGREP_DENSE=off` takes roughly 2.7s off a cold directory
search on a mid-size repo. A cold run over a 770-file tree is about 7s, while
warm calls are 35 to 60ms thanks to the in-process cache, so a long-running
MCP server pays that cost once instead of every query.

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

Exit code is 0 when something matched and 1 when nothing did, so scripts and
CI can branch on it.

## Development

```bash
git clone https://github.com/haxo98098/slicegrep
cd slicegrep
pip install -e ".[dev,mcp]"
pytest
```

Failed experiments are recorded in the CHANGELOG alongside the ones that
worked. Multiplicative history priors, adaptive budget splits, RRF packing and
a few others all lost to what is here, and knowing what did not work seemed
worth keeping.

## License

[MIT](LICENSE)
