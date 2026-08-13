# Phase 8 Case 1: Query recent posts

## Input

`查询我的最近帖子`

## Result

**PASS — real direct query path.**

| Field | Evidence |
|---|---|
| Conversation | `ede7636f-08f0-4395-8a44-700ba315e052` |
| Agent run | `227e0531-aa85-4272-a862-f28dbb603f4f` |
| Trace | `648e2567-c68d-4066-b2ce-6553712d1f72` |
| Result | `COMPLETED` |
| Java verification | `GET /api/v1/agent/me/posts?page=1&size=20` returned HTTP 200 and an empty list for the dedicated integration user |

## Real chain

```text
Frontend / Agent API request
  -> JWT authentication
  -> Conversation and Command understanding
  -> Tool selection
  -> MCP-compatible in-process Tool Runtime
  -> Java Agent Facade HTTP API
  -> MySQL-backed community data
  -> Agent run result
```

This was a read-only direct operation, so it did not create a durable Execution. The frontend uses the Agent run status API for this direct path; the durable Execution event stream is reserved for queued executions. No internal Python function was called as a substitute for the HTTP path.

## Checks

- Natural-language input was sent as UTF-8 JSON through the Agent API.
- The Java call used the real authenticated user context.
- The returned result was completed and did not invent posts when the real account had none.
- Frontend proxy validation separately confirmed `GET /agent-api/api/v1/agent/conversations` returned HTTP 200 with the same real JWT.
