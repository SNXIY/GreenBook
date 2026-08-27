# GREENBOOK_FINAL_STABILIZATION_COMPLETE

Date: 2026-08-27
Branch: `feature/hybrid-search-rag`
HEAD: `2b593fa4d521bca30a5a913eed2a0df6db308450`
Git policy: NO COMMIT / NO PUSH

## 1. Recovery and final-unit stabilization

Recovery validation matched the shutdown branch and HEAD. The dirty worktree was preserved; `shutdown-final.patch` was not applied. Required untracked harnesses, reports, checkpoints, and inspection scripts were present. Frontend, Agent API, MCP, Java, PostgreSQL, Redis, Kafka, Elasticsearch, Qdrant, and MySQL were UP/healthy during validation. Runtime tokens were recorded only as PRESENT/MATCH.

The final `.venv` unit audit completed with **1602 passed, 1 pytest-cache warning, 0 failures**. The final failure set was cleared by:

- host-injected Java observability callbacks, keeping `java_client` independent of `agent_core`;
- explicit production-memory dirty-scope auditing without hiding the full dirty list;
- correcting stale READY/RUNNING projection expectations and adding a live-sibling regression;
- updating the MCP catalog/activity expectations for the current 15-tool surface and `ANSWER_FROM_KNOWLEDGE` mapping;
- canonical Objective candidate/resource-title resolution expectations.

## 2. MO-08 root cause and fix

The first bad state was `OBJECTIVE_DECOMPOSITION`, specifically `OBJECTIVE_EXPANSION_DUPLICATE` in the interpreter's mixed CREATE/COMPLEX span-grouping path. A complex CREATE carrying a non-`CREATE_TASK` mutation delta was grouped again as a semantic SEARCH sibling. The reducer and HITL renderer were downstream observers, not the origin.

The minimal general fix skips that span-grouping expansion when the structured CREATE already carries the non-`CREATE_TASK` delta. MO-08 now freezes exactly two canonical Objectives: independent SEARCH and DELETE. SEARCH completes; DELETE enters `WAITING_APPROVAL`; browser rejection ends the execution safely with zero destructive writes. No prompt/keyword special case, second planner, or Durable Runtime bypass was added.

## 3. Multi-Objective correctness

MO-01 through MO-12 evidence is retained and was not rerun as a full matrix. The accepted corpus covers 2/3 independent CREATEs, mixed READ/CREATE, SEARCH -> CREATE, CREATE -> SCHEDULE, sibling DAGs, HITL + independent READ, independent schedules, publication intent, cross-turn target isolation, and conversation isolation. Objective identity, dependency ownership, target/resource binding, temporal scope, publication intent, HITL scope, and duplicate-objective checks passed in the focused/affected-family evidence.

The real independent benchmark is:

```text
Task
鈹溾攢 O1 CREATE_DRAFT Java
鈹斺攢 O2 CREATE_DRAFT Agent Memory
```

Both dependencies are `none`, resources are distinct, and the real Browser run completed both drafts. Existing three-objective semantic evidence remains MO-02/MO-06. Parallel eligibility is intentionally bounded to independent CREATE_DRAFT leaves with `max_parallel_objectives=2`; dependent, conflicting, mutating, and shared-HITL shapes remain serial.

## 4. Parallel execution and failure isolation

The implementation is single-runtime bounded Objective Executor parallelism. Each write still uses Durable Runtime -> MCP -> Java and retains `task_id + objective_id + action + resource_id` identity. The real Browser AFTER artifact recorded two overlapping Durable executions with distinct resources:

- browser E2E: 73,859 ms -> 55,672 ms (**-18,187 ms**);
- runtime total: 65,793 ms -> 48,071 ms (**-17,722 ms**);
- observed overlap: 6,730.941 ms;
- serial-equivalent objective execution: 14,648.791 ms;
- both executions: `COMPLETED`.

Focused isolation tests passed for sibling success/failure, dependency non-parallelization, shared-resource safety, RESULT_UNKNOWN stop, CAS merge, and idempotent continuation. No independent sibling is reported successful when another objective fails.

## 5. Browser E2E and UX

Previously accepted Browser Core remains PASS for search/read/view, draft-only, schedule update/cancel with draft preservation, two drafts, and delete rejection. Conversation lifecycle real-browser smoke passed new/switch/refresh/isolation and rejected implicit cross-conversation mutation without execution.

Final UX smoke (`tests/e2e/browser_ux_final_smoke.py`) passed after one minimal frontend fix:

- rapid double-send: initial reproduction admitted 2 Runs; synchronous `AgentPanel.send` gate reduced this to exactly 1 `COMPLETED` Run;
- stale response: A completed after switching to B; B had zero messages and no A marker in rendered conversation articles;
- progress/TUF: user-facing progress/status nodes were visible;
- internal identifier leakage: none observed in the final UX checks.

The progress probe became visible after approximately 31-54 seconds in the sampled long turns. Formal first-useful-feedback instrumentation remains `NOT_INSTRUMENTED`; this is a UX latency finding, not a fabricated TTFT/TTA value.

## 6. Performance BEFORE -> AFTER

