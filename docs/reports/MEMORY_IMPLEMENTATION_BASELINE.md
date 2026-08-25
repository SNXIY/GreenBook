# Memory Implementation Baseline

Audit date: 2026-08-25

Audit status: read-only. No production Memory, Conversation, Task, Runtime,
MCP, or RAG code was changed in Phase 1.

## Existing Components

### Domain contracts

- `packages/agent_core/greenbook_agent_core/memory/models.py`
  - `MemoryRecord` is the existing durable contract.
  - `MemoryQuery` supports user, conversation, task, type, metadata,
    importance, limit, and sort filters.
  - `MemoryType.PREFERENCE` is a compatibility alias whose storage value is
    `SEMANTIC`. This is the existing preference representation and should be
    preserved while the vertical slice adds an explicit preference contract.
- `memory/policy.py` contains a conservative event write policy. It allows
  explicit preferences and corrections, but does not itself classify natural
  language into a preference.
- `memory/manager.py` provides `remember`, `recall`, `forget`, preference,
  episodic, procedural, correction, and execution helper methods.
- `memory/retriever.py` performs lexical candidate retrieval and deterministic
  reranking. It has no embedding/index implementation and currently queries
  by `user_id` only.
- `memory/extractor.py` only contains `ProceduralMemoryExtractor`; there is no
  Conversation-to-preference extractor.

### Preference surface

- `conversation/preferences.py` contains `UserPreference` and
  `MemoryUserPreferenceProvider`.
- The provider reads semantic records and requires two observations unless a
  record is marked explicit. It has a schedule-time observation helper and an
  explicit set helper.
- The provider currently accepts `user_id` but not `tenant_id`, and the
  explicit set path mutates metadata after the manager has saved the record;
  the new slice must replace this with an auditable, tenant-scoped write.

### Context and runtime

- `context/models.py` already has bounded `user_preferences`,
  `recalled_memories`, and `memory_ids_used` fields on `ContextSnapshot`.
- `context/builder.py` is the canonical join point for Conversation, Task,
  Execution, Observation, Artifact, Resource, preference, and Memory
  projections. It bounds memory count and text size.
- `turn/context_assembler.py` reuses `ContextBuilder`; it does not create a
  second context source.
- `conversation_runtime_adapter.py` wires a `MemoryRetriever` into
  `ContextBuilder`, but `_build_context_snapshot()` passes
  `memory_recall=False`.
- `main.py` wires the same retriever into both the Conversation adapter and
  the canonical turn `ContextAssembler`. The retriever is present, but the
  production flag is not enabled.
- `context/projection.py::project_interpreter_context()` deliberately hides
  canonical identities, but currently omits `user_preferences` and
  `recalled_memories`; therefore existing recalled memory is not a clear
  provider-facing preference input.
- `execution/runtime_agent_service.py` still has a legacy memory context and
  episodic/procedural completion hooks. These are not the Preference Memory
  vertical slice and must not become a second current-state truth.

## Existing Database

### PostgreSQL table

The table is declared in both:

- `packages/agent_core/greenbook_agent_core/db/migrations/008_context_durable_memory.sql`
- `memory/repository.py::PostgresMemoryRepository.ensure_storage()`

Existing `agent_memories` columns are:

`memory_id`, `user_id`, `conversation_id`, `task_id`, `memory_type`, `content`,
`structured_metadata`, `importance`, `confidence`, `source_type`, `source_id`,
`created_at`, `updated_at`, `last_accessed_at`, `access_count`, and `expires_at`.

Existing indexes cover `(user_id, memory_type, updated_at DESC)` and
`(user_id, task_id, updated_at DESC)`.

### Gaps for this slice

- There is no `tenant_id` column or tenant-aware query predicate.
- There is no lifecycle `status` column for `active`, `inactive`, or
  `superseded` records.
- `conversation_id` is the current provenance field; the requested
  `source_conversation_id` name is not present as a distinct contract field.
  The slice should expose that meaning without creating a second conversation
  truth.
