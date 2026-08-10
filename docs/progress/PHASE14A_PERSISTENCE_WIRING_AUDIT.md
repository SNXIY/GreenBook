# Phase 14-A Persistence Wiring Audit

## Scope

This audit follows the production construction paths for the Assistant API,
the standalone Assistant worker, `RuntimeAgentService`, `ExecutionWorker`,
the state manager, and the Runtime persistence aggregate. It only addresses
dependency selection and lifecycle wiring; Planner, TaskProvider,
TaskOrchestrator, business services, and execution states are out of scope.

## Current construction path

The API lifespan constructs one `RuntimePersistence` aggregate and injects its
members into the state manager, `RuntimeManager`, `RuntimeAgentService`, the
external-operation tracker, and the retry scheduler:

```text
API lifespan
  -> RuntimePersistenceFactory
  -> ExecutionRepository
  -> ExecutionEventStore
  -> CheckpointStore
  -> ExternalOperationStore
  -> RetryTaskStore
  -> ExecutionQueue
  -> LeaseManager
  -> RuntimeAgentService / Runtime routes
```

The standalone worker also calls `RuntimePersistenceFactory.from_env()` and
uses the provider's retry task store. The PostgreSQL provider constructs all
seven database-backed dependencies from one SQLAlchemy bind and disposes an
owned engine during shutdown.

## Persistence inventory

| Dependency | Memory implementation | PostgreSQL implementation | Current wiring result |
| --- | --- | --- | --- |
| Execution repository | `ExecutionRepository` | `PostgresExecutionRepository` | Provider-selected and injected by API/worker |
| Execution events | `ExecutionEventStore` | `PostgresExecutionEventStore` | Provider-selected and injected by API/worker |
| Checkpoints | `MemoryCheckpointStore` | `PostgresCheckpointStore` | Provider-selected and injected into `RuntimeManager` |
| External operations | `ExternalOperationStore` | `PostgresExternalOperationStore` | Provider-selected and injected into `ExternalOperationTracker` |
| Retry tasks | `RetryTaskStore` | `PostgresRetryTaskStore` | Provider-selected and injected into `RetryScheduler` |
| Execution queue | `ExecutionQueue` | `PostgresExecutionQueue` | Provider-selected and exposed to API/worker |
| Execution lease | `ExecutionLeaseManager` | `PostgresExecutionLeaseManager` | Constructed and exposed; normal consumer use is audited in Phase 14-A-2 |

The adapters were therefore present and connected when PostgreSQL was
explicitly selected. The problem was the selection path, not the adapter
construction itself.

## Production wiring gaps found

### 1. Existing database configuration was ignored

`RuntimePersistenceFactory` previously looked only at
`ASSISTANT_RUNTIME_DATABASE_URL`, `ASSISTANT_DB_URL`, and `GREENBOOK_DB_URL`.
The repository's shared `.env.example` supplies `ASSISTANT_DATABASE_URL`.
With no `ASSISTANT_RUNTIME_STORAGE`, the factory consequently selected Memory
and the API's dispatch default became `direct`.

The factory now accepts `ASSISTANT_DATABASE_URL` and automatically selects the
PostgreSQL profile when any supported database URL is configured. An explicit
`ASSISTANT_RUNTIME_STORAGE=memory` still wins, so unit tests and local memory
development remain available.

### 2. PostgreSQL driver was not an Assistant Core runtime dependency

The adapters normalize PostgreSQL URLs to `postgresql+psycopg://`, but the
Assistant Core package did not declare `psycopg`. The package now declares
`psycopg[binary]>=3.2`, so API and worker deployments receive the driver
through their Core dependency.

### 3. API dispatch consequence

The API already defaults to `queue` when the selected persistence profile is
PostgreSQL. After the provider fix, the documented `ASSISTANT_DATABASE_URL`
configuration selects PostgreSQL and therefore selects queue dispatch. An
explicit `ASSISTANT_EXECUTION_DISPATCH=direct` remains a compatibility escape
hatch and is not the production default; its use should be visible in the
startup log.

### 4. Worker selection consequence

The worker's queue-consumer default still reads the raw
`ASSISTANT_RUNTIME_STORAGE` variable instead of the selected provider. This is
a real Phase 14-A-2 lifecycle defect and is intentionally fixed with the
worker wiring changes, using `persistence.storage` as the source of truth.

## Lifecycle observations

The API owns and closes its persistence aggregate. The standalone worker owns
and closes its own aggregate and already constructs a real retry background
consumer. The remaining lifecycle work is to make the execution consumer
default follow the provider, pass the lease manager to the queue consumer, and
make shutdown release both queue and retry claims. Those changes are outside
this persistence-selection commit.

## Verification

The persistence tests now cover:

- `ASSISTANT_DATABASE_URL` selecting the PostgreSQL adapter profile;
- explicit Memory selection overriding a configured database URL; and
- the existing shared-bind and missing-configuration behavior.

No service or external database is started by this audit.
