# GreenBook Control-Plane Closeout

Date: 2026-08-21  
Scope: architecture closeout, CORE capability acceptance, and isolated
lightweight Commitment/WorkItem control-plane POC.

The report is based on the current source wiring and tests. It does not treat
historical architecture documents as runtime evidence. Docker/Java/Agent API/
Frontend were not running in this workspace, so no live business acceptance
is claimed.

## 1. CURRENT PRODUCT CAPABILITY MATRIX

The detailed matrix is [PHASE_CONTROL_PLANE_CAPABILITY_MATRIX.md](PHASE_CONTROL_PLANE_CAPABILITY_MATRIX.md).

CORE conclusions:

- `SEARCH_POSTS`, `GET_POST`, Draft create/revise, publish-now, schedule,
  schedule update/cancel, post delete, target clarification, temporal
  clarification, HITL delete, `RESULT_UNKNOWN` and analytics/comments have
  real Tool + Java contracts.
- `SUMMARIZE`, unified account content management, multi-objective, cross-turn
  versioning and post-publish notification are `PARTIAL` at product acceptance
  level because the dedicated contract or live evidence is incomplete.
- Long-term operations are `MISSING`; Java's publication scheduler is not an
  Agent long-running operations service.
- Hot search is real Java behavior (`hotScore` uses recency/engagement), but
  diversity, richer quality signals and a dedicated hotspot analysis remain a
  GAP.

## 2. CURRENT ACTIVE ARCHITECTURE

The current production composition root is `apps/agent_api/greenbook_agent_api/main.py`:

```text
Frontend
  -> POST /api/v1/agent/conversations/{id}/messages
  -> durable AgentRun(ACCEPTED)
  -> AgentRunner claims the Run
  -> TurnCoordinator
       -> bounded ContextAssembler
       -> one CommandInterpreter pass
       -> TargetResolver + TemporalResolver
       -> ResolvedSemanticState
       -> FastPathGate
          |-- CHAT / CLARIFY / simple READ
          |     -> FastPathExecutor -> MCP ToolRuntime -> Java READ
          |-- simple WRITE
          |     -> ActionLoop write boundary
          |-- COMPLEX
                -> ActionLoopExecutor
                -> one typed semantic ActionDecision per iteration
                -> Objective-scoped ResourceBinding + Guard
  -> ConversationRuntimeAdapter
       |-- READ: execute_fast_path_read -> MCP -> Java
       |-- WRITE: submit_fast_path_write / submit_tool
                -> ExecutionInput -> RuntimeAgentService
                -> ExecutionRepository + OperationLedger
                -> Queue/Worker lease/fencing/checkpoint/retry
                -> MCP ToolRuntime -> JavaClient -> Java DB
  -> Java verification / ToolResult / Observation
  -> Objective satisfaction + Run aggregation
  -> Activity/FinalResponse projection -> SSE/polling -> Frontend
```

Path classification:

| Path | Current status | Evidence |
| --- | --- | --- |
| TurnCoordinator + FastPath + ActionLoop | ACTIVE | `main.py`, `services/turn_coordinator.py`, `services/action_loop_executor.py` |
| Durable queue/worker/runtime | ACTIVE | `RuntimeAgentService`, `ExecutionQueueWorker`, `OperationLedger`, worker reconciliation wiring |
| `ConversationRuntimeAdapter` Task/Goal snapshot compatibility | LEGACY COMPATIBILITY | historical recovery/test/repair callers remain; it is not the active TurnCoordinator decision owner |
| `GoalTree` / `GoalCompiler` / old dynamic planning | LEGACY COMPATIBILITY | retained until caller-level retirement evidence; not used as the current API's main control path |
| direct dispatch | CONFIGURATION FALLBACK | local in-memory profile only; it uses the same Runtime submission boundary, not a second business runtime |
| old write fallback after new-path failure | NOT ACTIVE | `_capability_requires_legacy` does not mask a new-path failure with the old AgentLoop |

