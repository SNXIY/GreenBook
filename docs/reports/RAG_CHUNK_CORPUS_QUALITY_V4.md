# RAG_CHUNK_CORPUS_QUALITY_V4

Baseline checkpoint: `2db9d9f1ec3a36858dc2398140a34ddd733bdc4a`
Frozen RAG diagnosis checkpoint: `df09dc8bc30a8da4ec1bd5d71d4a13fc035056b6`
Collection: `post_chunks_multilingual_v1` (read-only)

## Verdict

`RAG_CHUNK_CORPUS_QUALITY_ISSUE`

## Actual PostChunker behavior

The production `PostChunker` uses `maxChars=1200` and
`overlapChars=160` (the constructor clamps the minimum max size to
64 and overlap to at most half the max). It recognizes a paragraph boundary
only after a whitespace run containing at least two newline characters. A
short paragraph is emitted directly as its own chunk; overlap is used only
when one paragraph exceeds the max size. Chunk IDs are stable by
`post_id + chunk_index`, and offsets are source-text offsets.

Projection uses `content_object_key → OssStorageService.readTextObject →
PostSearchDocumentService.build → PostChunkProjectionService → PostChunker`.
Chunk embedding text remains labelled `title + tags + description + content`;
the chunk content remains the final evidence unit.

| Reproduction check | Value |
|---|---:|
| Posts compared | 211 |
| Exact PostChunker reproductions | 211 |
| Posts with any reproduction mismatch | 0 |
| Mean body characters | 1682.588 |
| Body p50 characters | 1715.0 |
| Mean stored chunks/post | 23.886 |
| Stored chunks/post p50 / p95 / max | 26.0 / 40.0 / 55 |
| Mean paragraph count/post | 23.877 |
| Short paragraphs (<100) | 3599 |
| Micro paragraphs (<50) | 2629 |
| Paragraphs split into multiple chunks | 1 |

The approximately 1700-character body median coexists with approximately 26
chunks/post because the splitter is paragraph-first and has no minimum chunk
length or short-paragraph merge. The live corpus contains many short Markdown
paragraphs; each becomes an independent retrieval point. The 160-character
overlap is not the explanation for the high chunk count because it applies
only inside an oversized paragraph.

## Chunk length distribution

| Corpus measure | Value |
|---|---:|
| MySQL chunk rows | 5040 |
| Qdrant points | 5040 |
| Unique chunk posts | 211 |
| Posts with 0 chunks | 0 |
| Posts with 1 chunk | 29 |
| Empty chunks | 0 |
| Exact duplicate groups | 373 |
| Near-duplicate pairs | 352 |
| Available-body posts with 0 chunks | 0 |

| Bucket | All chunks | All rate | 75 gold chunks | Gold rate |
|---|---:|---:|---:|---:|
| 0-20 | 1782 | 0.3536 | 0 | 0.0000 |
| 21-50 | 873 | 0.1732 | 1 | 0.0133 |
| 51-100 | 973 | 0.1931 | 20 | 0.2667 |
| 101-200 | 1186 | 0.2353 | 45 | 0.6000 |
| 201-400 | 216 | 0.0429 | 9 | 0.1200 |
| 401-800 | 8 | 0.0016 | 0 | 0.0000 |
| 801-1200 | 2 | 0.0004 | 0 | 0.0000 |
| >1200 | 0 | 0.0000 | 0 | 0.0000 |

| Statistic | All 5040 chunks | 75 unique gold chunks |
|---|---:|---:|
| min | 3 | 50 |
| p25 | 14.0 | 97.0 |
| p50 | 45.0 | 126.0 |
| p75 | 107.0 | 162.5 |
| p95 | 194.0 | 235.4 |
| mean | 68.545238 | 134.693333 |
| max | 1200 | 291 |

## Gold evidence × chunk size

The following is reference-level over 104 gold references. `Hit@10` and MRR
use the frozen current Top10 result; `post-present Hit@10` excludes gold refs
whose parent post was not in the frozen Top10 post pool.

