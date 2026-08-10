# Phase 11-E / Phase 12-A Runtime Wiring Implementation

## Delivered

The Runtime now has one configuration-driven persistence aggregate:

```text
RuntimePersistenceFactory
  -> ExecutionRepository
  -> ExecutionEventStore
  -> CheckpointStore
  -> ExternalOperationStore
  -> RetryTaskStore
  -> LeaseManager
```

`ASSISTANT_RUNTIME_STORAGE=memory` keeps the existing process-local profile
available for tests and local development. `postgres` selects the existing
SQLAlchemy adapters and requires a database URL. The API and standalone
worker use the same selection rules and database environment variables.

## API lifecycle wiring

The Assistant API lifespan now creates one provider and shares its stores with:

- `ExecutionStateManager`
- `RuntimeManager`
- `RuntimeAgentService`
- `ExternalOperationTracker`
- `RetryManager`
- `RetryScheduler`

`RuntimeAgentService` forwards the checkpoint store and operation tracker to
each `ExecutionWorker`. Runtime routes retain their explicit test/application
injection precedence and consult the provider before constructing a final
memory fallback.

The provider-owned SQLAlchemy engine is disposed during API shutdown.

## Standalone worker entry point

`apps/assistant_worker/greenbook_assistant_worker/main.py` now constructs the
same persistence profile and starts `RetryBackgroundWorker` with:

- `RetryScheduler` backed by the provider's `RetryTaskStore`
- `RetryManager` backed by the provider's execution repository and event store
- the provider's checkpoint store in `RuntimeManager`

Supported worker settings are:

```text
ASSISTANT_RETRY_WORKER_ID
ASSISTANT_RETRY_POLL_INTERVAL_SECONDS
ASSISTANT_RETRY_BATCH_SIZE
ASSISTANT_RETRY_LEASE_SECONDS
```

Shutdown requests stop polling, release claimed retry tasks, dispose the
provider-owned database bind, and close the Java client.

## Intentionally unchanged behavior

This phase does not add an execution state, invoke tools from the background
process, or change retry/reconciliation policy. `RetryBackgroundWorker` still
hands claimed tasks to `RetryManager`; the existing Runtime Worker remains the
tool execution boundary.

The lease manager is constructed and exposed as part of the common provider,
but existing execution code has no lease-consumer boundary yet. PostgreSQL
schema creation remains adapter bootstrap compatibility; production schema
migrations remain the deployment responsibility.

## Verification

Targeted persistence, scheduler, route, and standalone-entrypoint tests cover
the shared store wiring, PostgreSQL adapter restart behavior, and safe worker
shutdown. No service was started and no full test suite was run in this phase.

