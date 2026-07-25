## [0.5.1] - 2026-07-24

### Fixed — hangs

- **Catastrophic regex backtracking.** A nested-quantifier pattern such as
  `(a+)+$` hung indefinitely: measured at 197 seconds of CPU against one
  200-character line and still running when killed, because Python's `re`
  has no timeout. Patterns are now screened before compilation. A query made
  entirely of such patterns raises a `ValueError` naming the fix; an unsafe
  fragment inside a larger query degrades to a literal so the safe half of
  the query still returns results, matching the existing rule that one bad
  fragment must not kill a query.
- **Pathological line lengths.** Lines over 5,000 characters (minified
  bundles, generated data) are skipped rather than matched, bounding
  per-line regex cost.
- **Dense model load could block on the network.** `from_pretrained` reaches
  HuggingFace on a cold cache; a refused connection raised, but a stalled
  one blocked forever and was indistinguishable from a frozen search. The
  load now runs under `SLICEGREP_DENSE_TIMEOUT` (default 15s) and degrades
  to lexical-only.
- **Hook deadline.** The PreToolUse hook runs retrieval under
  `SLICEGREP_HOOK_TIMEOUT` (default 5s) and falls through to the plain read
  if exceeded, and never triggers a dense-model load. A hook that blocks is
  worse than no hook.
- **BOM-prefixed hook stdin.** Some shells prepend a UTF-8 BOM when piping,
  which made `json.load` fail and the hook silently no-op on every call.

### Added

- `PreToolUse` hook (`slicegrep-hook`) and Claude Code plugin: intercepts
  large whole-file reads and returns a file map plus ranked slices.
  Measured 17,849 -> 2,195 tokens on this repo's own `core.py`.
- 18 regression tests covering hook fail-open behaviour and hang cases.

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Region-level history scoring**: candidate chunks whose enclosing
  definition is lift-coupled (symbol co-change graph) to the query's anchor
  symbols get a scaled boost (`region-history` rank reason). Measured
  neutral at n=300 dev (28.0% vs 28.0% baseline; v2 unchanged); retained
  default-on for the mechanism and ablation infrastructure.
- **Learned query router** (`SLICEGREP_ROUTER=learned`, opt-in): logistic
  regression over query-shape features, trained on 519 burned benchmark
  outcomes (92.3% train accuracy; `benchmarks/train_router.py` retrains on
  any corpus). Dev: matches the hand rule (v2 68.7 = 68.7; v3 27.0 vs
  28.0) â€” the hand rule remains default.

## [0.5.0] - 2026-07-20

### Added
- **BM25 term-saturation scoring** in the semantic recall pass (k1=1.2,
  b=0.75) over **definition-aligned block chunks** (was: raw TF-IDF over
  fixed 60-line windows) with an inverted index for candidate selection.
- **In-process corpus cache** (mtime-validated os.scandir signature): warm
  directory calls dropped from ~880ms to ~35-60ms; match-window merging
  removed the O(n^2) dedupe hot path.
- **Temporally-safe history stack** (novel; ancestor-only git history, no
  future leakage): recency-decayed line-weighted churn
  (w = log(1+lines)/sqrt(files_in_commit)), significance-adjusted (lift)
  file co-change over softmax-weighted textual anchors, a symbol-level
  co-change graph mined from git hunk headers, and history-conditioned
  query expansion (coupled symbols become new retrieval objectives).
  Ablation switches: SLICEGREP_HISTORY=full|off|churn-only|cochange-only|
  shuffled. Measured effect at file granularity: neutral (kept for the
  mechanism + ablation infrastructure; region-level is the roadmap).
- **Optional dense stage** (`model2vec` potion-code-16M, extra
  `slicegrep[fast]`-style guarded import): dense cosine fused into recall
  scoring. Stdlib-only behaviour unchanged when absent.
- **Query-shape router**: 3+ plain lowercase words = vague -> objective
  guarantees first, then fused dense+BM25 fill; anything with identifiers,
  case, escapes, or 1-2 terms = precise -> lexical pipeline, dense fully
  gated out (it measurably dilutes precise packing).

### Evaluation protocol (three tiers, enforced after two caught mistakes)
- Tuning -> validation (seed 777) -> confirmation on virgin sessions with
  every previously-touched commit excluded (809 burned SHAs at final run).
  Confirmation ran ONCE against the frozen engine.
- Confirmation run 1 caught a router defect (result-score routing broke
  config/test+impl families: v2 44.5%); fixed to query-shape routing;
  run 2 caught dense dilution of the precise path (router-off ablation);
  fixed by gating dense to the vague route. Each fix cost its seeds.

### Confirmed results (virgin data, single runs)
- v2 controlled suite (seed 667): slicegrep 71.4% - first, +5.3 over BM25
  (66.1), 12 strategies.
- v3 real sessions (seeds 3331-3333, n=286): dense-only 28.3 [23.1,33.5],
  slicegrep 26.6 [21.5,31.7] - statistical tie for first; all other
  methods 21.7-23.4.
- Rejected variants recorded: multiplicative history priors, adaptive
  budget split, diverse semantic fill, RRF packing, subwords in the
  precision rerank, result-score routing.

## [0.4.0] - 2026-07-19

### Added
- **Natural-language queries**: a spaced phrase with 3+ content words
  ("how does budget packing guarantee definitions") auto-expands into
  content-word patterns plus synthesized snake_case bigrams for the lexical
  pass; the semantic passes see the full phrase. Two-word queries
  ("class Context") stay exact - expanding them measurably hurt precision.
