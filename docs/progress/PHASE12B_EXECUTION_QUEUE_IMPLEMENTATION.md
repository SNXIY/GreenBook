# Phase 12-B Execution Queue Implementation

## Queue contract

Added `ExecutionQueueMessage` and `ExecutionQueue` with the following
business-neutral operations:

```text
enqueue(execution_id)
claim(execution_id message)
ack(message)
fail(message)
release(message lease)
```

The message contains:

- `execution_id`
- `created_at` / `available_at`
- `attempt`
- `trace_id`
- claim owner and lease deadline
- queue status and last error
- opaque, non-secret dispatch payload

The queue does not call Planner, MCP, Java, Creator, or `ExecutionWorker`.

## Storage and worker

Both profiles are available:

- `ExecutionQueue`: thread-safe memory implementation;
- `PostgresExecutionQueue`: durable SQLAlchemy implementation using the
  Runtime persistence bind and conditional claim updates.

`RuntimePersistenceFactory` now creates the queue and the API exposes it as
`app.state.execution_queue`.

`ExecutionQueueWorker` owns polling, claim leases, handler delegation, ack,
failure marking, and safe release during shutdown. The standalone worker can
run it alongside `RetryBackgroundWorker` when an execution handler is
injected. With `ASSISTANT_RUNTIME_STORAGE=postgres`, the standalone entry
point enables the concrete Runtime handler by default; set
`ASSISTANT_EXECUTION_QUEUE_CONSUMER=false` for a controlled rollout.

## API dispatch

`RuntimeAgentService` supports `dispatch_mode="queue"`. In queue mode it:

1. performs the existing intent/task/plan/validation work;
2. creates and persists the canonical `PlanExecution`;
3. writes a queue message for its `execution_id`;
4. returns `RuntimeResult(status="QUEUED")` without invoking a tool.

Memory/local Runtime keeps direct execution by default. PostgreSQL API
startup defaults to queue dispatch; deployments may override it with
`ASSISTANT_EXECUTION_DISPATCH=direct|queue`.

## Worker execution handler

The concrete standalone handler reconstructs a process-local `RuntimeContext`
and validated `ExecutablePlan` from the queue payload, loads the existing
`PlanExecution`, and calls `RuntimeAgentService.execute_queued()`. That path
reuses the existing `ExecutionWorker` for tool execution and persists state and
events through the durable provider.

The queue payload intentionally removes `raw_access_token`, `access_token`,
and `refresh_token`. The worker requires `ASSISTANT_WORKER_ACCESS_TOKEN` and
uses it only in memory as its configured service credential while preserving
the queued user/tenant identity. A deployment must configure that credential
according to the Java facade's service-to-service authorization policy.

## Rollout configuration

```text
ASSISTANT_RUNTIME_STORAGE=postgres
ASSISTANT_RUNTIME_DATABASE_URL=postgresql://...
ASSISTANT_EXECUTION_DISPATCH=queue
ASSISTANT_EXECUTION_QUEUE_CONSUMER=true
ASSISTANT_WORKER_ACCESS_TOKEN=...
ASSISTANT_RETRY_WORKER_ID=assistant-worker-1
```

No new `ExecutionStatus` was added. Queue delivery status is separate from
the canonical execution state, so an enqueued execution remains
`PENDING` until the worker starts it.

## Remaining boundary

The queue payload is a dispatch envelope, not a replacement for the
Execution/Event/Checkpoint schema. Result projections such as the API's
in-memory `run_store` are still API-local read models; canonical execution
status and events remain the cross-process source of truth.

