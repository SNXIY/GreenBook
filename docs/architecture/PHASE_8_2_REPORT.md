# Phase 8.2 Workspace Consolidation Report

本阶段将仍处于 ACTIVE/部署边界的应用统一到 `apps/`，只改变目录和路径引用，不改变业务逻辑、包名、数据库或 Agent Runtime。

## Moved Paths

| Before | After | Result |
| --- | --- | --- |
| `greenbook-backend/` | `apps/backend/` | Moved |
| `greenbook-frontend/` | `apps/frontend/` | Moved |
| `creator-agent/` | `apps/creator-agent/` | Moved |
| `community-assistant-agent/` | unchanged | Retained as compatibility/legacy service |

The three destination directories exist and the three old top-level application directories no longer exist.

## Updated References

Path references were updated in:

- `.github/workflows/verify.yml`
- `docker-compose.yml`
- `infra/docker-compose.dev.yml`
- `scripts/dev-up.ps1`
- `scripts/ensure-jwt-keys.ps1`
- `scripts/setup-dev.ps1`
- `scripts/smoke-test.ps1`
- `scripts/start-be.ps1`
- `scripts/start-creator.ps1`
- `scripts/start-fe.ps1`
- `scripts/verify-all.ps1`
- `scripts/run_p0_e2e.py`
- `community-assistant-agent/tests/test_tool_runtime_step4.py`
- `README.md`
- `docs/greenbook-agent-runtime-technical-introduction.md`

The following were intentionally preserved because they are service identifiers or public contracts rather than filesystem paths:

- `creator-agent` job/service name;
- `/creator-agent` frontend proxy path;
- `greenbook-creator-agent` Python package name;
- Creator identity audience values;
- Java JWT audience values.

Historical reports and archived phase documents retain their original path descriptions. They are records of prior repository states, not executable references.

## Reference Scan

The post-move scan found no executable reference to the old directories `greenbook-backend/`, `greenbook-frontend/`, or the top-level `creator-agent/`. Active references now use `apps/backend/`, `apps/frontend/`, and `apps/creator-agent/`.

The remaining textual matches are limited to service names, URL paths, package metadata, historical architecture documents, and the explicit compatibility directory `community-assistant-agent`.

The root uv workspace was not changed: the moved applications were not workspace members before the move. No Python import path or package name was changed, and `uv.lock` was not rewritten.

## Rollback Instructions

Reverse the directory moves:

```text
apps/backend/       -> greenbook-backend/
apps/frontend/      -> greenbook-frontend/
apps/creator-agent/ -> creator-agent/
```

Then reverse the path-only changes listed above. No database rollback or package rename is required because this phase did not alter schemas, package identities, or runtime state models.

## Test Result

### `pytest tests/unit`

Collection failed with 2 errors after discovering 477 tests and 1 skip. Both errors are caused by the current interpreter missing `fastapi` while importing `apps/assistant_api/greenbook_assistant_api/api/routes.py`:

- `tests/unit/test_revision_orchestration.py`
- `tests/unit/test_time_parser.py`

### `pytest tests/evaluation`

44 tests passed. One test failed because the current interpreter does not have the `openai` module:

- `tests/evaluation/test_intent_v2_llm_eval.py::test_llm_intent_evaluation`

No test failure referenced a moved application path. `git diff --check` reported no whitespace errors for the path updates.

## Scope Confirmation

- `packages/` imports and package names: unchanged
- Planner: unchanged
- Worker: unchanged
- Execution Runtime: unchanged
- ToolRuntime: unchanged
- Database schema: unchanged
- `pyproject.toml`: unchanged
- `uv.lock`: unchanged
- Docker service behavior: unchanged; only host paths were updated
- `community-assistant-agent/`: not moved

