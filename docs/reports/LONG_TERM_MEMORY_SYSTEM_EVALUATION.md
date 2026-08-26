# LONG_TERM_MEMORY_SYSTEM_EVALUATION

Checkpoint: `17a156d8464a0f33176781f3717e4d0e80854afa` (`17a156d` Procedural Memory V1 checkpoint).
This was an evaluation-only run. No production file was changed, and no
commit, push, merge, or expensive L1/L2/L3/RAG/Search/Java suite was run.

## Verdict

**LONG_TERM_MEMORY_QUALITY_ISSUES**

Dataset families: **13 classification**
and **17 retrieval** cases, plus
**30 context-budget measurements**.

## Architecture Invariant Check

- PASS: one_memory_retriever_in_production_composition
- PASS: one_relevance_gate_in_canonical_retriever
- PASS: canonical_gate_implementation_is_single
- PASS: context_builder_has_bounded_memory_budget
- PASS: production_dirty_scope_is_evaluation_only
- PASS: no_production_file_changed

Canonical runtime:

`MemoryManager / Repository -> MemoryRetriever -> MemoryRelevanceGate -> bounded ContextBuilder`.

The four logical types share the repository, retriever, Gate, scope, lifecycle,
and bounded injection contract. Preference/Semantic persisted-enum compatibility
is separated by metadata contract and logical projection. Legacy Episodic is
quarantined by the `EPISODIC_V1` contract filter.

## Four-Type Classification

| Type | TP | FP | FN | Precision | Recall |
|---|---:|---:|---:|---:|---:|
| PREFERENCE | 3 | 0 | 0 | 1.0000 | 1.0000 |
| SEMANTIC | 2 | 0 | 0 | 1.0000 | 1.0000 |
| EPISODIC | 1 | 0 | 0 | 1.0000 | 1.0000 |
| PROCEDURAL | 2 | 0 | 0 | 1.0000 | 1.0000 |

Wrong-Type Admission Rate: **0.00%**.
Unsupported Inference Rate: **0.00%**.

Confusion metrics: `none`.

Boundary failures:

- None

## Retrieval Evaluation

| K | Recall@K | Fixed Precision@K | Returned Precision@K | Eligible |
|---:|---:|---:|---:|---:|
| 1 | 0.9444 | 1.0000 | 1.0000 | 12 |
| 3 | 0.9722 | 0.3611 | 0.9028 | 12 |
| 5 | 0.9722 | 0.2167 | 0.9028 | 12 |

| Metric | Value |
|---|---:|
| No-match false return rate | 0.00% |
| Irrelevant Memory Injection Rate | 45.83% |
| Required Memory Miss Rate | 7.14% |
| Cross-user leakage count | 0 |
| Cross-tenant leakage count | 0 |
| Current-instruction override failures | 0 |

### Candidate / Selected / Filtered by Type

| Type | Candidate | Selected | Filtered |
|---|---:|---:|---:|
| PREFERENCE | 8 | 6 | 2 |
| SEMANTIC | 7 | 7 | 0 |
| EPISODIC | 8 | 5 | 3 |
| PROCEDURAL | 7 | 6 | 1 |

Required recall by type: `{'PREFERENCE': 0.75, 'SEMANTIC': 1.0, 'EPISODIC': 1.0, 'PROCEDURAL': 1.0}`.

Retrieval failures:

- `A-preference-only` (A_four_types_present): expected `['8394cf0d-e1ae-4899-ac27-fc214a53600a']`, actual `['8394cf0d-e1ae-4899-ac27-fc214a53600a', 'prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783']`
- `A-procedural-only` (A_four_types_present): expected `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783']`, actual `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', '8394cf0d-e1ae-4899-ac27-fc214a53600a', 'epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77']`
- `C-multi-preference-semantic-procedure` (C_multi_type_required): expected `['8394cf0d-e1ae-4899-ac27-fc214a53600a', 'prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', 'semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`, actual `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', 'semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`
- `E-current-procedure-override` (E_current_instruction_override): expected `[]`, actual `['epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77']`
- `I-cross-user` (I_cross_user_tenant): expected `[]`, actual `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', 'epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77', 'semv1-2b80ce5adf8a95e228e8faf57ff61af8c1a9eb22fd08c72ac4a7caf3ad3e523f', 'semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03', '8394cf0d-e1ae-4899-ac27-fc214a53600a']`
- `I-cross-tenant` (I_cross_user_tenant): expected `[]`, actual `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', '8394cf0d-e1ae-4899-ac27-fc214a53600a']`

## Context Budget Evaluation

The measured model-facing memory payload is the serialized combination of
`user_preferences` and `recalled_memories` after `ContextBuilder` and the
interpreter projection. Measurements include 1, 4, and 12 candidate shapes.

