# Phase 13-A Trace Correlation Audit

## Scope

This audit follows correlation identifiers from the HTTP conversation boundary
through queue dispatch, Runtime execution, tool invocation, external operation
tracking, retry, and reconciliation. It does not change Planner, intent/task
providers, Creator business logic, or Java business logic.

## Current identifier flow

| Identifier | Current source | Current carriers | Current gap |
| --- | --- | --- | --- |
| `conversation_id` | Assistant message route / `RuntimeContext` | conversation adapter, queue payload, `SessionContext` | not present on `PlanExecution` or canonical `ExecutionEvent` |
| `trace_id` | HTTP route / `RuntimeContext` | queue message, `TraceEvent`, `ExecutionEvidence`, MCP call kwargs | `ExecutionEvent` has no durable trace field; worker `TraceCollector` is process-local |
| `execution_id` | `PlanExecution` created by `ExecutionWorker.init_from_plan` | repository, queue message, execution events, evidence | broadly present after creation |
| `step_id` | `PlanStep` / `StepExecution` | execution events, invocation context, evidence, operation record | broadly present, but not in every trace event payload |
| `invocation_id` | `ToolInvocationContext` | ledger, `InvocationResult`, evidence | not copied into canonical execution events or `TraceEvent` |
| `tool_call_id` | API Runtime raw MCP adapter | MCP kwargs, raw tool result/evidence | not a field on `ToolInvocationContext`; not copied into canonical events |
| `operation_id` | evidence or `ExternalOperationTracker` | external operation record and evidence | not directly present on trace events; generated after some tool events |

## Current execution path

```text
Conversation / HTTP
  -> RuntimeContext(trace_id, conversation_id, run_id)
  -> RuntimeAgentService
  -> ExecutionWorker creates execution_id
  -> CapabilityExecutor creates invocation_id
  -> Runtime MCP adapter creates tool_call_id
  -> ExecutionEvidence records invocation/tool/operation facts when available
  -> ExternalOperationTracker persists operation evidence
  -> ExecutionEventStore persists lifecycle and retry/reconciliation events
```

The queue preserves `trace_id`, `conversation_id`, `run_id`, and the dispatch
snapshot. The standalone Worker reconstructs a new `RuntimeContext`, so the
identifier values survive the process boundary. The Worker currently creates
an independent in-memory `TraceCollector`; its trace events are not a durable
source of truth after process exit.

## Event-system split

There are two intentionally different event models today:

1. `greenbook_assistant_core.observability.TraceEvent` is a detailed tool and
   artifact timeline. It carries `trace_id`, `execution_id`, `step_id`, and
   `tool_name`, but its collector is memory-only.
2. `greenbook_assistant_core.execution.ExecutionEvent` is the canonical
   lifecycle/recovery event model. It is available through the memory and
   PostgreSQL event stores, but currently carries only `execution_id`,
   `step_id`, event type, timestamp, and an opaque payload.

This split means an operator can correlate some live trace events or some
durable execution events, but cannot reliably follow one request through
Conversation -> Execution -> Step -> Invocation -> Tool Call -> External
Operation after the API/Worker process boundary.

## Missing correlation boundary

The Runtime needs one serializable `TraceContext` value containing the
correlation scope:

```text
conversation_id
run_id
trace_id
task_id
execution_id
step_id
invocation_id
tool_call_id
operation_id
```

The context should be copied into invocation/evidence and event payloads, not
added to the protected Execution state schema. Existing event storage can
persist the context inside its JSON payload, preserving database schema
compatibility. External operation records already persist `ExecutionEvidence`,
which can carry the same trace context until a dedicated operation read model
is introduced.

## Phase 13-A conclusion

The identifiers are mostly generated at the right boundaries, but they are
independent strings rather than one propagated correlation object. The
minimal safe integration is:

```text
RuntimeContext
  -> TraceContext
  -> AgentTrace / TraceEvent
  -> ToolInvocationContext
  -> ExecutionEvidence
  -> ExecutionEvent payload
  -> ExternalOperationRecord.evidence
```

Retry and reconciliation can then recover the context from prior canonical
events/evidence without changing `ExecutionStateManager` or adding a new
Execution status.

