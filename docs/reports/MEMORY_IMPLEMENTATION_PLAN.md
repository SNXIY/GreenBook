# Memory Implementation Plan

## Current Memory Capability

The repository already contains a Memory domain with `MemoryRecord`, in-memory
and PostgreSQL repositories, write policy, retrieval, preference projection,
episodic/procedural helpers, and durable schema support. The current audit
status is:

`MEMORY_CAPABILITY_EXISTS_BUT_RUNTIME_INTEGRATION_PARTIAL`

The canonical turn path has context fields reserved for memory, but long-term
recall is not enabled by default and the recalled preference evidence is not
yet a reliable Interpreter input. The existing PostgreSQL memory schema also
needs a tenant-scoped preference contract for this vertical slice.

## Phase Scope

This implementation will deliver the smallest verifiable long-term Memory
vertical slice:

1. audit and reuse existing Memory components;
2. persist tenant/user-isolated `preference` records with conversation
   provenance;
3. extract durable preferences from completed Conversation input using a
   structured result and an explicit long-term classification decision;
4. retrieve at most five relevant preferences for a new canonical turn and
   expose them as bounded context evidence to the Interpreter;
5. support lifecycle updates, historical conflicts, inactive/superseded
   records, and an explicit `MEMORY_ENABLED` feature flag;
6. validate restart visibility, isolation, disabled-mode compatibility, and
   the existing runtime regression suite.

## Explicitly Not Implemented

This phase does not implement or redesign:

- episodic Memory as a complete product;
- procedural Memory as a complete product;
- a Memory Agent;
- a Memory vector database, graph Memory, or new embedding pipeline;
- RAG integration or changes to the existing Hybrid Search/RAG boundary;
- large-scale prompt refactoring;
- ActionLoop, TaskManager, ToolRuntime, Durable Runtime, MCP, or Java
  business-facade changes;
- a second Conversation, Task, Execution, Resource, or current-state truth.

Memory remains cross-Conversation reusable evidence only. Current task,
execution, draft, schedule, resource identity, and live status continue to be
owned by their existing canonical domains.

## Acceptance Shape

The final handoff must document the data model, extraction and retrieval
flows, context boundary, tenant/user isolation, tests, limitations, and a
resume point. Every phase must leave a report/checkpoint, a non-interactive
commit, and a pushed branch state.
