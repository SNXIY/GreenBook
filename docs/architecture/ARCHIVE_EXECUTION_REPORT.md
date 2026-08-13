> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 7.10 Safe Archive Report

This report records the low-risk archive actions executed from `FINAL_CLEANUP_CANDIDATES.md`. No production Runtime, IntentSpec, Planner, Worker, ToolRuntime, Legacy Agent, or active `creator-agent` implementation was deleted or modified.

## 1. Moved Files

### Creator Workspace Package

Moved the complete contents of `services/creator_agent/` to `archive/creator_agent/`:

- `pyproject.toml`
- `greenbook_creator_agent/__init__.py`
- `greenbook_creator_agent/api/__init__.py`
- `greenbook_creator_agent/domain/__init__.py`
- `greenbook_creator_agent/graph/__init__.py`
- `greenbook_creator_agent/persistence/__init__.py`
- `greenbook_creator_agent/worker/__init__.py`

Reason: no Python runtime imports were found; the package contained only package entrypoints and metadata; references were limited to workspace metadata and lockfile entries; the complete runnable Creator service is `creator-agent/`.

### Unwired MCP Workflows

Moved:

- `services/greenbook_mcp/greenbook_mcp_server/workflows/create_draft.py`
- `services/greenbook_mcp/greenbook_mcp_server/workflows/revise_draft.py`

to `archive/greenbook_mcp/workflows/`.

Reason: no production caller, test import, Docker, CI, or deployment reference was found. Active content execution is implemented in `services/greenbook_mcp/greenbook_mcp_server/tools/content.py` and uses `CreatorClient.create_task()`.

The archived functions use an older `submit_task()` client shape and were not part of the active MCP tool registry.

## 2. Metadata Changes

Removed obsolete workspace-only references from the root metadata:

- `pyproject.toml`: `services/creator_agent` workspace member;
- `pyproject.toml`: `greenbook-creator-agent` workspace source;
- `pyproject.toml`: `services/creator_agent` test/pythonpath entry;
- `uv.lock`: `greenbook-creator-agent` manifest member and package record.

No application import or business contract was changed.

## 3. Reference Scan Results

After the move:

- `services/creator_agent` has no remaining Python import or runtime reference;
- `greenbook_creator_agent` has no remaining active source import;
- the old workflow paths have no remaining code, test, CI, or deployment caller;
- `creator-agent/` remains referenced by its own project, CI, Docker setup, and `scripts/run_p0_e2e.py`;
- `packages/creator_client/` remains referenced by Assistant API, MCP server, MCP context, active content tools, and integration tests;
- Intent compatibility and Legacy Agent references were not changed.

Historical architecture and phase documents still mention the old paths by design. They were not edited in this phase.

## 4. Recovery Procedure

The archive is recoverable because files were moved without content edits:

1. Move `archive/creator_agent/*` back to `services/creator_agent/`.
2. Move the two files from `archive/greenbook_mcp/workflows/` back to `services/greenbook_mcp/greenbook_mcp_server/workflows/`.
3. Restore the removed workspace member, source, pythonpath, and lockfile records for `greenbook-creator-agent`.
4. Run workspace lockfile validation and relevant tests.

The archive does not own runtime state or production data.

## 5. Tests

The required commands were run after archiving:

- `pytest tests/unit`: collection failed with 2 errors because the current interpreter does not have `fastapi` installed. This is an environment dependency failure, not an archive import failure.
- `pytest tests/evaluation`: 44 passed, 1 failed. The failure is the existing LLM evaluation case, which cannot import `openai`; no archived module appears in the failure.

No test failure referenced either archived path.

`uv lock --check` could not initialize the user uv cache because the current
environment denied access to `C:\Users\29238\AppData\Local\uv\cache`. This is
an environment permission issue; the direct `rg` scan found no stale workspace
path or archived workflow reference in active code/configuration.

## 6. Risk Assessment

- ACTIVE IntentSpec: unchanged.
- Planner: unchanged.
- Worker: unchanged.
- Execution Runtime: unchanged.
- ToolRuntime: unchanged.
- Legacy Agent: retained.
- Active `creator-agent/`: retained.
- Archived workspace package and unwired workflows: recoverable by path move.

## 7. Files Changed by This Phase

- Moved: `services/creator_agent/*` to `archive/creator_agent/`.
- Moved: the two historical MCP workflow files to
  `archive/greenbook_mcp/workflows/`.
- Updated workspace metadata: root `pyproject.toml` and `uv.lock`.
- Added this report.
- No production Python source was edited.
