# RAG_SEMANTIC_CHUNK_MERGE_V6

V5 checkpoint: `27350d5cafe5d2fc13d28528bf79f59d24dcc943`
V4 corpus checkpoint: `3f10819e6e76a6dae99a61ee254507ac2d3aec3d`
Frozen snapshot: `be65bfed2d90405472e0c19a6f472861ea23625b5372d4b7fa62b1df80c5de09`

## Verdict

`RAG_SEMANTIC_CHUNK_MERGE_NO_GAIN`

Chosen offline strategy: `None`

## Frozen inputs

| Item | Value |
|---|---:|
| Answerable queries | 45 |
| Gold references | 104 |
| Candidate post depth | 10 |
| Candidate posts in snapshot | 130 |
| Frozen chunk catalog | 5040 |
| Frozen snapshot rerun drift | 0 |
| Historical capture-vs-V3 drift | 6 |
| Frozen source posts | 211 |
| Source manifest digest | `6b18a50e6cb0ec48c2718e95226d609155205390dae021d37207ac35e01283e6` |

V6 uses the V5 frozen Top10 post IDs and the V5 frozen corpus audit. No live
Qdrant query, MySQL read, collection rebuild, post retrieval, query rewrite,
embedding-model change, evidence-selector change, or generator change is used.

## Semantic merge rules

The current source units are the existing PostChunker chunks, ordered by
`post_id + chunk_index`. A group may contain only adjacent source chunks from
one post. A Markdown heading starts a new section; no group crosses a section
boundary. The merged text is the exact trimmed source interval from
`source_start` through `source_end`; the harness never joins unrelated text.

The production baseline is paragraph-first with `maxChars=1200` and
`overlapChars=160`. A paragraph boundary requires a whitespace run containing
at least two newlines. Short paragraphs are emitted as independent chunks;
there is no production minimum length or short-paragraph merge. Overlap is
used only while splitting one oversized paragraph.

| Source audit | Value |
|---|---:|
| Source chunks | 5040 |
| Source posts | 211 |
| Markdown headings | 1795 |
| List-item chunks | 803 |
| Detected sections | 1960 |
| Existing source-span overlaps | 2 |
| Source ordering violations | 0 |

The strategies are:

- `CURRENT_SNAPSHOT_BASELINE`: existing frozen chunk boundaries and frozen
  candidate scores.
- `SHORT_TO_NEXT`: a short source unit (`length < 50`) is merged with the
  immediately following same-section unit when the 1200-character bound holds.
- `SHORT_TO_PREVIOUS`: a short source unit is merged into the immediately
  preceding same-section group when the bound holds.
- `HEADING_AWARE`: a heading is merged with following same-section units until
  the source span reaches 120 characters or the section ends.
- `BOUNDED_GROUP_120`, `BOUNDED_GROUP_180`, and `BOUNDED_GROUP_240`: adjacent
  same-section units accumulate until the target is reached, never exceeding
  the existing 1200-character maximum.

## Provenance guarantees

Every simulated chunk carries `source_chunk_ids`, `source_start`,
`source_end`, `post_id`, and source order. All strategies are required to
partition the 5040 source chunk IDs exactly once. The source checks below are
computed independently for every strategy.

| Strategy | Coverage | Loss | Duplication | Cross-section merges | Fabricated text | Exact partition |
|---|---:|---:|---:|---:|---:|---|
| CURRENT_SNAPSHOT_BASELINE | 1.000000 | 0.000000 | 0.000000 | 0 | 0 | True |
| SHORT_TO_NEXT | 1.000000 | 0.000000 | 0.000000 | 0 | 0 | True |
| SHORT_TO_PREVIOUS | 1.000000 | 0.000000 | 0.000000 | 0 | 0 | True |
| HEADING_AWARE | 1.000000 | 0.000000 | 0.000000 | 0 | 0 | True |
| BOUNDED_GROUP_120 | 1.000000 | 0.000000 | 0.000000 | 0 | 0 | True |
| BOUNDED_GROUP_180 | 1.000000 | 0.000000 | 0.000000 | 0 | 0 | True |
| BOUNDED_GROUP_240 | 1.000000 | 0.000000 | 0.000000 | 0 | 0 | True |

