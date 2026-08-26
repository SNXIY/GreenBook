# RAG Final Status

**Status:** `RAG_CURRENT_LIMIT_ACCEPTED`  
**RAG generation checkpoint:** `RAG_COMPLETENESS_PROMPT_NO_PRODUCTION_GAIN`  
**Semantic ranking checkpoint:** `RAG_SEMANTIC_RANKING_NO_GAIN`

This is the final RAG diagnostic checkpoint for the current branch. It consolidates the existing frozen evaluations; no new RAG experiment was run for this report.

## 1. Canonical production architecture

```text
Hybrid Search
  -> Post Retrieval (Top10)
  -> Chunk Retrieval
  -> Evidence Selection
  -> Grounded Generation
  -> Citation / No-answer
```

The RAG path is a conditional grounded capability over community evidence. It is not a mandatory default path for every knowledge question.

## 2. Final production retrieval baseline

| Metric | Baseline |
|---|---:|
| Post Recall@10 | 0.744444 |
| Conditional Chunk Recall@10 | 0.307692 |
| Final Evidence Recall@10 | 0.251852 |

The exact earliest observed retrieval boundary remains:

```text
POST_RETRIEVAL -> CHUNK_RETRIEVAL
```

Increasing post candidate depth added noise without recovering the required evidence reliably. The V7 frozen ranking diagnosis also found that the best lightweight reranker remained below the acceptance gate.

## 3. Final production generation baseline

| Metric | Baseline |
|---|---:|
| Semantic claim coverage | approximately 0.333 |
| Faithfulness | approximately 1.000 |
| Citation correctness | approximately 1.000 |
| No-answer accuracy | 5/5 |
| Hallucination | approximately 0 |

The V3 production-evidence A/B kept the current prompt in production. `COMPLETENESS_AWARE` improved semantic coverage by only `+0.044` on the frozen production evidence, did not improve the partial-evidence stratum, and therefore did not satisfy the production acceptance gate. Production prompt behavior remains unchanged.

## 4. FIRST_BAD_STATE distribution

| FIRST_BAD_STATE | Cases |
|---|---:|
| `POST_RETRIEVAL_FAILURE` | 6 |
| `CHUNK_RETRIEVAL_FAILURE` | 34 |
| `GENERATION_COMPLETENESS_FAILURE` | 5 |
| `EVIDENCE_SELECTION_FAILURE` | 0 |
| `CONTEXT_CONSTRUCTION_FAILURE` | 0 |
| `GENERATION_FAITHFULNESS_FAILURE` | 0 |
| `CITATION_FAILURE` | 0 |
| `NO_ANSWER_FAILURE` | 0 |

The counts are for the 45 answerable frozen evaluation queries. Retrieval failures are the dominant production limitation; the five generation completeness failures are observed only after evidence reaches the generation boundary.

## 5. Rejected approaches

The following approaches were evaluated offline or diagnostically and are not part of production:

| Approach | Decision | Reason |
|---|---|---|
| Post TopK expansion | Reject | Recall gains did not justify the additional candidate noise; deeper TopK reduced conditional chunk quality. |
| Boundary/parent simple ranking | Reject | Improvement was negligible and did not clear the gate. |
| Hard micro-chunk production filter | Reject | It improved the frozen dataset but could remove genuinely useful short evidence in production. |
| Soft length penalty | Reject | Did not reach the required gain. |
| Semantic chunk merge v2 | Reject | Improved some diagnostics but missed the acceptance gate and increased duplicate slot waste. |
| Lightweight reranker | Reject | Best `TOP50` result was Conditional R@10 `0.346154`, Strong Conditional R@10 `0.380208`, Final Evidence Recall@10 `0.274074`; below gate. |
| Completeness-aware production prompt | Reject | V3 production A/B gain was not stable across evidence strata and did not meet the production gate; prompt was not applied. |

These decisions preserve the current single retrieval and generation path without introducing a second runtime, planner, retriever, or RAG service.

## 6. Frozen evaluation checkpoints

| Checkpoint | Evidence |
|---|---|
| V2 generation completeness | `c8a5726` |
| V3 generation completeness evaluation | `cfed130` |
| Frozen Dataset V2 | 50 queries: 45 answerable, 5 no-answer |
| Frozen retrieval snapshot | drift `0`; no live retrieval was used by the V3 A/B harness |

The V3 artifact records the dataset SHA256, snapshot SHA256/digest, prompt fingerprints, A/B input contract, and per-case evidence IDs. The V7 and V3 artifacts remain the source of detailed traces and case-level diagnostics.

## 7. Final interpretation

The current limitation is accepted as a retrieval-quality boundary rather than a reason to expand the architecture. The production RAG capability remains useful when the required community evidence is retrieved and can produce grounded, correctly cited answers, including safe no-answer behavior. It is not currently reliable enough to be treated as universal knowledge coverage.

The next workstream may establish the Agent performance baseline, but must treat the RAG numbers above as fixed input. No further RAG ranking, chunk, embedding, prompt, or retrieval tuning is justified in this checkpoint.

## 8. Production boundary and files

- Production files changed by this finalization: `0`.
- Retriever, PostChunker, embedding model, Qdrant collection, Hybrid Search, EvidenceSelector, Generator, Memory, ActionLoop, Durable Runtime, MCP, and Java were not modified.
- This report is documentation only.

## 9. Final verdict

`RAG_CURRENT_LIMIT_ACCEPTED`