The stale topology in `docs/architecture/CURRENT_ARCHITECTURE.md` was corrected
to reflect this wiring. Durable Runtime was not rewritten.

## 3. SOURCE OF TRUTH MAP

| State | Owner | Projection | Duplicate? |
| --- | --- | --- | --- |
| User business commitment in current production | `Task` / `Objective` desired state, created from the validated `Command` | Understanding Activity, Run, Frontend | `Command` is transient input; no second persisted business outcome |
| B Commitment/WorkItem | Isolated POC only; not production state | POC tests only | No production duplicate; must not become a second store during migration |
| Resource ownership | Objective-scoped `related_resource_ids` plus Task resource index/ResourceBinding contract | Activity/result cards | Index and owner are two views of one binding, not separate business truth |
| Execution progress | Durable `Execution` repository/state manager | Run status, Execution API, Activity | Run does not re-evaluate execution truth |
| Human authorization | `ApprovalRuntimeService` / approval store | `WAITING_APPROVAL` Activity and approval card | No string-based `WAITING_FOR_HUMAN` multiplexing in the risk path |
| External side-effect call | `OperationLedger` / external operation store | Operation receipt, Observation, Activity | One claim/status record per external operation |
| Draft / Schedule / Post / publication / notification | Java DB and Java services | ToolResult, verification evidence, Run/Frontend projection | Java is the only business fact owner |
| Run aggregate | `AgentRun`/Run projection and completion reconciliation | API, SSE, Frontend | Projection only; never a second Draft/Schedule/Post state source |

## 4. KEEP / MODIFY / DELETE

### KEEP

Keep `Task`/`Objective` persistence, `TargetResolver`, `TemporalResolver`,
FastPath, ActionLoop, ToolRegistry, ToolRuntime, Durable Runtime, queue/worker,
lease/fencing, checkpoint/resume, OperationLedger, idempotency, retry,
`RESULT_UNKNOWN`, reconciliation, ResourceBinding, Approval, Observation,
Java verification and FinalResponse/Activity projection.

### MODIFY

- Keep the current production A path and apply only the thin Guard and
  completion corrections needed by real invariants.
- Use the isolated B adapter to test minimal Commitment projection, semantic
  confirmation, freeze and supersede behavior before adding production
  persistence/API.
- Keep canonical schedule time in Objective/Resolver facts; model-proposed
  schedule time is never authoritative.
- Keep synthesis completion behind a verified final Artifact; a search
  candidate or detail resource alone cannot complete a grounded summary.
- Keep JavaClient independent of Agent Core; optional Agent metrics must be
  emitted at the runtime boundary, not by the shared downstream client.

### DELETE / DEPRECATE

- No new core table, scheduler, Runtime, graph, planner or Tool was added.
- No unrelated dirty files were deleted or reset.
- Mark GoalTree/GoalCompiler/old AgentLoop callers as compatibility-only and
  retire them only after a caller audit. Do not remove them as part of this POC.

Change record:

| File / area | Why | Phase |
| --- | --- | --- |
| `docs/architecture/CURRENT_ARCHITECTURE.md` | Replace stale active topology with observed wiring | 0 |
| `docs/audit/PHASE_CONTROL_PLANE_CAPABILITY_MATRIX.md` | Freeze real CORE/EXTENSION capabilities and gaps | 0/1 |
| `packages/evaluation/.../business_semantic_seeds.py` and tests | Freeze variant/counterfactual evaluation oracle | 2 |
| `packages/agent_core/.../turn/commitment_poc.py` and tests | Isolated B Commitment/WorkItem projection | 4 |
| `actionloop/loop.py`, `actionloop/qualification.py` | Thin pre-write Guard, deterministic singleton handling, bounded decision behavior | 1/5 |
| `task/objective_reducer.py` | Prevent search/detail resources from completing grounded synthesis | 1/5 |
| `packages/java_client/.../client.py` | Remove downstream client -> Agent Core dependency | 1 |
| schedule-related unit fixtures | Add missing canonical time facts to old tests | 5 |

No persistent schema was changed.

## 5. COMMON RUNTIME CONTRACTS