| Bucket | Gold refs | Rate | Hit@10 | MRR | Post-present Hit@10 |
|---|---:|---:|---:|---:|---:|
| 0-20 | 0 | 0.0000 | 0.000000 | 0.000000 | 0.000000 |
| 21-50 | 1 | 0.0096 | 0.000000 | 0.000000 | 0.000000 |
| 51-100 | 24 | 0.2308 | 0.208333 | 0.026505 | 0.250000 |
| 101-200 | 61 | 0.5865 | 0.278689 | 0.088999 | 0.361702 |
| 201-400 | 18 | 0.1731 | 0.222222 | 0.089506 | 0.333333 |
| 401-800 | 0 | 0.0000 | 0.000000 | 0.000000 | 0.000000 |
| 801-1200 | 0 | 0.0000 | 0.000000 | 0.000000 | 0.000000 |
| >1200 | 0 | 0.0000 | 0.000000 | 0.000000 | 0.000000 |

Gold reference length distribution: `{'count': 104, 'min': 50, 'p25': 101.75, 'p50': 130.0, 'p75': 177.5, 'p95': 248.85, 'mean': 143.076923, 'max': 291}`.
The detailed 104-reference records, including lengths and ranks, are in the
JSON artifact.

## LOCAL_RANKING_FAILURE breakdown

The audit covers the 48 baseline `LOCAL_RANKING_FAILURE` references. Rules are
mutually exclusive and intentionally use only corpus/selection observables;
the full per-case trace includes competing text summaries, query score,
lexical similarity, and embedding similarity to the gold chunk.
The size-oriented categories (`MICRO_CHUNK_NOISE` plus
`GOLD_CHUNK_TOO_FRAGMENTED`) account for
`29`
of 48 local failures; this is evidence of chunk-level noise, not proof that
all such cases require re-chunking.

| Category | Count | Rate |
|---|---:|---:|
| `MICRO_CHUNK_NOISE` | 25 | 0.5208 |
| `DUPLICATE_CHUNK_NOISE` | 8 | 0.1667 |
| `SAME_POST_FRAGMENT_COMPETITION` | 3 | 0.0625 |
| `GOLD_CHUNK_TOO_FRAGMENTED` | 4 | 0.0833 |
| `GENUINE_SEMANTIC_RANKING_FAILURE` | 8 | 0.1667 |

## Duplicate competition

The live chunk audit found
`373` exact duplicate groups and
`352` heuristic near-duplicate pairs.
For the frozen baseline Top10 output, duplicate-equivalent content occupied
`22` of
`450` slots, giving a
slot-waste rate of `0.0489`.
Per-query unique semantic evidence, duplicate-equivalent results, and same-post
competition counts are in the JSON artifact.

## Offline eligibility experiments

All experiments used the same frozen Top10 post candidates and current
`post_chunks_multilingual_v1` scores/model. No production collection was rebuilt. `Avg
candidates` is the pre-output candidate pool per query; output remains bounded
to 10 chunks. `Gold removed` is conditional on the gold parent post being in
the frozen Top10 pool.

### ALL answerable queries

| Strategy | Cond R@5 | Cond R@10 | Final R@10 | MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Avg candidates | Dup waste | Gold removed | Rank p95 ms | Complexity |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| CURRENT_BASELINE | 0.209402 | 0.307692 | 0.251852 | 0.141755 | 0.022222 | 0.244444 | 0.266667 | 0.422222 | 247.73 | 0.0489 | 0.0000 | 0.058 | current frozen Top10; full fixed-post candidate pool |
| MICRO_CHUNK_FILTER_LT30 | 0.209402 | 0.371795 | 0.296296 | 0.178431 | 0.044444 | 0.244444 | 0.266667 | 0.466667 | 140.38 | 0.0311 | 0.0000 | 0.068 | filter content length < 30; rerank current full hits |
| MICRO_CHUNK_FILTER_LT50 | 0.217949 | 0.393162 | 0.314815 | 0.183325 | 0.044444 | 0.244444 | 0.288889 | 0.511111 | 116.47 | 0.0311 | 0.0000 | 0.038 | filter content length < 50; rerank current full hits |
| MICRO_CHUNK_FILTER_LT80 | 0.260684 | 0.371795 | 0.296296 | 0.184463 | 0.044444 | 0.244444 | 0.355556 | 0.488889 | 86.53 | 0.0400 | 0.1750 | 0.029 | filter content length < 80; rerank current full hits |
| MICRO_CHUNK_FILTER_LT100 | 0.273504 | 0.367521 | 0.292593 | 0.190026 | 0.044444 | 0.244444 | 0.377778 | 0.488889 | 70.36 | 0.0444 | 0.2375 | 0.030 | filter content length < 100; rerank current full hits |
| MERGE_SHORT_TO_120 | 0.294872 | 0.393162 | 0.325926 | 0.209799 | 0.088889 | 0.244444 | 0.422222 | 0.466667 | 98.04 | 0.0667 | 0.0000 | 15.616 | merge adjacent chunks while current span < 120, max merged span 1200 |
| MERGE_SHORT_TO_240 | 0.303419 | 0.427350 | 0.344444 | 0.281543 | 0.155556 | 0.333333 | 0.377778 | 0.488889 | 61.16 | 0.0667 | 0.0000 | 8.799 | merge adjacent chunks while current span < 240, max merged span 1200 |
| DEDUP_EXACT_NEAR | 0.209402 | 0.320513 | 0.262963 | 0.161698 | 0.022222 | 0.244444 | 0.266667 | 0.422222 | 214.62 | 0.0022 | 0.0125 | 10.629 | greedy representative selection for exact/heuristic near duplicates |
| MERGE_120_PLUS_DEDUP | 0.307692 | 0.418803 | 0.337037 | 0.217012 | 0.088889 | 0.244444 | 0.444444 | 0.488889 | 94.07 | 0.0044 | 0.0125 | 27.938 | merge short chunks to 120 then exact/near duplicate representative selection |
| MERGE_240_PLUS_DEDUP | 0.303419 | 0.452991 | 0.355556 | 0.283662 | 0.155556 | 0.333333 | 0.377778 | 0.511111 | 59.29 | 0.0044 | 0.0375 | 37.791 | merge short chunks to 240 then exact/near duplicate representative selection |

