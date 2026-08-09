# Phase 10 Final Engineering Report

## Completed Work

### Part 1: Evaluation

Added [docs/evaluation/evaluation_report.md](docs/evaluation/evaluation_report.md), which unifies the existing evaluation layer around:

- Intent: Mode Accuracy, Action Coverage, Condition Accuracy, Constraint Accuracy, Repair Success;
- Planner: Action Coverage, Resource Match, Step Ordering, Constraint Propagation;
- Execution: Success Rate, Failure Rate, Retry Rate, Latency, Human Approval Rate.

The document links the existing `ExecutionEvaluator`, `PlannerEvaluator`, `MetricsCalculator`, `BadCaseStore`, datasets and test entrypoints. No Runtime behavior was changed.

### Part 2: Repository Audit

Added [docs/architecture/FINAL_REPOSITORY_AUDIT.md](docs/architecture/FINAL_REPOSITORY_AUDIT.md).

Audit conclusions:

- ACTIVE: `apps/`, `packages/`, `services/greenbook_mcp/`, and current tests;
- COMPATIBILITY: `community-assistant-agent`, `LegacyAgentService`, `TaskIntent`, `intent_compat`, `RunRepository`, `run_id`, IntentDraft and IntentElements;
- ARCHIVE: `archive/` and `docs/archive/`;
- DELETE_CANDIDATE: no production code is safe to delete in this pass.

The baseline report `docs/reports/assistant-runtime-baseline.json` was moved to `docs/archive/phase-reports/` without content changes. No legacy production code was deleted.

### Part 3: Documentation

Updated the root [README.md](README.md) as the formal project entrypoint. It now covers project positioning, architecture, repository boundaries, Runtime flow, startup commands, demo entrypoint and evaluation entrypoint.

The primary documentation structure is:

```text
docs/
  architecture/   formal architecture and audit
  demo/            end-to-end demonstration
  evaluation/     unified evaluation definitions
  archive/        historical reports and drafts
```

`docs/design-system/`, `docs/ACCEPTANCE.md`, `docs/INTEGRATION.md` and other operational reference documents remain available as supporting material.

## Validation

### Evaluation tests

`pytest tests/evaluation`:

- 44 passed;
- 1 failed during LLM evaluator import because the current interpreter lacks `openai`:
  `tests/evaluation/test_intent_v2_llm_eval.py::test_llm_intent_evaluation`.

This is an environment dependency failure. No live model metric is reported from that run.

### End-to-end demo

`pytest tests/e2e/test_greenbook_runtime_demo.py`:

- 1 passed.

The demo validates `User Request -> IntentSpec -> Planner -> TaskPlan -> PlanExecution -> EventStore`, including retry, pause/resume and approval events.

## Scope Confirmation

Unchanged:

- Worker;
- Planner core logic;
- ToolRuntime;
- IntentSpec;
- ExecutionStateManager;
- PlanExecution and the canonical Runtime state model.

No second Runtime lifecycle or state model was introduced.

