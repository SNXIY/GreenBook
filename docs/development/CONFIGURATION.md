# Configuration

`.env.example` is the complete canonical local template. Runtime code and
scripts consume the names below; retired `ASSISTANT_*`, bare service URL
aliases, and duplicate Java aliases are not supported.

| Variable | Owner | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `GREENBOOK_JAVA_BASE_URL` | Java client | no | `http://127.0.0.1:8080` | Java Backend base URL |
| `GREENBOOK_AGENT_API_PORT` | Agent API | no | `8094` | Agent API host port |
| `GREENBOOK_MCP_PORT` | Business MCP | no | `8095` | GreenBook Business MCP Streamable HTTP host port |
| `GREENBOOK_BUSINESS_MCP_BASE_URL` | Agent/Worker | no | `http://127.0.0.1:8095/mcp` | Business MCP endpoint |
| `GREENBOOK_MCP_TRANSPORT` | Agent/Worker | no | `mcp` | Canonical production transport: `mcp`; explicit isolated test/development transport: `local` |
| `GREENBOOK_MCP_RUNTIME_TOKEN` | Agent/Business MCP | deployment | — | Internal runtime trust token; never model-editable |
| `GREENBOOK_MCP_TIMEOUT_SECONDS` | Agent/Worker | no | `30` | MCP request timeout |
| `GREENBOOK_AGENT_RUNTIME_STORAGE` | Agent Runtime | no | inferred | `postgres` or explicit `memory` |
| `GREENBOOK_AGENT_DATABASE_URL` | Agent Runtime | production | — | Agent Runtime PostgreSQL URL |
| `GREENBOOK_AGENT_RUNTIME_DATABASE_URL` | Agent Runtime | queue profile | derived | Durable execution database URL |
| `GREENBOOK_AGENT_EXECUTION_DISPATCH` | Agent Runtime | no | `queue` | `queue` or `direct` |
| `GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER` | Agent Worker | queue mode | `true` | Enables durable queue consumption |
| `GREENBOOK_AGENT_MAX_CONCURRENT_RUNS` | Agent Runner | no | `4` | Bounded active Agent Runs per process |
| `GREENBOOK_AGENT_MAX_CONCURRENT_RUNS_PER_CONVERSATION` | Agent Runner | no | `2` | Fairness limit for one Conversation |
| `GREENBOOK_AGENT_MAX_CONCURRENT_WORK_PER_CONVERSATION` | Agent Runtime | no | `3` | Independent ready Goals allowed to overlap |
| `GREENBOOK_AGENT_MAX_CONCURRENT_DIRECT_TOOLS` | Agent Runtime | no | `6` | Bounded direct read/tool invocations |
| `GREENBOOK_AGENT_EXECUTION_WORKER_CONCURRENCY` | Agent Worker | no | `4` | Bounded durable Execution handlers per worker |
| `GREENBOOK_AGENT_CONTINUATION_CONCURRENCY` | Agent API | no | `4` | Bounded Observation continuations per consumer |
| `GREENBOOK_AGENT_WORKER_ACCESS_TOKEN` | Agent Worker | external worker | — | Service JWT for an external worker |
| `GREENBOOK_AGENT_WORKER_HEALTH_FILE` | Agent Worker | no | `.runtime/agent-worker-health.json` | Worker heartbeat path |
| `GREENBOOK_AGENT_IDENTITY_ISSUER` | Agent identity | no | Java URL | Token issuer |
| `GREENBOOK_AGENT_IDENTITY_AUDIENCE` | Agent identity | no | `greenbook-agent-runtime` | Accepted service audience |
| `GREENBOOK_AGENT_IDENTITY_JWKS_URL` | Agent identity | no | Java JWKS URL | Public key endpoint |
| `GREENBOOK_AGENT_SERVICE_SHARED_SECRET` | Agent/Java | deployment | — | Agent service handoff secret |
| `GREENBOOK_CREATOR_POSTGRES_DB` | Compose | no | `mindflow_creator` | Shared PostgreSQL database name (legacy name retained) |
| `GREENBOOK_POSTGRES_HOST_PORT` | infrastructure | no | `25432` | PostgreSQL host port |
| `GREENBOOK_REDIS_HOST_PORT` | infrastructure | no | `26379` | Redis host port |
| `QDRANT_HTTP_HOST_PORT` | infrastructure | no | `26333` | Qdrant HTTP host port |
| `QDRANT_GRPC_HOST_PORT` | infrastructure | no | `26334` | Qdrant gRPC host port |
| `VITE_API_BASE_URL` | Frontend | no | Java URL | Java API base URL |
| `VITE_GREENBOOK_AGENT_URL` | Frontend | no | `/agent-api` | Agent API proxy path |
| `VITE_GREENBOOK_AGENT_PROXY_TARGET` | Frontend | no | Agent API URL | Vite Agent proxy target |
| `JWT_SECRET` | Java identity | deployment | — | Java signing secret |
| `DEEPSEEK_API_KEY` | model provider | model use | — | DeepSeek credential |

Additional Agent limits, model routing, tool HTTP, memory, approval, and
publication tuning variables are listed once in `.env.example` under the
`GREENBOOK_AGENT_*` namespace.
