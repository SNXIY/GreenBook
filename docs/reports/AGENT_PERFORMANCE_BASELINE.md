# AGENT_PERFORMANCE_BASELINE_COMPLETE

**Scope:** measurement and diagnosis only
**Branch:** `feature/hybrid-search-rag`
**Sample set:** 6 scenarios × 2 repetitions = 12 real local Agent E2E runs
**Production code changed:** 0

## 1. RAG final checkpoint

The RAG checkpoint is fixed by [RAG_FINAL_STATUS.md](RAG_FINAL_STATUS.md):

```text
Hybrid Search
  -> Post Retrieval
  -> Chunk Retrieval
  -> Evidence Selection
  -> Grounded Generation
  -> Citation / No-answer
```

| Metric | Accepted production baseline |
|---|---:|
| Post Recall@10 | 0.744444 |
| Conditional Chunk Recall@10 | 0.307692 |
| Final Evidence Recall@10 | 0.251852 |
| Production semantic generation coverage | approximately 0.333 |
| Faithfulness | approximately 1.000 |
| Citation correctness | approximately 1.000 |
| No-answer accuracy | 5/5 |
| Hallucination | approximately 0 |

Final RAG verdict: `RAG_CURRENT_LIMIT_ACCEPTED`. No RAG experiment was run as
part of this performance baseline, and the values above were not changed.

## 2. Measurement contract

The harness uses the existing public boundaries:

```text
Java login
  -> Agent API conversation/message
  -> durable Run polling and Run SSE
  -> existing Run performance projection
  -> existing debug trace
```

Configuration observed from `.env`:

| Setting | Observed value |
|---|---|
| Agent dispatch | `queue` |
| Runtime storage | `postgres` |
| Process role | `all` |
| In-process worker | `true` |
| Agent API | `http://127.0.0.1:8094` |
| MCP health boundary | `http://127.0.0.1:8095` |
| Java boundary | `http://127.0.0.1:8080` |
| Service health | Agent API, MCP, and Java checks returned ready |

Each repetition used a fresh conversation and the authenticated configured E2E
account. `observed_e2e_ms` is measured from completion of the accepted message
POST to the terminal Run read. `server_total_latency_ms` is the existing Run
projection. The reported TTFT is explicitly a proxy: time from accepted POST to
the first Run SSE activity. The API does not expose provider token-level TTFT.

The result artifact is [agent_performance_baseline_results.json](../evaluation/agent_performance_baseline_results.json), and the evaluation harness is [measure_agent_performance_baseline.py](../../apps/backend/scripts/measure_agent_performance_baseline.py).

## 3. Scenario set

| ID | Scenario | Request shape | Observed outcome |
|---|---|---|---|
| A | Simple READ | Search recent Agent posts | 2/2 `COMPLETED` |
| B | Simple WRITE | Create and save a Java draft without publishing | 2/2 `FAILED` |
| C | Sequential dependent | Create a post, then schedule publication | 2/2 `FAILED` |
| D | Independent multi-objective | Create two independent drafts | 2/2 `FAILED` |
| E | Search + Creation | Search Java posts, then create a draft using the results | 2/2 `FAILED` |
| F | RAG grounded query | Explicit `ANSWER_FROM_KNOWLEDGE` community-evidence request | 2/2 `COMPLETED` on `route_chat`; canonical RAG tool not observed |

The F wording was made explicit to test reachability of the canonical RAG
capability, not to introduce a new RAG evaluation query. Both runs had
`tool_calls=0` and trace stage `route_chat`; they are therefore not counted as
RAG executions or RAG latency measurements.

## 4. E2E and TTFT baseline

Values are `p50 / p95` in milliseconds. Every per-scenario group has `n=2`, so
its p95 is descriptive only. The aggregate has `n=12` but mixes different
workloads and is not a product SLO.

| Scenario | Status | E2E p50 / p95 | TTFT proxy p50 / p95 |
|---|---|---:|---:|
| A. Simple READ | 2 completed | 44,168 / 53,350 | 3,625 / 6,708 |
| B. Simple WRITE | 2 failed | 43,831 / 44,783 | 12,909 / 19,430 |
| C. Sequential dependent | 2 failed | 36,143 / 37,184 | 12,048 / 12,172 |
| D. Independent multi-objective | 2 failed | 28,113 / 28,474 | 12,337 / 12,425 |
| E. Search + Creation | 2 failed | 71,250 / 71,814 | 3,406 / 6,524 |
| F. RAG request, route_chat | 2 completed | 17,116 / 17,756 | 10,651 / 10,750 |
| Mixed aggregate | 4 completed, 8 failed | 36,143 / 71,814 | 10,651 / 19,430 |

The mixed aggregate also reports server total latency of `39,749 / 71,501 ms`.

## 5. LLM, token, ActionLoop, and component metrics

All values below are `p50 / p95`. Token counts are the existing projection's
input/output counts per turn. `—` means no observed value, not zero.

