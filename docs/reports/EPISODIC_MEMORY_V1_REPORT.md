# EPISODIC_MEMORY_V1_REPORT

## Verdict

`EPISODIC_MEMORY_V1_PASS`

This is a deliberately conservative vertical slice for one verified content-publication history. It does not introduce a new table, vector collection, repository, retriever, or relevance policy. Episodic implementation files remain uncommitted for acceptance.

## 1. Design checkpoint

- Design checkpoint commit: `0b9ad39a4dc9b7f9fcc06a9e353dcc2715fb7774`
- Branch: `feature/hybrid-search-rag`
- The design checkpoint was committed and pushed before implementation.

## 2. Blocker audit

All implementation blockers are resolved for this V1 scope.

### Objective join contract

The projector uses only the persisted, exact join:

```text
ActionObservation.execution_id
  -> PlanExecution.execution_id
  -> PlanExecution.objective_id
  -> Task.task_id
  -> exactly one terminal Objective.objective_id
```

`active_task_id`, `active_objective`, recent execution, and callback payload guesses are not fallbacks. Missing or mismatched `objective_id` produces no candidate. This is covered by `test_projector_rejects_missing_objective_correlation_without_fallback`.

### Legacy EPISODIC isolation

Canonical V1 records carry `structured_metadata.memory_contract=EPISODIC_V1`. Strict production retrieval filters EPISODIC records by this contract and excludes rows without it. Existing legacy rows therefore cannot enter the V1 model-facing path until an explicit migration/quarantine decision is made. No second table was added.

### PREFERENCE / SEMANTIC compatibility

The existing `MemoryType.PREFERENCE` storage alias (`SEMANTIC`) remains unchanged. Retrieval uses explicit memory-type and contract filters; the preference extractor and lifecycle path do not process `EPISODIC` records. The separation is covered by the legacy-isolation and preference-separation test.

## 3. Canonical write chain

```text
terminal execution
  -> completion projection
  -> persisted terminal ActionObservation
  -> exact execution/objective/task join
  -> terminal Objective + verified POST business outcome
  -> EpisodeCandidateBuilder
  -> WorthRememberingPolicy
  -> EpisodicMemoryService
  -> MemoryManager.remember
  -> canonical Memory Repository
```

The `ActionObservationWriter` callback runs only after the observation is saved. Candidate/write failures are isolated from runtime completion and do not change Task, Execution, Observation, reconciliation, or Java business truth.

The projector accepts only the V1 source contract:

- terminal `ActionObservation` and terminal `Objective`;
- exact persisted objective correlation;
- verified POST resource read-back;
- explicit user revision evidence for both title and publication time.

It rejects LLM intermediate text, untrusted ToolResult/runtime results, RUNNING or pending execution, `RESULT_UNKNOWN`, retryable failure, waiting external, pending approval, and unverified business outcomes.

## 4. EpisodeCandidate and policy

`EpisodeCandidate` contains only:

```text
user_id, tenant_id, category, summary, outcome,
occurred_at, confidence, provenance, source_type
```

The only V1 category is `CONTENT_PUBLICATION_WORKFLOW`, with outcome `VERIFIED_PUBLICATION_AFTER_USER_REVISION`. Runtime IDs are restricted to internal provenance; they are not part of the summary or model-facing context.

`WorthRememberingPolicy` has exactly three decisions:

| Decision | V1 behavior |
|---|---|
| `KEEP` | Verified publication after the user actively changed both title and publication time, with sufficient confidence and reusable historical meaning. |
| `DROP` | Ordinary publication, ordinary CRUD, retry/reconciliation, scheduler state, one-off tool success, or any non-reusable runtime event. |
| `UNKNOWN` | Incomplete, ambiguous, or insufficiently corroborated evidence. `UNKNOWN` is write-disabled and is treated as `DROP` in V1. |

The stored summary describes one past experience. It does not infer “the user usually changes titles” or any other Preference from one Episode.

## 5. Idempotency and storage

The implementation reuses `MemoryRecord` and `agent_memories` with `memory_type=EPISODIC`. No episode table, vector collection, or second repository was added.

The write key is deterministic:

```text
sha256(EPISODIC_V1 | user_id | tenant_id | source_type | observation_id)
```

It becomes the canonical `MemoryRecord.memory_id`. Replaying the same verified source therefore resolves to the same record through `MemoryManager.remember`; the focused replay test confirms one stored Episode after two writes. User and tenant scope are mandatory on both write and read. Lifecycle status is `ACTIVE`, with normal Memory lifecycle fields preserved.

## 6. Retrieval integration

Production API and worker composition now use one canonical `MemoryRetriever` configured for active, tenant-scoped Preference and V1 EPISODIC records. EPISODIC retrieval follows:

```text
MemoryRetriever
  -> one MemoryRelevanceGate
  -> bounded ContextBuilder memory context
  -> Interpreter projection
```

There is no `EpisodicRetriever -> Interpreter` bypass and no second ranking or threshold policy. No-match returns zero memories. Context marks an Episode as `relevant_past_experience` and keeps Preference as `preference`; internal provenance is omitted from the model-facing compact projection.

The existing `ConversationRuntimeAdapter` PreferenceRetriever fallback is compatibility-only for callers that do not inject the production retriever. The API/worker production path injects the canonical combined retriever.

## 7. One vertical slice

