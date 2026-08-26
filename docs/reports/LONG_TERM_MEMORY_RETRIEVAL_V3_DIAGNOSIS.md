# LONG_TERM_MEMORY_RETRIEVAL_V3_DIAGNOSIS

Baseline evaluation commit: `75529b13227fa1bee3c4a9bbbd99bc2c21154c34`
Evaluation checkpoint: `17a156d8464a0f33176781f3717e4d0e80854afa`

This is a read-only selected-set diagnosis. No production file was changed by
the diagnosis. The existing V2 retriever and Gate were observed as composed;
no alternate Retriever or Gate was installed.

## Exact FIRST_BAD_STATE

**`retrieval selected set`**

The repository candidate pool is scoped and contract-filtered. The first
incorrect state is the set returned after the canonical Retriever's global
relevance Gate. Classification, write policy, lifecycle, user/tenant scope,
and authority checks are upstream or parallel invariants and are not the
source of these retrieval failures.

Failure families: **`RETRIEVAL_ISSUE`, `RELEVANCE_GATE_ISSUE`**.

## Current V2 decision mechanics

- Candidate search uses the existing single `MemoryRetriever` and one
  repository, with per-type query variants only for the persisted
  Preference/Semantic compatibility alias.
- Candidates are first ordered by the Retriever's `_score` for operational
  ranking. The Gate then applies a single global normalized relevance score
  (`lexical_relevance`, or an exact conversation/task relation), requires
  relevance `>= 0.5` and confidence `>= 0.5`, sorts that shared score, and
  takes global `limit=5`.
- Therefore the current Gate is **global threshold + global top-K**, not a
  type-aware threshold, type-aware score, or required-type coverage policy.
- Scores are numerically normalized to `[0, 1]`, but are not semantically
  calibrated across types. A longer Episode or Procedure can share generic
  domain terms with a request, while a concise Preference can have fewer
  overlapping terms in a multi-type request.

## Metric-definition clarification

The existing report intentionally contains several denominators:

| Metric | Definition used by V2 baseline |
|---|---|
| Fixed Precision@K | Hits in the first K positions divided by K, even when fewer than K records were returned. |
| Returned Precision@K | Hits in the first K returned positions divided by the number actually returned in that prefix; empty prefixes contribute 0. |
| Irrelevant Memory Injection Rate | Selected records not in the case's expected set divided by all selected records across retrieval cases, including selected wrong-type records and selected records for cases whose expected set is empty. |
| Required Memory Miss Rate | Expected records not selected divided by all expected records. |
| No-match False Return Rate | No-memory cases that returned at least one record divided by the explicitly marked `D_no_memory` cases. |

Returned Precision can remain high when a case returns only one correct record,
while Irrelevant Injection Rate can be high because it counts every extra
selected record across all cases and includes no-memory/wrong-type selections.
They answer different questions and must not be optimized as interchangeable
metrics.

## False-positive type pairs

| Selected type -> expected type(s) | Count |
|---|---:|
| `EPISODIC -> NONE` | 2 |
| `EPISODIC -> PROCEDURAL` | 1 |
| `PREFERENCE -> NONE` | 2 |
| `PREFERENCE -> PROCEDURAL` | 1 |
| `PROCEDURAL -> NONE` | 2 |
| `PROCEDURAL -> PREFERENCE` | 1 |
| `SEMANTIC -> NONE` | 2 |

The expected-type label is `NONE` for an explicitly no-memory case. This table
shows cross-type lexical contamination rather than scope leakage.

## Required-memory misses

| Case | Missing memory | Type | Selection reason |
|---|---|---|---|
| `C-multi-preference-semantic-procedure` | `8e13779b-20c3-488a-9f80-1e6534f772b5` | PREFERENCE | filtered_below_global_relevance_threshold |

In particular, the multi-type case's Preference is present in the scoped
candidate pool but falls below the same global threshold after the query terms
are distributed across Preference, Semantic, and Procedure vocabulary. It is
not rejected by lifecycle, confidence, user scope, tenant scope, or type
contract. This is why the required Preference recall is lower while the other
types pass.

