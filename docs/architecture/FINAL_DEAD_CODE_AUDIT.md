# Phase 11 Final Dead Code Audit

## Scope and Method

This is a read-only dead-code audit of the repository at the current working
tree. It covers `community-assistant-agent`, the legacy Agent path,
`TaskIntent` and `intent_compat`, `services/`, `scripts/`, `tests/`, and root
temporary files.

The deletion rule is conjunctive. A file is a deletion candidate only when it
has no production reference, no test reference, no CI reference, no README
reference, and an existing replacement. `rg` scans were used over source,
tests, scripts, CI, README files, and architecture documentation. Generated
environments and caches were treated separately from source code.

The worktree already contains unrelated uncommitted changes. This audit does
not interpret those changes as permission to remove or restore files.

## Executive Result

No production code satisfies the deletion rule in this audit.

Several components are plausible archive candidates, but each still has an
explicit compatibility, workspace, test, CI, or migration dependency. They
are listed below with prerequisites rather than being treated as dead code.

## KEEP / ACTIVE

### Active Runtime

| Path | Role | Evidence / dependants | Decision |
| --- | --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/` | Assistant core, IntentSpec, planning, execution, capabilities, and persistence contracts | Imported by the Assistant API and covered by unit/evaluation tests | KEEP |
| `apps/assistant_api/` | Current API composition and Runtime API boundary | Application entrypoint, API routes, runtime services, and tests | KEEP |
| `apps/assistant_worker/` | Worker process and execution integration | Deployment/startup configuration and worker tests | KEEP |
| `services/greenbook_mcp/` | Active MCP server, registry, schemas, and community/content tools | Imported by API/tool integration and tested under MCP/integration suites | KEEP |
| `apps/backend/` | Java backend/client integration | Backend tests, deployment configuration, and integration references | KEEP |
| `apps/frontend/` | Execution Console and user-facing application | Frontend workspace and API integration | KEEP |
| `apps/creator-agent/` | Current Creator service implementation | Current workspace application and Creator-specific tests/deployment files | KEEP |
| `packages/creator_client/` | Client boundary used to reach Creator capabilities | Assistant API, MCP context/tools, and integration tests | KEEP |

These paths are part of the ACTIVE architecture and are outside the scope of
safe dead-code removal.

### Operational Scripts

The following scripts have operational, verification, or CI/P0 roles and must
remain available:

- `scripts/run_p0_e2e.py` and `scripts/test_run_p0_e2e.py`: P0 process harness
  and its test; referenced by the verification workflow.
- `scripts/runtime-report.ps1`, `scripts/smoke-test.ps1`,
  `scripts/setup-dev.ps1`, and `scripts/verify-all.ps1`: runtime reporting,
  smoke testing, environment setup, and verification entrypoints. Several
  also reference `community-assistant-agent`.
- `scripts/start-assistant.ps1`, `scripts/start-be.ps1`,
  `scripts/start-creator.ps1`, `scripts/start-fe.ps1`, `scripts/dev-up.ps1`,
  and `scripts/e2e-test.ps1`: local service and end-to-end entrypoints.
- `scripts/ensure-jwt-keys.ps1`: local/development authentication setup.

Decision: KEEP pending a separate script ownership and CI audit.

## COMPATIBILITY

### Legacy Agent and Run Contract

| Path / symbol | Role | Current references | Migration target | Decision |
| --- | --- | --- | --- | --- |
| `community-assistant-agent/` | Historical Assistant application, legacy API/data behavior, approval and `run_id` flows | `.github/workflows/verify.yml`, `scripts/run_p0_e2e.py`, `scripts/runtime-report.ps1`, `scripts/smoke-test.ps1`, `scripts/setup-dev.ps1`, `scripts/verify-all.ps1`, and its own app/tests/migrations | Runtime API, `execution_id`, and the run-to-execution compatibility adapter | KEEP |
| `packages/assistant_core/greenbook_assistant_core/agent.py` | Legacy Agent implementation | `apps/assistant_api/.../services/legacy_agent_service.py` and legacy/integration tests | Retire after legacy API and data migration | KEEP / MIGRATE |
| `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py` | Legacy API service/fallback | Constructed by `main.py`, used by `assistant_service.py`, and lazily imported by `api/routes.py` | RuntimeAgentService and Execution Runtime API | KEEP / MIGRATE |
| `packages/assistant_core/greenbook_assistant_core/db/repositories.py` (`RunRepository`) | Persistence for legacy runs | API routes and legacy persistence paths | `RunExecutionAdapter` plus execution repositories | KEEP / MIGRATE |
| `run_id`, `assistant_runs` | Legacy public/data identifiers and records | API, approval, MCP context, integration/contract/E2E tests, and P0 harness | `execution_id` with persisted compatibility links | COMPATIBILITY |

These are not dead code. Removing them now would break legacy API behavior,
test harnesses, or historical data access.

### Intent Compatibility

| Path / symbol | Role | Current references | Removal prerequisite | Decision |
| --- | --- | --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/task/intent_draft.py` | Old import shim | `tests/compat/intent/test_intent_draft.py` and legacy import boundary | All old imports and compatibility tests retired | ARCHIVE CANDIDATE |
| `packages/assistant_core/greenbook_assistant_core/task/intent_elements.py` | Old import shim | `tests/compat/intent/test_intent_elements.py` and legacy import boundary | Same as above | ARCHIVE CANDIDATE |
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_draft.py` | IntentDraft and compiler implementation | Compatibility adapter, retained legacy parser methods, and compatibility tests | Legacy parser methods removed or isolated | KEEP / MIGRATE |
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_elements.py` | IntentElements and builder implementation | Compatibility adapter, retained legacy parser methods, and compatibility tests | Legacy parser methods removed or isolated | KEEP / MIGRATE |
| `packages/assistant_core/greenbook_assistant_core/task/intent_compat.py` | IntentSpec-to-TaskIntent projection | `tests/evaluation/intent_spec_consumer_loss.py` and compatibility consumers | Planner and evaluation no longer need legacy projection | KEEP / MIGRATE |
| `TaskIntent` | Legacy task contract still used by resolver/resource and compatibility consumers | `task/resolver.py`, `resource/resolver.py`, registry, planner/evaluation/human/memory code, and tests | Complete consumer migration to IntentSpec/PlanningContext | KEEP / MIGRATE |