The final focused AFTER measurement completed five non-RAG samples, one per scenario, in `.runtime/agent_performance_final_after_focus.json`. They are valid observations, not p50 replacements:

| Scenario | BEFORE p50 | AFTER sample | LLM calls | Notes |
|---|---:|---:|---:|---|
| Simple READ | 38,776 ms | 47,274 ms | 2 | n=1; no optimization claim |
| Simple WRITE | 44,143 ms | 45,965 ms | 2 | n=1; completed |
| Sequential draft -> schedule | 55,824 ms | 80,585 ms | 2 | queue wait observed 32,822 ms |
| Search + creation | 61,792 ms | 77,629 ms | 3 | dependency remained serial |
| 2 independent objectives | 73,859 ms serial | 48,646 ms focused API sample; 55,672 ms real Browser parallel | 1/2 | real parallel comparison is the accepted evidence |

Java remained approximately 67-243 ms in the focused samples and was not treated as the critical path. Provider token timestamps are unavailable, so TTFT is `UNAVAILABLE`, not approximated from TTA.

## 7. Context, Memory, ActionLoop, Durable claim

Memory semantics, authority, types, relevance, ranking, and retention were not changed. Only independently scoped repository I/O was overlapped. The focused memory Browser sample recorded memory repository search at 6,400 ms; the final performance sample recorded memory totals of 5,996-6,651 ms.

No deterministic ActionLoop shortcut was added: simple READ still requires two LLM calls under current evidence. No Durable claim interval change was made because the 5.1-8.2 second created-to-claimed range includes worker availability/startup and is not proven to be polling alone.

## 8. Rejected optimizations and RAG

Rejected: new MQ/Kafka for command claim, broad async refactor, unbounded concurrency, Multi-Agent, second Planner, Durable permission-policy changes, direct `tool.invoke` writes, RAG chunk/embedding/reranker/top-k/prompt tuning, and Memory semantic tuning. The architecture remains PostgreSQL Durable Queue for agent execution, Kafka for business events/projections, and synchronous MCP -> Java for immediate business truth.

RAG canonical `community.answer_from_knowledge` admission remains fixed and accepted. Existing RAG browser evidence remains fail-closed/no-answer with incomplete citation proof under `RAG_CURRENT_LIMIT_ACCEPTED`; it was not rerun in this final scope.

## 9. Reliability and full-system acceptance

Recovery, Durable Runtime/HITL rejection, retry/continuation, RESULT_UNKNOWN safeguards, conversation isolation, business truth checks, frontend projection checks, focused multi-objective isolation, and final UX smoke are covered by the retained artifacts and focused tests. Full broad RAG grounding and a statistically comparable P1-P7 performance corpus remain open/partial by explicit scope.

## 10. Modified files and dirty audit

Tracked dirty files: **43**. Untracked top-level entries: **14** after adding the final report/evaluation and final checkpoint. The worktree is intentionally dirty and no changes were discarded.

Production/runtime groups include `apps/agent_api/**`, `apps/agent_worker/**`, `packages/agent_core/**`, `packages/contracts/**`, `packages/java_client/**`, and `services/greenbook_mcp/**`. Evaluation/runtime harness groups include `apps/backend/scripts/measure_agent_performance_baseline.py`, `scripts/dev/**`, and `scripts/memory_evaluation_harness.py`. Tests include the affected unit suites and `tests/e2e/browser_ux_final_smoke.py`. Frontend production change is `zhiguang-fe/src/components/agent/AgentPanel.tsx`.

Untracked evidence/harness entries include `apps/backend/scripts/run_overnight_multi_objective_matrix.py`, `checkpoints/`, `docs/evaluation/`, `docs/reports/`, `docs/worklogs/`, safe inspection scripts, `tests/e2e/browser_ux_final_smoke.py`, and `tests/unit/test_action_loop_parallel_objectives.py`.

Files to remove before a future cleanup commit: **none automatically**. The original checkpoint/worklog material was preserved under `docs/archive/recovery/`; runtime/evaluation artifacts and test-only harnesses remain subject to deliberate review. The pre-cleanup baseline has now been committed and tagged separately.

## 11. Verdict and next three priorities

Final verdict: **`GREENBOOK_FINAL_ACCEPTANCE_PARTIAL`**.

The functional/correctness gates are green for the completed scope, but strict full acceptance is not claimed because RAG grounding remains an accepted limitation, first-useful-feedback is not instrumented, and P1-P7 does not have a multi-sample equivalent corpus (P5 RAG and 3-objective performance were intentionally not rerun/expanded).

Next priorities:

1. Add formal first-useful-feedback instrumentation and repeat the performance scenarios with equivalent samples if a strict performance PASS is required.
2. Revisit RAG grounding/citation only under a separately approved RAG change.
3. Before any future cleanup change, review the dirty audit and archived checkpoint/evaluation artifacts, then stage selectively.

Evidence index: `docs/archive/recovery/overnight-20260826-27/STATE.md`, `NEXT_CURSOR.md`, `TEST_MATRIX.json`, `FAILURES.json`, `PERFORMANCE_BEFORE_AFTER.json`, `UX_FINDINGS.md`, `ARCHITECTURE_FINDINGS.md`, and the latest incremental checkpoint.
