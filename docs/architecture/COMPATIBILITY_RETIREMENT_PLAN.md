# Compatibility Retirement Plan

## 1. Purpose and Boundary

This document defines how GreenBook can retire compatibility code without
changing the ACTIVE Runtime contract:

```text
User -> IntentSpec -> Validator -> PlanningContext -> Planner -> TaskPlan
      -> PlanExecution -> ExecutionStateManager -> Worker -> ToolRuntime
```

The plan is based on `ACTIVE_ARCHITECTURE.md` and
`FINAL_DEAD_CODE_AUDIT.md`. It is a migration plan only. It does not delete,
move, or modify code.

The central rule is that compatibility layers may translate identifiers or
legacy representations, but must not become a second source of execution
state. `PlanExecution` remains the execution source of truth.

## 2. Retirement Principles

1. Migrate consumers before implementations.
2. Keep public legacy API paths working while their internals are adapted.
3. Preserve historical `run_id` and `assistant_runs` reads until data and API
   migration is complete.
4. Retire one boundary at a time and prove the absence of production, test,
   CI, README, and deployment references before removal.
5. Keep compatibility tests until the corresponding compatibility contract is
   intentionally retired.
6. Do not copy `ExecutionStatus`, events, checkpoints, or step state into a
   compatibility model.

## 3. Compatibility Inventory

| Compatibility area | Main paths / symbols | Current role | Migration target | Risk |
| --- | --- | --- | --- | --- |
| Legacy intent models | `compatibility/intent/intent_draft.py`, `compatibility/intent/intent_elements.py`, `task/intent_draft.py`, `task/intent_elements.py` | Preserve historical Draft/Elements imports and parsing behavior | Direct `IntentSpec` extraction and validation | High |
| Legacy intent adapter | `compatibility/intent/adapter.py` | Single bridge for old parser/compiler/builder operations | Remove after legacy parser callers are retired | High |
| Legacy task contract | `task/models.py:TaskIntent` | Input to resolver/resource, human, memory, evaluation, and older tests | `IntentSpec` plus `PlanningContext` | High |
| Intent projection | `task/intent_compat.py` | Projects richer IntentSpec into TaskIntent for legacy consumers | Planner and consumers read IntentSpec directly | High |
| Legacy Agent | `apps/assistant_api/.../services/legacy_agent_service.py`, `packages/.../agent.py` | Legacy API fallback and approval compatibility | `RuntimeAgentService`, Runtime API, and Execution Runtime | Very high |
| Legacy application | `community-assistant-agent/` | Legacy API, `assistant_runs`, approvals, P0 and CI surface | Active Runtime/API and execution persistence | Very high |
| Legacy run identity | `run_id`, `assistant_runs`, `agent_run_id` | Public/API, MCP, Java client, approval and test identifiers | `execution_id` with persisted RunExecutionAdapter links | Very high |
| Run persistence | `packages/.../db/repositories.py:RunRepository` | Stores/reads legacy run records | Execution repositories plus compatibility lookup | Very high |
| Run operation adapter | `compatibility/history/run_execution_link.py`, `run_operation_adapter.py`, `execution_reference.py` | Maps old IDs and operations to execution APIs | Remove only after legacy API paths are retired | High |
| Legacy Creator workspace | `services/creator_agent/` | Workspace/package skeleton with metadata references | `apps/creator-agent/` and `packages/creator_client/` | Medium |
| Unwired Creator workflows | `services/greenbook_mcp/.../workflows/create_draft.py`, `revise_draft.py` | Historical workflow functions with no discovered active caller | Active capability/content tool path | Medium |

## 4. Retirement Tracks

### Track A: IntentDraft and IntentElements

**Current references**

- `packages/assistant_core/greenbook_assistant_core/task/understanding.py`
  retains legacy parser methods and imports the compatibility adapter.
- `packages/assistant_core/greenbook_assistant_core/compatibility/intent/adapter.py`
  exposes the historical parsing/building operations.
- `packages/assistant_core/greenbook_assistant_core/task/intent_draft.py` and
  `task/intent_elements.py` preserve old import paths.
- `tests/compat/intent/test_intent_draft.py` and
  `tests/compat/intent/test_intent_elements.py` protect the compatibility
  contract.

**Migration target**

Direct L2 `IntentSpec` extraction, `IntentValidator`, and targeted repair.
The formal path must not construct `IntentElements`, `IntentDraft`, a builder,
or a compiler.

**Retirement conditions**

- All production callers of the legacy parser methods are removed or moved
  behind an explicitly isolated legacy module.
