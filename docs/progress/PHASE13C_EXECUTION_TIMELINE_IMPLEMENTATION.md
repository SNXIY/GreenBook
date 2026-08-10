# Phase13-C Execution Timeline API

## Scope

Phase13-C adds a read-only timeline projection for one canonical Runtime
execution. It does not change the existing `ExecutionEvent` contract, event
storage, Execution state machine, or Worker behavior.

## Read model

`ExecutionTimelineService` reads the durable/in-memory `ExecutionEventStore`
and, when configured, the `ExternalOperationStore`. It returns chronological
items with:

- event type and source event id;
- execution, step, invocation, tool-call, operation, and external identifiers;
- `TraceContext` when present;
- evidence and original event payload;
- retry, reconciliation, and external-operation categories.

The operation record is exposed as a separate item so its latest status remains
visible even when reconciliation does not emit a new execution event.

## API

`GET /api/v1/executions/{execution_id}/timeline`

The endpoint uses the same execution lookup and ownership authorization as the
existing status, steps, and events endpoints. A missing execution returns 404;
the read model itself performs no state transition.
