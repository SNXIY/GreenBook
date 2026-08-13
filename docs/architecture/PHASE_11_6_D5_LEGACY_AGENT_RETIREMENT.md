> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D5 Legacy Agent Retirement Audit

## 1. Result

The default Assistant path is Runtime-only. No ACTIVE request path imports or
constructs `LegacyAgentService`, and Runtime failures no longer invoke a
Legacy fallback.

The historical `assistant_runs` and `RunRepository` boundary is unchanged.
`RunExecutionAdapter` remains the identifier/reference bridge for history and
projection.

## 2. Legacy Agent classification

| Item | Classification | Finding | Action |
| --- | --- | --- | --- |
| `apps/assistant_api/.../services/legacy_fallback_adapter.py` | DELETE candidate | No remaining production or test import after fallback removal | Deleted |
| `apps/assistant_api/.../services/legacy_agent_service.py` | DELETE candidate | No application import or initialization remains; file is retained for a later explicit archive/removal decision | Keep temporarily |
| `packages/assistant_core/.../agent.py` | DELETE candidate | Historical implementation; no ACTIVE Runtime import | Keep temporarily pending Legacy Agent archive decision |
| `ENABLE_LEGACY_AGENT_FALLBACK` | DELETE candidate | No active configuration consumer remains | Removed from service initialization |
| `ASSISTANT_RUNTIME_MODE=off` | COMPATIBILITY/test dependency | `RuntimeRouter` still recognizes explicit off mode and one historical revision test documents that path | No implicit use; test is retired/skipped |
| `community-assistant-agent/` | DOC/script/integration dependency | Referenced by runtime-report scripts, P0 E2E environment, integration docs, and JWT audience configuration | Do not delete in this phase |

## 3. Service changes

`AssistantService` now:

- accepts only the Runtime mode;
- returns `LEGACY_EXECUTION_DISABLED` for an explicit Legacy route;
- preserves Runtime errors as Runtime failures;
- has no `LegacyFallbackAdapter` dependency or fallback telemetry;
- does not construct or call `LegacyAgentService`.

The application lifespan and test-only lazy initialization no longer create a
Legacy Agent instance. `RuntimeAgentService`, Worker, Planner, ToolRuntime,
and execution state models were not changed.

## 4. Community assistant audit

The `community-assistant-agent` directory is not an ACTIVE GreenBook Runtime
dependency, but it cannot be deleted safely yet:

- `scripts/runtime-report.ps1` changes into its workspace;
- `scripts/run_p0_e2e.py` starts the current assistant workspace and sets the
  `community-assistant-agent` JWT audience;
- `scripts/start-assistant.ps1` retains the same audience default;
- `docs/COMMUNITY_ASSISTANT.md` and `docs/INTEGRATION.md` document its
  deployment/integration contract;
- CI/Docker references must be removed or redirected in a separate migration
  before archive.

These are deployment and operational references, not Runtime execution
dependencies. No CI/Docker/script/JWT changes are made here.

## 5. Preserved data and compatibility

The following remain untouched:

- `assistant_runs` and its migrations;
- `RunRepository`;
- `RunExecutionAdapter` and persistent link repositories;
- `LegacyAgent` source and `community-assistant-agent`.

Retiring the execution implementation does not remove or rewrite historical
run records.

## 6. Verification

- Full unit tests: `518 passed, 1 skipped`.
- Compatibility runtime and evaluation tests: `59 passed, 1 skipped`.
- Frontend tests: `4 passed`.
- Frontend production build: passed.
- Python `compileall`: passed.
- `git diff --check`: passed.

The skipped test is the former explicit `ASSISTANT_RUNTIME_MODE=off` revision
execution scenario; it is retained only as a documented retirement marker,
not as an executable Legacy path.
