# GreenBook Agent Platform

Python Agent Monorepo for the GreenBook community platform.

## Architecture

```
apps/
  assistant_api/     - FastAPI chat endpoint with lightweight tool-calling agent
  assistant_worker/  - Async worker for Kafka consumers and scheduled jobs

services/
  greenbook_mcp/     - MCP Streamable HTTP Server exposing business tools
  creator_agent/     - LangGraph-based professional content creation service

packages/
  assistant_core/    - Agent loop, SessionContext, ConversationMemory, prompts
  contracts/         - ToolResult, ErrorCode, BusinessEvent, AuthContext
  java_client/       - Async HTTPX client with retry, idempotency, error mapping
  creator_client/    - Async client for Creator Agent Task API
  security/          - JWT validation, AuthResolver
  observability/     - OpenTelemetry tracing
  evaluation/        - E2E and contract test harness
```

## Quick Start

```bash
# Infrastructure only (PostgreSQL, Redis, Qdrant)
docker compose -f infra/docker-compose.dev.yml up -d

# Install dependencies (requires uv)
uv sync

# Run assistant API
cd apps/assistant_api
uv run uvicorn greenbook_assistant_api.main:create_app --factory --reload --port 8094

# Run tests
uv run pytest tests/ -v
```

## Environment Variables

Copy `.env.example` to `.env` and configure:
- `ASSISTANT_DEEPSEEK_API_KEY` - DeepSeek API key
- `ASSISTANT_JAVA_BASE_URL` - Java backend URL
- `ASSISTANT_IDENTITY_JWKS_URL` - JWT JWKS endpoint

## Key Design Decisions

1. **Lightweight single-agent loop** — no LangGraph for conversations
2. **MCP Streamable HTTP** for business tools — one server, all capabilities
3. **Creator Agent** retains LangGraph for content creation workflows
4. **Java backend** is the source of truth for all business data
5. **AuthContext from JWT only** — model never provides user_id or tenant_id
6. **Idempotency-Key** required for all write operations
7. **DEPENDENCY_UNAVAILABLE** for all connection failures — never RESULT_UNKNOWN

## Legacy Code

The following directories are legacy pending golden E2E verification:
- `community-assistant-agent/` — old multi-agent hierarchical system
- `moderation-agent/` — deprecated moderation service
- `zhiguang-be/` — Java backend (migrating to independent repo)
- `zhiguang-fe/` — frontend (migrating to independent repo)
- `scripts/` — legacy helper scripts
- `design-system/` — design assets
