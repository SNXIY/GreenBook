> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# Phase 4.5 Canonical Runtime Cutover

## 1. Goal

Make the Phase 1–4 runtime the only default production intelligence path.
User messages enter through `CommandInterpreter`; structured goals are owned by
`GoalDecomposer`; durable work is owned by `TaskManager`; runtime decisions are
made by `AgentLoop`; reliable work is submitted to Queue/Worker through a typed
execution boundary.

This cleanup removes business-intelligence compatibility code while retaining
execution compatibility required by the existing Worker, retry, checkpoint,
ledger, artifact, ToolRuntime, and MCP contracts.

## 2. Production Path Before

The default API had already been routed through the new Conversation Runtime,
but the repository still contained reachable historical paths:

```text
message -> TaskCommand / IntentSpec -> graph/template fallback -> Runtime
message -> Command -> GoalTree -> AgentLoop -> execution callback
```

The first path duplicated understanding, target resolution, decomposition, and
planning. Some direct `RuntimeAgentService` callers also instantiated the old
template planner as part of execution.

## 3. Final Production Path

```text
User/API
  -> CommandInterpreter -> Command -> TargetResolver
  -> GoalDecomposer -> GoalTree
  -> TaskManager -> TaskRepository
  -> AgentLoop (Observe -> Reason -> Act -> Reflect)
       -> ToolSelector -> ToolMetadata -> ToolPolicyGate
       -> GoalCompiler -> TaskPlan
  -> ExecutionSubmissionService
  -> ExecutionQueue -> ExecutionQueueWorker -> ExecutionWorker
  -> ToolRuntime -> MCP -> Java/Creator services
```

Read-only tools may run synchronously after `ToolPolicyGate`. Multi-step,
side-effecting, long-running, and recoverable work is queue-native.

## 4. Queue-native Submission

`QueueExecutionSubmissionService` is the typed boundary used by AgentLoop.
`ConversationRuntimeAdapter` supplies a compiled `TaskPlan` to
`RuntimeAgentService.submit_plan()`. `submit_plan()` always allocates the
normal `PlanExecution` and publishes its dispatch envelope to the configured
`ExecutionQueue`, even when the container's default dispatch mode is direct.

The boundary does not execute a Worker inline. Queue delivery, leases, retry,
checkpoint, ledger, evidence, and completion projection remain in the existing
Reliable Execution Runtime. Side-effect Tool calls use the same submission
path and bind the resulting Execution to the canonical Task.

## 5. Removed Intent Layer

The following user-understanding paths were removed from the Assistant Runtime:

- `task/intent_spec_provider.py`
- `task/understanding.py`
- `task/intent_compat.py`
- `compatibility/intent/provider.py`
- `services/intent_compiler.py`
- `conversation/commands.py` (`TaskCommand`)
- `command/adapter.py`

Control and approval semantics now use explicit `ExecutionControlCommand` in
`conversation/control.py`. Natural-language understanding is exclusively the
canonical `command/` + `goal/` path.

`IntentSpec` and `TaskIntent` are no longer produced by GoalCompiler or used as
the user-facing graph semantic. A small `TaskIntent` projection remains only
inside the Reliable Execution dispatch snapshot and direct resolved-context
compatibility method; it carries already-compiled execution inputs and does not
interpret user text.

## 6. Removed Task/Graph Compatibility

`TaskGraphBuilder`, old multi-task decomposition, duplicate target evidence and
old TaskProvider semantic projections were removed. `task/graph_models.py`
contains graph data and validation only; `GoalCompiler` is the sole GoalTree →
graph/TaskPlan compiler.

`TaskProvider` now owns request scope, storage setup, authorization, and API
projection. Durable Task lifecycle changes go through `TaskManager` and
`TaskRepository`.

## 7. Planner Consolidation

The canonical path uses `GoalCompiler` for deterministic GoalTree → TaskPlan
compilation and `DynamicPlanner` for typed runtime decisions. It does not select
business templates or reinterpret the user's message.

