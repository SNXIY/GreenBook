# RAG_SEMANTIC_RANKING_DIAGNOSIS_V7

V3 diagnosis checkpoint: `df09dc8bc30a8da4ec1bd5d71d4a13fc035056b6`  
V6 evaluation checkpoint: `32f57b8`  
Frozen snapshot: `be65bfed2d90405472e0c19a6f472861ea23625b5372d4b7fa62b1df80c5de09`

## Verdict

`RAG_SEMANTIC_RANKING_NO_GAIN`

## 1. Current checkpoint and frozen reproducibility

- Current FIRST_BAD_STATE: `POST_RETRIEVAL -> CHUNK_RETRIEVAL`.
- Frozen snapshot digest recomputed: `True`.
- Frozen snapshot rerun drift: `0`.
- Historical capture-vs-V3 drift retained as metadata: `6`.
- Queries / gold refs / frozen catalog: `45 / 104 / 5040`.
- Candidate post depth is fixed at Top10; no live post retrieval, Qdrant read, MySQL read, or snapshot recapture was performed.

## 2. Baseline miss states and failure families

Baseline misses are 78 of 104 gold references. A/B is the retrieval-state split; C-G are mutually exclusive root-cause labels within B when the frozen observables support them. `UNRESOLVED_PRESENT_RANKED_LOW` is retained instead of forcing an unsupported cause.

| State | Count | Share of misses | Share of all gold |
|---|---:|---:|---:|
| `GOLD_NOT_IN_RETRIEVAL_POOL` | 24 | 0.307692 | 0.230769 |
| `GOLD_PRESENT_RANKED_LOW` | 54 | 0.692308 | 0.519231 |

### B root-cause distribution

| Root cause | Count | Share of B | Share of all misses |
|---|---:|---:|---:|
| `SAME_POST_COMPETITION` | 18 | 0.333333 | 0.230769 |
| `DUPLICATE_COMPETITION` | 12 | 0.222222 | 0.153846 |
| `QUERY_EVIDENCE_SEMANTIC_GAP` | 14 | 0.259259 | 0.179487 |
| `EMBEDDING_REPRESENTATION_LIMIT` | 5 | 0.092593 | 0.064103 |
| `GOLD_AMBIGUITY` | 0 | 0.000000 | 0.000000 |
| `UNRESOLVED_PRESENT_RANKED_LOW` | 5 | 0.092593 | 0.064103 |

Typical cases and full Top10/Top20/Top50 traces are in the JSON artifact. The report keeps examples compact to avoid hiding the aggregate evidence.

## 3. Gold rank distribution

| Gold rank bucket | Count | Rate |
|---|---:|---:|
| `1` | 1 | 0.009615 |
| `2-3` | 10 | 0.096154 |
| `4-5` | 5 | 0.048077 |
| `6-10` | 10 | 0.096154 |
| `11-20` | 13 | 0.125000 |
| `21-50` | 16 | 0.153846 |
| `>50` | 25 | 0.240385 |
| `missing` | 24 | 0.230769 |
| `>50 / missing` | 49 | 0.471154 |

## 4. Rerankable failure rate

Definition: `Gold is in the frozen bounded chunk candidate pool and current dense rank > 10.`

- Rerankable failures: `54` / `78` = `0.692308` of baseline misses.
- Rate over all gold refs: `0.519231`.
- Large-signal threshold used: `0.5`; result: `True`.

## 5. Strategies tested

All strategies keep the frozen Top10 parent posts and final evidence depth at 10. The two lightweight reranker variants use only the frozen dense score plus deterministic query-term coverage and frozen parent-post rank; no cross-encoder, LLM, second runtime, or new service was introduced.

| Strategy | ALL Cond R@10 | Strong Cond R@10 | Final R@10 | MRR | Dup waste | p95 ms | Accepted |
|---|---:|---:|---:|---:|---:|---:|---|
| `CURRENT_DENSE` | 0.307692 | 0.354167 | 0.251852 | 0.156315 | 0.048889 | 0.146000 | False |
| `DENSE_PLUS_LEXICAL` | 0.316239 | 0.375000 | 0.259259 | 0.157275 | 0.046667 | 0.326000 | False |
| `PARENT_AWARE` | 0.311966 | 0.359375 | 0.255556 | 0.150479 | 0.062222 | 0.325000 | False |
| `REDUNDANCY_AWARE` | 0.243590 | 0.286458 | 0.211111 | 0.150621 | 0.015556 | 785.103000 | False |
| `LIGHTWEIGHT_RERANKER_TOP30` | 0.337607 | 0.369792 | 0.266667 | 0.162688 | 0.071111 | 0.354000 | False |
| `LIGHTWEIGHT_RERANKER_TOP50` | 0.346154 | 0.380208 | 0.274074 | 0.164917 | 0.073333 | 0.349000 | False |

## 6. Baseline to best

Best observed strategy by ALL Conditional Recall@10 then MRR: `LIGHTWEIGHT_RERANKER_TOP50`.

| Metric | Baseline | Best | Delta |
|---|---:|---:|---:|
| Conditional Recall@5 | 0.209402 | 0.217949 | 0.008547 |
| Conditional Recall@10 | 0.307692 | 0.346154 | 0.038462 |
| Final Evidence Recall@10 | 0.251852 | 0.274074 | 0.022222 |
| Chunk MRR | 0.156315 | 0.164917 | 0.008602 |
| Duplicate slot waste | 0.048889 | 0.073333 | 0.024444 |
| Gold harmed rate | 0.000000 | 0.037500 | 0.037500 |

## 7. Latency and bounded output

| Strategy | Candidate pool | Final max | p50 ms | p95 ms | max ms |
|---|---:|---:|---:|---:|---:|
| `CURRENT_DENSE` | 247.733333 avg / 416 max | 10 | 0.098000 | 0.146000 | 0.178000 |
| `DENSE_PLUS_LEXICAL` | 247.733333 avg / 416 max | 10 | 0.189000 | 0.326000 | 0.355000 |
| `PARENT_AWARE` | 247.733333 avg / 416 max | 10 | 0.196000 | 0.325000 | 5.649000 |
| `REDUNDANCY_AWARE` | 247.733333 avg / 416 max | 10 | 507.255000 | 785.103000 | 850.989000 |
| `LIGHTWEIGHT_RERANKER_TOP30` | 30.000000 avg / 30 max | 10 | 0.206000 | 0.354000 | 0.387000 |
| `LIGHTWEIGHT_RERANKER_TOP50` | 50.000000 avg / 50 max | 10 | 0.216000 | 0.349000 | 0.378000 |

## 8. Decision

- Reranker justified for offline testing: `True`.
- Acceptance gate passed by any strategy: `False`.
- Exact FIRST_BAD_STATE: `POST_RETRIEVAL -> CHUNK_RETRIEVAL`.
- Generation and EvidenceSelector remain outside this evaluation.

## 9. Production and dirty state

Production files changed: `[]`.

Evaluation-only artifacts produced by this phase:
- `apps/backend/scripts/evaluate_rag_semantic_ranking_v7.py`
- `docs/evaluation/rag_semantic_ranking_v7_results.json`
- `docs/reports/RAG_SEMANTIC_RANKING_V7_REPORT.md`

These V7 evaluation artifacts are intentionally left dirty for review; no V7 commit or push is performed.

## 10. Next recommendation

Rerankable failures are present, but no bounded offline strategy clears the acceptance gate. Stop ranking experiments; keep production unchanged and treat the remaining gap as an embedding/query representation or dataset/corpus issue.

`RAG_SEMANTIC_RANKING_DIAGNOSIS_V7_COMPLETE`
