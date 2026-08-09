# Phase 11.4-B Legacy Fallback Isolation

## Scope

This phase isolates the Legacy Agent fallback at the Assistant API service
boundary. It does not remove the fallback and does not modify the ACTIVE
execution chain.

Protected components:

- Worker
- Planner
- ToolRuntime
- ExecutionStateManager
- `community-assistant-agent/`

## 1. Audit Result

Before this change, `AssistantService` selected a path through
`RuntimeRouter`, but its Runtime exception handler directly invoked
`LegacyAgentService`:

```text
RuntimeAgentService.execute()
  -> exception
  -> AssistantService._execute_legacy()
  -> LegacyAgentService.execute()
```

The existing behavior had three gaps:

- fallback was controlled only by the context's optional
  `fallback_allowed` attribute;
- there was no explicit deployment-level `ENABLE_LEGACY_AGENT_FALLBACK`
  switch;
- fallback count, failure reason, and route were not exposed as service
  telemetry.

The normal legacy route selected by `RuntimeRouter` is not counted as a
fallback. Only a Runtime exception followed by a Legacy invocation is
counted.

## 2. Configuration

Added configuration:

```text
ENABLE_LEGACY_AGENT_FALLBACK=true|false
```

Behavior:

| Configuration | Runtime success | Runtime failure |
| --- | --- | --- |
| unset | Runtime result | Fallback through adapter; compatibility default |
| `true`, `yes`, `on`, `1` | Runtime result | Fallback through adapter |
| `false`, `no`, `off`, `0` | Runtime result | Return Runtime `FAILED`; do not invoke Legacy |

The default is `true` to preserve current deployments. An explicit false
value takes precedence over the context's legacy compatibility behavior.

The setting can also be passed as `enable_legacy_fallback` to
`AssistantService`, which makes unit and controlled deployment tests
deterministic.

## 3. Adapter Boundary

Added:

`apps/assistant_api/greenbook_assistant_api/services/legacy_fallback_adapter.py`

`LegacyFallbackAdapter` is the only invocation boundary used by
`AssistantService` for Legacy execution. It owns no state and does not alter
the result; it delegates the existing `execute(RuntimeContext)` contract.

The resulting service relationship is:

```text
AssistantService
  -> LegacyFallbackAdapter
  -> LegacyAgentService
  -> legacy agent implementation
```

Runtime success has no Legacy dependency. Legacy is reachable only from the
explicit fallback branch or from the pre-existing Legacy route selected by
`RuntimeRouter` when the application is configured for legacy mode.

## 4. Fallback Telemetry

`AssistantService` now records:

- `fallback_count`: number of Runtime failures that invoked Legacy;
- `failure_reasons`: counts keyed by Runtime exception type;
- `last_fallback_route`: currently
  `runtime_failure->legacy_adapter`.

The service exposes these through `fallback_metrics()` for diagnostics and
evaluation. It also logs:

- Runtime failure reason and whether fallback is enabled;
- fallback count, route, and reason when fallback is triggered.

Example:

```json
{
  "fallback_count": 1,
  "failure_reasons": {"TimeoutError": 1},
  "last_fallback_route": "runtime_failure->legacy_adapter"
}
```

The telemetry is process-local diagnostic state. It is not an execution state
model and does not replace Trace, PlanExecution, or the Execution EventStore.

## 5. Failure Behavior

When Runtime execution raises an exception:

1. The exception type is logged as the Runtime failure reason.
2. If `ENABLE_LEGACY_AGENT_FALLBACK` is enabled and the context does not
   explicitly disable fallback, the fallback counter is updated.
3. The LegacyFallbackAdapter invokes the existing Legacy service.
4. The returned result is marked `execution_path="legacy"`.
5. If fallback is disabled, no Legacy call occurs and a Runtime `FAILED`
   result with `execution_path="runtime"` is returned.

The existing context type does not currently declare `fallback_allowed` in all
versions of the codebase. The boundary therefore reads it defensively with a
default of `true`, while the new environment/configuration flag remains the
authoritative deployment control.

## 6. Files Changed

- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
  - added explicit fallback configuration, adapter use, failure telemetry, and
    disabled-fallback behavior.
- `apps/assistant_api/greenbook_assistant_api/services/legacy_fallback_adapter.py`
  - added the Legacy invocation boundary.
- `apps/assistant_api/greenbook_assistant_api/main.py`
  - continues injecting the existing Legacy service into `AssistantService`,
    which owns the single adapter wrapping boundary.
- `tests/unit/test_legacy_fallback_isolation.py`
  - added success isolation, default fallback, and disabled fallback tests.
- `docs/architecture/PHASE_11_4_B_LEGACY_FALLBACK_ISOLATION.md`
  - this audit and implementation report.

No changes were made to Worker, Planner, ToolRuntime, ExecutionStateManager,
RuntimeAgentService execution logic, or `community-assistant-agent/`.

## 7. Verification

Passed:

- `tests/unit/test_legacy_fallback_isolation.py`: 3 passed
- `tests/unit/test_runtime_router.py tests/compat/runtime/test_run_operation_adapter.py`: 16 passed
- `git diff --check` for changed source, tests, and report

The full repository test suite was not run in this phase.

## 8. Remaining Risks

- `ASSISTANT_RUNTIME_MODE=off` still intentionally routes new turns to
  Legacy; this phase only controls Runtime-failure fallback.
- Legacy fallback telemetry is process-local and should later be connected to
  the existing observability/evaluation pipeline if operational dashboards
  require aggregate counts.
- The Runtime and Legacy services may have different side-effect semantics;
  fallback policy should be tightened after idempotency and partial-execution
  evidence is available.
- The fallback adapter isolates invocation but does not retire
  `LegacyAgentService`, `agent.py`, or `community-assistant-agent`.

## Decision

Legacy fallback remains enabled by default for compatibility, but it no longer
has implicit execution authority: Runtime failure must pass through an
explicit adapter and is observable. Setting
`ENABLE_LEGACY_AGENT_FALLBACK=false` provides a Runtime-only failure mode
without changing the ACTIVE Runtime implementation.