### STRONG_COVERAGE_ONLY

| Strategy | Cond R@5 | Cond R@10 | Final R@10 | MRR | Hit@1 | Hit@3 | Hit@5 | Hit@10 | Avg candidates | Dup waste | Gold removed | Rank p95 ms | Complexity |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| CURRENT_BASELINE | 0.255208 | 0.354167 | 0.280702 | 0.164108 | 0.026316 | 0.289474 | 0.315789 | 0.473684 | 249.34 | 0.0474 | 0.0000 | 0.058 | current frozen Top10; full fixed-post candidate pool |
| MICRO_CHUNK_FILTER_LT30 | 0.255208 | 0.401042 | 0.320175 | 0.199289 | 0.052632 | 0.289474 | 0.315789 | 0.500000 | 143.84 | 0.0368 | 0.0000 | 0.068 | filter content length < 30; rerank current full hits |
| MICRO_CHUNK_FILTER_LT50 | 0.265625 | 0.427083 | 0.342105 | 0.204075 | 0.052632 | 0.289474 | 0.342105 | 0.552632 | 118.13 | 0.0368 | 0.0000 | 0.038 | filter content length < 50; rerank current full hits |
| MICRO_CHUNK_FILTER_LT80 | 0.286458 | 0.401042 | 0.320175 | 0.207997 | 0.052632 | 0.289474 | 0.394737 | 0.526316 | 87.84 | 0.0474 | 0.1571 | 0.029 | filter content length < 80; rerank current full hits |
| MICRO_CHUNK_FILTER_LT100 | 0.302083 | 0.395833 | 0.315789 | 0.214285 | 0.052632 | 0.289474 | 0.421053 | 0.526316 | 71.13 | 0.0500 | 0.2286 | 0.030 | filter content length < 100; rerank current full hits |
| MERGE_SHORT_TO_120 | 0.317708 | 0.427083 | 0.359649 | 0.229902 | 0.105263 | 0.263158 | 0.447368 | 0.500000 | 98.97 | 0.0632 | 0.0000 | 15.616 | merge adjacent chunks while current span < 120, max merged span 1200 |
| MERGE_SHORT_TO_240 | 0.359375 | 0.453125 | 0.368421 | 0.316581 | 0.184211 | 0.394737 | 0.421053 | 0.500000 | 62.08 | 0.0658 | 0.0000 | 8.799 | merge adjacent chunks while current span < 240, max merged span 1200 |
| DEDUP_EXACT_NEAR | 0.255208 | 0.369792 | 0.293860 | 0.181303 | 0.026316 | 0.289474 | 0.315789 | 0.473684 | 215.55 | 0.0026 | 0.0143 | 10.629 | greedy representative selection for exact/heuristic near duplicates |
| MERGE_120_PLUS_DEDUP | 0.333333 | 0.427083 | 0.359649 | 0.236636 | 0.105263 | 0.263158 | 0.473684 | 0.500000 | 94.66 | 0.0053 | 0.0143 | 27.938 | merge short chunks to 120 then exact/near duplicate representative selection |
| MERGE_240_PLUS_DEDUP | 0.359375 | 0.453125 | 0.368421 | 0.317477 | 0.184211 | 0.394737 | 0.421053 | 0.500000 | 60.08 | 0.0053 | 0.0429 | 37.791 | merge short chunks to 240 then exact/near duplicate representative selection |

