# RAG_CHUNK_RETRIEVAL_DIAGNOSIS

Checkpoint: `df09dc8bc30a8da4ec1bd5d71d4a13fc035056b6`
Dataset: `rag_evidence_v2` — 50 queries, 45 answerable, 104 gold chunk references.

## Scope and exact FIRST_BAD_STATE

The post candidate list is frozen at Top10 from the completed RAG Dataset V2
diagnosis. Hybrid Search is not rerun or widened. The current production
representation, embedding model, RRF, evidence selector, generator, Java
business truth, and collection are unchanged.

Within the fixed Top10 post candidate set, the exact FIRST_BAD_STATE is:

`POST_RETRIEVAL → CHUNK_RETRIEVAL (ranked selected chunk set)`

Gold posts absent from Top10 are reported separately as
`POST_CANDIDATE_FAILURE`; they are not charged to the isolated chunk retriever.
The dataset validator reported no annotation issue.

## Baseline metrics

| Metric | Current production Top10 |
|---|---:|
| Conditional Chunk Recall@5 | 0.209402 |
| Conditional Chunk Recall@10 | 0.307692 |
| Final Evidence Recall@5 | 0.166667 |
| Final Evidence Recall@10 | 0.251852 |
| Gold Chunk MRR (overall) | 0.141755 |
| Gold Chunk MRR (post present) | 0.163563 |

## Failure-family distribution

The denominator for the share column is the 78
gold chunk references missed by the current Top10 chunk result. The second
share column uses all 104 answerable
gold references.

| Failure family | Count | Share of misses | Share of all gold |
|---|---:|---:|---:|
| `CHUNK_BOUNDARY_FAILURE` | 6 | 0.0769 | 0.0577 |
| `QUERY_CHUNK_MISMATCH` | 0 | 0.0000 | 0.0000 |
| `PARENT_CONTEXT_LOSS` | 0 | 0.0000 | 0.0000 |
| `LOCAL_RANKING_FAILURE` | 48 | 0.6154 | 0.4615 |
| `POST_CANDIDATE_FAILURE` | 24 | 0.3077 | 0.2308 |
| `ANNOTATION_ISSUE` | 0 | 0.0000 | 0.0000 |

`PARENT_CONTEXT_LOSS` is assigned only when the controlled parent-aware
offline strategy recovers a baseline miss into Top10. `CHUNK_BOUNDARY_FAILURE`
requires a same-post baseline hit immediately adjacent to the gold index.
`LOCAL_RANKING_FAILURE` means the gold chunk is present in the full fixed-post
Qdrant candidate pool but below the current Top10. `QUERY_CHUNK_MISMATCH` is a
remaining semantic/representation mismatch signal, not a dataset claim.
`ANNOTATION_ISSUE` is zero because the frozen dataset validator reports complete
fixture coverage. Zero-valued families remain in the table so the distribution
is explicit rather than inferred from omitted rows.

## Complete answerable-query trace

Each of the 45 answerable queries has candidate post ranks, Top10 chunk IDs,
scores, chunk ranks, gold ranks, text summaries, hit/miss state, and the
primary failure family in:

[rag_chunk_retrieval_v3_diagnosis.json](../evaluation/rag_chunk_retrieval_v3_diagnosis.json)

## Evidence conclusion

Selection loss remains zero in the frozen benchmark path. The diagnosis is
therefore retrieval-only: candidate post misses account for the post-stage
portion, while the conditional deficit is caused by chunk candidate/ranking
quality inside already selected posts. No production change is authorized by
this diagnosis document alone.

## Verdict

`RAG_CHUNK_RETRIEVAL_DIAGNOSIS_COMPLETE`
