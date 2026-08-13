> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D4-C4 Legacy Operation Removal

## 1. Removal scope

The final repository scan found no production import or call site for
`RunOperationAdapter`. Its only remaining consumers were:

- `packages/assistant_core/greenbook_assistant_core/compatibility/runtime/run_operation_adapter.py`
- `tests/compat/runtime/test_run_operation_adapter.py`
- the compatibility package export in `runtime/__init__.py`

All three were removed. No `/runs` control or event route remained in the API
before this deletion.

## 2. Canonical Runtime boundary

Runtime operations remain available only through `execution_id`:

- pause, resume, cancel: Execution Runtime control routes;
- retry: Runtime step retry route;
- approval: execution approval route;
- events and SSE: Runtime Event API and stream route.

The frontend has no ACTIVE `/runs` list, control, approval, or stream
consumer. Legacy-only records remain displayable as history and do not gain a
new operation fallback.

## 3. Retained compatibility layer

`RunExecutionAdapter` remains because it is still required for the
`run_id -> execution_id` reference boundary:

- `/runs/{run_id}` history projection;
- Runtime result link persistence;
- `ExecutionReference` construction;
- Legacy response compatibility;
- persistent link and duplicate-binding tests.

It maps identifiers only and does not own execution state, events, or control
behavior.

The following were deliberately preserved:

- `assistant_runs`;
- `RunRepository`;
- `LegacyAgent`;
- `community-assistant-agent`.

## 4. Files changed

- Deleted `packages/assistant_core/greenbook_assistant_core/compatibility/runtime/run_operation_adapter.py`.
- Deleted `tests/compat/runtime/test_run_operation_adapter.py`.
- Removed `RunOperationAdapter` from the compatibility package exports.
- Added this report.

No Worker, Planner, ToolRuntime, ExecutionStateManager, or PlanExecution code
was modified.

## 5. Verification

- Repository scan: no `RunOperationAdapter` or `run_operation_adapter`
  references remain.
- Runtime API and compatibility tests pass.
- Frontend tests pass.
- `compileall` passes.
- `git diff --check` passes.
