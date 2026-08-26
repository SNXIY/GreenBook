# RAG_GENERATION_EVALUATION_V1

**Verdict:** `RAG_GENERATION_QUALITY_ISSUE`  
**Reason:** The same generator remains weak even when gold evidence is supplied.

## 1. Checkpoint and scope

- V7 checkpoint commit: `19f6f8e`.
- V7 verdict: `RAG_SEMANTIC_RANKING_NO_GAIN`; production retrieval unchanged.
- Frozen snapshot: `docs\evaluation\rag_retrieval_frozen_snapshot_v1.json`; digest `be65bfed2d90405472e0c19a6f472861ea23625b5372d4b7fa62b1df80c5de09`.
- Snapshot file SHA matches V7: `True`; drift: `0`.
- Evaluation is evidence-only. Java, MySQL, Qdrant, Hybrid Search, and production retrieval were not called.

## 2. Real production generation chain

`community.answer_from_knowledge` uses the canonical path:

`community.answer_from_knowledge` → `ctx.java.retrieve_knowledge_evidence` → evidence payload → `structured_call` → `_grounded_payload` → `_validated_sources` → response.

- Evidence is passed in returned order as user JSON `{question, evidence[{chunkId, postId, title, content, startOffset, endOffset}]}`; the evaluator did not reorder or truncate beyond the canonical Top10 input.
- Production function/schema defaults are `top_posts=8`, `top_chunks=8`. To reproduce the frozen canonical Top10 baseline, this evaluation explicitly passed `top_posts=10`, `top_chunks=10`; max evidence in this evaluation: `10`.
- System prompt constant: `community._GROUNDED_ANSWER_PROMPT`, SHA256 `44472ddedbef44f1b27d2fed78cc0f78990d1053f59fa2c05ca50b844b0d07d4`; temperature `0.0`; model `deepseek-v4-flash`.
- Citation mapping: exact supplied `chunkId` lookup, deduplication, and canonical `postId/title`; inline claim-position citations are not generated (`global sources array only; no claim-position inline marker`).
- Insufficient evidence: empty Java evidence returns `exact community._INSUFFICIENT_EVIDENCE and empty sources` without an LLM call; malformed output or no valid sources fails closed to the same sentinel.

## 3. Dataset and frozen inputs

- Dataset: `docs\evaluation\rag_evidence_dataset_v2.jsonl`; rows `50`; answerable `45`; no-answer `5`.
- Gold references: `104`; unique gold chunks `75`; annotation status remains human-audited.
- Answerable production evidence: frozen Top10 `candidate_chunks[:10]` from the V1 snapshot.
- No-answer production evidence: existing `docs\evaluation\rag_evidence_runs_20260825.jsonl` only; it was not used to alter the frozen answerable snapshot and is called out as a historical captured input.
- Oracle: exact gold chunks from Dataset V2, same production function/prompt/model; diagnostic only.

## 4. Deterministic metric definitions

No LLM judge was used. Correctness/completeness use normalized lexical term and claim coverage; faithfulness/hallucination use sentence-level overlap with supplied evidence; citation checks use exact IDs and canonical metadata. These are deterministic audit proxies, not semantic proof of equivalence.

- Answer correctness: F1 of gold-answer terms and generated-answer terms.
- Gold claim coverage: lexical term recall for each human-audited evidence claim, averaged per query.
- Answer completeness: gold-answer term recall.
- Faithfulness: factual answer claims supported by supplied evidence terms.
- Hallucination rate: unsupported factual claims / factual claims.
- Citation correctness: returned source IDs exist in evidence and post/title match the canonical evidence row.
- Citation completeness: generated claims supported by the cited evidence subset.

## 5. Overall answerable metrics

| Run | N | Correctness | Gold claim coverage | Faithfulness | Citation correctness | Citation completeness | Hallucination | Completeness |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| PRODUCTION_EVIDENCE | 45 | 0.053 | 0.122 | 1.000 | 1.000 | 1.000 | 0.000 | 0.122 |
| GOLD_EVIDENCE_ORACLE | 45 | 0.087 | 0.189 | 0.991 | 1.000 | 0.991 | 0.009 | 0.189 |

## 6. Retrieval-aware generation metrics

