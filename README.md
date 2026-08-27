# GreenBook

GreenBook is a community platform with one Agent Runtime. A user message is
interpreted into a typed semantic request, resolved against conversation and
community resources, and executed through a durable control path. Java owns
community business truth; the Agent owns coordination, policy, execution, and
user-facing progress.

## Project overview

The active product surface supports community search/read, evidence-grounded
answers when evidence is available, draft creation, immediate or scheduled
publication, draft/schedule updates, cancellation, approval-gated deletion,
cross-turn resource identity, and bounded execution of independent draft
Objectives.

The Agent is a single runtime. Parallelism is Objective-centric and bounded;
the current proven slice is independent `CREATE_DRAFT` leaves with
`max_parallel_objectives=2`. It is not Multi-Agent and it does not add a
second Planner or Runtime.

## Product scope

The Agent surface is responsible for Conversation, Context, semantic
interpretation, Target and Temporal resolution, Task/Objectives, durable
Executions, approvals, retries, idempotency, resource binding, progress, and
projection. Java remains the source of truth for Draft, Schedule, Post,
publication, identity, notifications, and business state.

Comments/Interaction, Analytics/Hotspot, Moderation Agent, ResultSetBinding,
and ordinal cross-turn search-result binding are outside the current Agent
scope.

## Current capabilities

- READ: community search, post detail, and result viewing.
- RAG: conditional evidence retrieval, grounded generation, citations when
  available, and fail-closed/no-answer behavior when evidence is insufficient.
- WRITE: create draft, publish now, schedule, update draft/schedule, cancel
  schedule, and approval-gated delete.
- Multi-Objective: canonical Objective identity, dependencies, resource
  ownership, temporal/publication intent isolation, failure isolation, and
  bounded independent draft execution.
- Memory: Preference, Episodic, Semantic, and Procedural memory contracts;
  Memory is not current Task state and is not business truth. The current
  frontend memory surface is read-only.

Known limitation: RAG grounding/citation remains
`RAG_CURRENT_LIMIT_ACCEPTED`. This repository does not claim generic high-
accuracy knowledge answering.

## Architecture

```text
Frontend (zhiguang-fe)
  -> Agent API (apps/agent_api)
  -> Conversation / Context / Memory
  -> Command Interpreter
  -> Target + Temporal Resolver
  -> Task / canonical Objectives
  -> bounded Objective Scheduler
  -> ActionLoop / Execution
  -> Durable Runtime + PostgreSQL queue
  -> MCP Streamable HTTP tool boundary
  -> Java Backend
  -> MySQL / Redis / OSS / Kafka
       -> Elasticsearch / Qdrant projections
```

For independent Objectives, the scheduler may admit at most two eligible
Objective Executors. Dependencies, resource conflicts, mutation ordering, and
shared HITL gates remain serial. Every write still follows:

```text
Frontend -> TurnCoordinator -> ActionLoop -> Durable Runtime -> MCP -> Java
```

See [current architecture](docs/architecture/CURRENT_ARCHITECTURE.md),
[service communication](docs/architecture/SERVICE_COMMUNICATION.md), and the
[pre-cleanup acceptance report](docs/reports/GREENBOOK_FINAL_ACCEPTANCE_PRECOMMIT.md).

## Service topology

| Service | Location | Local endpoint | Responsibility |
|---|---|---:|---|
| Frontend | `zhiguang-fe` | `5173` | User input and projections |
| Agent API | `apps/agent_api` | `8094` | Authenticated Agent API and Run admission |
| Agent Worker | `apps/agent_worker` | queue profile | Durable queue consumer |
| Business MCP | `services/greenbook_mcp` | `8095/mcp` | Typed tool boundary |
| Java backend | `apps/backend` | `8080` | Community business truth |
| PostgreSQL | Docker Compose | `25432` | Agent durable runtime |
| Redis | Docker Compose | `26379` | Runtime/application support |
| Kafka | Docker Compose | `39092` | Business events/projections |
| Qdrant | Docker Compose | `26333/26334` | Vector projection |
| Elasticsearch | Docker Compose | `29200` | Search projection |
| MySQL | Docker Compose | `33306` | Java business data |

## Directory structure

```text
apps/
  agent_api/       Agent API composition root and routes
  agent_worker/    durable queue worker
  backend/         Java community backend
packages/
  agent_core/      context, commands, Objectives, ActionLoop, execution
  contracts/       shared contracts and user-facing activity types
  java_client/     Java HTTP client boundary
services/
  greenbook_mcp/   MCP Streamable HTTP server and typed tools
tests/
  unit/            focused contracts and invariants
  integration/     runtime integration tests
  e2e/             real browser and end-to-end tests
scripts/
  dev/             startup, diagnostics, and browser helpers
  ops/             local operational helpers
docs/
  architecture/   current ownership and boundaries
  development/    setup, startup, configuration, testing
  evaluation/     durable benchmark inputs and summaries
  reports/        current reports and acceptance outputs
  archive/        historical experiments and generated evidence
evaluation/       reusable semantic benchmark code and datasets
contracts/        OpenAPI contracts
infra/            infrastructure notes
zhiguang-fe/      React/TypeScript frontend
```

Runtime outputs, local logs, caches, browser profiles, and checkpoint patches
are not part of the primary source tree. Historical generated evaluation
evidence is under `docs/archive/evaluations/`.

## Technology stack

