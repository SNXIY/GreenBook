# Phase 6C Architecture Consolidation

> This is the Phase 6C delivery report. The current architecture authority is
> [`docs/architecture/CURRENT_ARCHITECTURE.md`](../architecture/CURRENT_ARCHITECTURE.md).
> Earlier phase reports remain historical records and do not define the active
> product topology.

## 1. Goal

Phase 6C consolidated the final GreenBook names, directory ownership, shared
contracts, configuration, scripts, documentation, and dependency boundaries.
No new business capability was added, and the reliable execution path
(`Queue`, `Worker`, `Checkpoint`, `Ledger`, `Recovery`, `Memory`, and
`Evaluation`) was preserved.

## 2. Naming Before

- The Agent Runtime was physically named `assistant_core`, `assistant_api`, and
  `assistant_worker`, even though the active product is GreenBook Agent.
- The frontend still exposed `AssistantPanel`, `assistantService`, and
  assistant-oriented type names.
- `conversation.ContextManager` and `context.ContextManager` overlapped.
- Planning contracts and graph names were split between `task`, `goal`, and
  `planning`, including `ConversationTaskGraph` and `TaskPlanRevision`.
- Capability definitions carried execution policy that also appeared in tool
  runtime and MCP code.
- Tool schema, policy, and metadata were projected independently in contracts,
  Agent tool runtime, and MCP.
- Creator's physical directory was `creator-agent`, while product-facing
  names varied between Creator Agent and Creator.
- Old `ASSISTANT_*`, bare Creator/Java aliases, assistant proxy paths, and old
  launcher names remained in local setup surfaces.

## 3. Naming After

| Area | Canonical result |
| --- | --- |
| Agent core | `packages/agent_core`, module `greenbook_agent_core` |
| Agent API | `apps/agent_api`, module `greenbook_agent_api` |
| Agent Worker | `apps/agent_worker`, module `greenbook_agent_worker` |
| Agent product | GreenBook Agent / GreenBook Agent Runtime |
| Creator product | GreenBook Creator Service; physical `creator-agent` retained |
| Frontend panel/client | `AgentPanel`, `agentService`, `types/agent.ts` |
| Conversation boundary | `ConversationService` and `ConversationRepository` |
| Context boundary | `ContextBuilder`, `ContextSnapshot`, projection-only context |
| Planning graph | `PlanGraph` and `PlanNode` in `planning/graph.py` |
| Runtime identity | `execution_id` |
| Public history | `run_id` and `assistant_runs` retained as history projection only |
| Agent HTTP API | `/api/v1/agent/*` |
| Ports | Java `8080`, Creator `8092`, Agent API `8094`, frontend `5173` |

There is no compatibility copy of the old Agent package names. All imports,
metadata, workspace declarations, tests, scripts, CI references, and docs were
migrated together.

## 4. Final Monorepo Tree

The following is the active source tree. Generated environments, build output,
pytest scratch directories, and `.runtime/` data are omitted.

```text
green-book/
├── apps/
│   ├── backend/
│   ├── agent_api/
│   └── agent_worker/
├── packages/
│   ├── agent_core/
│   ├── contracts/
│   ├── creator_client/
│   ├── evaluation/
│   ├── java_client/
│   ├── observability/
│   └── security/
├── services/
│   └── greenbook_mcp/
├── creator-agent/
├── zhiguang-fe/
├── contracts/
│   ├── agent-openapi.yaml
│   └── java-openapi.yaml
├── infra/
├── scripts/
├── tests/
├── docs/
│   ├── architecture/
│   ├── development/
│   ├── migration/
│   └── progress/
├── design-system/
├── archive/                 # historical, not an active import/deployment surface
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
├── README.md
└── PROJECT_CONTEXT.md
```

## 5. Service Naming

