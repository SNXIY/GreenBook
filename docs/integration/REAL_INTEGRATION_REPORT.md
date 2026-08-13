# GreenBook Real Integration Report

> Phase 8.0 final report for the real local environment. No fake LLM, mock Java client, mock Creator, or test substitute was used.

# 1. 环境信息

- Date: 2026-08-12, Asia/Shanghai.
- Repository: `D:\agent\green-book`.
- Agent execution storage: PostgreSQL on `127.0.0.1:25432`.
- Community / identity storage: MySQL on `127.0.0.1:33306`.
- Queue and Creator support: Redis `26379`, Redpanda `39092`, Qdrant `26333`.
- Live model provider: configured and reachable during Cases 1–3, then returned HTTP 402 `Insufficient Balance` for later new reasoning calls.
- Dedicated integration user credentials were used, but secrets and JWT values are intentionally excluded from this report.

# 2. 启动方式

Infrastructure:

```powershell
docker compose up -d
```

Applications, started separately:

```powershell
.\scripts\start-be.ps1
.\scripts\start-creator.ps1
.\scripts\start-agent.ps1 -ApiOnly -NoReload
.\scripts\start-agent-worker.ps1 -WorkerAccessToken <fresh service JWT>
.\scripts\start-fe.ps1
```

The Agent API and Worker were deliberately separate processes for this validation. The API submitted to the durable Postgres queue; the Worker claimed and executed queue messages.

# 3. 服务状态

All required services were online at capture time. The complete table, PIDs, container names, health results, and log locations is in [REAL_ENVIRONMENT_STATUS.md](REAL_ENVIRONMENT_STATUS.md).

Summary:

| Layer | Status |
|---|---|
| Frontend 5173 | UP; HTTP 200 |
| Agent API 8094 | UP; HTTP 200; Java/Creator reachable |
| External Agent Worker | READY; Postgres consumer active |
| Java 8080 | UP; actuator HTTP 200 |
| Creator 8092 | UP; readiness HTTP 200 |
| PostgreSQL / Redis / MySQL / Redpanda | Docker healthy |
| Qdrant | running; `/collections` HTTP 200 |

# 4. 每个案例结果

| Case | Result | Evidence |
|---|---|---|
| 1. Query recent posts | PASS | Real Agent API -> Tool Runtime -> Java Agent Facade; run `227e0531-aa85-4272-a862-f28dbb603f4f` |
| 2. Create content | PASS | Real Worker -> Creator task/artifact -> Java draft; execution `9c86372a-b42d-4a48-9725-55a1643cda09` |
| 3. Schedule publish | PASS | Real Worker -> MCP -> Java schedule; execution `929cf550-2813-418b-8746-fb969b734370`, schedule `345780237461753856` |
| 4. Multi-turn update | BLOCKED | Pre-fix duplicate-task behavior was found and fixed; clean post-fix live rerun blocked by Creator/model availability |
| 5. Multi-goal task | NOT RUN | Blocked by live model provider HTTP 402; no fake plan was created |
| 6. HITL approval | PARTIAL / FAIL-CLOSED | Durable approval and restart recovery passed; final Java operation failed `AUTHENTICATION_FAILED` and was safely not replayed |

Detailed evidence is in [case1_query.md](case1_query.md), [case2_create_content.md](case2_create_content.md), [case3_schedule_publish.md](case3_schedule_publish.md), [case4_multiturn_update.md](case4_multiturn_update.md), [case5_multi_goal.md](case5_multi_goal.md), and [case6_hitl.md](case6_hitl.md).

## Representative end-to-end chain

```text
Frontend
  -> Agent API + JWT + Conversation
  -> Command / Goal / Planner
  -> Postgres Execution Queue
  -> Claim / Worker / Checkpoint / Ledger
  -> ToolSelector / ToolPolicyGate
  -> MCP-compatible in-process Tool Runtime
  -> Java Agent Facade or Creator HTTP API
  -> MySQL / Creator PostgreSQL / Redis / Qdrant
  -> completion projection and frontend status
```

# 5. Bug列表

## Resolved during Phase 8

| Severity | Bug | Fix / evidence |
|---|---|---|
| High | Standalone Worker did not create the durable approval row during an approval pause | Worker now composes `ApprovalRuntimeService`; startup reconciliation restored the real approval row `6b28fc9d-17bd-46ea-81df-440ecc357475` |
| High | Approval resume set the step to `RUNNING`, while the queue scheduler only claims `PENDING` steps | Resume now restores the approved step to `PENDING`; restart recovery and real queue re-enqueue were verified |
| High | Approved execution did not persist the approval marker into the requeued payload | Durable requeue now updates payload; observed `approval_granted=true` in the recovered queue message |
| Medium | Windows validation scripts treated non-fatal native stderr warnings as PowerShell failures | `verify-all.ps1` and `smoke-test.ps1` now use exit-code-based native checks; both pass |
| Medium | Agent Loop unit test still expected queue acceptance to be final success | Test now asserts asynchronous `RUNNING` handoff; full suite is green |

