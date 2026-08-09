# GreenBook Creator Runtime Boundary

Phase 7.9-A audit. This document records the Creator implementation boundary
after inspecting source imports, API wiring, workspace configuration, CI, and
runtime capability calls. It does not move or delete code.

## 1. ACTIVE Creator

### 1.1 Unique active service implementation

`creator-agent/` is the only repository directory containing a complete,
runnable Creator service implementation.

Evidence:

- `creator-agent/app/main.py` creates the FastAPI application and mounts the
  Creator API router.
- `creator-agent/app/creator/api/routes.py` exposes the `/api/v1/creator`
  contract, including task, artifact, event, and publication-handoff APIs.
- `creator-agent/app/creator/runtime/` contains the graph/runtime composition,
  and `creator-agent/app/creator/worker/` contains the worker composition.
- `creator-agent/Dockerfile` defines the service image and its readiness
  health check at `/actuator/health/ready`.
- `.github/workflows/verify.yml` runs the service's own dependency, lint, and
  test workflow from `creator-agent/`.
- `scripts/run_p0_e2e.py` starts this directory as the Creator process and
  polls its API on the configured Creator port.

This is the ACTIVE Creator server implementation. It owns content research,
generation, revision, artifact production, Creator task state, and its own
long-running Creator execution details. It does not own GreenBook
`PlanExecution`, Java community persistence, or publication business truth.

### 1.2 Active client boundary

`packages/creator_client/greenbook_creator_client/client.py` is the ACTIVE
Assistant-side client. `apps/assistant_api/greenbook_assistant_api/main.py`
constructs `CreatorClient` from `ASSISTANT_CREATOR_BASE_URL` (default
`http://127.0.0.1:8092`) and injects it into
`GreenBookMCPServer`.

The client owns transport concerns only:

- submit a Creator task through `POST /api/v1/creator/tasks`;
- wait for task completion;
- fetch the final artifact;
- create a publication handoff when that contract is explicitly needed;
- classify timeout, unavailable, validation, and downstream errors.

### 1.3 Runtime integration

The formal boundary is:

```text
User
  -> IntentSpec
  -> Planner / PlanningContext
  -> TaskPlan
  -> PlanExecution / Worker
  -> Capability / ToolRuntime
  -> GreenBook MCP content tools
  -> CreatorClient
  -> creator-agent HTTP API
```

The Assistant API injects `CreatorClient` into the MCP server in
`apps/assistant_api/greenbook_assistant_api/main.py`. The active content
tools in `services/greenbook_mcp/greenbook_mcp_server/tools/content.py` call
`create_task()`, `wait_for_completion()`, and `get_artifact()`. Java remains
the source of truth for community drafts; the content tool writes and
verifies the resulting draft through the Java client.

Neither IntentSpec nor Planner imports Creator implementation details. A
capability identifies the business operation; ToolRuntime/MCP performs the
external call.

### 1.4 Deployment status

The source, CI, Dockerfile, API contract, and P0 harness all point to
`creator-agent/` as the executable Creator service. The root
`docker-compose.yml` provisions shared PostgreSQL and Redis infrastructure;
the process launch and service image are defined separately by the Creator
project and P0 harness.

This audit confirms the repository's active implementation and integration
contract. It does not prove which service instance is currently deployed in
every environment; deployment inventory remains an operational check.

## 2. COMPATIBILITY Creator

### 2.1 `services/creator_agent/`

This directory is a workspace member in the root `pyproject.toml`, but its
package currently contains only `__init__.py` files under `api`, `domain`,
`graph`, `persistence`, and `worker`. No runnable API, graph, worker,
Dockerfile, or direct production import was found.

Classification: `COMPATIBILITY / DEPLOYMENT REVIEW`, not ACTIVE.

It is retained because the workspace manifest and lockfile declare the
package, and a future packaging or deployment workflow may still refer to
it. It can be archived only after all of the following are confirmed:

1. no deployment manifest or external build references the package;
2. the workspace dependency is removed in a separate approved change;
3. no import or test requires its package name;
4. `creator-agent/` is confirmed as the sole deployment owner;
5. the Creator API, persistence, and migration contracts have been compared.

### 2.2 Legacy Creator paths

`community-assistant-agent/` contains an older Assistant runtime and Creator-
related task/tool behavior. It is not imported by the ACTIVE
`assistant_core` Runtime, but it remains relevant through its own API,
`assistant_runs` persistence, CI job, migrations, and compatibility routes.

