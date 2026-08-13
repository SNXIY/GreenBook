# Phase 8.2 Real Integration Gap Closure Report

Date: 2026-08-12 (Asia/Shanghai)

Certification: **PARTIALLY_CERTIFIED**

This report records real HTTP/service execution. No fake LLM, mock Java client, mock Creator, mock ToolResult, hard-coded workflow, or direct internal-function shortcut was used as acceptance evidence.

## 1. Phase 8.1 Gaps

Phase 8.1 exposed five real gaps:

1. Creator rejected narrow title-only revisions with HTTP 422.
2. Tool failure stopped safely but did not demonstrate evidence-aware continuation.
3. Empty search results were safely schema-blocked but did not consistently cause adaptive planning.
4. Broad destructive intent was safe but surfaced as `TASK_TARGET_NOT_FOUND` instead of a scope policy decision.
5. Completion projections did not consistently expose external resource references.

## 2. Root Cause

| Gap | Root cause |
|---|---|
| Creator revision | Revision scope and parent artifact were not part of the narrow request contract. |
| Tool failure | Failure evidence was not always fed back through the dynamic planner boundary. |
| Empty result | `CONTINUE` after `EMPTY` was not uniformly repaired into a validated read-only decision. |
| Broad delete | Target resolution ran before policy-level unbounded-scope classification. |
| Projection | Java/Creator references were not consistently carried through completion projections. |
| Case F timeout | Creator client used a fixed 240-second completion deadline while the tool policy allowed 600 seconds. |

## 3. Fix

- Added explicit Creator revision scope and source artifact propagation for title/content/style/structure-equivalent revisions.
- Added evidence-aware empty observation repair in `DynamicPlanner`.
- Preserved failure kind, request evidence, and candidate read-only capabilities for dynamic replanning.
- Added broad destructive scope normalization and fail-closed policy audit semantics.
- Added Creator and Java resource references to result/projection paths.
- Made Creator completion waiting configurable with canonical environment variable `GREENBOOK_AGENT_CREATOR_COMPLETION_DEADLINE_SECONDS` (default 600 seconds) and removed the hard-coded 240-second MCP override.
- Corrected Command guidance so content creation maps to canonical `GENERATE_CONTENT`, not the retired `SAVE_DRAFT` capability.
- Corrected active Agent Runtime import ordering in `execution/__init__.py`.

## 4. Architecture Impact

**No major architecture change.** The existing boundaries remain:

```text
Command -> GoalTree -> TaskManager -> AgentLoop -> DynamicPlanner
        -> TaskPlan -> ExecutionInput -> Queue/Worker
        -> Checkpoint/Ledger/Recovery -> ToolRuntime -> MCP
        -> Java Backend / Creator Service
```

The changes are contract, evidence, timeout, policy, and projection corrections inside existing boundaries.

## 5. Real Evidence

The detailed evidence is in:

- [Case A](phase8_2/CASE_A_CREATOR_NARROW_REVISION.md)
- [Case B](phase8_2/CASE_B_EMPTY_RESULT_REPLAN.md)
- [Case C](phase8_2/CASE_C_ALTERNATIVE_TOOL_REPLAN.md)
- [Case D](phase8_2/CASE_D_HITL_RECOVERY.md)
- [Case E](phase8_2/CASE_E_BROAD_DESTRUCTIVE_SAFETY.md)
- [Case F](phase8_2/CASE_F_COMPLEX_BUSINESS_CLOSURE.md)

The current Case F is deliberately still waiting for user approval. The report does not treat a pending approval as a successful publication.

## 6. Before / After

| Behavior | Before | After |
|---|---|---|
| Title-only Creator revision | HTTP 422 / contract rejection | Real `TITLE_ONLY` revision, same lineage, body SHA unchanged |
| Empty read | Could continue with an invalid downstream target | Evidence-bounded alternative or `ASK_HUMAN`; no fabricated ID |
| Read dependency failure | Safe stop after retry budget | Dynamic changed-argument replan completed a real analysis/draft case |
| Broad delete | `TASK_TARGET_NOT_FOUND` | `REJECT_UNBOUNDED_SCOPE`, audit event, zero destructive calls |
| Completion evidence | External refs could be missing | Draft, Creator task/artifact, approval refs exposed in Case F projection |
| Long Creator task | Agent’s 240-second wait could expire before Creator finished | Configurable 600-second completion deadline; real Case F Creator/draft completed |

## 7. Cases A–F

| Case | Result |
|---|---|
| A — Creator narrow revision | PASS |
| B — Empty-result adaptive planning | PASS as evidence-bounded safe stop |
| C — Failure replanning | PASS for changed-argument dynamic replan; distinct alternative Tool B not available |
| D — HITL recovery | PASS |
| E — Broad destructive safety | PASS |
| F — Complex business closure | PARTIAL: real Creator/draft/approval path reached, but schedule is pending explicit approval and data-provenance projection remains incomplete |

## 8. Remaining Gaps

1. Do not claim the final Case F scheduled publication until the pending approval is explicitly approved and the resulting Java schedule resource is projected.
2. Resolve the discrepancy between the Agent-owned-post/analytics tool projection (zero records) and the direct Java account read (real records) for the same integration identity.
3. After all candidate evidence sources are empty, enforce either an explicit provenance-labelled continuation contract or `ASK_HUMAN`; do not silently proceed with a normal evidence-bearing content plan.
4. A separate equivalent read-only Tool B was not available in Case C; the demonstrated fallback was a dynamic changed-argument read.
5. Public run DTOs still do not expose a separate numeric `goal_tree_version`/`plan_version`; evidence uses durable event IDs and plan revision step IDs instead.

## 9. Regression Results

Full and focused checks completed during this closure:

```text
uv run pytest -q
591 passed, 1 skipped, 1 warning

uv run pytest --collect-only -q
592 tests collected

creator-agent: uv run pytest -q
64 passed

apps/backend: mvn test -q
PASS

zhiguang-fe: npm run lint
PASS

zhiguang-fe: npm run build
PASS (356 modules transformed)

uv lock --check
PASS

Agent Runtime/MCP/Creator client Ruff
PASS

Creator Ruff F/I selection
PASS

compileall (active Agent Runtime/MCP/clients)
PASS

docker compose config --quiet
PASS

git diff --check
PASS
```

The root pytest warning is a Windows permission warning while pytest tries to write `.pytest_cache`; it does not affect test execution. Creator full Ruff still reports 58 historical UP/SIM/B905/typing/style findings; Creator F/I are clean, and the findings were not introduced by the Phase 8.2 fix. Creator full tests remain green.

## 10. Final Certification

**PARTIALLY_CERTIFIED**

Reason: Cases A–E have real evidence at the stated level. Case F has real multi-goal planning, dynamic empty-result handling, Creator strategy, Creator generation, Java draft persistence, and an approval boundary, but it is intentionally pending user approval and still lacks complete personal-vs-community evidence provenance. Therefore `CERTIFIED` would overstate the current system.
