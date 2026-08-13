> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.6-D9-B Final Naming and Configuration

## Configuration alignment

The local and example configuration now use the same non-secret integration
values:

| Setting | Final value | Consumers |
|---|---|---|
| Redis host port | `26379` | `.env`, `.env.example`, Compose host mapping, Java/Python local startup |
| Assistant identity audience | `greenbook-assistant-runtime` | `.env`, `.env.example`, Python `AuthContextResolver`, Assistant startup defaults |
| Java access-token audience | includes `greenbook-assistant-runtime` | Java `JwtService`, Python Assistant JWT validation |

No secret value was changed. The Java issuer remains the authority that signs
the access token, and Python validates the same issuer, JWKS, and Runtime
audience. Creator's separate `creator-agent` audience remains unchanged.

## Repository naming

`assistant_runs` is a Legacy history/projection table. Its repository is now
canonically named `LegacyRunHistoryRepository`, making it explicit that it
does not store `PlanExecution` state, Runtime events, checkpoints, or tool
progress.

`RunRepository` remains an alias to `LegacyRunHistoryRepository` for existing
API and external callers. This preserves behavior and does not rename or
remove database data. Runtime state remains owned by `PlanExecution`,
`ExecutionStateManager`, and `ExecutionEventStore`; ID mapping remains in
`compatibility/history/RunExecutionLink`.

## Script organization

Scripts are classified by responsibility:

| Category | Current contents |
|---|---|
| `scripts/dev/` | Development-only additions may be placed here; existing root startup scripts remain compatibility entrypoints. |
| `scripts/verify/` | Verification and acceptance scripts may be placed here; existing root names remain stable for CI/docs. |
| `scripts/ops/` | Operator actions; `promote-admin.ps1` is now here. |

Only the operator script moved in this phase. Root startup and verification
entrypoints were retained to avoid breaking documented developer and CI
commands.

## Boundaries

```text
ACTIVE Runtime
  PlanExecution
  ExecutionStateManager
  ExecutionEventStore

History Compatibility
  compatibility/history/RunExecutionLink
  ExecutionReference
  run_execution_link repository

Legacy boundary
  assistant_runs
  LegacyRunHistoryRepository
  RunRepository compatibility alias
```

Worker, Planner, ToolRuntime, ExecutionStateManager, PlanExecution,
`assistant_runs` data, and `RunExecutionLink` were not removed or redesigned.

## Validation

The affected Assistant projection, History compatibility, and Runtime link
tests are the required regression set. Python compileall and `git diff
--check` must pass before this phase is accepted.
