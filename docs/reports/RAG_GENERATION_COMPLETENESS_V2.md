# RAG_GENERATION_COMPLETENESS_V2

**Verdict:** `RAG_GENERATION_PROMPT_IMPROVEMENT_FOUND`  
**Reason:** Oracle-only variant COMPLETENESS_AWARE materially improved semantic claim coverage while meeting the offline faithfulness, citation, token, hallucination, and p95 latency guardrails.

## 1. V1 checkpoint and scope

- V1 checkpoint: `92f0923`; V1 verdict was `RAG_GENERATION_QUALITY_ISSUE`.
- Dataset: 50 rows; 45 answerable; 5 no-answer. Frozen snapshot drift: `0`.
- Reproducibility: dataset SHA256 `71ed1db0924f034f2a98cea641d15fa1e790d4d808999c9b551e4b31b5c6b1b5`, snapshot SHA256 `d646cc8af45dcaf37671a558af27272125399043bf08c207bc1ee1de860c6fcd`, snapshot digest `be65bfed2d90405472e0c19a6f472861ea23625b5372d4b7fa62b1df80c5de09`, V1 result SHA256 `10ce2c1a0809189eaad145658ae6f2f7e09d2f0000f60567c4a4003de5659587`.
- Production files changed: `0`; all semantic/judge/prompt work is evaluation-only.

## 2. Metric validation

V1 lexical metrics are retained. Semantic coverage is an auxiliary fixed-model claim audit; it is not used as the only verdict signal.
- No local embedding runtime was available; judge prompt/model/input/output are fixed and recorded as an auxiliary audit, not a sole decision maker.

- Audited claims: `45` across all answerable oracle cases.
- Detailed rule/semantic audit sample: `20` cases.
- Oracle lexical misses: `39`; production lexical misses: `45`.
- Oracle lexical false-negative rate among misses: `0.513`; production: `0.267`.
- Lexical false-negative rate over all claims: `0.444`.

### Coverage

| Scope | Lexical claim coverage | Semantic claim coverage |
|---|---:|---:|
| Production (V1) | 0.122 | 0.267 |
| Gold oracle | 0.189 | 0.556 |

### Missed-claim classification

| Category | Count | Rate of lexical misses |
|---|---:|---:|
| TRUE_MISSING | 19 | 0.487 |
| PARAPHRASE_FALSE_NEGATIVE | 20 | 0.513 |
| GOLD_OVER_SPECIFIED | 0 | 0.000 |
| EVIDENCE_NOT_SUPPORTING_GOLD | 0 | 0.000 |
| AMBIGUOUS | 0 | 0.000 |

### Production missed-claim classification

Production lexical misses: `45`; semantic false-negative rate among those misses: `0.267`.

| Category | Count | Rate of lexical misses |
|---|---:|---:|
| TRUE_MISSING | 33 | 0.733 |
| PARAPHRASE_FALSE_NEGATIVE | 12 | 0.267 |
| GOLD_OVER_SPECIFIED | 0 | 0.000 |
| EVIDENCE_NOT_SUPPORTING_GOLD | 0 | 0.000 |
| AMBIGUOUS | 0 | 0.000 |

The detailed audit records include query, gold answer/claims, supplied evidence, generated answer, matched/missed lexical terms, semantic classification, confidence, and rationale for 20 oracle cases and the corresponding 20 production cases.

## 3. Production generation chain

`community.answer_from_knowledge` -> `ctx.java.retrieve_knowledge_evidence (replaced by frozen Java boundary double in this evaluation)` -> evidence payload `{question, evidence[{chunkId, postId, title, content, startOffset, endOffset}]}` -> `greenbook_agent_core.llm_compat.structured_call` -> `community._validated_sources` -> response.
- Evidence budget: production handler defaults `{'top_chunks': 8, 'top_posts': 8}`; V1/V2 frozen evaluation used top_posts=`10`, top_chunks=`10`, max evidence=`10`.
- Evidence is passed in retrieval order; citation output is a global sources array with exact chunkId validation, not inline claim-position markers.
- Empty or insufficient evidence returns the exact sentinel with empty sources: `当前社区资料不足`.

## 4. Overall generation metrics

These metrics preserve V1 deterministic correctness/completeness and add the V2 oracle semantic claim coverage. Production and oracle use the same generator path; oracle replaces only the evidence input with gold evidence.

| Scope | Answer correctness | Lexical claim coverage | Semantic claim coverage | Faithfulness | Citation correctness | Citation completeness | Hallucination | Completeness |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Production evidence | 0.053 | 0.122 | 0.267 | 1.000 | 1.000 | 1.000 | 0.000 | 0.122 |
| Gold evidence oracle | 0.087 | 0.189 | 0.556 | 0.991 | 1.000 | 0.991 | 0.009 | 0.189 |

