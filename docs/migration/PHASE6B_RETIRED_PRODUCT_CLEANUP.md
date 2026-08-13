> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# Phase 6B Retired Product Cleanup

## 1. Goal

Phase 6B removes product code that no longer belongs to GreenBook:

- the Moderation Agent product and its UI/data/configuration surface;
- the retired `community-assistant-agent` runtime and its archived runnable copy;
- the `/api/v1/assistant-tools` Java compatibility surface;
- the old Intent/TaskIntent execution projection, old orchestration wrapper, and duplicate root evaluator.

The reliable execution and security assets were deliberately retained: `ToolPolicyGate`, permission, approval, risk and side-effect policy, `security/`, idempotency, ledger, checkpoint, retry, lease, evidence, artifacts, and human approval.

## 2. Moderation Removal

The repository no longer contains a Moderation Agent service. The following retired business surfaces were removed:

- `moderation-agent/` and its agents, policy/RAG code, routes, prompts, workflow, tests, evals, and migrations;
- moderation startup and secret handling in PowerShell scripts;
- moderation service configuration and database initialization;
- Java moderation controllers/services/DTOs from the old backend owner;
- frontend moderation page, service, types, route, navigation, and moderation-specific state.

The canonical runtime still enforces execution safety. A security policy is not a content-moderation product and remains active.

## 3. Moderation Data Decision

No production data export or retention requirement was present in this development repository. The current local Compose topology creates no Moderation database and no current schema recreates Moderation tables.

The canonical Java backend contains two explicit history migrations:

- `V4__retire_moderation_columns.sql` drops `know_posts.moderation_reason` and `know_posts.moderation_task_id`, and normalizes old `reviewing` rows to `draft`;
- `V5__retire_legacy_assistant_tables.sql` drops `assistant_capabilities` and `assistant_comment_provenance`, which belonged only to `/api/v1/assistant-tools`.

The migration files remain as database history; the active schema, Compose files, Java models, and queries no longer create or consume those fields/tables.

`assistant_runs` was not dropped. It is an active canonical runtime-history projection used by `packages/assistant_core` and the current API. It is not the retired `community-assistant-agent` application.

## 4. Frontend Removal

Removed:

- `zhiguang-fe/src/pages/AdminModerationPage.tsx`;
- `zhiguang-fe/src/pages/AdminModerationPage.module.css`;
- `zhiguang-fe/src/services/moderationService.ts`;
- `zhiguang-fe/src/types/moderation.ts`;
- Admin moderation route, redirect, and navigation.

Publication UI state now uses the actual product states (`draft`, `published`, `rejected`, `deleted`) and the reliable publication flow. Former review wording and moderation fields were removed from create/task pages. Generic image preview naming was left intact; it is not moderation behavior.

## 5. Java Removal

The old `com.tongji.assistant` controller/DTO/mapper/service package and its tests were deleted. The canonical Java surface is `/api/v1/agent/*`.

The old `/api/v1/assistant-tools` security allowance, shared audience configuration, capability service, provenance mapper, moderation fields, and old Assistant DTOs were removed. Agent-generated comment responses retain the normal comment contract; they no longer read the retired provenance table.

`apps/backend` remains the only Java source tree. The Phase6A duplicate `zhiguang-be` was already removed and remains absent.

## 6. DB Removal

Active Compose and initialization files no longer reference:

- `content_moderation`;
- Moderation init scripts or networks;
- `assistant_capabilities` creation;
- `assistant_comment_provenance` creation;
- retired `know_posts` moderation columns.

The exact active data owners are now:

| Data | Owner | Decision |
| --- | --- | --- |
| Community posts/comments/users | `apps/backend` MySQL | KEEP |
| Task/goal/execution/checkpoint/ledger | Agent Runtime PostgreSQL/runtime stores | KEEP |
| Conversation and memory | Agent Runtime repositories | KEEP |
| Creator state | `creator-agent` PostgreSQL | KEEP |
| `assistant_runs` history projection | Agent Runtime PostgreSQL | KEEP |
| Moderation tables/columns | none in active schema | DROP via migration |
| Old Assistant-only goal/intent/run-step/memory tables | no active schema/caller | RETIRED; no new creation |

