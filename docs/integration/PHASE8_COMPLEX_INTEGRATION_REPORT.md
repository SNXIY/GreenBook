# Phase 8.1 Complex Real-world Agent Integration Report

Date: 2026-08-12 (Asia/Shanghai)

Overall status: **PARTIAL — real environment and core long-task path are operational, but dynamic fallback, one Creator revision contract, and broad-delete HITL routing remain incomplete.**

This report records real HTTP/service execution. No fake LLM, mock Java client, mock Creator, hard-coded plan, or test substitute was used.

## 1. Environment status

| Service | Port / endpoint | Status | Evidence |
|---|---:|---|---|
| Frontend | 5173 | UP | Vite process; `/agent-api/health` returns 200 |
| Agent API | 8094 | UP | `/health` returns `UP`, PostgreSQL storage, queue dispatch, external consumer |
| Agent Worker | external PostgreSQL consumer | READY | `.runtime/agent-worker-health.json`, PID 20696 during validation |
| Java Backend | 8080 | UP | actuator health 200; real Agent Facade calls returned Java responses |
| Creator Service | 8092 | UP/reachable | protected status endpoint requires JWT; real Creator task/artifact calls succeeded in Cases 1, 2, 3, and 4 |
| PostgreSQL | 25432 | healthy | `greenbook-postgres` |
| Redis | 26379 | healthy | `greenbook-redis` |
| MySQL | 33306 | healthy | `greenbook-mysql` |
| Kafka/Redpanda | 39092 | healthy | `greenbook-kafka` |
| Qdrant | 26333/26334 | running | `greenbook-qdrant` |

Relevant current process evidence: Frontend PID 57964, Java PID 32288, Creator PID 26956, Agent API PID 55772. PIDs are diagnostic evidence for this validation session, not stable deployment identifiers.

### Startup commands

```powershell
cd D:\agent\green-book
docker compose up -d
.\scripts\start-be.ps1
.\scripts\start-creator.ps1
.\scripts\start-agent.ps1 -NoReload -ApiOnly
.\scripts\start-agent-worker.ps1 -WorkerAccessToken <fresh service token>
.\scripts\start-fe.ps1
```

API logs: `.runtime/phase81-agent-api-goal-prompt-fix.out.log` and `.runtime/phase81-agent-api-goal-prompt-fix.err.log`.

Worker logs: `.runtime/phase81-agent-worker-resource-ref.out.log` and `.runtime/phase81-agent-worker-resource-ref.err.log`.

## 2. Case list

| Case | Scenario | Result |
|---|---|---|
| 1 | Community content growth, multi-goal | PASS for runtime; projection defect fixed afterward |
| 2 | Three-turn complex revision | PARTIAL |
| 3 | Tool failure and replanning | PARTIAL |
| 4 | Human approval and Worker restart | PASS |
| 5 | Multi-agent collaboration / column operation | FAIL end-to-end; architecture PARTIAL |
| 6 | High-risk broad delete | SAFE BLOCK; policy/HITL PARTIAL |

Detailed records:

- [case_1_growth_operation.md](phase8_complex/case_1_growth_operation.md)
- [case_2_multiturn_revision.md](phase8_complex/case_2_multiturn_revision.md)
- [case_3_failure_replanning.md](phase8_complex/case_3_failure_replanning.md)
- [case_4_hitl_recovery.md](phase8_complex/case_4_hitl_recovery.md)
- [case_5_multi_agent_collaboration.md](phase8_complex/case_5_multi_agent_collaboration.md)
- [case_6_high_risk_delete.md](phase8_complex/case_6_high_risk_delete.md)

## 3. Agent capability evaluation

| Capability | Assessment | Evidence |
|---|---|---|
| Intent understanding | PASS for composite content-growth and multi-turn revisions; targetless destructive intent is rejected too early | Cases 1, 2, 6 |
| Goal decomposition | PASS for Case 1 and the post-fix Case 5 graph; LLM task hints still need stronger semantic validation | Cases 1, 5 |
| Task management | PASS: Case 2 reused one Task across the first three turns; stale completion protection prevented an old execution from overwriting the current failed state | Case 2 |
| Multi-turn consistency | PASS for the required three turns; later narrow Creator revision failed at the external contract | Case 2 |
| Dynamic planning | PARTIAL: dynamic DAGs and plan revisions exist; automatic alternative-tool replanning was not completed | Cases 3, 5 |
| Tool selection | PASS for normal search/analytics/content/schedule paths; target-specific selection needs empty-result safeguards | Cases 1, 3, 5 |
| MCP communication | PASS: real MCP-compatible Tool Runtime dispatched canonical tools | Cases 1, 3, 4 |
| Creator collaboration | PASS when the Creator request matches its contract; title-only revision returned real HTTP 422 | Cases 1–4 |
| Human-in-the-loop | PASS for publish approval and restart recovery; broad delete stopped before PolicyGate | Cases 4, 6 |
| Reliable execution | PASS for queue, claim, checkpoint, ledger, retry bounds, approval persistence, and recovery; evidence projection still needs completion | Cases 1, 3, 4 |