No-answer accuracy: production `5/5 = 1.000`; empty-context control `5/5 = 1.000`.
Production -> Gold Oracle delta: correctness `0.033791`, lexical completeness `0.067001`, semantic completeness `0.288889`, faithfulness `-0.009383`, completeness `0.067001`.

## 5. Retrieval-aware generation metrics

The following are inherited from the frozen V1 production-evidence run; V2 does not rerun or change retrieval.

| Evidence state | Cases | Answer correctness | Claim coverage | Faithfulness | Citation correctness | Citation completeness |
|---|---:|---:|---:|---:|---:|---:|
| GOLD_EVIDENCE_RETRIEVED | 5 | 0.054 | 0.134 | 1.000 | 1.000 | 1.000 |
| PARTIAL_EVIDENCE_RETRIEVED | 14 | 0.083 | 0.196 | 1.000 | 1.000 | 1.000 |
| REQUIRED_EVIDENCE_MISSING | 26 | 0.036 | 0.080 | 1.000 | 1.000 | 1.000 |

## 6. Oracle failure-family distribution

| Classification | Cases | Rate |
|---|---:|---:|
| Semantic generation-limited (semantic coverage < 0.50) | 20 | 0.444 |
| Semantic adequate | 25 | 0.556 |
| V1 lexical completeness failures | 39 | 0.867 |

## 7. Production prompt audit

- System prompt: `community._GROUNDED_ANSWER_PROMPT`, SHA256 `44472ddedbef44f1b27d2fed78cc0f78990d1053f59fa2c05ca50b844b0d07d4`.
- Evidence payload: `{question, evidence[{chunkId, postId, title, content, startOffset, endOffset}]}`; original evidence order preserved; no secondary summary.
- Structured call: temperature `0.0`, observed max tokens `8192`, response format `json_object`.
- Structured schema: `JSON object: answer string plus sources array of {postId,title,chunkId}; additional properties rejected; no answer length bound`.
- Stop/length: `no stop parameter observed; provider/structured_call completion`; answer length constraint: `none in production schema or handler`.
- No production instruction says concise/core-only, and no answer field max length exists. The hard constraints are evidence-only, insufficient sentinel, valid source IDs, and JSON shape.

## 8. Answer-length correlation (oracle)

- Pearson chars -> lexical: `0.601499`; tokens -> lexical: `0.659344`; sentences -> lexical: `0.506472`.
- Pearson chars -> semantic: `0.841788`; tokens -> semantic: `0.882621`; sentences -> semantic: `0.746619`.
- Spearman chars/tokens -> lexical: `0.747466` / `0.742302`.
- Spearman chars/tokens -> semantic: `0.840094` / `0.822266`.
- These are descriptive correlations, not causal proof. Per-query chars, tokens, sentence count, gold claim count, matched claim count, and both coverage values are in the JSON artifact.

## 9. Evidence utilization (oracle)

- Evidence support rate: `0.767`.
- Evidence utilization rate by generated answer: `0.600`.
- Claim utilization rate: `0.189`.
- Evidence available but underused: `14` cases.
- Evidence not supporting gold by coarse deterministic support check: `8` cases; this is separate from per-claim semantic category D.

## 10. Oracle-only prompt experiments

Experiment sample: `12` oracle cases; CURRENT is the saved V1 oracle output, not a new production call. B/C/D were evaluation-only calls.

| Variant | Lexical coverage | Semantic coverage | Faithfulness | Hallucination | Citation | Avg tokens | p95 tokens | p95 latency ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| COMPLETENESS_AWARE | 0.312 | 0.917 | 1.000 | 0.000 | 1.000 | 303.8 | 394.9 | 2759.3 |
| CURRENT | 0.183 | 0.583 | 1.000 | 0.000 | 1.000 | 175.2 | 348.5 | 2559.3 |
| EVIDENCE_ORDERING | 0.247 | 0.750 | 1.000 | 0.000 | 1.000 | 215.5 | 372.2 | 2937.4 |
| STRUCTURED_CLAIM_PLAN | 0.321 | 1.000 | 0.965 | 0.035 | 1.000 | 398.8 | 493.1 | 3478.5 |

- Semantic-best variant: `STRUCTURED_CLAIM_PLAN`; offline-gate selected variant: `COMPLETENESS_AWARE`; reported best: `COMPLETENESS_AWARE`.
- Prompt improvement gate: `True`.
- C is a single structured call with an additional supportingPoints field; it does not add a second Agent loop.

