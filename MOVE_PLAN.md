# Phase 8.1-B Move Plan

This is the migration plan produced before archive moves. It records the
pre-move `rg` audit and separates executable moves from blocked moves.

## 1. community-assistant-agent

Target: `archive/legacy/community-assistant-agent/`

Status: BLOCKED. Do not move in this phase.

References found:

- `.github/workflows/verify.yml` uses it as the `assistant-agent` working
  directory;
- `scripts/verify-all.ps1` runs its tests;
- `scripts/smoke-test.ps1` starts/checks it;
- `scripts/setup-dev.ps1` provisions it;
- `scripts/runtime-report.ps1` enters it;
- `scripts/run_p0_e2e.py` starts its process and polls its legacy API;
- API identity and compatibility documentation still names the service;
- the project owns historical `assistant_runs`, `run_id`, approval, and
  migration data.

Moving it without updating CI/scripts would break the supported legacy test
and P0 workflows. Updating those references is outside the requested
no-code/no-CI scope. Revisit after the Legacy Runtime migration is complete.

## 2. zhiguang-be

Target: `archive/legacy/zhiguang-be/`

Status: SAFE TO MOVE after verifying the target does not exist.

Pre-move result:

- directory contains no files in the current checkout;
- no Python import found;
- no script, Docker Compose, or GitHub Actions reference found;
- references are documentation-only descriptions of the older backend.

Rollback: move `archive/legacy/zhiguang-be/` back to `zhiguang-be/`.

## 3. design-system

Target: `docs/design-system/`

Status: SAFE TO MOVE with documentation reference updates.

Pre-move result:

- contains design references and image previews only;
- no Python import, Docker, CI, or runtime dependency found;
- README and design master files contain path text that should be updated
  after the move.

Rollback: move `docs/design-system/` back to `design-system/` and restore
the documented path strings.

## 4. Existing Archive Normalization

Current archive paths:

- `archive/creator_agent/`
- `archive/greenbook_mcp/workflows/`

Target organization:

- `archive/creator/creator_agent/`
- `archive/workflows/`

Files are to be moved without content changes. This does not affect active
imports or workspace members.

## 5. Execution Order

1. Create `archive/legacy/`, `archive/creator/`, `archive/workflows/`, and
   `docs/design-system/` only after checking they do not contain conflicting
   files.
2. Move the empty `zhiguang-be/` directory.
3. Move `design-system/` and update documentation-only references.
4. Normalize the existing archive directories.
5. Re-run `rg` for old paths and verify no active code/config reference was
   introduced.
6. Do not move `community-assistant-agent/` until its CI, scripts, API, and
   data dependencies are retired.
