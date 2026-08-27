# AGENT_PERFORMANCE_BASELINE_VALIDATION_V2

**Branch:** `feature/hybrid-search-rag`
**Scope:** validation, tracing, and performance diagnosis only
**Primary verdict:** `PRODUCTION_ROUTING_ISSUE`
**Secondary findings:** `SEMANTIC_BENCHMARK_INVALID` for D; `OBSERVABILITY_INSUFFICIENT` for a few cross-process stages
**Business-logic optimization:** none

## 1. Checkpoints and scope

The V1 performance artifact was checked with `git status` and `git diff --check`,
then committed and pushed as:

```text
2b593fa test: add agent performance baseline
```

The accepted RAG checkpoint remains:

```text
7e4455a
```

No RAG experiment was run in this phase. The accepted RAG baseline is still:

| Metric | Baseline |
|---|---:|
| Post Recall@10 | 0.744444 |
| Conditional Chunk Recall@10 | 0.307692 |
| Final Evidence Recall@10 | 0.251852 |
| Production semantic generation coverage | approximately 0.333 |
| Faithfulness | approximately 1.000 |
| Citation correctness | approximately 1.000 |
| No-answer accuracy | 5/5 |
| Hallucination | approximately 0 |

The V2 harness used the public boundary:

```text
Java login
  -> POST /api/v1/agent/conversations/{conversation_id}/messages
  -> Run polling / Run SSE
  -> existing run projection and debug trace
```

Each scenario used a fresh conversation and the configured authenticated E2E
account. No internal Agent service or Java business tool was called directly by
the harness.

## 2. Scenario validation: expected versus actual path

Each scenario first received one validation run. `path_valid` means that the
observed path was sufficient for the scenario's intended measurement; it does
not claim that every internal stage has a dedicated span.

| ID | Expected path | Actual path / observed tool | First divergence | Validation |
|---|---|---|---|---|
| A Simple READ | `POST /messages -> TurnCoordinator -> Interpreter -> Complex/ActionLoop -> MCP read -> Java -> terminal Run` | `POST /messages -> TurnCoordinator -> Interpreter -> COMPLEX -> ActionLoop -> MCP -> Java`; `community.search_public_posts` | none | valid |
| B Simple WRITE | `POST /messages -> TurnCoordinator -> Interpreter -> ActionLoop -> Durable Runtime -> MCP write -> Java -> verified draft` | `POST /messages -> TurnCoordinator -> Interpreter -> COMPLEX -> ActionLoop -> Durable Runtime -> MCP -> Java`; `content.create_draft` | none | valid |
| C Sequential dependency | `POST /messages -> TurnCoordinator -> Interpreter -> ActionLoop -> Durable Runtime draft -> continuation -> Durable Runtime schedule` | `POST /messages -> TurnCoordinator -> ORCHESTRATED -> ActionLoop -> Durable Runtime -> Continuation -> MCP -> Java`; trace contains `GENERATE_CONTENT -> SCHEDULE_PUBLISH` | none | valid |
| D Independent objectives | `POST /messages -> TurnCoordinator -> Interpreter -> ActionLoop -> two independent Objectives -> Durable Runtime -> MCP/Java writes` | one Objective, two serial `GENERATE_CONTENT` operations, no parallel signal | `SEMANTIC_BENCHMARK_INVALID` | invalid |
| E Search + Creation | `POST /messages -> TurnCoordinator -> ActionLoop -> MCP search -> Observation -> ActionLoop -> Durable Runtime write` | `POST /messages -> TurnCoordinator -> Interpreter -> COMPLEX -> ActionLoop -> MCP -> Java`; tools `community.search_public_posts`, `content.create_draft` | none | valid |
| F RAG grounded query | `POST /messages -> TurnCoordinator -> Interpreter -> ANSWER_FROM_KNOWLEDGE -> MCP -> Java Hybrid/RAG -> generation/citation` | `POST /messages -> TurnCoordinator -> Interpreter -> COMPLEX -> ActionLoop -> MCP -> Java`; tools `community.search_public_posts`, `community.get_post`; no `community.answer_from_knowledge` | `ANSWER_FROM_KNOWLEDGE_ADMISSION` | invalid |