- `apps/backend` is the single Java Backend and owns the Java Agent Tool API.
- `apps/agent_api` is the Agent HTTP composition root.
- `apps/agent_worker` is the durable execution queue consumer.
- `packages/agent_core` is the Agent Runtime core, not a second product.
- `creator-agent` is documented and displayed as GreenBook Creator Service.
  Its internal `CreatorSupervisorAgent` and specialist boundaries remain
  because they represent real Creator reasoning boundaries.
- `services/greenbook_mcp` is an MCP-compatible in-process tool runtime, not a
  separately deployed MCP server.

## 6. Package Naming Decision

The rename was completed in one migration because the repository-wide scan
showed that the old package paths could be changed atomically. The old physical
directories, Python modules, package metadata, imports, workspace members,
scripts, CI, tests, and documentation were removed or updated together.

Creator's directory was not renamed to `creator-service`: the directory name is
an internal repository path and the rename would create broad, low-value
breaking changes across its local tooling. The service name is consistently
GreenBook Creator Service in product UI, docs, descriptions, logs, and config.

## 7. Context / Conversation Cleanup

- `conversation/` owns facts and persistence for messages, summaries, and
  preferences through `ConversationService` and repositories.
- `context/` owns the bounded decision working set through `ContextBuilder`,
  `ContextSnapshot`, and projections.
- The duplicate `context.ContextManager` wrapper was removed.
- The no-op `ConversationMemory` wrapper was removed; durable memory remains
  owned by the memory repository/retriever boundary.
- The intended invariant is now explicit: Conversation is facts; Context is a
  working projection.

## 8. Planning Contract Cleanup

`planning/contracts.py` is the sole owner of `TaskPlan`, `PlanStep`,
`PlanRevision`, and `PlanningDecision`. `task/` owns Task lifecycle and
`goal/` owns the Goal tree. The graph intermediate model moved to
`planning/graph.py` and is now `PlanGraph`/`PlanNode`.

`ConversationTaskGraph` and `TaskPlanRevision` are no longer active names.
`execution/` consumes `ExecutionInput`; it does not own or recreate planning
contracts.

## 9. Capability Cleanup

Capability is now a semantic catalog. It contains capability identity,
description/category, tags, candidate tools, semantic input/output information,
and LLM/parallelization hints where needed for selection.

Risk, approval, side-effect, retry, timeout, cost, and permission policy were
removed from the Capability model and registry. Plan validation resolves the
concrete tool policy from the shared contract catalog, with fail-closed behavior
for unknown tools.

## 10. ToolMetadata Consolidation

`packages/contracts/greenbook_contracts/tool_contract.py` is the canonical source
for:

- `ToolContract` — schema, handler-facing contract, and policy projection;
- `ToolMetadata` — descriptive discovery metadata;
- `ToolPolicyMetadata` — risk, approval, permission, side-effect, retry, cost,
  and timeout policy;
- `TOOL_POLICY_CATALOG` — the concrete policy catalog for registered tools.

Agent selection, planning validation, security policy, tool runtime, and MCP
consume these contracts. MCP `ToolDefinition`-style duplicate policy metadata
was replaced with a contract projection. There is no second risk/approval/
retry/timeout source in the Agent Runtime or MCP registry.

## 11. MCP Contract

The MCP package is an in-process registry and handler runtime. `GreenBookMCPServer`
was retained because it accurately names the dispatch boundary and changing it
would not improve ownership. Its docs and package metadata now state the
MCP-compatible in-process role explicitly.

Tool names use the canonical `domain.operation` convention, including:
`community.search_public_posts`, `content.create_draft`,
`content.revise_draft`, `publication.schedule`,
`publication.update_schedule`, `publication.cancel_schedule`,
`publication.publish_now`, `interaction.send_reply`, and
`analytics.get_post_performance`.

## 12. Java API Naming

Java active API classes and packages use `AgentFacadeController`,
`AgentFacadeService`, and Agent Tool API terminology. OpenAPI and client paths
are under `/api/v1/agent/*`; the old assistant Controller/DTO surface is not
active.

