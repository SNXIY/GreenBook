# PROCEDURAL_MEMORY_DESIGN

## Review scope

- Repository: `D:\agent\green-book`
- Branch at review start: `feature/hybrid-search-rag`
- Checkpoint: `347727a8a07faf3962495a1584091a53622d9ac8`
- Review date: `2026-08-26`
- Scope: read-only architecture audit and design proposal
- Production code changed: none
- Tests run: none; this phase is design-only
- `git diff --check` at start: passed
- Worktree at start: clean

The report deliberately proposes contracts and boundaries only. It does not
implement Procedural Memory, change the runtime, add a table/collection, or
change any existing Memory type.

## 1. Definition

Procedural Memory is a bounded, long-term advisory record of **how a user or
the system has found it useful to approach a class of work across tasks**.
It is reusable operating experience, not an execution authority.

It answers **HOW TO DO**:

- a reusable precondition or verification habit;
- a user-explicit, cross-task working rule;
- a stable strategy supported by multiple independently verified outcomes.

It is not:

| Concept | Meaning | Procedural boundary |
|---|---|---|
| Preference | What the user likes or prefers | Never infer a preference from one procedure or one behavior |
| Semantic | A stable fact about the user or long-term context | Never infer identity or expertise from a single procedure |
| Episodic | A verified summary of what happened in one past experience | A single Episode is evidence, not a general procedure |
| Current State | What a Task, Execution, Resource, Approval, or business object is doing now | Never copy it into a procedure record |
| Procedural | A reusable advisory way of approaching work | Cannot execute, authorize, or redefine work |

The canonical authority ordering is:

```text
Security / Policy
    > Current explicit request
    > Capability / Tool contract
    > Durable Runtime invariants
    > Procedural Memory
```

An explicit user request provides intent, but does not grant permission or
override a hard safety/runtime rule. Procedural Memory is always the lowest
priority input in an execution decision.

## 2. Current Memory architecture compatibility

### Current model

`greenbook_agent_core.memory.models.MemoryRecord` already provides the minimum
storage shape needed for a future Procedural record:

- `user_id` and `tenant_id` scope;
- `memory_type` with `PROCEDURAL` already present;
- bounded `content` plus `structured_metadata`;
- `status` (`ACTIVE`, `INACTIVE`, `SUPERSEDED`);
- confidence, importance, timestamps, source type/id, and provenance-capable
  metadata;
- one repository contract backed by the existing `agent_memories` storage.

`MemoryManager` is the canonical write facade and `MemoryRepository` is the
canonical persistence contract. `MemoryRetriever` owns candidate retrieval,
scope/type/status filtering, deterministic ranking, and the single
`MemoryRelevanceGate`. `ContextBuilder` owns the bounded model-facing
projection.

Therefore the schema is **compatible in shape**, but it is not yet a safe
Procedural V1 admission contract. The existing generic helper
`MemoryManager.remember_pattern()` accepts an arbitrary string under
`REUSABLE_STRATEGY`, and `MemoryWritePolicy` still recognizes that legacy event
type. Those surfaces must not be treated as the new Procedural policy.

### Current production composition

The production composition in `apps/agent_api/greenbook_agent_api/main.py`
creates one `MemoryRetriever` with:

- strict tenant scope;
- `ACTIVE` status;
- legacy Episodic exclusion;
- the existing Preference/Semantic compatibility contract;
- `PREFERENCE` and `EPISODIC` in the configured type allowlist.

`PROCEDURAL` is not in that production allowlist. This is currently safe:
legacy procedural rows are not model-facing through the canonical path.

`ContextBuilder` already provides a bounded `recalled_memories` projection
and labels Preference, Semantic fact, and Episodic memory roles. It does not
currently expose a production `Relevant Procedures` category. A future
Procedural enablement should extend this same projection rather than add a
Procedural prompt path.

No second repository, vector collection, retriever, or relevance policy is
needed by this design.

## 3. Existing Architecture Collision Audit