Validation totals:

| Measure | Count |
|---|---:|
| Scenarios validated | 6 |
| Completed validation runs | 5 |
| Failed validation runs | 1 |
| Path-valid scenarios | 4 |
| Path-invalid scenarios | 2 |
| Formal measurement scenarios admitted | A, B, C, E |

The formal measurement therefore deliberately contains only the four valid
scenario classes. D and F were not mixed into latency percentiles.

## 3. Durable write diagnosis

### Evidence

The original V1 write failures returned:

```text
PERMISSION_DENIED:
This write must be submitted through the durable runtime.
```

The harness had already entered through the public message endpoint. It did not
call `content.create_draft` directly. The first bad state was the MCP durable
side-effect admission check: the local MCP process did not have the same
`GREENBOOK_MCP_RUNTIME_TOKEN` as the Agent runtime context.

After the ignored local `.env` was configured with the shared runtime token and
the service was restarted, B, C, and E produced the expected traces:

```text
operation_created
  -> operation_claimed
  -> MCP / Java execution
  -> operation_completed
```

The formal measurement then completed all 12 admitted samples. The write
permission policy itself was not relaxed or changed.

### Classification

```text
FIRST_BAD_STATE = DURABLE_SIDE_EFFECT_ADMISSION
FAILURE_FAMILY = TEST_ENVIRONMENT_ISSUE
```

This is not a benchmark internal-service bypass, because the harness used the
public POST boundary. It is not a confirmed routing regression or an auth
identity loss, because the same canonical path succeeds once the required local
runtime admission configuration is present.

### Production-path answer

Normal public write requests currently reach the Durable Runtime correctly. The
successful B/C/E traces are the focused regression evidence for that conclusion.

## 4. RAG routing diagnosis

The V2 validation used a natural request equivalent to:

> 鏍规嵁 GreenBook 绀惧尯閲岀幇鏈夊叧浜?Agent Memory 鐨勫笘瀛愶紝鎬荤粨澶у涓昏璁ㄨ浜嗗摢浜涢棶棰橈紝骞剁粰鍑哄搴旂殑绀惧尯璇佹嵁銆?
This was not an internal `RAG` keyword trigger. The latest targeted trace
recorded:

```text
semantic_action = SEARCH_AND_SUMMARIZE
capabilities    = SEARCH_COMMUNITY, ANSWER_FROM_KNOWLEDGE
route           = route_complex
```

The request then entered ActionLoop and made eight MCP attempts in the failed
validation run:

```text
community.search_public_posts
community.get_post
community.search_public_posts
community.search_public_posts
community.get_post
community.get_post       (INVALID_TOOL_ARGUMENT)
community.get_post
community.get_post
```

There was no `community.answer_from_knowledge` call, no RAG retrieval span, and
no grounded generation/citation stage. The run ended with
`ACTION_LOOP_NO_PROGRESS`.

Static inspection of
[`fast_path_gate.py`](../../packages/agent_core/greenbook_agent_core/turn/fast_path_gate.py)
shows that the capability-to-action mapping does not map
`ANSWER_FROM_KNOWLEDGE`, while `SEARCH_AND_SUMMARIZE` is not included in the
non-fast action set used by action extraction. That is consistent with the
observed admission failure. This is a production routing diagnosis only; the
file was not changed in V2.

The initial V1 observation was `route_chat` with zero tool calls. The targeted
V2 trace is more precise: after minimal observability was added, the natural
request reaches `route_complex`, but still fails before canonical RAG admission.

```text
FIRST_BAD_STATE = ANSWER_FROM_KNOWLEDGE_ADMISSION
FAILURE_FAMILY = PRODUCTION_ROUTING_ISSUE
```

The F scenario has no valid RAG performance measurement. `RAG latency =
unavailable`, not zero.

## 5. Independent Objective audit

D did not establish a two-Objective workload:

| Field | Observation |
|---|---|
| `objective_count` | 1 |
| `dependencies` | unavailable/null in the run projection |
| `resource_bindings` | unavailable/null in the run projection |
| `execution_order` | one `CREATE_DRAFT` step in the semantic projection |
| Trace operations | two `GENERATE_CONTENT` operations executed serially |
| Parallel signal | none |

The two user-requested drafts were not decomposed into two independent
Objectives. Therefore the first bad state is
`SEMANTIC_BENCHMARK_INVALID`, not evidence that the Objective scheduler failed
to parallelize. A valid two-Objective case must be prepared and revalidated
before measuring parallelism.

## 6. Formal V2 sampling

After validation, A/B/C/E each ran three successful measurements. All 12
formal samples completed. Failed or semantically invalid validation runs were
kept separate and excluded from latency percentiles.

All times are milliseconds. Per-scenario `p95` has `n=3` and is descriptive
only; these values are not SLO estimates.

| Scenario | n | E2E p50 / p95 | Server total p50 / p95 | TTA p50 / p95 | LLM calls p50 / p95 | Input tokens p50 / p95 | Output tokens p50 / p95 | ActionLoop iterations p50 / p95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A Simple READ | 3 | 38,776 / 46,349 | 38,592 / 46,116 | 11,241 / 11,924 | 2 / 2 | 7,231 / 7,235 | 333 / 348 | 2 / 2 |
| B Simple WRITE | 3 | 44,143 / 47,691 | 43,855 / 47,415 | 11,490 / 12,235 | 2 / 2 | 7,485 / 7,487 | 816 / 880 | 1 / 1 |
| C Sequential dependency | 3 | 55,824 / 56,361 | 55,533 / 56,062 | 10,950 / 12,256 | 2 / 2 | 7,072 / 7,073 | 1,230 / 1,247 | 2 / 2 |
| E Search + Creation | 3 | 61,792 / 86,641 | 72,926 / 86,300 | 10,846 / 12,416 | 3 / 3 | 10,784 / 10,840 | 1,824 / 1,953 | 2 / 2 |
| Mixed admitted set | 12 | 50,413 / 86,641 | 50,129 / 86,300 | 11,305 / 12,416 | 2 / 3 | 7,360 / 10,840 | 890 / 1,953 | 2 / 2 |

The mixed aggregate is descriptive only because it combines different
workloads. The E server projection being greater than observed E2E on some
samples is a projection/measurement discrepancy, not a claim that the stage
ran longer than wall clock.

Component measurements for the admitted set:

| Scenario | Queue metric p50 / p95* | Semantic/interpreter p50 / p95* | Memory p50 / p95* | MCP/tool p50 / p95* | Java p50 / p95* | Final response p50 / p95* | Search p50 / p95* | RAG |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A | 484 / 508 | 6,157 / 6,263 | 6,017 / 6,637 | 907 / 6,759 | 91 / 91 | unavailable | 907 / 6,759 | unavailable |
| B | 160 / 264 | 6,273 / 6,318 | 5,836 / 6,094 | 116 / 7,833 | 62 / 77 | 3 / 4 | unavailable | unavailable |
| C | 17,279 / 19,091 | unavailable in continuation projection | 5,635 / 6,289 | 6,933 / 8,312 | 113 / 114 | 0 / 1 | unavailable | unavailable |
| E | 143 / 225 | 6,420 / 7,481 | 5,967 / 18,689 | 6,658 / 7,765 | 254 / 256 | 3 / 4 | unavailable as an independent span | unavailable |
| Mixed admitted set | 244.5 / 19,091 | 6,263 / 7,481, n=9 | 5,943 / 18,689 | 3,783 / 8,312 | 99.5 / 256 | 3 / 4, n=9 | 907 / 6,759, n=3 | unavailable |

\* Per-scenario values have three samples. `unavailable` means no trustworthy
value was exposed; it is not zero.

Java latency is the measured Java boundary duration bridged through MCP. Java
start/end timestamps do not yet reach the Agent API process, so
`execution_to_java_ms` and `projection_after_java_ms` remain unavailable.

## 7. Tracing coverage and critical path

The observed critical path is:

```text
accepted POST
  -> admission / runner wait when applicable
  -> context construction
       -> conversation/tasks/executions + memory retrieval
  -> semantic interpretation
  -> route / ActionLoop LLM
  -> durable submit when side effect is required
  -> queue claim / worker execution
  -> MCP
  -> Java
  -> observation / continuation
  -> terminal Run projection
  -> final response read
```

Stage values are nested and must not be added as independent wall-clock
durations.

| Required stage | Coverage | Evidence / limitation |
|---|---|---|
| `TURN_TOTAL` | partial | observed E2E and server total projection; no universal explicit span |
| `CONTEXT_BUILD` | observed | context timestamps and stage durations |
| `MEMORY_RETRIEVAL` | observed | repository search and memory retrieval stages |
| `INTERPRETER` | observed on n=9 | semantic LLM duration; continuation-shaped projections omit it |
| `SEMANTIC_CONFIRMATION` | unavailable | no dedicated confirmation span; observed confirmation count is zero |
| `TARGET_RESOLUTION` | unavailable | no dedicated trustworthy span |
| `TEMPORAL_RESOLUTION` | unavailable | no dedicated trustworthy span |
| `ACTION_LOOP_TOTAL` | observed | ActionLoop start/finish and iteration projection |
| `ACTION_LOOP_LLM` | observed | per-call `llm_events` |
| `DURABLE_SUBMIT` | observed | execution submit and `operation_created` |
| `QUEUE_WAIT` | partial/definition-sensitive | API-to-runner metric and operation-created-to-claimed timestamps are both available, but they are different intervals |
| `WORKER_EXECUTION` | partial | claim/completion trace exists; independent worker span is not present for every operation |
| `MCP_CALL` | observed | MCP call trace with tool and duration |
| `JAVA_CALL` | observed | cross-process MCP metadata bridge |
| `RAG_RETRIEVAL` | unavailable | F never entered canonical RAG |
| `RAG_GENERATION` | unavailable | F never entered canonical RAG |
| `FINAL_RESPONSE` | partial | B/C/E measured at 0鈥? ms; A and some projections have no stage value |

### Context and semantic preparation

The prior 10鈥?9 second label 鈥渃ontext/semantic preparation鈥?can now be split:

| Layer | Observation |
|---|---|
| `PRE_LLM_CONTEXT` | context p50 11,442 ms; maximum 24,380 ms; context parallel-wait p50 11,423 ms and maximum 24,363 ms |
| `MEMORY_RETRIEVAL` | p50 5,943 ms; maximum 18,689 ms; it is nested inside context preparation |
| `INTERPRETER_LLM` | p50 6,263 ms, n=9; maximum 7,481 ms |
| `POST_LLM_SEMANTIC` | route/target/temporal resolution does not have a complete independent span; do not treat missing values as 0 ms |
| Prompt assembly | approximately 4鈥? ms in the stage records |

The large context number is therefore not merely string formatting. It includes
existing repository/context waits and the memory retrieval join. The trace does
not yet prove which underlying I/O should be changed.

### ActionLoop and Search + Creation

E is the clearest serial critical path. Across its three formal samples:

| Stage | Observed |
|---|---:|
| ActionLoop iterations | 2 each |
| Total LLM calls | 3 each: one semantic plus two ActionLoop calls |
| Semantic LLM | 5,731鈥?,477 ms |
| ActionLoop LLM 1 | 6,204鈥?,565 ms |
| ActionLoop LLM 2 | 7,779鈥?4,554 ms |
| Search tool trace | 796鈥?,649 ms |
| Pre-submit time | 25,538 / 36,045 / 35,489 ms; p50 35,489 ms |
| Durable submit | 92鈥?05 ms |

The trace order is search, observation, second ActionLoop decision, then draft
submission. Search and creation are `SERIAL_REQUIRED` because creation consumes
the search result. The evidence supports a multiple-LLM plus tool wait
bottleneck; it does not prove that either ActionLoop iteration is unnecessary.

The failed F validation is a separate `UNNECESSARY_LOOP` candidate: it made
nine LLM calls and eight MCP attempts while repeatedly searching/getting posts
without admitting the canonical answer action. This should be corrected as a
routing issue before any loop optimization is considered.

