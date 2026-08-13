> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# GreenBook Final Cleanup Candidates

Phase 7.9-B read-only verification before any deletion. The scan excluded
generated environments and caches (`.venv`, `.mypy_cache`, `.pytest_cache`,
`__pycache__`, `node_modules`). Reference counts below mean matching source
files/locations found by `rg`; historical documentation is listed separately
and is not treated as a production dependency.

## 1. Executive Result

No requested candidate is safe for immediate deletion.

The Planner, Execution Runtime, and Capability packages have no direct source
references to `IntentDraft`, `IntentElements`, `IntentCompiler`,
`IntentSpecBuilder`, `LegacyAgentService`, `CommunityOperationsAssistant`, or
`services/creator_agent`. However, the active `task/understanding.py` module
still imports the compatibility intent adapter and retains legacy parser
methods. This is a static compatibility dependency and must be migrated before
the intent compatibility implementation can be deleted.

The legacy Agent remains wired through API compatibility services and tests.
The Creator service under `creator-agent/` is retained as the active external
service; `services/creator_agent/` is only a workspace package skeleton.

## 2. Intent Legacy Candidates

### 2.1 Summary

| Candidate | Source reference count | Production dependency | Risk | Action |
| --- | ---: | --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/task/intent_draft.py` | 2 source locations in the shim; 2 compatibility tests | Yes, migration import boundary | High | KEEP / MIGRATE |
| `packages/assistant_core/greenbook_assistant_core/task/intent_elements.py` | 5 source locations in the shim; 2 compatibility tests | Yes, migration import boundary | High | KEEP / MIGRATE |
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_draft.py` | 7 adapter/model locations; 1 compatibility test module | Yes, through adapter and retained parser methods | High | KEEP |
| `packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_elements.py` | 8 adapter/model locations; 1 compatibility test module | Yes, through adapter and retained parser methods | High | KEEP |
| `IntentCompiler` implementation | No separate file; defined in `compatibility/intent/intent_draft.py` | Yes, adapter and legacy parser methods | High | MIGRATE |
| `IntentSpecBuilder` implementation | No separate file; defined in `compatibility/intent/intent_elements.py` | Yes, adapter and legacy parser methods | High | MIGRATE |

### 2.2 Reference locations

Production/compatibility references:

- `task/understanding.py:17-20` imports `parse_draft`, `compile_draft`,
  `parse_elements`, and `build_elements` from the adapter.
- `task/understanding.py:920-991` retains the old Elements/Builder and
  Draft/Compiler helper methods.
- `compatibility/intent/adapter.py` is the only intended production bridge to
  the historical models and converters.
- `task/intent_draft.py` and `task/intent_elements.py` are re-export shims for
  old imports.
- `tests/compat/intent/test_intent_draft.py` and
  `tests/compat/intent/test_intent_elements.py` intentionally exercise the
  compatibility contract.

The Direct IntentSpec method `_try_l2_v2()` calls the direct path, but the
module still has a static compatibility import and legacy fallback methods.
Therefore deletion is blocked until the import boundary is made lazy or the
fallback is retired with its tests and callers.

### 2.3 Missing candidates

No standalone files named `IntentCompiler` or `IntentSpecBuilder` exist.
Those classes are embedded in the two compatibility implementation modules;
they must be tracked as symbols, not treated as absent code.

## 3. Legacy Agent Candidates

| Candidate | Source reference count | Reference locations | Production dependency | Risk | Action |
| --- | ---: | --- | --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/agent.py` | 1 production import; 9 test/import locations | `apps/assistant_api/.../services/legacy_agent_service.py`; `tests/unit/test_revision_orchestration.py`; `tests/integration/test_assistant_runtime_contracts.py` | Yes, through legacy service | High | KEEP / MIGRATE |
| `apps/assistant_api/.../services/legacy_agent_service.py` | 3 external production files plus internal implementation | `main.py`, `services/assistant_service.py`, `api/routes.py` | Yes | High | KEEP / MIGRATE |
| `community-assistant-agent/` | CI, P0 harness, own tests, API/data references | `.github/workflows/verify.yml`, `scripts/run_p0_e2e.py`, own `app/`, tests, migrations | Compatibility/deployment surface | Very high | KEEP |

The runtime relationship remains:

```text
Assistant API
  -> AssistantService
  -> RuntimeRouter
  -> RuntimeAgentService or LegacyAgentService
  -> CommunityOperationsAssistant (legacy path)
