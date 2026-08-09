# Phase 11.6-D8.4 History Compatibility Reorganization

## Result

The `run_id` to `execution_id` bridge now belongs to the History
compatibility layer:

```text
compatibility/history/
  run_execution_link.py
  execution_reference.py
  run_execution_repository.py
```

The retired Runtime compatibility package has no active exports. The active
Runtime remains `PlanExecution`,
`ExecutionStateManager`, and `ExecutionEventStore`.

## Compatibility behavior

`RunExecutionAdapter` behavior is unchanged:

- mapped Runtime runs resolve `run_id -> execution_id`;
- Legacy-only runs resolve without an execution ID;
- `ExecutionReference` reports the same source and identifiers;
- persistent and in-memory link repositories retain their existing behavior.

The adapter stores identifiers and mapping metadata only. It does not own
Runtime status, events, checkpoints, or execution state.

## Updated consumers

Assistant API routes, runtime linking service, projection tests, freeze tests,
approval reference tests, and link compatibility tests import from
`greenbook_assistant_core.compatibility.history`.

The compatibility test suite is now located at `tests/compat/history` and
covers mapped Runtime runs, Legacy-only runs, persistent links, duplicate
binding protection, and execution references.

## Boundaries preserved

This reorganization does not modify Worker, Planner, ToolRuntime,
ExecutionStateManager, or PlanExecution. It does not delete `assistant_runs`,
`RunRepository`, or `RunExecutionAdapter`; it only places the latter under
the history compatibility ownership boundary.

## Retirement direction

The History compatibility layer remains until Legacy `run_id` consumers and
history lookup endpoints are retired. Runtime execution APIs can continue to
evolve independently because the link layer exposes IDs and references, not a
second execution store.
