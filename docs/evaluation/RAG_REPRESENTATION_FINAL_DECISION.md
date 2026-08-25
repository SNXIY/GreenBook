# RAG_REPRESENTATION_FINAL_DECISION

Generated: 2026-08-25

## Comparison

Full `rag_evidence_v2` dataset, 50 queries, fixed Top10 post candidates, and
offline exact cosine ranking over 3399
canonical chunks.

| Representation | Conditional Recall@5 | Conditional Recall@10 | Final Evidence Recall@5 | Final Evidence Recall@10 | Rank p50/p95 ms |
|---|---:|---:|---:|---:|---:|
| A_CONTENT_ONLY | 0.098291 | 0.179487 | 0.085185 | 0.155556 | 5.155 / 8.383 |
| B_TITLE_CONTENT | 0.106838 | 0.256410 | 0.092593 | 0.196296 | 5.458 / 7.894 |
| C_TITLE_TAGS_CONTENT | 0.179487 | 0.269231 | 0.155556 | 0.233333 | 5.169 / 7.862 |
| D_TITLE_DESCRIPTION_CONTENT | 0.170940 | 0.264957 | 0.148148 | 0.229630 | 5.880 / 10.867 |

The requested D is `title + description + content`.

The code-observed production variant is also the strongest result in the
experiment:

| Production variant | Conditional Recall@5 | Conditional Recall@10 | Final Evidence Recall@5 | Final Evidence Recall@10 | Rank p50/p95 ms |
|---|---:|---:|---:|---:|---:|
| title + tags + description + content | 0.209402 | 0.307692 | 0.166667 | 0.251852 | 5.188 / 8.057 |

## Production Baseline

The task-defined production baseline is D. The source code audit found that
`PostChunk.textForEmbedding` actually builds `title + tags + description +
content`; this exact code-observed variant is reported as
`D_ACTUAL_CODE_TITLE_TAGS_DESCRIPTION_CONTENT` below the requested A/B/C/D table. It was also evaluated offline
without Qdrant access.

The code-observed production representation is the best of all tested
representations. Its Conditional Chunk Recall@10 and Final Evidence
Recall@10 also match the previously observed live baseline exactly.

Latency details:

- Shared query embedding: p50 29.824 ms, p95 32.24 ms.
- Requested D document embedding: p50 37.256 ms, p95 50.772 ms.
- Code-observed production document embedding: p50 33.278 ms, p95 48.6 ms.

Failure classification over 104 gold chunk references:

| Representation | Gold post missing | Chunk retrieval miss | Hit@10 |
|---|---:|---:|---:|
| A | 24 | 66 | 14 |
| B | 24 | 61 | 19 |
| C | 24 | 56 | 24 |
| Requested D | 24 | 57 | 23 |
| Code-observed production | 24 | 54 | 26 |

## Recommended Change

Do not modify production or rebuild vectors. The requested D is not the exact
current code contract and is slightly worse than C. The code-observed
production representation is already the best tested representation, so there
is no evidence-based reason to change the embedding input.

## Migration Cost

Any representation change would require recomputing every live chunk vector,
upserting a new embedding version, validating post/chunk IDs and event
versions, and a rollback/dual-version plan. No Qdrant update or rebuild was
performed here.

Detailed D failure cases are in
[rag_current_production_representation_audit_20260825.json](./rag_current_production_representation_audit_20260825.json).

## Verdict

`RAG_REPRESENTATION_FINAL_DECISION_COMPLETE`

`OFFLINE_ONLY_NO_PRODUCTION_CHANGE`

`CURRENT_PRODUCTION_REPRESENTATION_WINS`

`NO_REBUILD_REQUIRED`
