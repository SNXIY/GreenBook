> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# GreenBook Agent Runtime v2 Fast Track — Phase 2

## 1. Current Problems

The Command Runtime established one structured command boundary, but complex
goals still depended on fixed `IntentSpec` actions, fixed orchestration
templates, and hand-authored task graph proposals. That made command
understanding and execution planning difficult to separate.

## 2. New Architecture

```text
User Goal
  -> CommandInterpreter
  -> Command
  -> GoalDecomposer (LLM structured output)
  -> GoalTree
  -> GoalCompiler
  -> existing ConversationTaskGraph / TaskPlan
  -> Planner validation
  -> existing Execution Runtime
```

`GoalDecomposer` understands a structured Command and produces semantic Goals
and explicit dependency edges. `GoalCompiler` converts that result to the
existing graph and plan contracts. Worker, Queue, Execution, Artifact,
Checkpoint, Ledger, ToolRuntime, and MCP execution paths are unchanged.

The Planner now has a GoalTree entry point. It compiles declared capability
requirements and does not reinterpret the user's message or select an MCP
tool. Tool selection remains a later AgentLoop concern.

## 3. New Files

- `packages/assistant_core/greenbook_assistant_core/goal/__init__.py`
- `packages/assistant_core/greenbook_assistant_core/goal/models.py`
- `packages/assistant_core/greenbook_assistant_core/goal/decomposer.py`
- `packages/assistant_core/greenbook_assistant_core/goal/compiler.py`
- `tests/unit/test_goal_decomposer.py`
- `docs/migration/PHASE2_GOAL_DECOMPOSITION.md`

`Goal`, `GoalTree`, and `TaskNode` are Pydantic contracts. The decomposer
accepts only structured LLM output and a capability-only catalog; tool names
are deliberately omitted from its model context.

## 4. Modified Files

- `orchestration/orchestrator.py`: added `generate_goal_tree_plan()` and the
  `generate_plan(goal_tree=...)` primary entry point.
- `orchestration/templates.py`: documented existing templates as fallback
  execution templates for legacy/recovery callers.
- `conversation_runtime_adapter.py`: production composition can pass the
  canonical Command through GoalDecomposer and reuse `execute_graph()`.
- `main.py`: default API composition wires GoalDecomposer with the capability
  registry.
- `docs/migration/FAST_TRACK_PROGRESS.md`: Phase 2 progress and risks added.

## 5. Deleted Files

No core execution files were deleted. `TaskGraphBuilder` and the legacy
Intent/template path remain as compatibility fallback until all callers have
migrated.

## 6. Test Results

- `tests/unit/test_goal_decomposer.py`: 4 passed.
- Goal/Command/task-graph/runtime regression selection:
  `63 passed`.
- Full suite collection is still blocked by 5 pre-existing import errors.
  Excluding those files: `887 passed, 54 failed, 2 skipped`.
- The existing `TaskGraphBuilder`, Worker, Queue, and Execution contracts were
  not modified by this phase.

## 7. Next Phase

- Migrate remaining graph-producing callers to GoalDecomposer output.
- Remove duplicate Intent/resolver paths after their callers are drained.
- Add durable task-node input/output binding once AgentLoop/tool selection is
  introduced.
- Keep Memory and Execution changes out of this migration boundary.

## Current Risks

- The repository still contains pre-existing full-suite collection failures and
  dirty Runtime/API baseline failures recorded in `FAST_TRACK_PROGRESS.md`.
- Legacy template and Intent paths remain reachable for compatibility; they
  are no longer the primary path when Goal Runtime is wired.
