# SEMANTIC_MEMORY_V1_REPORT

## Verdict

`SEMANTIC_MEMORY_V1_PASS`

Semantic V1 is intentionally narrow: it admits only explicit, stable,
non-sensitive user facts for the two supported predicates below. The
implementation remains uncommitted after the design checkpoint, as requested.

## Checkpoints

- Starting checkpoint: `24d2fc6a484fc8651288584f546ec17697dc3d4e`
- Design checkpoint: `7f8cec00d975e9939decf7b2e5e488043ffbccf8`
- Branch: `feature/hybrid-search-rag`

The design report was committed and pushed before implementation. No Semantic
V1 implementation commit was created.

## Current Schema Compatibility

Semantic V1 reuses the existing `MemoryRecord` and `agent_memories`
repository. It uses the existing persisted `SEMANTIC` enum value plus explicit
metadata:

- `memory_contract = SEMANTIC_V1`
- `memory_role = stable_fact`
- `subject`, `predicate`, `object`, and `normalized_fact`
- `provenance`, `source_type`, and `observed_at`
- existing `ACTIVE` / `SUPERSEDED` lifecycle fields

No semantic table, vector collection, second repository, second retriever, or
second relevance policy was added. `PREFERENCE` and `SEMANTIC` remain a
storage-enum compatibility alias; metadata contracts keep their projections
isolated.

## Preference / Semantic / Episodic / Current State Boundary

| Kind | Meaning | V1 treatment |
| --- | --- | --- |
| Preference | How the user wants the assistant or content to behave | Existing Preference extraction and metadata only; never emitted by the Semantic builder |
| Semantic | A stable, explicit fact about the user or long-term context | Admitted only through `EXPLICIT_USER_STATEMENT` |
| Episodic | A reusable summary of one verified past experience | Existing Episodic V1 contract; never used as a Semantic source |
| Current State | Task, Objective, Execution, Resource, approval, or reconciliation truth for the active runtime | Remains in the runtime repositories; never written by Semantic V1 |

Examples:

- “I like Java content” is Preference, not `occupation_domain`.
- “I am a Java backend developer” is Semantic, not a Java Preference.
- “I published a Java post yesterday” is history/current conversation evidence,
  not a Semantic fact.
- A task, execution, resource, approval, runtime ID, or unverified result is
  never a Semantic fact.

## Canonical Semantic Write Chain

```text
completed-turn user text
        -> SemanticCandidateBuilder
        -> SemanticAdmissionPolicy
        -> predicate conflict resolver
        -> MemoryManager.remember_semantic
        -> existing canonical MemoryRepository
```

The route hook reads only the authenticated `user_id` and `tenant_id` and runs
after a `COMPLETED` turn. It does not derive facts from ActionLoop output,
ToolRuntime output, Task/Objectives, or Java business state. The builder never
writes storage directly, and the ContextBuilder never writes Semantic memory.

## Production Files Changed

The dirty Semantic V1 implementation changes are limited to:

- `apps/agent_api/greenbook_agent_api/api/routes.py`
- `apps/agent_api/greenbook_agent_api/main.py`
- `packages/agent_core/greenbook_agent_core/context/builder.py`
- `packages/agent_core/greenbook_agent_core/memory/__init__.py`
- `packages/agent_core/greenbook_agent_core/memory/manager.py`
- `packages/agent_core/greenbook_agent_core/memory/preference_retriever.py`
- `packages/agent_core/greenbook_agent_core/memory/repository.py`
- `packages/agent_core/greenbook_agent_core/memory/retriever.py`
- `packages/agent_core/greenbook_agent_core/memory/semantic.py`

No ActionLoop, Durable Runtime, Task/Objectives lifecycle, MCP, Search, RAG,
Java backend, Preference semantics, or Episodic semantics were changed.

## Supported Predicates

The first vertical slice supports only:

| Predicate | Canonical values | Explicit example |
| --- | --- | --- |
| `occupation_domain` | `java_backend` | “I am a Java backend developer.” |
| `learning_focus` | `java`, `ai_agent` | “I am now learning Agent.” |

The implementation is a deterministic, bounded parser rather than a general
knowledge-graph extractor. A combined statement such as “我是 Java 后端开发，
现在在学习 Agent” produces exactly two candidates.

## Candidate Contract and Provenance

`SemanticCandidate` contains:

- `user_id`, `tenant_id`
- `subject`, `predicate`, `object`
- `normalized_fact`
- `confidence`
- `source_type`
- `provenance`
- `observed_at`

Accepted provenance includes `source = explicit_user_statement`,
`author_role = user`, the `SEMANTIC_V1` contract/version, a source hash, and a
source reference. Runtime identifiers are not part of the fact text; if a
runtime identity or UUID is present in the proposed fact, it is rejected.

## Admission Policy

The policy has `KEEP`, `DROP`, and `UNKNOWN` outcomes. `UNKNOWN` is
write-disabled and has the effective result `DROP`.

`KEEP` requires:

- explicit user self-statement provenance;
- a supported predicate and the `user` subject;
- confidence at least `0.85`;
- stable, cross-session value;
- no current-task/runtime identity or sensitive content.