- `understanding.py` no longer has an eager compatibility dependency on the
  old implementation; remaining fallback code uses a deliberate lazy boundary
  or is retired.
- Compatibility tests are either retired with the contract or moved to a
  separately owned legacy test suite.
- Repository-wide scans show no production import of the old models except
  the intentional shim during the deprecation window.

**Risk**

High. Removing the shim early can break external imports and hidden legacy
flows. Removing the parser before its tests and callers are retired can cause
silent understanding regressions.

### Track B: TaskIntent and intent_compat

**Current references**

- `task/resolver.py` and `resource/resolver.py` consume `TaskIntent`.
- Registry, Planner-adjacent, human, memory, and evaluation code still use
  the model.
- `tests/evaluation/intent_spec_consumer_loss.py` explicitly measures loss in
  the IntentSpec-to-TaskIntent projection through
  `task/intent_compat.py`.
- Unit, integration, E2E, and evaluation tests construct `TaskIntent`.

**Migration target**

`PlanningContext` carrying the original `IntentSpec`, with legacy fields
derived only at a compatibility boundary where a consumer cannot yet accept
IntentSpec.

**Retirement conditions**

- Resolver/resource/human/memory/evaluation consumers accept IntentSpec or a
  defined context contract.
- The planner and all active plan checks no longer rely on a TaskIntent
  projection for actions, resources, conditions, or constraints.
- The consumer-loss evaluation is replaced by a regression check for direct
  IntentSpec propagation.
- No active API response, persistence record, or integration contract requires
  TaskIntent fields.

**Risk**

High. This is a broad consumer migration. Premature retirement can lose
conditions, constraints, resources, or legacy task identifiers.

### Track C: Legacy Agent and community-assistant-agent

**Current references**

- `apps/assistant_api/greenbook_assistant_api/main.py` constructs
  `LegacyAgentService`.
- `apps/assistant_api/greenbook_assistant_api/api/routes.py` retains a lazy
  legacy import path.
- `apps/assistant_api/.../services/assistant_service.py` accepts the legacy
  service boundary.
- `community-assistant-agent/` is referenced by
  `.github/workflows/verify.yml`, `scripts/run_p0_e2e.py`,
  `scripts/runtime-report.ps1`, `scripts/smoke-test.ps1`,
  `scripts/setup-dev.ps1`, and `scripts/verify-all.ps1`.
- The legacy application owns or participates in `assistant_runs`, approval,
  API, migration, and worker behavior.

**Migration target**

`RuntimeAgentService`, Runtime API, `PlanExecution`, ExecutionStateManager,
Execution Event Store, and the run-to-execution compatibility adapter.

**Retirement conditions**

- All supported API routes have a Runtime implementation with equivalent
  authorization, approval, event, artifact, cancel, and error behavior.
- P0 and CI workflows run against the ACTIVE Runtime without the legacy
  application.
- Legacy `assistant_runs` records have a documented read-only/archive policy
  or have been migrated.
- Legacy approval, interrupt, cancel, SSE, and event consumers use the
  execution adapter or the Runtime API.
- Deployment manifests and operational scripts no longer start or inspect the
  legacy service.
- A full unit, integration, contract, E2E, and evaluation run passes after a
  feature-flagged compatibility shutdown test.

**Risk**

Very high. This is an API, persistence, deployment, and operational migration;
it must not be treated as ordinary dead-code removal.

### Track D: run_id and RunRepository

**Current references**

- `RunRepository` remains in `packages/assistant_core/greenbook_assistant_core/db/repositories.py`.
- Legacy API routes and service models expose `run_id`.
- `packages/security/greenbook_security/approval.py` uses `run_id` for
  approval identity.
- `services/greenbook_mcp/greenbook_mcp_server/context.py` and server/tool
  paths carry `agent_run_id`.
- `packages/java_client/greenbook_java_client/client.py` sends
  `X-Agent-Run-Id`.
- `scripts/e2e-test.ps1`, the P0 harness, integration/contract tests, and
  legacy/E2E tests use run identifiers.

**Migration target**

`execution_id` from `PlanExecution`, with
`compatibility/history/run_execution_link.py`, persisted link storage, and
`ExecutionReference` at API boundaries.

**Retirement conditions**

- New executions are created and queried by `execution_id` internally.
- Legacy run IDs resolve through a persisted link for every supported operation.
- API, SSE, approval, cancel, interrupt, MCP, Java client, and test contracts
  have an explicit compatibility or versioned replacement.
- No worker or ExecutionStateManager code depends on `run_id` as its execution
  state key.
