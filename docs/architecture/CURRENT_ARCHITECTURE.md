# GreenBook Current Architecture

This is the current architecture authority. Phase reports and audit files are
historical records; they do not define active topology.

## Active product surface

| Surface | Location | Responsibility |
| --- | --- | --- |
| Frontend | `zhiguang-fe` | User-facing Conversation, Task, Progress, Execution, Artifact, Approval, and Schedule UI |
| Java Backend | `apps/backend` | Community data, identity, REST APIs, and Java-side Agent Tool API |
| Agent API | `apps/agent_api` | HTTP composition root for conversation and runtime requests |
| Agent Worker | `apps/agent_worker` | Durable queue consumer and execution worker |
| Agent Core | `packages/agent_core` | Command, Context, Goal, Task, planning, AgentLoop, tool selection, execution, memory, and recovery contracts |
| Creator Service | `creator-agent` | Creator-domain research, writing, quality, artifacts, and approvals |
| MCP runtime | `services/greenbook_mcp` | MCP-compatible in-process tool registry and handlers |

There is one Java owner, one Creator owner, and one GreenBook Agent Runtime.
The MCP package is imported by the Agent API/Worker and is not a standalone
deployment process.

## Runtime flow

```text
Conversation facts
  -> ContextBuilder -> ContextSnapshot (bounded working projection)
  -> CommandInterpreter -> Command
  -> GoalDecomposer -> GoalTree
  -> TaskManager / TaskRepository
  -> AgentLoop (Observe -> Reason -> Act -> Reflect)
  -> DynamicPlanner / ToolSelector
  -> ToolPolicyGate
  -> GoalCompiler -> planning.TaskPlan / PlanStep
  -> ExecutionInput
  -> ExecutionSubmissionService -> Queue
  -> Agent Worker -> ToolRuntime / MCP
  -> Java Backend or Creator Service
```

## Agent intelligence contract

- `CommandInterpreter` asks the LLM for a semantic understanding payload
  (`goal`, `entities`, `constraints`, `references`, `ambiguity`, and
  `required_capabilities`). The coarse `CommandType` is only an operation
  envelope; it is not a keyword-driven Intent classifier.
- `GoalDecomposer` turns that payload into a validated `GoalTree`. Goals may
  form sequential or parallel dependencies, and `DynamicPlanner` can apply
  evidence-based insert/remove/reorder/alternative-tool decisions during the
  `AgentLoop` without becoming a workflow-template engine.
- `AgentLoop` owns Observe/Reason/Act/Reflect. `Execution` remains the durable
  runtime for queueing, worker delivery, checkpointing, ledger evidence,
  retry, and recovery.
- `ToolSelector` sees semantic `ToolMetadata`; `ToolPolicyGate` remains the
  code-owned approval, permission, side-effect, cost, retry, and timeout gate.
- `ConversationService` preserves durable facts and bounded summaries, while
  `ContextBuilder` projects the current decision working set. Memory remains a
  separate long-term retrieval boundary.

## Ownership boundaries

- `conversation/` owns durable Conversation facts: messages, summaries, and preferences. Its service is `ConversationService`; durable data access is `ConversationRepository`.
- `context/` owns the decision working set: `ContextBuilder`, `ContextSnapshot`, and projections. It does not persist Conversation facts.
- `command/` understands the current user expression. It does not select tools or execute work.
- `goal/` owns `GoalTree` and semantic goal decomposition.
- `task/` owns Task lifecycle, priority, ownership, preemption, resume, and version history.
- `planning/` owns `TaskPlan`, `PlanStep`, `PlanRevision`, `PlanningDecision`, and `PlanGraph`.
- `agent/` owns the decision loop and reflection; it does not directly perform external side effects.
- `contracts/ToolMetadata` is the single source for concrete tool policy. `Capability` is semantic catalog data only.
- `execution/` owns `ExecutionInput`, queue/worker behavior, checkpoints, leases, retries, ledger/evidence, idempotency, artifacts, and recovery.
- `services/greenbook_mcp` consumes shared contracts and supplies handlers; it does not redefine policy.
- API modules compose these boundaries. Core modules do not import API routes.

## Canonical identities and paths

- Runtime identity: `execution_id`.
- Public history projection: `run_id` and the retained `assistant_runs` table. `Run` is a history projection, not runtime state.
- Agent API: `/api/v1/agent/*` on port `8094`.
- Java Agent Tool API: `/api/v1/agent/*` on port `8080`.
- Creator Service: port `8092`.
- Frontend: port `5173`.

The old `/api/v1/assistant-tools` surface, retired products, IntentSpec queue
payloads, and workflow-template routing are not active callers.

## Persistence

Each domain repository remains authoritative for its own facts: Conversation,
Task, Execution, Artifact, Memory, and Java community data. `assistant_runs`
remains only for public history compatibility; no new execution decision is
stored there as runtime truth.
