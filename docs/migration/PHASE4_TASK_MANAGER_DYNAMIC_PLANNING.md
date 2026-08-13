> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# GreenBook Agent Runtime v2
# Phase 4 TaskManager & Dynamic Planning

## 1. Goal

Phase 4 establishes one durable Task lifecycle boundary above the existing
Reliable Execution Runtime. A Task now owns a root Goal, GoalTree projection,
Plan versions, execution references, priority, and deterministic state
transitions. AgentLoop may request a replan, but it cannot bypass Task
persistence, policy checks, Queue, Worker, Retry, Checkpoint, Ledger, or MCP.

## 2. Architecture Before

The canonical Command and Goal paths existed, but durable work still crossed
several legacy projections:

```text
Command -> GoalTree -> AgentLoop -> GoalCompiler -> callback/graph execution
                 \-> TaskCommand / IntentSpec / TaskProvider projections
```

Planner output was effectively static for a run. Task lifecycle state was
spread between `TaskProvider`, execution projections, and compatibility
models. Tool metadata described tools, but permission, approval, and queue
mode were not yet a single AgentAction gate.

## 3. Architecture After

```text
User/API
  -> CommandInterpreter -> Command -> TargetResolver
  -> GoalDecomposer -> GoalTree
  -> TaskManager -> TaskRepository
  -> AgentLoop (Observe -> Reason -> Act -> Reflect)
       |-> DynamicPlanner -> typed PlanningDecision -> versioned Goal/Plan
       |-> ToolSelector -> ToolMetadata -> ToolPolicyGate
       |       |-> sync read-only ToolRuntime
       |       \-> approval / ExecutionSubmissionService
       \-> GoalCompiler -> TaskPlan
             -> ExecutionSubmissionService
             -> existing Execution Queue / Worker / Retry / Checkpoint / Ledger
             -> ToolRuntime / MCP / external systems
```

`CallbackExecutionSubmissionService` is an integration boundary around the
existing API Runtime service. It does not replace the existing queue or
worker; the configured Runtime service remains responsible for allocating an
Execution and selecting its reliable dispatch mode.

### Caller scan

| Module | Status | Evidence / boundary |
| --- | --- | --- |
| `agent.py` | DEAD | Deleted in Phase 3.5; no production import remains. |
| `agent_runtime/` | DEAD | Deleted in Phase 3.5; `AgentLoop` and Execution Worker are canonical. |
| `conversation/commands.py` | PARTIAL | Control, approval, preferences, and explicit API command payloads still use it; canonical message understanding uses `command/`. |
| `conversation/target_resolver.py` | DEAD | Deleted; `command/target.py` is the only facade. |
| `task/intent_spec_provider.py` | ACTIVE FALLBACK | Used by `_skip_multi` recovery and the compatibility TaskProvider path. It is no longer the canonical message entry. |
| `task/intent_compat.py` | ACTIVE FALLBACK | Converts the old execution projection for RuntimeAgentService and TaskProvider. |
| `task/reference_resolver.py` | DEAD | Deleted; evidence helpers now sit behind `TargetResolver`. |
| `task/multi_task.py` | PARTIAL | Legacy conversation segmentation/index projection remains in the adapter; new durable scheduling is `TaskManager`. |
| `task/decomposer.py` | DEAD | Deleted; Goal decomposition is `goal/decomposer.py`. |
| `TaskGraphBuilder` | PARTIAL | Only fallback graph recovery and graph-focused tests still call it; GoalCompiler is the canonical GoalTree compiler. |
| `orchestration/templates.py` | ACTIVE FALLBACK | Used by `TaskOrchestrator` for deterministic recovery/known workflows, not default Agent planning. |
| `orchestration/orchestrator.py` | ACTIVE FALLBACK | Existing RuntimeAgentService/Worker plan contract still consumes it. |
| `capability/mapper.py` | ACTIVE EXECUTION COMPATIBILITY | Legacy Plan/Execution mapping remains below GoalCompiler; no AgentLoop tool selection depends on it. |
| `CapabilityExecutor` | ACTIVE CORE | Reliable Execution Worker uses it; it is retained as execution infrastructure, not an Agent decision layer. |

