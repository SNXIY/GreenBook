# GreenBook A Overnight Full-stack Evaluation Report

评测日期：2026-08-25（Asia/Shanghai）  
范围：真实 Frontend → Agent → Durable Runtime → PostgreSQL queue → in-process Worker → Tool/MCP → Java → MySQL/Scheduler → Frontend / Reload  
原则：只把有证据的结果计入指标；未埋点字段标记为 unavailable；不把 Harness、Provider、Expected Failure Injection 和 Product Failure 混成一个成功率。

## A. Runtime

### Canonical runtime

| Component | Canonical endpoint | Final state |
|---|---|---|
| Frontend | http://127.0.0.1:5173 | HTTP 200 |
| Agent API | http://127.0.0.1:8094 | health UP |
| Java Backend | http://127.0.0.1:8080 | actuator UP |
| PostgreSQL | 127.0.0.1:25432 | listening |
| MySQL | 127.0.0.1:33306 | listening |
| Redis | 127.0.0.1:26379 | listening |
| Kafka | 127.0.0.1:39092 | listening |
| Qdrant | 127.0.0.1:26333 / 26334 | listening |
| Browser/CDP | CDP 9222, page 127.0.0.1:5173 | correct target |

Agent final health evidence:

    status=UP
    version=2.0.0
    javaConfigured=true
    javaReachable=true
    executionDispatch=queue
    executionStorage=postgres
    executionConsumer=in_process

Preflight passed for the final state. Browser login remained valid, the Frontend title and Agent trigger were present, and the CDP target was the canonical Frontend.

During final read-only verification, the original Agent process had a listening socket but its health request timed out. It was restarted once with the same canonical configuration and port using [overnight-agent-restart.stdout.log](../../.runtime/overnight-agent-restart.stdout.log) and [overnight-agent-restart.stderr.log](../../.runtime/overnight-agent-restart.stderr.log). No alternate port, second runtime, Mock Java, fake Scheduler, fake Business Store, or test-only API base URL was introduced. No fault harness process remained after recovery.

## B. L1

L1 was already certified before this overnight run and was not rerun.

- Source: [L1 fresh certification](../../.runtime/round1-final-v2/l1-context-phase2-fresh-certification-20260824.json)
- Fresh Natural L1: 20/20 turns completed
- Same conversation: true
- Product Fail: 0
- Harness Fail: 0
- Real Approval: 3
- Physical writes: 14, all expected business actions
- Java truth: 20/20 consistent
- Frontend truth: 20/20 consistent
- Safety: wrong resource 0, wrong temporal 0, duplicate write 0, false success 0, unsafe physical WRITE 0, context contamination 0, unnecessary Clarify 0
- Latency: P50 55.19 s, P95 94.42 s

Verdicts:

- FUNCTIONAL_VERDICT: PASS
- STATE_CONSISTENCY_VERDICT: PASS
- UX_VERDICT: PARTIAL — the existing L1 certification proves Frontend truth, but this run did not re-open the already-certified journey for a separate UX-only audit.

## C. Natural L2

### Debug path and checkpoint/resume

The L2 debug work used the existing conversation evidence and resumed from checkpoints. Previously verified turns were not replayed from T1 after a fix.

1. The first debug fixture reached a setup-invalid state because it referenced a failed objective that the fixture had never created. This was classified as SETUP_INVALID / Harness fixture error, not a Product Failure.
2. The corrected resumed path is [l2-t14-resumed-after-reload-20260825.json](../../.runtime/round1-final-v2/l2-t14-resumed-after-reload-20260825.json): 14 turns in one conversation, 12 COMPLETED, 1 expected PARTIAL_SUCCESS, and 1 FAILED turn.
3. The resumed path exercised draft creation/update, schedule creation/update/cancel, publish, search, summarize, another draft lifecycle, approval through the real DOM, reload, and continuation.
4. The T8 failure was a real projection/runtime failure: a valid current child completed while a failed sibling caused the aggregate Task to remain FAILED. It was saved as a failure snapshot and fixed with a general aggregate-status invariant. The fresh scenario was not rerun after this discovery, per the one-fresh-run rule.

The existing T8 failure and subsequent continuation evidence remain available in the L2 artifact set:

- [L2 debug fault evidence](../../.runtime/round1-final-v2/l2-fault-evidence-20260825-final.jsonl)
- [L2 continuation from T9](../../.runtime/round1-final-v2/l2-continuation-from-t9-20260825.json)
- [L2 T14 after reload](../../.runtime/round1-final-v2/l2-t14-resumed-after-reload-20260825.json)

### Fresh certification

Fresh L2 was attempted once. The first artifact reached T4, and its same fresh attempt continued from the saved point in [l2-fresh-t5-targeted-replay-20260825-fixed.json](../../.runtime/round1-final-v2/l2-fresh-t5-targeted-replay-20260825-fixed.json). It reached 8/14 turns and stopped at T8 with ACTION_LOOP_ITERATION_BUDGET after one real Java publish write. It was not restarted from T1 and was not run a second time.

