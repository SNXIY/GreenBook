# Memory Checkpoint

Current Phase: Phase 2 complete

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

Git Commit: Phase 2 storage commit containing this report and checkpoint (see
`git log --oneline` for the exact hash).

Tests: 39 focused Memory/storage/repository/retriever/context tests passed;
`uv run ruff check packages/agent_core/greenbook_agent_core/memory tests/unit/test_preference_memory_storage.py`
passed; migration splitter tests passed.

Remaining: Phase 3 structured Preference Memory extraction from completed
Conversation turns.

Resume From: Add a structured `PreferenceMemoryExtractor` beside the existing
procedural extractor, with long-term classification and no one-off task
writes.
