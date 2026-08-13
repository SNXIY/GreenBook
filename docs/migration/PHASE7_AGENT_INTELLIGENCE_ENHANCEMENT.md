# Phase 7 Agent Intelligence Enhancement

> This is the Phase 7 implementation report. The current architecture
> authority remains [`docs/architecture/CURRENT_ARCHITECTURE.md`](../architecture/CURRENT_ARCHITECTURE.md).
> Phase reports are historical delivery records and do not replace that
> authority.

## 1. Current problems

The existing runtime already had `CommandInterpreter`, `GoalDecomposer`,
`AgentLoop`, `DynamicPlanner`, `ToolSelector`, Context/Memory, and the durable
Execution path. The main intelligence gaps were contract-level gaps rather
than missing runtime infrastructure:

- Command output exposed mostly a coarse operation, objective, and parameters;
  goal entities, constraints, references, ambiguity, and required semantic
  capabilities were not explicit.
- Follow-up context contained history, but active tasks and unfinished goals
  were not first-class fields in the command understanding contract.
- The no-LLM DynamicPlanner fallback treated an unknown side-effect failure as
  a retry candidate, which could encourage a blind repeat.
- ToolSelector used the complete metadata catalog for every semantic task and
  did not consume a planner-selected alternative tool as a one-action hint.
- Evaluation models did not expose the Phase 7 behavioral rates directly.
- The deterministic conversation compactor could evict the beginning of an
  existing summary when appending older messages.

There was no active keyword-based user-message router in the canonical Agent
API path. Existing `not found` checks are response/error mapping, not command
understanding. Older IntentSpec/TaskIntent references remain only in
historical documents or archived compatibility material and are not used by
the active path.

## 2. Preserved capabilities

No business capability or reliable execution boundary was removed. The
following remain active:

- Natural-language understanding, multi-turn continuation, multi-goal tasks,
  Goal decomposition, and dynamic planning;
- Task lifecycle, GoalTree, TaskManager, Execution, Queue, Worker,
  Checkpoint, Ledger, retry, reconciliation, recovery, preemption, and
  resume;
- Community search, post/draft management, revision, scheduled publication,
  comments/replies, analytics, and approval flows;
- Creator Service research/writing/revision/artifact generation;
- MCP-compatible in-process Tool Runtime, shared ToolMetadata/ToolPolicy,
  Memory, Context, and Evaluation.

The AgentLoop remains the intelligence boundary and Execution remains the
durable runtime boundary. They were not collapsed into one loop.

## 3. Removed complexity

Phase 7 removed or constrained incorrect complexity without removing business
complexity:

- Command understanding is explicitly an LLM structured semantic extraction,
  not a keyword-driven Intent taxonomy. `CommandType` remains only the coarse
  operation envelope required by the existing adapter contract.
- Goal decomposition remains LLM-generated and typed. The runtime validates
  that every Command-required capability survives into the GoalTree instead of
  introducing workflow templates or tool-name routing.
- DynamicPlanner evidence fallback now re-observes when an external request
  may already have been sent and asks for human input for destructive or
  non-idempotent failures.
- Tool selection uses a semantic metadata projection. It does not create a
  second capability-to-tool routing table, and policy remains outside the
  model in `ToolPolicyGate`.
- Conversation compression keeps the existing bounded recent-message window
  while preserving the prior durable summary in deterministic fallback mode.
- Planner-selected alternative tools are one-action, catalog-validated hints;
  they do not bypass ToolMetadata or policy checks.

## 4. Agent Loop optimization

The production chain remains:

```text
Command understanding -> GoalTree -> Task lifecycle
  -> AgentLoop: Observe -> Reason -> Act -> Reflect
  -> GoalCompiler / DynamicPlanner -> ExecutionInput / TaskPlan
  -> Queue / Worker -> ToolRuntime / MCP -> Java or Creator
```

LLM-owned decisions are now explicit and typed:

- `CommandInterpreter`: `goal`, `entities`, `constraints`, `references`,
  `ambiguity`, `needs_clarification`, and `required_capabilities`;
- `GoalDecomposer`: semantic GoalTree, dependencies, outputs, and capability
  requirements;
- AgentLoop `Reason` and `Reflect`;
- ToolSelector choice from the supplied ToolMetadata projection;
- DynamicPlanner decision after new runtime evidence.

Code-owned responsibilities remain deterministic: scope/authentication,
target resolution, schema validation, catalog membership, policy/approval,
Task and plan persistence, queue delivery, execution identity, idempotency,
ledger evidence, checkpoints, retries, recovery, and result projection.

Ambiguous Command output or ambiguous target resolution now returns
`WAITING_HUMAN` with a clarification payload. It does not create a guessed
Task. A second turn can bind to the active Task through the existing structured
target resolver.

## 5. Planner optimization

`GoalTree` and `PlanGraph` continue to represent the semantic plan without
hard-coded workflow templates:

- Dependencies provide sequential ordering.
- Independent child Goals and capability metadata retain parallelizable work.
- Runtime observations can lead to typed conditional mutations:
  `INSERT_STEP`, `REMOVE`, `REORDER`, or `SELECT_ALTERNATIVE_TOOL`.
- `RETRY_WITH_NEW_ARGS` is restricted to typed plan mutation; side-effect
  failures first re-observe or request human confirmation.
- Plan changes remain versioned through the existing Task/plan revision path.

Conditional behavior is therefore an observation-conditioned planner decision,
not an executable condition string or a pre-authored business workflow.

## 6. Tool optimization

`packages/contracts/greenbook_contracts/tool_contract.py` remains the canonical
source for `ToolContract`, `ToolMetadata`, and `ToolPolicyMetadata`.