The historical `TaskOrchestrator` and `orchestration/templates.py` remain only
behind the lazy `RuntimeAgentService.execute()` compatibility branch for callers
that explicitly provide a resolved `TaskContext`. They are not constructed by
the default Conversation Runtime and are not used by `submit_plan()`.

## 8. Tool Policy Consolidation

`ToolMetadata` is the Agent-facing policy source for permission, approval,
risk, side effects, retry policy, cost, timeout, and execution mode.
`ToolPolicyGate` runs after `ToolSelector` and before ToolRuntime or Queue.
`PlanValidator` uses the injected ToolMetadata registry when available; its
Capability policy fields are only a compatibility fallback for isolated legacy
execution tests/containers without exported tool metadata.

CapabilityRegistry remains a semantic capability catalog and execution contract
index. `CapabilityExecutor` can execute an explicitly selected `PlanStep.tool_name`
or an unambiguous single-tool execution compatibility plan; it does not choose
among multiple tools or apply policy.

## 9. Deleted Files

| Path | Old Responsibility | Replacement | Reason |
| --- | --- | --- | --- |
| `conversation/commands.py` | Mixed natural-language TaskCommand and controls | `command/models.py`, `conversation/control.py` | Removed mixed business/control contract |
| `task/intent_spec_provider.py` | Legacy IntentSpec understanding | `command/interpreter.py`, `goal/decomposer.py` | No production caller after canonical adapter cutover |
| `task/understanding.py` | Intent understanding fallback | `goal/decomposer.py` | Duplicate understanding layer |
| `task/intent_compat.py` | IntentSpec → TaskIntent projection | `TaskManager`, `GoalCompiler`, execution projection | Business projection caller set was drained |
| `task/graph.py` | Text/Intent → TaskGraphBuilder | `goal/compiler.py`, `task/graph_models.py` | Graph model remains; second builder does not |
| `task/multi_task.py` | Legacy multi-task decomposition/resolver | `TaskManager`, `GoalTree` | Duplicate decomposition and Task lifecycle |
| `services/intent_compiler.py` | API Intent compilation | `CommandInterpreter`, `GoalDecomposer` | Removed old API intelligence entry |
| `compatibility/intent/provider.py` | Compatibility Intent provider | Canonical Command/Goal runtime | No production caller |

Retired tests that asserted deleted private APIs or the removed Intent/TaskGraph
architecture were deleted or rewritten rather than used to restore the old path.

## 10. Remaining Compatibility

The old `assistant_task_intents` persistence path was also removed from
`task/registry.py`; the registry now stores canonical Task projections only.
`task/intent_models.py` remains solely for direct execution-input snapshots
and queue payload compatibility, not for user-message interpretation.

| Path | Concrete caller | Why it remains | Removal condition |
| --- | --- | --- | --- |
| `RuntimeAgentService.execute()` legacy resolved-context branch | `AssistantService` direct resolved-context API and existing reliable-runtime regression tests | It consumes already-resolved execution context and preserves Worker/Retry behavior | Migrate remaining direct callers to `ConversationRuntimeAdapter` + `submit_plan()` |
| `RuntimeAgentService.execute_queued()` TaskIntent deserialization | `RuntimeExecutionQueueHandler` | Queue payloads must remain readable by the current Worker process | Replace dispatch snapshot with a dedicated execution-input contract after a rolling payload migration |
| `execution/argument_binder.py` and `execution/capability_executor.py` IntentSpec type parameters | Reliable Execution compatibility tests and direct resolved execution | They bind/execute typed plan steps; they do not understand user messages | Introduce the final execution-input schema and migrate persisted/queued payloads |
| `orchestration/orchestrator.py` and `orchestration/templates.py` | Lazy `RuntimeAgentService.execute()` branch plus plan-construction tests | Existing direct resolved execution still needs deterministic plan recipes | Delete after direct resolved callers and tests use `GoalCompiler` plans |
| `task/intent_models.py` | Direct execution fixtures and queue dispatch snapshot | Execution-input compatibility only; `task/registry.py` no longer persists Intent rows | Replace with a dedicated immutable execution-input contract |