| Existing capability | Current owner and responsibility | May Procedural Memory influence it? | Hard boundary |
|---|---|---|---|
| Current user instruction / Command Interpreter | Resolves the current user request into typed command semantics, target, time, and required capabilities | Yes, only as low-priority context after the current request is known | Memory cannot rewrite the request, target, permission, or explicit constraint |
| Task / Objective / Goal Compiler / Objective reducer | Owns business intent, Objective identity, dependencies, resource ownership, and completion truth | No direct mutation; a procedure may be supplied as advisory evidence to a decision | Memory cannot create, complete, supersede, or reopen an Objective |
| ActionLoop | Observe → build context → choose one semantic action → act → observe; enforces bounded iterations, write guards, and verified FINISH | At most a soft hint to the decision maker about a non-binding approach | It cannot be a hidden workflow engine, and it must not treat memory as completion or authorization |
| Capability Registry / Mapper / Plan Validator | Defines the active capability catalog, maps semantic actions, validates capability/tool mapping, dependencies, artifacts, and approval requirements | No expansion or substitution of capabilities; a procedure may suggest an already-available action | Memory cannot add a capability, alter required fields, or turn an invalid plan valid |
| Capability filtering / target and resource binding | Restricts the selected operation to the current typed capability and Objective-owned resource | No | Memory cannot select a cross-Objective resource, fill unsafe IDs, or bypass binding errors |
| Tool Registry / ToolMetadata | Defines active tools, schemas, side-effect and approval metadata, timeout and retry policy | No schema or policy changes | Memory cannot add a tool, change a schema, lower a risk level, or alter retry/approval metadata |
| ToolRuntime / CapabilityExecutor | Binds arguments, creates invocation context, applies idempotency/timeout/audit behavior, invokes the tool, and classifies results | No direct influence on invocation authority | Memory cannot call a tool or turn `RESULT_UNKNOWN` into success |
| MCP server / protocol adapter | Validates active ToolContract input/output and invokes the registered handler with trusted auth/session context | No | Memory cannot bypass MCP validation, identity injection, auth, or the Java boundary |
| Durable Runtime / Worker / StateManager / OperationLedger | Owns durable Execution state, claims, checkpoints, idempotency, pause/resume, artifact projection, and terminal transitions | No state-machine changes; only bounded advisory context before a decision | Memory cannot transition Execution, replay a side effect, or change operation ownership |
| Retry / reconcile | Classifies failures, authorizes safe retries, and reconciles ambiguous external operations | No candidate source and no retry decision influence | `RESULT_UNKNOWN`, response loss, retry traces, and reconciliation state are not Procedural evidence |
| HITL / ApprovalRuntimeService | Owns durable approval records, authenticated user decision, rejection, and resume | No | Memory cannot approve, reject, bypass, or requeue an operation |
| Java business service | Owns business resource truth and verified postconditions | No direct Memory write; its verified outcome may later be an input to a candidate builder | Memory cannot override Java state or treat an unverified response as a fact |
| Conversation / Run metadata | Provides conversation/run envelope, scope, trace, and bounded context | Only as provenance and scope metadata | IDs are internal trace references, never procedure semantics |
| Skill system | No active Python `Skill` runtime/registry/caller was found in the audited repository paths | Do not re-enable or introduce it in this phase | A future Skill, if added, must remain separate from user/history-derived Memory |

### Important schedule example

The proposed example rule is:

> Before modifying an existing scheduled publication, read its current state
> and version, then update it and verify the final state.

This is already a hard business execution invariant in
`services/greenbook_mcp/greenbook_mcp_server/tools/publication.py`:

1. `update_schedule` reads the current schedule;
2. it rejects non-modifiable states;
3. it takes the authoritative current version;
4. it sends the update with that version;
5. it reads the schedule again and verifies identity, status, requested time,
   and version advancement.

The active ToolRegistry, ToolRuntime, Durable Runtime, and Java receipt gates
then preserve schema, authorization, idempotency, unknown-result, and verified
completion semantics. ActionLoop is intentionally dynamic and does not encode
a second fixed workflow. The result is the same for this Memory design:
storing this rule as a Procedural record would duplicate an existing hard
invariant and must not be used to drive execution.

## 4. Production Reachability

### Canonical active path

The current production flow is structurally:

```text
Current turn
  -> TurnCoordinator / ConversationRuntimeAdapter
  -> ContextAssembler
  -> ContextBuilder
  -> configured MemoryRetriever
  -> MemoryRelevanceGate
  -> bounded context projection
  -> Interpreter / ActionLoop
  -> Goal/Capability validation
  -> Durable Worker
  -> CapabilityExecutor
  -> ToolRuntime
  -> MCP ToolContract / handler
  -> Java business truth and verified receipt
```

