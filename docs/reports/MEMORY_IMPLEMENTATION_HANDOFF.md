# Memory Implementation

## Architecture

This slice adds Preference Memory as a bounded, optional projection over the
existing canonical runtime. Conversation remains the source of current-turn
messages and continuity; Task/Objectives remain the source of user goals and
task lifecycle; Execution/Observation remain the source of verified runtime
facts; Resource/Artifact projections remain the source of business identity.
Memory stores only reusable cross-Conversation preference evidence.

The production path is:

```text
completed Conversation turn
        -> structured PreferenceMemoryExtractor
        -> scoped MemoryManager write
new user message
        -> PreferenceRetriever (active, user + tenant scoped, top five)
        -> ContextBuilder / ContextAssembler
        -> sanitized Interpreter/Agent context
```

No ActionLoop, TaskManager, ToolRuntime, MCP boundary, RAG pipeline, or Java
business facade was refactored for this slice.

## Data Model

Preference records reuse `agent_memories` and the existing Memory repository.
The vertical slice adds or normalizes:

- `memory_id`
- `user_id`
- `tenant_id`
- `memory_type` (`PREFERENCE` is retained as the existing `SEMANTIC` storage
  value for compatibility; literal `preference` input is normalized)
- `content`
- `confidence`
- `source_conversation_id` (one provenance value, compatible with the existing
  `conversation_id` field)
- `status` (`active`, `inactive`, or `superseded`)
- `created_at` / `updated_at`

The migration is idempotent and adds the scope/type/status/updated index. The
repository always filters durable search by both `user_id` and `tenant_id`.

## Extraction Flow

Extraction runs only at the completed-turn boundary. It uses a structured
`PreferenceExtraction` contract with memory type, normalized key/value,
confidence, long-term classification, decision, and reason. The MVP
recognizes a small high-confidence set: title style, technical depth, concise
replies, and technology stack. One-off or invalid requests return `SKIP` and
are not written.

The current extractor is deterministic. Its structured contract is compatible
with a future LLM structured-output implementation without making every turn
depend on an LLM memory write.

## Retrieval Flow

`PreferenceRetriever` requires a non-empty authenticated user and tenant,
queries only active preferences, ranks lexical overlap plus confidence,
importance, and recency, and clamps recall to five records. It does not touch
access metadata by default. Missing tenant scope fails closed.

Lifecycle convergence keeps one active value per preference key: repeated
same-value evidence merges confidence/evidence and provenance; a new value
supersedes the previous active row while retaining that row for history.

## Context Integration

`ContextAssembler` forwards the current user input as `target_query` to the
canonical `ContextBuilder`. The production API wires the preference retriever
into both the conversation adapter and the Turn context assembler. The
Interpreter-facing projection exposes bounded `user_preferences` and
`recalled_memories` evidence, recursively removing memory, Conversation, and
other internal identifiers before provider serialization.

The default context memory budget is five. Existing callers can explicitly
disable recall, and the production `MEMORY_ENABLED` flag disables extraction,
preference-provider reads, retrieval, and memory touch while leaving the
Conversation/Task/Execution context path intact.

## Isolation

Every new durable write carries `user_id`, `tenant_id`, and source Conversation
provenance. In-memory CRUD, PostgreSQL search/get/touch/delete, lifecycle
changes, API record listing, and retrieval are scope-aware. Wrong-user or
wrong-tenant lifecycle operations return no record and do not mutate the
source row.

## Tests

Focused Memory/storage/retrieval/extraction/lifecycle/context validation:

- 56 passed
- focused Memory/lifecycle Ruff checks passed
- `compileall` passed for `packages`, `apps`, and `services`

Existing unit/integration runtime suite:

- 1529 passed
- 1 skipped
- 6 failures in pre-existing unrelated areas: objective completion owner
  status, MCP catalog count/activity mapping, and Turn target ambiguity
- 2 existing pytest cache-path permission warnings

Full repository Ruff reported 174 existing lint findings. The changed-file
check reports only pre-existing import-order/legacy lint findings in the
large API/context/runtime modules; the new Memory/lifecycle files pass their
focused check. No changed file belongs to the ActionLoop, Durable Runtime,
MCP, RAG, or Java business-facade boundary.

## Limitations

- This is Preference Memory only; Episodic full implementation, Procedural
  Memory expansion, Memory Agent, vector database, graph memory, and RAG
  integration remain out of scope.
- Extraction is deterministic and intentionally conservative; it does not yet
  infer arbitrary user preferences.
- PostgreSQL production wiring retains the existing MemoryManager in-process
  primary plus durable shadow arrangement. Durable shadow writes are scheduled
  asynchronously by the existing manager, so an operator needing a strict
  write acknowledgement should add an explicit awaited persistence boundary.
- Ranking is lexical and bounded; semantic/vector retrieval is not part of this
  vertical slice.

## Next Steps

1. Add an awaited durable-write boundary for preference extraction if strict
   post-response durability is required.
2. Expand the structured extraction vocabulary only with labeled tests and
   explicit write policy thresholds.
3. Add retention/administrative UX for inactive and superseded history.
4. Revisit existing repository-wide Ruff findings and the unrelated six
   runtime test failures in their owning phases.

## Commits

- `32e0caa` — `docs: add memory architecture audit`
- `cf0c6aa` — `checkpoint: complete memory phase 0`
- `1bd2761` — `docs: record memory implementation baseline`
- `8b97f27` — `feat: add preference memory storage`
- `178335e` — `feat: add preference memory extraction`
- `d505c61` — `feat: integrate preference memory retrieval`
- `481c4c5` — `test: validate memory lifecycle`
