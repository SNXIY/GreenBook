> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 9.2 Runtime Control API Report

## Implemented Endpoints

Added to `apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py`:

| Method | Path | Runtime delegate |
| --- | --- | --- |
| POST | `/executions/{execution_id}/pause` | `RuntimeManager.pause_execution()` |
| POST | `/executions/{execution_id}/resume` | `RuntimeManager.resume_execution()` |
| POST | `/executions/{execution_id}/cancel` | `RuntimeManager.cancel_execution()` |
| POST | `/executions/{execution_id}/steps/{step_id}/retry` | `RetryManager.retry_step()` |

Control responses reuse the existing execution and step response models. No new execution state or step model was introduced.

## State and Error Handling

- Missing execution or step: `404`.
- Illegal state transition: `409`.
- Non-retryable or exhausted step retry: `409`.
- Missing authentication: `401`.
- Optional host-provided `execution_authorizer` denial: `403`.

The API checks access before invoking a mutation. State changes are delegated to `RuntimeManager`, `ExecutionStateManager` through that manager, and `RetryManager`; the API never writes to the repository directly.

## Permission Boundary

Mutation endpoints require `request.state.auth_context`. The API supports an optional `request.app.state.execution_authorizer(auth_context, execution)` callback for user/tenant ownership checks without adding ownership fields to `PlanExecution`. If no callback is configured, authentication remains required and the host's existing authorization boundary applies.

## Tests

Added `tests/unit/test_execution_control_api.py`, covering:

- normal `RUNNING -> PAUSED -> RUNNING -> CANCELLED` flow;
- illegal pause conflict;
- retryable `TIMEOUT` failure reset to `PENDING`;
- unauthenticated request rejection;
- explicit execution authorizer denial.

Result in the current interpreter:

- `pytest tests/unit/test_execution_control_api.py`: skipped because `fastapi` is not installed;
- `python -m py_compile` for API and test: passed;
- `git diff --check`: passed.

This environment limitation is consistent with the existing unit-suite collection failure and is not caused by the API change.

## Scope Confirmation

Unchanged:

- PlanExecution model;
- Execution Runtime core and state transition logic;
- Worker;
- ToolRuntime;
- Planner;
- IntentSpec and Validator;
- repository implementation.

