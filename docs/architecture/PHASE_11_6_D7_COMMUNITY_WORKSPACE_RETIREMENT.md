# Phase 11.6-D7 Community Assistant Workspace Retirement

## 1. Result

The former Community Assistant workspace is no longer part of the ACTIVE
workspace. It was moved, without deleting files, to:

```text
archive/legacy/community-assistant-agent/
```

The historical product note was also moved to:

```text
docs/archive/history/COMMUNITY_ASSISTANT.md
```

The ACTIVE execution path remains:

```text
apps/assistant_api -> packages/assistant_core -> Execution API
```

`assistant_runs`, `RunRepository`, and `RunExecutionAdapter` were deliberately
left in place. They remain the compatibility/history boundary and were not
made dependent on the archived workspace.

## 2. Reference migration

| Surface | Result |
| --- | --- |
| Runtime startup | `scripts/start-assistant.ps1` starts the ACTIVE Assistant API and no longer uses the archived workspace as a default. |
| P0 E2E | `scripts/run_p0_e2e.py` starts the active API and uses the Runtime audience and `execution_id` lifecycle. |
| Runtime report | `scripts/runtime-report.ps1` queries `GET /api/v1/assistant/executions`; it does not import or enter a legacy directory. |
| CI | The Assistant job runs the repository workspace tests (`tests/unit`, `tests/evaluation`). No legacy working directory remains. |
| Docker | Compose files contain infrastructure only and had no direct legacy workspace dependency. |
| Integration docs | The active JWT contract now names `greenbook-assistant-runtime`. Historical product documentation is archived. |

## 3. JWT audience migration

`greenbook-assistant-runtime` is now the canonical audience for user access
tokens consumed by the active Assistant Runtime.

Updated components:

- Python Assistant API default audience;
- `.env.example` and Assistant startup configuration;
- P0 E2E environment;
- Java token issuance;
- Java audience validation;
- Assistant capability delegation validation;
- Java audience contract tests.

The old audience is not emitted by the ACTIVE issuer and is not accepted by
the ACTIVE capability boundary. Existing tokens must be refreshed through the
normal login/refresh flow. Rollback is limited to restoring the previous
application release and configuration before issuing new tokens; no database
or execution-state migration is involved.

## 4. Archive and recovery

The move is reversible with:

```powershell
Move-Item archive/legacy/community-assistant-agent community-assistant-agent
Move-Item docs/archive/history/COMMUNITY_ASSISTANT.md docs/COMMUNITY_ASSISTANT.md
```

The archived application retains its own source, tests, migrations, and
metadata. It is not included in the ACTIVE Python workspace or Runtime
control path.

## 5. Verification boundary

Scans covered Python imports, scripts, CI, Docker, E2E, integration docs, and
JWT audience configuration. Remaining references to the old workspace name are
historical architecture/audit records or the archived directory itself; no
ACTIVE Runtime source imports it.

The following were not changed:

- Worker;
- Planner;
- ToolRuntime;
- ExecutionStateManager;
- PlanExecution;
- `assistant_runs`;
- `RunRepository`;
- `RunExecutionAdapter`.

## 6. Verification results

Recorded after the migration:

- Java backend: `mvn -q test` passed;
- compatibility/history: `15 passed`;
- evaluation (excluding the optional LLM case): `44 passed`;
- ACTIVE Python `compileall`: passed; the requested historical
  `packages/persistence` path does not exist in this checkout;
- `git diff --check`: passed;
- full unit/evaluation collection is environment-blocked in the system
  interpreter because `fastapi` and `openai` are not installed. The isolated
  LLM evaluation also requires `openai`.

The remaining old workspace-name matches are confined to historical
architecture/audit documents and archived content. ACTIVE scripts, CI,
Docker, tests, and source configuration contain no such reference.