`IntentCompiler` and `IntentSpecBuilder` are symbols embedded in the
compatibility implementation modules; they are not standalone files.
`task/understanding.py` remains an important boundary because it retains
legacy parser methods and compatibility imports even though Direct
IntentSpec is the formal L2 path.

## ARCHIVE CANDIDATE

These candidates require one more reference/deployment decision before any
move. They are not deletion candidates.

| Path | Current evidence | Why not safe now | Proposed action |
| --- | --- | --- | --- |
| `services/creator_agent/` | Workspace/package metadata references; no discovered Python runtime import | Root `pyproject.toml`, `uv.lock`, and its package metadata still reference it | ARCHIVE after manifest/lockfile audit |
| `services/creator_agent/greenbook_creator_agent/` | Package skeleton only; no direct runtime import found | Parent workspace dependency has not been removed | ARCHIVE with parent |
| `services/greenbook_mcp/.../workflows/create_draft.py` | No caller found outside the module in the scan | Workflow deployment and manual invocation need confirmation | ARCHIVE after caller/deployment audit |
| `services/greenbook_mcp/.../workflows/revise_draft.py` | No caller found outside the module in the scan | Same as above | ARCHIVE after caller/deployment audit |
| Historical Creator portions under `community-assistant-agent/` | Legacy application and data boundary | CI, P0, API and data migration still depend on the parent application | ARCHIVE only after legacy retirement |
| Historical phase reports and drafts under `docs/` | Process documentation rather than runtime code | Some are retained as project history and may be referenced by navigation | ARCHIVE through documentation-only cleanup |

The exact workflow paths should be resolved with `rg --files` before a future
move; this report intentionally does not move or rename them.

## DELETE_CANDIDATE

### Production and Compatibility Code

None.

No audited production file meets all five deletion conditions. In particular,
the following are explicitly **not** safe to delete:

- `community-assistant-agent/`, because CI, P0, scripts, API/data behavior,
  and its own tests still reference it;
- `LegacyAgentService` and `agent.py`, because the API still constructs or
  lazily imports the legacy path;
- `TaskIntent`, `intent_compat`, `RunRepository`, and `run_id`, because they
  remain part of active compatibility consumers;