## 8. Queue wait diagnosis

There are two different intervals in the data:

1. `queue_wait_ms` in the harness is `api_received -> runner_started`. It is a
   broad admission/runner interval, not necessarily Durable Queue claim wait.
2. The durable operation interval is `operation_created -> operation_claimed`.

For the admitted set, the first metric is p50 244.5 ms and p95 19,091 ms. The
19-second C value is not a clean durable queue measurement: in the same C
record, `context_start` precedes the projected `worker_claimed/runner_started`
timestamp. It therefore includes or is affected by runner/admission/context
projection timing. Do not label the full 17鈥?9 seconds as worker queue delay.

The actual durable operation traces show:

| Interval | Observation |
|---|---:|
| Draft `operation_created -> operation_claimed` | approximately 5.1鈥?.2 s across the C operations |
| Worker claim -> worker start | 0鈥? ms in the available projection |
| C draft/schedule operation claim -> completion | approximately 1.2鈥?4.5 s per operation, depending on the operation/tool path |
| E draft `operation_created -> operation_claimed` | approximately 7.0鈥?.4 s |

Representative C validation timestamps are UTC and demonstrate the event
ordering without relying on a derived percentile:

| Event | Timestamp |
|---|---|
| API received | `14:14:00.528215Z` |
| Context start | `14:14:00.879118Z` |
| Worker/runner claimed and started | `14:14:19.619560Z` |
| Draft operation created | `14:14:26.226141Z` |
| Draft operation claimed | `14:14:32.701237Z` |
| Draft tool step completed | `14:14:44.568806Z` |
| Draft operation completed | `14:14:44.667198Z` |
| Continuation started | `14:14:44.822413Z` |
| Schedule operation created | `14:14:44.845888Z` |
| Schedule operation claimed | `14:14:52.977654Z` |
| Schedule operation completed | `14:14:54.461724Z` |
| Turn completed | `14:14:56.184569Z` |

The available trace does not expose an independent `worker_start` and tool
invoke timestamp for every continuation operation, and the final run
projection exposes only the first execution for C. This is an observability
limitation, not evidence for changing worker polling or capacity.

## 9. TTA and TTFT

The benchmark now reports:

```text
TTA  = time from accepted POST response to first Run SSE activity or first
       non-accepted Run state
TTFT = unavailable unless provider/model token timestamps are exposed
```

TTA for the admitted 12 samples is p50 11,304.727 ms and p95 12,415.527 ms.
There is no genuine provider-token TTFT in this run. The old first SSE activity
value is retained as TTA and is not called TTFT.

## 10. Async and parallelization audit

No parallel implementation was made.

| Work | Classification | Evidence |
|---|---|---|
| Context repository reads / memory retrieval | `ASYNC_IO` candidate inside the existing context join | I/O-shaped and already represented by a context parallel-wait stage; dependency is satisfied only after the join |
| Simple read | serial at user-visible completion | result must be available before terminal response |
| Simple write | `SERIAL_REQUIRED` | side effect requires durable submission and verification |
| Draft then schedule | `SERIAL_REQUIRED` | schedule depends on the real draft resource |
| D two independent drafts | potential `PARALLELIZABLE_OBJECTIVES`, not established | the request produced one Objective, so dependency/resource checks cannot be evaluated |
| Search then creation | `SERIAL_REQUIRED` | creation consumes search output |
| RAG evidence then generation | serial once admitted | F did not reach the chain |

The context parallel wait is an existing internal fan-out/join signal, not a
claim that business Objectives may be parallelized. D must be fixed and
revalidated before any scheduler conclusion.

## 11. MQ and decoupling audit

| Mechanism | Classification | Decision |
|---|---|---|
| PostgreSQL Durable Queue | `QUEUE_APPROPRIATE` | keep for durable/long-running Agent execution |
| Kafka | `EVENT_APPROPRIATE` | keep for asynchronous business events and ES/Qdrant projections |
| MCP -> Java command | `SYNC_REQUIRED`, `KEEP_SYNC` | immediate business result is required by the caller |