## 4. Task vs Goal vs Plan vs Execution

- **Task**: one durable, user-owned work unit with lifecycle, priority,
  version, and resumable references.
- **Goal**: the desired result; a Task may contain one root Goal and many
  dependent child Goals.
- **Plan**: a versioned compilation of Goals into typed `TaskPlan` steps. A
  replan appends a `TaskPlanRevision`; it does not overwrite the old version.
- **Execution**: one reliable runtime instance of a Plan. Queue, Worker,
  retry, checkpoint, ledger, and external side-effect truth remain there.

## 5. Task Lifecycle

`TaskManager` owns the following states:

```text
CREATED -> PLANNING -> READY -> RUNNING
                             |-> WAITING_HUMAN
                             |-> WAITING_EXTERNAL
                             |-> PAUSED -> READY/RUNNING
                             |-> COMPLETED/FAILED/CANCELLED
```

`IN_PROGRESS` remains readable for the old execution projection, while new
mutations use `RUNNING`. Invalid transitions raise
`TaskStateTransitionError`; an LLM cannot emit a state mutation directly.
`TaskRepository` is a Protocol, `InMemoryTaskRepository` supports isolated
tests, and `TaskProviderRepository`/`TaskRegistryRepository` adapt the
existing SQL-backed Task store.

## 6. Dynamic Planning

`DynamicPlanner` receives `GoalTree`, `AgentState`, optional durable Task,
ToolMetadata catalog, execution history, and observations. It returns a
structured `PlanningDecision`:

`CONTINUE`, `INSERT_STEP`, `REMOVE`, `REORDER`,
`RETRY_WITH_NEW_ARGS`, `SELECT_ALTERNATIVE_TOOL`, `ASK_HUMAN`, `FINISH`, or
`ABORT`.

The LLM can propose only this typed decision. Applying a plan mutation is
deterministic and validates the resulting GoalTree. Tool execution is outside
the planner.

## 7. Replan Flow

After Reflect requests an adjustment (or an explicitly injected DynamicPlanner
observes a failed action), AgentLoop:

1. records the observation;
2. asks DynamicPlanner for a `PlanningDecision`;
3. applies and validates any GoalTree mutation;
4. appends a Task plan revision with the decision, reason, and observation;
5. increments Task `plan_version` and preserves the previous version;
6. returns to Observe for the next decision.

`ASK_HUMAN` becomes `WAITING_HUMAN`; `ABORT` becomes a failed run; the
planner never calls a ToolRuntime.

## 8. Multi-task Scheduling

`TaskManager` supports priority, active/queued/paused projections,
`preempt_for()`, pause/resume, and independent Task records. A high-priority
foreground Task can pause a running background Task. Resuming preserves its
`active_execution_id`, GoalTree version, and Plan history. This is a small
community-agent scheduler, not a second operating-system scheduler.

## 9. Tool Policy Gate

`ToolPolicyGate` evaluates the selected `ToolMetadata` before invocation.
Permission scopes, approval, side effects, destructive status, retry policy,
long-running work, and multi-step work determine `DENY`, `WAITING_HUMAN`,
`SYNC`, or `QUEUE`. `ToolSelector` proposes a tool; deterministic code decides
whether and how it may run. `ToolMetadata` now exposes canonical
`requires_approval`, `retry_policy`, and `timeout` fields while the older
`approval`/`retry` spellings remain synchronized projections for MCP callers.

## 10. Removed Legacy

No additional legacy module was deleted in Phase 4 because the remaining
modules below still have concrete callers. Deleting them now would remove a
reliable execution/control path rather than remove dead code.