## Per-case candidate trace

The following is the complete trace for every retrieval case. `Raw lexical` is
the direct pre-normalization `lexical_relevance` output; `Current relevance` is
the V2 callback value before `MemoryRelevanceGate` clamps it to `[0, 1]`.
`Ranking score` is retained in the JSON result for the Retriever's pre-Gate
order. A missing expected candidate is represented with `n/a` scores and an
explicit candidate-pool reason.

### `A-preference-only` (A_four_types_present)

Query: `deep technical articles`
Terms: `['deep', 'technical', 'articles']`
Expected IDs: `['8e13779b-20c3-488a-9f80-1e6534f772b5']`
Selected IDs: `['8e13779b-20c3-488a-9f80-1e6534f772b5', 'prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `8e13779b-20c3-488a-9f80-1e6534f772b5` | PREFERENCE | `1.0000` | `1.0000` | `0.90` | yes | selected_by_global_threshold_and_top_k | no | no |
| `epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77` | EPISODIC | `0.3333` | `0.3333` | `0.95` | no | filtered_below_global_relevance_threshold | no | no |
| `prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783` | PROCEDURAL | `1.3333` | `1.3333` | `0.98` | yes | selected_by_global_threshold_and_top_k | yes | no |

### `A-semantic-only` (A_four_types_present)

Query: `Agent learning`
Terms: `['agent', 'learning']`
Expected IDs: `['semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`
Selected IDs: `['semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03` | SEMANTIC | `2.5000` | `2.5000` | `0.98` | yes | selected_by_global_threshold_and_top_k | no | no |

### `A-episodic-only` (A_four_types_present)

Query: `verified publication revised title publication time`
Terms: `['verified', 'publication', 'revised', 'title', 'time']`
Expected IDs: `['epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77']`
Selected IDs: `['epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77` | EPISODIC | `1.8000` | `1.8000` | `0.95` | yes | selected_by_global_threshold_and_top_k | no | no |

### `A-procedural-only` (A_four_types_present)

Query: `outline body technical article`
Terms: `['outline', 'body', 'technical', 'article']`
Expected IDs: `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783']`
Selected IDs: `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', '8e13779b-20c3-488a-9f80-1e6534f772b5', 'epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783` | PROCEDURAL | `1.5000` | `1.5000` | `0.98` | yes | selected_by_global_threshold_and_top_k | no | no |
| `8e13779b-20c3-488a-9f80-1e6534f772b5` | PREFERENCE | `0.5000` | `0.5000` | `0.90` | yes | selected_by_global_threshold_and_top_k | yes | no |
| `epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77` | EPISODIC | `0.5000` | `0.5000` | `0.95` | yes | selected_by_global_threshold_and_top_k | yes | no |

### `B-preference-only` (B_single_type)

Query: `prefer deep articles`
Terms: `['prefer', 'deep', 'articles']`
Expected IDs: `['8e13779b-20c3-488a-9f80-1e6534f772b5']`
Selected IDs: `['8e13779b-20c3-488a-9f80-1e6534f772b5']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `8e13779b-20c3-488a-9f80-1e6534f772b5` | PREFERENCE | `1.6667` | `1.6667` | `0.90` | yes | selected_by_global_threshold_and_top_k | no | no |

### `B-semantic-only` (B_single_type)

Query: `Java backend`
Terms: `['java', 'backend']`
Expected IDs: `['semv1-2b80ce5adf8a95e228e8faf57ff61af8c1a9eb22fd08c72ac4a7caf3ad3e523f']`
Selected IDs: `['semv1-2b80ce5adf8a95e228e8faf57ff61af8c1a9eb22fd08c72ac4a7caf3ad3e523f']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `semv1-2b80ce5adf8a95e228e8faf57ff61af8c1a9eb22fd08c72ac4a7caf3ad3e523f` | SEMANTIC | `1.5000` | `1.5000` | `0.98` | yes | selected_by_global_threshold_and_top_k | no | no |

### `B-episodic-only` (B_single_type)

Query: `past publication experience`
Terms: `['past', 'publication', 'experience']`
Expected IDs: `['epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77']`
Selected IDs: `['epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77` | EPISODIC | `1.3333` | `1.3333` | `0.95` | yes | selected_by_global_threshold_and_top_k | no | no |

### `B-procedural-only` (B_single_type)

Query: `outline then body`
Terms: `['outline', 'then', 'body']`
Expected IDs: `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783']`
Selected IDs: `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783` | PROCEDURAL | `1.3333` | `1.3333` | `0.98` | yes | selected_by_global_threshold_and_top_k | no | no |

### `C-multi-preference-semantic-procedure` (C_multi_type_required)

Query: `deep technical Agent learning outline body`
Terms: `['deep', 'technical', 'agent', 'learning', 'outline', 'body']`
Expected IDs: `['8e13779b-20c3-488a-9f80-1e6534f772b5', 'prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', 'semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`
Selected IDs: `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', 'semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783` | PROCEDURAL | `0.8333` | `0.8333` | `0.98` | yes | selected_by_global_threshold_and_top_k | no | no |
| `8e13779b-20c3-488a-9f80-1e6534f772b5` | PREFERENCE | `0.3333` | `0.3333` | `0.90` | no | filtered_below_global_relevance_threshold | no | yes |
| `semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03` | SEMANTIC | `0.8333` | `0.8333` | `0.98` | yes | selected_by_global_threshold_and_top_k | no | no |
| `epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77` | EPISODIC | `0.3333` | `0.3333` | `0.95` | no | filtered_below_global_relevance_threshold | no | no |

### `D-no-memory-public-posts` (D_no_memory)

Query: `鏌ヤ竴涓嬫渶杩戝叕寮€甯栧瓙`
Terms: `['鏌ヤ竴涓嬫渶杩戝叕寮€甯栧瓙', '鏌ヤ竴', '涓€涓?, '涓嬫渶', '鏈€杩?, '杩戝叕', '鍏紑', '寮€甯?, '甯栧瓙']`
Expected IDs: `[]`
Selected IDs: `[]`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| None | - | - | - | - | - | - | - | - |

### `D-no-memory-weather` (D_no_memory)

Query: `weather forecast and astronomy`
Terms: `['weather', 'forecast', 'astronomy']`
Expected IDs: `[]`
Selected IDs: `[]`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| None | - | - | - | - | - | - | - | - |

### `E-current-procedure-override` (E_current_instruction_override)

Query: `This time write the technical article directly without an outline.`
Terms: `['this', 'time', 'write', 'technical', 'article', 'directly', 'without', 'outline']`
Expected IDs: `[]`
Selected IDs: `['epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77']`
Procedure override detected: `True`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783` | PROCEDURAL | `0.7500` | `0.7500` | `0.98` | no | filtered_by_current_procedure_override | no | no |
| `8e13779b-20c3-488a-9f80-1e6534f772b5` | PREFERENCE | `0.2500` | `0.2500` | `0.90` | no | filtered_below_global_relevance_threshold | no | no |
| `epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77` | EPISODIC | `0.5000` | `0.5000` | `0.95` | yes | selected_by_global_threshold_and_top_k | yes | no |

### `F-semantic-new-truth` (F_memory_conflict)

Query: `Agent`
Terms: `['agent']`
Expected IDs: `['semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`
Selected IDs: `['semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03` | SEMANTIC | `2.0000` | `2.0000` | `0.98` | yes | selected_by_global_threshold_and_top_k | no | no |

### `G-preference-new-truth` (G_preference_update)

Query: `concise replies`
Terms: `['concise', 'replies']`
Expected IDs: `['joint-preference-concise-new']`
Selected IDs: `['joint-preference-concise-new']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `joint-preference-concise-new` | PREFERENCE | `1.0000` | `1.0000` | `0.90` | yes | selected_by_global_threshold_and_top_k | no | no |

### `H-cross-conversation` (H_cross_conversation)

Query: `Agent`
Terms: `['agent']`
Expected IDs: `['semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`
Selected IDs: `['semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03` | SEMANTIC | `2.0000` | `2.0000` | `0.98` | yes | selected_by_global_threshold_and_top_k | no | no |

### `I-cross-user` (I_cross_user_tenant)

Query: `deep technical articles owned by another user`
Terms: `['deep', 'technical', 'articles', 'owned', 'another', 'user']`
Expected IDs: `[]`
Selected IDs: `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', 'epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77', 'semv1-2b80ce5adf8a95e228e8faf57ff61af8c1a9eb22fd08c72ac4a7caf3ad3e523f', 'semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03', '8e13779b-20c3-488a-9f80-1e6534f772b5']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `8e13779b-20c3-488a-9f80-1e6534f772b5` | PREFERENCE | `0.5000` | `0.5000` | `0.90` | yes | selected_by_global_threshold_and_top_k | yes | no |
| `epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77` | EPISODIC | `0.6667` | `0.6667` | `0.95` | yes | selected_by_global_threshold_and_top_k | yes | no |
| `prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783` | PROCEDURAL | `1.3333` | `1.3333` | `0.98` | yes | selected_by_global_threshold_and_top_k | yes | no |
| `semv1-2b80ce5adf8a95e228e8faf57ff61af8c1a9eb22fd08c72ac4a7caf3ad3e523f` | SEMANTIC | `0.6667` | `0.6667` | `0.98` | yes | selected_by_global_threshold_and_top_k | yes | no |
| `semv1-0f1eba6b3ac3334c213ae515d73351c47800adc6a8d12d537af09387f1502a03` | SEMANTIC | `0.6667` | `0.6667` | `0.98` | yes | selected_by_global_threshold_and_top_k | yes | no |

### `I-cross-tenant` (I_cross_user_tenant)

Query: `deep technical articles in another tenant`
Terms: `['deep', 'technical', 'articles', 'another', 'tenant']`
Expected IDs: `[]`
Selected IDs: `['prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783', '8e13779b-20c3-488a-9f80-1e6534f772b5']`
Procedure override detected: `False`

| Candidate ID | Type | Raw lexical | Current relevance | Confidence | Selected | Reason | FP | FN |
|---|---|---:|---:|---:|---|---|---|---|
| `8e13779b-20c3-488a-9f80-1e6534f772b5` | PREFERENCE | `0.6000` | `0.6000` | `0.90` | yes | selected_by_global_threshold_and_top_k | yes | no |
| `epv1-b36f162c1e61aae8372a825a529c5fd52d0b4d3d0e5da1ae7a0597c75506dc77` | EPISODIC | `0.4000` | `0.4000` | `0.95` | no | filtered_below_global_relevance_threshold | no | no |
| `prov1-73839f1c495d0a332faf0f549e901c537d2f26e325920ce49168234b3ba64783` | PROCEDURAL | `1.2000` | `1.2000` | `0.98` | yes | selected_by_global_threshold_and_top_k | yes | no |


## Root cause and minimal design direction

Evidence shows two coupled quality problems:

1. Shared lexical terms such as `technical`, `article`, `publication`, and
   `writing` allow a record from another logical type to clear a global
   threshold.
2. A multi-type request has no explicit memory-need profile or required-type
   coverage. The global threshold therefore filters a required Preference even
   though the request contains a Preference signal.

The smallest safe experiment is one type-aware score inside the existing Gate,
derived from deterministic request features, followed by optional required-type
coverage within the same total bound. It must preserve no-match, confidence,
scope, lifecycle, and authority invariants. No production change is justified
until offline results meet the V3 acceptance gate.
