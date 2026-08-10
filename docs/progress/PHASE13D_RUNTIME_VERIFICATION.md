# Phase13-D Runtime Verification Scenarios

## Scope

The verification suite exercises the asynchronous Runtime boundary with a
deterministic MCP test double. The production components remain real:

`RuntimeAgentService` (API dispatch) → `ExecutionQueue` →
`ExecutionQueueWorker` → `RuntimeAgentService.execute_queued` →
`ExecutionWorker` → `ToolRuntime` → EventStore/OperationStore.

The test double replaces only the external MCP service, so the suite does not
require Java, Creator, or network availability.

## Scenarios

1. Query and analyze community posts.
2. Create a Java learning post.
3. Create a post and schedule publication for a future time.

Each scenario asserts that the API-side dispatch does not invoke MCP before the
queue worker claims the message, that the worker acknowledges the queue item,
that the canonical execution reaches `COMPLETED`, and that tool and external
operation evidence is persisted.

The suite also verifies:

- TraceContext across conversation/run/trace/execution/step/tool and operation
  records;
- MemoryMetricsCollector execution, step, and tool counters;
- the timeline read model;
- retry and reconciliation event categories;
- reconciliation success recovering the existing Execution without replaying a
  tool.

No new Execution state, retry scheduler, or external business behavior is
introduced by these tests.
