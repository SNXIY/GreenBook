# GreenBook Merge Readiness Review

Date: 2026-08-27
Branch: feature/hybrid-search-rag
Reviewed HEAD: 77da72b37b038c79d1cafe3f91f5bfe645f0e1b7
Reviewed tag: greenbook-final-system-evaluation-20260827
Clean comparison tag: greenbook-clean-baseline-20260827

## Verdict

GREENBOOK_MERGE_READY_WITH_LIMITATIONS

The branch is merge-ready for human review because the current delta contains no
production runtime changes, retained correctness evidence is green, and no
duplicate physical mutation, wrong-resource binding, HITL bypass, or durable
runtime bypass was found.

Limitations that must remain visible:

- strict equivalent performance acceptance is inconclusive;
- performance is classified PERFORMANCE_ENVIRONMENT_VARIANCE, with moderate
  confidence rather than a performance PASS;
- the formal TUF observer was unavailable on the final Browser smoke;
- saved Browser parallel artifacts do not provide three clean completed controlled
  runs: one is RUNNING and one completed run contains a sibling
  TaskVersionConflictError during parallel submission.

This review did not merge main, create a post-merge stable tag, or push to main.

## 1. Recovery and repository boundary

Recovery validation confirmed the shutdown checkpoint, required untracked
evaluation material, local service health, and runtime-token presence and
matching without exposing token values. The working tree was clean before this
review and the branch is aligned with origin/feature/hybrid-search-rag.

The delta greenbook-clean-baseline-20260827..HEAD contains seven files:
five evaluation/report files, two Browser evaluation harness files, and zero
production runtime files. git diff --check is clean and the tracked
secret-pattern scan found no matches.

## 2. Correctness evidence reused

The existing final evaluation was reused without rerunning the full suites:

- retained unit baseline: 1602 passed, 0 failed;
- final focused functional/reliability suites: 143 passed, 0 failed;
- bounded-parallel scheduler tests: 5 passed, 0 failed;
- MO-08 and affected Multi-Objective family: PASS evidence;
- duplicate Objective count: 0;
- wrong dependency/resource binding: 0;
- duplicate physical mutation: 0;
- RAG canonical admission: PASS; grounding remains
  RAG_CURRENT_LIMIT_ACCEPTED_PARTIAL;
- Memory semantics and authority: PASS; leakage: 0;
- final Browser smoke: PASS for rapid double-send idempotency, stale-response
  isolation, return-to-conversation behavior, and internal-ID/error-code
  non-leakage.

## 3. Performance classification

Classification: PERFORMANCE_ENVIRONMENT_VARIANCE
Confidence: moderate
Strict performance verdict: INCONCLUSIVE, not PASS.

The current controlled artifacts show large timing spread but no deterministic
extra work in successful single-objective paths.

| Scenario | Baseline p50 E2E | Current successful E2E | Current logic shape |
|---|---:|---:|---|
| Simple READ | 38,776 ms | 111,308 / 194,270 ms | 2 LLM, 2 iterations, 1 tool, 1 Java |
| Simple WRITE | 44,143 ms | 62,120 / 69,289 ms | 2 LLM, 1 iteration, 1 tool, 1 Java |
| Sequential dependent | 55,824 ms | 97,905 / 105,043 / 119,240 ms | 2 LLM, 2 iterations, 2 tools, 2 Java |
| Search + Creation | 61,792 ms | 214,151 ms successful sample | 3 LLM, 2 iterations, 2 tools, 2 Java |

The result is not a strict statistical acceptance because Simple READ, Simple
WRITE, Search + Creation, and independent multi-objective repeat sets contain
failed or incomplete repeats.

### Provider and external-stage variance

- Simple READ semantic calls were 14,156 ms and 27,922 ms with the same 6,029
  input-token shape; ActionLoop calls were 7,248 ms and 6,881 ms.