```text
MQ changes = 0
```

The queue/admission intervals justify better measurement and later diagnosis;
they do not justify adding another queue or converting synchronous MCP commands
to fire-and-forget work.

## 12. Top three later optimization targets

These are diagnosis targets only. They were not implemented in V2.

1. **Context, memory, and semantic preparation.** Context is about 11.4 s at
   p50 and reaches 24.4 s in the small sample; memory is about 5.9 s at p50
   and reaches 18.7 s. The existing join and provider calls need a more precise
   breakdown before any async change.
2. **Serial ActionLoop/provider reasoning.** Search + Creation spends about
   35.5 s before draft submission and makes two ActionLoop LLM calls after the
   semantic call. This is a real critical-path cost, but the search dependency
   means it is not automatically parallelizable.
3. **Durable admission and operation claim timing.** C shows a broad
   API-to-runner interval of 16.8鈥?9.1 s and operation-created-to-claimed
   intervals of roughly 5.1鈥?.2 s. First repair the interval definitions and
   confirm worker behavior before changing queue or lease settings.

Java itself is not a top-three bottleneck in this sample: the bridged Java
boundary is p50 99.5 ms and p95 256 ms across the admitted set. The missing
canonical RAG route is a correctness blocker, not a measured RAG performance
target.

## 13. Files changed and worktree state

Eight production files were changed only to expose or preserve measurement
spans/metadata. They do not change retrieval, routing policy, business
permissions, execution semantics, or queue behavior:

- `apps/agent_api/greenbook_agent_api/api/routes.py`
- `apps/agent_api/greenbook_agent_api/services/action_loop_executor.py`
- `apps/agent_api/greenbook_agent_api/services/turn_coordinator.py`
- `packages/agent_core/greenbook_agent_core/observability/run_metrics.py`
- `packages/java_client/greenbook_java_client/client.py`
- `services/greenbook_mcp/greenbook_mcp_server/client.py`
- `services/greenbook_mcp/greenbook_mcp_server/protocol.py`
- `services/greenbook_mcp/greenbook_mcp_server/server.py`

The measurement harness change is:

- `apps/backend/scripts/measure_agent_performance_baseline.py`

The final V2 artifacts are intentionally dirty:

- `docs/reports/AGENT_PERFORMANCE_BASELINE_V2.md`
- `docs/evaluation/agent_performance_baseline_v2_results.json`

The local runtime token is in the ignored `.env` only and is not part of the
worktree diff.

## 14. Final verdict

```text
PRODUCTION_ROUTING_ISSUE
```

The evidence is sufficient to identify a real production routing issue for the
natural community-evidence request: the semantic state contains
`ANSWER_FROM_KNOWLEDGE`, but the canonical answer capability is not admitted,
and the request falls into a repeated search/get-post ActionLoop instead.

The performance benchmark is valid only for the admitted subset A/B/C/E:

```text
12 / 12 formal samples completed
4 / 4 admitted scenario classes path-valid
```

It is not a valid six-scenario benchmark because D was not decomposed into two
Objectives and F did not reach RAG. The missing Java start/end propagation,
continuation execution projection, final-response coverage for A, and true
provider TTFT keep the observability result partial.

## 15. Next recommendation

Stop performance optimization at this checkpoint.

1. Separately diagnose and minimally fix/validate the
   `ANSWER_FROM_KNOWLEDGE` admission mapping, then rerun only F through the
   same public boundary to establish real RAG timing.
2. Prepare a benchmark request that the existing Interpreter demonstrably
   decomposes into two independent Objectives; revalidate D before considering
   parallel execution.
3. If performance work resumes, first complete the missing worker/tool,
   continuation, Java start/end, final-response, and provider-token timing
   spans, then repeat the same small A鈥揊 validation/measurement protocol.

Do not add MQ, implement Objective parallelism, start Multi-Agent work, or
perform a broad async refactor from this dataset.

## 16. Status

`AGENT_PERFORMANCE_BASELINE_VALIDATION_V2_COMPLETE`
