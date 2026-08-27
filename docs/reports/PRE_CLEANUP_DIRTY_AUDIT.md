# GreenBook Pre-Cleanup Dirty Audit

Date: 2026-08-27
Branch: `feature/hybrid-search-rag`
Pre-cleanup HEAD: `2b593fa4d521bca30a5a913eed2a0df6db308450`
Original dirty count: **43 tracked modified + 14 untracked top-level entries**
Policy: no reset, no clean, no force push. This audit is before any cleanup.

## Classification policy

`PRODUCTION_KEEP`, `TEST_KEEP`, `EVALUATION_KEEP`, `DOC_KEEP`, `OBSERVABILITY_KEEP`, and `DEV_SCRIPT_KEEP` are candidates for the stable commit. `TEMPORARY_REMOVE`, `CHECKPOINT_ARCHIVE`, `GENERATED_IGNORE`, and `UNKNOWN_REVIEW` are not staged in the stable commit. No file is deleted solely because it looks old.

## Tracked modified files

| Path | Category | Why changed / current caller | Needed after reboot | Needed for regression | Enter Git | Remain after cleanup |
|---|---|---|---|---|---|---|
| `apps/agent_api/greenbook_agent_api/api/routes.py` | PRODUCTION_KEEP | Agent public route and activity projection changes; called by API | yes | yes | yes | yes |
| `apps/agent_api/greenbook_agent_api/main.py` | PRODUCTION_KEEP | runtime wiring and Java observability host callback; API startup caller | yes | yes | yes | yes |
| `apps/agent_api/greenbook_agent_api/services/action_loop_executor.py` | PRODUCTION_KEEP | Objective admission/continuation and identity; API runtime caller | yes | yes | yes | yes |
| `apps/agent_api/greenbook_agent_api/services/retrieval_synthesis_projection.py` | PRODUCTION_KEEP | retrieval/citation projection; RAG route caller | yes | yes | yes | yes |
| `apps/agent_api/greenbook_agent_api/services/turn_coordinator.py` | PRODUCTION_KEEP | turn admission and semantic routing; API caller | yes | yes | yes | yes |
| `apps/agent_worker/greenbook_agent_worker/main.py` | PRODUCTION_KEEP | worker runtime observability wiring; worker startup caller | yes | yes | yes | yes |
| `apps/backend/scripts/measure_agent_performance_baseline.py` | EVALUATION_KEEP | repeatable performance harness; evaluation caller | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/actionloop/loop.py` | PRODUCTION_KEEP | Objective selection, bounded parallelism, failure isolation | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/command/interpreter.py` | PRODUCTION_KEEP | MO-08 span grouping and semantic decomposition | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/command/models.py` | PRODUCTION_KEEP | canonical Objective semantic model | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/command/target.py` | PRODUCTION_KEEP | canonical target/resource resolution | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/execution/capability_executor.py` | PRODUCTION_KEEP | capability/resource identity and durable execution | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/execution/queue_execution_handler.py` | PRODUCTION_KEEP | durable queue execution handling | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/execution/runtime_agent_service.py` | PRODUCTION_KEEP | canonical runtime execution boundary | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/memory/retriever.py` | PRODUCTION_KEEP | independent repository I/O only; runtime memory caller | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/observability/run_metrics.py` | OBSERVABILITY_KEEP | run timing/latency metrics; runtime/evaluation caller | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/task/manager.py` | PRODUCTION_KEEP | concurrent execution binding and CAS merge | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/task/objective_compat.py` | PRODUCTION_KEEP | Objective compatibility/admission | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/task/objective_reducer.py` | PRODUCTION_KEEP | canonical Objective reduction/dedupe | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/time_parser.py` | PRODUCTION_KEEP | objective-scoped temporal parsing | yes | yes | yes | yes |
| `packages/agent_core/greenbook_agent_core/turn/fast_path_gate.py` | PRODUCTION_KEEP | canonical ANSWER_FROM_KNOWLEDGE admission | yes | yes | yes | yes |
| `packages/contracts/greenbook_contracts/user_activity.py` | PRODUCTION_KEEP | public user-facing activity mapping | yes | yes | yes | yes |
| `packages/java_client/greenbook_java_client/client.py` | OBSERVABILITY_KEEP | host-injected observability boundary; Java client caller | yes | yes | yes | yes |
| `scripts/dev/overnight_stable_baseline_browser.py` | DEV_SCRIPT_KEEP | real-browser evaluator used by retained evidence | yes | yes | yes | yes |
| `scripts/memory_evaluation_harness.py` | EVALUATION_KEEP | memory evaluation and dirty-scope harness | yes | yes | yes | yes |
| `services/greenbook_mcp/greenbook_mcp_server/client.py` | PRODUCTION_KEEP | MCP client transport/runtime token boundary | yes | yes | yes | yes |
| `services/greenbook_mcp/greenbook_mcp_server/protocol.py` | PRODUCTION_KEEP | MCP protocol contract | yes | yes | yes | yes |
| `services/greenbook_mcp/greenbook_mcp_server/server.py` | PRODUCTION_KEEP | MCP server/tool boundary | yes | yes | yes | yes |
| `tests/unit/test_action_loop_phase3b.py` | TEST_KEEP | ActionLoop safety/submit regressions | yes | yes | yes | yes |
| `tests/unit/test_capability.py` | TEST_KEEP | capability contract regression | yes | yes | yes | yes |
| `tests/unit/test_command_runtime.py` | TEST_KEEP | MO-08/interpreter runtime regression | yes | yes | yes | yes |
| `tests/unit/test_cross_turn_resource_identity.py` | TEST_KEEP | cross-turn target/resource identity | yes | yes | yes | yes |
| `tests/unit/test_long_term_memory_system_evaluation.py` | TEST_KEEP | memory authority/evaluation isolation | yes | yes | yes | yes |
| `tests/unit/test_memory_retriever.py` | TEST_KEEP | memory I/O concurrency and semantic invariants | yes | yes | yes | yes |
| `tests/unit/test_objective_completion_owner.py` | TEST_KEEP | Objective completion/projection ownership | yes | yes | yes | yes |
| `tests/unit/test_real_mcp_boundary.py` | TEST_KEEP | real MCP catalog/boundary regression | yes | yes | yes | yes |
| `tests/unit/test_reliable_execution_wiring_phase4b1.py` | TEST_KEEP | durable reliability wiring | yes | yes | yes | yes |
| `tests/unit/test_retrieval_synthesis_projection.py` | TEST_KEEP | RAG projection/fail-closed regression | yes | yes | yes | yes |
| `tests/unit/test_schedule_draft_ownership.py` | TEST_KEEP | schedule/draft ownership regression | yes | yes | yes | yes |
| `tests/unit/test_semantic_control_plane.py` | TEST_KEEP | semantic confirmation/control regression | yes | yes | yes | yes |
| `tests/unit/test_semantic_state_projection.py` | TEST_KEEP | Objective state projection regression | yes | yes | yes | yes |
| `tests/unit/test_time_parser.py` | TEST_KEEP | temporal parsing regression | yes | yes | yes | yes |
| `zhiguang-fe/src/components/agent/AgentPanel.tsx` | PRODUCTION_KEEP | frontend send race guard and stale-generation UI | yes | yes | yes | yes |

## Untracked top-level entries

| Path | Category | Why changed / current caller | Needed after reboot | Needed for regression | Enter Git | Remain after cleanup |
|---|---|---|---|---|---|---|
| `apps/backend/scripts/run_overnight_multi_objective_matrix.py` | EVALUATION_KEEP | repeatable MO evaluation harness | yes | yes | yes | yes |
| `checkpoints/` | CHECKPOINT_ARCHIVE | historical recovery patches and snapshots | no | useful for rollback | no | archive/review |
| `docs/evaluation/agent_performance_baseline_v2_results.json` | EVALUATION_KEEP | baseline source referenced by reports | yes | yes | yes | yes |
| `docs/evaluation/greenbook_final_acceptance_precommit.json` | DOC_KEEP | machine-readable final acceptance handoff | yes | yes | yes | yes |
| `docs/reports/AGENT_PERFORMANCE_BASELINE_V2.md` | DOC_KEEP | baseline interpretation and limitations | yes | yes | yes | yes |
| `docs/reports/GREENBOOK_FINAL_ACCEPTANCE_PRECOMMIT.md` | DOC_KEEP | final acceptance report | yes | yes | yes | yes |
| `docs/worklogs/` | CHECKPOINT_ARCHIVE | overnight evidence, state, patches, and recovery logs | no | useful for audit/rollback | no | archive/review |
| `scripts/dev/inspect_execution_safe.py` | UNKNOWN_REVIEW | one-time safe execution inspection; no production caller found | no | only historical diagnosis | no | review before removal |
| `scripts/dev/inspect_live_browser_safe.py` | UNKNOWN_REVIEW | one-time safe browser inspection; no production caller found | no | only historical diagnosis | no | review before removal |
| `scripts/dev/inspect_live_runs_safe.py` | UNKNOWN_REVIEW | one-time safe run inspection; no production caller found | no | only historical diagnosis | no | review before removal |
| `scripts/dev/inspect_tasks_safe.py` | UNKNOWN_REVIEW | one-time safe task inspection; no production caller found | no | only historical diagnosis | no | review before removal |
| `scripts/dev/run_existing_browser_turn.py` | DEV_SCRIPT_KEEP | repeatable focused browser turn helper; manual evaluation caller | useful | yes for focused rerun | yes | yes |
| `tests/e2e/browser_ux_final_smoke.py` | TEST_KEEP | final real-browser rapid/stale/progress UX regression | yes | yes | yes | yes |
| `tests/unit/test_action_loop_parallel_objectives.py` | TEST_KEEP | bounded parallel/failure-isolation regression | yes | yes | yes | yes |

## Staging decision

The first stable commit stages all `PRODUCTION_KEEP`, `TEST_KEEP`, `EVALUATION_KEEP`, `DOC_KEEP`, and `OBSERVABILITY_KEEP` items above, plus the repeatable `DEV_SCRIPT_KEEP` helpers. It does not stage `checkpoints/`, `docs/worklogs/`, or `UNKNOWN_REVIEW` inspection scripts. This is selective staging; `git add .` is prohibited.

## Deletion/move decision

No deletion or directory move is authorized by this audit alone. Historical checkpoints and worklogs remain recoverable until the stable commit is verified. One-time inspection scripts remain `UNKNOWN_REVIEW` rather than being guessed dead. Structure cleanup starts only after recording the successful pre-cleanup commit/tag.