This is one model-facing Memory read boundary. A future Procedural type must
join this path at the existing retriever and gate, then appear only as a
bounded context role.

The current write owners remain separate from Runtime truth:

```text
Explicit user statement -> Preference/Semantic service -> MemoryManager -> repository
Verified ActionObservation -> Episodic projector -> MemoryManager -> repository
```

There is no active Procedural writer in this composition.

### Legacy procedural recall

The historical path was:

```text
RuntimeAgentService._execute_single
  -> _recall_memories
  -> MemoryManager.recall for SEMANTIC/EPISODIC
  -> StrategyRetriever(repository.search(PROCEDURAL))
  -> RuntimeContext.memory_context
```

The current `_execute_single` contains an explicit boundary comment that
long-term Memory is assembled by `ContextBuilder` before Runtime, and the
repository call graph has no invocation of `_recall_memories`. The old helper
still exists, but it is not part of the current production call path. It also
does not use the canonical tenant/relevance gate and must not be reactivated.

Classification:

| Symbol/path | Classification | Evidence and risk |
|---|---|---|
| `RuntimeAgentService._recall_memories` | `DEAD_CODE` in repository; `UNKNOWN` for untracked external callers | Private legacy helper; no active `_execute_single` caller; contains a bypassing direct recall model |
| `StrategyRetriever` | `DEAD_CODE` in repository; `DEPRECATE_CANDIDATE` | Only referenced by the dead helper; directly searches `PROCEDURAL`, filters `success`, and never invokes `MemoryRelevanceGate` |
| `RuntimeContext.memory_context` | `DEAD_CODE` for Memory recall; `UNKNOWN` as a public compatibility field | Field remains for compatibility, but current Runtime does not populate it from Memory |

### Legacy procedural write

The historical path was:

```text
RuntimeAgentService._execute_single
  -> _record_procedural
  -> ProceduralMemoryExtractor.extract
  -> MemoryManager.remember
  -> MemoryRecord(PROCEDURAL)
```

The extractor derives strings such as “resolved execution succeeded with N
tool calls” from goal category, plan source, status, and counts. That is an
execution summary, not a verified reusable procedure, and it has no repeated
evidence admission boundary. The `_execute_single` caller was removed; no
production caller remains.

| Symbol/path | Classification | Evidence and risk |
|---|---|---|
| `RuntimeAgentService._record_procedural` | `DEAD_CODE` in repository; `DEPRECATE_CANDIDATE` | Private helper retained for compatibility; no active caller; would write directly from runtime outcome |
| `ProceduralMemoryExtractor` | `DEAD_CODE` in repository; `DEPRECATE_CANDIDATE` | Only referenced by the dead helper; accepts execution status/counts and lacks V1 candidate/policy semantics |
| `MemoryManager.remember_pattern` | `TEST_ONLY` by repository references; `DEPRECATE_CANDIDATE` | Unit test calls it directly; generic arbitrary-string writer is not a safe future Procedural admission path |
| `MemoryWritePolicy.REUSABLE_STRATEGY` | `TEST_ONLY`/compatibility surface; `DEPRECATE_CANDIDATE` | Generic event is accepted by the shared policy through the legacy helper; it must not be mistaken for the V1 policy |

The API main still injects a `MemoryManager` into `RuntimeAgentService`. That
constructor dependency alone does not establish a production procedural
caller: the active execution method does not call the legacy read/write
helpers. Keeping the injection is a compatibility concern, not evidence that
Runtime owns Memory truth.

### Other Memory paths

`EpisodicMemoryProjector` is active for the existing Episodic V1 path through
the terminal `ActionObservation` callback. It is not a procedural helper and
must not be reused as a direct procedure writer. Preference and Semantic
services are active only through their own explicit contracts and canonical
`MemoryManager` methods.

## 5. Execution authority boundary

Procedural Memory may:

- provide a ranked, bounded advisory statement;
- help the model consider a non-binding approach that is already legal;
- explain a prior verified pattern to the user when relevant;
- be omitted when no relevant procedure exists.

Procedural Memory may not:

- execute or enqueue a Tool;
- select an unavailable capability or tool;
- alter Tool schema, arguments required by schema, permission scopes, or
  approval metadata;
