# PROCEDURAL_MEMORY_V1_REPORT

## Verdict

**PROCEDURAL_MEMORY_V1_PASS**

This vertical slice implements exactly one Procedural Memory scenario:
an explicit, reusable user instruction for creating a technical article.
Procedural Memory remains bounded soft guidance. It is not an executor,
planner, workflow engine, capability grant, or runtime invariant.

## 1. Design checkpoint

- Design checkpoint commit: 7a94743
- Design commit message: docs: add procedural memory design
- Branch: feature/hybrid-search-rag
- Design checkpoint push: succeeded to origin/feature/hybrid-search-rag
- Episodic/Semantic/Procedural design checkpoint ancestry remains unchanged.

## 2. Production files changed

- packages/agent_core/greenbook_agent_core/memory/procedural.py
  - ProceduralCandidateBuilder
  - ProceduralAdmissionPolicy
  - ProceduralMemoryService
- packages/agent_core/greenbook_agent_core/memory/manager.py
  - canonical remember_procedural
  - scoped fallback and durable shadow persistence
- packages/agent_core/greenbook_agent_core/memory/repository.py
  - existing in-memory and PostgreSQL repository replacement contracts
  - no new table or collection
- packages/agent_core/greenbook_agent_core/memory/retriever.py
  - existing retriever contract filtering
  - current explicit instruction override
- packages/agent_core/greenbook_agent_core/memory/__init__.py
  - public V1 exports
- packages/agent_core/greenbook_agent_core/context/builder.py
  - bounded relevant_procedure projection marked advisory_only
- apps/agent_api/greenbook_agent_api/main.py
  - production service composition and existing retriever allowlist
- apps/agent_api/greenbook_agent_api/api/routes.py
  - completed-turn hook using the explicit user message only

No ActionLoop, Durable Runtime, Task/Objectives, RESULT_UNKNOWN/reconciliation,
MCP, Tool Registry, Capability Filtering, Search, RAG, Java backend, Skills,
or workflow engine semantics were changed.

## 3. Candidate contract

ProceduralCandidate contains only:

- user_id
- tenant_id
- procedure_key
- trigger
- guidance
- confidence
- source_type
- provenance
- observed_at

V1 accepts only source_type=EXPLICIT_USER_INSTRUCTION with provenance
source=explicit_user_instruction, author_role=user, and
procedural_contract=PROCEDURAL_V1.

The only supported identity is:

    procedure_key = technical_article_creation
    trigger       = create_technical_article

Supported guidance values are the two explicit V1 states:

- generate an outline first, then write the body from it;
- explicitly update the rule to write a draft directly without an outline.

No execution trace, plan, tool arguments, runtime ID, or hidden reasoning is
stored as Procedure semantics.

## 4. Admission policy

The policy returns KEEP, DROP, or UNKNOWN; UNKNOWN is write-disabled and is
treated as DROP by the service.

KEEP requires a complete authenticated scope, explicit user authorship, the
supported key/trigger/guidance, confidence at least 0.85, and no
runtime/policy invariant markers.

The builder rejects:

- Preference expressions;
- Semantic self-descriptions;
- past-history or single-episode statements;
- current-task/current-state instructions;
- schedule/version/retry/reconcile/approval/permission rules;
- runtime IDs and UUIDs;
- unsupported or ambiguous workflow text.

The V1 rule is conservative: missing a candidate is preferred to learning an
unsupported procedure.

## 5. Type-boundary results

| Input meaning | Result |
|---|---|
| I prefer short technical articles | Preference; no Procedure |
| I am a Java backend developer | Semantic; no Procedure |
| Last time I listed an outline first | Episode/history; no Procedure |
| This time write an article in this order | Current state; no Procedure |
| From now on, create an outline first, then write the article body | Procedural V1 |

The builder never upgrades one Preference, Semantic fact, or Episode into a
Procedure. Existing legacy PROCEDURAL records without the V1 contract are
quarantined.

## 6. Conflict, lifecycle, and idempotency

The conflict scope is:

    tenant_id + user_id + procedure_key

The deterministic memory ID includes the V1 contract, scope, key, trigger, and
normalized guidance. Consequently:

- replaying the same explicit rule reuses one ACTIVE record;
- different guidance receives a new identity;
- the old same-key ACTIVE record becomes SUPERSEDED;
- the new record becomes the only canonical ACTIVE value;
- different users and tenants never share the projection;
- historical superseded rows remain auditable and point to their replacement.

The in-memory manager lock protects the local fallback. The PostgreSQL
repository uses a scoped transaction advisory lock and performs the
supersede/upsert in one transaction. No second repository was introduced.

## 7. Canonical write chain

    Explicit User Instruction
            ↓
    ProceduralCandidateBuilder
            ↓
    ProceduralAdmissionPolicy
            ↓
    Conflict Resolver
            ↓
    MemoryManager.remember_procedural
            ↓
    existing agent_memories repository

The API hook runs only after a COMPLETED turn and passes the authenticated
user and tenant scope. ActionLoop, ToolRuntime, RuntimeAgentService, Creator,
and Java do not write Procedural Memory directly.

## 8. Canonical retrieval and soft guidance

