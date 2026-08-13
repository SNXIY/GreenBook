> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 8.3 Workspace Stabilization Report

## Workspace Check

- `apps/backend/`: present and referenced by Docker database mounts, CI, and backend scripts.
- `apps/frontend/`: present and referenced by CI, frontend scripts, and README.
- `apps/creator-agent/`: present and referenced by CI, P0 E2E, Creator scripts, and its local Dockerfile.
- `community-assistant-agent/`: unchanged and retained as the legacy compatibility application.
- Root uv workspace: valid in structure; it continues to contain the Python libraries, Assistant API/worker, and MCP service. The three independently built applications are not workspace members.
- `uv.lock`: no workspace or package-name change was required.
- Package imports: unchanged; imports continue to use package names rather than the moved application directory names.
- Docker Compose: backend schema mounts point to `apps/backend/db`; infrastructure service definitions are unchanged.
- Dockerfile: the Creator Dockerfile is now at `apps/creator-agent/Dockerfile`; no image logic or package name changed.
- GitHub Actions: Java, frontend and Creator jobs use the new `apps/` working directories; the legacy Assistant job remains at `community-assistant-agent`.
- Scripts: startup, smoke, setup, verification and P0 paths use the new locations.

## Modified Files

Added:

- `docs/architecture/REPOSITORY_STRUCTURE_FINAL.md`
- `docs/architecture/PHASE_8_3_REPORT.md`

No source, package metadata, planner, worker, ToolRuntime, IntentSpec, database or Execution Runtime files were modified in Phase 8.3.

## Validation

The path audit found no executable reference to the removed top-level application directories. Remaining `creator-agent` matches are intentional service names, URL paths, identity audience values, package metadata, or historical documentation.

`git diff --check` passed for the Phase 8.2 path updates and the new documentation.

## Test Result

The repository test suites were rerun after the stabilization audit:

- `pytest tests/unit`: collection blocked by the environment missing `fastapi`.
- `pytest tests/evaluation`: 44 passed; 1 LLM evaluation failed because the environment is missing `openai`.

These are dependency/environment failures, not workspace path failures.

## Scope Confirmation

- Python package names: unchanged
- IntentSpec: unchanged
- Planner: unchanged
- Worker: unchanged
- Execution Runtime: unchanged
- ToolRuntime: unchanged
- `community-assistant-agent`: not moved