Database/protocol fields such as `AI_ASSISTED` and historical `assistant_runs`
were not renamed as a cosmetic operation because they are persisted or
compatibility contracts. Their scope is documented rather than expanded.

## 13. Frontend Naming

The frontend now uses `AgentPanel`, `agentService`, Agent types, and the
`/agent-api` proxy. User-visible language is GreenBook Agent. Creator UI copy
uses Creator Service or AI 创作. Internal protocol values such as an `assistant`
message role remain only where they are part of an existing wire/history
contract; they are not product labels.

Frontend API ownership is separated into Agent API, Java API, Creator API, and
execution clients. No assistant compatibility alias was left in source imports.

## 14. ENV Cleanup

The root `.env.example` is the complete canonical local template. Names are
owned by the service that consumes them:

- `GREENBOOK_AGENT_*` for Agent API, Worker, Runtime, execution, memory, and
  model configuration;
- `GREENBOOK_JAVA_*` for Java URLs, identity, and client settings;
- `GREENBOOK_CREATOR_*` for Creator Service and Creator handoffs;
- `VITE_GREENBOOK_AGENT_*` and `VITE_GREENBOOK_CREATOR_*` for frontend routing.

The service-local Creator example remains a self-contained Creator deployment
template; each template has no duplicate keys. Old `ASSISTANT_*`, bare
`CREATOR_*`, and duplicate Java URL aliases were migrated out of active callers
and are unsupported.

## 15. Script Cleanup

The canonical launch set is:

`start-greenbook.ps1`, `start-be.ps1`, `start-creator.ps1`, `start-agent.ps1`,
`start-agent-worker.ps1`, `start-fe.ps1`, `setup-dev.ps1`, `verify-all.ps1`,
`smoke-test.ps1`, and `check-runtime-status.ps1`.

P0 and runtime health scripts use Agent naming and canonical environment keys.
`start-assistant.ps1` and `start-assistant-worker.ps1` were removed rather than
kept as aliases.

## 16. Docker / Infra Cleanup

The root `docker-compose.yml` is the shared local infrastructure authority for
Postgres, Redis, Qdrant, Kafka, and MySQL. The duplicate
`infra/docker-compose.dev.yml` was removed because it defined overlapping
services with drift.

The Creator repository's own compose file remains because it is a separate
Creator Service deployment profile, not a second definition of the root
GreenBook topology. `infra/README.md` documents this distinction.

## 17. Documentation Cleanup

`README.md` now covers only the current product, architecture, tree, stack,
startup, capabilities, and testing.

Current architecture authority is split only by topic across:

- `docs/architecture/CURRENT_ARCHITECTURE.md`
- `AGENT_RUNTIME.md`, `EXECUTION_RUNTIME.md`, `SERVICE_COMMUNICATION.md`, and
  `DATA_MODEL.md`
- `NAMING_CONVENTIONS.md`
- `docs/development/LOCAL_SETUP.md`, `CONFIGURATION.md`, and `TESTING.md`

Other architecture and migration reports were retained for traceability and
marked as historical. They do not compete with the current authority.

## 18. Dead Code Removed

The consolidation removed or merged the old Agent package copies, old frontend
assistant-named modules, duplicate ContextManager wrapper, no-op conversation
memory wrapper, old graph and planning aliases, duplicate Capability policy
fields, duplicate MCP policy definitions, old assistant launchers, obsolete
configuration aliases, and the duplicate development compose topology.

The architecture boundary test also verifies that the removed physical package
paths are not recreated and that core modules do not cross into API or external
implementation boundaries.

## 19. Dependency Rules

`tests/unit/test_architecture_boundaries.py` provides a lightweight AST-based
architecture test. It enforces, at minimum:

- execution does not import API routes, command interpretation, goal
  decomposition, or AgentLoop;
- tool runtime does not import API routes;
- goal code does not import the ExecutionWorker;
- Creator and Java client packages do not import Agent intelligence internals;
- old Agent package paths and duplicate graph/context modules are absent.