- bypass HITL, authentication, authorization, capability filtering, or
  Objective-owned resource binding;
- mutate Task/Objectives/Execution/Run/Resource/Approval state;
- decide that a write succeeded, especially after `RESULT_UNKNOWN`;
- choose a retry, reconcile an operation, or convert a failure into success;
- override current explicit instructions, verified Observation, or Java truth;
- become a hidden Planner, Workflow Engine, ActionLoop, Tool Router, or Skill
  system.

The safe interpretation of a procedure is “consider this advisory evidence;
revalidate everything at the existing hard boundary.”

## 6. Candidate sources

### Source A: explicit user rule

The highest-quality first source is a current user statement that explicitly
defines a durable, cross-task working rule. The source adapter should retain:

- authenticated `user_id` and `tenant_id`;
- the current conversation/message provenance;
- a bounded source hash or source identity;
- the exact user-authored evidence needed for audit;
- the normalized procedure candidate.

The assistant, a planner, or a ToolResult cannot author this source on the
user's behalf. LLM interpretation may normalize a statement, but it cannot
invent the rule.

### Source B: repeated verified successful histories

A learned procedure may be proposed only after multiple independent histories
show the same reusable approach. Each contributing history must be anchored
to:

```text
terminal Objective
  + terminal ActionObservation(s)
  + verified business outcome / receipt
```

The minimum V1 design should require at least two distinct, terminal,
successful, non-retry evidence groups with the same normalized trigger and
guidance. A later implementation may raise this threshold after benchmark
review. One successful Task or one Episodic record is insufficient.

`ActionObservation` is the best runtime source because it is persisted only
after terminal projection and carries business/resource evidence for
continuation. It is still evidence, not the Procedure itself. The candidate
builder must join it to the exact Objective and verified business outcome;
it must not use an active/recent Task fallback.

### Explicit exclusions

Do not generate a Procedural Candidate from:

- a single Tool success without verified business postcondition;
- a single Episode;
- `RUNNING`, `PENDING`, `WAITING_EXTERNAL`, `WAITING_HUMAN`, or
  `RESULT_UNKNOWN` state;
- retry, resume, reconcile, timeout, or transport traces;
- LLM chain-of-thought, intermediate reasoning, or an assistant summary;
- an unverified ToolResult or a failed/unconfirmed execution;
- a current resource ID, schedule ID, draft ID, run ID, execution ID, or
  operation ID as the semantic subject.

Conversation metadata may supply scope and provenance, but conversation text
alone does not prove a successful procedure.

## 7. ProceduralCandidate contract

The smallest useful future contract is:

| Field | Contract |
|---|---|
| `user_id` | Required authenticated user scope |
| `tenant_id` | Required authenticated tenant scope |
| `procedure_key` | Stable normalized identity for one advisory procedure; no runtime ID embedded |
| `trigger` | Bounded class of work, such as `UPDATE_EXISTING_SCHEDULE`; not a Task ID |
| `guidance` | Short advisory “how”; no executable DSL, full plan, or tool argument payload |
| `confidence` | Numeric evidence confidence, constrained and policy-checked |
| `source_type` | Explicit enum-like value, initially `EXPLICIT_USER_RULE` or `REPEATED_VERIFIED_HISTORY` |
| `provenance` | Contract/version, evidence count, source references, verification markers, and audit data; internal IDs are allowed only here |
| `observed_at` | ISO timestamp for when the rule/evidence was observed |

The persisted `MemoryRecord` supplies lifecycle, memory type, timestamps,
importance, and canonical `memory_id`. A future record should use:

```text
memory_type = PROCEDURAL
metadata.memory_contract = PROCEDURAL_V1
metadata.memory_role = relevant_procedure
```

The procedure body must not depend on `run_id`, `execution_id`,
`operation_id`, `draft_id`, `schedule_id`, or other runtime identities. Those
may be bounded internal provenance references for audit and idempotency only.

Do not store a complete Execution trace, complete Plan, raw Tool arguments,
hidden reasoning, or a general-purpose workflow DSL.

## 8. Admission policy

The type-specific policy is conservative:

| Decision | KEEP | DROP | UNKNOWN |
|---|---|---|---|
| Meaning | A durable advisory procedure is explicitly authored or supported by repeated verified evidence | It is transient, redundant, unsafe, unsupported, or not worth retaining | Evidence/meaning is ambiguous or cannot be proven |
| V1 write behavior | Candidate may enter the canonical MemoryManager after all checks | No write | Treat exactly as `DROP` |