## Open findings

| Severity | Finding | Impact / next action |
|---|---|---|
| High | Delegated Java authorization expired before the approved publication was executed | Case 6 failed `AUTHENTICATION_FAILED`; reconcile the external operation, refresh authorization, and rerun. Do not blind-retry because evidence says side effect is possible |
| High | Live LLM provider balance was exhausted during validation (`402 Insufficient Balance`) | Cases 4 and 5 cannot be honestly completed until external model quota/configuration is restored |
| Medium | Some historical Agent run IDs are not returned by the current run-history endpoint after the run was retained outside the current projection window | Case 1 evidence is retained here from the live capture; investigate retention/projection policy before relying on old run IDs operationally |
| Medium | `creator-agent` full Ruff run reports 58 existing UP/SIM/style findings; active F/I checks and Creator tests pass | Clean up incrementally without changing Creator behavior |
| Low | Qdrant has no Docker Compose healthcheck | Add a non-invasive readiness check so orchestration distinguishes `running` from `ready` |
| Low | JVM, Browserslist, and pytest cache warnings remain | Maintenance only; they did not change command exit status or test results |

# 6. 修复建议

1. Restore live model-provider credit or configure an approved live provider, then rerun Cases 4 and 5 from fresh conversations.
2. Define a delegated-token refresh/reconciliation policy for long-running and approval-paused executions. A stale user JWT must not remain the only credential at the publication boundary.
3. Add an operator-facing external-operation reconciliation endpoint or workflow before permitting a retry after `side_effect_state=POSSIBLE`.
4. Add a Qdrant readiness check to Compose and the runtime health report.
5. Decide and document Agent run-history retention/projection semantics so old direct runs remain auditable.
6. Reduce Creator Ruff findings in small batches, prioritizing actual F/I errors before stylistic UP/SIM changes.

# 7. 当前系统真实完成度

## Capability completion

- Environment startup: **100% verified** for the requested local services.
- Frontend -> Agent API authentication and proxy: **100% verified**.
- Read-only community query: **100% verified**.
- Creator draft generation and Java draft handoff: **100% verified**.
- Schedule persistence and timezone conversion: **100% verified**.
- Durable queue / Worker / checkpoint / ledger path: **verified on Cases 2, 3, and 6**.
- Human approval persistence and restart recovery: **verified through approval and resume**; final side effect remains blocked by stale authorization.
- Multi-turn update: **not yet re-certified after the binding fix**.
- Multi-goal DAG: **not run because the real LLM provider was unavailable**.

Overall Phase 8 real-environment completion is **3 cases passed, 1 partial, 1 blocked, and 1 not run**. The system is not certified as a complete six-case product closure yet. That conclusion is intentional: the missing cases require external model credit and a fresh delegated authorization, and no substitute execution was used.

## Verification commands

The following checks passed after the Phase 8 fixes:

```text
uv run pytest -q                         571 passed, 1 skipped
uv run pytest --collect-only -q          572 collected
mvn test -q                              passed
creator-agent: uv run pytest -q         64 passed
zhiguang-fe: npm run lint                passed
zhiguang-fe: npm run build               passed
zhiguang-fe: npm run test:execution      passed
uv lock --check                          passed
uv run python -m compileall -q ...       passed
uv run ruff check active Agent runtime   passed
docker compose config --quiet            passed
scripts\verify-all.ps1                   passed
scripts\smoke-test.ps1                  passed
scripts\e2e-test.ps1 -HealthOnly         passed
```

## Final active tree

```text
green-book/
├── apps/
│   ├── agent_api/
│   ├── agent_worker/
│   └── backend/
├── packages/
│   ├── agent_core/
│   ├── contracts/
│   ├── creator_client/
│   ├── evaluation/
│   ├── java_client/
│   ├── observability/
│   └── security/
├── services/
│   └── greenbook_mcp/
├── creator-agent/
├── zhiguang-fe/
├── contracts/
├── infra/
├── scripts/
├── tests/
├── docs/
│   └── integration/
└── README.md
```

The repository also contains explicitly retained historical/archive material; it is not part of the active runtime tree above.
