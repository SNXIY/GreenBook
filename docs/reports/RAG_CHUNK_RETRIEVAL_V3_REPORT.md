# RAG_CHUNK_RETRIEVAL_V3_REPORT

Checkpoint: `df09dc8bc30a8da4ec1bd5d71d4a13fc035056b6`

## Verdict

`RAG_CHUNK_RETRIEVAL_NO_GAIN`

## Frozen conditions

- Dataset: `rag_evidence_v2`, 50 queries / 45 answerable / 104 gold chunk refs.
- Post candidates: frozen current-production Top10 lists.
- Chunk collection: `post_chunks_multilingual_v1`, read-only.
- Embedding: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, 384d normalized.
- Production representation: unchanged `title + tags + description + content`.
- Evidence selection and generation: not evaluated as a change surface.
- Qdrant writes/rebuild: none.

## Strategy comparison

| Strategy | Conditional Recall@5 | Conditional Recall@10 | Final Evidence Recall@5 | Final Evidence Recall@10 | Chunk MRR | Added candidates | p95 rank ms | Complexity |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| BASELINE | 0.209402 | 0.307692 | 0.166667 | 0.251852 | 0.141755 | 0.00 | 0.004 | current production score |
| PARENT_AWARE (parent_alpha=0.02) | 0.183761 | 0.307692 | 0.159259 | 0.251852 | 0.155575 | 0.00 | 3.405 | O(candidate chunks) lexical parent signal |
| PARENT_AWARE (parent_alpha=0.05) | 0.183761 | 0.307692 | 0.159259 | 0.251852 | 0.154260 | 0.00 | 3.018 | O(candidate chunks) lexical parent signal |
| PARENT_AWARE (parent_alpha=0.1) | 0.192308 | 0.307692 | 0.166667 | 0.251852 | 0.156257 | 0.00 | 3.009 | O(candidate chunks) lexical parent signal |
| PARENT_AWARE (parent_alpha=0.15) | 0.192308 | 0.307692 | 0.166667 | 0.251852 | 0.157916 | 0.00 | 3.123 | O(candidate chunks) lexical parent signal |
| NEIGHBOR_EXPANSION (neighbor_penalty=0.02) | 0.209402 | 0.307692 | 0.166667 | 0.251852 | 0.143464 | 14.16 | 0.111 | O(top10 local neighbors) |
| NEIGHBOR_EXPANSION (neighbor_penalty=0.05) | 0.209402 | 0.307692 | 0.166667 | 0.251852 | 0.143464 | 14.16 | 0.151 | O(top10 local neighbors) |
| BOUNDARY_CONTEXT (context_alpha=0.02) | 0.209402 | 0.311966 | 0.166667 | 0.255556 | 0.156821 | 0.00 | 3.648 | O(candidate chunks) local context |
| BOUNDARY_CONTEXT (context_alpha=0.05) | 0.183761 | 0.299145 | 0.159259 | 0.244444 | 0.154257 | 0.00 | 3.654 | O(candidate chunks) local context |

`Hit@K` is query-level hit rate; `overall` includes post misses and
`gold_post_present` is conditional on at least one gold post being in Top10.
Conditional Chunk Recall uses only gold references whose parent post is in the
fixed Top10 candidate set. Final Evidence Recall uses all 104 gold references.
Gold Chunk MRR uses the first gold chunk rank in the strategy output. The
`irrelevant_chunk_rate` field is an exact-gold-ID distractor proxy, not a human
judgement that every non-gold chunk is semantically irrelevant.

## Baseline Hit@K

| K | Overall query hit | Gold post present |
|---:|---:|---:|
| 1 | 0.022222 | 0.025641 |
| 3 | 0.244444 | 0.282051 |
| 5 | 0.266667 | 0.307692 |
| 10 | 0.422222 | 0.487179 |

The complete per-strategy values for Hit@1/3/5/10, the distractor proxy,
selected counts, and latency are in the JSON result artifact.

## Tested strategies

1. `BASELINE`: current Qdrant chunk score over the fixed Top10 post pool.
2. `PARENT_AWARE`: unified chunk score plus a small deterministic lexical
   signal from title/tags/description. Chunk remains the evidence unit.
3. `NEIGHBOR_EXPANSION`: current Top10 hits plus immediate same-post neighbors,
   with a penalty; total output remains bounded at 10.
4. `BOUNDARY_CONTEXT`: chunk score plus lexical signal from chunk_i-1,
   chunk_i, and chunk_i+1. This is an offline boundary diagnostic only.

No alternate embedding, Hybrid Search, post depth, RRF, selector, generator,
or Memory path was changed.

The query-side expansion experiment was not run: the primary diagnosis found
no `QUERY_CHUNK_MISMATCH` cases after the fixed-post full-candidate trace, so no
deterministic context-term expansion was justified.

## Acceptance gate

The offline gate requires both @10 recall metrics to improve by at least 0.02,
MRR to improve, irrelevant selected-chunk rate to remain within 0.05 of
baseline, and the post candidate depth to remain fixed at Top10.

No strategy passed the acceptance gate; keep current production.

## Before → after

| Metric | Baseline | Chosen strategy |
|---|---:|---:|
| Conditional Chunk Recall@10 | 0.307692 | 0.307692 |
| Final Evidence Recall@10 | 0.251852 | 0.251852 |
| Gold Chunk MRR | 0.141755 | 0.141755 |
| Exact-gold distractor proxy | 0.942222 | 0.942222 |

## Latency and context impact

| Measure | Baseline | Maximum across tested strategies |
|---|---:|---:|
| Selected chunks (mean) | 10.00 | 10.00 |
| Selected chunks (max) | 10 | 10 |
| Rank latency p50 (ms) | 0.003 | 2.321 |
| Rank latency p95 (ms) | 0.004 | 3.654 |
| Rank latency max (ms) | 0.008 | 4.735 |

Every strategy keeps the output bound at 10 chunks. No evidence selection,
generator, or production context assembly was changed or executed by this
offline harness, so final answer token impact is unchanged and not re-measured.

## Production / collection status

No production files were changed by the offline experiments. The existing
`post_chunks_multilingual_v1` collection was read only; no new collection and
no rebuild was performed.

## Focused verification

The offline harness completed with 45 answerable-query traces and no dataset
annotation issue. The focused RAG regression was run separately after the
offline decision: Python RAG tests passed 19/19 and Java chunk/evidence tests
passed 9/9. Memory, ActionLoop, Durable Runtime, MCP, Search, RRF, Java
business truth, EvidenceSelection, and Generation remained outside this
change.

## Verdict detail

- FIRST_BAD_STATE: `POST_RETRIEVAL → CHUNK_RETRIEVAL`.
- Dataset annotation issue count: `0`.
- Query-vector / Qdrant Top10 snapshot drift count: `6`.
- Strategy output remains bounded at `10` chunks.

`RAG_CHUNK_RETRIEVAL_V3_REPORT_COMPLETE`