| Contract | Current behavior |
| --- | --- |
| TargetResolver | `NOT_FOUND` for 0, `RESOLVED` for 1, `AMBIGUOUS` for >1; no latest/active-target guessing; writes stop before resolution |
| TemporalResolver | Natural language -> canonical timezone-aware future `run_at`; invalid/past/unresolved future remains unresolved; unresolved future cannot fall back to publish-now |
| ToolRuntime | Typed tool registry and ToolResult boundary; MCP handlers call JavaClient; READ may be synchronous |
| DurableRuntime | `ExecutionInput` -> Runtime submission -> repository/queue/worker -> lease/fencing/checkpoint/retry/resume; existing runtime retained |
| OperationLedger | Claims external side effects and stores status; `RESULT_UNKNOWN` goes to reconciliation, never blind retry |
| ResourceBinding | Objective-scoped resource identity; Draft/Schedule/Post cannot be borrowed from another Objective |
| Approval | Durable risk authorization and resume; delete/publish/reply are not silently executed without the required boundary |
| Observation | Typed success/failure/waiting evidence with resource/operation identity; internal rejection includes code/action/objective/retryable detail |
| JavaClient | One downstream boundary for Java Agent Facade; maps validation, authorization, conflict, unavailable and unknown-result states |

## 6. EVALUATION BASELINE

### Current A observation

Reused from `docs/reports/assistant-runtime-baseline.json` (observed runtime
report, generated 2026-07-30; not a semantic benchmark):

| Metric | A observed value |
| --- | ---: |
| Runs / completed / failed / waiting approval | 30 / 20 / 8 / 2 |
| Run completion rate | 66.67% |
| Average model calls | 3.17 |
| Average tool calls | 1.40 |
| End-to-end latency p50 / p95 | 12.301s / 105.407s |
| Retry recovery rate | 100% (4/4) |
| Resumed runs | 0 |
| Semantic accuracy / target binding / temporal binding | NOT MEASURED |
| Wrong target / duplicate write / unsafe write | NOT LABELED in this report |

### Frozen evaluation contract

- Existing semantic baseline: 16 categories × 5 expressions = 80 cases.
- Existing Business Acceptance Set: 50 clear-language cases.
- New frozen Seed set: 5 Seeds × 7 expressions = 35 cases, including explicit
  counterfactual binding flips. Expected values are hand-authored.
- Full local regression: `1346 passed, 22 skipped, 1 pytest cache permission warning`.
- Static checks for the newly added/changed control-plane files: Ruff PASS.

The same-model/same-Java A/B run was not executed. Docker failed to connect to
the Linux engine, and ports 8080, 8094 and 5173 were all not listening. The
baseline therefore remains an observation, not a migration score.

## 7. B POC IMPLEMENTATION

Files added/changed:

- `packages/agent_core/greenbook_agent_core/turn/commitment_poc.py` — 531 LOC;
  minimal `DesiredOutcome`, `WorkItem`, Commitment status/version, HITL type,
  deterministic renderer, freeze/supersede/revalidation and Objective adapter.
- `tests/unit/test_commitment_poc.py` — 184 LOC, 7 focused tests.
- `packages/evaluation/greenbook_evaluation/cases/business_semantic_seeds.py` —
  208 LOC; `tests/unit/test_business_semantic_seeds.py` — 34 LOC.
- `turn/__init__.py` exports the POC types; no production route imports them.
- `ActionLoop`, Guard and Objective reducer receive only minimal correctness
  fixes; the Durable Runtime is reused.

| Measure | B POC result |
| --- | --- |
| Production main-path wiring | 0 lines; feature is isolated |
| New POC code | 531 LOC |
| POC test code | 184 LOC |
| New evaluation contract code | 208 LOC + 34 LOC tests |
| New persistent schema/table | 0 |
| New Runtime/queue/worker/scheduler | 0 |
| New Resource model | 0; adapter uses Objective/ResourceBinding |
| New abstraction count | 1 isolated Commitment POC module; no second execution engine |
| `B_POC_TOO_COUPLED` | NO |

