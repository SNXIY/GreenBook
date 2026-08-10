# Phase 11-E Runtime Persistence Wiring Audit

## Scope

This audit records the Runtime construction paths before durable persistence is
wired as the default deployment option. It covers the Assistant API lifespan,
Runtime routes, `RuntimeAgentService`, `ExecutionWorker`, the state manager,
and the standalone Assistant worker entry point.

The audit is intentionally limited to dependency construction and lifecycle
ownership. It does not change execution semantics or business behavior.

## Current startup and execution path

The Assistant API currently constructs its Runtime dependencies directly in
`apps/assistant_api/greenbook_assistant_api/main.py`:

```text
FastAPI lifespan
  -> ExecutionRepository()              [memory]
  -> ExecutionEventStore()              [memory]
  -> ExecutionStateManager(repository, event_store)
  -> RuntimeManager(state_manager)      [private checkpoint memory]
  -> RuntimeAgentService(repository, event_store)
  -> ConversationRuntimeAdapter
  -> ExecutionWorker
```

`RuntimeAgentService` passes the repository and event store to each
`ExecutionWorker`. Before this wiring change, it does not receive a
checkpoint store or an external-operation tracker from the application
lifecycle, so those dependencies fall back to process-local implementations
inside the execution path.

The Runtime routes first use `app.state.execution_runtime_manager`. Their
fallback path can build a manager from `execution_state_manager`, or create a
new memory `ExecutionRepository` when no application state was configured.
That fallback is useful for isolated route tests, but it must not replace the
provider selected by the production lifespan.

The standalone entry point at
`apps/assistant_worker/greenbook_assistant_worker/main.py` currently creates a
`JavaClient` and keeps the process alive. It does not construct a Runtime
state manager, retry scheduler, retry manager, or background retry worker.

## Persistence implementation inventory

| Runtime dependency | Memory implementation | PostgreSQL adapter | Current API startup | Current standalone worker |
| --- | --- | --- | --- | --- |
| `ExecutionRepository` | Yes | `PostgresExecutionRepository` | Memory, directly constructed | Not constructed |
| `ExecutionEventStore` | Yes | `PostgresExecutionEventStore` | Memory, directly constructed | Not constructed |
| `CheckpointStore` | `RuntimeManager` private memory map | `PostgresCheckpointStore` | Private memory by default | Not constructed |
| `ExternalOperationStore` | Yes | `PostgresExternalOperationStore` | Not injected into service | Not constructed |
| `RetryTaskStore` | Yes | `PostgresRetryTaskStore` | Not injected into a scheduler | Not constructed |
| `LeaseManager` | `ExecutionLeaseManager` | `PostgresExecutionLeaseManager` | Not constructed | Not constructed |

The PostgreSQL adapters already share a synchronous SQLAlchemy bind and each
adapter can initialize its table metadata. Their existence does not make them
the default: the current API path still calls the memory constructors
explicitly.

## Injection points

The minimal safe injection boundary is the API lifespan. It should create one
`RuntimePersistence` aggregate and pass its members to the state manager,
`RuntimeManager`, and `RuntimeAgentService`. The service then forwards the
checkpoint store and `ExternalOperationTracker` to `ExecutionWorker`.

The same aggregate should be created by the standalone worker process. That
process can then construct a state manager, `RuntimeManager`, `RetryManager`,
`RetryScheduler`, and `RetryBackgroundWorker` over the same durable stores.

The route fallback should consult the configured aggregate if a test or
alternate application does not install an already-built manager. Existing
explicit state injection remains higher priority for compatibility.

## Production selection

Storage selection is configuration-driven:

```text
ASSISTANT_RUNTIME_STORAGE=postgres
ASSISTANT_RUNTIME_DATABASE_URL=postgresql://...
```

`ASSISTANT_DB_URL` and `GREENBOOK_DB_URL` remain accepted aliases for existing
deployments. The provider normalizes async PostgreSQL URLs to the synchronous
driver required by the existing adapters. Selecting PostgreSQL without a
database URL fails during startup instead of silently falling back to memory.

`memory` remains available for local development and unit tests. Production
should select `postgres`, use one provider instance per process, and share the
same database with API and retry-worker processes. Table creation is retained
as adapter bootstrap compatibility; a production deployment should still use
its normal schema migration process rather than relying on startup DDL.

## Lifecycle ownership

The process that creates a PostgreSQL provider owns its SQLAlchemy engine and
must dispose it during shutdown. API clients and the persistence provider are
independent resources; closing the provider must not alter execution state or
invoke business services.

## Result of the audit

The durable adapters are present, but the default API path and the standalone
worker path were not using a common construction boundary. The required
change is therefore provider/DI wiring, not a change to Planner, service
business logic, tool execution, execution states, or external business
systems.