## Before and after chunk distribution

### ALL frozen source corpus

| Strategy | Chunks | Median | <50 rate | Cond R@5 | Cond R@10 | Final R@10 | MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Dup waste | Gold preserve |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_SNAPSHOT_BASELINE | 5040 | 45.0 | 0.521627 | 0.209402 | 0.307692 | 0.251852 | 0.156315 | 0.022222 | 0.244444 | 0.266667 | 0.422222 | 0.048889 | 1.000000 |
| SHORT_TO_NEXT | 3359 | 94.0 | 0.207800 | 0.192308 | 0.264957 | 0.229630 | 0.166172 | 0.044444 | 0.244444 | 0.266667 | 0.355556 | 0.051111 | 1.000000 |
| SHORT_TO_PREVIOUS | 4151 | 68.0 | 0.359431 | 0.183761 | 0.316239 | 0.259259 | 0.158317 | 0.022222 | 0.244444 | 0.244444 | 0.444444 | 0.053333 | 1.000000 |
| HEADING_AWARE | 2665 | 127.0 | 0.160225 | 0.252137 | 0.346154 | 0.288889 | 0.211336 | 0.088889 | 0.288889 | 0.333333 | 0.444444 | 0.055556 | 1.000000 |
| BOUNDED_GROUP_120 | 2549 | 132.0 | 0.136132 | 0.252137 | 0.346154 | 0.288889 | 0.212123 | 0.088889 | 0.288889 | 0.333333 | 0.444444 | 0.055556 | 1.000000 |
| BOUNDED_GROUP_180 | 2312 | 145.0 | 0.118512 | 0.256410 | 0.324786 | 0.270370 | 0.199873 | 0.088889 | 0.244444 | 0.333333 | 0.422222 | 0.060000 | 1.000000 |
| BOUNDED_GROUP_240 | 2130 | 151.0 | 0.111737 | 0.264957 | 0.346154 | 0.288889 | 0.219781 | 0.111111 | 0.266667 | 0.355556 | 0.422222 | 0.075556 | 1.000000 |

The current corpus has `5040` chunks and a `<50`
rate of `0.521627`. The simulated
strategies do not change the source corpus, only its offline grouping.

| Distribution | Min | P25 | P50 | P75 | P95 | Mean | Max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Current source chunks | 3 | 14.0 | 45.0 | 107.0 | 194.0 | 68.545238 | 1200 |
| Strongest observed `BOUNDED_GROUP_240` | 4 | 96.0 | 151.0 | 231.75 | 336.0 | 164.989671 | 1200 |

### STRONG_COVERAGE_ONLY retrieval metrics

| Strategy | Strong Cond R@10 | Strong Final R@10 | Strong MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_SNAPSHOT_BASELINE | 0.354167 | 0.280702 | 0.175448 | 0.026316 | 0.289474 | 0.315789 | 0.473684 |
| SHORT_TO_NEXT | 0.302083 | 0.254386 | 0.185894 | 0.052632 | 0.289474 | 0.289474 | 0.394737 |
| SHORT_TO_PREVIOUS | 0.364583 | 0.289474 | 0.177111 | 0.026316 | 0.289474 | 0.289474 | 0.500000 |
| HEADING_AWARE | 0.369792 | 0.311403 | 0.231308 | 0.105263 | 0.315789 | 0.342105 | 0.473684 |
| BOUNDED_GROUP_120 | 0.369792 | 0.311403 | 0.232240 | 0.105263 | 0.315789 | 0.342105 | 0.473684 |
| BOUNDED_GROUP_180 | 0.343750 | 0.289474 | 0.215501 | 0.105263 | 0.236842 | 0.342105 | 0.447368 |
| BOUNDED_GROUP_240 | 0.369792 | 0.311403 | 0.239045 | 0.131579 | 0.263158 | 0.368421 | 0.447368 |

## Gold preservation

Each old gold chunk ID is remapped through `source_chunk_ids`, rather than
being treated as lost merely because the simulated chunk ID changes.