### KEEP

Keep only when all of the following hold:

- the scope is complete (`user_id` and `tenant_id`);
- the rule is explicit and user-authored, or has repeated independent verified
  support;
- it is stable and likely reusable across tasks;
- it is short, non-sensitive, and semantically free of resource/runtime IDs;
- it does not claim authority over security, policy, tools, capability, or
  runtime invariants;
- it is not merely a Preference, Semantic fact, Episodic summary, or current
  state projection;
- its `procedure_key` is supported by a deterministic identity strategy.

### DROP

Drop:

- ordinary one-time CRUD and one-off queries;
- current Task/Execution/Resource/Approval status;
- a single successful operation or single Episode;
- retry/reconcile internals and transport details;
- LLM guesses or inferred user habits;
- a rule that simply repeats a hard Tool/Runtime invariant;
- a rule that would skip approval, permissions, schema validation, or
  verification;
- a Skill/package/workflow definition better owned by the codebase;
- sensitive or inappropriate long-term information.

### UNKNOWN

Ambiguous phrasing, insufficient evidence, unresolved type boundaries, or a
conflict that cannot be scoped deterministically is `UNKNOWN`, and V1 treats
it as `DROP`. The model must never resolve the ambiguity by deleting an old
record or choosing an execution strategy.

The existing generic `REUSABLE_STRATEGY` event must not be used as this
policy's substitute. Future writes should enter through one explicit
Procedural admission boundary and then call the existing canonical
`MemoryManager`/repository.

## 9. Conflict and lifecycle policy

### Identity

The minimum conflict identity is:

```text
tenant_id + user_id + procedure_key
```

`trigger` may be part of `procedure_key` normalization, but a raw runtime ID
must never define identity. Different procedure keys may coexist.

### Lifecycle

```text
ACTIVE --new authoritative rule--> SUPERSEDED
ACTIVE --explicit deactivation--> INACTIVE
```

For the same scoped `procedure_key`:

- duplicate equivalent guidance must reuse/merge the existing ACTIVE record;
- a new explicit rule supersedes an older learned rule;
- a new explicit correction supersedes an older explicit rule when the user
  clearly changes it;
- a learned update cannot silently supersede an explicit rule without a new
  explicit user statement or a separately approved policy;
- the old row remains auditable as `SUPERSEDED` and points to the replacement;
- different procedures are never superseded merely because their triggers are
  similar.

The supersede plus new ACTIVE projection must be atomic at the repository
boundary. There must be no externally visible durable state in which two
conflicting records for the same scope/key are ACTIVE. The LLM may not freely
delete history; lifecycle transitions are code-owned and provenance-backed.

System security, policy, capability, runtime, HITL, and Java truth remain
higher authority even when a Procedural record is ACTIVE.

## 10. Retrieval and injection design

Future Procedural retrieval must be exactly:

```text
Current turn
  -> one canonical MemoryRetriever
  -> one MemoryRelevanceGate
  -> bounded ContextBuilder projection
  -> Interpreter / ActionLoop as advisory evidence only
```

There must be no `ProceduralRetriever -> ActionLoop` or
`ProceduralMemory -> Prompt` side channel.

### Required future configuration

- Add `PROCEDURAL` to the existing production retriever allowlist only after
  V1 admission and legacy quarantine are complete.
- In the same `MemoryRetriever`, require `memory_contract=PROCEDURAL_V1`
  when querying that type; do not let legacy `PROCEDURAL` rows enter by type
  alone.
- Preserve strict `user_id` and `tenant_id` filtering, `ACTIVE` status,
  expiry handling, confidence checks, relevance threshold, and bounded limit.
- Reuse the existing `MemoryRelevanceGate`; do not create a per-type gate.
- Preserve no-match as an empty result. A relevant procedure is optional.

### Context shape

The bounded context may distinguish:

```text
Preferences:
  ...
Relevant Facts:
  ...
Relevant Past Experiences:
  ...
Relevant Procedures:
  - advisory guidance only
```

The total Memory budget remains bounded. A future implementation should cap
procedures as a small subset of the existing memory budget and strip
provenance/raw payloads from the model-facing view. The context must state or
encode that procedures are advisory; it must not present them as commands,
permissions, current state, or verified completion.

