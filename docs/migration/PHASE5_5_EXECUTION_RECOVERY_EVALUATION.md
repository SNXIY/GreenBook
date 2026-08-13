> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# Phase 5.5 Execution Finalization & Agent Evaluation

## 1. Goal

Phase 5.5 closes the Intelligence Layer to Reliable Execution boundary. A
resolved `ExecutionInput` is now the only canonical queue payload. Resume and
recovery use the existing durable Task/Execution/Checkpoint/Ledger/Artifact
state, and evaluation measures runtime behavior rather than generated prose.

## 2. Execution Contract Before

The canonical path already produced a plan, but direct and queued callers
could still carry a `TaskIntent` projection or an embedded executable-plan
dictionary. `RuntimeAgentService`, `ArgumentBinder`, and
`CapabilityExecutor` therefore had to distinguish old and new envelopes.

## 3. Execution Contract After

```text
Command -> GoalTree -> TaskManager -> AgentLoop
  -> DynamicPlanner / ToolSelector -> ToolPolicyGate
  -> GoalCompiler -> ExecutionInput
  -> ExecutionSubmissionService -> ExecutionQueue -> Worker
  -> ToolRuntime / MCP -> Community services
```

`ExecutionInput` contains resolved step facts: selected tool, validated
arguments, dependencies, artifact references, execution mode, policy snapshot,
idempotency key, and trace context. Queue consumers do not receive the user
message or an Intent object.

## 4. ExecutionInput

Added `ExecutionStepInput` and expanded `ExecutionInput` in
`packages/assistant_core/greenbook_assistant_core/execution/input.py`.
`ExecutionInput.from_executable_plan()` is the compiler at the intelligence
boundary; `to_executable_plan()` only rebuilds the protected execution graph.
The request uses `extra="forbid"`, rejects old envelopes without resolved
steps at `execute_queued()`, and carries the selected `ToolMetadata` policy
snapshot when a registry is available.

Worker state now persists `tool_name`, resolved `arguments`, execution mode,
policy snapshot, and stable idempotency keys on each `StepExecution`.

## 5. Removed TaskIntent / IntentSpec

The queue and execution infrastructure no longer imports or reconstructs
`TaskIntent`/`IntentSpec`. `ArgumentBinder` accepts `ExecutionInput` for the
canonical path and performs schema filtering, typed conversion, artifact
binding, and timezone normalization only. It does not infer arguments from
user text on that path. `CapabilityExecutor` receives an explicit selected
tool and fails when a multi-tool capability has no selected tool.

The old direct API test/embedding boundary still accepts a resolved
`TaskContext.task_intent` field; it is not used by ConversationRuntime and is
documented as the remaining compatibility caller in section 19.

## 6. RuntimeAgentService Cleanup

`execute_queued()` now accepts only `payload.execution_input` with resolved
steps. The old `IntentSpec -> TaskIntent -> Worker` queue projection was
removed. `_execution_dispatch_payload()` serializes a typed execution request
and strips access tokens and raw user text. `submit_plan()` remains the
queue-native entry for multi-step, side-effecting, long-running, and
recoverable work; read-only synchronous tools remain behind the policy gate.

## 7. Orchestrator / Template Removal

The canonical Conversation Runtime uses `GoalCompiler` and does not select a
business template. The old `TaskOrchestrator`/template route remains only for
the explicit resolved-context direct execution compatibility method, whose
callers are existing runtime regression tests and embedders that construct a
`RuntimeContext` directly. It is not reachable from the API conversation path.
The next cleanup can remove it after those direct callers are migrated to
`submit_plan()`.

## 8. Resume Architecture

`agent/recovery.py` adds `ResumeContext` and `AgentRecoveryService`. It joins
the current Task, latest Plan/Goal versions, `PlanExecution`, checkpoint
projection, artifacts, memory IDs, and trace context into a bounded AgentLoop
projection. `AgentLoop.run(resume_context=...)` restores completed goals and
steps, exposes the last observation summary, and does not append an unbounded
history to `AgentState`.

## 9. Recovery State Machine

`AgentRecoveryService` emits one typed `RecoveryKind`:

| Durable evidence | Recovery action |
| --- | --- |
| Terminal Task/Execution | `ABORT_TASK` |
| Human approval/input required | `WAIT_FOR_HUMAN` |
| External operation pending | `WAIT_FOR_EXTERNAL` |
| Retryable failed step within budget | `RETRY_STEP` |
| Non-retryable failed step | `REPLAN_FROM_FAILURE` |
| Running/paused/pending checkpoint | `RESUME_EXECUTION` |

No recovery branch depends on message keywords.

## 10. Idempotent Recovery

The Worker checkpoints the successful tool result and output artifact before
marking the step complete. On restart, it finalizes that checkpointed success
without invoking the external tool again. `IdempotentRecoveryGuard` also
consults the existing `ToolExecutionLedger` for a result recorded immediately
before a process crash. This protects successful side effects such as
`publish_post` from duplicate delivery.

## 11. Correction Learning

`command/correction.py` adds the typed `CorrectionEvent` contract. The
canonical `MemoryManager.remember_correction()` applies `MemoryWritePolicy`
and stores only a bounded correction summary, target evidence, and whether the
event is a preference candidate. It never stores hidden reasoning or raw CoT.
The command/goal layer remains responsible for applying the current-turn
correction; long-term storage is optional and policy-controlled.

## 12. Evaluation Architecture

`packages/evaluation/greenbook_evaluation/` now contains:

- `models.py`: `EvalCase`, `EvalResult`, trace IDs, failure taxonomy, and
  behavioral metrics.