Exact gold chunk presence is intentionally conservative: equivalent paraphrase evidence is not silently counted as gold retrieved.

| Evidence stratum | N | Correctness | Gold claim coverage | Faithfulness | Citation completeness | Hallucination | Completeness |
|---|---:|---:|---:|---:|---:|---:|---:|
| GOLD_EVIDENCE_RETRIEVED | 5 | 0.054 | 0.134 | 1.000 | 1.000 | 0.000 | 0.134 |
| PARTIAL_EVIDENCE_RETRIEVED | 14 | 0.083 | 0.196 | 1.000 | 1.000 | 0.000 | 0.196 |
| REQUIRED_EVIDENCE_MISSING | 26 | 0.036 | 0.080 | 1.000 | 1.000 | 0.000 | 0.080 |

## 7. No-answer behavior

- Production no-answer accuracy: `1.000` (5/5).
- Canonical sentinel: `当前社区资料不足`; correct means exact sentinel and empty sources.
- The five captured no-answer contexts contained retrieved chunks, so they exercised the actual generation refusal rule. Empty-context control follows the production short-circuit and is recorded separately in JSON.

## 8. Production versus gold-evidence oracle

Delta is `oracle - production`; positive correctness/completeness/faithfulness is an oracle improvement, while positive hallucination is worse.

| Correctness Δ | Gold claim coverage Δ | Faithfulness Δ | Citation correctness Δ | Citation completeness Δ | Hallucination Δ | Completeness Δ |
|---:|---:|---:|---:|---:|---:|---:|
| 0.034 | 0.067 | -0.009 | 0.000 | -0.009 | 0.009 | 0.067 |

## 9. Failure families and FIRST_BAD_STATE

Failures are classified at the earliest observed boundary, preserving retrieval failures instead of charging them to generation.

### FIRST_BAD_STATE distribution

| FIRST_BAD_STATE / failure family | Count | Rate of 45 answerable cases |
|---|---:|---:|
| CHUNK_RETRIEVAL_FAILURE | 34 | 0.756 |
| POST_RETRIEVAL_FAILURE | 6 | 0.133 |
| GENERATION_COMPLETENESS_FAILURE | 5 | 0.111 |
| CITATION_FAILURE | 0 | 0.000 |
| CONTEXT_CONSTRUCTION_FAILURE | 0 | 0.000 |
| DATASET_ISSUE | 0 | 0.000 |
| EVIDENCE_SELECTION_FAILURE | 0 | 0.000 |
| GENERATION_FAITHFULNESS_FAILURE | 0 | 0.000 |
| NO_ANSWER_FAILURE | 0 | 0.000 |

### Representative failed cases