| Scenario | LLM calls | Input tokens | Output tokens | ActionLoop iterations | Queue wait ms | Interpreter semantic ms | Memory ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| A. Simple READ | 2 / 2 | 7,206 / 7,228 | 361 / 375 | 2 / 2 | 3,095 / 5,691 | 6,792 / 7,068 | 5,926 / 6,221 |
| B. Simple WRITE | 2 / 2 | 7,490 / 7,495 | 735 / 737 | 1 / 1 | 128 / 183 | 7,004 / 7,333 | 10,050 / 13,717 |
| C. Sequential dependent | 2 / 2 | 7,072 / 7,076 | 1,058 / 1,150 | 1 / 1 | 19,215 / 19,367 | — | 6,258 / 6,263 |
| D. Independent multi-objective | 1 / 1 | 5,817 / 5,817 | 330 / 330 | 1 / 1 | 285 / 319 | 6,568 / 6,585 | 6,470 / 6,565 |
| E. Search + Creation | 3 / 3 | 10,773 / 10,800 | 1,883 / 1,933 | 2 / 2 | 620 / 903 | 10,431 / 14,136 | 6,369 / 6,501 |
| F. RAG request, route_chat | 1 / 1 | 5,813 / 5,813 | 201 / 202 | 0 / 0 | 2,667 / 5,203 | 6,077 / 6,621 | 5,590 / 5,775 |

| Scenario | Search ms | RAG ms | MCP/tool ms | Java calls / latency | Final response latency |
|---|---:|---:|---:|---:|---:|
| A. Simple READ | 6,767 / 6,852¹ | — | 6,767 / 6,852 | — | — |
| B. Simple WRITE | — | — | 138 / 141 | — | — |
| C. Sequential dependent | — | — | 130 / 136 | — | — |
| D. Independent multi-objective | — | — | 129 / 130 | — | — |
| E. Search + Creation | — | — | 7,331 / 7,724 | — | — |
| F. RAG request, route_chat | — | — | — | — | — |

¹ Search is an explicitly labelled single-tool boundary-inclusive proxy. The
existing projection does not expose an independent Java search duration.

There were no observed Java call counts or Java durations in the current
projection after suppressing its default zero values. Final response duration
was also not exposed as a measured stage. The harness records these as
unavailable rather than converting missing instrumentation into zero latency.

## 6. Critical-path breakdown

The observed execution shape is:

```text
accepted message
  -> queue admission/wait when applicable
  -> context construction
       -> conversation/tasks/executions + memory retrieval
  -> semantic interpretation
  -> route / ActionLoop LLM
  -> durable execution and MCP boundary
  -> worker/continuation when applicable
  -> terminal Run projection
```

Stage values are nested in places and must not be added as independent wall
clock durations. Across the 12 runs, representative observed p50 values were:

| Stage | Observed p50 | Observed maximum | Interpretation |
|---|---:|---:|---|
| Context construction | 12,131 ms | 19,312 ms | Major request-path cost; includes internal context waits |
| Context parallel wait | 12,026 ms | 19,294 ms | Existing context fan-out join, not a new parallelism claim |
| Memory retrieval | 6,258 ms | 13,717 ms | Dominant sub-stage inside context construction |
| Semantic interpretation | 6,648 ms | 14,136 ms | Measured on 10 runs; absent on two continuation-shaped samples |
| ActionLoop first LLM | 6,972 ms | 7,465 ms | Measured on 8 runs that entered this stage |
| ActionLoop pre-submit | 7,139 ms | 35,152 ms | The Search + Creation path has the largest multi-step delay |
| Continuation | 6,668 ms | 6,823 ms | Present on the sequential dependent samples |
| Execution submit | 107 ms | 121 ms | Small relative to queue/context/LLM costs |

Scenario-specific first bad states and bottlenecks:

- **A — Simple READ:** context and semantic preparation dominate before the
  single search tool boundary. The search proxy is about 6.8 seconds.
- **B — Simple WRITE:** context/memory/semantic preparation completes, then the
  write is rejected with `PERMISSION_DENIED: This write must be submitted
  through the durable runtime.` The trace reaches operation creation and the
  failed operation boundary; no successful write latency was measured.
- **C — Sequential dependent:** queue wait is approximately 19.2 seconds and
  continuation is approximately 6.7 seconds. The requested dependency is
  serial by definition; the run fails at the write path before a successful
  schedule result is established.
- **D — Independent multi-objective:** the run creates one operation and fails
  before proving completion or parallel scheduling. No trace reported a
  parallel signal. This is not evidence that parallel execution is safe or
  implemented.
- **E — Search + Creation:** ActionLoop pre-submit is approximately 35 seconds,
  MCP/tool time is approximately 7.3–7.7 seconds, and total E2E is about 71
  seconds. Search must precede creation because the draft consumes its result;
  the path is serial. The write then fails at the same durable admission
  boundary.