- Historical `assistant_runs` data has a retention, read-only, or migration
  decision.
- `RunRepository` has no active write path and only an approved historical
  reader remains, if needed.

**Risk**

Very high. Identifier migration affects authorization, event lookup,
idempotency, external clients, and historical data.

### Track E: Creator Compatibility Workspace

**Current references**

- `services/creator_agent/` has workspace/package metadata references but no
  discovered Python runtime import in the audit.
- `apps/creator-agent/` is the current Creator service implementation.
- `packages/creator_client/` is the active client boundary used by API/MCP
  integration.
- The MCP draft/revise workflow modules have no discovered active caller, but
  require deployment and manual invocation checks.

**Migration target**

Active Creator capability integration through `packages/creator_client/`,
MCP content tools, and the Runtime capability boundary.

**Retirement conditions**

- Root `pyproject.toml`, `uv.lock`, CI, Docker, release, and developer scripts
  no longer reference `services/creator_agent/`.
- No deployment or manual runbook depends on the workspace skeleton.
- `create_draft.py` and `revise_draft.py` have no caller, test dependency,
  package export, or deployment registration.
- Active Creator behavior is covered by the current app/client/tool path.

**Risk**

Medium. The absence of Python imports does not prove that workspace metadata,
deployment, or manual operational workflows are unused.

## 5. Ordered Retirement Plan

### Phase 0: Inventory Freeze

- Record current API, CI, Docker, workspace, test, and deployment references.
- Mark each compatibility boundary with an owner and a migration issue.
- Add no new consumers to legacy modules.

### Phase 1: Establish Observability

- Measure legacy API calls by route and response type.
- Measure `run_id` to `execution_id` link resolution and unresolved IDs.
- Record IntentDraft/IntentElements fallback usage.
- Record Creator workspace package usage separately from active Creator usage.

### Phase 2: Migrate Consumers

- Move understanding consumers to IntentSpec/PlanningContext.
- Move API operations to RuntimeManager and Execution Runtime through the
  existing adapter.
- Migrate approval, event, SSE, MCP, Java client, and P0 paths to execution
  references while preserving old contracts.
- Remove direct dependencies one boundary at a time and keep shims intact.

### Phase 3: Deprecation Window

- Make legacy paths emit deprecation telemetry and documentation warnings.
- Keep read compatibility for historical runs.
- Run full tests and a representative production-like workflow during the
  window.
- Require an explicit owner sign-off for each retirement condition.

### Phase 4: Archive and Removal Review

- Archive only modules with zero production, test, CI, README, and deployment
  references.
- Remove a shim only after external import compatibility is no longer needed.
- Delete only after the archive period and rollback plan are complete.

## 6. Deletion Gate

No compatibility module may be deleted until all items below are true:

- `rg` finds no production import or runtime caller;
- no unit, integration, contract, E2E, evaluation, or compatibility test uses
  the module, unless that test is intentionally retired in the same reviewed
  change;
- no GitHub Actions, Dockerfile, compose file, script, README, runbook, or
  workspace metadata references it;
- the ACTIVE replacement is deployed and tested;
- historical data and external API behavior have a documented policy;
- rollback consists of restoring the archived path or reverting the reviewed
  change;
- the change does not introduce a second execution state source.

## 7. Current Status

| Area | Status | Immediate action |
| --- | --- | --- |
| IntentDraft / IntentElements | COMPATIBILITY | Stop new usage; migrate legacy parser consumers |
| TaskIntent / intent_compat | COMPATIBILITY | Migrate resolver/resource/evaluation consumers |
| LegacyAgentService / agent.py | COMPATIBILITY | Instrument and migrate API fallback |
| community-assistant-agent | COMPATIBILITY / LEGACY | Migrate CI, P0, API, approval, and data paths |
| run_id / RunRepository | COMPATIBILITY | Complete execution reference and historical data strategy |
| services/creator_agent | ARCHIVE CANDIDATE | Audit workspace/deployment references |
| Unwired Creator workflows | ARCHIVE CANDIDATE | Verify callers, exports, tests, and deployment |
| ACTIVE Runtime | ACTIVE | No retirement action |

## 8. Scope Confirmation

This plan does not authorize deletion, movement, or source changes. The
following remain protected throughout the migration:

- IntentSpec and IntentValidator;
- Planner, PlanningContext, and TaskPlan;
- PlanExecution and ExecutionStateManager;
- Worker and ToolRuntime;
- Runtime API and Event/Checkpoint/Persistence contracts.