| Query | Stratum | FIRST_BAD_STATE | Correctness | Faithfulness | Completeness | Answer |
|---|---|---|---:|---:|---:|---|
| rag-001 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.000 | 1.000 | 0.000 | 当前社区资料不足 |
| rag-002 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.008 | 1.000 | 0.045 | 根据社区资料，Java 后端稳定性实践主要包括： 1. **架构设计**：多副本部署（至少两个实例，负载均衡）、多机房部署（关键服务至少两个机房，DNS 或负载均衡切换）、多活或主备（数据库和缓存跨机 |
| rag-003 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.064 | 1.000 | 0.122 | 根据社区资料，Java 后端稳定性需要从代码、架构、运维监控等多个维度系统性建设。具体实践包括：从可观测性入手，做好防御性编程和并发控制，设计冗余和异步化架构，并通过演练和预案提升应急能力；同时建议团 |
| rag-004 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.107 | 1.000 | 0.232 | 从 Java 入门到生产实践需要掌握以下可靠性要点： 1. **异常处理**：通过 try-catch-finally 块捕获和处理运行时错误，保证程序健壮性。 2. **JVM 调优**：掌握内存分 |
| rag-006 | PARTIAL_EVIDENCE_RETRIEVED | CHUNK_RETRIEVAL_FAILURE | 0.301 | 1.000 | 0.489 | Java 稳定性验证通常包括压力测试、故障注入、资源监控、代码质量等多个方面。具体来说，需要从压力测试、故障注入、资源监控、代码质量等方面入手，通过制定合理的验证计划，执行严格的测试，并持续优化，确保 |
| rag-007 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.073 | 1.000 | 0.206 | 根据社区资料，Java 后端架构设计支持可靠性主要通过以下方式： 1. **多机房部署**：实现异地多活，至少做到同城双活，保证机房级故障时业务不中断。 2. **数据备份**：定期全量备份 + 实时 |
| rag-009 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.012 | 1.000 | 0.059 | 根据社区资料，Java 服务处理异常、重试与稳定性的实践包括： - **异常处理**：通过熔断、降级、限流等手段防止局部故障扩散（chunkId: 13f82ea2-5860-3d3f-b3fa-b3 |
| rag-012 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.000 | 1.000 | 0.000 | 当前社区资料不足 |
| rag-013 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.000 | 1.000 | 0.000 | 当前社区资料不足 |
| rag-015 | PARTIAL_EVIDENCE_RETRIEVED | CHUNK_RETRIEVAL_FAILURE | 0.027 | 1.000 | 0.100 | 根据社区资料，Redis 的持久化方式选择取决于对数据安全性和恢复速度的需求。RDB 快照适合备份和灾难恢复，文件紧凑、恢复速度快，但可能丢失最后一次快照后的数据；AOF 日志记录每次写操作，数据安全 |
| rag-016 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.055 | 1.000 | 0.150 | Redis 高可用部署需要注意：单机存在单点故障，生产环境必须考虑高可用；支持主从复制、哨兵（Sentinel）模式实现高可用，以及 Redis Cluster 实现数据分片和水平扩展；实战中至少部署 |
| rag-020 | REQUIRED_EVIDENCE_MISSING | CHUNK_RETRIEVAL_FAILURE | 0.000 | 1.000 | 0.000 | 当前社区资料不足 |

## 10. Latency and token baseline

| Run/metric | p50 | p95 | Notes |
|---|---:|---:|---|
| Production generation latency (ms) | 2252.819 | 3901.7654 | live provider calls through production function |
| Oracle generation latency (ms) | 1732.613 | 2458.002 | same generator, synthetic gold evidence |
| Production prompt tokens | 1638.0 | 1811.8 | provider usage when returned |
| Production output tokens | 344.0 | 678.8 | provider usage when returned |
| Evidence context token estimate | 620.25 | 929.35 | UTF-8 bytes / 4 estimate |
| Historical retrieval total (ms) | 271.666 | 320.64415 | captured artifact; not rerun or used for evidence |
| Estimated production total RAG (ms) | 2533.484 | 4224.0532 | historical retrieval total + generation latency |

## 11. Diagnosis

- Exact FIRST_BAD_STATE for the canonical retrieval path remains `POST_RETRIEVAL → CHUNK_RETRIEVAL` when required gold evidence is absent.
- Generation-limited cases are those with `GOLD_EVIDENCE_RETRIEVED` where deterministic correctness/faithfulness/completeness/citation thresholds still fail; their distribution is in the JSON artifact.
- Oracle result: `oracle does not materially exceed production on the deterministic correctness proxy`.
- No ranking, reranking, chunking, embedding, prompt, generator, or production implementation change was made.

## 12. Files and next recommendation

- Production files changed: `0`.
- Evaluation files: `apps\backend\scripts\evaluate_rag_generation_v1.py`, `docs\evaluation\rag_generation_evaluation_v1_results.json`, `docs\reports\RAG_GENERATION_EVALUATION_V1.md`.
- Dirty files at evaluation completion: `apps/backend/scripts/evaluate_rag_generation_v1.py, docs/evaluation/rag_generation_evaluation_v1_results.json, docs/reports/RAG_GENERATION_EVALUATION_V1.md`.
- Recommendation: Keep production retrieval and generation unchanged for this phase. Use the oracle delta and retrieval strata to decide whether the next checkpoint is retrieval-focused or a separately scoped generation-quality diagnosis; do not implement a fix from this report alone.

## Reproducibility notes

- The live provider is nondeterministic across time even with temperature 0; model, prompt hash, input evidence IDs, request counts, usage, and errors are recorded per case.
- No prompt/output was sent to a separate judge. Detailed case records contain answers and hashes, but no API credentials or full prompts.
