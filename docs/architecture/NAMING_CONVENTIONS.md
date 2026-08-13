# Naming Conventions

Names describe a real boundary. A class that only forwards one call should
not acquire a Manager, Service, or Adapter suffix.

| Name | Use when it owns |
| --- | --- |
| Agent | decision-making behavior over typed runtime state |
| Service | a business or application boundary |
| Runtime | the actual execution environment, loop, or state machine |
| Manager | lifecycle transitions and coordination state |
| Repository | durable data access for one domain |
| Store | persistence or an in-memory state collection with a stable contract |
| Adapter | conversion between contracts or protocols |
| Provider | an implementation of an external or infrastructure port |
| Compiler | deterministic transformation from one typed representation to another |
| Planner | produces or revises a plan from structured evidence |
| Resolver | selects a concrete target from candidates |
| Executor | performs an already-decided action |
| Tool | one concrete callable operation with a `domain.operation` name |
| Capability | semantic catalog entry; it does not own tool policy |
| Contract | typed boundary shared by independent owners |

## Canonical product names

- The Python products are `GreenBook Agent Runtime`, `Agent API`, and `Agent Worker`.
- The public creator product is `GreenBook Creator Service`; the `creator-agent` directory is retained to avoid an unhelpful filesystem breaking change.
- The frontend product is `GreenBook`; its assistant-era component names are `AgentPanel`, `agentService`, and `Agent` types.
- MCP is an MCP-compatible in-process tool runtime, not a standalone server deployment.
- `Conversation` means durable facts. `Context` means a bounded working projection.
- `Execution` means canonical runtime state. `Run` means public history projection.

## Contract names

`planning/contracts.py` owns `TaskPlan`, `PlanStep`, `PlanRevision`, and
`PlanningDecision`. `planning/graph.py` owns `PlanGraph` and `PlanNode`.
`execution/input.py` owns `ExecutionInput`. `Capability` owns semantic names,
descriptions, tags, candidate tool names, and semantic input/output types.
`ToolMetadata` and `ToolPolicyMetadata` in `packages/contracts` own concrete
schema and policy metadata.