`ToolSelector` now narrows the model-facing catalog when Goal/current-task
semantic capabilities match metadata. If annotations are incomplete, it
falls back to the complete catalog so an existing valid integration is not
silently lost. A returned tool must still be in the catalog.

The selected metadata then passes through `ToolPolicyGate`, which remains the
only code-owned source for permission scopes, approval, risk, side effects,
retry, timeout, cost, and queue/inline mode. MCP consumes the same contract
projection and does not define a second policy catalog.

Canonical tool naming remains `domain.operation`, including community search,
content draft/revision, publication scheduling/publishing, interaction, and
analytics operations.

## 7. Multi-task tests

Added `tests/unit/test_phase7_agent_intelligence.py` covering:

- A second-turn “change the existing article to interview style and publish in
  the evening” command preserving the active Task target, semantic entities,
  constraints, references, and required capabilities;
- Rejection when Goal decomposition silently drops a required capability;
- Metadata-based candidate narrowing for ToolSelector;
- Re-observation for a possibly-sent idempotent side effect;
- Human escalation for a destructive/non-idempotent failure;
- Catalog validation for an alternative tool;
- A multi-goal article plan containing research, creation, validation,
  scheduling, and analysis with dependent and independent work;
- Preservation of prior facts during context compression;
- Evaluation aggregation for task success, plan quality, recovery,
  multi-task accuracy, and long-conversation consistency.

The multi-goal test keeps the requested shape: search references, create,
validate/draft, schedule, then analyze. It is compiled into the existing
`PlanGraph`/`TaskPlan`; it is not reduced to one workflow template.

## 8. Evaluation results

`AgentEvaluationMetrics` and `EvaluationMetricsCalculator` now expose:

- `task_success_rate`;
- `tool_selection_accuracy`;
- `plan_quality`;
- `recovery_success`;
- `multi_task_accuracy`;
- `long_conversation_consistency`.

The deterministic Phase 7 contract test produced the expected values for all
five newly added quality fields and the existing tool-selection field. The
repository regression suite also exercised the existing golden community
flows and evaluation runner.

No external-LLM benchmark number is claimed here: this workspace run used
deterministic test doubles and local contract/e2e fixtures, not a production
model key or live Java/Creator deployment. The metrics are now available for
the existing Evaluation runner to record when a live benchmark dataset is
enabled.

## 9. Test results

| Check | Result |
| --- | --- |
| `uv run pytest -q` | **561 passed, 1 skipped** |
| `pytest --collect-only -q` | **562 tests collected** |
| `uv run pytest -q tests/e2e` | **15 passed** |
| `uv run pytest -q` in `creator-agent` | **64 passed** |
| `mvn test` in `apps/backend` | **37 tests, 0 failures, 2 skipped** |
| Frontend `npm run lint` | **passed** |
| Frontend `npm run build` | **passed** |
| Frontend `npm run test:execution` | **passed** |
| `uv lock --check` | **passed** |
| `python -m compileall` (active Python packages) | **passed** |
| Active Agent/API/Evaluation Ruff check | **passed** |
| Creator Ruff `F,I` check | **passed** |
| `docker compose config` | **passed** |
| `git diff --check` | **passed** |

The full Creator Ruff profile still reports 58 pre-existing UP/SIM/B905
style findings, including 28 E501 line-length findings. No Creator business
logic was rewritten in Phase 7; unused/undefined/import hygiene is clean.

## 10. Final monorepo tree

Generated environments, build output, pytest scratch data, and historical
archive contents are omitted from this active tree:

```text
green-book/
├── apps/
│   ├── backend/
│   ├── agent_api/
│   └── agent_worker/
├── packages/
│   ├── agent_core/
│   ├── contracts/
│   ├── creator_client/
│   ├── evaluation/
│   ├── java_client/
│   ├── observability/
│   └── security/
├── services/
│   └── greenbook_mcp/
├── creator-agent/
├── zhiguang-fe/
├── contracts/
│   ├── agent-openapi.yaml
│   └── java-openapi.yaml
├── infra/
├── scripts/
├── tests/
├── docs/
│   ├── architecture/
│   ├── development/
│   ├── migration/
│   └── progress/
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
└── README.md
```

## 11. Final architecture

```text
LLM: Command understanding / Goal decomposition / Reason / Reflect / planning decisions
  -> AgentLoop: Observe -> Reason -> Act -> Reflect
  -> Execution Runtime: TaskPlan -> Queue -> Worker -> Checkpoint/Ledger/Recovery
  -> Tool Runtime: ToolMetadata -> PolicyGate -> MCP-compatible in-process handlers
  -> Business boundaries: Java Backend or GreenBook Creator Service
```

Conversation remains durable facts; Context remains a bounded working
projection; Memory remains long-term experience. Creator specialists remain
inside Creator Service and are not visible as direct Agent Runtime tools.
`assistant_runs` and `run_id` remain only the documented public history
projection; `execution_id` remains runtime identity.

## 12. Remaining technical debt

- A live-model benchmark still needs configured model/provider credentials and
  representative production-like datasets to populate the new quality rates.
- Creator formatting findings (UP/SIM/B905 and E501) remain intentionally
  outside the Phase 7 intelligence change; F/I hygiene is clean.
- Historical migration and archive documents still contain old names by
  design. `CURRENT_ARCHITECTURE.md` is the only current topology authority.
- `GreenBookMCPServer` remains the established class name for the
  MCP-compatible in-process runtime; changing it would add naming churn
  without improving the contract boundary.

Phase 7 stops here. No subsequent phase has been started automatically.