API and Worker remain composition roots. Creator specialists remain behind the
Creator Service/API boundary.

## 20. Ruff / Code Quality

- Agent Runtime, contracts, security, external clients, and MCP active Python
  source: clean under the configured Ruff checks (`E`, `F`, `I`, and selected
  quality rules).
- Creator active source: `F` and `I` clean; safe Ruff formatting/import fixes
  were applied without changing Creator business behavior.
- Full Creator Ruff still reports 58 modernization/simplification findings
  (primarily `UP042`, `SIM*`, `B905`, and `UP046`). These are retained as
  bounded Creator technical debt rather than triggering a Creator rewrite.
- `compileall` passed for active Python packages and services.
- `git diff --check` passed; Git emitted only normal LF/CRLF normalization
  warnings on Windows.

## 21. Test Results

| Check | Result |
| --- | --- |
| `uv run pytest -q` | `552 passed, 1 skipped` |
| `uv run pytest --collect-only -q` | `553 tests collected` |
| `uv run pytest -q tests/e2e` | `15 passed` |
| Creator `uv run pytest -q` | `64 passed` |
| `uv run pytest -q scripts/test_run_p0_e2e.py` | `9 passed` |
| Java `mvn test` | `37 tests, 0 failures/errors, 2 skipped` |
| Frontend `npm run lint` | passed |
| Frontend `npm run build` | passed |
| Frontend `npm run test:execution` | passed |
| root and Creator `uv lock --check` | passed |
| active Python `compileall` | passed |
| active Agent Runtime Ruff | passed |
| Creator Ruff `F,I` | passed |
| `docker compose config --quiet` | passed |
| `git diff --check` | passed |
| P0 harness `--help` and unit harness | passed; live service run not started |

The repository E2E tests cover search, draft creation/revision, scheduling,
approval, reply, analytics, preemption/resume, recovery, memory, and
evaluation flows. A live P0 run was not claimed because this environment did
not have the Java, Creator, Agent, Worker, database, model credentials, and
queue services running together.

Pytest emitted a Windows permission warning while trying to write its cache;
this did not affect test execution.

## 22. Final Architecture

```text
Conversation facts
  -> Context projection
  -> Command
  -> GoalTree
  -> Task lifecycle / preemption / resume
  -> AgentLoop + DynamicPlanner + ToolSelector
  -> ToolPolicyGate
  -> planning.TaskPlan
  -> ExecutionInput
  -> Queue / Agent Worker
  -> ToolRuntime / MCP-compatible in-process handlers
  -> Java Backend or Creator Service
  -> Checkpoint / Ledger / Artifact / Recovery / Memory / Evaluation
```

The architecture has one Agent Runtime, one Java Backend, one Creator Service,
one shared contract package, and one ToolMetadata policy source. Conversation
facts, Context projections, Plans, and Executions have distinct owners.

## 23. Remaining Technical Debt

- `assistant_runs`, `run_id`, and `RunExecutionAdapter` remain as an explicit
  public history compatibility boundary. `execution_id` is the runtime source
  of truth; a future database migration may retire the history names only after
  all external readers are migrated.
- The Creator directory remains `creator-agent`; product-facing naming is
  already unified.
- Full Creator Ruff modernization has the 58 findings listed in section 20.
- Frontend build tooling reports stale Browserslist/Baseline data; it does not
  affect the successful build.
- Historical documents and database migrations necessarily mention retired
  products and old identifiers. They are marked historical and are not active
  callers.
- A live multi-service P0 E2E run still requires a configured local deployment
  and credentials.

## 24. Phase6D Input

Phase 6D was not started. If a later phase is authorized, its useful inputs are
the explicit retirement plan for `assistant_runs`/`run_id`, a live deployment
E2E environment, and a separately scoped Creator Ruff modernization pass. No
new product, Skill, A2A, supervisor, RAG platform, or business module is
proposed by this Phase 6C report.
