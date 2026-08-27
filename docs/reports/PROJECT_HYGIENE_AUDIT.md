# GreenBook Project Hygiene Audit

Date: 2026-08-27
Branch: `feature/hybrid-search-rag`
Scope: pre-cleanup stabilization, structure cleanup, documentation standardization

## 1. Baseline and safety

The pre-cleanup working tree was captured before any cleanup mutation. The
pre-cleanup stabilization chain is:

- `a6f6256 fix: stabilize multi-objective semantics and runtime`
- `a45a00b feat: add bounded parallel objective execution`
- `779bcf3 perf: overlap safe memory I/O and add runtime metrics`
- `a037b93 test: add final browser and evaluation coverage`

The annotated recovery point is
`greenbook-pre-cleanup-stable-20260827`. The original audit recorded 43
tracked modified files and 14 untracked top-level entries. No reset, force
push, broad delete, or `git add .` was used.

## 2. Keep, archive, and delete decisions

All production/runtime files, affected tests, evaluation harnesses, reusable
evaluation data, reports, and browser helpers were kept. No valuable
regression or evaluation test was deleted, merged away, or rewritten.

Historical generated evaluation artifacts were moved without content changes:

- `artifacts/` -> `docs/archive/evaluations/artifacts/`
- 2,411 files preserved, approximately 70 MB

Recovery material was moved to an ignored, recoverable archive:

- `checkpoints/` -> `docs/archive/recovery/checkpoints/`
- `docs/worklogs/overnight-20260826-27/` ->
  `docs/archive/recovery/overnight-20260826-27/`
- four one-time safe inspection helpers ->
  `docs/archive/recovery/inspection/`

No material file was permanently deleted. Root runtime caches, `.runtime`,
local data, and reusable `evaluation/` and `contracts/` directories remain
outside the tracked documentation archive.

## 3. Standardized structure

The repository now has an explicit separation between:

- `apps/`, `packages/`, `services/`: production runtime code
- `tests/`: unit, integration, e2e, and evaluation tests
- `scripts/`: repeatable development and operational helpers
- `contracts/`: API contracts
- `infra/`: infrastructure notes and topology
- `docs/architecture/`: current architecture and boundaries
- `docs/development/`: setup, startup, configuration, and testing guidance
- `docs/evaluation/`: evaluation index and formal result artifacts
- `docs/reports/`: durable audits and acceptance reports
- `docs/archive/`: historical generated evidence and recovery material

Evaluation data remains separate from production data. Historical reports were
retained so evidence remains auditable; links were updated for the new archive
paths.

## 4. Documentation and configuration

- Replaced the root README with current scope, architecture, topology,
  startup, testing, evaluation, performance, reliability, and known-limit
  guidance.
- Added [STARTUP.md](../development/STARTUP.md) as the canonical local startup
  and health-check guide.
- Added [evaluation README](../evaluation/README.md) with focused, affected-
  family, and broad regression policy.
- Standardized `TESTING.md`, `LOCAL_SETUP.md`, and `CONFIGURATION.md`.
- Updated current architecture documentation for bounded max-two independent
  `CREATE_DRAFT` execution, RAG accepted limitations, and Memory ownership.
- Replaced unsafe-looking `.env.example` credential values with explicit local
  placeholders and updated startup validation for those placeholders.
- Added ignores for transient checkpoints, overnight recovery logs, and the
  recovery archive. No actual credentials were added to the repository.

The documented scope remains unchanged: no new business feature, MQ, second
Planner, Multi-Agent runtime, RAG experiment, or Memory semantic change was
introduced by cleanup.

## 5. Cleanup regression

Cleanup did not alter production/runtime code after the pre-cleanup stable
commit. Validation results:

- Python compileall: PASS
- focused Python regression: **90 passed**, one existing pytest cache warning
- previously completed full `.venv` unit audit: **1602 passed, 0 failed**, one
  pytest cache warning; not mechanically rerun
- frontend TypeScript lint: PASS
- frontend projection/contract/lifecycle/semantic tests: PASS
- frontend production build: PASS
- `docker compose config --quiet`: PASS
- Frontend `5173`, Agent API `8094`, MCP `8095`, and Java `8080`: UP / HTTP 200
- PostgreSQL, Redis, Kafka, Elasticsearch, Qdrant, and MySQL: running
- runtime token configuration: PRESENT; Agent/MCP runtime configuration:
  MATCH; secret values were not printed

The final Browser UX smoke remains PASS from the immediately preceding
stabilization checkpoint: rapid double-send admitted exactly one completed Run,
stale response did not leak across conversations, and user-facing progress was
visible. No browser production code changed during cleanup, so the full
Browser matrix was not rerun.

## 6. Functional baseline retained

The pre-cleanup acceptance report remains the source of truth for functional
stabilization:

- MO-08 root cause: `OBJECTIVE_DECOMPOSITION`, specifically mixed
  CREATE/COMPLEX span-grouping expansion; focused fix is in the Interpreter.
- MO-08: two canonical Objectives, zero duplicate SEARCH, DELETE scoped to
  `WAITING_APPROVAL`, zero destructive writes before approval.
- Multi-Objective: focused MO-01..MO-12 evidence retained; no full matrix rerun.
- Parallel execution: bounded single-runtime executor, max two independent
  `CREATE_DRAFT` leaves; Durable Runtime -> MCP -> Java path preserved.
- Real Browser parallel comparison: 73,859 ms -> 55,672 ms; runtime 65,793 ms
  -> 48,071 ms; observed overlap 6,730.941 ms.
- RAG canonical admission: PASS; grounding/citation remains the explicitly
  accepted `RAG_CURRENT_LIMIT_ACCEPTED_PARTIAL` limitation.
- Formal first-useful-feedback instrumentation and statistically equivalent
  multi-sample P1-P7 performance evidence remain open.

## 7. Commits and final state

The structure/documentation cleanup commit is:

- `d18669c chore: standardize project structure and documentation`

The follow-up commit containing this audit is created after the cleanup
regression. The final annotated baseline tag is
`greenbook-clean-baseline-20260827` and is created only after this report and
the final clean-tree check are complete.

The feature branch and both annotated baseline tags are pushed to `origin`.
The final expected Git state is a clean worktree on
`feature/hybrid-search-rag`, with recovery archives ignored and preserved on
disk.

## 8. Remaining technical debt and next priorities

1. Add formal first-useful-feedback instrumentation and repeat equivalent
   performance samples when strict performance acceptance is required.
2. Revisit RAG grounding/citation only under a separately approved RAG change.
3. Continue reviewing historical scripts and reports incrementally; keep
   evidence until its replacement and retention decision are explicit.