## 7. Old Community Assistant Retirement

Deleted after the Phase6A caller scan:

- `community-assistant-agent/`;
- `archive/legacy/community-assistant-agent/`, including its vendored `.venv` and runnable source copy.

The old runtime was absent from startup, CI, smoke tests, E2E, and runtime reports before deletion. No canonical Python package imports it. The new runtime owner remains `apps/assistant_api`, `apps/assistant_worker`, and `packages/assistant_core`.

## 8. Old Assistant Data Decision

The repository has no declared production-history migration requirement for the deleted standalone Assistant. Its old application migrations and data models were removed rather than preserved as a second runtime.

The decision is intentionally selective:

- retain `assistant_runs` because the current runtime uses it as a history projection;
- retain current execution/task/artifact/memory repositories because they are canonical sources of truth;
- remove old application-only conversation-goal, target-binding, intent-delta, run-step, scheduled-action, side-effect, receipt, and old memory surfaces because no active caller or current schema depends on them.

If a real deployment later supplies historical rows outside this development workspace, that is a data migration project, not a reason to restore the deleted runtime.

## 9. Assistant-tools API Retirement

Repository-wide caller scanning found no frontend, Agent Runtime, MCP, test, script, OpenAPI, or CI caller for `/api/v1/assistant-tools`.

The endpoint package, DTOs, services, capability table, provenance table, security allowance, and associated tests were removed. `/api/v1/agent/*` is the only canonical Java Agent tool surface.

## 10. Intent/TaskIntent Cleanup

The canonical semantic chain is now:

```text
Command -> GoalTree -> Task -> TaskPlan -> ExecutionInput
```

Removed from active code:

- `intent_models.py`;
- `intent_preprocessor.py`;
- `intent_validator.py`;
- `intent_llm_trace.py`;
- `intent_validation_trace.py`;
- old Intent/TaskIntent-only tests and datasets;
- old `TaskIntent` and `IntentSpec` execution projections.

An active-source scan found no `TaskIntent`, `IntentSpec`, `intent_compat`, or `TaskOrchestrator` caller outside migration/history text. `ExecutionInput` is the only intelligence-to-execution payload.

## 11. RuntimeAgentService Cleanup

`RuntimeAgentService.execute()` legacy resolved-context behavior was removed. The service now exposes the typed execution boundary:

- `submit_plan(TaskPlan)`;
- `execute_queued(ExecutionQueueMessage)`;
- queue/native worker execution and result projection.

`ArgumentBinder` accepts `ExecutionInput`/typed plan data only. It no longer infers arguments from Intent or user text. `CapabilityExecutor` requires an explicit selected tool and does not select a first tool or infer a hidden capability mapping.

The duplicate approval wrapper `approval_service.py` and its old execution-reference tests were deleted. `ApprovalRuntimeService` now distinguishes the canonical direct-resume callback from queue resume; both paths operate on the existing execution checkpoint.

## 12. Orchestration Cleanup

Deleted `packages/assistant_core/greenbook_assistant_core/orchestration/`, including the old `TaskOrchestrator`, template selector, and orchestration context/models.

Canonical plan contracts now live in:

- `packages/assistant_core/greenbook_assistant_core/planning/contracts.py`;
- `packages/assistant_core/greenbook_assistant_core/goal/compiler.py`.

`GoalCompiler` maps typed Goal capability requirements into `TaskPlan`/`PlanStep`; it does not parse user text or choose tools. No business workflow template router remains in the default path.

## 13. Evaluation Cleanup

The canonical evaluator is `packages/evaluation/greenbook_evaluation` with `EvaluationRunner`, behavioral metrics, badcase taxonomy, and community golden cases.

Removed:

