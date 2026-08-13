> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Final Repository Audit

本审计基于当前 import、CI、脚本、测试和目录引用完成。结论区分 ACTIVE、COMPATIBILITY、ARCHIVE 和 DELETE_CANDIDATE；未执行代码删除。

## KEEP / ACTIVE

| Path | Role | Evidence |
| --- | --- | --- |
| `apps/assistant_api/` | FastAPI Assistant API、Runtime API 和 legacy compatibility boundary | CI、API routes、Runtime routes |
| `apps/assistant_worker/` | Assistant background worker | uv workspace、worker entrypoint |
| `apps/backend/` | Java community backend | CI、Docker schema mounts、JWT integration |
| `apps/frontend/` | React/Vite user interface | CI、package.json、dev scripts |
| `apps/creator-agent/` | Active Creator service | CI、P0 harness、Dockerfile、Creator client contract |
| `packages/assistant_core/` | IntentSpec、Planner、PlanExecution、Execution Runtime | active imports and unit tests |
| `packages/evaluation/` | Intent/Planner/Execution evaluation infrastructure | evaluator imports and evaluation tests |
| `services/greenbook_mcp/` | Capability/tool service boundary | API imports, tool contracts, CI |
| `tests/` | Active unit, integration, evaluation, compatibility and E2E coverage | pytest collection |

## KEEP / COMPATIBILITY

| Path / symbol | Reason retained | Migration target |
| --- | --- | --- |
| `community-assistant-agent/` | GitHub Actions、P0 scripts、legacy API、`assistant_runs` 和 historical data boundary 仍引用 | Runtime API / `execution_id` adapter |
| `apps/assistant_api/.../services/legacy_agent_service.py` | Existing API fallback and approval compatibility | `RuntimeAgentService` |
| `packages/assistant_core/.../task/models.py:TaskIntent` | Resource resolver, persistence, legacy API and compatibility tests still consume it | `IntentSpec` + `PlanningContext` |
| `packages/assistant_core/.../task/intent_compat.py` | Preserves old consumers without losing IntentSpec snapshot | direct IntentSpec consumers |
| `task/intent_draft.py`, `task/intent_elements.py` | Import shims for migration tests and historical callers | `compatibility/intent/adapter.py`, then archive |
| `packages/assistant_core/.../db/repositories.py:RunRepository` | Legacy routes, approval and run compatibility still use it | `RunExecutionAdapter` / `execution_id` |
| `assistant_runs` and `run_id` | Historical persistence and API contract | execution persistence and references |

## ARCHIVE

| Path | Reason |
| --- | --- |
| `archive/legacy/` | Historical backend and legacy material; no ACTIVE import |
| `archive/creator/` | Superseded Creator service implementation |
| `archive/workflows/` | Superseded MCP workflow files |
| `docs/archive/` | Phase reports and drafts retained for history |
| `docs/reports/` and `docs/drafts/` | Empty process-era containers after archive consolidation; do not add new documents here |

## DELETE CANDIDATE

No production code is safe to delete in this audit. The following remain candidates only after a separate owner and deployment confirmation:

- `community-assistant-agent/` after CI, P0, API, approval and data migration retirement;
- `LegacyAgentService` after the legacy API fallback is removed;
- `TaskIntent` and `intent_compat` after all resolver, persistence and compatibility callers migrate;
- IntentDraft/IntentElements implementations after shim consumers are removed;
- any duplicate Creator implementation after deployment ownership is formally confirmed.

## Duplicate / Boundary Findings

- Runtime state: `PlanExecution` is the ACTIVE source of truth; legacy `assistant_runs` is a compatibility source only for old API paths.
- Creator: `apps/creator-agent` is the complete active deployed Creator service; archived `creator_agent` and `services/creator_agent` are not ACTIVE entrypoints.
- Intent: Direct `IntentSpec` is the formal L2 representation; Draft/Elements are compatibility implementations and must not re-enter the primary understanding path.
- Evaluation: `packages/evaluation` is the formal evaluation library; phase reports and test datasets are not runtime implementations.

## Final Boundary

```text
ACTIVE:
  apps/assistant_api, apps/assistant_worker, apps/backend, apps/frontend,
  apps/creator-agent, packages/, services/greenbook_mcp, tests/

COMPATIBILITY:
  community-assistant-agent, TaskIntent, RunRepository, run_id,
  IntentDraft, IntentElements, LegacyAgentService

ARCHIVE:
  archive/, docs/archive/
```

