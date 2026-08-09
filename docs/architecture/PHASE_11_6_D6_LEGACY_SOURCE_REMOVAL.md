# Phase 11.6-D6 Legacy Agent Source Removal

## 1. Reference audit

| Symbol/file | ACTIVE dependency | Test dependency | Documentation/archive references | Result |
| --- | --- | --- | --- | --- |
| `LegacyAgentService` | None | None after D5 fallback test removal | Historical phase reports and migration docs | Source deleted |
| `packages/assistant_core/greenbook_assistant_core/agent.py` | None | None | Historical architecture plans and retirement reports | Source deleted |
| `LegacyFallbackAdapter` | None | None | D5 retirement report | Already deleted in D5 |
| `community-assistant-agent/` | Not imported by ACTIVE Runtime | Own service tests and operational scripts | Integration, deployment, JWT audience docs | Retained |

The final Python scan found no import of `LegacyAgentService`,
`greenbook_assistant_core.agent`, or the removed Legacy source files outside
historical documentation. `AssistantService` no longer accepts a Legacy
implementation and returns an explicit failure for the retired Legacy route.

## 2. Deleted source

- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
- `packages/assistant_core/greenbook_assistant_core/agent.py`

No directory became empty as a result. The Assistant API `services/` package
and `greenbook_assistant_core/` package still contain active modules.

## 3. Preserved history boundary

The following were intentionally not changed:

- `assistant_runs` and its database migrations;
- `RunRepository`;
- `RunExecutionAdapter` and persistent Run/Execution links;
- Legacy `GET /runs` history lookup;
- `community-assistant-agent/`.

Historical documentation may continue to mention the removed implementation;
those references describe past architecture and are not runtime imports.

## 4. Community assistant status

`community-assistant-agent` is not an ACTIVE GreenBook Runtime dependency,
but it remains outside this deletion because it is still referenced by:

- `scripts/runtime-report.ps1`;
- P0 E2E environment setup and JWT audience configuration;
- `scripts/start-assistant.ps1`;
- `docs/COMMUNITY_ASSISTANT.md` and `docs/INTEGRATION.md`;
- its own service tests and operational metadata.

That service requires a separate archive/operations migration. No CI, Docker,
JWT, or service metadata was modified in this phase.

## 5. Verification

- Full unit: `518 passed, 1 skipped`.
- Compatibility/evaluation: `59 passed, 1 skipped`.
- Frontend tests: `4 passed`.
- Frontend build: passed.
- Python `compileall`: passed.
- `git diff --check`: passed.

Worker, Planner, ToolRuntime, ExecutionStateManager, and PlanExecution were
not modified.
