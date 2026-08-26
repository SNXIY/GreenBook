# RAG_GENERATION_COMPLETENESS_V3_PRODUCTION_AB

**Verdict:** `RAG_COMPLETENESS_PROMPT_NO_PRODUCTION_GAIN`  
**Reason:** The candidate did not satisfy every production-evidence A/B acceptance gate; production prompt remains unchanged.

## 1. V2 checkpoint and frozen scope

- V2 checkpoint: `c8a5726`; V2 verdict: `RAG_GENERATION_PROMPT_IMPROVEMENT_FOUND`.
- Dataset: `50` rows (`45` answerable, `5` no-answer).
- Dataset SHA256: `71ed1db0924f034f2a98cea641d15fa1e790d4d808999c9b551e4b31b5c6b1b5`; snapshot SHA256: `d646cc8af45dcaf37671a558af27272125399043bf08c207bc1ee1de860c6fcd`; snapshot digest: `be65bfed2d90405472e0c19a6f472861ea23625b5372d4b7fa62b1df80c5de09`.
- V2 SHA matches: dataset `True`, snapshot `True`, V1 results `True`.
- Frozen snapshot drift: `0`; live retrieval calls: `0`.
- Input A/B contract valid: `True`; all input fingerprints and evidence IDs are recorded in JSON.
- The 45 answerable contexts came from the frozen production Top10 chunk order. The five no-answer contexts reuse the captured V1 production evidence, exactly as the V1 frozen protocol requires.

## 2. Actual production generation chain

`community.answer_from_knowledge` -> `ctx.java.retrieve_knowledge_evidence` -> evidence payload -> `structured_call` -> `_grounded_payload` -> `_validated_sources` -> response.
- Current prompt SHA256: `44472ddedbef44f1b27d2fed78cc0f78990d1053f59fa2c05ca50b844b0d07d4`.
- Candidate prompt SHA256: `33f3b82ceb1233a02ad6f735f7c8a18587ecfe7dac19ddff32411101a96527bf`.
- Model: `deepseek-v4-flash`; temperature observed: `0.0`; max tokens: `8192`; response format: `json_object`.
- Evidence maximum: `10`; ordering and citation rewrite unchanged.
- A/B changed only this instruction: `- Within the supplied evidence, cover all key facts needed to answer the current question. Do not omit important supported facts merely to be concise. Do not add or guess facts unsupported by the evidence.`

## 3. CURRENT vs COMPLETENESS_AWARE overall metrics

All answerable metrics below use the same frozen production evidence; semantic coverage is an auxiliary judge metric and lexical/deterministic metrics remain visible.

| Variant | Semantic claim coverage | Lexical claim coverage | Correctness | Faithfulness | Hallucination | Citation correctness | Citation completeness | Completeness |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT | 0.333 | 0.127 | 0.055 | 1.000 | 0.000 | 1.000 | 1.000 | 0.127 |
| COMPLETENESS_AWARE | 0.378 | 0.144 | 0.055 | 0.993 | 0.007 | 1.000 | 0.993 | 0.144 |
| Delta (candidate - current) | 0.044 | 0.018 | -0.000 | -0.007 | 0.007 | 0.000 | -0.007 | 0.018 |

V2 production semantic reference: `0.267`; V2 production lexical false-negative rate: `0.267`. Both are retained as non-exclusive diagnostic references, not as the sole gate signal.

## 4. Retrieval-aware generation metrics

| Evidence stratum | N | CURRENT semantic | AWARE semantic | CURRENT correctness | AWARE correctness | CURRENT faithfulness | AWARE faithfulness |
|---|---:|---:|---:|---:|---:|---:|---:|
| GOLD_EVIDENCE_RETRIEVED | 5 | 0.800 | 1.000 | 0.055 | 0.057 | 1.000 | 0.967 |
| PARTIAL_EVIDENCE_RETRIEVED | 14 | 0.500 | 0.500 | 0.089 | 0.082 | 1.000 | 1.000 |
| REQUIRED_EVIDENCE_MISSING | 26 | 0.154 | 0.192 | 0.037 | 0.039 | 1.000 | 0.994 |

- Frozen production retrieval-limited: `40/45` = `0.889`.
- V2 gold-oracle generation-limited reference: `20/45` = `0.444`; this is not charged to production retrieval.
- These diagnostic rates are intentionally not treated as a mutually exclusive partition: retrieval absence, generator omission, and metric false negatives can overlap at different observations.

## 5. No-answer and missing-evidence safety

- CURRENT no-answer accuracy: `5/5` = `1.000`.
- COMPLETENESS_AWARE no-answer accuracy: `5/5` = `1.000`.