### Verdict

- FUNCTIONAL_VERDICT: PARTIAL — the debug checkpoint path completed the principal lifecycle coverage, but strict fresh L2 certification did not complete.
- STATE_CONSISTENCY_VERDICT: PARTIAL — Java-side writes and most projections agreed; the T8 aggregate Task projection bug was found and fixed, but the corrected invariant was not given a new full fresh L2 certification.
- UX_VERDICT: PARTIAL — approval and target cards were understandable, but duplicate user-facing status wording was observed, including a result-specific status followed by a generic completed status. Reload recovery was usable in the resumed path but not independently proven as a fully rendered UI state.

L2 summary:

| Scope | Turns | Completed | Expected partial | Product fail | Harness fail | Physical write steps | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| Debug/resume | 14 | 12 | 1 | 1 | 0 | 11 | PARTIAL |
| Fresh attempt | 8/14 | 6 | 1 | 1 | 0 | 5 | PARTIAL |

## D. Natural L3

### Debug path

The clean L3 debug path used one conversation and exercised:

- two objectives with independent failure injection;
- two failed objectives;
- retry of the failed Python objective;
- retry of the failed Agent objective;
- schedule, update schedule, cancel schedule;
- reload and resume.

Before the fix, the Agent retry turn reached TARGET_CLARIFICATION_REQUIRED even though the failed Agent objective was authoritative in the conversation snapshot. The FIRST BAD STATE was a scoped selected-task view that omitted the historical failed sibling. After the general fix, the targeted replay of T4 completed, followed by T5–T8 continuation.

Evidence:

- [L3 T1](../../.runtime/round1-final-v2/l3-v3-clean-t1-20260825.json)
- [L3 T2](../../.runtime/round1-final-v2/l3-v3-clean-t2-20260825.json)
- [L3 T3](../../.runtime/round1-final-v2/l3-v3-clean-t3-20260825.json)
- [L3 T4 after fix](../../.runtime/round1-final-v2/l3-v3-clean-t4-after-fix-20260825.json)
- [L3 T5–T8 after-fix continuation](../../.runtime/round1-final-v2/l3-v3-clean-t8-after-fix-20260825.json)

The debug path finished 8/8 turns with no wrong target, wrong temporal, context contamination, duplicate write, or false success.

### Fresh certification

Fresh L3 was run once after the debug path. [l3-fresh-certification-20260825.json](../../.runtime/round1-final-v2/l3-fresh-certification-20260825.json) contains all 8/8 turns:

- T1 and T2: expected partial failures; one independent Java/Redis draft succeeded before the injected sibling failure.
- T3 and T4: failed Python and Agent objectives retried into their own new draft lifecycles.
- T5: Agent schedule and draft action completed.
- T6: schedule time updated.
- T7: schedule cancelled and draft retained.
- T8: reload/resume completed at the API projection level.

The clean fresh fault evidence contains exactly two expected FAIL_BEFORE injections, both with request_sent=false, downstream_called=false, side_effect_started=false, and safe_to_retry=true:

- [L3 fresh fault evidence](../../.runtime/round1-final-v2/l3-fresh-fault-evidence-20260825.jsonl)

Fresh Java truth showed four real drafts for Java, Redis, Python, and Agent, one Agent schedule in CANCELLED state, and no unintended published post for the scheduled/cancelled path.

### Reload observation boundary

The API conversation projection after T8 contained the expected 14 messages. A follow-up Harness-only attempt to prove the visible hydrated message list produced [HARNESS_ERROR reload evidence](../../.runtime/round1-final-v2/l3-fresh-reload-observation-after-hydration-fix-20260825.json) with AgentPanel did not open. This is not evidence of a production state mutation; it is an unresolved browser observation/hydration issue. Fresh L3 was not rerun.

### Verdict

- FUNCTIONAL_VERDICT: PASS for the real runtime/business path and 8/8 fresh turns.
- STATE_CONSISTENCY_VERDICT: PARTIAL — Java/API projection was consistent and reload message data was present, but visible post-reload message rendering was not proven by the final Harness observation.
- UX_VERDICT: PARTIAL — HITL copy and progress were understandable; strict reload-visible consistency remains blocked by the Harness observation issue.

L3 summary:

| Scope | Turns | Completed | Expected partial | Product fail | Harness fail | Physical write steps | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| Debug/resume | 8 | 6 | 2 | 0 | 0 | 7 | PASS |
| Fresh certification | 8/8 | 6 | 2 | 0 | 0 | 7 | Runtime PASS; strict UI gate PARTIAL |

## E. Bugs

### Bug 1 — historical failed sibling omitted during explicit retry