Classification: `COMPATIBILITY` with a `LEGACY` implementation boundary.

It must not be deleted until the legacy API, run data, approval behavior, CI,
and deployment dependencies have been retired or migrated.

### 2.3 Legacy service adapter status

`apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`,
`assistant_service.py`, and `runtime_router.py` preserve selection and
fallback behavior between legacy and Runtime modes. They are not Creator
implementations, but they can still route requests into historical behavior.

Classification: `COMPATIBILITY`. Their removal requires runtime mode to be
fully migrated, fallback disabled, and legacy API/data compatibility closed.

## 3. LEGACY Creator

### 3.1 Candidate files and directories

The following are legacy or archive candidates, not deletion approvals:

| Path | Current evidence | Classification |
| --- | --- | --- |
| `community-assistant-agent/` Creator/tool portions | Historical service with its own run/task pipeline; retained by CI and API/data contracts | LEGACY / ARCHIVE CANDIDATE |
| `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py` | Instantiated by Assistant compatibility wiring | LEGACY / COMPATIBILITY |
| `services/greenbook_mcp/greenbook_mcp_server/workflows/create_draft.py` | Defines `create_draft_via_creator`, but no production caller was found | ARCHIVE CANDIDATE |
| `services/greenbook_mcp/greenbook_mcp_server/workflows/revise_draft.py` | Defines `revise_draft_via_creator`, but no production caller was found | ARCHIVE CANDIDATE |
| `services/creator_agent/greenbook_creator_agent/` | Empty workspace package skeleton; no runnable implementation found | ARCHIVE CANDIDATE |

The two unused workflow modules also expose a different `submit_task()`
client shape than the active `CreatorClient`, whose active tools use
`create_task()`. This is evidence that they are historical/unwired code, not
an alternative ACTIVE integration. They should be removed or archived only
after their tests and any external callers are explicitly ruled out.

### 3.2 Deletion conditions

No Creator directory is approved for immediate deletion by this audit.
Deletion or archival requires:

- repository-wide import and script reference confirmation;
- CI, Docker, compose, and deployment inventory confirmation;
- API contract and authentication comparison;
- database and migration ownership confirmation;
- integration and E2E tests pointed to the surviving service;
- a rollback/data-retention plan for Creator tasks and artifacts.

## 4. Migration Plan

### Keep as ACTIVE

- `creator-agent/`: unique complete Creator service implementation;
- `packages/creator_client/`: Assistant-to-Creator HTTP contract;
- `services/greenbook_mcp/greenbook_mcp_server/tools/content.py`: active
  capability-side Creator integration;
- `apps/assistant_api/greenbook_assistant_api/main.py`: client wiring;
- Creator contract, integration, and E2E tests that exercise the active API.

### Migrate or isolate

- Keep `Run`, `PlanExecution`, and Creator task identifiers distinct. The
  GreenBook Runtime owns `execution_id`; Creator owns its task/run identifiers.
- Keep the existing `RunExecutionAdapter` at the legacy API boundary; do not
  leak Creator-internal run state into PlanExecution.
- Resolve the workspace status of `services/creator_agent/` before changing
  the root workspace manifest.
- Keep legacy Assistant/Creator routes behind compatibility boundaries until
  their API and persistence consumers are retired.

### Archive after verification

- `services/creator_agent/` if it is confirmed to be only a package skeleton;
- the two unwired `greenbook_mcp_server/workflows/*_via_creator.py` modules;
- historical Creator portions of `community-assistant-agent/` after API and
  data migration.

### Delete only after explicit approval

Delete candidates require a separate change with evidence in the cleanup
report. This phase performs no deletion and does not alter any runtime path.

## 5. Boundary Rules

1. IntentSpec expresses user intent; it never contains Creator graph or API
   implementation details.
2. Planner maps content operations to capabilities and a TaskPlan; it never
   calls Creator directly.
3. Worker and Execution Runtime own PlanExecution, step state, pause/resume,
   retry, checkpoint, and events for the Assistant execution.
4. ToolRuntime/MCP owns validation, authorization, timeout, idempotency, and
   external invocation.
5. Creator owns content-generation workflow state and artifacts.
6. Java owns community draft and publication business state.
7. The only supported Assistant-to-Creator production boundary is the typed
   `CreatorClient` HTTP contract.

## 6. Audit Limitations

This is a source and repository audit. It cannot establish live production
traffic, external deployment manifests outside this repository, or whether a
manually started service is still used. Those checks are prerequisites for
archiving the compatibility and legacy candidates above.