- Simple WRITE semantic calls were 6,645 ms and 13,762 ms with the same 6,059
  input-token shape; ActionLoop calls were 6,468 ms and 7,153 ms.
- Java stayed small and stable at approximately 54–98 ms.
- MCP varied from approximately 0.1–15.0 seconds across current successful
  controls.

### Queue/admission variance

The queue metric is a broad admission/runner interval, not pure worker polling.
Current successful controls include approximately 153–6,859 ms before runner
work, while retained simple baseline samples ranged approximately 129–508 ms.
Historical durable created-to-claimed evidence remains approximately 5.1–8.2
seconds, with worker claim-to-start approximately 0–1 ms. This points to
admission, worker availability, polling, or service pressure; it does not prove
a new queue-code regression.

### Context and Memory

Current successful controls show Context approximately 12.3–18.5 seconds and
Memory approximately 6.3–6.8 seconds. The implementation uses one bounded
Context join for independent reads and one repository retrieval path; Memory
ranking/touch/format semantics are unchanged. No duplicate Context or Memory
semantic read was found in this review.

### Logic equivalence

No extra deterministic LLM call, ActionLoop iteration, tool call, or durable
operation was found in successful Simple READ/WRITE control shapes. The
single-objective path checks len(ready) > 1 before fan-out; one ready objective
therefore follows normal execution and does not wait for a second objective,
parallel timeout, or artificial join.

## 4. Parallel execution readiness

The scheduler is deterministic and bounded at max_parallel_objectives=2 and
admits only independent, dependency-free, non-conflicting CREATE_DRAFT
objectives. Each write crosses the ActionLoop durable submitter and then
MCP/Java; no direct write invocation was added.

Focused tests prove maximum concurrency 2, dependency exclusion, sibling-failure
isolation, preservation of both execution IDs, and normal single-objective
degradation.

Retained Browser evidence reports bounded parallel PASS with no wrong resource or
duplicate physical mutation; one clean completed run measured 55,672 ms versus a
73,859 ms serial-equivalent Browser run with 6,730.941 ms observed overlap.
However, the three saved named artifacts are not three clean acceptance runs:

- AFTER completed, but one sibling recorded TaskVersionConflictError during
  parallel submission;
- AFTER2 remained RUNNING in the saved artifact;
- AFTER3 completed with two submitted execution IDs and bounded mode.

Therefore implementation status is PASS_BOUNDED_SCOPE, while the formal
three-clean-run acceptance remains incomplete and is a limitation.

## 5. Browser UX and TUF

The final Browser smoke passed its functional checks. The strict TUF observer
searched only user-facing rendered progress/status/activity nodes and found no
meaningful text within its controlled observation window. Decision:
OBSERVABILITY_LIMITATION, not UX_LATENCY_LIMITATION. The smoke did not establish
that the UI falsely claims completion or leaks internal state; it lacks a
dedicated observable progress signal for the formal TUF metric.

## 6. Merge-blocker checklist

| Blocker | Finding |
|---|---|
| Correctness regression | No evidence in retained focused suites |
| Duplicate physical write | 0 in retained evaluation |
| Wrong resource/objective binding | 0 in retained evaluation |
| HITL bypass or destructive pre-approval write | No evidence |
| RESULT_UNKNOWN/idempotency violation | No evidence; rapid double-send stayed one Run |
| Single-objective waits for parallel batch | No; gate and tests cover this |
| RAG canonical admission regression | No; canonical admission PASS |
| Deterministic code performance regression | Not found in logic-equivalence audit |
| Strict performance acceptance | Inconclusive; tracked as limitation |
| TUF formal observability | Unavailable; tracked as limitation |
| Three clean controlled parallel runs | Not established; tracked as limitation |

## 7. Allowed next step

A human may merge this feature branch into main subject to accepting the listed
limitations. After the human merge, create the final stable tag and freeze the
project. This review performs neither the merge nor the final tag.