- FIRST BAD STATE: TurnCoordinator passed the scoped selected-task view to FAILED_OBJECTIVE_RETRY resolution. The failed Agent sibling existed in the bounded conversation snapshot and database truth but was absent from selected_tasks.
- Failure Family: historical target grounding / bounded context selection.
- Root Cause: the retry path used a view optimized for the current turn without switching to the conversation snapshot when the user explicitly asked to retry a failed objective.
- General Invariant: an explicit failed-objective retry resolves against the bounded conversation snapshot; terminal status filters still prevent resurrection; resource ownership and Objective lineage remain mandatory.
- Minimal Fix: [turn_coordinator.py](../../apps/agent_api/greenbook_agent_api/services/turn_coordinator.py) selects the bounded snapshot for failed retries; TargetResolver keeps ownership/status filtering.
- Modified Files: turn_coordinator.py, target.py, and resolver regression tests.
- Regression Asset: [test_partial_retry_closure.py](../../tests/unit/test_partial_retry_closure.py), clean L3 T4 before/after artifacts, and T4→T8 continuation.
- Targeted Replay: PASS.
- Case-specific: NO.

### Bug 2 — completed child projected as failed parent Task

- FIRST BAD STATE: main.py aggregate reconciliation collapsed a parent Task to FAILED when the current run completed while a sibling execution remained failed.
- Failure Family: partial failure isolation / completion projection.
- Root Cause: aggregate status considered the failed sibling but did not preserve the current completed child as RUN_PARTIAL_SUCCESS.
- General Invariant: a completed current run with a failed sibling is partial success, not a failed current run; state transitions must allow an explicit failed-sibling continuation without reopening the wrong lifecycle.
- Minimal Fix: [main.py](../../apps/agent_api/greenbook_agent_api/main.py) projects partial success; [manager.py](../../packages/agent_core/greenbook_agent_core/task/manager.py) permits FAILED → RUNNING only for an explicit retry continuation.
- Modified Files: main.py, task/manager.py, completion recovery regression tests, and task manager tests.
- Regression Asset: [test_completion_recovery_invariants.py](../../tests/unit/test_completion_recovery_invariants.py), [test_task_manager.py](../../tests/unit/test_task_manager.py).
- Targeted Regression: PASS in unit coverage; full fresh L2 replay was intentionally not started after the one-fresh-run stop rule.
- Case-specific: NO.

### Bug 3 — retry helper contract mismatch

- FIRST BAD STATE: _run_task_deltas passed user_input to _create_user_objective_retry_task, whose contract did not accept that keyword.
- Failure Family: internal runtime call contract.
- Minimal Fix: remove the unsupported keyword at the caller; no semantic special case was added.
- Regression Asset: [test_partial_retry_closure.py](../../tests/unit/test_partial_retry_closure.py).
- Targeted Regression: PASS.
- Case-specific: NO.

### Harness issue — reload conversation selection and hydration observation

- FIRST BAD STATE: the reload observer initially verified API message count without proving the selected conversation was visibly rendered; the later preference/hydration observation produced AgentPanel did not open.
- Failure Family: Harness CDP / SPA navigation / conversation selection / hydration observation.
- Minimal Fix: Harness-only preference and post-reload hydration observation changes in [overnight_stable_baseline_browser.py](../../scripts/dev/overnight_stable_baseline_browser.py) and [round1_long_session_v2.py](../../scripts/dev/round1_long_session_v2.py).
- Production change: none.
- Regression Asset: [reload observation evidence](../../.runtime/round1-final-v2/l3-fresh-reload-observation-after-hydration-fix-20260825.json).
- Case-specific: NO.

### Test fixture issue

The original L2 debug fixture referenced a failed objective that was not created by its own preceding turns. It was classified as SETUP_INVALID and not patched into production.

### Production modified files

Only scoped changes required for the above fixes were added on top of the existing dirty worktree:

- apps/agent_api/greenbook_agent_api/services/conversation_runtime_adapter.py
- apps/agent_api/greenbook_agent_api/services/turn_coordinator.py
- apps/agent_api/greenbook_agent_api/main.py
- packages/agent_core/greenbook_agent_core/command/target.py
- packages/agent_core/greenbook_agent_core/task/manager.py
- tests/unit/test_partial_retry_closure.py
- tests/unit/test_task_delta_target_resolution.py
- tests/unit/test_completion_recovery_invariants.py
- tests/unit/test_task_manager.py
- scripts/dev/round1_long_session_v2.py
- scripts/dev/overnight_stable_baseline_browser.py
- zhiguang-fe/src/components/agent/SemanticConfirmationCard.tsx

The worktree already contained extensive historical modifications and deletions. No reset, clean, checkout, restore, broad revert, or unrelated cleanup was performed.

## F. Safety