| Path | Original responsibility | Replacement | Why not deleted yet |
| --- | --- | --- | --- |
| `conversation/commands.py` | Control/approval and old command projection | `command/` plus control contracts | `ConversationControlService`, `ApprovalRuntimeService`, and explicit API control payloads still consume it. |
| `task/intent_spec_provider.py` | Legacy IntentSpec fallback | `CommandInterpreter` + `GoalDecomposer` | `_skip_multi` recovery and compatibility tests still use the projection. |
| `task/understanding.py` | Intent provider fallback implementation | `goal/decomposer.py` | Called by the remaining IntentSpec provider. |
| `task/intent_compat.py` | IntentSpec-to-execution projection | `TaskManager` + `GoalCompiler` | Runtime service and TaskProvider still need one execution projection during migration. |
| `task/multi_task.py` | Conversation task segmentation/index projection | `TaskManager` + GoalTree | Adapter fallback still builds legacy graph segments. |
| `task/graph.py` / `TaskGraphBuilder` | Old message-to-graph recovery path | `goal/compiler.py` | GoalCompiler uses its graph model and the fallback has active callers. |
| `orchestration/templates.py` / `TaskOrchestrator` | Known-flow plan fallback | `DynamicPlanner` + `GoalCompiler` | Reliable legacy execution and recovery still use the deterministic recipe. |

These are now explicitly compatibility/recovery paths; the default main
composition creates a canonical TaskManager and uses Command + GoalTree +
AgentLoop first.

## 11. New Files

- `packages/assistant_core/greenbook_assistant_core/task/repository.py`
- `packages/assistant_core/greenbook_assistant_core/task/manager.py`
- `packages/assistant_core/greenbook_assistant_core/planning/dynamic.py`
- `packages/assistant_core/greenbook_assistant_core/toolruntime/policy.py`
- `packages/assistant_core/greenbook_assistant_core/execution/submission.py`
- `packages/assistant_core/greenbook_assistant_core/db/migrations/007_task_manager_dynamic_planning.sql`
- `tests/unit/test_task_manager.py`
- `tests/unit/test_dynamic_planner.py`
- `tests/unit/test_tool_policy_gate.py`

## 12. Changed Files

- Task models, SQL projection, and API TaskProvider now carry canonical
  lifecycle, GoalTree, execution, and Plan-version fields.
- GoalTree, TaskPlan, ExecutablePlan, and GoalCompiler carry version metadata.
- AgentState and AgentLoop accept durable Task, DynamicPlanner,
  ToolPolicyGate, TaskManager, and ExecutionSubmissionService boundaries.
- Default API composition wires `TaskManager` to the existing SQL Task store.
- Goal decomposition no longer exposes risk/approval/side-effect policy as
  LLM planning input; policy is evaluated from ToolMetadata at runtime.

## 13. Test Results

- Phase 4 additions plus AgentLoop regression: **15 new policy/task/planner
  tests passed**, and the combined targeted run completed **79 passed**.
- Focused legacy execution/runtime run: **74 passed**.
- Full `tests/unit` collection still stops at five pre-existing private API
  imports (`_run_projection_fields`, `_conversation_target_task`,
  `_close_request_db_session`, `_append_schedule_confirmation`); no Phase 4
  test fails during collection.
- Assistant core/API/contracts `compileall`: **passed**.
- Project `.venv\Scripts\ruff.exe` targeted check for all Phase 4 files and
  touched integration files: **passed**. The bare `ruff` command is not on
  PATH; repository-wide baseline findings remain documented separately.

## 14. Remaining Technical Debt

1. Move the adapter's `_skip_multi` graph execution from IntentSpec projection
   to a queue-native GoalCompiler submission without touching Worker/Queue.
2. Replace the remaining `TaskProvider` IntentSpec mutation methods with
   TaskManager calls, then remove `TaskCommand`, `IntentSpecProvider`, and
   `TaskGraphBuilder` once their callers are gone.
3. Feed ToolMetadata policy into the legacy PlanValidator/CapabilityExecutor
   boundary so Capability remains only a semantic retrieval index there too.
4. Apply the new `007_task_manager_dynamic_planning.sql` migration in every
   deployment; startup additive DDL remains only a compatibility guard.
5. Install/run Ruff in CI and separate baseline findings from Phase 4 files.

## 15. Next Phase

Next work should make plan submission queue-native and finish draining the
Intent/TaskCommand fallback. Memory and richer dynamic planning can then build
on the canonical Task/Goal/Plan contracts without modifying Reliable
Execution Runtime internals.