Production uses the existing single path:

    Current turn
      → MemoryRetriever
      → MemoryRelevanceGate
      → bounded ContextBuilder
      → Interpreter / ActionLoop as advisory evidence

Production retrieval allows PROCEDURAL only with:

    memory_contract = PROCEDURAL_V1
    memory_role     = relevant_procedure
    status          = ACTIVE

The context projection labels the result relevant_procedure and
advisory_only=true; provenance and raw payloads are not sent to the model
view. The total existing memory budget remains bounded.

An explicit current exception such as “this time do not use an outline, write
directly” suppresses the stored procedure for that turn. This is a
retrieval/context boundary only; it does not create a new Task, alter an
Objective, rewrite a Tool candidate, execute a Tool, or bypass any policy.

Unrelated requests return zero Procedure records.

## 9. Focused evaluation metrics

The small V1 benchmark is intentionally narrow and uses one positive scenario
plus negative/type-boundary cases.

| Metric | Result |
|---|---:|
| Admission Precision | 1.00 |
| Admission Recall | 1.00 |
| Unsupported Procedure Inference Rate | 0.00 |
| Runtime-Invariant Misclassification Rate | 0.00 |
| Duplicate Active Rate | 0.00 |
| Supersede Correctness | 1.00 |
| Relevant Retrieval Recall | 1.00 |
| Relevant Retrieval Precision | 1.00 |
| No-match False Return Rate | 0.00 |
| Current Instruction Override Correctness | 1.00 |
| Runtime/Policy Override Rate | 0.00 |
| Preference/Procedural Confusion | 0.00 |
| Semantic/Procedural Confusion | 0.00 |
| Episodic/Procedural Confusion | 0.00 |
| Cross-user leakage | 0.00 |
| Cross-tenant leakage | 0.00 |

The benchmark is a focused V1 safety check, not a claim about broad
production coverage or general procedure learning.

## 10. Focused tests

Executed:

    uv run pytest tests/unit/test_procedural_memory_v1.py \
      tests/unit/test_semantic_memory_v1.py \
      tests/unit/test_episodic_memory_v1.py \
      tests/unit/test_memory_runtime_convergence.py \
      tests/unit/test_preference_memory_extractor.py \
      tests/unit/test_preference_memory_retrieval.py \
      tests/unit/test_memory_retriever.py \
      tests/integration/test_context_memory_runtime.py -q

Result: 81 passed.

The dedicated Procedural V1 suite contains 17 passed tests covering:

- explicit admission;
- Preference/Semantic/Episodic/current-state isolation;
- runtime-invariant rejection;
- deterministic replay;
- same-key explicit update and supersede;
- canonical retrieval and bounded context labels;
- irrelevant-query no-match;
- current-instruction override;
- legacy quarantine;
- completed-turn-only hook;
- feature flag OFF;
- cross-user and cross-tenant isolation;
- focused benchmark assertions.

Targeted Ruff checks for the new/modified Memory V1 core and focused test
file: passed.

git diff --check: passed.

No L1/L2/L3, RAG/Search matrix, Java/browser E2E, or full expensive evaluation
was run.

## 11. Runtime and policy override result

Runtime/Policy Override Rate = 0.00.

The implementation has no execution authority. Hard runtime, security,
capability, approval, tool-contract, Task/Objectives, and verified business
truth remain higher authority than this Memory type. The current explicit
user instruction can suppress a stored soft procedure for the current
retrieval, but stored Procedure cannot suppress the current instruction.

## 12. Blockers and limitations

No V1 blocker was found.

Intentional limitations:

- only one technical-article workflow is supported;
- only explicit user instructions are admitted;
- no automatic learning from Episodes, ActionObservations, tool traces, or
  repeated success;
- no generic procedure DSL, planner, Skill runtime, or workflow engine;
- legacy StrategyRetriever, ProceduralMemoryExtractor, and generic
  REUSABLE_STRATEGY compatibility surfaces remain quarantined/available for
  old tests and are not production V1 callers;
- full Postgres behavior is represented by the repository transaction path but
  was not exercised against a live database in this focused run.

## 13. Dirty files and Git status

Implementation, focused tests, and this report are intentionally uncommitted
for acceptance review. The design checkpoint is already committed and pushed.

Expected dirty files:

- apps/agent_api/greenbook_agent_api/api/routes.py
- apps/agent_api/greenbook_agent_api/main.py
- packages/agent_core/greenbook_agent_core/context/builder.py
- packages/agent_core/greenbook_agent_core/memory/__init__.py
- packages/agent_core/greenbook_agent_core/memory/manager.py
- packages/agent_core/greenbook_agent_core/memory/procedural.py
- packages/agent_core/greenbook_agent_core/memory/repository.py
- packages/agent_core/greenbook_agent_core/memory/retriever.py
- tests/unit/test_procedural_memory_v1.py
- docs/reports/PROCEDURAL_MEMORY_V1_REPORT.md

No Procedural implementation commit or push was performed.

## 14. Next recommendation

Stop at Procedural Memory V1 and wait for acceptance. Do not add new
Procedure scenarios, re-enable legacy procedural learning, enter Semantic
expansion, or change Runtime semantics until this dirty diff is reviewed.