Scope for the live Browser safety aggregate: 58 observed turns from the L1 baseline artifact, L2 debug/resume, L2 fresh attempt, L3 debug/resume, and L3 fresh certification. These artifacts contain 44 observed physical write steps; all 44 were recorded as COMPLETED. The same business actions appear in debug and fresh evidence, so this is an evidence-sample count, not a deduplicated production-operation count.

| Safety metric | Numerator / denominator | Result |
|---|---:|---|
| Unsafe Physical WRITE | 0 / 44 write steps | PASS |
| Wrong Resource | 0 / 58 turns | PASS |
| Wrong Temporal | 0 / 58 turns | PASS |
| Duplicate WRITE | 0 / 58 turns | PASS |
| False Success | 0 / 58 turns | PASS |
| Objective Resurrection | 0 / 58 turns | PASS |
| Blind RESULT_UNKNOWN Retry | 0 / 1 RESULT_UNKNOWN | PASS |
| Approval Wrong Action | 0 / 8 approvals | PASS |
| Context Contamination | 0 / 58 turns | PASS |
| Java / UI mismatch | 0 / 58 truth observations | PASS |
| Internal ID leak in observed UI | 0 / 58 turns | PASS |
| Business side effect without required HITL | 0 observed | PASS |

Expected failure injection:

- 3 clean FAIL_BEFORE evidence records were inspected across L2/L3 fresh fault evidence.
- All had no downstream request and no side effect.
- All were marked safe_to_retry=true by the fault boundary.
- Expected failure injection is not counted as Product Failure or unsafe semantic behavior.

RESULT_UNKNOWN:

- One ACK-loss / RESULT_UNKNOWN path was observed in the fault evidence.
- Authoritative reconciliation completed 1/1.
- Java truth contained the resulting draft.
- Blind retry: 0.

Provider residual:

- Offline semantic benchmark unsafe semantic outputs: 8/78.
- Contained by the fail-closed deterministic boundary: 8/8.
- System unsafe semantic paths: 0/78.
- Unsafe Containment Rate for this offline provider-error denominator: 8/8 = 100.0%.
- This containment rate does not claim that every offline semantic sample executed a business write; the benchmark is a provider-sample layer.

## G. Semantic Evaluation

Source layer: DETERMINISTIC semantic benchmark with 60 primary cases and 78 utterance variants. Artifact: [semantic report](../../artifacts/overnight_semantic_20260825/report.json) and [semantic results](../../artifacts/overnight_semantic_20260825/results.json). This is separate from LIVE_BROWSER L2/L3 results.

| Metric | Numerator / denominator | Result |
|---|---:|---|
| Core Intent Accuracy | 68 / 78 | 87.18% |
| Strict semantic exactness | 55 / 78 | 70.51% |
| Primary strict exactness | 43 / 60 | 71.67% |
| Primary Core Intent Accuracy | 55 / 60 | 91.67% |
| Goal Count Accuracy | 71 / 75 explicit-count cases | 94.67% |
| Goal Ownership Accuracy | dedicated benchmark field unavailable; live L3 owner-binding proxy 4 / 4 | proxy PASS; insufficient dedicated sample |
| Target Grounding Accuracy | 70 / 78 | 89.74% |
| Temporal Resolution Accuracy | 75 / 78 | 96.15% |
| Publication Intent Accuracy | 74 / 78 | 94.87% |
| Action / Query / Chat Routing Accuracy | 63 / 78 | 80.77% |
| Required Clarify Recall | 17 / 19 | 89.47% |
| Unnecessary Clarify Rate | 3 / 59 non-required cases | 5.08% |
| Missing Clarify Rate | 2 / 19 required cases | 10.53% |
| Normalization Drift Rate | 0 / 78 | 0.00% |
| Paraphrase Consistency | 3 / 6 groups | 50.00% |
| Multi-objective semantic accuracy, core | 7 / 8 | 87.50% |
| Multi-objective semantic accuracy, strict | 5 / 8 | 62.50% |
| Cross-turn reference, core | 6 / 8 | 75.00% |
| Cross-turn reference, strict | 3 / 8 | 37.50% |
| Provider Unsafe | 8 / 78 | 10.26% |
| Provider Unsafe but Contained | 8 / 8 unsafe samples | 100.00% |
| System Unsafe | 0 / 78 | 0.00% |

Deterministic failure distribution:

- wrong_action: 15
- wrong_target: 8
- wrong_goal_split: 5
- wrong_publication: 4
- wrong_time: 3
- constraint_lost: 3
- constraint_violation: 3
- missing_clarification: 2
- unnecessary_clarification: 3
- UNKNOWN_should_have_been_used: 2

The largest failure clusters were paraphrase wrong_action, ambiguous/incomplete wrong_action, and cross-turn constraint/target errors. No prompt tuning was performed.

## H. Context

### Live long-session context

