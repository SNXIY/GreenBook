# Phase 13-B Runtime Metrics Implementation

## Scope

Phase 13-B adds a small instrumentation seam. It does not introduce a
monitoring vendor, exporter, database table, or new Execution state.

## Collector contract

`MetricsCollector` records five Runtime dimensions:

- Execution total/success/failure and cumulative duration;
- Step total/failure and cumulative latency;
- Tool invocation/error counts and cumulative latency;
- Retry requests and eventual retry successes;
- Reconciliation unknown observations and resolved observations.

`MemoryMetricsCollector` is thread-safe and returns a typed
`RuntimeMetricsSnapshot`, including rates and average durations. A process can
later provide another implementation without changing Runtime execution code.

## Integration boundaries

- `ToolRuntime` records one actual tool-handler invocation, including timeout,
  handler failure, pending acknowledgement, and success.
- `ExecutionWorker` records one step result and passes the collector to its
  existing `RetryManager`.
- `RuntimeAgentService` records terminal execution duration.
- `RetryManager` records an authorized retry request; Worker records an
  eventual retry success.
- `ReconciliationService` records `UNKNOWN` versus non-unknown observations.

All records accept the Phase 13-A `TraceContext`; the memory backend currently
aggregates counters rather than exporting high-cardinality labels.

## Runtime wiring

Assistant API and standalone Assistant Worker each create a process-local
`MemoryMetricsCollector` and inject it through their existing dependency
construction. Queue execution uses the Worker process collector, so metrics for
an asynchronous execution are recorded where the execution actually runs.

## Deliberate limits

Metrics are process-local in this phase. Cross-process aggregation, durable
metric storage, dashboards, and alerting remain outside the Phase 13-B scope.

