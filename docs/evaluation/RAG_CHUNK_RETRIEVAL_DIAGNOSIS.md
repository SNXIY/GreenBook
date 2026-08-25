# RAG_CHUNK_RETRIEVAL_DIAGNOSIS

Generated: 2026-08-25

## Candidate Depth Analysis

The frozen dataset v2 was used without modification. Chunk retrieval used a
fixed Qdrant `post_chunks_multilingual_v1` limit of 10. Top50 and
Top100 use paginated Search candidates because the production RAG request
contract caps `topPosts` at 20; no production limit was changed.

| Candidate depth | Post Recall | Conditional Chunk Recall | Raw Chunk Recall@10 | Final Evidence Recall@10 |
|---|---:|---:|---:|---:|
| Top10 | 0.744444 | 0.307692 | 0.251852 | 0.251852 |
| Top20 | 0.900000 | 0.248062 | 0.237037 | 0.237037 |
| Top50 | 1.000000 | 0.237037 | 0.237037 | 0.237037 |
| Top100 | 1.000000 | 0.214815 | 0.214815 | 0.214815 |

For Top10 and Top20, the live domain evidence endpoint was used to verify the
final-evidence path. For Top50 and Top100, the deterministic no-loss path is
calculated from the same read-only Qdrant hits. Selection loss remains zero in
the verified Top10 path.

## Failure Classification

- Gold post missing: 14
- Gold post found but chunk missing: 23
  - Chunk split issue: 17
  - Chunk embedding miss: 6
- Failure cases exported: 20

The 20 exported cases include query, gold post/chunk, candidate posts,
candidate chunks, scores, and the classification. They are in the JSON
artifact next to this report.

## Embedding Representation Analysis

The offline A/B/C comparison is scoped to the exported failure-case candidate
sets and is intentionally separate from production projection. No model,
chunking, Qdrant, or production embedding code is changed.

Scope: 20 exported failure queries, 83 Top10 candidate posts, and 1,817
canonical candidate chunks. Ranking is exact cosine over the fixed candidate
post chunk pool. Sixteen queries had at least one gold post in the Top10 pool
and were included in conditional chunk recall.

| Representation | Input | Conditional Chunk Recall@10 |
|---|---|---:|
| A | content | 0.031250 |
| B | title + content | 0.177083 |
| C | title + tags + content | 0.062500 |
| C_PRODUCTION | current title + tags + description + content vector | 0.072917 |

B is the strongest of the three offline representations on this restricted
failure-case contrast set, but the absolute recall remains low and the audit
does not prove a production change. It is not a full corpus benchmark and no
embedding was written to Qdrant. The detailed per-query ranks and Top10 lists
are in [rag_chunk_representation_audit_20260825.json](./rag_chunk_representation_audit_20260825.json).

## First Bad State

The first confirmed loss remains the transition from post candidates to
candidate-scoped chunk retrieval. Evidence selection introduces no measured
loss in the verified path.

## Minimal Fix

No implementation fix is authorized by this diagnosis. Increasing candidate
depth is not a fix: Top20 reaches 0.900000 Post Recall, but Chunk Recall@10
falls to 0.248062; Top50/Top100 reach complete post recall while Chunk Recall
falls further. The dominant observed failure label is a same-post wrong-chunk
or split signal, not evidence that the chunking strategy itself should change.
Hybrid Search, embedding model, chunking, evidence ranking, generation, and
Agent code were not modified.

## Verdict

`RAG_CHUNK_RETRIEVAL_DIAGNOSIS_COMPLETE`

`FIRST_BAD_STATE=POST_RETRIEVAL_TO_CHUNK_RETRIEVAL`

`CANDIDATE_CUTOFF_NOT_CONFIRMED`