The POC is deliberately not a production migration. A production Commitment
would need a backend persistence/API/frontend confirmation contract and a
measured migration gate first.

## 8. HITL

| HITL type | Current owner | Status |
| --- | --- | --- |
| Clarification | TargetResolver/TemporalResolver -> TurnCoordinator -> user-facing ASK_USER/WAITING_HUMAN projection | Working in current path; no write before resolution |
| Semantic Confirmation | B POC `HITLType.SEMANTIC_CONFIRMATION`, deterministic `render_confirmation` | POC only; not production-wired or persisted |
| Risk Approval | ApprovalRuntimeService -> durable approval -> `WAITING_APPROVAL` -> resume | Working contract; live Java/Frontend proof blocked |
| Async pending | Execution/Run/Activity status | Existing Durable Runtime boundary |
| RESULT_UNKNOWN reconciliation | OperationLedger + ReconciliationWorker | Existing Durable Runtime boundary |

The POC does not collapse these types into a stringly-typed
`WAITING_FOR_HUMAN` state.

## 9. WORKITEM / COMMITMENT MODEL

### Current production model

Production continues to use `Task` + `Objective`. `Objective` remains the
persisted work envelope and `ObjectiveStateReducer` is the deterministic
completion authority. Capabilities are derived/used at the Objective adapter
boundary; Java resources and verification facts decide completion.

### B POC model

`WorkItem` fields:

```text
work_item_id
commitment_version
supersedes?
parent_work_item_id?       # trace only
source_span
subject?
desired_outcome
target_reference?
resolved_target_ref?
temporal_expression?
canonical_run_at?
execution_requirements?    # only evidence_required
resource_refs
status
```

`Commitment` adds `commitment_id`, `commitment_version`,
`supersedes_version`, `source_message_id`, `work_items`, and one of
`DRAFT/CONFIRMED/FROZEN/SUPERSEDED` states. `FrozenCommitment` is immutable in
the POC. A cross-turn change creates version 2 and marks version 1
`SUPERSEDED`; it does not mutate the frozen outcome.

`desired_outcome` is limited to final business results such as `DRAFT`,
`PUBLISHED`, `SCHEDULED`, `REVISED`, `SCHEDULE_UPDATED`,
`SCHEDULE_CANCELLED`, `DELETED` and `SEARCH_RESULT`. It does not store
`first_action`, plan steps, tool sequence, DAG metadata, tone, style or content
prompt data. The adapter derives the existing Objective capability list only
when handing execution to the current A runtime.

## 10. TOOL-FIRST LOOP

Actual decision path:

1. ContextAssembler supplies bounded active Objective/resource/observation
   facts.
2. Qualification computes a small allowed action set from deterministic facts.
3. Control singletons (`CLARIFY`, `WAIT`, `FINISH`) are materialized without a
   second model call.
4. Otherwise the model returns one typed `ActionDecision`/semantic ToolCall;
   resolver-owned capability/tool mapping is deterministic.
5. Existing verified Draft -> Schedule continuation and search candidate ->
   deterministic GET_POST evidence acquisition avoid unnecessary re-selection.
6. Action/Observation is recorded against the current Objective; the loop
   resumes, waits, completes or fails within bounded iteration/tool/LLM budgets.

The existing disposable `TaskPlan` projection is used for Objective readiness
and dependency compatibility; B does not compile a static workflow or add a
new Planner Graph. The next action is still selected from current facts.

## 11. WRITE SAFETY

```text
FastPath/ActionLoop
  -> TargetResolver + TemporalResolver facts
  -> thin ActionGuard immediately before write submit
  -> ConversationRuntimeAdapter
  -> ExecutionInput / RuntimeAgentService
  -> OperationLedger + durable queue/worker
  -> MCP typed ToolRuntime
  -> Java Agent Facade
  -> Java verification / ToolResult
  -> ResourceBinding / ObjectiveStateReducer
```

