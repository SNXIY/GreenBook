> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# GreenBook Agent Runtime v2

# Phase 5 Context & Durable Memory

## 1. Goal

Make Context a bounded working-set projection and Memory a durable,
retrievable long-term decision input without changing the reliable execution
layer. Context and Memory now enter the Command, Goal, AgentLoop, and Planner
contracts; neither module is a business router or a replacement for facts in
Task, Artifact, or Execution repositories.

## 2. Architecture Before

Conversation context was persisted by `conversation/context_manager.py`, but
the API adapter independently joined Tasks, Artifacts, and Executions. Agent
state retained an ever-growing dictionary/history projection. `agent_memory`
stored records in a process-local dictionary and was only partially consumed
by direct Runtime execution. Queue messages also carried a `TaskIntent`
snapshot.

## 3. Architecture After

```text
Conversation / API
    -> ContextBuilder -> bounded ContextSnapshot
    -> CommandInterpreter(message + snapshot)
    -> Command
    -> ContextBuilder(Command + durable facts + Memory retrieval)
    -> GoalDecomposer(Command + snapshot)
    -> GoalTree -> TaskManager -> AgentLoop
    -> DynamicPlanner(snapshot projection) / ToolSelector
    -> ToolPolicyGate -> ExecutionInput -> Queue / Worker
```

`TaskRepository`, `ExecutionRepository`, and `ArtifactStore` remain facts.
`MemoryRepository` is the source of long-term memory. PostgreSQL is used by
the production composition when the Runtime persistence profile is
PostgreSQL; local tests use the injected repository implementation.

## 4. Context Model

Added `context/models.py` with `ContextSnapshot` and `ContextBudget`. The
snapshot contains conversation scope, bounded recent messages and summary,
active Tasks, unfinished Goals, Task/Execution states, Artifact references,
target candidates, recent operations, resources, preferences, recalled
memories, and trace identifiers (`snapshot_id`, `memory_ids_used`, and
`plan_version`).

`context/builder.py` is the only cross-source join. `context/projection.py`
keeps deterministic conversion of Pydantic and repository records. The old
conversation manager remains the durable message/summary storage adapter;
summary never replaces structured Task, Goal, Artifact, or Execution IDs.

## 5. Context Budget

`ContextBudget` enforces a recent-message count and character limit while
preserving structured state independently. It also bounds Tasks, Goals,
Artifacts, resources, operations, target candidates, and recalled memories.
Messages are selected from the recent end and restored to chronological order;
the system does not blindly inject an unbounded `messages[-50:]` slice.

## 6. Durable Memory Model

Added `memory/models.py` with `MemoryRecord`, `MemoryQuery`, and
`MemoryType`: `EPISODIC`, `SEMANTIC`/`PREFERENCE`, and `PROCEDURAL`.
Records include user/conversation/Task relations, structured metadata,
importance, confidence, source information, timestamps, access tracking, and
optional expiry. No chain-of-thought field exists.

Added `MemoryRepository` with `InMemoryMemoryRepository` for tests/local use
and `PostgresMemoryRepository` for the current PostgreSQL session boundary.

## 7. Memory Write Policy

`memory/policy.py` permits writes only for Task completion, major failure or
success, explicit preference, user correction, an explicit remember request,
or a validated reusable strategy. Ordinary conversation does not create a
record. Existing execution outcome extraction writes bounded summaries and
structured evidence rather than hidden reasoning.

## 8. Memory Retrieval

`memory/retriever.py` performs candidate retrieval, lexical/structured
reranking, relation boosts, recency/importance/confidence scoring, filtering,
and access accounting. It does not create hash embeddings. Cross-conversation
preferences are eligible; conversation and Task relations are ranking evidence
rather than unsafe hard selection. Empty retrieval is a normal runtime case.

## 9. Target Resolution Integration

`ContextBuilder` projects Task, Artifact, Execution, resource, and explicit
active bindings into `target_candidates`. `CommandContext.from_any` consumes
those candidates and active fields, while `TargetResolver` still returns only
`Resolved`, `Ambiguous`, or `NotFound`. No candidate is selected merely
because it is first or currently active.

## 10. AgentLoop Integration

`ConversationRuntimeAdapter` builds the initial snapshot for Command
interpretation, rebuilds it with the structured Command for relevant Memory
retrieval, and supplies the same bounded projection to GoalDecomposer and
AgentLoop. AgentLoop can refresh the snapshot before each Observe cycle using
the injected builder and scope. `AgentState` stores the latest snapshot and
audit IDs, not an unbounded context history. Observation includes execution
states, Artifact projections, waiting-human state, and Memory IDs used.

