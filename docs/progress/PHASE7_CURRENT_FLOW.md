# Phase 7 Current Agent Flow

> This is a Phase 7 working audit. The current architecture authority remains
> [`docs/architecture/CURRENT_ARCHITECTURE.md`](../architecture/CURRENT_ARCHITECTURE.md).

## 1. Runtime chain

```text
User message
  -> Agent API route
  -> ConversationRuntimeAdapter
  -> ContextBuilder / Conversation / Task / Execution / Memory projections
  -> CommandInterpreter (LLM structured understanding)
  -> TargetResolver (code-owned safety resolution)
  -> GoalDecomposer (LLM structured GoalTree)
  -> TaskManager (durable Task lifecycle)
  -> AgentLoop: Observe -> Reason -> Act -> Reflect
       -> ToolSelector (ToolMetadata + LLM)
       -> ToolPolicyGate (code-owned permission/approval/mode)
       -> GoalCompiler / DynamicPlanner
       -> ExecutionInput / TaskPlan
  -> Queue / Agent Worker
  -> ExecutionWorker / Checkpoint / Ledger / Retry / Recovery
  -> ToolRuntime
  -> MCP-compatible in-process runtime
  -> Java Backend or Creator Service
```

The API route owns authentication, request/history projection, response
projection, and compatibility `run_id` handling. It does not interpret natural
language or execute tools.

## 2. LLM-owned work

The model is responsible for semantic decisions, with typed JSON validation at
each boundary:

- Command understanding: requested outcome, entities, constraints, references,
  ambiguity, and semantic capabilities;
- Goal decomposition: one root Goal, child Goals, dependencies, outputs, and
  capability requirements;
- AgentLoop Reason: the next AgentAction from current observation;
- Tool selection: concrete tool choice from the supplied ToolMetadata catalog
  when the Reason output does not name a tool;
- Reflection: whether the latest observation is complete, needs another step,
  or warrants a plan adjustment;
- Dynamic replanning: a typed plan mutation or human clarification after new
  runtime evidence.

Planner behavior is intentionally represented by the existing typed plan
contracts: dependencies provide sequential/parallel structure, while runtime
observations can produce conditional `INSERT_STEP`, `REMOVE`, `REORDER`, or
`SELECT_ALTERNATIVE_TOOL` decisions. No executable branch expression or fixed
workflow template is introduced.

LLM output is never treated as executable authority. It must pass Pydantic
contracts, catalog membership checks, plan validation, and policy gates.

## 3. Code-owned work

Python code owns the deterministic and security-sensitive boundaries:

- request authentication, tenant/user scope, and conversation persistence;
- bounded ContextSnapshot construction and target candidate projection;
- target resolution, including explicit `RESOLVED`, `AMBIGUOUS`, and
  `NOT_FOUND` outcomes;
- GoalTree validation, DAG/plan compilation, artifact dependency checks, and
  plan versioning;
- Task create/update/preemption/resume lifecycle;
- ToolMetadata catalog membership and input contract handoff;
- ToolPolicyGate permission, approval, cost, retry, side-effect, and queue mode;
- Execution identity, queue delivery, Worker leasing, checkpoints, ledger,
  idempotency, retry, reconciliation, recovery, and result projection;
- MCP handler dispatch and Java/Creator protocol adapters;
- context budgets, message compression persistence, memory isolation, and
  evaluation checks.

## 4. Current hard-coded complexity

The audit found the following bounded complexity rather than a second product
runtime:

- Command/Goal/Loop/Selector each make a separate structured model call. This
  is intentional separation of semantic understanding, decomposition, action,
  and reflection, but their schemas need a shared richer understanding payload.
- Capability catalog entries contain semantic candidate tool names. They are
  catalog data, not business workflow routing; concrete policy remains in
  ToolMetadata.
- GoalCompiler contains only a small fallback from generic Goal types to
  capabilities for incomplete model output. It is a validation fallback, not a
  user-message keyword router.
- AgentLoop has a deterministic reflection fallback for embedding tests and a
  DynamicPlanner evidence fallback when no model is available. The latter must
  distinguish safe re-observation from blind side-effect retry.
- ConversationService and ContextBuilder both expose bounded model-facing
  history helpers. They have different ownership: ConversationService stores
  facts and ContextBuilder creates the decision projection.

No active path uses `if message contains ...` routing, fixed workflow templates,
or a second Assistant/Creator runtime.

## 5. Boundaries that must not be deleted

The following are deliberate reliability boundaries and remain required:

- `Task`, `GoalTree`, and `TaskManager` for long-lived multi-turn work;
- `TaskPlan`, `PlanGraph`, `ExecutionInput`, and `GoalCompiler` for typed plan
  handoff;
- `AgentLoop` as the intelligence decision loop;
- `ExecutionWorker`, Queue, Checkpoint, Ledger, Retry, and Recovery as the
  reliable execution runtime;
- `ToolRuntime`, MCP, `ToolMetadata`, and `ToolPolicyGate` as separate
  capability, protocol, and safety layers;
- Conversation facts, Context projections, Memory retrieval, and context
  compression;
- Java Backend and Creator Service as external business boundaries;
- Evaluation and trace/metric contracts.

## 6. Phase 7 focus from this audit

Phase 7 changes are intentionally incremental:

1. make semantic Command understanding explicit without introducing an Intent
   classifier;
2. preserve continuation references and ambiguity as typed data;
3. make safe DynamicPlanner fallback evidence-aware;
4. narrow ToolSelector's LLM candidate set by semantic metadata without
   replacing the catalog or policy gate;
5. expose the requested multi-task and long-conversation evaluation metrics;
6. add deterministic tests for the multi-goal article/search/schedule example
   and second-turn modification behavior.
