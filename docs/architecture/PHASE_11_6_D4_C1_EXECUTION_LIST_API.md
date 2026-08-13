> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D4-C1 Execution List API

## 1. Implemented Endpoint

```http
GET /api/v1/assistant/executions?limit=20&cursor=<opaque-cursor>
```

The route is registered by the existing Runtime API router. It is a read-only
Runtime-native list endpoint and does not call `RunRepository`, read
`assistant_runs`, or inspect Legacy execution state.

## 2. Response Contract

```json
{
  "items": [
    {
      "execution_id": "execution-123",
      "task_id": "task-456",
      "plan_id": "plan-789",
      "status": "RUNNING",
      "current_step": "search_posts",
      "progress": 0.5,
      "created_at": "2026-08-09T00:00:00Z",
      "updated_at": "2026-08-09T00:01:00Z"
    }
  ],
  "next_cursor": null
}
```

`progress` is derived from completed steps divided by total steps. Empty plans
return `1.0`, matching the existing Runtime status endpoint semantics.

## 3. Data Flow

```text
Execution API
  -> RuntimeManager.list_executions()
  -> ExecutionStateManager.list_executions()
  -> ExecutionRepository.list_all()
  -> PlanExecution projection
```

The API derives `current_step`, `progress`, and `status` from each
`PlanExecution`. No `assistant_runs` fields are used as a state source.

`RuntimeManager.list_executions()` is a thin delegation method; it does not
create another state store or execution model.

## 4. Authorization

Every request requires an authenticated `AuthContext` and a configured
`request.app.state.execution_authorizer`.

- Without authentication: `401`.
- Without an explicit execution authorization policy: `403`.
- Unauthorized executions are filtered from the result set.
- Authorized executions only are paginated and returned.

This fail-closed behavior is required because the current `PlanExecution`
model does not itself contain tenant/user ownership fields. The host must
provide the ownership policy before enabling the endpoint in production.

## 5. Cursor Pagination

- Default page size: `20`.
- Allowed page size: `1..100`.
- Sort order: `updated_at DESC`, then `execution_id DESC`.
- Cursor payload: opaque URL-safe encoding of the last sort key.
- Invalid cursors return `400`.
- `next_cursor` is returned only when another authorized page exists.

The cursor is independent of `run_id` and remains valid across Legacy
history/projection changes.

## 6. Frontend Compatibility

The response fields are compatible with the existing TaskCenter execution
summary needs. The next frontend migration can replace the remaining
`GET /runs` index discovery with this endpoint and use `execution_id` as the
primary key.

Until that migration is completed:

- mapped Runtime status and steps use Execution API;
- `/runs` remains only as a compatibility metadata/history index;
- Legacy-only history retains `run_id`.

No Legacy endpoint was deleted.

## 7. Files Changed

- `packages/assistant_core/greenbook_assistant_core/execution/runtime_manager.py`
  - Added a read-only delegation for execution listing.
- `apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py`
  - Added list response models, authorization filtering, cursor handling, and
    `GET /executions`.
- `tests/unit/test_execution_list_api.py`
  - Added response, authorization, filtering, pagination, and Runtime-source
    coverage.

## 8. Verification

- Execution list/API/control tests: `12 passed`.
- Python `compileall`: passed.
- `git diff --check`: passed.

## 9. Unchanged Boundaries

Not modified:

- `Worker`
- `Planner`
- `ToolRuntime`
- `ExecutionStateManager` implementation
- `PlanExecution`
- `assistant_runs` schema
- `RunRepository`
- Legacy `/runs` API
