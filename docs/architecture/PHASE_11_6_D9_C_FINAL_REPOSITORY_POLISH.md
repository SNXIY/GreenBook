# Phase 11.6-D9-C Final Repository Polish

## Reference Classification

| Reference | Classification | Boundary |
|---|---|---|
| `PlanExecution`, `ExecutionStateManager`, `ExecutionEventStore` | ACTIVE | Runtime execution source of truth |
| `RunExecutionLink`, `ExecutionReference`, history link repository | COMPATIBILITY | Identifier mapping and history lookup only |
| `LegacyRunHistoryRepository`, `assistant_runs` | COMPATIBILITY | Legacy history/projection metadata |
| `LegacyAgent`, `LegacyAgentService` | COMPATIBILITY / ARCHIVE boundary | Legacy fallback and historical documentation; not Runtime state |
| `community-assistant-agent` | ARCHIVE | Preserved historical workspace under `archive/legacy/` |
| `compatibility/runtime` | ARCHIVE / retired path | No active package or import remains |
| `assistant_runs.status` | COMPATIBILITY schema field | Nullable historical field; never Runtime truth |

Archive implementations and reports were not deleted or rewritten.

## Repository Alias

`LegacyRunHistoryRepository` is the canonical name for the repository owning
`assistant_runs`. The public compatibility alias remains:

```python
RunRepository = LegacyRunHistoryRepository
```

The alias is deprecated for new code because its generic name can be mistaken
for the Runtime execution repository. Existing API and external imports remain
valid. The alias can be removed only after all compatibility consumers,
historical reads, and deprecation-window checks are retired.

## Empty Resource Review

The repository contains empty package/test `__init__.py` markers, generated
build/browser profile directories, and intentional `.gitkeep` category files.
Package markers, generated state, and directory-layout contracts were
retained. Four confirmed empty, unreferenced containers were removed:
`apps/assistant_api/api`, `apps/assistant_api/dependencies`,
`apps/assistant_api/streaming`, and
`services/greenbook_mcp/greenbook_mcp_server/workflows` (including its empty
`__init__.py`). No archive content was touched.

No unreferenced configuration or script was proven safe to delete. Root
startup and verification scripts are referenced by development, acceptance,
or CI flows; `scripts/ops/promote-admin.ps1` remains an operator entrypoint.

## Current Architecture

```text
ACTIVE Runtime
  PlanExecution
  ExecutionStateManager
  ExecutionEventStore

History Compatibility
  RunExecutionLink -> execution_id
  ExecutionReference
  LegacyRunHistoryRepository -> assistant_runs

ARCHIVE
  archive/legacy/community-assistant-agent
  docs/archive/
```

`assistant_runs` must remain a history projection and must not become a second
Runtime execution store.

## Validation

The affected projection, History compatibility, Runtime link, and approval
reference tests pass. Python compileall and `git diff --check` are required
gates for this phase.