```

`LegacyAgentService` is instantiated in `main.py`, accepted by
`AssistantService`, and lazily reconstructed by `routes.py`. `agent.py` is
therefore not dead code even though it is outside the ACTIVE Runtime path.

`community-assistant-agent` also owns historical `assistant_runs`, `run_id`,
approval, and task/worker behavior. No deletion is possible until the API,
data, CI, and deployment migration is complete.

## 4. Creator Candidates

| Candidate | Source reference count | Reference locations | Production dependency | Risk | Action |
| --- | ---: | --- | --- | --- | --- |
| `services/creator_agent/` | 5 manifest/lockfile locations; 0 Python imports | root `pyproject.toml`, `uv.lock`, own `pyproject.toml` | Workspace/package dependency only; no runnable code found | Medium | ARCHIVE after manifest audit |
| `services/creator_agent/greenbook_creator_agent/` | 0 runtime imports | package `__init__.py` files only | No direct runtime use found | Medium | ARCHIVE |
| `services/greenbook_mcp/.../workflows/create_draft.py` | 0 callers outside itself | function definition only | No active caller found | Medium | ARCHIVE |
| `services/greenbook_mcp/.../workflows/revise_draft.py` | 0 callers outside itself | function definition only | No active caller found | Medium | ARCHIVE |
| `creator-agent/` | CI, P0 harness, Docker/deployment, own tests | `.github/workflows/verify.yml`, `scripts/run_p0_e2e.py`, own project | Yes, active service implementation | Very high | KEEP |
| `packages/creator_client/` | API main, MCP server/context, tools, integration tests | `apps/assistant_api/.../main.py`, `services/greenbook_mcp/...`, tests | Yes, active client boundary | High | KEEP |

The active path uses `tools/content.py` and `CreatorClient.create_task()` /
`wait_for_completion()` / `get_artifact()`. The two `*_via_creator.py`
workflow functions use an older `submit_task()` shape and have no discovered
production caller; this is strong archive evidence, but not deletion proof.

## 5. Scripts and Temporary Tools

The requested files `scripts/debug_intent_parse.py` and
`scripts/debug_llm_output.py` are not present in the current repository.
No debug script matching those names was found by `rg`.

The existing scripts are operational or verification tools:

- `scripts/run_p0_e2e.py` launches Creator/Assistant/Java processes and is
  referenced by the current end-to-end workflow.
- `scripts/test_run_p0_e2e.py` tests that harness.
- `scripts/start-*.ps1`, `dev-up.ps1`, `verify-all.ps1`, and `e2e-test.ps1`
  are local environment or verification entrypoints.

No script is a DELETE candidate solely because it contains temporary-file
handling. Temporary paths in `run_p0_e2e.py` are deliberate test isolation.

## 6. ACTIVE Runtime Dependency Check

### No direct dependency found

The following ACTIVE packages have no direct references to the audited legacy
symbols:

- `packages/assistant_core/greenbook_assistant_core/planning/`
- `packages/assistant_core/greenbook_assistant_core/execution/`
- `packages/assistant_core/greenbook_assistant_core/capability/`
- `apps/assistant_api/.../services/runtime_agent_service.py` for the legacy
  Agent and Creator service names.

### Dependency still present at the Understanding boundary

`packages/assistant_core/greenbook_assistant_core/task/understanding.py` is
part of the active Understanding package, but it still imports the
compatibility adapter and contains the old parser methods. This produces the
following boundary:

```text
Active Understanding module
  -> compatibility.intent.adapter
  -> IntentDraft / IntentElements / Compiler / Builder
```

This is not a Planner or Execution Runtime dependency, and the Direct
IntentSpec method is the formal L2 path. It is nevertheless a real module
dependency and blocks a DELETE recommendation for the intent implementations.

## 7. Recommended Final Actions

### KEEP

- `IntentSpec`, validator, Planner, TaskPlan, PlanExecution, and Execution
  Runtime packages;
- `compatibility/intent/` and the two task import shims during migration;
- `agent.py`, `LegacyAgentService`, and `community-assistant-agent` until
  legacy API/data consumers are retired;
- `creator-agent/`, `packages/creator_client/`, and active MCP content tools;
- operational scripts and their tests.

### MIGRATE

- Remove the static compatibility import from `understanding.py` after
  retiring or lazy-loading all legacy parser methods.
- Confirm no callers need `IntentDraft`, `IntentElements`, `IntentCompiler`, or
  `IntentSpecBuilder`; then move compatibility tests to a final legacy suite.
- Migrate all legacy run/approval/event consumers to the execution adapter.

### ARCHIVE

- `services/creator_agent/` and its empty package skeleton after workspace
  dependency review;
- the two unwired Creator workflow modules after test/reference confirmation;
- historical Creator portions of `community-assistant-agent` only after data
  and deployment retirement.

### DELETE

No file meets the required four-part deletion proof in this audit. In
particular, no Intent shim or legacy Agent file is currently safe to delete.

## 8. Scan Evidence

The audit used repository-wide `rg` scans over source, tests, scripts, and
documentation while excluding generated environments and caches. Important
findings were:

- no files named `debug_intent_parse.py` or `debug_llm_output.py` exist;
- no standalone `IntentCompiler.py` or `IntentSpecBuilder.py` exists;
- legacy intent symbols are implemented under `compatibility/intent/` and
  re-exported by `task/` shims;
- `agent.py` has direct production use through `LegacyAgentService` and direct
  test coverage;
- `services/creator_agent` is referenced by workspace metadata but has no
  runtime Python imports;
- `creator-agent` is referenced by CI and the P0 process harness.

No source code, test, import, or runtime behavior was changed in this phase.