| Strategy | Gold preservation | Span containment | Lost refs | Mapped gold length p50 |
|---|---:|---:|---:|---:|
| CURRENT_SNAPSHOT_BASELINE | 1.000000 | 1.000000 | 0 | 130.0 |
| SHORT_TO_NEXT | 1.000000 | 1.000000 | 0 | 147.0 |
| SHORT_TO_PREVIOUS | 1.000000 | 1.000000 | 0 | 131.0 |
| HEADING_AWARE | 1.000000 | 1.000000 | 0 | 169.0 |
| BOUNDED_GROUP_120 | 1.000000 | 1.000000 | 0 | 169.5 |
| BOUNDED_GROUP_180 | 1.000000 | 1.000000 | 0 | 202.0 |
| BOUNDED_GROUP_240 | 1.000000 | 1.000000 | 0 | 238.0 |

## Offline retrieval metrics

All ranking uses the same frozen Top10 candidate post set. Simulated chunks
use `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` with dimension `384`
and normalized cosine scoring. `CURRENT_SNAPSHOT_BASELINE` retains the frozen
snapshot ranking so the V5 baseline remains directly comparable.

### ALL

| Strategy | Chunks | Median | <50 rate | Cond R@5 | Cond R@10 | Final R@10 | MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Dup waste | Gold preserve |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_SNAPSHOT_BASELINE | 5040 | 45.0 | 0.521627 | 0.209402 | 0.307692 | 0.251852 | 0.156315 | 0.022222 | 0.244444 | 0.266667 | 0.422222 | 0.048889 | 1.000000 |
| SHORT_TO_NEXT | 3359 | 94.0 | 0.207800 | 0.192308 | 0.264957 | 0.229630 | 0.166172 | 0.044444 | 0.244444 | 0.266667 | 0.355556 | 0.051111 | 1.000000 |
| SHORT_TO_PREVIOUS | 4151 | 68.0 | 0.359431 | 0.183761 | 0.316239 | 0.259259 | 0.158317 | 0.022222 | 0.244444 | 0.244444 | 0.444444 | 0.053333 | 1.000000 |
| HEADING_AWARE | 2665 | 127.0 | 0.160225 | 0.252137 | 0.346154 | 0.288889 | 0.211336 | 0.088889 | 0.288889 | 0.333333 | 0.444444 | 0.055556 | 1.000000 |
| BOUNDED_GROUP_120 | 2549 | 132.0 | 0.136132 | 0.252137 | 0.346154 | 0.288889 | 0.212123 | 0.088889 | 0.288889 | 0.333333 | 0.444444 | 0.055556 | 1.000000 |
| BOUNDED_GROUP_180 | 2312 | 145.0 | 0.118512 | 0.256410 | 0.324786 | 0.270370 | 0.199873 | 0.088889 | 0.244444 | 0.333333 | 0.422222 | 0.060000 | 1.000000 |
| BOUNDED_GROUP_240 | 2130 | 151.0 | 0.111737 | 0.264957 | 0.346154 | 0.288889 | 0.219781 | 0.111111 | 0.266667 | 0.355556 | 0.422222 | 0.075556 | 1.000000 |

### STRONG_COVERAGE_ONLY

| Strategy | Strong Cond R@10 | Strong Final R@10 | Strong MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_SNAPSHOT_BASELINE | 0.354167 | 0.280702 | 0.175448 | 0.026316 | 0.289474 | 0.315789 | 0.473684 |
| SHORT_TO_NEXT | 0.302083 | 0.254386 | 0.185894 | 0.052632 | 0.289474 | 0.289474 | 0.394737 |
| SHORT_TO_PREVIOUS | 0.364583 | 0.289474 | 0.177111 | 0.026316 | 0.289474 | 0.289474 | 0.500000 |
| HEADING_AWARE | 0.369792 | 0.311403 | 0.231308 | 0.105263 | 0.315789 | 0.342105 | 0.473684 |
| BOUNDED_GROUP_120 | 0.369792 | 0.311403 | 0.232240 | 0.105263 | 0.315789 | 0.342105 | 0.473684 |
| BOUNDED_GROUP_180 | 0.343750 | 0.289474 | 0.215501 | 0.105263 | 0.236842 | 0.342105 | 0.447368 |
| BOUNDED_GROUP_240 | 0.369792 | 0.311403 | 0.239045 | 0.131579 | 0.263158 | 0.368421 | 0.447368 |