`DROP` or `UNKNOWN` covers LLM profile inference, tool/topic inference, single
Episode inference, transient statements, current operation state, ordinary
CRUD, resource IDs, and unsupported predicates. The goal is conservative
under-admission: do not guess a user fact.

## Supersede Strategy

Semantic identity is scoped by:

```text
tenant_id + user_id + subject + predicate
```

For one identity:

- the same object reuses the deterministic fact `memory_id` and merges evidence;
- a new object supersedes all other active values and becomes `ACTIVE`;
- a different predicate is not superseded;
- repeated writes do not create duplicate active facts.

The in-process canonical manager serializes the operation with a lock. The
PostgreSQL repository uses a transaction and a predicate-scoped advisory lock,
then locks active rows before superseding and upserting. This keeps the
`old ACTIVE -> SUPERSEDED` and `new -> ACTIVE` transition in one repository
operation for the durable path.

## Retrieval Integration

Production uses one canonical path:

```text
MemoryRetriever
        -> MemoryRelevanceGate
        -> bounded ContextBuilder
        -> Interpreter provider projection
```

The existing retriever is configured with `semantic_contract=SEMANTIC_V1`.
Because Preference and Semantic share the persisted enum value, the retriever
classifies Preference metadata and Semantic contract metadata in the same
candidate/gate path. Strict Semantic reads can set
`include_preference_alias=False`; no new retriever or gate is introduced.

ContextBuilder labels a Semantic record as `memory_role = relevant_fact`, while
existing Preference and Episodic records remain `preference` and
`relevant_past_experience`. Semantic facts are bounded in the existing
`recalled_memories` projection, are not copied into `user_preferences`, and an
irrelevant query returns zero memory. The existing compatibility
`PreferenceRetriever` now filters by Preference metadata so the enum alias
cannot render Semantic facts as Preferences.

Legacy Semantic rows without the V1 contract/role are excluded from canonical
V1 retrieval and cannot enter through the V1 admission path.

## Focused Evaluation Metrics

The small benchmark and focused tests produced these fixture-level results:

| Metric | Result | Scope |
| --- | ---: | --- |
| Fact Extraction Precision | 1.00 | 2 eligible facts / 2 emitted facts |
| Fact Extraction Recall | 1.00 | 2 / 2 target facts in the explicit combined statement |
| Unsupported Inference Rate | 0.00 | 0 writes across 4 negative inputs |
| Duplicate Active Fact Rate | 0.00 | repeated facts retain one active record per predicate |
| Supersede Correctness | 1.00 | explicit learning update produces one active new value and a superseded old value |
| Preference/Semantic Confusion Rate | 0.00 | isolated Preference and Semantic fixtures |
| Episode/Semantic Confusion Rate | 0.00 | history fixture produces no Semantic record |
| Relevant Retrieval Recall | 1.00 | relevant `Agent` fact recalled across conversations |
| Relevant Retrieval Precision | 1.00 | no unrelated Semantic fact selected in the relevant fixture |
| No-match False Return Rate | 0.00 | unrelated recent-post query returns no Semantic memory |
| Cross-user leakage | 0 | scoped retrieval fixture |
| Cross-tenant leakage | 0 | scoped retrieval fixture |

## Focused Tests

Passed:

- `tests/unit/test_semantic_memory_v1.py`: **16 passed**
- Semantic V1 plus Memory Runtime convergence, Preference retrieval/extraction,
  MemoryRetriever, repository, lifecycle, agent-memory, ContextBuilder, and
  Interpreter-boundary tests: **82 passed**
- Ruff on the changed Semantic memory modules and new focused test: **passed**
- `git diff --check`: **passed**
- Python compile check for the changed modules: **passed**

The test runner emitted one non-failing warning because the local environment
could not write `.pytest_cache`; it did not affect test execution. No L1/L2/L3,
RAG/Search matrix, Java E2E, or full expensive evaluation was run.

## Blockers and Limitations

No V1 blocker remains for the requested vertical slice.

Known limitations:

- only two predicates and a narrow explicit vocabulary are supported;
- the focused run uses the deterministic in-memory repository; the durable
  PostgreSQL transaction path is implemented but was not exercised against a
  live database in this run;
- legacy private helpers remain for compatibility/deprecation review, but no
  production call path reactivates them;
- Semantic writes are intentionally admitted only at the completed-turn hook;
  no broader automatic profile inference is enabled.

## Dirty Files / Commit State

The Semantic implementation, focused tests, and this report are intentionally
dirty and uncommitted. The expected dirty set after report creation is:

- the nine production files listed above;
- `tests/unit/test_semantic_memory_v1.py`;
- `docs/reports/SEMANTIC_MEMORY_V1_REPORT.md`.

HEAD remains the design checkpoint `7f8cec00d975e9939decf7b2e5e488043ffbccf8`.

## Next Recommendation

Stop at Semantic V1 acceptance and review the dirty diff. If accepted, commit
the implementation, tests, and report in a separate checkpoint. Do not expand
to Procedural Memory or add new Semantic scenarios until the narrow admission,
scope, lifecycle, and canonical retrieval contracts are accepted.