- root `evaluation/` package and Phase15-F JSONL datasets;
- `scripts/run-agent-evaluation.py` and `scripts/runtime-report.ps1`, which depended on the retired evaluator and multi-agent labels;
- `tests/evaluation/test_phase15f_evaluation.py`;
- `tests/e2e/test_phase15f_final_multi_agent_e2e.py`;
- old Intent/Planner evaluator modules and tests.

The current evaluator checks Command, target, Goal, Tool, Task, side-effect, recovery, Memory, Context, latency, and tool-call behavior. It does not score hidden reasoning or preserve fake business-Agent identities.

## 14. Deleted Files

The Phase6B deletion set is grouped below; directory entries include their complete contents.

| Path | Old responsibility | Replacement/reason |
| --- | --- | --- |
| `scripts/start-moderation.ps1` | Start retired Moderation service | Deleted; no Moderation product |
| `apps/backend/src/main/java/com/tongji/assistant/` | `/api/v1/assistant-tools` Java compatibility API | `/api/v1/agent/*` |
| `apps/backend/src/test/java/com/tongji/assistant/` | Old Assistant API tests | Canonical Agent/contract tests |
| `apps/backend/db/assistant_capability_migration.sql` | Old capability table setup | `ToolMetadata`/`ToolPolicyGate` |
| `apps/backend/db/assistant_comment_migration.sql` | Old provenance table setup | Canonical comment contract |
| `apps/backend/db/fix-reviewing-posts.sql` | Moderation state repair helper | `V4__retire_moderation_columns.sql` |
| `zhiguang-fe/src/pages/AdminModerationPage.tsx` | Moderation UI | No product replacement |
| `zhiguang-fe/src/pages/AdminModerationPage.module.css` | Moderation UI styles | No product replacement |
| `zhiguang-fe/src/services/moderationService.ts` | Moderation API client | No product replacement |
| `zhiguang-fe/src/types/moderation.ts` | Moderation DTOs | Current task/publication types |
| `packages/.../task/intent_*.py` | Intent compatibility contracts/traces | Command + Goal + ExecutionInput |
| `apps/assistant_api/.../services/approval_service.py` | Duplicate approval wrapper | `ApprovalRuntimeService` |
| `evaluation/` | Phase15-F root evaluator/datasets | `packages/evaluation/greenbook_evaluation` |
| `scripts/run-agent-evaluation.py` | Phase15-F live evaluator | Canonical EvaluationRunner |
| `scripts/runtime-report.ps1` | Wrapper for retired evaluator | Current tests and runtime status checks |
| `tests/evaluation/test_phase15f_evaluation.py` | Root evaluator tests | Canonical evaluation tests |
| `tests/e2e/test_phase15f_final_multi_agent_e2e.py` | Old multi-Agent E2E | Canonical Agent E2E/runtime tests |
| `README.md`, `PROJECT_CONTEXT.md`, `docs/INTEGRATION.md` old content | Stale product/runtime descriptions | Rewritten current documents |
| `docs/COMMUNITY_ASSISTANT.md` and stale current architecture documents | Retired current-architecture answers | `docs/architecture/CURRENT_ARCHITECTURE.md` |
| `docs/greenbook-agent-runtime-technical-introduction.md` | Stale Intent/Orchestration runtime introduction | Current architecture and migration documents |

## 15. Deleted Directories

| Directory | Decision |
| --- | --- |
| `moderation-agent/` | DELETE; no canonical caller |
| `community-assistant-agent/` | DELETE; retired runtime owner |
| `archive/legacy/community-assistant-agent/` | DELETE; runnable backup and vendored environment had no product role |
| `packages/assistant_core/greenbook_assistant_core/orchestration/` | DELETE; merged into GoalCompiler/planning contracts |
| root `evaluation/` | DELETE; duplicate evaluator |
| `infra/postgres/` | DELETE; became empty after Moderation init removal |