## 11. Creator Boundary

Memory remains inside GreenBook. Creator tools receive prepared preferences,
strategy summaries, or Artifact references through typed arguments; Creator
does not access the GreenBook Memory database directly.

## 12. Removed Legacy

The old queue snapshot dependency was removed from the new submission path:
`TaskIntent` is no longer serialized into new queue envelopes. A narrow worker
boundary conversion can read a pre-Phase-5 development envelope once; it does
not restore Intent understanding or become a default route.

## 13. ExecutionInput Migration

Added `execution/input.py`. `ExecutionInput` contains resolved Task/Goal/Plan
IDs, capabilities, arguments/constraints, Artifact references, policy-safe
execution metadata, and the validated executable plan. New submissions use:

```text
GoalCompiler -> TaskPlan -> ExecutionInput -> ExecutionQueue -> Worker
```

Queue/Worker/Retry/Checkpoint/Ledger/Lease/Evidence behavior was not
rewritten. Direct resolved execution still accepts the old `TaskIntent` shape
for existing reliable-execution callers only.

## 14. Deleted Files

| Path | Old responsibility | Replacement | Reason |
|---|---|---|---|
| `packages/assistant_core/greenbook_assistant_core/context.py` | Single lightweight session model module | `context/__init__.py` plus `context/models.py`, `builder.py`, `manager.py`, `projection.py` | Context now has an explicit runtime boundary and the session contract is re-exported from the package. |
| `packages/assistant_core/greenbook_assistant_core/memory.py` | Empty/in-memory conversation-memory port | `memory/conversation.py` and durable `memory/` package; conversation facts remain in `conversation.ContextManager` | Removed the file/package name collision and made long-term Memory contracts canonical. |

The `agent_memory/` path was not duplicated: its models/store are now thin
compatibility exports over `memory/` so existing reliable-execution callers
can be migrated without a second Memory implementation.

## 15. Database Migration

Added `packages/assistant_core/greenbook_assistant_core/db/migrations/008_context_durable_memory.sql`.
It creates `agent_memories` with user, relation, type, structured metadata,
importance/confidence, provenance, lifecycle timestamps, access count, expiry,
and indexes for user/type and Task retrieval. PostgreSQL remains the source of
truth; no Redis dependency was introduced.

## 16. Test Results

Full suite after the Context/Memory/ExecutionInput changes:

```text
665 passed, 2 skipped, 3 warnings
```

New coverage:

- `tests/unit/test_context_builder.py`
- `tests/unit/test_memory_repository.py`
- `tests/unit/test_memory_retriever.py`
- `tests/integration/test_context_memory_runtime.py`

Also verified Command Runtime, Goal Decomposer, AgentLoop, Target Resolution,
queue execution, conversation context, and the existing Agent Memory tests.
`compileall` passed. Ruff passed for all Phase 5 touched modules after import
cleanup.

## 17. Memory Evaluation Cases

Covered behavior includes:

1. Java and Python candidates remain distinguishable by structured/semantic
   evidence.
2. Multiple equal candidates return `Ambiguous`.
3. A preference stored under one conversation is retrievable in another.
4. A normal “say hello” command creates no Memory.
5. Repository access counts are durable within the repository boundary.
6. Empty Memory retrieval does not fail the Agent runtime.
7. Context budgets preserve Task/Artifact state while limiting messages.

## 18. Remaining Technical Debt

- `RuntimeAgentService`, `ArgumentBinder`, and `CapabilityExecutor` still
  accept `TaskIntent`/`IntentSpec` as resolved execution compatibility
  contracts. They are not used for natural-language understanding or new
  queue payloads; the next cleanup can replace their internal projection with
  `ExecutionInput` completely.
- The synchronous `agent_memory.MemoryManager` facade maintains a local cache
  for old direct callers and mirrors to PostgreSQL when injected. New
  ContextBuilder retrieval uses the canonical repository directly.
- Conversation message compression still uses the existing deterministic
  summary builder; an LLM summary provider can be added behind that boundary
  without moving structured facts into the summary.

## 19. Next Phase

Use the bounded ContextSnapshot and durable Memory contracts for evaluation,
resume/recovery, and user-correction learning. Do not add another router or
business Agent class. The next migration should retire the remaining resolved
execution `TaskIntent` parameters after all reliable-execution callers accept
`ExecutionInput`.