Per-strategy JSON contains candidate counts, selected counts, Hit@K, MRR,
duplicate slot waste, rank timings, and compact selected source mappings.

## Remaining ranking failure families

The V5 48 local-ranking cases are rechecked after each simulated strategy. A
case is resolved when a selected merged item covers its original gold source
chunk ID; unresolved cases are reclassified using the same noise families.

### Strategy shown: `BOUNDED_GROUP_240`

`BOUNDED_GROUP_240` is the chosen strategy when the gate passes; because this
run has no accepted strategy, it is the strongest observed candidate by
Conditional Recall@10 and then MRR. It is diagnostic only and is not a
production recommendation.

| Family | V5 baseline | Resolved | Remaining |
|---|---:|---:|---:|
| `MICRO_CHUNK_NOISE` | 25 | 2 | 3 |
| `DUPLICATE_CHUNK_NOISE` | 8 | 0 | 11 |
| `SAME_POST_FRAGMENT_COMPETITION` | 3 | 1 | 0 |
| `GOLD_CHUNK_TOO_FRAGMENTED` | 4 | 1 | 7 |
| `GENUINE_SEMANTIC_RANKING_FAILURE` | 8 | 1 | 22 |

All strategy-level failure counts and case traces are in the JSON artifact.

## V2 collection decision

No versioned chunk collection is justified by the acceptance gate; retain `post_chunks_multilingual_v1`.

## Acceptance decision

The gate requires: conditional R@10 materially above `0.307692`
(preferred approximately 0.38 or higher), strong conditional R@10 above
`0.354167`, an MRR improvement, 100% gold/span
preservation, zero source loss/duplication, materially fewer micro-chunks, no
larger output depth, fixed candidate post depth, and acceptable offline
complexity. No production change is made even if the offline gate passes.

- `CURRENT_SNAPSHOT_BASELINE`: **REJECTED** — all_conditional_recall_below_0.38, strong_conditional_recall_below_0.40, mrr_not_materially_improved, micro_chunk_rate_not_reduced
- `SHORT_TO_NEXT`: **REJECTED** — all_conditional_recall_below_0.38, strong_conditional_recall_below_0.40
- `SHORT_TO_PREVIOUS`: **REJECTED** — all_conditional_recall_below_0.38, strong_conditional_recall_below_0.40, mrr_not_materially_improved
- `HEADING_AWARE`: **REJECTED** — all_conditional_recall_below_0.38, strong_conditional_recall_below_0.40
- `BOUNDED_GROUP_120`: **REJECTED** — all_conditional_recall_below_0.38, strong_conditional_recall_below_0.40
- `BOUNDED_GROUP_180`: **REJECTED** — all_conditional_recall_below_0.38, strong_conditional_recall_below_0.40
- `BOUNDED_GROUP_240`: **REJECTED** — all_conditional_recall_below_0.38, strong_conditional_recall_below_0.40

## Production files changed

`[]`

`post_chunks_multilingual_v1` was not modified. `post_chunks_multilingual_v2`
was not created. No Java, Qdrant, MySQL, RAG runtime, EvidenceSelector, or
Generator file was changed.

## Validation

- V5 checkpoint committed and pushed before V6 evaluation.
- Frozen source hashes and every source span were checked read-only.
- Snapshot input was fixed; no snapshot capture or live retrieval occurred.
- Embedding and ranking were offline only.
- Ruff and syntax validation were run for the V6 evaluator.
- No rebuild, migration, production regression, Memory evaluation, or
  Generation evaluation was run.

## Next recommendation

Keep production chunking and post_chunks_multilingual_v1 unchanged. The tested semantic grouping strategies did not clear all gates; do not rebuild a v2 collection or move to reranking yet.