In the fresh L3 evidence, scoped task count evolved as 0 → 1 → 2 → 3 → 4 and remained 4 for T5–T8. Objective count remained 0 in the exposed evidence projection because the runner records task-level context at this boundary. This is evidence of bounded task selection for this scenario, not a claim that all hidden context fields are fully instrumented.

- Cross-turn target success in fresh L3: 8/8 turns.
- Resource owner binding in the fresh L3 lifecycle: 4/4 topic resources stayed attached to their own objective lineage.
- Context contamination: 0/58 live Browser turns.
- Historical-resource mis-selection: 0/58 live Browser turns.
- Terminal-resource mis-selection: 0/58 live Browser turns.
- Reference candidate cardinality accuracy: dedicated live numerator not exposed; deterministic candidate safety passed for the observed ambiguous cases.
- Multi-resource Clarify accuracy: 1/1 observed live candidate-selection interaction; the historical omission before Bug 1 fix is recorded as a failure snapshot, not hidden.
- Verified outcome recall: 58/58 evidence rows contain Java/UI truth snapshots; a separate field-level recall denominator is unavailable.

### Projection size

The deterministic scoped-context measurement covered all 78 utterances:

- Average: 1,405.85 characters
- P50: 1,278 characters
- P95: 4,960 characters
- Max: 5,213 characters
- Old snapshot average: 1,357.87 characters
- Old snapshot max: 4,498 characters

The scoped projection is bounded per benchmark case, but the 78-case benchmark is not a single 20-turn conversation. The fresh L3 task-count trace is the long-session boundedness evidence available tonight.

### Context latency and token availability

- Context assembly, 55 product telemetry samples: P50 5.460 s, P95 6.361 s.
- Relevant resource count: unavailable as a dedicated per-turn metric.
- Relevant objective count: exposed task-level value only; full objective projection count unavailable.
- Reference evidence count: unavailable.
- Input/context/output token split for live Browser turns: unavailable.
- The offline provider benchmark recorded 449,089 input tokens and 20,435 output tokens over 78 calls; this is provider-sample cost data, not a live Browser context-token measurement.

## I. Performance

### Measurement boundary

- Browser observations: 58 turns.
- Product telemetry with T_TOTAL: 55 turns.
- Reload/no-run observations without product telemetry: 3 turns.
- PRODUCT_EXECUTION_TIME uses run.performance.total_latency_ms.
- HARNESS_OVERHEAD is estimated as elapsed Browser time minus product telemetry, clamped at zero; it is not attributed to Agent.
- T_FRONTEND_SUBMIT, T_API_ADMISSION, resolver-only time, Java request/business split, verification, reconciliation, projection, and frontend hydration are not fully instrumented. They are marked unavailable rather than reconstructed.

### Overall product and Harness time

| Measure | Product execution | Harness overhead estimate |
|---|---:|---:|
| Samples | 55 | 55 |
| P50 | 32.815 s | 18.723 s |
| P75 | 49.554 s | 25.036 s |
| P90 | 59.837 s | 31.540 s |
| P95 | 66.832 s | 41.948 s |
| Max | 236.862 s | 131.742 s |
| Mean | 44.078 s | 20.199 s |

### Workload breakdown

Workload labels below are inferred from the natural utterance and runner policy. Small samples are explicitly marked insufficient.

| Workload | N | P50 | P95 | Mean | Max |
|---|---:|---:|---:|---:|---:|
| CHAT | 1 | 17.878 s | INSUFFICIENT_SAMPLE | 17.878 s | 17.878 s |
| SEARCH | 4 | 55.352 s | 72.348 s | 55.198 s | 74.350 s |
| SUMMARIZE | 1 | 24.062 s | INSUFFICIENT_SAMPLE | 24.062 s | 24.062 s |
| GENERATE_DRAFT | 11 | 50.556 s | 62.125 s | 51.597 s | 63.610 s |
| UPDATE_DRAFT | 7 | 26.452 s | 30.908 s | 26.118 s | 32.367 s |
| SCHEDULE | 7 | 27.314 s | 175.866 s | 57.783 s | 236.862 s |
| UPDATE_SCHEDULE | 6 | 29.593 s | 32.145 s | 29.516 s | 32.236 s |
| CANCEL_SCHEDULE | 7 | 31.557 s | 38.748 s | 31.954 s | 41.291 s |
| PUBLISH_NOW | 5 | 33.679 s | 47.700 s | 34.939 s | 49.409 s |
| MULTI_OBJECTIVE | 6 | 51.392 s | 147.257 s | 71.870 s | 176.798 s |
| VIEW | 0 | unavailable | unavailable | unavailable | unavailable |
| APPROVAL-only wait | no isolated product sample | unavailable | unavailable | unavailable | unavailable |
| SEMANTIC_CONFIRMATION | no isolated product sample | unavailable | unavailable | unavailable | unavailable |
| RESULT_UNKNOWN | no isolated product sample | unavailable | unavailable | unavailable | unavailable |
| RECONCILIATION | no isolated product sample | unavailable | unavailable | unavailable | unavailable |
| RELOAD/RESUME | 3 UI/Harness observations | unavailable | unavailable | unavailable | unavailable |

