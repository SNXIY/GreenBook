# Service & Module Communication

Canonical communication map for the GreenBook Agent Runtime. It replaces the
historical Creator-era topology: the standalone Creator Service is retired and
content generation is assistant-first (host LLM → `content.create_draft` → Java).

## Active services

| Owner | Host port | Protocol / boundary |
| --- | ---: | --- |
| Java Backend | 8080 | REST, identity (JWT), community data, drafts, publication, notifications |
| Agent API | 8094 | Conversation / Run / Execution / SSE, `/api/v1/agent/*`, `/api/v1/*` |
| Frontend | 5173 | Vite development server |

The frontend uses `/api` for Java, `/agent-api` for the Agent API, and
`/api/v1/...` for runtime routes in local Vite proxy mode. These proxy paths
are frontend-only and rewritten before the request reaches a service.

## In-process boundaries (Agent API process)

```text
API routes (fastapi)
  -> ConversationRuntimeAdapter (immediate-accept / agent-loop)
  -> AgentLoop (Observe/Reason/Act/Reflect)        [agent_core]
  -> DynamicPlanner / ToolSelector / ToolPolicyGate
  -> GoalCompiler -> ExecutionInput
  -> Queue / Worker (durable) / Retry / Lease
  -> ToolRuntime -> GreenBookMCPServer (in-process, no separate MCP process)
  -> JavaClient -> Java Backend (community data, drafts, publication)
```

- `apps/agent_api` owns the HTTP/SSE surface and durable Run lifecycle.
- `packages/agent_core` owns planning, execution, memory, and recovery; it
  never imports the API package.
- `services/greenbook_mcp` is imported in-process; tools call Java through
  `packages/java_client` with the user JWT (no service-to-service secrets).
- Identity is issued by Java and validated at every boundary from the token,
  never from model-supplied tool arguments.

## Durable stores

- PostgreSQL `mindflow_creator` (Compose service `creator-postgres`, name kept
  for volume compatibility): Runs, Executions, Observations, queue messages,
  retry tasks, approvals, memory, run events.
- Redis: Java cache (DB1); Agent DB0. Kafka: Java outbox events.
- Queue mode `GREENBOOK_AGENT_EXECUTION_DISPATCH=queue` with an external worker
  (or in-process consumer in dev).

## Run event stream — `/api/v1/agent/runs/{run_id}/stream` (SSE)

The single progressive-activity channel for the frontend. The first meaningful
activity is pushed before any Execution exists, so the UI never depends on
`execution_id`. Event type names are the canonical vocabulary in
`packages/contracts/greenbook_contracts/events.py` (single source of truth —
producers in `agent_core` / API, consumers in `AgentPanel`).

| event_type | produced by | payload |
| --- | --- | --- |
| `UNDERSTANDING` | AgentLoop (adapter) | `{summary, tasks[]}` — shown before execution so a wrong understanding can be stopped early |
| `SEMANTIC_ACTION_SELECTED` | AgentLoop / API projection | `{semantic_action, goal_id, task_id}`; projected terminal copy adds `{phase: SUCCEEDED\|FAILED}` |
| `ACTION_COMPLETED` | AgentLoop (internal) | `{semantic_action, goal_id, task_id, execution_id, ok, result}` — never exposed; the API projects it into the two rows above |
| `PARTIAL_RESULT` | API projection | `{title, count?, run_at?, goal_id, task_id}` — "找到 N 篇" / "草稿已生成" / "将于 X 发布" |
| `FOLLOW_UP_QUEUED` | API message accept | `{run_id, follow_up_run_id, message}` — mid-turn injection hint on the parent card |
| `REASONING_STARTED` | AgentRunner | `{run_id}` |
| `WAITING_APPROVAL` | AgentRunner / reconciliation | approval context |
| `RUN_COMPLETED` / `RUN_FAILED` | AgentRunner / reconciliation | terminal lifecycle |

Mid-turn injection: a message sent while a working Run exists is accepted as a
new durable Run with `payload.follow_up_of = <parent run id>`; the runner keeps
it unclaimed while the parent is in `RUN_WORKING`, and the parent stream emits
`FOLLOW_UP_QUEUED`. The frontend renders it as a hint on the parent card
instead of a second parallel card.

## Execution stream — `/api/v1/executions/{execution_id}/stream` (SSE)

Fine-grained step/event stream for a durable Execution (step starts, evidence,
artifacts). Used by the execution detail views; the run stream above is the
primary business-activity channel.

## Message flow (immediate accept)

1. `POST /api/v1/agent/conversations/{id}/messages` validates, persists the
   user message and a durable `AgentRun(status=ACCEPTED)`, returns `202` with
   `run_id` (+ `follow_up_of` when queued behind a working Run).
2. The in-process `AgentRunner` claims ACCEPTED Runs atomically (advisory lock
   + lease) and executes the canonical adapter path.
3. Progressive events are appended to the Run event store; the SSE stream
   replays them. Side-effecting actions go through the durable
   Queue/Worker/Execution pipeline unchanged.
4. Run terminal status is converged only after queued executions and
   observations settle (`_reconcile_agent_run_status`).

## Caller discipline

- `ExecutionInput` is the only intelligence-to-execution request; workers never
  consume Command, Intent, or raw user text.
- Event payloads are additive-only: the frontend ignores unknown fields, and
  new event types must be registered in `events.py` before use.