- Python 3.12+, `uv`, FastAPI, Pydantic, SQLAlchemy, PostgreSQL, Redis,
  Elasticsearch, Qdrant, and Streamable HTTP MCP.
- Java 21+, Spring Boot, MySQL, Redis, Kafka, and OSS/local storage.
- React 18, TypeScript, Vite, and CSS modules.
- Docker Compose for local infrastructure.

## Prerequisites

- Windows PowerShell or an equivalent shell.
- Python 3.12+, `uv`, Java 21+, Maven 3.9+, Node.js 20+, npm, and Docker
  Compose.
- A configured model provider key for Agent execution.
- A dedicated USER account for real Browser E2E. Never use an administrator
  account for destructive acceptance tests.

## Configuration

Copy `.env.example` to `.env` and fill deployment secrets locally. Never commit
`.env`, real model keys, JWT secrets, runtime tokens, passwords, or OSS
credentials. Agent and MCP runtime tokens must match, but their values must
never be printed or documented.

The canonical template is [.env.example](.env.example). Configuration details
are in [CONFIGURATION.md](docs/development/CONFIGURATION.md).

Important namespaces include `GREENBOOK_AGENT_*`,
`GREENBOOK_MCP_*`, `GREENBOOK_JAVA_*`, `VITE_*`, database settings, and model
provider settings. The default local topology uses queue dispatch with the
API's in-process consumer; an external worker is supported when configured.

## Startup order

Use the canonical startup guide: [STARTUP.md](docs/development/STARTUP.md).

The order is infrastructure -> Java -> MCP -> Agent API/Worker -> Frontend.
The scripts perform environment and health checks and do not bypass MCP for
business writes.

```powershell
Copy-Item .env.example .env
docker compose up -d
.\scripts\start-greenbook.ps1
.scripts\check-runtime-status.ps1
```

## Development

Install dependencies:

```powershell
uv sync
Set-Location zhiguang-fe
npm install
Set-Location ..
```

Use the individual scripts in `scripts/` when a service must be restarted in
isolation. Keep runtime tokens, ports, and dispatch mode consistent across
Agent API, Worker, and MCP.

## Testing

See [TESTING.md](docs/development/TESTING.md) for the test strategy and the
focused -> affected-family -> broad regression rule.

Typical checks:

```powershell
uv run pytest -q
uv run ruff check packages apps services tests
python -m compileall -q apps packages services scripts tests
Set-Location zhiguang-fe
npm run lint
npm run build
```

Run Browser E2E only with services and a dedicated E2E account. The final
Browser UX smoke is `tests/e2e/browser_ux_final_smoke.py`; it uses real UI
input and read-only probes.

## Evaluation

Evaluation entry points and retained summaries are indexed in
[docs/evaluation/README.md](docs/evaluation/README.md). The semantic benchmark
code remains under `evaluation/`; long historical generated snapshots are
archived under `docs/archive/evaluations/artifacts/`.

The final pre-cleanup evidence is in
[greenbook_final_acceptance_precommit.json](docs/evaluation/greenbook_final_acceptance_precommit.json)
and [GREENBOOK_FINAL_ACCEPTANCE_PRECOMMIT.md](docs/reports/GREENBOOK_FINAL_ACCEPTANCE_PRECOMMIT.md).

## Performance

Performance measurements are small local observations, not load benchmarks.
The valid BEFORE baseline and focused AFTER measurements record E2E, TTA,
LLM-call, context, memory, ActionLoop, queue, MCP, and Java timings. Provider
token timestamps are not exposed, so TTFT is reported as unavailable.

The current proven performance change is bounded parallel execution of two
independent draft Objectives plus narrowly scoped independent Memory repository
I/O. Java's approximately 100 ms response time is not the current critical
path. See `docs/archive/recovery/overnight-20260826-27/PERFORMANCE_BEFORE_AFTER.json`.

## Reliability

The runtime persists Runs, Executions, Operations, approvals, leases,
checkpoints, and observations. `RESULT_UNKNOWN` is reconciled rather than
blindly retried. HITL is Objective-scoped, resource identity is preserved
across turns, and conversation switching uses generation guards to prevent
stale response projection.

Writes require Durable Runtime admission and the MCP boundary. Direct tool
invocation and permission-policy bypasses are not supported.

## Known limitations

- RAG grounding/citation remains `RAG_CURRENT_LIMIT_ACCEPTED`.
- Formal first-useful-feedback instrumentation is not yet present.
- The current performance AFTER set is focused rather than a statistically
  equivalent multi-sample P1-P7 corpus.
- Parallelism is intentionally limited to independent `CREATE_DRAFT` leaves;
  dependent or conflicting Objectives remain serial.
- Historical compatibility modules remain until caller-level retirement proof
  exists.

## Out of scope

Do not infer active production support for Comments/Interaction,
Analytics/Hotspot, Moderation Agent, ResultSetBinding, ordinal cross-turn
search binding, Multi-Agent orchestration, a second Planner, or a new MQ.

## Project hygiene

Current source changes are kept separate from historical checkpoints and
generated runtime evidence. Before future cleanup, use
[PRE_CLEANUP_DIRTY_AUDIT.md](docs/reports/PRE_CLEANUP_DIRTY_AUDIT.md), stage
selectively, and run the documented regression checks. Do not use `git clean`,
`git reset --hard`, or `git add .` for repository housekeeping.