- **Subword semantic recall**: the TF-IDF recall pass now tokenizes into
  snake_case/camelCase subwords with light suffix stemming
  (invalidation/invalidate -> one stem), closing the morphology gap where
  embedding retrievers beat lexical tools. Whole-word matches get 3x weight
  over fragment matches. The precision rerank keeps exact vocabulary -
  using subwords there regressed the controlled benchmark 63.9% -> 57.7%.

### Measured (full tuning path, failures included)
- v3 real sessions: 21.2% -> 23.8% session hit, 22.6% -> 23.8% coverage -
  now first on BOTH metrics (tfidf 22.5/20.4, semble 20.0/17.6, rg+rank
  20.0/20.8). v2 controlled: 63.9% -> 62.6%, still first (rg+windows 57.3%),
  call-chain improved 35.0 -> 37.5.
- Rejected variants (recorded to prevent re-treading): subwords in both
  passes (v2 57.7%), subwords recall-only without whole-word weighting
  (v2 58.6%), NL expansion of 2-word queries (symbol family 82.5 -> 72.5).
- semble (MinishLab, embeddings+BM25) added as a baseline by community
  request: v2 38.3% overall, v3 20.0% hit / 17.6% coverage.

## [0.3.0] - 2026-07-19

### Added
- **Hybrid semantic recall** (`semantic=` param, on by default in directory
  mode): alongside regex precision candidates, a TF-IDF pass over 60-line
  windows of the whole corpus proposes candidates whose *vocabulary* matches
  the query even when no line literally matches. Lexical chunks claim ~65%
  of the budget first; non-overlapping `semantic-recall` chunks fill the
  rest. Motivated by benchmark v3 (real sessions mined from git history),
  where regex-gated candidacy lost to pure TF-IDF retrieval.
- Benchmark v3 (`benchmarks/bench3.py`): 80 real changes mined from corpus
  git history; query from the commit message only; ground truth = the
  pre-image regions the real fix touched.

### Measured
- v3 session hit 16.2% -> 21.2%; mean coverage 18.8% -> 22.6% (best of all
  7 strategies). TF-IDF alone still edges hit rate (22.5%) - recorded.
- v2 overall held at 63.9% (no regression): comprehend +7.5, config-flow
  +5.9, symbol -2.5, call-chain -5.0, test+impl -5.0.
- Cost: ~0.3-0.5s per directory call for the corpus TF-IDF pass.

## [0.2.0] - 2026-07-19

### Added
- **Retrieval objectives** (`objective=` / `--objective`): `auto` (default)
  reserves guaranteed budget slots for the best definition chunk, the best
  cross-file usage chunk, and the best test-file chunk when candidates exist;
  `def+caller` / `def+test` reserve just that pair; `single` restores v0.1
  score-order packing.
- **Diversity-aware budget packing**: chunks from already-represented files
  take a score discount during greedy fill, so one hot file cannot consume
  the whole budget and related code from different files is selected together.
- **Semantic rerank**: a TF-IDF cosine stage (computed over the candidate
  set, zero dependencies) blends into lexical scores, improving ranking for
  concept-style queries.

- Benchmark v2 (`benchmarks/bench2.py`): six task families (symbol,
  docstring-concept comprehension, cross-file call-chain, bug localization,
  config/data-flow, test+impl) Ã— seven strategies (raw rg, whole-file,
  rg+windows, rg+file-ranking, jedi/LSP symbol search, TF-IDF vector
  retriever, slicegrep). 240 seeded tasks; multi-span ground truth. slicegrep
  leads overall (60.8% hit rate at 1,995 median tokens, 1 call) but
  rg+windows wins multi-span families and TF-IDF wins concept queries â€”
  documented as the v0.2 roadmap. `bench` extra installs jedi.
- Scaled benchmark mode (`--scale N`): generates up to N seeded, reproducible
  lookup tasks across four pinned corpora (click, flask, requests, rich) and
  measures tokens, success, irrelevance, tool calls, and latency per strategy.

### Fixed
- **Ranking bug found by the scaled benchmark:** the `definition` scoring
  signal only fired in `--boundary fn` mode (it depended on `chunk.symbol`,
  which the default mode never sets), so usage-heavy chunks could crowd the
  actual definition out of the token budget. Definition lines are now detected
  directly (pattern match on a `def`/`class`/`fn`/... line, +25), and the best
  definition chunk is guaranteed a slot when packing the budget.
  300-task success rate: 71.7% â†’ 84.7%.

### Measured (benchmark v2, 240 tasks)
- Overall ground-truth hit rate 60.8% -> 63.9% (best baseline: 57.3%).
  test+impl 30.0% -> 47.5%; symbol 80.0% -> 85.0%; call-chain 37.5% -> 40.0%
  (rg+windows still leads that family at 57.5%); comprehend 65.0% -> 60.0%
  (small regression, TF-IDF retriever still leads at 75.0%). Recorded
  honestly; call-chain and concept ranking remain the open gaps.


## [0.1.0] - 2026-07-19

### Added
- `focused_read()` core engine: grep â†’ slice â†’ rank â†’ dedupe â†’ token-budget â†’
  negative evidence, standard-library only.
- Ranking signals: co-occurrence, all-patterns, rare terms, definition-vs-usage,
  with test/vendor/comment demotion.
- Near-duplicate de-duplication (Jaccard over line sets) and a token-budget cap
  that truncates rather than returning zero chunks.
- Negative evidence: reports patterns/symbols not found, and distinguishes
  "absent from the file" from "fell outside the budgeted chunks".
- Language-aware `--boundary fn` mode (Python indentation + brace languages).
- CLI: `slicegrep` and `fr`, with `--budget`, `--boundary`, `--recursive`,
  `--no-dedupe`, and `--json`.
- MCP server (`slicegrep-mcp`) exposing `focused_read` to any MCP client.