The observed boundary remains:

```text
LLM: understand, decompose, select, reflect
Agent Runtime: Goal / Task / Plan / Execution lifecycle
Worker: claim, checkpoint, ledger, retry, recovery
Tool Runtime + MCP: schema-validated capability invocation
Java: community data, draft, publication, analytics business operations
Creator: research, writing, revision, artifact generation
```

## 4. Bugs and remaining gaps

### Critical

None observed in the tested flows. No destructive side effect occurred in Case 6.

### High

1. **Automatic alternative replanning is incomplete.** When Java access failed or search returned no target records, the runtime exhausted safe retries or stopped the dependent graph instead of selecting a valid lower-scope alternative. (Cases 3 and 5.)
2. **Creator title-only revision contract mismatch.** A real revision request returned HTTP 422 and `CREATOR_REQUEST_REJECTED`. (Case 2.)

### Medium

1. Broad targetless destructive requests return `TASK_TARGET_NOT_FOUND` before ToolPolicyGate/HITL. The fail-closed result is safe, but the user-facing approval route is incomplete. (Case 6.)
2. Java publication resource IDs are not consistently projected into the final Agent runtime result after an approved publish. (Case 4.)
3. The first Case 1 task projection left child Goal status stale after a completed Execution. The terminal projection fix was added and tested afterward.

### Low

1. One live Case 5 semantic-plan submission exceeded the practical request window. The system was not changed to replace it with a fake result; bounded provider/request timeout observability should be improved.

## 5. Implemented fixes in this validation

- Preserved AgentLoop planning decisions across runtime state transitions.
- Allowed synchronous read-only actions while retaining durable queue execution for side effects and long-running work.
- Added real ToolSelector resolution for composite capabilities before queue submission.
- Persisted tool name, arguments, idempotency, execution mode, and policy data through the deployed `execution_step` schema using checkpoint metadata compatibility fields.
- Reconciled partial LLM TaskNode hints with the complete executable Goal capability set, preventing the pre-fix two-step plan truncation observed in Case 5.
- Added stable resource references from upstream artifacts and downstream argument binding without copying artifact bodies.
- Added terminal Goal/Task projection and stale historical-execution guards.
- Added approval-time artifact materialization and duplicate-artifact protection.
- Kept strict structured-output validation while accepting the known provider schema-metadata echo.
- Tightened Goal decomposition guidance against target-specific tools without a target and against fabricated post IDs.

## 6. Test results

The focused tests covering the fixes passed:

```text
tests/unit/test_agent_loop.py
tests/unit/test_goal_decomposer.py
tests/unit/test_capability_executor.py
tests/unit/test_execution_worker.py
tests/unit/test_execution_persistence.py
tests/unit/test_phase17c_result_projection.py
```

Result: **46 passed** in the focused Phase 8.1 selection. The full root suite then completed with **582 passed, 1 skipped, 1 warning**. Collection completed with **583 tests collected**. The warning is a Windows permission warning while pytest tries to write `.pytest_cache`; it does not affect test execution.

Additional service checks:

- `uv run ruff check` on changed active files: PASS;
- `uv run python -m compileall ...`: PASS;
- `uv lock --check`: PASS;
- `docker compose config --quiet`: PASS;
- `git diff --check`: PASS;
- Frontend `npm run lint`: PASS;
- Frontend `npm run build`: PASS (356 modules transformed);
- Java `mvn test -q`: PASS;
- Creator `uv run pytest -q`: **64 passed**;
- The six real cases in this report are the primary validation; they are not replaced by unit tests.

The environment checks used in this session were:

- Agent API health: PASS;
- Frontend proxy health: PASS;
- Docker Compose services: healthy/running;
- real Java and Creator HTTP handoffs: PASS in the cases noted above;
- external Worker readiness: PASS;
- the frontend build emitted only stale browser-data advisories; no build error occurred.

## 7. Current real completion

GreenBook is a real multi-service Agent product, not merely `LLM + API call`: Case 1 completed a multi-goal content operation; Case 2 maintained one task across multiple revisions; Case 4 recovered a waiting approval after a Worker restart; and the failure cases demonstrated no-write and no-fabrication safety.

It is not yet accurate to claim full Phase 8.1 completion. The next required engineering work is:

1. add a policy-driven alternative-tool/replanner path after tool failure or empty result;
2. align Creator title-only revision payloads with a versioned contract;
3. route broad destructive intent to an explicit bounded approval/denial explanation without adding an unscoped delete tool;
4. complete publication resource projection and provider timeout observability;
5. rerun Cases 2, 3, 5, and 6 after those fixes with fresh evidence.

## 8. Final judgment

**Phase 8.1 complex real-environment validation: PARTIAL.** The core reliable runtime, real service boundaries, multi-turn Task binding, Creator collaboration, and HITL recovery are demonstrated. Dynamic semantic fallback and several external-contract/projection edges remain before the system can claim the full target:

```text
user goal
  -> agent understanding
  -> goal decomposition
  -> task planning
  -> dynamic execution
  -> tool orchestration
  -> external Java / Creator operation
  -> human collaboration
  -> reliable completion
```