Guard checks only action admission: allowed action, unique target and kind,
canonical temporal fact, Objective ownership, approval state and already
verified duplicate result. It does not score content or classify intent.

For `RESULT_UNKNOWN`:

```text
write sent -> RESULT_UNKNOWN -> no blind retry -> query/reconcile Java truth
             -> known success | safe retry | controlled failure
```

The existing fault-injection, duplicate-action, reconciliation and worker
fencing tests pass. This is code-level evidence; Java DB proof is still live
blocked.

## 12. REAL BUSINESS ACCEPTANCE

The required acceptance boundary is Frontend -> Agent API -> Runtime -> Java ->
DB/Scheduler -> Frontend. Because no services were running, the final status
for every true live case is `BLOCKED`, not PASS. The middle column records the
available code/contract evidence.

| Case | Code/contract evidence | True E2E verdict | Java/business evidence |
| --- | --- | --- | --- |
| 1 Simple hot/public search | PASS: Tool registry + Java search/hotScore contract | BLOCKED | Java/Frontend not running |
| 2 Simple draft | PASS: create_draft Tool + Java Draft contract | BLOCKED | No Draft row observed |
| 3 Publish now | PASS: publish-now Tool, approval, verification contract | BLOCKED | No Java Post observed |
| 4 Schedule | PASS: canonical future-time and schedule verification tests | BLOCKED | No Java Schedule observed |
| 5 Two objectives | PASS: Commitment/Objective and resource-isolation tests | BLOCKED | No Post/Schedule pair observed |
| 6 Three objectives | PASS: multi-objective ActionLoop test | BLOCKED | No three-resource Java evidence |
| 7 Cross-turn update | PARTIAL: resolver/update path plus POC v2 supersede; production frozen version absent | BLOCKED | No existing Schedule update observed |
| 8 Ambiguous target | PASS: 0/1/>1 resolver contract and no-write tests | BLOCKED | No Java DELETE attempted/verified |
| 9 Delete HITL | PASS: durable approval/resume tests | BLOCKED | No Java deletion truth observed |
| 10 Mid-run change | PARTIAL: Objective/task mutation path; production Commitment version not frozen | BLOCKED | No stale-worker Java evidence |
| 11 Partial failure | PASS: per-objective status/Run aggregation contracts | BLOCKED | No independent Java child outcomes |
| 12 RESULT_UNKNOWN | PASS: reconciliation/fault-injection tests | BLOCKED | No Java authoritative reconciliation run |
| 13 No progress | PASS: bounded `ACTION_LOOP_NO_PROGRESS` test | BLOCKED | No live model/runtime token trace |
| 14 Search -> Create | PASS: structured evidence, GET_POST and synthesis tests | BLOCKED | No Java Draft/ownership evidence |
| 15 Search -> Create -> Schedule | PASS: tool/action/temporal contracts | BLOCKED | No Java Draft + Schedule + Scheduler evidence |

## 13. A/B RESULT

The migration comparison was intentionally not fabricated. A and B were not
run under the same live model/Java deployment in this turn.

| Metric | A | B |
| --- | --- | --- |
| Semantic/business accuracy | NOT MEASURED; observed run completion 66.67% is not semantic accuracy | NOT RUN |
| Multi-objective omission | NOT LABELED | NOT RUN; frozen oracle exists |
| Wrong action | NOT LABELED | NOT RUN |
| Wrong target write | No live labeled sample | Unit contracts pass; live NOT RUN |
| Duplicate write | No live labeled sample | Fault/duplicate tests pass; live NOT RUN |
| Unsafe write | No live labeled sample | Guard/approval tests pass; live NOT RUN |
| Cross-turn correctness | Not in observed report | POC supersede tests pass; production/live NOT RUN |
| Model calls / prompt tokens | 3.17 average model calls; token count unavailable | NOT RUN |
| Latency | p50 12.301s / p95 105.407s observed | NOT RUN |
| Loop count | Not reported in baseline | Bounded by existing loop budget; live NOT RUN |
| HITL frequency | 6 approval records / 30 observed runs; 2 pending at report time | POC trigger contract only; NOT RUN |
| Confirmation correction rate | NOT MEASURED; no production semantic confirmation | NOT RUN |
| Production LOC | Existing dirty production baseline; not comparable from this report | 0 main-path LOC; 531 isolated POC LOC |