| Variant | Missing-evidence cases | Unsupported expansion cases | Unsupported factual claims | Faithfulness failures | Invalid/fake citation cases | Safe refusal cases |
|---|---:|---:|---:|---:|---:|---:|
| CURRENT | 26 | 0 | 0 | 0 | 0 | 12 |
| COMPLETENESS_AWARE | 26 | 0 | 0 | 0 | 0 | 11 |

The critical safety condition is zero candidate unsupported factual expansion in `REQUIRED_EVIDENCE_MISSING`; list headings and evidence-qualified limitations remain visible in raw diagnostics but are not factual hallucinations. Evidence insufficiency must not trigger model-knowledge completion.

## 6. Evidence utilization

| Variant | Evidence support rate | Evidence utilization rate | Lexical claim utilization | Semantic claim utilization | Available-but-underused cases |
|---|---:|---:|---:|---:|---:|
| CURRENT | 0.598 | 0.602 | 0.127 | 0.333 | 15 |
| COMPLETENESS_AWARE | 0.598 | 0.671 | 0.144 | 0.378 | 12 |

## 7. Semantic/rule audit (20 cases)

- Method: `stratified semantic/rule audit; case artifacts are retained for manual review`.
- Classification counts: `{'CLEAR_IMPROVEMENT': 2, 'NO_MEANINGFUL_CHANGE': 16, 'OVER_VERBOSE': 2, 'UNSUPPORTED_EXPANSION': 0, 'REGRESSION': 0}`.
- The JSON artifact retains query, gold answer/claims, full provided evidence, both answers, lexical support diagnostics, semantic claim decisions, and the classification for each selected case.

| Query | Stratum | Classification | CURRENT semantic | AWARE semantic | CURRENT tokens | AWARE tokens |
|---|---|---|---:|---:|---:|---:|
| rag-019 | GOLD_EVIDENCE_RETRIEVED | CLEAR_IMPROVEMENT | 0.000 | 1.000 | 15 | 442 |
| rag-014 | GOLD_EVIDENCE_RETRIEVED | NO_MEANINGFUL_CHANGE | 1.000 | 1.000 | 222 | 328 |
| rag-018 | GOLD_EVIDENCE_RETRIEVED | NO_MEANINGFUL_CHANGE | 1.000 | 1.000 | 351 | 534 |
| rag-034 | GOLD_EVIDENCE_RETRIEVED | NO_MEANINGFUL_CHANGE | 1.000 | 1.000 | 524 | 553 |
| rag-008 | GOLD_EVIDENCE_RETRIEVED | NO_MEANINGFUL_CHANGE | 1.000 | 1.000 | 613 | 669 |
| rag-026 | PARTIAL_EVIDENCE_RETRIEVED | OVER_VERBOSE | 0.000 | 0.000 | 15 | 549 |
| rag-006 | PARTIAL_EVIDENCE_RETRIEVED | OVER_VERBOSE | 1.000 | 1.000 | 122 | 295 |
| rag-015 | PARTIAL_EVIDENCE_RETRIEVED | NO_MEANINGFUL_CHANGE | 1.000 | 1.000 | 370 | 499 |
| rag-038 | PARTIAL_EVIDENCE_RETRIEVED | NO_MEANINGFUL_CHANGE | 0.000 | 0.000 | 622 | 387 |
| rag-031 | PARTIAL_EVIDENCE_RETRIEVED | NO_MEANINGFUL_CHANGE | 0.000 | 0.000 | 808 | 890 |
| rag-001 | REQUIRED_EVIDENCE_MISSING | CLEAR_IMPROVEMENT | 0.000 | 1.000 | 15 | 416 |
| rag-024 | REQUIRED_EVIDENCE_MISSING | NO_MEANINGFUL_CHANGE | 0.000 | 0.000 | 15 | 15 |
| rag-041 | REQUIRED_EVIDENCE_MISSING | NO_MEANINGFUL_CHANGE | 0.000 | 0.000 | 202 | 271 |
| rag-043 | REQUIRED_EVIDENCE_MISSING | NO_MEANINGFUL_CHANGE | 1.000 | 1.000 | 493 | 737 |
| rag-023 | REQUIRED_EVIDENCE_MISSING | NO_MEANINGFUL_CHANGE | 0.000 | 0.000 | 836 | 829 |
| rag-002 | REQUIRED_EVIDENCE_MISSING | NO_MEANINGFUL_CHANGE | 0.000 | 0.000 | 467 | 747 |
| rag-003 | REQUIRED_EVIDENCE_MISSING | NO_MEANINGFUL_CHANGE | 0.000 | 0.000 | 595 | 758 |
| rag-004 | REQUIRED_EVIDENCE_MISSING | NO_MEANINGFUL_CHANGE | 1.000 | 1.000 | 575 | 633 |
| rag-005 | REQUIRED_EVIDENCE_MISSING | NO_MEANINGFUL_CHANGE | 0.000 | 0.000 | 15 | 13 |
| rag-007 | REQUIRED_EVIDENCE_MISSING | NO_MEANINGFUL_CHANGE | 0.000 | 0.000 | 356 | 303 |