### Available stage telemetry

| Stage | N | P50 | P95 | Mean | Max | Median share of product total | Classification |
|---|---:|---:|---:|---:|---:|---:|---|
| Context assembly | 55 | 5.460 s | 6.361 s | 5.671 s | 11.600 s | 16.64% | PRODUCT |
| LLM semantic | 55 | 11.837 s | 12.744 s | 14.005 s | 138.149 s | 36.07% | PROVIDER / PRODUCT boundary |
| ActionLoop pre-submit | 44 | 3.220 s | 20.393 s | 7.094 s | 22.743 s | 9.81% | PRODUCT orchestration |
| Queue wait | 55 | 0.413 s | 5.897 s | 1.655 s | 6.350 s | 1.26% | PRODUCT / queue I/O |
| Tool/MCP | 55 | 0.285 s | 7.180 s | 1.816 s | 8.170 s | 0.87% | EXTERNAL I/O |
| Creator LLM, non-zero calls | 17 | 10.739 s | 14.643 s | 11.143 s | 14.959 s | not comparable | PRODUCT / PROVIDER |
| Resolver-only | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | not instrumented |
| Worker-only | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | not instrumented |
| Java request/business | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | field was not attributed; zero is not reported as latency |
| Verification | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | not instrumented |
| Reconciliation | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | not instrumented |
| Projection | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | not instrumented |
| Frontend hydration | unavailable | unavailable | unavailable | unavailable | unavailable | unavailable | Harness observation only |

### Top 5 latency contributors

1. LLM semantic: P50 11.837 s, P95 12.744 s, median 36.07% of product total; affected all semantic workloads; PROVIDER/PRODUCT boundary.
2. Context assembly: P50 5.460 s, P95 6.361 s, median 16.64%; PRODUCT.
3. ActionLoop pre-submit: P50 3.220 s, P95 20.393 s, median 9.81%; PRODUCT orchestration.
4. Creator LLM on its 17 non-zero write samples: P50 10.739 s, P95 14.643 s; concentrated in GENERATE_DRAFT; PRODUCT/PROVIDER.
5. Tool/MCP tail: P50 0.285 s, P95 7.180 s; EXTERNAL I/O.

Queue wait is an additional tail contributor at P95 5.897 s. No performance refactor was started.

## J. UX

UX evidence combines live Browser observation, static Agent-panel review, and the Web Interface Guidelines review. Core Agent outcomes:

| UX metric | Numerator / denominator | Verdict | Evidence |
|---|---:|---|---|
| Internal ID leak | 0 / 58 live turns | PASS | runner ui_internal_leak |
| Raw SVG text leak | 0 observed | PASS | static icon scan; no raw SVG text in Agent panel |
| Redundant status patterns | 2 observed patterns | PARTIAL | result-specific status followed by generic completed status |
| Missing long-operation progress | 0 observed | PASS | AgentActivityCards progress states |
| Clarify readability | 1 / 1 live selection | PASS | human-readable title/type/status/time; no IDs |
| Semantic confirmation readability | static PASS; dynamic card-required denominator unavailable | PARTIAL | human labels and side-effect copy present; no dynamic click sample recorded |
| Approval readability | 8 / 8 observed approval actions | PASS | real DOM clicks on understandable approval labels |
| Auto-refresh | API projection observed; direct DOM numerator unavailable | PARTIAL | truth snapshots updated, visual refresh not separately instrumented |
| Reload consistency, API | 1 / 1 | PASS | message projection remained in the same conversation |
| Reload consistency, visible UI | 0 / 1 proven | PARTIAL | final Harness observation produced AgentPanel did not open |
| User-recoverable expected failure | 3 / 3 fault boundaries | PASS | user-facing retry copy; internal fault text not shown in UI |
| Button/card state correctness | 58 / 58 no internal leak or stale approval action | PASS | runner UI observations |

Positive UI details:

- Agent dialog has dialog semantics, modal labeling, focus handling, and an accessible close control.
- Composer has a labeled textarea, keyboard Enter/Shift+Enter behavior, and a labeled send button.
- Activity updates use a live status region.
- Clarify, semantic confirmation, and approval cards use user-readable action/resource/time language.
- The mojibake cancel label in [SemanticConfirmationCard.tsx](../../zhiguang-fe/src/components/agent/SemanticConfirmationCard.tsx) was corrected to 取消; Frontend tests and build passed.

Non-blocking static findings outside the core Agent panel:

- [EditProfilePage.tsx](../../zhiguang-fe/src/pages/EditProfilePage.tsx) uses a div with role=button without a keyboard handler for the avatar action.
- [CourseDetailPage.tsx](../../zhiguang-fe/src/pages/CourseDetailPage.tsx) uses a direct locale date conversion instead of the recommended Intl formatting path.

These were not changed because tonight was an evaluation and minimal-fix window, not a broad Frontend redesign.

## K. Final Verdict

| Certification | Verdict | Reason |
|---|---|---|
| GREENBOOK_CONTEXT_PHASE2_VALIDATED | YES | existing validated baseline retained; no Context production changes were made |
| GREENBOOK_A_L1_VALIDATED | YES | existing 20/20 fresh L1 baseline retained |
| GREENBOOK_A_L2_VALIDATED | NO | fresh L2 stopped at 8/14 with a real projection/runtime failure; no second fresh run |
| GREENBOOK_A_L3_VALIDATED | NO, strict gate | fresh L3 runtime/business path 8/8 PASS, but visible reload hydration was not proven by the final Harness observation |
| GREENBOOK_A_LONG_SESSION_BASELINE_VALIDATED | NO | L2 fresh incomplete and strict L3 reload UX gate partial |

The L3 runtime/business sub-certificate is PASS; the strict full-stack certification remains NO until the reload-visible UI observation is reliable.

## L. Tomorrow Top 3

1. Verify Bug 2 with a new controlled L2 fresh certification after the aggregate Task projection fix; confirm the valid sibling publish/update path from a fresh conversation.
2. Repair and isolate the reload conversation selection/hydration Harness, then re-prove visible message rendering and strict L3 reload consistency.
3. Analyze the deterministic semantic clusters for cross-turn constraints, ambiguous routing, and paraphrase action drift; keep Provider residuals separated from System safety and do not use case-specific patches.

No Top 3 item was started automatically tonight.

# Agent Evaluation Summary

| Category | Metric | Before | After | Sample Size | Verdict | Evidence |
|---|---|---|---|---:|---|---|
| Semantic | Core intent | not previously comparable | 68/78 = 87.18% | 78 utterances | MEASURED | semantic report |
| Semantic | Strict exact | not previously comparable | 55/78 = 70.51% | 78 | MEASURED | semantic report |
| Context | Cross-turn target success | pre-fix L3 T4 unresolved | 8/8 fresh L3 turns | 8 | PASS for scenario | L3 fresh artifact |
| Context | Context contamination | existing L1 zero | 0/58 | 58 live turns | PASS | five live artifacts |
| Objective | Failed sibling isolation | T4 clarification blocker | T4 replay + T5–T8 continuation completed | 1 replay + 4 continuation turns | PASS targeted | L3 debug artifacts |
| Objective | Terminal resurrection | no prior issue | 0/58 | 58 | PASS | live safety aggregate |
| Tool | Physical write completion | not comparable | 44/44 COMPLETED | 44 write steps | PASS | live evidence |
| Runtime | Resume success | existing L1 baseline | L3 1/1 continuation; L2 resumed path 13/14 terminal non-failed | L2/L3 | PARTIAL overall | L2/L3 artifacts |
| Runtime | RESULT_UNKNOWN reconciliation | not previously measured | 1/1 reconciled | 1 | PASS | fault evidence and Java truth |
| HITL | Clarify recall | not previously measured | 17/19 | 19 required cases | PARTIAL | semantic report |
| HITL | Approval action | not previously measured | 8/8 approved, 0 wrong/duplicate | 8 | PASS | Browser click evidence |
| Safety | Unsafe physical WRITE | L1 0 | 0/44 | 44 writes | PASS | live evidence |
| Safety | Wrong resource / temporal | L1 0 | 0/58 / 0/58 | 58 | PASS | live evidence |
| Business Truth | Java/UI mismatch | L1 0 | 0/58 | 58 truth observations | PASS | Java/UI snapshots |
| Full-stack | L3 fresh runtime/business journey | not certified | 8/8 | 8 turns | PASS | L3 fresh artifact |
| Full-stack | Strict visible reload | not proven | 0/1 UI-render proof | 1 reload observation | PARTIAL | Harness reload artifact |
| Long-session | L1 completion | baseline PASS | 20/20 | 20 | PASS | L1 artifact |
| Long-session | L2 fresh completion | not completed | 8/14 | 14 expected | FAIL | L2 fresh artifact |
| Long-session | L3 fresh completion | not completed | 8/8 | 8 | PASS runtime; partial strict UI | L3 fresh artifact |
| Recovery | Target resolver fix | T4 unresolved | targeted replay PASS | 1 bug family | PASS | unit + L3 replay |
| Recovery | Aggregate projection fix | T8 failed | unit regression PASS; fresh replay unavailable | 1 bug family | PARTIAL | unit artifact; fresh stop rule |
| Performance | Product P50/P95 | L1 baseline 55.19/94.42 s | 32.815/66.832 s across 55 product samples | 55 | MEASURED | derived from live artifacts |
| Performance | Harness overhead P50/P95 | not separated | 18.723/41.948 s | 55 | MEASURED | derived from live artifacts |
| UX | Internal leak | L1 zero | 0/58 | 58 | PASS | runner |
| UX | Reload visible consistency | not proven | 0/1 | 1 | PARTIAL | Harness reload artifact |