- There is no database-level uniqueness/upsert key for one preference identity
  within a user/tenant scope.
- PostgreSQL search filters keywords in Python after a limited SQL result set;
  it is lexical only and has no memory vector database.

## Existing Runtime Integration

The current production startup path is:

```text
PostgreSQL runtime
  -> PostgresMemoryRepository (durable shadow)
  -> MemoryManager (default InMemoryMemoryRepository primary)
  -> MemoryRetriever (durable repository for recall)
  -> ContextBuilder / ContextAssembler
```

The legacy runtime also calls `RuntimeAgentService._recall_memories()` and
records episodic/procedural outcomes. Those calls populate a legacy runtime
context and are not the canonical Preference Memory decision input.

`ContextBuilder` can recall when its caller leaves `memory_recall` unset and
provides a structured command, but the two canonical production callers
explicitly pass `False` or use the `ContextAssembler` default. Existing tests
therefore prove an opt-in cross-conversation recall path, not default Agent
behavior.

The existing read API (`/memory/settings`, `/memory/records`) is read-only for
the user-facing surface. It returns default-disabled settings and queries by
`user_id` without a tenant predicate.

## Missing Integration

1. A canonical tenant-aware Preference Memory record and repository contract.
2. A completed-turn extractor that returns structured classification output,
   writes only durable user preferences, and rejects one-off task requests.
3. A single durable write owner for Preference Memory rather than an
   in-process primary plus fire-and-forget durable shadow.
4. A production feature flag with disabled-mode behavior identical to the
   current path.
5. A `PreferenceRetriever` that filters to preference records, applies the
   user/tenant boundary, and caps default recall at five records.
6. A ContextBuilder integration that passes bounded preference evidence to the
   Interpreter while never passing Memory as Task, Execution, Resource, or
   target identity truth.
7. Lifecycle update/conflict/invalidating behavior using `status` and
   confidence, with historical records retained.
8. Restart, cross-conversation, user-isolation, tenant-isolation, and
   disabled-feature tests.

## Reuse Plan

- Extend `MemoryRecord`/`MemoryQuery` and the existing repository protocol
  instead of adding another profile/fact/summary store.
- Use a forward-only migration after migration 008. Keep migration 008
  compatible with existing installations; do not rewrite the baseline tag.
- Keep `agent_memories` as the canonical durable table. Add only the minimal
  tenant/lifecycle/provenance fields needed by the preference slice.
- Reuse `MemoryManager` policy and idempotent record identity, but make the
  Preference Memory manager path durable-first and explicit about scope.
- Add a dedicated structured `PreferenceMemoryExtractor` beside the existing
  procedural extractor. Its MVP can be deterministic, with a Pydantic result
  contract that is also suitable for a future LLM structured-output adapter.
- Reuse `ContextSnapshot` and `ContextBuilder`; add a bounded
  `PreferenceRetriever` projection and provider-facing preference evidence.
- Make recall opt-in through `MEMORY_ENABLED` in Phase 4. The disabled path
  must preserve the existing empty-memory behavior and must not touch records.
- Do not modify ActionLoop, TaskManager, ToolRuntime, Durable Runtime,
  ActionObservation truth, MCP, Hybrid Search/RAG, or Java business truth.

## Baseline Risks

- Existing records have no tenant value. The migration must fail closed for
  new scoped writes and must not expose legacy unscoped data through the new
  PreferenceRetriever unless an explicit compatibility policy is chosen.
- `MemoryType.PREFERENCE` currently serializes as `SEMANTIC`; changing that
  enum value would break existing records and tests.
- A durable-first change must preserve the in-memory test profile while
  preventing production retrieval from reading a divergent process-local
  store.
- The current schema DDL is duplicated between migration and repository
  startup; the new migration is the forward compatibility boundary, while
  repository `ensure_storage()` must remain idempotent for focused tests.
