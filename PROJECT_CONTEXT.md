# GreenBook Agent Runtime v2 — Current Project Context

This file describes the current runtime only. Historical architecture decisions belong in `docs/migration/` and are not production topology.

## Canonical services

```text
zhiguang-fe                         Frontend
apps/backend                        Java Community Backend
apps/agent_api                  Agent API
apps/agent_worker               Agent queue/execution worker
packages/agent_core             Agent intelligence and execution contracts
services/greenbook_mcp              In-process MCP/tool runtime
```

## Canonical Agent path

```text
User/API
  -> CommandInterpreter(message, ContextSnapshot)
  -> Command
  -> TargetResolver
  -> GoalDecomposer
  -> GoalTree
  -> TaskManager / TaskRepository
  -> AgentLoop (Observe -> Reason -> Act -> Reflect)
  -> DynamicPlanner / ToolSelector
  -> ToolPolicyGate
  -> GoalCompiler / TaskPlan
  -> ExecutionInput
  -> ExecutionSubmissionService
  -> Queue / Worker
  -> ToolRuntime / MCP
  -> Java Backend
```

## Layer boundaries

| Layer | Responsibility |
| --- | --- |
| Command | Understand the current user expression. |
| Context | Build the bounded working set from repositories and memory retrieval. |
| Goal | Represent the desired result and dependencies. |
| Task | Persist work lifecycle, ownership, priority, and versions. |
| Agent | Make runtime decisions and request the next action. |
| Planning | Compile or revise typed plans; it does not interpret user text. |
| Tool | Describe and select external capabilities. |
| Execution | Queue, worker, retry, checkpoint, lease, ledger, evidence, idempotency, artifacts, and approval. |
| Memory | Persist and retrieve long-term preferences, episodes, facts, and validated strategies. |

`ExecutionInput` is the only intelligence-to-execution request. Workers do not consume Command, Intent, or raw user text.

## Retired surfaces

The Moderation product, `moderation-agent`, `community-assistant-agent`, the standalone Creator Service (`creator-agent`, `packages/creator_client`), old Intent contracts, `/api/v1/assistant-tools`, and workflow-template routing are retired. Historical migration files may mention them, but they are not active callers or startup services. Content generation is now assistant-first: the host LLM writes drafts directly via `content.create_draft` and Java persists them.

## Important retained assets

`ToolPolicyGate`, permission and approval contracts, risk and side-effect metadata, security checks, idempotency, ledger, checkpoint, retry, lease, artifact, and human approval remain required execution safety infrastructure. `assistant_runs` remains the canonical runtime-history projection.

## MCP boundary

`services/greenbook_mcp` is imported by the Agent API/Worker as an in-process runtime package. The repository does not currently deploy a separate MCP server.