Migration gates therefore cannot be evaluated yet.

## 14. COMPLEXITY VERDICT

**MODERATE** overall.

- Durable execution is strong and should remain the execution base.
- The active control path is understandable, but `ConversationRuntimeAdapter`
  still carries compatibility responsibilities and the repository contains
  historical Goal/Planner surfaces.
- The B POC is isolated and schema-free, but 531 LOC is larger than a final
  production commitment contract should be without measured benefit.
- No second Runtime, scheduler, Resource model or business state source was
  added. The complexity risk is therefore contained, not eliminated.

## 15. FINAL ARCHITECTURE

```text
User
  -> Frontend
  -> Agent API / durable Run acceptance
  -> AgentRunner
  -> ContextAssembler
  -> CommandInterpreter (one semantic extraction)
  -> TargetResolver + TemporalResolver
  -> Simple/Complex Gate
       |-- Simple CHAT/READ -> FastPath -> ToolRuntime -> Java -> projection
       |-- Simple WRITE ----> Guard -> Durable Runtime -> ToolRuntime -> Java
       `-- Complex ---------> ActionLoop
                              -> one typed ToolCall per iteration
                              -> Objective-scoped ResourceBinding
                              -> Guard
                              -> Durable Runtime for every WRITE
  -> OperationLedger / Queue / Worker / Lease / Fencing / Checkpoint
  -> MCP ToolRuntime -> JavaClient -> Spring Boot -> Java DB/Scheduler/Outbox
  -> Typed ToolResult / Verification / Observation
  -> Objective satisfaction + Run aggregation
  -> Activity / FinalResponse projection
  -> Frontend

B POC (isolated evaluation lane only)
  Structured Command + resolved target/time
    -> Minimal Commitment/WorkItem projection
    -> deterministic confirmation/freeze/supersede tests
    -> Objective adapter
    `-> existing production ActionLoop/Durable Runtime (not wired in main)
```

## 16. FINAL VERDICT

**HYBRID_MINIMAL_CHANGE**

Keep A as the production control path. Keep the minimal Guard and completion
correctness fixes. Retain B as an isolated POC/evaluation contract until the
real Java/Frontend acceptance run and same-model A/B metrics exist. Do not
`MIGRATE_B` yet: B has no production confirmation API, persistence, or stale
worker version integration, and no measured superiority.

## 17. REMAINING GAPS

### P0

1. Bring up the real Java DB/Scheduler, Agent API/Worker and Frontend, then run
   Cases 1-15 with Java row/resource/notification and user-visible evidence.
2. Add production Semantic Confirmation as a distinct durable interaction with
   backend schema/API, deterministic renderer and frontend confirmation card;
   revalidate edited target/time server-side before freeze.
3. Integrate Commitment version/supersede with existing Task/Execution fencing
   so a stale Worker cannot write an older confirmed outcome after a cross-turn
   change. Reuse existing fencing/version primitives.
4. Execute and verify the scheduled publish -> Java Post -> profile/my-content
   -> notification chain; do not report Schedule creation as actual publication.

### P1

1. Run the 35 frozen Seed cases through A and B with the same model and record
   split, target binding, temporal binding, omission, wrong action, token,
   latency and confirmation-correction metrics.
2. Improve Java search quality with pagination, diversity/deduplication and
   richer quality/engagement signals; keep ranking Java-owned.
3. Add a unified account-content projection if product requires posts + drafts
   in one request; do not infer it in the Frontend.
4. Complete Frontend mapping for distinct clarification, semantic-confirmation
   and risk-approval events without exposing Runtime internals.

### P2

1. Hot-topic analysis beyond current `hotScore`.
2. Long-term operations/recurring operations, only after a real Java/API
   contract exists.
3. Expanded comments/interaction analytics and operational reporting.

