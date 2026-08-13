> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 8.1-B Archive Report

本报告记录 Phase 8.1-B 的历史目录归档结果。此次操作只处理已完成审计、且不属于 ACTIVE Runtime 的目录；未修改 ACTIVE Runtime、Planner、Worker、Execution Runtime、ToolRuntime、pyproject、uv、Docker 或 CI 配置。

## Moved Files

| 原路径 | 新路径 | 结果 | 说明 |
| --- | --- | --- | --- |
| `zhiguang-be/` | `archive/legacy/zhiguang-be/` | 已移动 | 旧 Java backend；审计确认没有 Python import、脚本、Docker 或 CI 引用。目录中的数据库 schema 和迁移文件随目录保留。 |
| `design-system/` | `docs/design-system/` | 已移动 | 设计资料和预览，不属于 Python workspace；仅更新了 README、设计主文档和架构文档中的路径说明。 |
| `archive/creator_agent/` | `archive/creator/creator_agent/` | 已整理 | 既有 Creator 历史归档，保持文件内容不变。 |
| `archive/greenbook_mcp/workflows/` | `archive/workflows/` | 已整理 | 已归档的旧 workflow，保持文件内容不变。 |

### 未移动

`community-assistant-agent/` 保持原路径。它仍被以下运行或验证入口使用：

- `.github/workflows/verify.yml`
- `scripts/verify-all.ps1`
- `scripts/smoke-test.ps1`
- `scripts/setup-dev.ps1`
- `scripts/runtime-report.ps1`
- `scripts/run_p0_e2e.py`
- `apps/assistant_api/greenbook_assistant_api/main.py` 的旧服务身份配置

此外，旧 API、`assistant_runs`、`run_id`、审批和数据迁移文档仍以它为兼容边界。移动它需要同步修改 CI、脚本、部署和历史数据迁移，超出本阶段范围。

## Reference Scan

前置扫描通过 `rg` 检查了脚本、Docker、GitHub Actions、Python import 和文档引用：

- `zhiguang-be/`：未发现脚本、Docker、CI 或 Python import 依赖；现存引用属于历史文档描述。
- `design-system/`：未发现运行时依赖；移动后 README 和设计资料中的旧路径已更新。
- `community-assistant-agent/`：发现 CI、开发脚本、P0 E2E 和身份/API 兼容引用，因此保留。
- 归档旧路径 `archive/creator_agent/`、`archive/greenbook_mcp/workflows/`：未发现 ACTIVE Python import；历史报告中的旧路径属于已完成阶段记录，未改写。

移动后，ACTIVE 代码路径仍未引用归档目录；旧路径残留仅限于历史报告、兼容文档或明确仍在使用的 `community-assistant-agent` 边界。

## Rollback Path

所有移动均可通过反向目录移动恢复：

1. 将 `archive/legacy/zhiguang-be/` 移回 `zhiguang-be/`。
2. 将 `docs/design-system/` 移回 `design-system/`，并恢复相应文档中的路径文本。
3. 将 `archive/creator/creator_agent/` 移回 `archive/creator_agent/`。
4. 将 `archive/workflows/` 移回 `archive/greenbook_mcp/workflows/`。

`community-assistant-agent/` 未移动，无需回滚。

## Test Result

### `pytest tests/unit`

未完成收集：当前解释器缺少 `fastapi`，在导入 `apps/assistant_api/greenbook_assistant_api/api/routes.py` 时发生 2 个 collection errors。错误与本次目录归档无关。

### `pytest tests/evaluation`

44 个测试通过，1 个测试失败：`tests/evaluation/test_intent_v2_llm_eval.py::test_llm_intent_evaluation`。失败原因是当前环境缺少 `openai` 模块；未涉及移动路径。

## Scope Confirmation

- ACTIVE Runtime：未修改
- Planner：未修改
- Worker：未修改
- Execution Runtime：未修改
- ToolRuntime：未修改
- `pyproject.toml`、`uv.lock`、Docker 和 CI：未修改
- `community-assistant-agent`：保留在原路径