No retained item is part of the default natural-language production path.

## 11. Directory Changes

- `command/` is the single user-command boundary.
- `goal/` owns decomposition and compilation.
- `agent/` owns runtime decisions.
- `task/` owns durable Task models, repository, manager, and graph contracts.
- `planning/` owns DynamicPlanner and plan validation.
- `toolruntime/` owns ToolMetadata policy gates.
- `execution/`, `artifact/`, `human/`, `observability/`, `db/`, and MCP
  boundaries remain reliable infrastructure.
- The old `compatibility/intent` package has no source modules remaining.

## 12. Test Fixes

- Removed tests for retired Intent providers, TaskGraphBuilder, duplicate
  resolvers, old TaskCommand adapters, and deleted private route helpers.
- Rewrote GoalCompiler graph assertions to validate capabilities, constraints,
  and target evidence rather than an IntentSpec projection.
- Retired `/runs/{run_id}` control/event endpoints; runtime execution control
  and execution event routes are canonical.
- Added queue-native cutover tests covering forced queue submission and the typed
  submission boundary.
- Added durable async-step continuation coverage: a completed or timed-out
  async Tool result reopens the same Step and is replayed through the ledger.

## 13. Test Results

Final verification for this cutover:

- `pytest -q`: **657 passed, 2 skipped, 3 warnings**.
- `pytest --collect-only -q`: **659 tests collected**.
- Key E2E/runtime/evaluator subset: **494 passed**.
- Changed-module Ruff check: **passed**.
- Assistant core/API/contracts/security `compileall`: **passed**.

The remaining warnings are an upstream Starlette/httpx deprecation, an
unregistered `integration` marker, and the workspace pytest-cache permission
warning; none is a collection or test failure.

Latest recorded targeted results:

- `pytest -q tests/unit`: **484 passed**.
- `pytest -q tests/unit/test_phase4_5_canonical_runtime.py`: **2 passed**.
- Agent/Goal regression: **7 passed**.
- Changed-module Ruff check: **passed**.
- Assistant core/API `compileall`: **passed**.

## 14. Final Runtime Dependency Graph

```text
API
  -> command.Command
  -> goal.GoalTree
  -> task.TaskManager / task.TaskRepository
  -> agent.AgentLoop
       -> planning.DynamicPlanner
       -> toolruntime.ToolSelector / ToolMetadata / ToolPolicyGate
       -> goal.GoalCompiler
  -> execution.Queue / Worker / Retry / Checkpoint / Ledger / Evidence
  -> ToolRuntime / MCP
  -> Java Community Backend / Creator Agent
```

Execution does not understand Command. ToolRuntime does not call AgentLoop.
AgentLoop does not write Java directly. GoalDecomposer does not call tools.

## 15. Technical Debt Remaining

1. Migrate direct `RuntimeAgentService.execute()` callers to the canonical
   adapter and delete the template/orchestrator fallback.
2. Replace the queue TaskIntent snapshot with a dedicated immutable execution
   input contract after rolling queue payload compatibility is complete.
3. Remove policy fields from the semantic Capability model once all isolated
   execution compatibility containers export ToolMetadata.
4. Replace the remaining direct/queued `TaskIntent` snapshot with a dedicated
   immutable execution-input contract after rolling payload migration.

## 16. Next Phase

Proceed to Memory/Context enhancement only on top of this cutover. New work
must consume Command, GoalTree, TaskManager, AgentState, ToolMetadata, and the
Reliable Execution contracts; it must not reintroduce Intent routing, template
selection, TaskCommand, duplicate Resolver, or a second Agent runtime.
