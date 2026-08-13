> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.2 Creator Compatibility Retirement Report

## Scope

This phase audited only:

- `services/creator_agent/`
- `services/greenbook_mcp/greenbook_mcp_server/workflows/create_draft.py`
- `services/greenbook_mcp/greenbook_mcp_server/workflows/revise_draft.py`

The scan covered Python imports, `pyproject.toml`, `uv.lock`, Docker and
compose files, GitHub Actions, scripts, README files, and tests.

## Result

No new move was executed in this phase.

The requested source paths are already absent from the working tree and show
as deleted in the existing Git worktree state. Their apparent replacements
are already present under:

- `archive/creator/creator_agent/`
- `archive/workflows/create_draft.py`
- `archive/workflows/revise_draft.py`

Because these are pre-existing working-tree/archive changes, they were not
re-moved or rewritten. This preserves the user's current uncommitted state.

The requested destination for a fresh retirement move was `archive/legacy/`.
The existing archive locations were not changed to that destination in this
phase.

## Reference Scan

### `services/creator_agent/`

| Reference class | Result |
| --- | --- |
| Python runtime imports | No reference to `services/creator_agent` or `greenbook_creator_agent` found in the scanned active source paths |
| Workspace metadata | The source `services/creator_agent/pyproject.toml` is absent in the current working tree and is already marked deleted; no active path reference was found by the scan |
| `uv.lock` / root `pyproject.toml` | No matching `services/creator_agent` or `greenbook_creator_agent` reference found |
| Docker / compose | No matching source-path reference found |
| CI | No matching source-path reference found; CI points to `apps/creator-agent` |
| Scripts | No matching source-path reference found; scripts point to `apps/creator-agent` |
| README | README documents `apps/creator-agent` as the active Creator service |
| Tests | No active test import of the removed workspace package found |

The current active Creator path is `apps/creator-agent/`, referenced by
`.github/workflows/verify.yml`, `scripts/setup-dev.ps1`,
`scripts/smoke-test.ps1`, `scripts/start-creator.ps1`,
`scripts/verify-all.ps1`, and `scripts/run_p0_e2e.py`. Those references are
not references to the retired compatibility package and must remain.

### MCP historical workflows

| Path | Python imports/callers | Tests | CI/Docker/scripts/README | Current state |
| --- | --- | --- | --- | --- |
| `services/greenbook_mcp/greenbook_mcp_server/workflows/create_draft.py` | No caller/import found for this workflow module | No direct test import found | No path reference found | Already present at `archive/workflows/create_draft.py`; source marked deleted |
| `services/greenbook_mcp/greenbook_mcp_server/workflows/revise_draft.py` | No caller/import found for this workflow module | No direct test import found | No path reference found | Already present at `archive/workflows/revise_draft.py`; source marked deleted |

The active names `content.create_draft` and `content.revise_draft` are not
these historical workflow modules. They are active MCP tools implemented in
`services/greenbook_mcp/greenbook_mcp_server/tools/content.py`, registered by
`tool_registry.py`, consumed by capabilities, and covered by unit,
integration, evaluation, and E2E tests. They must not be archived.

## Archive Decisions

| Candidate | Eligibility | Decision |
| --- | --- | --- |
| `services/creator_agent/` | No active reference found; source is already absent and its contents are represented under `archive/creator/` | Already archived in existing worktree state; no action |
| Historical `create_draft.py` workflow | No caller or deployment reference found; source is already absent | Already archived under `archive/workflows/`; no action |
| Historical `revise_draft.py` workflow | No caller or deployment reference found; source is already absent | Already archived under `archive/workflows/`; no action |
| `apps/creator-agent/` | Active CI, scripts, README, P0, and deployment references | KEEP |
| `services/greenbook_mcp/.../tools/content.py` | Active tool registry and broad test coverage | KEEP |

## Why No Additional Move Was Performed

The source candidates are not present to move, and the worktree already
contains archive directories and uncommitted deletions. Moving the existing
archive content from `archive/creator` or `archive/workflows` to
`archive/legacy` would be an additional archive reorganization, not the
requested source retirement, and could overwrite or alter user work.

No code, ACTIVE Creator service, Runtime, Planner, Worker, ToolRuntime,
ExecutionStateManager, or Runtime API was modified.

## Rollback / Recovery

The existing archive state can be recovered by restoring the corresponding
files from the repository's Git history or by reversing the pre-existing
working-tree move:

- `archive/creator/creator_agent/` -> `services/creator_agent/`
- `archive/workflows/create_draft.py` ->
  `services/greenbook_mcp/greenbook_mcp_server/workflows/create_draft.py`
- `archive/workflows/revise_draft.py` ->
  `services/greenbook_mcp/greenbook_mcp_server/workflows/revise_draft.py`

Any rollback must be reviewed against the current worktree before execution;
no rollback operation was run here.

## Test Result

No tests were run. This phase produced an audit report only and made no new
source or test changes. Existing uncommitted changes remain untouched.

## Final Status

- `services/creator_agent/`: already absent/archived in existing worktree state
- Historical MCP workflows: already absent/archived in existing worktree state
- New files moved by this phase: none
- Active Creator path: retained
- Active MCP content tools: retained
- Runtime architecture: unchanged
