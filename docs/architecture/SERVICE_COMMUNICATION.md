# Service Communication

## Active services

| Owner | Host port | Protocol / boundary |
| --- | ---: | --- |
| Java Backend | 8080 | REST, `/api/v1/agent/*`, identity and community data |
| Creator Service | 8092 | Creator task API and studio |
| Agent API | 8094 | Conversation and runtime API, `/api/v1/agent/*` |
| Frontend | 5173 | Vite development server |

The frontend uses `/api` for Java, `/agent-api` for Agent API, and
`/creator-api` for Creator Service in local Vite proxy mode. These proxy paths
are frontend-only and are rewritten before the request reaches a service.

## Internal calls

- Agent API/Worker -> Java through `packages/java_client`.
- Agent API/Worker -> Creator through `packages/creator_client`.
- Agent API/Worker -> MCP through the in-process `GreenBookMCPServer` tool runtime.
- Creator -> Java through its Creator-owned provider boundary.
- Queue and persistence use the root Compose PostgreSQL, Redis, and Qdrant services.

Identity is issued by Java and validated at each service boundary. User and
tenant identity comes from the validated token, never from a model-supplied
tool argument.