## 11. Skill / Tool / Capability boundary

| Layer | Answers | Owner | Procedural relationship |
|---|---|---|---|
| Tool | “What concrete operation can be called?” | ToolContract / Tool Registry / MCP handler | Procedures cannot add, alter, or call tools |
| Capability Filtering | “Which registered capability is allowed for this request/objective?” | Command/Objective/Capability Registry and validation | Procedures cannot expand the allowlist or bypass required capability checks |
| Skill | “What reusable packaged ability/context/operation knowledge exists in the product?” | A future code-owned capability package, if introduced | Not enabled or redesigned here; it is not user-history Memory |
| Procedural Memory | “What advisory way of approaching this class of work has proven useful?” | Canonical Memory admission/repository/retriever | Lowest-priority, bounded evidence; never an executor or workflow definition |

The distinction is substantive:

- a Tool performs an operation;
- a Capability says the Agent can perform a class of operation;
- filtering decides whether that capability is allowed now;
- a Skill packages code-owned ability and knowledge;
- Procedural Memory supplies optional history-derived guidance.

Procedural Memory must not be used to resurrect or silently substitute a Skill
system. If a procedure becomes a deterministic multi-step workflow with
required ordering, validation, retries, or authorization, it belongs in the
existing code-owned Planner/ActionLoop/Tool/Runtime boundary, not in Memory.

## 12. One vertical slice proposal

### Candidate scenario

The natural GreenBook scenario is the explicit user rule:

> “以后涉及修改定时发布任务，先查询现有状态，再执行修改并验证结果。”

### Admission result for the current architecture

**DROP as redundant with an existing hard runtime/business invariant.**

This is intentionally a no-write design slice, not a reason to invent a
duplicate procedure record. The active `publication.update_schedule` handler
already reads authoritative state/version, performs a versioned update, and
verifies the postcondition. Tool policy, MCP validation, Durable Runtime, and
Java business truth protect the same boundary. The user statement may be
acknowledged as the current request or audited as an explicit rule, but it
must not become a second source that tells ActionLoop to reproduce the same
workflow.

### What this slice proves

1. Explicit user rules can be recognized without letting them become hidden
   execution plans.
2. The admission policy can detect a rule already enforced by a hard owner.
3. No `PROCEDURAL` row is written for the redundant rule.
4. The existing publication operation continues to use its Tool/Java
   precondition, version, approval, idempotency, and verification paths.
5. Removing or omitting a future advisory memory cannot weaken execution
   safety.

If a future benchmark requires a positive Procedural write, select a different
explicit rule only after proving that it is genuinely non-mandatory, is not a
Preference or Skill, and does not duplicate the current ActionLoop/Tool/Runtime
semantics. That selection is outside this read-only checkpoint.

## 13. Evaluation design

The next implementation should use a small, deterministic benchmark rather
than a broad production evaluation.

### Admission and write metrics

- correct explicit-procedure admission;
- correct drop of the redundant schedule invariant;
- unsupported procedure inference rate;
- single-success-to-procedure false admission rate;
- duplicate ACTIVE procedure rate;
- atomic supersede correctness;
- learned-vs-explicit precedence correctness;
- `UNKNOWN -> DROP` correctness;
- write idempotency/replay duplicate rate.

### Retrieval metrics

- relevant retrieval Recall@K;
- relevant retrieval Precision@K;
- no-match false-return rate;
- unnecessary procedure injection rate;
- Preference/Procedural confusion rate;
- Episodic/Procedural confusion rate;
- Semantic/Procedural confusion rate;
- bounded-context/token budget compliance.

### Protected-boundary metrics

- safety/runtime override rate must be `0`;
- approval bypass rate must be `0`;
- capability/tool schema bypass rate must be `0`;
- Task/Objective/Execution/Resource mutation caused by Memory must be `0`;
- `RESULT_UNKNOWN` converted into procedure or success must be `0`;
- cross-user leakage must be `0`;
- cross-tenant leakage must be `0`.

The benchmark must include negative cases for retry traces, one ordinary CRUD
success, an unverified ToolResult, a pending/unknown execution, a procedure
that tries to override approval, and a legacy `PROCEDURAL` row. All should
produce no unsafe model-facing procedure.

