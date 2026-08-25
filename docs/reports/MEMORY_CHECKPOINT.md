# Memory Checkpoint

Current Phase: Phase 5 complete

Completed:

- Phase 0 protected the RAG checkpoint, committed the existing Memory audit,
  added the implementation plan, and pushed the branch.
- Phase 1 completed a read-only audit of the Memory domain, repositories,
  migration/schema, preference provider, ContextBuilder, ContextAssembler,
  Interpreter projection, and production wiring.
- Added `MEMORY_IMPLEMENTATION_BASELINE.md` with the reuse plan and missing
  integration boundaries.
- Confirmed the existing focused Memory/context suite passes before code
  changes.
- Added tenant-aware `MemoryRecord`/`MemoryQuery` scope, lifecycle status, and
  explicit source-conversation provenance compatibility.
- Added forward migration `009_preference_memory_vertical_slice.sql` and
  idempotent PostgreSQL storage/index support.
- Added scoped CRUD, user isolation, tenant isolation, provenance, and
  PostgreSQL parameter contract tests.
- Added structured `PreferenceExtraction`, deterministic
  `PreferenceMemoryExtractor`, and `PreferenceMemoryService` contracts.
- Added high-confidence extraction for title style, technical depth, concise
  replies, and technology stack preferences; transient tasks and invalid input
  return `SKIP`.
- Wired completed-turn extraction into the API result projection with
  deterministic source-id idempotency. Memory extraction failures cannot break
  turn convergence.
- Added tenant-scoped `PreferenceRetriever` with active-status filtering,
  deterministic relevance ranking, default top-five recall, fail-closed tenant
  scope, and no-touch-by-default behavior.
- Integrated preference recall into the canonical ContextBuilder,
  ContextAssembler, and ConversationRuntimeAdapter. The current user input is
  now a retrieval query before Interpreter/Agent decisions.
- Projected bounded preference evidence into the Interpreter-facing context
  while stripping memory/conversation identifiers from that provider view.
- Added `MEMORY_ENABLED` wiring for retrieval and completed-turn extraction;
  the disabled path leaves Conversation/Task/Execution context intact.
- Added cross-conversation retrieval, user/tenant isolation, top-five,
  provider projection, and disabled-feature tests.
- Added preference lifecycle convergence: same key/value merges into one
  active record with confidence/evidence updates; a new value supersedes the
  previous active value while retaining its historical row.
- Added scoped `deactivate` and `supersede` operations, replacement metadata,
  and retry protection so superseded/inactive source events cannot resurrect
  old preferences.
- Changed the legacy preference provider to read without touching access
  metadata, and exposed the `MEMORY_ENABLED` state through the settings
  projection. Preference type input now accepts the literal `preference`
  spelling while retaining the existing SEMANTIC storage compatibility.
- Added lifecycle, conflict, confidence, inactive, scope, disabled-service,
  and no-touch provider tests.

Git Commit: Phase 5 lifecycle commit containing this report and checkpoint
(see `git log --oneline` for the exact hash).

Tests: 56 focused Memory/storage/retrieval/extraction/lifecycle/context tests
  passed; compileall passed; focused Memory/lifecycle ruff checks passed.
  Pytest emitted only the existing cache-path permission warning.

Remaining: Phase 6 final tests, handoff, compile/lint audit, and protected
  boundary confirmation.

Resume From: Inspect `git status`, `git log`, and this checkpoint. Continue
  with the final handoff and full acceptance audit without changing ActionLoop,
  Durable Runtime, MCP, or RAG.
