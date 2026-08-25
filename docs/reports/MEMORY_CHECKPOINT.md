# Memory Checkpoint

Current Phase: Phase 3 complete

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

Git Commit: Phase 3 extraction commit containing this report and checkpoint
(see `git log --oneline` for the exact hash).

Tests: 46 focused Memory/storage/extraction/repository/retriever/context tests
passed;
`uv run ruff check packages/agent_core/greenbook_agent_core/memory tests/unit/test_preference_memory_storage.py`
passed; compileall and migration splitter tests passed.

Remaining: Phase 4 PreferenceRetriever and canonical Context/Interpreter
integration with `MEMORY_ENABLED`.

Resume From: Add tenant-aware preference retrieval capped at five records,
project only bounded preference evidence to the Interpreter, and keep the
disabled path unchanged.