## 14. Blockers and remaining legacy candidates

There is no blocker to completing this design. The following are blockers to
**implementing and enabling** Procedural Memory safely:

1. **Legacy write surface:** `remember_pattern`,
   `ProceduralMemoryExtractor`, and the generic `REUSABLE_STRATEGY` policy
   remain callable compatibility surfaces. They must be quarantined or
   guarded before a new `PROCEDURAL_V1` writer is enabled.
2. **Legacy read isolation:** the existing `StrategyRetriever` bypasses the
   canonical gate and has no V1 contract filter. It must stay unused, or be
   adapted to the single canonical retriever before any procedural rows are
   model-facing.
3. **Exact source join:** the future learned-history builder needs an exact
   terminal Objective + ActionObservation + verified business outcome join;
   it cannot use active-task, recent-execution, or last-result fallback.
4. **Positive-slice selection:** the proposed schedule rule is already a hard
   invariant and therefore is a deliberate no-write case. A genuinely useful
   positive procedure must be selected without duplicating Runtime or Skill
   ownership.
5. **Context role:** the existing ContextBuilder must add a bounded
   `relevant_procedure` role in the same projection if and only if the type is
   enabled. No separate prompt injection path is acceptable.

Legacy candidates should be handled conservatively:

| Candidate | Recommended status |
|---|---|
| `RuntimeAgentService._recall_memories` | `DEPRECATE_CANDIDATE`; no production invocation |
| `RuntimeAgentService._record_procedural` | `DEPRECATE_CANDIDATE`; no production invocation |
| `RuntimeAgentService._record_episodic` | `DEPRECATE_CANDIDATE`; current Episodic projector is the active path |
| `StrategyRetriever` | `DEPRECATE_CANDIDATE`; never re-enable as a second retriever |
| `ProceduralMemoryExtractor` | `DEPRECATE_CANDIDATE`; incompatible with V1 verified candidate semantics |
| `MemoryManager.remember_pattern` | `TEST_ONLY` compatibility; guard before future enablement |
| `MemoryManager.remember_execution` | `TEST_ONLY` compatibility; remains Current State/Episodic legacy, not Procedural |
| `MemoryWritePolicy.REUSABLE_STRATEGY` | `DEPRECATE_CANDIDATE`; do not use as V1 admission |

No deletion is recommended in this phase because public compatibility and
external callers are not fully observable from repository search alone.

## 15. Remote verification status

The local read-only command:

```text
git ls-remote origin refs/heads/feature/hybrid-search-rag
```

was attempted and failed before any credential/configuration change with:

```text
SEC_E_NO_CREDENTIALS (0x8009030e)
```

Status:

```text
REMOTE_VERIFICATION_BLOCKED_BY_LOCAL_GIT_CREDENTIALS
```

No Git credential configuration was changed and no push was attempted.

## 16. Next implementation recommendation

When implementation is explicitly authorized, use this order:

1. Freeze `PROCEDURAL_V1` metadata/provenance and the exact source join.
2. Guard legacy generic procedural write/read surfaces without deleting them
   prematurely.
3. Choose one positive, non-redundant explicit-rule slice; keep the schedule
   rule as a negative no-duplicate case.
4. Implement one candidate builder and one conservative admission policy that
   calls the existing `MemoryManager`/repository only after validation.
5. Add scoped idempotent write and atomic supersede behavior using the existing
   MemoryRecord lifecycle.
6. Extend the same `MemoryRetriever`, `MemoryRelevanceGate`, and bounded
   `ContextBuilder` projection with a strict `PROCEDURAL_V1` contract filter.
7. Run only the focused benchmark and protected-boundary regression suite.

Do not change ActionLoop semantics, Durable Runtime, Task/Objectives,
RESULT_UNKNOWN/reconciliation, HITL, MCP, Tool Registry, Capability
Filtering, Search, RAG, or Java business truth as part of Procedural Memory.

## Verdict

**PROCEDURAL_MEMORY_DESIGN_COMPLETE**

The existing architecture can host a future Procedural type by reusing the
current MemoryRecord/repository/retriever/gate/context stack. Procedural
Memory is not production-enabled today. The legacy procedural code is not
currently repository-reachable from the active Runtime path, but its public
compatibility surfaces must be quarantined before any new Procedural writer or
reader is enabled.