| Metric | Value |
|---|---:|
| Maximum selected memory count | 5 |
| Memory context chars p50 | 2337.0 |
| Memory context chars p95 | 2820.0 |
| Memory context chars max | 2820.0 |
| Estimated memory tokens p50/p95/max | 585.0 / 705.0 / 705.0 |
| Nominal memory budget | 6000 chars (5 × 1200) |
| Budget percentage p50/p95/max | 39.0% / 47.0% / 47.0% |
| Bounded context rate | 100.00% |

The 12-candidate shape selected at most five records, confirming that Memory
does not grow the model context without the ContextBuilder bound. Token values
are a conservative `ceil(chars / 4)` estimate, not a provider tokenizer count;
the percentage uses the nominal five-record × 1200-character Memory budget.

Per-type selected contribution across the 30 measurements:
`{'PREFERENCE': 45, 'SEMANTIC': 30, 'EPISODIC': 15, 'PROCEDURAL': 10}`.

## Lifecycle Correctness

| Case | Result |
|---|---|
| superseded_preference_excluded | PASS |
| superseded_semantic_excluded | PASS |
| superseded_procedure_excluded | PASS |
| inactive_memory_excluded | PASS |
| legacy_episodic_excluded | PASS |

Lifecycle: **5/5 passed**.
Superseded and inactive rows were excluded; legacy Episodic was not admitted
to the canonical retrieval contract.

## Duplicate / Consolidation Evaluation

| Metric | Value |
|---|---:|
| Duplicate Active Memory Rate | 0.00% |
| Duplicate Active Memory Count | 0 |
| Preference replay same ID | True |
| Semantic replay same ID | True |
| Episode replay same ID | True |
| Distinct Episode not collapsed | True |
| Procedure replay same ID | True |

## Instruction / Truth Priority

- PASS: current_instruction_overrides_procedure
- PASS: no_memory_is_no_task_mutation
- PASS: provider_projection_hides_memory_identity
- PASS: procedural_memory_is_advisory_only

Memory Authority Violation Rate: **0.00%**.
Procedural guidance remained advisory and the current explicit exception won.

## Isolation and Cross-Conversation Behavior

- Cross-user leakage: **0**.
- Cross-tenant leakage: **0**.
- Cross-conversation reuse is allowed for relevant long-term Memory; current
  Task, target, resource, approval, and execution state are not copied into a
  new ContextBuilder snapshot.

## Failure Diagnosis

FIRST_BAD_STATE: **retrieval selected set**.
Failure families: **['RETRIEVAL_ISSUE', 'RELEVANCE_GATE_ISSUE']**.

Evidence:

- `A-preference-only` selected the intended Preference plus an unrelated
  Procedure sharing `technical/article` terms.
- `A-procedural-only` selected the intended Procedure plus unrelated Preference
  and Episode records sharing the same publication vocabulary.
- `C-multi-preference-semantic-procedure` selected Semantic and Procedure but
  missed the required Preference.
- `E-current-procedure-override` correctly removed the stored Procedure but
  still returned an Episode for a request explicitly asking to bypass the
  outline.
- The two scope-qualified I cases selected only in-scope records; they caused
  no user or tenant leakage, but show that lexical matching does not understand
  a request about another scope.

Root cause: the current single Gate receives a type-neutral lexical relevance
score. Shared domain words can clear the same threshold across types, while a
multi-type query can distribute its terms so that one required type falls
below the threshold. This is a retrieval-quality limitation, not evidence of a
second runtime, lifecycle bypass, or authority violation.

General invariant affected: selected Memory must be relevant to the current
request, and no-match must remain allowed. The scope and lifecycle invariants
still passed. Minimal fix proposal for a later, separately reviewed change:
improve the scoring/intent evidence supplied to this one Gate (including
stronger unique-term or type-aware relevance) while retaining one canonical
Gate, bounded injection, and no-match behavior. No production fix was applied
in this evaluation-only run.

## Production Files Changed

**None.** The evaluator changed only evaluation assets. Dirty paths observed by
the architecture audit:

`['scripts/memory_evaluation_harness.py', 'docs/evaluation/long_term_memory_system_dataset.json', 'docs/evaluation/long_term_memory_system_results.json', 'docs/reports/LONG_TERM_MEMORY_SYSTEM_EVALUATION.md', 'tests/unit/test_long_term_memory_system_evaluation.py']`

Out-of-scope paths: `[]`.

## Targeted Test Scope

The intended verification scope is limited to the four Memory V1/V2 focused
tests, Memory runtime convergence tests, this joint evaluation test, and Ruff
on evaluation assets. No L1/L2/L3, RAG/Search matrix, Java/browser E2E, or
full expensive evaluation was included in this report.

## Next Recommendation

Keep the architecture unchanged and address the diagnosed quality issue only after reviewing the listed cases and FIRST_BAD_STATE.
