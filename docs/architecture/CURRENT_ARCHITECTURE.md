# GreenBook Current Architecture

This document describes the active production wiring observed in the source
tree on 2026-08-21. Historical phase reports remain useful evidence, but they
do not override the composition root in `apps/agent_api/greenbook_agent_api/main.py`.

## Active product surface

| Surface | Location | Current responsibility |
| --- | --- | --- |
| Frontend | `zhiguang-fe` | Conversation input, durable Run/Activity projection, clarification and approval controls |
| Agent API | `apps/agent_api` | Authenticated conversation API, immediate Run acceptance, Runner/Turn wiring, user-facing projection |
| Agent Worker | `apps/agent_worker` | Durable queue consumer, lease/fencing, checkpoint/retry, completion projection, reconciliation |
| Agent Core | `packages/agent_core` | Context, Command interpretation, Target/Temporal resolution, FastPath, Objective/ActionLoop, observation and satisfaction contracts |
| MCP Tool Runtime | `services/greenbook_mcp` | Typed tool registry, policy/approval boundary, JavaClient adapters, Java verification evidence |
| Java business backend | `apps/backend` | Community API, Draft/Schedule/Post state, scheduled publication, notifications/outbox |

The standalone Creator Agent is not an active production entry. Content
generation is performed through the Agent control path and persisted by Java.
The MCP server is imported in-process by the API/Worker; it is not a second
business runtime.

## Active request and execution flow

```text
Frontend
  -> POST /api/v1/agent/conversations/{conversation_id}/messages
  -> authenticate + persist AgentRun(ACCEPTED)
  -> AgentRunner claims the Run
  -> TurnCoordinator
       -> bounded ContextAssembler
       -> one CommandInterpreter pass
       -> TargetResolver + TemporalResolver
       -> ResolvedSemanticState
       -> FastPathGate
          |-- CHAT / CLARIFY / simple READ
          |     -> FastPathExecutor -> MCP ToolRuntime -> Java READ
          |-- simple WRITE
          |     -> Target/temporal checks -> ActionLoop write boundary
          |-- COMPLEX
                -> ActionLoopExecutor
                -> one typed semantic ActionDecision per iteration
                -> Objective-scoped ResourceBinding and deterministic Guard
  -> ConversationRuntimeAdapter
       |-- READ: execute_fast_path_read -> MCP ToolRuntime -> Java
       |-- WRITE: submit_fast_path_write / submit_tool
                -> ExecutionInput -> RuntimeAgentService
                -> Durable ExecutionRepository + OperationLedger
                -> Queue -> Worker lease/fencing/checkpoint/retry
                -> MCP ToolRuntime -> JavaClient -> Java DB
  -> Java verification / ToolResult / Observation
  -> ResourceBinding + deterministic Objective/Run satisfaction
  -> completion/activity projection -> SSE/polling -> Frontend
```

The API may use direct dispatch for the local in-memory profile, but that is a
dispatch configuration of the same Runtime submission boundary, not a second
business path. PostgreSQL/worker deployment uses the queue and reconciliation
worker.

## Control-plane owners

- `ContextAssembler` owns the bounded decision context; it is not business truth.
- `CommandInterpreter` extracts the current structured user request. It does
  not execute tools or decide Java state.
- `TargetResolver` is the only owner of natural-language target grounding.
- `TemporalResolver` is the only owner of natural-language publication-time
  canonicalization. An unresolved future request cannot become publish-now.
- `FastPathGate` answers only whether a request is sufficiently certain for a
  short path. It is not a second intent taxonomy.
- `ActionLoop` selects one typed semantic action at a time. It does not compile
  a static DAG and does not own durable delivery.
- `Objective`/`Task` are the current persisted work-item envelope. Their
  reducer owns deterministic satisfaction until a Commitment migration is
  justified.
- `ActionGuard` is a thin write-admission recheck for target, temporal,
  ownership, approval and duplicate-result facts.
- The isolated B POC in
  `packages/agent_core/greenbook_agent_core/turn/commitment_poc.py` models a
  minimal Commitment/WorkItem projection, but is not production-wired or
  persisted.

## Durable and business owners

- `Execution` owns delivery progress, leases, fencing, checkpoints, retries,
  resume and execution status.
- `OperationLedger` owns external side-effect claim/status and keeps
  `RESULT_UNKNOWN` on the reconciliation path; it must not blindly retry an
  uncertain write.
- `ResourceBinding` owns which Draft/Schedule/Post belongs to an Objective.
- `Approval` owns human authorization/waiting for risky writes.
- Java owns Draft, Schedule, Post, publication and notification business facts.
- `Run`, Activity and API/Frontend objects are projections/aggregations. They
  must not independently re-derive Java business truth.

## Compatibility and legacy boundary

`ConversationRuntimeAdapter` still contains compatibility code for historical
Task/Goal snapshots and test/repair tooling. `GoalTree`, `GoalCompiler`,
`DynamicPlanner` and the old AgentLoop are not the active TurnCoordinator
decision path for the current API wiring. They should be retired only after
caller-level evidence, not by deleting them during the lightweight POC.

## Canonical identities and endpoints

- Runtime identity: `execution_id`.
- Public conversation history: `run_id` and its projection stores.
- API: `/api/v1/agent/*` on port `8094`.
- Java Agent Facade: `/api/v1/agent/*` on port `8080`.
- Frontend: port `5173`.

## Persistence rule

Each domain owns its facts: Conversation, Task/Objective, Execution,
Operation, Approval, ResourceBinding and Java community data. The control
layer may project and resolve these facts, but it must not create a parallel
Draft/Schedule/Post state source.