Experiments tested: current baseline; micro-chunk filters at `<30`, `<50`,
`<80`, and `<100`; adjacent-short-chunk merge targets of 120 and 240
characters; duplicate/near-duplicate representative selection; and merge-120
plus merge-240 deduplication. Merge simulation preserves source chunk IDs and
source offset span so gold evidence can be traced back to the original corpus.

## Acceptance and winning strategy

The offline acceptance rule uses a meaningful gain threshold of +0.02 for both
ALL and STRONG conditional Recall@10, +0.02 for ALL Final Evidence Recall@10,
strict MRR improvement, gold removal ≤1%, reduced duplicate slot waste, and
fixed Top10 post candidates.

Chosen strategy: `MICRO_CHUNK_FILTER_LT50`.
Acceptance summary: `{"accepted_strategies": ["MICRO_CHUNK_FILTER_LT30", "MICRO_CHUNK_FILTER_LT50"], "accepted_strategy_count": 2, "thresholds": {"gold_removed_rate_max": 0.01, "recall_gain": 0.02}}`

## Retrieval failure correlation

`LOCAL_RANKING_FAILURE` remains the dominant family: 48/78 missed references,
all from `GOOD_CORPUS` posts in the prior corpus audit. The V4 chunk-noise
classification is a diagnosis of the selected competitors, not a replacement
for that frozen failure-family label.

| Failure family | Missed refs | Good-corpus refs | Other-corpus refs |
|---|---:|---:|---:|
| `POST_CANDIDATE_FAILURE` | 24 | 19 | 5 |
| `LOCAL_RANKING_FAILURE` | 48 | 48 | 0 |
| `CHUNK_BOUNDARY_FAILURE` | 6 | 6 | 0 |

## Exact FIRST_BAD_STATE

`POST_RETRIEVAL → CHUNK_RETRIEVAL`

The audit does not move the first bad state to evidence selection or
generation. MySQL and Qdrant projection identities remain aligned; this V4
phase only tests whether the chunk corpus causes candidate noise.

## v2 collection decision

`NO — the winning strategy is an eligibility filter; it does not justify a new v2 collection.` No `post_chunks_multilingual_v2` collection was created or
rebuilt. The accepted `<50` filter can be considered as a minimal retrieval
eligibility policy; it does not require a new collection. A versioned v2
collection remains unjustified unless a merge/rechunk strategy separately
passes the gate.

## Next recommendation

Keep `post_chunks_multilingual_v1` and production retrieval unchanged in this phase. Treat
paragraph-first construction without short-paragraph merging as the primary
corpus-quality hypothesis, and keep the `<50` filter as an offline eligibility
hypothesis rather than a production setting. Before authorizing a versioned v2
rebuild, pin the retrieval snapshot and validate one bounded merge/section-aware
projection whose deduplication preserves every gold source span; no v2
migration is authorized by the current acceptance results.

## Production / dirty state

Production files changed: `[]`. No Memory, ActionLoop, Durable Runtime, MCP,
Hybrid Search, Java business truth, generator, or evidence selection code was
modified. The V4 script and result/report artifacts are evaluation-only.

Baseline audit commit already saved and pushed: `722a072` (`test: add rag
corpus quality audit`). V4 artifacts are intentionally left dirty for review.

## Verdict detail

- Dataset: 50 queries / 45 answerable / 104 gold refs / 75 unique gold chunks.
- Full frozen per-case retrieval traces: 45.
- Qdrant collection: `post_chunks_multilingual_v1`, read-only; no rebuild.
- Embedding: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`.
- Query snapshot drift against frozen V3 Top10: `6`
  (rag-008, rag-011, rag-012, rag-014, rag-016, rag-017).
- `CURRENT_BASELINE` preserves the frozen V3 Top10 selection; simulated
  strategies use the fresh read-only full candidate ranking for the same frozen
  Top10 post pool. The snapshot drift is retained as a comparability caveat.
- No tests beyond the offline harness, Ruff, and syntax validation were run in
  this phase.

`RAG_CHUNK_CORPUS_QUALITY_V4_COMPLETE`