### Offline acceptance guardrails

A candidate needs semantic delta >= 0.10, faithfulness delta >= -0.05, hallucination delta <= 0.05, citation non-regression, p95 answer-token ratio <= 1.50, average answer-token ratio <= 2.00, and p95 generation-latency ratio <= 1.50.

| Variant | Semantic delta | Faithfulness delta | Hallucination delta | Avg token ratio | p95 token ratio | p95 latency ratio | Citation non-regression | Gate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| STRUCTURED_CLAIM_PLAN | 0.417 | -0.035 | 0.035 | 2.277 | 1.415 | 1.359 | True | False |
| COMPLETENESS_AWARE | 0.333 | 0.000 | 0.000 | 1.735 | 1.133 | 1.078 | True | True |
| EVIDENCE_ORDERING | 0.167 | 0.000 | 0.000 | 1.230 | 1.068 | 1.148 | True | True |

## 11. Baseline -> selected best impact

- Semantic coverage delta: `0.333334`.
- Lexical coverage delta: `0.12923`.
- Faithfulness delta: `0.0`; hallucination delta: `0.0`.
- Average token delta: `128.666666`; p95 token delta: `46.4`; p95 latency delta ms: `200.0309`.
- The semantic-highest C variant delta is `{'semantic_claim_coverage': 0.416667, 'lexical_claim_coverage': 0.137693, 'faithfulness': -0.034722, 'hallucination_rate': 0.034722, 'answer_tokens': 223.666666, 'p95_answer_tokens': 144.65, 'p95_generation_latency_ms': 919.2333}`; it did not pass the average-token guardrail, so it is not the selected best.

## 12. Latency and token baseline

- Production generation p50/p95: `2252.819` / `3901.7654 ms`.
- Production total estimated p50/p95: `2533.484` / `4224.0532 ms`.
- Production prompt tokens p50/p95: `1638.0` / `1811.8`; completion tokens p50/p95: `344.0` / `678.8`.
- Evidence context token estimate p50/p95: `620.25` / `929.35`.
- Retrieval timing is historical only in V1; it was not re-run in V2. Prompt-variant timing above is oracle-only and not production latency.

## 13. Independent retrieval vs generation conclusions

- RETRIEVAL_LIMITED: `0.889` (40/45 production answerable cases had partial/missing exact gold evidence).
- GENERATION_LIMITED: `0.444` (20/45 oracle cases had semantic coverage below `0.50`).
- METRIC_LIMITED: `0.513` among lexical misses in the semantic audit.
- These rates use different conditioning sets and are not additive: retrieval-limited is production evidence status, generation-limited is gold-evidence oracle output, and metric-limited is the lexical audit subset.

## 14. FIRST_BAD_STATE and protected boundaries

Production first-bad-state distribution from V1 remains:

| FIRST_BAD_STATE / failure family | Cases | Rate |
|---|---:|---:|
| CHUNK_RETRIEVAL_FAILURE | 34 | 0.756 |
| GENERATION_COMPLETENESS_FAILURE | 5 | 0.111 |
| POST_RETRIEVAL_FAILURE | 6 | 0.133 |

Oracle semantic diagnosis: GENERATION_COMPLETENESS_FAILURE `20` / `45`; semantically adequate `25` / `45`.
No V1 evidence-selection, context-construction, citation, or faithfulness first-bad-state was observed. V2 does not reinterpret retrieval failures as generator failures.

Protected production files changed: `0`.
- Dirty files: `apps/backend/scripts/evaluate_rag_generation_completeness_v2.py, docs/evaluation/rag_generation_completeness_v2_results.json, docs/reports/RAG_GENERATION_COMPLETENESS_V2.md`.
- Evaluation script: `apps\backend\scripts\evaluate_rag_generation_completeness_v2.py`.
- Results: `docs\evaluation\rag_generation_completeness_v2_results.json`.
- Report: `docs\reports\RAG_GENERATION_COMPLETENESS_V2.md`.

## 15. Recommendation

Keep production unchanged. COMPLETENESS_AWARE is an oracle-only candidate that passes the stated offline guardrails on the 12-case experiment sample; STRUCTURED_CLAIM_PLAN has the highest semantic score but fails the average-token guardrail. Preserve both as evaluation artifacts and require explicit approval plus a larger fixed/manual audit before any production prompt change.
Retrieval remains the earliest production limitation at 40/45 cases. Do not use the prompt experiment to justify retrieval, chunk, embedding, runtime, or MCP changes.

No production prompt, generator, retrieval, chunking, embedding, or runtime change was made.