- **F — RAG request:** both runs stop at `route_chat`, with no ActionLoop, MCP,
  Java, or RAG tool call. The first bad state is route admission, before the
  canonical `community.answer_from_knowledge` chain. Static inspection also
  shows that the current Fast Path action/capability mapping does not expose
  `ANSWER_FROM_KNOWLEDGE` as a recognized single-query ActionLoop action. This
  was diagnosed only; no production change was made.

## 7. Serial versus parallel audit

| Work | Classification | Evidence / boundary |
|---|---|---|
| Simple read | Serial at user-visible completion; internal context fan-out already joins | Search result must be available before the Run can complete |
| Simple write | Serial and durable | Side effect requires ordered submission and verification |
| Draft then schedule | `SERIAL_REQUIRED` | Schedule depends on the real draft resource |
| Two independent drafts | Potentially parallelizable only after dependency/resource checks | No parallel trace signal; both samples failed before completion |
| Search then creation | `SERIAL_REQUIRED` | Creation consumes search output; no safe reordering |
| Grounded answer | One serial evidence-to-generation chain once admitted | Current Agent route did not admit the canonical RAG action |

The existing context trace contains a parallel wait, which is evidence of an
internal context fan-out rather than permission to parallelize business
Objectives. No new parallel execution was implemented or inferred.

## 8. Async and concurrency opportunities

The following are audit candidates only:

- Context repository reads and memory retrieval are I/O-shaped, but the final
  context and semantic interpretation consume their results. Preserve the
  existing context join; only independent sub-reads within that boundary are
  candidates for async I/O.
- Independent Objectives in D are structurally parallel candidates only when
  dependency resolution, resource conflict checks, and mutation ordering all
  pass. This run does not validate those preconditions.
- The sequential draft/schedule and search/creation chains must remain
  ordered.
- MCP commands requiring an immediate Java business result should remain
  synchronous.
- Long-running Agent execution remains a Durable Queue concern; converting it
  to fire-and-forget would lose the required terminal business result.
- Final response assembly remains after verified terminal state and projection;
  it is not an independent background task for this request contract.

## 9. MQ and decoupling audit

| Existing mechanism | Appropriate responsibility | Decision |
|---|---|---|
| PostgreSQL Durable Queue | Agent commands and long-running execution | `QUEUE_APPROPRIATE`, keep |
| Kafka | Business events and ES/Qdrant projections | `EVENT_APPROPRIATE`, keep |
| MCP → Java synchronous command | Immediate business result required by the caller | `SYNC_REQUIRED`, `KEEP_SYNC` |

No additional MQ is justified by this baseline. The measured queue wait is a
performance target to diagnose, not evidence that another queue should be
added.

## 10. Top three optimization targets

These are diagnosis targets for a later performance phase, not changes made in
this checkpoint:

1. **Durable write admission and queue behavior.** Sequential samples have
   approximately 19 seconds of queue wait, while write samples fail with the
   durable-runtime permission error. Establish the first bad state and fix the
   execution contract before measuring successful write latency.
2. **Context/memory/semantic preparation.** Context is roughly 10–19 seconds,
   memory is roughly 5–14 seconds, and semantic interpretation is roughly 5.5–
   14 seconds. Determine which existing fan-out/join or provider calls are
   responsible before considering async changes.
3. **Multi-step ActionLoop pre-submit.** Search + Creation reaches roughly
   35 seconds before submission and about 71 seconds E2E. Decompose the existing
   trace into dependency wait, LLM decision, and execution preparation before
   considering scheduling or objective parallelism.

Canonical RAG route admission and missing Java/final-response instrumentation
are measurement blockers to resolve separately; they are not RAG retrieval
optimization work in this phase.

## 11. Limitations

- This is a small real-E2E observation, not a load, throughput, capacity, or
  concurrency benchmark.
- Per-scenario p95 has only two observations and is descriptive.
- Eight of twelve runs failed. All B–E samples failed, so successful write and
  multi-step business latency is not established.
- The current API exposes first Run SSE activity, not provider token-level TTFT.
- Java call/latency and final response stage instrumentation were not present
  in the projection; default zeros were treated as unavailable.
- Independent search latency is only a labelled single-tool boundary proxy.
- The RAG scenario did not reach the canonical RAG tool and therefore has no
  RAG latency measurement. The accepted RAG quality baseline remains the fixed
  checkpoint in Section 1.
- Write scenarios were executed against the configured E2E account and were
  not deleted by the harness. The harness did not add a cleanup operation to
  the measured path.

## 12. Files and next recommendation

Production files changed: `0`.

Evaluation artifacts are intentionally dirty:

- `apps/backend/scripts/measure_agent_performance_baseline.py`
- `docs/evaluation/agent_performance_baseline_results.json`
- `docs/reports/AGENT_PERFORMANCE_BASELINE.md`

Recommended next step is a focused diagnosis of the durable write rejection,
followed by adding/validating Java, MCP, final-response, and route-admission
observability. Then rerun this same small scenario set. Do not add a queue, do
not implement Objective parallelism yet, and do not start Multi-Agent work from
these samples.

## 13. Final status

`AGENT_PERFORMANCE_BASELINE_COMPLETE`