The selected scenario is:

```text
technical content publication
  -> user actively revises title
  -> user actively adjusts publication time
  -> POST is verified as published by business/resource read-back
  -> one historical Episode
```

This is Episodic rather than Preference because it records one verified occurrence and its outcome. A Preference requires explicit user instruction or stronger repeated evidence and is not generated by this path.

## 8. Focused benchmark metrics

These are small deterministic fixtures, not the full Memory Evaluation matrix.

| Metric | Result | Fixture interpretation |
|---|---:|---|
| Candidate Precision | 1.00 | The only emitted candidate was eligible: 1/1. |
| Write Precision | 1.00 | The only write was eligible: 1/1. |
| Unnecessary Episode Rate | 0.00 | Ordinary/ambiguous/non-terminal fixtures produced no Episode. |
| Relevant Retrieval Recall | 1.00 | The relevant Episode was found from another conversation: 1/1. |
| Relevant Retrieval Precision | 1.00 | The returned record was the relevant scoped Episode: 1/1. |
| No-match false return rate | 0.00 | An unrelated query returned zero Episode: 0/1 false returns. |
| User leakage | 0 | No other-user Episode was returned. |
| Tenant leakage | 0 | No other-tenant Episode was returned. |
| Duplicate write rate | 0.00 | Two replays produced one record and no duplicate. |

## 9. Focused tests

Latest targeted run:

```text
uv run pytest tests/unit/test_agent_memory.py tests/unit/test_memory_lifecycle.py tests/unit/test_memory_repository.py tests/unit/test_memory_retriever.py tests/unit/test_preference_memory_extractor.py tests/unit/test_preference_memory_retrieval.py tests/unit/test_preference_memory_storage.py tests/unit/test_memory_runtime_convergence.py tests/unit/test_episodic_memory_v1.py tests/integration/test_context_memory_runtime.py tests/unit/test_context_builder.py tests/unit/test_derived_conversation_context.py tests/unit/test_interpreter_context_boundary.py tests/unit/test_action_observation_continuation.py tests/integration/test_observation_continuation_e2e.py -q
```

Result: `152 passed`.

The V1 test module covers verified write, ordinary/unknown/failed/waiting rejection, exact objective join, replay idempotency, cross-conversation recall, user/tenant isolation, legacy isolation, no-match filtering, Preference separation, context labeling, feature flag OFF, post-save callback ordering, and the conservative benchmark fixture. The run emitted one pytest cache permission warning; it did not affect test results.

Additional checks:

- focused changed-core `ruff check`: passed;
- API and worker imports: passed;
- changed-module compile check: passed;
- `git diff --check`: passed.

No L1/L2/L3 suite, Search/RAG matrix, or expensive full evaluation was run.

## 10. Protected boundaries

The V1 implementation does not add Episodic semantics to ActionLoop, Durable Runtime, Task/Objectives lifecycle, RESULT_UNKNOWN/reconciliation, MCP, Search, RAG, or Java business truth. Memory remains lower priority than the current explicit request, verified current runtime/business truth, and conversation context.

Legacy `MemoryManager.remember_execution` and `remember_pattern` compatibility helpers remain in place. They are not production Episodic writers in this path and should be classified as `TEST_ONLY` / `DEPRECATE_CANDIDATE`, not broadly deleted in this checkpoint.

## 11. Production files changed

- `apps/agent_api/greenbook_agent_api/main.py` — inject canonical retriever and post-observation projector.
- `apps/agent_worker/greenbook_agent_worker/main.py` — same worker wiring and feature flag.
- `packages/agent_core/greenbook_agent_core/context/builder.py` — distinguish Preference from relevant past experience and hide internal provenance.
- `packages/agent_core/greenbook_agent_core/execution/action_observation.py` — post-save projection hook; runtime write semantics remain unchanged.
- `packages/agent_core/greenbook_agent_core/memory/episodic.py` — candidate builder, policy, service, and exact-join projector.
- `packages/agent_core/greenbook_agent_core/memory/__init__.py` — exports V1 components.
- `packages/agent_core/greenbook_agent_core/memory/relevance.py` — shared gate tokenization support for CJK and stopwords.
- `packages/agent_core/greenbook_agent_core/memory/repository.py` — existing metadata filter support; no schema change.
- `packages/agent_core/greenbook_agent_core/memory/retriever.py` — canonical scoped multi-type retrieval and legacy contract filtering.

Focused test file:

- `tests/unit/test_episodic_memory_v1.py`

This report is intentionally uncommitted with the implementation.

## 12. Blockers and next recommendation

There is no blocker for acceptance of this V1 slice. Known limits are intentional: only the verified content-publication scenario is eligible, legacy EPISODIC rows remain excluded until separately classified, and no consolidation into Preference/Procedural/Semantic memory is attempted.

Next step: review and accept the dirty implementation diff, then commit this V1 implementation as a separate checkpoint. Do not begin Semantic or Procedural Memory until the V1 contract, legacy quarantine decision, and production benchmark are accepted.

## 13. Dirty files at report generation

Expected dirty scope:

- the nine production files listed above;
- `tests/unit/test_episodic_memory_v1.py`;
- this report: `docs/reports/EPISODIC_MEMORY_V1_REPORT.md`.

No commit or push was performed for the Episodic implementation.

`EPISODIC_MEMORY_V1_PASS`