- `dataset.py` and `cases/community.py`: twelve GreenBook community golden
  cases, including multi-turn references, preemption, replan, creator failure,
  approval/cancel, memory recall, and idempotent publication recovery.
- `runner.py`: `EvaluationRunner` with injected deterministic fake LLM/tool
  handlers or an integration runtime. It returns per-case checks, the shared
  Runtime trace identifiers, and failure categories.
- `metrics.py`: `EvaluationMetricsCalculator` and `AgentMetricsCalculator`.

The older execution evaluator remains available for persisted execution
regression measurements; the behavioral runner is the Phase 5.5 entry point.

## 13. Evaluation Metrics

The framework reports command accuracy, target resolution accuracy, goal
decomposition accuracy, tool selection accuracy, task completion rate, plan
success, replan recovery, clarification precision, side-effect safety,
idempotent recovery, memory retrieval precision, context continuity, latency,
and tool-call count. It does not use BLEU/ROUGE as a proxy for agent quality.

## 14. Golden Cases

The dataset covers:

1. Java learning article creation.
2. Search, create, and schedule as one multi-goal Task.
3. “Just created article” modification.
4. Java-vs-Python target selection.
5. Ambiguous target clarification.
6. Task B preemption and Task A resume.
7. Search failure replan.
8. Creator failure alternative strategy.
9. Successful publish followed by process restart without duplicate publish.
10. Cancellation of a waiting schedule.
11. Cross-conversation concise-style preference recall.
12. Long-context continuity under a bounded context budget.

## 15. Badcase Taxonomy

The runner classifies failures as `COMMAND_ERROR`, `TARGET_ERROR`,
`GOAL_ERROR`, `PLAN_ERROR`, `TOOL_SELECTION_ERROR`, `TOOL_ARGUMENT_ERROR`,
`POLICY_ERROR`, `EXECUTION_ERROR`, `RECOVERY_ERROR`, `MEMORY_ERROR`,
`HALLUCINATION`, `UNNECESSARY_CLARIFICATION`, and
`MISSING_CLARIFICATION`.

## 16. Deleted Files

| Path | Old responsibility | Replacement | Reason |
| --- | --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/agent_memory/extractor.py` | Old procedural-memory import surface | `memory/extractor.py` | No production caller remained. |
| `packages/assistant_core/greenbook_assistant_core/agent_memory/manager.py` | Old MemoryManager import surface | `memory/manager.py` | Canonical manager is now used by API/Worker. |
| `packages/assistant_core/greenbook_assistant_core/agent_memory/models.py` | Duplicate model names | `memory/models.py` | Canonical durable model owns storage fields. |
| `packages/assistant_core/greenbook_assistant_core/agent_memory/store.py` | Duplicate in-memory repository | `memory/repository.py` | Repository protocol is the single source of truth. |
| `packages/assistant_core/greenbook_assistant_core/agent_memory/strategy.py` | Duplicate strategy retriever | `memory/strategy.py` | Retrieval behavior was merged into `memory/`. |
| `apps/assistant_api/greenbook_assistant_api/services/query_handler.py` | Uncalled IntentSpec read-only handler | Goal/AgentLoop/ToolPolicyGate | No caller existed; it duplicated tool selection. |
| `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py` | Legacy Runtime/Legacy wrapper | ConversationRuntimeAdapter | No production caller existed. |
| `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py` | Legacy-vs-Runtime path switch | ConversationRuntimeAdapter | Canonical runtime no longer needs a business router. |
| `tests/unit/test_runtime_router.py` | Retired routing behavior tests | Canonical runtime tests | Tests verified deleted architecture. |

## 17. Directory / Naming Cleanup

The `agent_memory/` compatibility package is now empty and is no longer part
of the import surface. Tests were migrated to `memory/`. `ExecutionInput` is
the named intelligence-to-execution contract; `TaskIntent` is not a queue
payload name. Tool policy snapshots are copied from `ToolMetadata`; capability
metadata remains a semantic index.

`pyproject.toml` now declares `pyyaml` as a test dependency and defines the
workspace sources needed by `uv lock`.

## 18. Test Results

After the contract and recovery changes:

- `pytest --collect-only -q`: 665 tests collected successfully.
- `pytest -q`: 663 passed, 2 skipped.
- Phase 5.5 targeted contract/recovery/evaluation tests: passed.
- Touched-module Ruff checks: passed.
- Touched-module `compileall`: passed.

The remaining warnings are an existing Starlette/httpx deprecation warning,
an unregistered `integration` marker, and a Windows permission warning when
pytest tries to update the existing `.pytest_cache` directory.

## 19. Remaining Technical Debt

1. `RuntimeAgentService.execute()` still supports direct callers that build a
   resolved `TaskContext` with the historical `task_intent` field. It is used
   by runtime regression tests and direct embedders, not by
   `ConversationRuntimeAdapter`. Migrate those callers to `submit_plan()` and
   delete the branch in the next runtime cleanup.
2. `orchestration/orchestrator.py`, `orchestration/context.py`, and
   `orchestration/templates.py` remain behind that direct compatibility branch.
   They must be removed together after the caller migration; splitting them
   would leave a second planning path.
3. The legacy evaluation-only `EvalRunner` and its IntentSpec datasets remain
   for historical quality snapshots. They do not run in the production path;
   future evaluation work should use `EvaluationRunner` and the Goal/Command
   golden cases.
4. `task/models.py` still exposes historical `TaskIntent` fields for direct
   regression fixtures. No new queue or Worker contract depends on them.

## 20. Next Phase

Use the canonical `ContextSnapshot`, durable Memory, `ResumeContext`, and
behavioral evaluation traces to improve context continuity and recovery
quality. Do not add another Intent, Planner, Agent wrapper, or execution
payload format.