The Phase6A duplicate owners `zhiguang-be`, `apps/creator-agent`, and `services/creator_agent` were already removed before Phase6B and were rechecked as absent.

## 16. Dependency Cleanup

The root Python workspace contains only active Agent API/Worker/core, Creator client, MCP, security, observability, contracts, and evaluation packages. No Moderation-only package or root evaluator dependency remains.

`uv lock --check` passes. Creator-owned LangGraph/Qdrant dependencies were not removed because they belong to the active `creator-agent` service.

## 17. Config Cleanup

Removed Moderation environment variables, secret rotation, startup switches, health checks, Compose mounts, and database-init references. `start-greenbook.ps1` remains the owner of the Java Backend, Creator, Agent API, Agent Worker, and frontend topology.

The `ASSISTANT_API_PORT=8094` value now belongs to the canonical Agent API; it is not an old community-assistant process. MCP remains an in-process runtime package, not a new standalone service.

## 18. Test Results

| Check | Result |
| --- | --- |
| `uv run pytest -q` | **551 passed, 1 skipped** |
| `uv run pytest --collect-only -q` | **552 collected, exit 0** |
| `uv run pytest -q tests/e2e` | **15 passed** |
| `mvn -q test` in `apps/backend` | **PASS** |
| `npm run lint` in `zhiguang-fe` | **PASS** |
| `npm run build` in `zhiguang-fe` | **PASS** |
| `uv lock --check` | **PASS** |
| `python -m compileall` on active Python packages/tests | **PASS** |
| targeted Ruff on changed runtime/evaluation modules | **PASS** |
| `docker compose config` | **PASS; no Moderation service/database** |
| `docker compose -f infra/docker-compose.dev.yml config` | **PASS** |
| `git diff --check` | **PASS** |

The full live-service E2E was not claimed: it requires running infrastructure and dedicated credentials. The repository E2E suite and canonical unit/contract tests pass.

## 19. Final Product Surface

```text
Frontend:       zhiguang-fe
Java Backend:   apps/backend
Agent API:      apps/assistant_api
Agent Worker:   apps/assistant_worker
Agent Core:     packages/assistant_core
Creator:        creator-agent
MCP runtime:    services/greenbook_mcp (in-process)
Contracts:      packages/contracts and Java OpenAPI contracts
Infrastructure: root/infra Compose, MySQL, PostgreSQL, Redis, Kafka/Redpanda, Qdrant
```

Canonical request path:

```text
User
 -> Command + Context
 -> GoalTree
 -> TaskManager
 -> AgentLoop
 -> DynamicPlanner / ToolSelector
 -> ToolPolicyGate
 -> ExecutionInput
 -> Queue / Worker / ToolRuntime
 -> MCP boundary
 -> Java Backend or Creator Service
```

There is no Moderation product, no `moderation-agent`, no old `community-assistant-agent`, no second Java/Creator owner, no old Intent Router, and no default business-template planner.

## 20. Remaining Technical Debt

1. Historical audit and migration documents still contain old names by design. They are not imported, packaged, started, or tested as runtime code. `docs/architecture/CURRENT_ARCHITECTURE.md` is the active architecture authority.
2. `compatibility/history/RunExecutionAdapter` remains because current API/history routes and execution-reference tests still project public run identifiers to canonical executions. This is execution/history compatibility, not business intelligence compatibility; it can be removed only with an API history contract decision.
3. `assistant_runs` remains a canonical history projection and should not be confused with the deleted standalone Assistant database.
4. Package names `assistant_core`, `assistant_api`, and `assistant_worker` were intentionally not renamed in Phase6B to avoid a package-level breaking change.
5. Full-repository Ruff still contains unrelated pre-existing findings in untouched execution infrastructure; all modules changed for this phase pass targeted Ruff.

## 21. Phase6C Input

Phase6B is complete. Phase6C may evaluate package/product naming (`Assistant` versus `GreenBook Agent`) and further historical-document archival, but it must start from the single-owner topology established here. No Phase6C work was started in this change.
