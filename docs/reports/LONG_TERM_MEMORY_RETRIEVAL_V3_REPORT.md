# LONG_TERM_MEMORY_RETRIEVAL_V3_REPORT

Evaluation baseline commit: `75529b13227fa1bee3c4a9bbbd99bc2c21154c34`
Evaluation checkpoint: `17a156d8464a0f33176781f3717e4d0e80854afa`
Diagnosis: [LONG_TERM_MEMORY_RETRIEVAL_V3_DIAGNOSIS.md](LONG_TERM_MEMORY_RETRIEVAL_V3_DIAGNOSIS.md)

## Verdict

**MEMORY_RETRIEVAL_V3_PASS**

The V2 evaluation baseline was committed and pushed before diagnosis. Offline
experiments reused the same mixed four-type dataset. Only after an offline
strategy passed the acceptance gate was the selected policy applied to the
existing canonical `MemoryRetriever -> MemoryRelevanceGate` path.

## Exact FIRST_BAD_STATE and failure families

- FIRST_BAD_STATE: **`retrieval selected set`**
- Failure families: **`RETRIEVAL_ISSUE`, `RELEVANCE_GATE_ISSUE`**
- Baseline report: [LONG_TERM_MEMORY_SYSTEM_EVALUATION.md](LONG_TERM_MEMORY_SYSTEM_EVALUATION.md)

The diagnosis found global, type-neutral lexical scoring: shared article and
publication words produced cross-type false positives, while the required
Preference in the multi-type request scored below the global threshold.

## Offline strategies

| Strategy | Configuration | Recall@5 | Irrelevant Injection | Required Miss |
|---|---|---:|---:|---:|
| V2 baseline | `{}` | 0.9722 | 45.83% | 7.14% |
| Type-aware score only | `{'required_boost': 0.15, 'optional_factor': 0.35, 'relevance_threshold': 0.45, 'coverage_threshold': None}` | 1.0000 | 0.00% | 0.00% |
| Type-aware score + required-type coverage | `{'required_boost': 0.05, 'optional_factor': 0.35, 'relevance_threshold': 0.45, 'coverage_threshold': 0.35}` | 1.0000 | 0.00% | 0.00% |

The selected offline policy uses one deterministic `MemoryNeedProfile`, one
unified type-aware score, and one required-type coverage pass inside the same
Gate. Required types receive a small additive boost; non-required types are
attenuated but not hard-blocked; an explicit no-memory profile returns zero;
coverage is allowed only above its bounded coverage threshold. Total selection
remains bounded by the caller's limit.

## V2 → V3 metrics

| Metric | V2 | V3 | Delta |
|---|---:|---:|---:|
| Recall@1 | 0.9444 | 0.9444 | 0.0000 |
| Fixed Precision@1 | 1.0000 | 1.0000 | 0.0000 |
| Returned Precision@1 | 1.0000 | 1.0000 | 0.0000 |
| Recall@3 | 0.9722 | 1.0000 | 0.0278 |
| Fixed Precision@3 | 0.3611 | 0.3889 | 0.0278 |
| Returned Precision@3 | 0.9028 | 1.0000 | 0.0972 |
| Recall@5 | 0.9722 | 1.0000 | 0.0278 |
| Fixed Precision@5 | 0.2167 | 0.2333 | 0.0167 |
| Returned Precision@5 | 0.9028 | 1.0000 | 0.0972 |
| No-match false return rate | 0.00% | 0.00% | 0.00% |
| Irrelevant Memory Injection Rate | 45.83% | 0.00% | -45.83% |
| Required Memory Miss Rate | 7.14% | 0.00% | -7.14% |
| Selected memory count max | 5 | 3 | - |
| Context chars p50 | 2337.0 | 2337.0 | - |
| Context chars p95 | 2820.0 | 2892.0 | - |
| Context chars max | 2820.0 | 2920.0 | - |
| Estimated context tokens p50 | 585.0 | 585.0 | - |
| Estimated context tokens p95 | 705.0 | 723.0 | - |
| Estimated context tokens max | 705.0 | 730.0 | - |

`Fixed Precision@K` uses K as denominator. `Returned Precision@K` uses the
number actually returned in the prefix. `Irrelevant Memory Injection Rate`
counts every selected record outside the expected set across all retrieval
cases, including no-memory cases; `Required Memory Miss Rate` counts missed
expected records. These denominators are intentionally distinct.

