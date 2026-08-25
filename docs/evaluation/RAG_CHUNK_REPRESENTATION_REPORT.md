# RAG_CHUNK_REPRESENTATION_REPORT

Generated: 2026-08-25

## Experiment Setup

- Dataset: `rag_evidence_v2`, all 50 queries.
- Answerable queries: 45; no-answer queries: 5.
- Post candidates: frozen Top10 lists from the completed chunk-retrieval diagnosis; no Search rerun.
- Candidate posts: 138 unique posts.
- Canonical chunks: 3399 public/published MySQL chunks.
- Ranking: offline exact cosine over chunks belonging to each query's frozen candidate posts.
- A: content only; B: title + content; C: title + tags + content.
- Query/document encoder: existing multilingual embedding sidecar, 384 dimensions, normalized vectors.
- Qdrant: not contacted. No Qdrant update, rebuild, production code, chunking, model, or prompt change.

## Metrics

| Representation | Conditional Chunk Recall@5 | Conditional Chunk Recall@10 | Final Evidence Recall@5 | Final Evidence Recall@10 | Rank p50 ms | Rank p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| A_CONTENT_ONLY | 0.098291 | 0.179487 | 0.085185 | 0.155556 | 5.155 | 8.383 |
| B_TITLE_CONTENT | 0.106838 | 0.256410 | 0.092593 | 0.196296 | 5.458 | 7.894 |
| C_TITLE_TAGS_CONTENT | 0.179487 | 0.269231 | 0.155556 | 0.233333 | 5.169 | 7.862 |

Conditional metrics count only gold chunks whose gold post is present in the
fixed Top10 candidate list. Final evidence metrics count all gold chunks, so
post-retrieval misses remain visible. Deterministic evidence selection is
modeled as the same ranked top-k list; the prior live diagnosis measured zero
selection loss.

Embedding latency is a one-time offline document-vector construction cost;
query latency is shared by all three representations:

| Operation | A p50/p95 ms | B p50/p95 ms | C p50/p95 ms |
|---|---:|---:|---:|
| Document embedding | 30.866 / 44.341 | 40.032 / 51.547 | 41.765 / 52.594 |
| Exact ranking | 5.155 / 8.383 | 5.458 / 7.894 | 5.169 / 7.862 |
| Offline query + ranking | 27.450 / 38.256 | 27.933 / 36.788 | 27.373 / 36.613 |

Shared query embedding latency was 22.368 ms p50 and 31.728 ms p95.

## Comparison

C is the strongest representation on the complete v2 query set. B improves
over A, but does not beat C on either conditional or final recall. The
representation ranking is an offline controlled experiment; it does not imply
a production embedding change by itself.

## Failure Analysis

| Representation | Gold post missing | Chunk retrieval miss with post present | Gold chunks hit@10 |
|---|---:|---:|---:|
| A_CONTENT_ONLY | 24 | 66 | 14 |
| B_TITLE_CONTENT | 24 | 61 | 19 |
| C_TITLE_TAGS_CONTENT | 24 | 56 | 24 |

Detailed per-query and per-gold-chunk ranks are in
[rag_chunk_representation_full_20260825.json](./rag_chunk_representation_full_20260825.json).

## Recommendation

Do not change production yet. B is not the winner on the complete dataset, so
there is no B-specific production proposal. C wins this offline comparison,
but the current production vector also includes description; proving a change
from the current production representation requires a separate controlled
benchmark. Any future proposal must preserve the current model, 384
dimensions, normalization contract, collection, chunk IDs, event guards, and
retrieval API, and would require separate approval and rebuild planning. None
was performed here.

## Verdict

`RAG_CHUNK_REPRESENTATION_EVALUATION_COMPLETE`

`OFFLINE_ONLY_NO_PRODUCTION_CHANGE`
