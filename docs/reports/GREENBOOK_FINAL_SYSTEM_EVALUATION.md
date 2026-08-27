# GREENBOOK_FINAL_SYSTEM_EVALUATION_COMPLETE

Date: 2026-08-27
Branch: `feature/hybrid-search-rag`
Baseline tag: `greenbook-clean-baseline-20260827`

## Final verdict

`GREENBOOK_SYSTEM_ACCEPTANCE_PASS_WITH_LIMITATIONS`

Functional correctness, objective isolation, bounded parallel execution,
Durable safety, HITL behavior, reliability safeguards, Browser core behavior,
Memory policy, and canonical RAG admission are accepted for the evaluated
scope. Strict performance acceptance is `INCONCLUSIVE`, and RAG grounding
remains the documented accepted limitation.

## 1. Evaluation coverage

The unified manifest contains **64 cases** across 17 categories: semantic,
multi-turn, Multi-Objective, dependency DAG, target, temporal, parallel, HITL,
Durable/reliability, RESULT_UNKNOWN, idempotency, conversation isolation,
Search, RAG, Memory, Browser UX, and performance.

It reuses the retained MO-01..MO-12, semantic, Browser, reliability, RAG,
Memory, and V2 performance evidence. Browser acceptance cases remain
Browser-originated; unit tests are used for localization and regression.

## 2. Functional and semantic metrics

- Full unit baseline retained: **1602 passed, 0 failed**.
- Final focused functional/reliability suites: **143 passed, 0 failed**.
- Semantic retained benchmark: exact `55/78`; core intent `68/78`.
- Objective decomposition: MO-01..MO-12 focused evidence PASS; duplicate
  objective count `0`.
- Wrong dependency count: `0` in the accepted focused corpus.
- Wrong resource count: `0`.
- Business truth mismatches: `0` observed.
- Duplicate physical mutations: `0`.

MO-08 remains fixed at the canonical decomposition layer: SEARCH and DELETE
are distinct objectives, DELETE is approval-gated, and SEARCH is not rebuilt
when HITL pauses DELETE.

## 3. Parallel and reliability

The implementation remains one Agent Runtime with deterministic bounded
Objective Executor concurrency, `max_parallel_objectives=2`, and eligibility
limited to independent CREATE_DRAFT leaves. Durable Runtime -> MCP -> Java and
`task_id + objective_id + action + resource_id` identity are preserved.

Real Browser parallel evidence: `73,859ms -> 55,672ms`; runtime
`65,793ms -> 48,071ms`; observed overlap `6,730.941ms`. Focused tests cover
failure isolation, RESULT_UNKNOWN stop, CAS merge, retry, resume, idempotent
callback, same-resource conflict, and dependent readiness.

## 4. Browser, RAG, and Memory

Final Browser acceptance is PASS with limitations. The final smoke proved one
Run for rapid send, no stale cross-conversation UI leak, and stable A/B
conversation behavior. TUF observation is formalized but measured
`UNAVAILABLE` because no dedicated meaningful DOM progress node appeared in
the controlled 60-second samples.

RAG canonical admission is PASS through `community.answer_from_knowledge`.
No chunk, embedding, reranker, TopK, or prompt experiment was performed.
Grounding/citation remains `RAG_CURRENT_LIMIT_ACCEPTED_PARTIAL`; no-answer
behavior is fail-closed.

Memory remains stage-sealed. Preference, semantic, episodic, and procedural
evaluation evidence passes with no conversation/tenant leakage and no current
state authority granted to recalled memory.

## 5. Strict performance

Equivalent AFTER samples were attempted for the same public-boundary scenarios
as the V2 BEFORE corpus. The run produced request timeouts for some samples and
large queue/LLM variance. Therefore the strict result is
`STRICT_ACCEPTANCE_INCONCLUSIVE`, not PASS. No optimization was started from
this observation. See [GREENBOOK_FINAL_PERFORMANCE_ACCEPTANCE.md](GREENBOOK_FINAL_PERFORMANCE_ACCEPTANCE.md).

## 6. Architecture and scope audit

- Planner count: `1`.
- Runtime count: `1`.
- Memory runtime count: `1`.
- Tool Registry count: `1`.
- Scheduler decision remains deterministic ready/conflict/budget.
- New MQ: `0`.
- Multi-Agent/SUB_AGENT: `0`.
- Second Planner: `0`.
- RAG/Memory semantic changes: `0`.

No Interaction Agent, Analytics Agent, Moderation Agent, ResultSetBinding, or
new business module was introduced.

## 7. Reproducibility and artifacts

- Manifest: [final_system_evaluation_manifest.json](../evaluation/final_system/final_system_evaluation_manifest.json)
- Results: [final_system_evaluation_results.json](../evaluation/final_system_evaluation_results.json)
- Browser report: [GREENBOOK_FINAL_BROWSER_ACCEPTANCE.md](GREENBOOK_FINAL_BROWSER_ACCEPTANCE.md)
- Performance report: [GREENBOOK_FINAL_PERFORMANCE_ACCEPTANCE.md](GREENBOOK_FINAL_PERFORMANCE_ACCEPTANCE.md)
- Hygiene report: [PROJECT_HYGIENE_AUDIT.md](PROJECT_HYGIENE_AUDIT.md)

The evaluation-only TUF change is in
`tests/e2e/browser_ux_final_smoke.py`; the CDP harness serializes one WebSocket
connection with an async lock. No production runtime semantics were changed.

## 8. Remaining limitations and next step

Known limitations are RAG grounding/citation quality, unavailable provider
TTFT, unavailable measured TUF in the controlled samples, and inconclusive
strict performance due to current request/queue variance. These are separated
from correctness bugs.

Next step: perform a merge-readiness review of this evidence and limitations;
do not add features or optimize further until the performance variance and TUF
surface are separately decided.
