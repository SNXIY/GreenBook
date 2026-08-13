# GreenBook Integration Contracts

This document describes the current service boundaries. Retired Moderation and legacy Assistant contracts are documented only in migration history.

## Service boundaries

```text
Frontend -> Java Community Backend       user-authenticated REST
Frontend -> Agent API                    user-authenticated Agent requests
Agent API/Worker -> in-process MCP       typed ToolMetadata and Tool contracts
MCP -> Java Backend                      `/api/v1/agent/*` external tool surface
MCP -> Creator Service                   Creator task/artifact contract
Agent API -> PostgreSQL                  conversation, task, execution projection, memory
Worker -> queue and execution stores     durable execution lifecycle
```

## Authentication

- User-facing calls use the GreenBook JWT and `AuthContext`.
- Service-to-service calls use service credentials or the explicit Creator handoff contract.
- User identity, tenant identity, and permissions are derived by trusted middleware; they are not LLM arguments.
- Tool policy is enforced by `ToolPolicyGate` before any invocation.

## Agent API

The canonical external Agent tool surface is `/api/v1/agent/*`. The retired `/api/v1/assistant-tools` surface is removed. Agent requests are translated into `Command`, `GoalTree`, `Task`, and typed `ExecutionInput` contracts.

## Creator handoff

Creator receives prepared, typed task/tool input from GreenBook. Creator may run its internal research, writing, quality, and human approval workflow, but it does not own GreenBook Command understanding, TaskManager state, cross-conversation memory, or global tool routing.

## Publication

Publication is an explicit side-effecting operation. Draft creation and publication use idempotency keys and the reliable execution layer. A normal publication path is:

```text
draft -> published
```

There is no Moderation Agent callback or moderation-owned publication state.

## Reliability

Long-running, side-effecting, or resumable work is submitted to the queue and handled by the Worker. Retry, checkpoint, lease, ledger/evidence, artifact references, and approval state remain execution-layer responsibilities.