## 8. Latency and token A/B

| Metric | CURRENT p50 | CURRENT p95 | AWARE p50 | AWARE p95 | Delta/ratio |
|---|---:|---:|---:|---:|---:|
| Generation latency (ms) | 2657.050 | 4807.283 | 2949.842 | 4814.347 | 7.064 ms |
| Total RAG latency (ms, historical retrieval + generation) | 2943.161 | 5079.393 | 3244.600 | 5086.023 | 6.630 ms |
| Input prompt tokens | 1638.000 | 1811.800 | 1677.000 | 1850.800 | ratio 1.024 |
| Output tokens | 351.000 | 769.200 | 499.000 | 825.800 | ratio 1.268 |
- V1 reference generation p50/p95 was 2253/3902 ms; V3 records fresh provider observations in the JSON artifact and does not optimize them in this phase.

## 9. FIRST_BAD_STATE and failure families

| FIRST_BAD_STATE / family | CURRENT count | AWARE count |
|---|---:|---:|
| CHUNK_RETRIEVAL_FAILURE | 34 | 34 |
| CITATION_FAILURE | 0 | 0 |
| CONTEXT_CONSTRUCTION_FAILURE | 0 | 0 |
| DATASET_ISSUE | 0 | 0 |
| EVIDENCE_SELECTION_FAILURE | 0 | 0 |
| GENERATION_COMPLETENESS_FAILURE | 5 | 5 |
| GENERATION_FAITHFULNESS_FAILURE | 0 | 0 |
| NO_ANSWER_FAILURE | 0 | 0 |
| POST_RETRIEVAL_FAILURE | 6 | 6 |

The protected ordering remains `POST_RETRIEVAL -> CHUNK_RETRIEVAL` for cases where required evidence is absent. Only after evidence reaches generation can a completeness or citation failure be charged to the generator boundary.

## 10. Acceptance gate

- Gate passes: `False`.
- `production_semantic_gain`: `False`.
- `gold_evidence_stable_gain`: `True`.
- `partial_evidence_stable_gain`: `False`.
- `faithfulness_non_regression`: `True`.
- `hallucination_guard`: `True`.
- `citation_correctness_non_regression`: `True`.
- `citation_completeness_non_regression`: `True`.
- `no_answer_current_5_of_5`: `True`.
- `no_answer_candidate_5_of_5`: `True`.
- `missing_evidence_no_unsupported_expansion`: `True`.
- `p95_generation_latency_acceptable`: `True`.
- `p95_total_rag_latency_acceptable`: `True`.
- `average_output_tokens_acceptable`: `True`.
- `p95_output_tokens_acceptable`: `True`.
- `audit_has_no_unsafe_expansion`: `True`.
- `audit_improvements_not_outnumbered_by_regressions`: `True`.

## 11. Production boundary and final state

- Production files changed at evaluation start: `0`.
- Production files changed after optional application: `[]`.
- Prompt applied: `False`.
- Focused regression status: `not run; candidate not applied`.
- Dirty files: `apps/backend/scripts/evaluate_rag_generation_completeness_v3.py, docs/evaluation/rag_generation_completeness_v3_results.json, docs/reports/RAG_GENERATION_COMPLETENESS_V3_REPORT.md`.
- Retrieval, chunking, embedding, Qdrant, hybrid search, evidence selection, schema, model configuration, Memory, Runtime, MCP, and Java were not modified.

## 12. Verdict and recommendation

`RAG_COMPLETENESS_PROMPT_NO_PRODUCTION_GAIN` — The candidate did not satisfy every production-evidence A/B acceptance gate; production prompt remains unchanged.

Production remains unchanged because the V3 candidate prompt was not applied. Stop prompt experiments unless a new acceptance decision is requested.

Remaining accepted limitations: post/chunk retrieval quality remains the dominant production limitation; semantic judge results are auxiliary and provider-dependent; latency uses frozen/historical retrieval observations and is a baseline, not an optimization result.