## Resume-ready Verified Metrics

Each metric below has an artifact, sample size, and explicit measurement definition.

- Semantic benchmark: 60 primary cases / 78 utterances; strict exact 55/78 and core intent 68/78. Source: [semantic report](../../artifacts/overnight_semantic_20260825/report.json). Definition: deterministic evaluator result over the frozen dataset.
- Live long-session coverage: L1 20 turns, L2 debug/resume 14 turns, L3 fresh 8 turns. Sources: [L1](../../.runtime/round1-final-v2/l1-context-phase2-fresh-certification-20260824.json), [L2](../../.runtime/round1-final-v2/l2-t14-resumed-after-reload-20260825.json), [L3](../../.runtime/round1-final-v2/l3-fresh-certification-20260825.json). Definition: real Frontend Browser turns in the canonical conversation.
- Real business writes: 44 observed physical write steps, all 44 recorded COMPLETED. Source: the five live artifacts listed in section F. Definition: evidence.physical_write.steps across the Browser runs; duplicate debug/fresh evidence is not deduplicated.
- Safety: wrong resource 0/58, wrong temporal 0/58, duplicate write 0/58, unsafe physical write 0/44, false success 0/58. Source: live evidence aggregate. Definition: runner flags and physical-write evidence.
- RESULT_UNKNOWN recovery: 1/1 reconciled, blind retry 0/1. Source: [fault evidence](../../.runtime/round1-final-v2/l3-fault-evidence-20260825.jsonl). Definition: authoritative Java reconciliation after ACK loss.
- Clarify recall: 17/19 required cases; unnecessary 3/59 non-required cases. Source: semantic report. Definition: deterministic expected-vs-observed clarification boundary.
- Target grounding: 70/78 deterministic utterances. Source: semantic report. Definition: evaluator target-resolution outcome against case truth.
- Context projection size: P50 1,278, P95 4,960, max 5,213 characters over 78 utterances. Source: semantic results. Definition: serialized scoped interpreter projection length.
- Long-session boundedness: fresh L3 task count grew 0→4 and remained 4 from T4 through T8. Source: L3 fresh evidence. Definition: exposed scoped task-count trace.
- Product latency: P50 32.815 s, P95 66.832 s over 55 product telemetry samples. Source: five live Browser artifacts. Definition: run.performance.total_latency_ms.
- Harness overhead: P50 18.723 s, P95 41.948 s over 55 samples. Source: same artifacts. Definition: Browser elapsed time minus product telemetry, clamped at zero.
- Java/UI truth consistency: 0 mismatches over 58 truth observations. Source: live evidence. Definition: no java/ui mismatch flag and matching business snapshots.

# Evaluation Engineering Summary

1. LLM output alone is insufficient: a semantically plausible response can still choose the wrong resource, temporal value, lifecycle, or physical action.
2. Semantic, Runtime, Business Truth, and Full-stack layers expose different failure classes and must keep separate denominators.
3. Bad cases were discovered through natural Browser journeys, deterministic semantic cases, partial-failure injections, and authoritative Java/MySQL/Scheduler reconciliation.
4. FIRST BAD STATE was located by comparing provider semantic output, normalized semantic state, target candidates, objective projection, execution state, and Java truth.
5. Snapshot Replay reduces the cost of long-session debugging: verified turns stay checkpointed and only the failed turn is replayed.
6. Failure Family Regression converts a case into an invariant: failed-objective retry uses bounded historical context, while terminal filters still prevent resurrection.
7. Side-effect safety was verified from physical-write evidence and Java truth, not from Agent COMPLETED alone; 44 observed write steps were completed without unsafe or duplicate mutation.
8. Long-session Context was evaluated through scoped projection size, task-count growth, candidate selection, and contamination flags; the available L3 trace remained bounded at four tasks.
9. Provider semantic errors were separated from System unsafe behavior: 8 offline unsafe semantic samples were contained with zero live unsafe physical writes and zero System Unsafe cases.
10. The main measured performance contributors are LLM semantic time, Context assembly, ActionLoop orchestration, Creator LLM on write paths, and Tool/MCP tail latency; Java/verification/projection splits remain instrumentation gaps.

The evaluation is complete for tonight. No new development phase, performance refactor, Prompt tuning, Context redesign, or architecture experiment was started.
