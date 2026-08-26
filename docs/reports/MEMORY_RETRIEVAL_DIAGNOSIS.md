# Memory Retrieval Diagnosis

Checkpoint: `4ef8240` (`test: add memory evaluation baseline`).

This is a read-only diagnosis of the MemoryRetriever, PreferenceRetriever,
ContextBuilder, and current memory scoring path. No production code was
changed while establishing this diagnosis.

## FIRST_BAD_STATE

**`candidate scoring -> selection` is the first bad state.** A candidate with
zero lexical relevance to the current user request can still be selected
because confidence, importance, recency, and relation bonuses produce a
positive score. The selected candidate then reaches ContextBuilder, where it
is compacted and bounded but not relevance-filtered.

## Current Flow

```text
user request / target_query
  -> ContextBuilder.build()
  -> ContextBuilder._recall()
  -> PreferenceRetriever.retrieve() or MemoryRetriever.retrieve()
  -> candidate scoring and top-N selection
  -> ContextBuilder bounded compaction (max 5)
  -> recalled_memories / user_preferences
```

`target_query` is passed through the canonical ContextBuilder path. The
ContextBuilder cap limits quantity and field size, but it is not a relevance
gate and has no explicit no-memory result policy.

## Findings

### PreferenceRetriever

- Searches all active, same-user and same-tenant preference records without a
  repository keyword filter.
- Scores lexical overlap from content, `preference_type`, and value at `2.0`
  per matching term.
- Adds positive confidence (`0.8`), importance (`0.3`), and recency (`0.1`)
  components even when lexical overlap is zero.
- Returns the top bounded records without requiring a positive relevance
  score. An unrelated request therefore receives the same-scope preferences.

### MemoryRetriever

- Builds lexical terms from command, goal, context, and `target_query`.
- Scores overlap plus conversation/task relation, importance, confidence, and
  recency.
- Selects candidates when `_score(...) > 0`; this is not a relevance
  threshold because non-relevance components can make the score positive.
- Falls back to ranked candidates when the request has no terms, which is a
  full candidate injection path for an empty query.

### ContextBuilder

- Receives the current request and forwards it to the configured memory
  provider.
- Treats the provider result as already selected evidence.
- `_compact_memory()` only projects bounded fields; it does not inspect a
  relevance score, enforce a confidence threshold, or return an explicit
  no-memory result.
- The final memory projection is capped at five records, so the current issue
  is bounded but irrelevant injection rather than an unbounded payload.

## Baseline Evidence

The baseline retrieval fixture has 100 cases: 90 targeted requests and 10
irrelevant requests.

| Metric | Baseline |
|---|---:|
| Recall@1/3/5 | 1.0000 / 1.0000 / 1.0000 |
| Precision@1/3/5 | 1.0000 / 0.3333 / 0.2000 |
| Irrelevant-query memory-return rate | 100.00% |
| Harmful injection candidate rate | 0.2000 |

The first quality failure is therefore not candidate recall. It is the lack
of a post-scoring relevance decision that can produce `[]` when no candidate
clears a meaningful threshold.

## Scope Check

This diagnosis does not propose changes to storage schema, extraction,
Task/Objectives, ActionLoop, MCP, or RAG. The next implementation phase is
limited to a small retrieval-side relevance gate and its evaluation evidence.