## Per-type metrics

| Type | Precision | Recall | Required | Selected |
|---|---:|---:|---:|---:|
| PREFERENCE | 1.0000 | 1.0000 | 4 | 4 |
| SEMANTIC | 1.0000 | 1.0000 | 5 | 5 |
| EPISODIC | 1.0000 | 1.0000 | 2 | 2 |
| PROCEDURAL | 1.0000 | 1.0000 | 3 | 3 |

V3 required recall by type from the canonical regression:
`{'PREFERENCE': 1.0, 'SEMANTIC': 1.0, 'EPISODIC': 1.0, 'PROCEDURAL': 1.0}`.

## Acceptance gate

| Gate | Result |
|---|---|
| no_match_false_return_zero | PASS |
| authority_violation_zero | PASS |
| user_leakage_zero | PASS |
| tenant_leakage_zero | PASS |
| required_miss_materially_below_baseline | PASS |
| irrelevant_injection_materially_below_baseline | PASS |
| recall_at_5_not_meaningfully_lower | PASS |
| context_budget_bound_preserved | PASS |
| lifecycle_pass | PASS |
| duplicate_active_zero | PASS |
| architecture_scope_pass | PASS |

## Lifecycle, duplicate, isolation and authority

- Lifecycle: **5/5** passed.
- Duplicate ACTIVE rate: **0.00%**.
- No-match false return rate: **0.00%**.
- User leakage: **0**; tenant leakage: **0**.
- Memory authority violation rate: **0.00%**.
- Current explicit instruction override failures: **0**.
- ContextBuilder bounded rate: **100.00%**.

## Production files changed

`['packages/agent_core/greenbook_agent_core/memory/relevance.py', 'packages/agent_core/greenbook_agent_core/memory/retriever.py']`

The production diff is limited to the canonical memory relevance path:
`memory/retriever.py` and `memory/relevance.py`. No write architecture,
admission, lifecycle, repository schema, ActionLoop, Durable Runtime, MCP,
Search, RAG, or Java file was changed.

## Focused tests

- `.\.venv\Scripts\python.exe -m pytest tests/unit/test_memory_retrieval_v3.py tests/unit/test_long_term_memory_system_evaluation.py tests/unit/test_memory_runtime_convergence.py tests/unit/test_memory_retriever.py tests/unit/test_preference_memory_retrieval.py tests/unit/test_semantic_memory_v1.py tests/unit/test_episodic_memory_v1.py tests/unit/test_procedural_memory_v1.py tests/integration/test_context_memory_runtime.py -q`
- `.\.venv\Scripts\ruff.exe check packages/agent_core/greenbook_agent_core/memory/retriever.py packages/agent_core/greenbook_agent_core/memory/relevance.py scripts/memory_evaluation_harness.py tests/unit/test_long_term_memory_system_evaluation.py tests/unit/test_memory_retrieval_v3.py`
- `git diff --check`

## Dirty files

`['docs/evaluation/long_term_memory_system_results.json', 'docs/reports/LONG_TERM_MEMORY_SYSTEM_EVALUATION.md', 'packages/agent_core/greenbook_agent_core/memory/relevance.py', 'packages/agent_core/greenbook_agent_core/memory/retriever.py', 'scripts/memory_evaluation_harness.py', 'tests/unit/test_long_term_memory_system_evaluation.py', 'docs/evaluation/long_term_memory_retrieval_v3_diagnosis.json', 'docs/evaluation/long_term_memory_retrieval_v3_results.json', 'docs/reports/LONG_TERM_MEMORY_RETRIEVAL_V3_DIAGNOSIS.md', 'docs/reports/LONG_TERM_MEMORY_RETRIEVAL_V3_REPORT.md', 'tests/unit/test_memory_retrieval_v3.py']`

No V3 implementation commit or push was performed. The V2 evaluation baseline
commit was `75529b13227fa1bee3c4a9bbbd99bc2c21154c34` and remains the comparison point.

## Remaining limitations and next recommendation

The profile is intentionally deterministic and lightweight; it is not a
second Interpreter and does not infer new Memory. The current experiment covers
the existing mixed dataset only. Keep the four-type write architecture and
single Retriever/Gate boundary unchanged. Do not add Memory types or automatic
learning until a separately designed benchmark demonstrates need.