- IntentDraft/IntentElements shims and implementations, because compatibility
  tests and the legacy parser boundary still use them;
- `services/greenbook_mcp/`, because it is an active tool service.

### Low-Risk Script Candidates Requiring Proof

The following are only provisional candidates, not approved deletions:

- `scripts/Import-GreenBookEnv.ps1`
- `scripts/ops/promote-admin.ps1`
- `scripts/rotate-dev-secrets.ps1`

The scan did not establish production callers for these scripts, but absence
of a textual caller is not proof that developers or deployment runbooks do
not use them. They may become `DELETE_CANDIDATE` only after checking CI,
deployment documentation, runbooks, and ownership.

## Services Audit

`services/greenbook_mcp/` is ACTIVE: its server, registry, schemas, tools,
and tests form the MCP/tool boundary used by the runtime.

`services/creator_agent/` is a compatibility/workspace package skeleton rather
than the current Creator runtime. It is an ARCHIVE CANDIDATE after the root
workspace metadata and lockfile no longer require it. The active Creator
service is `apps/creator-agent/`; the two must not be conflated.

No other `services/` directory can be classified as deletable from this audit
without a deployment-level check.

## Tests Audit

The current test tree contains active unit, evaluation, integration, contract,
E2E, and compatibility suites. It also contains explicit tests for legacy
contracts, including:

- `tests/compat/intent/` for IntentDraft and IntentElements shims;
- `tests/compat/runtime/` for run/execution mapping;
- `tests/evaluation/intent_spec_consumer_loss.py` for the TaskIntent projection;
- legacy Agent, API, runtime, MCP, Creator, and P0 harness tests throughout
  `tests/` and the relevant application directories.

These tests are evidence of compatibility obligations, not dead tests. A test
may be archived only together with the behavior it protects and after CI
ownership is updated. No test is recommended for deletion by this audit.

## Root Temporary Files

| Path / pattern | Classification | Reason | Action |
| --- | --- | --- | --- |
| `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `__pycache__/`, `*.pyc` | Local generated | Reproducible tooling output, not source | Handle in a dedicated generated-file cleanup; not part of this audit |
| `.venv/`, `.venv-v2/` | Local environment | Developer environments, not repository runtime code | KEEP locally or clean through environment policy |
| `.idea/`, `.vscode/` | Local IDE metadata | May contain developer settings; not proven disposable | KEEP / review separately |
| `.env` | Local configuration/secrets | Not source and potentially operational; must not be deleted casually | KEEP / manage through secret policy |
| `CLEANUP_REPORT.md`, `MOVE_PLAN.md`, `PROJECT_CONTEXT.md`, `PHASE_10_FINAL_REPORT.md` | Root project/process documents | Historical or workspace guidance; not code | ARCHIVE/retain only after documentation index review |
| `greenbook-backend/` | Existing workspace directory | Current working tree contains it; role and ownership must be confirmed | KEEP pending separate repository-structure audit |

No root file was deleted or moved. Local generated artifacts should not be
confused with dead production code, and their cleanup belongs to a separate,
explicitly scoped housekeeping phase.

## Reference Boundary Summary

```text
ACTIVE Runtime
  IntentSpec -> Validator -> PlanningContext -> Planner -> TaskPlan
  -> PlanExecution -> ExecutionStateManager -> Worker -> ToolRuntime

Compatibility boundary
  Legacy API / run_id / assistant_runs
  -> RunExecutionAdapter -> execution_id / Execution Runtime

Legacy implementation
  LegacyAgentService -> agent.py / community-assistant-agent
```

The ACTIVE Planner and Execution Runtime do not directly depend on the legacy
Agent implementation. The remaining dependencies are at API,
understanding-compatibility, persistence, test, CI, and deployment boundaries.
That distinction supports future migration, but does not satisfy the current
deletion proof.

## Final Decision

- KEEP all ACTIVE Runtime, API, MCP, Creator client/service, operational
  scripts, and current test infrastructure.
- KEEP or MIGRATE compatibility layers until their consumers and data are
  retired.
- ARCHIVE only the explicitly listed workspace skeletons and unwired workflow
  modules after a second deployment/reference audit.
- DELETE_CANDIDATE: none at this time.

This report is analysis only. No source code, tests, imports, CI files, or
runtime behavior were modified by the audit.
