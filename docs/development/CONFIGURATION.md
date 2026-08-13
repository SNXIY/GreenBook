# Configuration

`.env.example` is the complete canonical local template. Runtime code and
scripts consume the names below; retired `ASSISTANT_*`, bare service URL
aliases, and duplicate Java/Creator aliases are not supported.

| Variable | Owner | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `GREENBOOK_JAVA_BASE_URL` | Java client | no | `http://127.0.0.1:8080` | Java Backend base URL |
| `GREENBOOK_CREATOR_BASE_URL` | Creator client | no | `http://127.0.0.1:8092` | Creator Service base URL |
| `GREENBOOK_AGENT_CREATOR_COMPLETION_DEADLINE_SECONDS` | Agent → Creator client | no | `600` | Maximum time to wait for a Creator task to reach a terminal state |
| `GREENBOOK_AGENT_API_PORT` | Agent API | no | `8094` | Agent API host port |
| `GREENBOOK_AGENT_RUNTIME_STORAGE` | Agent Runtime | no | inferred | `postgres` or explicit `memory` |
| `GREENBOOK_AGENT_DATABASE_URL` | Agent Runtime | production | — | Agent/Creator PostgreSQL URL |
| `GREENBOOK_AGENT_RUNTIME_DATABASE_URL` | Agent Runtime | queue profile | derived | Durable execution database URL |
| `GREENBOOK_AGENT_EXECUTION_DISPATCH` | Agent Runtime | no | `queue` | `queue` or `direct` |
| `GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER` | Agent Worker | queue mode | `true` | Enables durable queue consumption |
| `GREENBOOK_AGENT_WORKER_ACCESS_TOKEN` | Agent Worker | external worker | — | Service JWT for an external worker |
| `GREENBOOK_AGENT_WORKER_HEALTH_FILE` | Agent Worker | no | `.runtime/agent-worker-health.json` | Worker heartbeat path |
| `GREENBOOK_AGENT_IDENTITY_ISSUER` | Agent identity | no | Java URL | Token issuer |
| `GREENBOOK_AGENT_IDENTITY_AUDIENCE` | Agent identity | no | `greenbook-agent-runtime` | Accepted service audience |
| `GREENBOOK_AGENT_IDENTITY_JWKS_URL` | Agent identity | no | Java JWKS URL | Public key endpoint |
| `GREENBOOK_AGENT_SERVICE_SHARED_SECRET` | Agent/Java | deployment | — | Agent service handoff secret |
| `GREENBOOK_CREATOR_HANDOFF_SHARED_SECRET` | Creator/Java | deployment | — | Creator publication handoff secret |
| `GREENBOOK_CREATOR_AGENT_SHARED_SECRET` | Creator/Agent | deployment | — | Creator trusted-proxy secret |
| `GREENBOOK_CREATOR_API_PORT` | Creator Service | no | `8092` | Creator Service host port |
| `GREENBOOK_CREATOR_DATABASE_URL` | Creator Service | no | SQLite | Creator database URL |
| `GREENBOOK_CREATOR_POSTGRES_DB` | Creator Service | Compose | `mindflow_creator` | Shared PostgreSQL database name |
| `GREENBOOK_POSTGRES_HOST_PORT` | infrastructure | no | `25432` | PostgreSQL host port |
| `GREENBOOK_REDIS_HOST_PORT` | infrastructure | no | `26379` | Redis host port |
| `QDRANT_HTTP_HOST_PORT` | infrastructure | no | `26333` | Qdrant HTTP host port |
| `QDRANT_GRPC_HOST_PORT` | infrastructure | no | `26334` | Qdrant gRPC host port |
| `GREENBOOK_AGENT_REDIS_URL` | Agent Runtime | queue profile | — | Agent queue/limit Redis URL |
| `GREENBOOK_AGENT_MEMORY_QDRANT_URL` | Agent Memory | semantic memory | — | Qdrant endpoint |
| `GREENBOOK_AGENT_MEMORY_QDRANT_COLLECTION` | Agent Memory | semantic memory | `greenbook_agent_memory` | Collection name |
| `VITE_API_BASE_URL` | Frontend | no | Java URL | Java API base URL |
| `VITE_GREENBOOK_AGENT_URL` | Frontend | no | `/agent-api` | Agent API proxy path |
| `VITE_GREENBOOK_AGENT_PROXY_TARGET` | Frontend | no | Agent API URL | Vite Agent proxy target |
| `VITE_GREENBOOK_CREATOR_URL` | Frontend | no | `/creator-api` | Creator API proxy path |
| `JWT_SECRET` | Java identity | deployment | — | Java signing secret |
| `DEEPSEEK_API_KEY` | model provider | model use | — | DeepSeek credential |

Additional Agent limits, model routing, tool HTTP, memory, approval, and
publication tuning variables are listed once in `.env.example` under the
`GREENBOOK_AGENT_*` namespace.
