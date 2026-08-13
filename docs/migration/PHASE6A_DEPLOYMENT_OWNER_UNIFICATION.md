> **Historical migration document.** Retained for traceability; it does not define current architecture or active topology. See [current architecture](../architecture/CURRENT_ARCHITECTURE.md).

# Phase 6A Deployment Owner Unification

## 1. Problem

Phase 6A 处理的是部署 owner 分裂，不是功能扩展。审计前存在三类 split-brain：

| 领域 | 旧状态 | 风险 |
| --- | --- | --- |
| Java | `start-greenbook.ps1` 启动 `apps/backend`，Docker schema 和 CI 指向 `zhiguang-be` | 代码、schema、测试不是同一份 |
| Creator | 根 `creator-agent/` 被启动脚本使用，`apps/creator-agent/` 仍是重复实现，`services/creator_agent/` 是空骨架 | 两个可部署 Creator owner |
| Assistant | 新 Runtime 在 `apps/assistant_api`、`apps/assistant_worker`、`packages/assistant_core`，旧 `community-assistant-agent/` 仍被验证和运维入口触达 | 8094 与运行语义冲突 |

本阶段基于真实启动脚本、CI、Docker、workspace manifest、import/caller 扫描完成切换；没有把 Moderation 代码迁入新的 Community Backend，也没有开始 Phase6B。

## 2. Java Owner Before

选择前的事实：

- `apps/backend`：256 个非生成文件，已由 `scripts/start-be.ps1` 启动，包含 Agent Facade、Agent 幂等/发布能力和对应测试、迁移。
- `zhiguang-be`：235 个非生成文件，被 CI、Docker/schema 引用，包含完整 `com.tongji.moderation` Java 模块及其测试。
- 两份代码有 17 个共同路径但内容不同；差异集中在认证/安全、社区映射、配置和 Agent/Moderation 边界，不能按目录相似度直接互换。

## 3. Java Diff

| 分类 | 结果 | 处理 |
| --- | --- | --- |
| COMMON | 社区基础模型、认证和基础设施代码 | 以 `apps/backend` 为 owner，运行 Maven 测试 |
| AGENT_ONLY | `com.tongji.agentfacade/**`、Agent 相关 DTO/mapper/service、幂等和 scheduled publication、Agent migration/test/resource | 保留在 `apps/backend` |
| USEFUL_UNIQUE | `zhiguang-be` 的 Dockerfile、`.dockerignore` 和部署说明 | 复制到 `apps/backend` 后改写 owner 引用 |
| MODERATION_ONLY | `com.tongji.moderation/**`、Moderation 测试及其业务配置 | 不迁移，留给 Phase6B 产品级删除/数据处理 |
| STALE / DUPLICATE | `zhiguang-be` 的第二份 Java source tree | caller/schema 切换后删除 |

## 4. Final Java Owner

唯一 Java source tree 是：

```text
apps/backend/
```

统一结果：

- `scripts/start-be.ps1` → `apps/backend`。
- `.github/workflows/verify.yml` 的 Java job → `apps/backend`。
- root `docker-compose.yml` 的 MySQL schema/migration mounts → `apps/backend/db/`。
- `apps/backend/Dockerfile` 和 `.dockerignore` 已补齐部署所需文件。
- `contracts/java-openapi.yaml`、Java client、MCP 相关调用以 `/api/v1/agent` 为 canonical Agent Tool API。

`/api/v1/assistant-tools` 目前仍是 canonical backend 内的兼容 endpoint。仓库内没有发现外部 caller；由于外部客户端和历史 capability token 数据无法由源码证明已经迁空，本阶段没有贸然删除，列入 Phase6B/后续 API retirement。

## 5. Creator Owner Before

对比结果：

- 根 `creator-agent/`：179 个文件，实际启动脚本和本地部署 owner。
- `apps/creator-agent/`：182 个文件，与根实现高度重复；额外内容主要是 4 个 graph diagnostic probe tests。
- 两份实现的生产源码差异集中在 `app/creator/api/routes.py` 的请求校验日志；该日志已经迁入根 Creator。
- `services/creator_agent/` 只有 workspace skeleton 和空 package init，无 runtime import、startup、CI caller。

## 6. Final Creator Owner

唯一 Creator Service 是：

```text
creator-agent/
```

`scripts/start-creator.ps1`、root Creator 的 `.env`、migration、测试和 API 均继续使用该路径。`apps/creator-agent/` 已删除；4 个仅用于诊断图探针的测试没有迁入生产测试集，因为根 Creator 已有对应 runtime composition/config/harness 覆盖。`services/creator_agent/` skeleton 已删除。

Creator 的边界保持不变：它可以拥有自己的创作 workflow、specialist、checkpoint、HITL 和 evaluation，但不拥有 GreenBook 的 Command、TaskManager、跨域 Memory 或全局 Tool routing。

## 7. Agent Runtime Owner

唯一 GreenBook Agent Runtime 是：

```text
apps/assistant_api/
apps/assistant_worker/
packages/assistant_core/
services/greenbook_mcp/       # in-process runtime package
```

默认运行链路为：

```text
User/API
  -> apps/assistant_api
  -> packages/assistant_core Command/Context/Goal/Task/AgentLoop
  -> ToolPolicy / GoalCompiler / ExecutionInput
  -> assistant_worker (queue mode)
  -> ToolRuntime / in-process MCP
  -> apps/backend or creator-agent
```

`community-assistant-agent/` 已从默认 CI、启动、smoke、P0 E2E、runtime report、setup-dev 和默认验证路径移除，但本阶段没有删除其目录。它被标记为 **RETIREMENT READY**，原因是其 `assistant_runs` 等历史数据库/API 边界尚未完成数据迁移证明。

## 8. Community Assistant Retirement

已完成：

- CI 不再以 `community-assistant-agent` 为 working directory 或测试目标。
- `scripts/setup-dev.ps1` 默认不再安装/启动旧 Assistant。
- `scripts/smoke-test.ps1`、`scripts/runtime-report.ps1`、`scripts/run_p0_e2e.py` 改用 canonical Assistant API/评测入口。
- `scripts/start-greenbook.ps1` 不启动旧服务，8094 只由 `apps/assistant_api` 使用。
- 旧服务仍保留在仓库，供 Phase6B 做运行数据、旧 API 和 migration 盘点。

## 9. Startup Topology

默认 `scripts/start-greenbook.ps1` 只启动：

```text
Java Backend      apps/backend             :8080
Creator Service   creator-agent            :8092
Agent API         apps/assistant_api       :8094
Agent Worker      apps/assistant_worker    queue mode only
Frontend          zhiguang-fe              :5173 (dev default)
```

生产语义是 API 与 durable Queue Worker 分进程。`ASSISTANT_EXECUTION_DISPATCH=direct` 可用于本地开发，此时不启动 Worker。独立 `scripts/start-assistant.ps1` 仍支持显式的 API 内嵌 Worker 开发模式，但不再是默认总启动拓扑。

MCP 在本阶段冻结为 `services/greenbook_mcp` 的 in-process package；没有增加 standalone MCP server。

## 10. CI Changes

`.github/workflows/verify.yml` 现在验证实际 owner：

- Java：`apps/backend`，生成的 JWT 测试 key 也写入该目录。
- Frontend：`zhiguang-fe` 的 lint/build。
- Agent Runtime：root workspace 的 `uv sync --frozen`、pytest 和本阶段 touched-module Ruff 检查。
- Creator：根 `creator-agent` 的 frozen sync、pytest 和合并后的 route Ruff 检查。

旧 `zhiguang-be`、旧 Assistant 和默认 Moderation job 已移除。全量历史 Ruff 基线并未在本阶段强行重写；CI 的 Ruff 范围遵循“本阶段触及模块必须通过”，完整测试仍覆盖 canonical runtime。

## 11. Docker Changes

- root `docker-compose.yml` 的 Java schema/migration source 统一为 `apps/backend/db/`。
- `apps/backend/Dockerfile`、`.dockerignore` 已从旧 Java owner 的有效部署内容迁入。
- `infra/docker-compose.dev.yml` 的说明和 owner 指向 canonical Java/Creator/Agent。
- Moderation database/profile 暂时保留，未新增任何新依赖；其物理清理属于 Phase6B。

## 12. Workspace Changes

root `pyproject.toml` 的 uv workspace 现在只包含存在且参与 canonical Python runtime 的成员：

```text
packages/assistant_core
packages/contracts
packages/java_client
packages/creator_client
packages/security
packages/observability
packages/evaluation
services/greenbook_mcp
apps/assistant_api
apps/assistant_worker
```

已移除不存在的 `packages/persistence` 和空的 `services/creator_agent` workspace member；`tool.uv.dev-dependencies` 已迁移到 dependency group；`uv.lock` 重新生成并解析 82 个 package。

## 13. Script Changes

已收敛：

- `start-greenbook.ps1`：唯一 Java/Creator/Assistant/Worker/Frontend 拓扑，queue mode 等待 Worker health file。
- `setup-dev.ps1`：默认只准备 canonical Python owner；Moderation 改为显式 `-IncludeModeration`。
- `verify-all.ps1`：默认验证 canonical Java、Creator、Agent、Frontend，不再运行旧 Assistant/Moderation 检查。
- `smoke-test.ps1`：检查 canonical Java、Creator、Frontend 及 Agent API/Worker import。
- `runtime-report.ps1`：改用 root evaluation runner。
- `run_p0_e2e.py`：Assistant harness 使用 `apps/assistant_api`，Creator 使用根 `creator-agent`。
- `e2e-test.ps1`：移除默认 Moderation health/API 路径，Assistant health 指向 canonical API。

## 14. Deleted Files / Directories

本阶段删除的 owner/垃圾目标：

| Path | 删除原因 |
| --- | --- |
| `zhiguang-be/` | 第二份 Java source tree；caller、CI、Docker schema 已切换到 `apps/backend`，Moderation source 不属于 canonical backend |
| `apps/creator-agent/` | 与根 Creator 重复；生产差异已迁移，额外内容只是诊断 probe |
| `services/creator_agent/` | 无 runtime caller 的 workspace skeleton |
| `packages/assistant_core/greenbook_assistant_core/agent_memory/` | 空/无调用的历史 compatibility 目录 |
| `packages/assistant_core/greenbook_assistant_core/resource/` | 空/无调用的历史 compatibility 目录 |
| `services/greenbook_mcp/greenbook_mcp_server/workflows/` | 动态 import/caller 扫描为空，能力已由 canonical ToolRuntime/GoalCompiler 覆盖 |
| 根目录两个 8192-byte stray 文件 | 无 import、startup 或测试引用的明显垃圾文件 |
| `MOVE_PLAN.md` | 已失效的历史移动计划，当前 owner 决策已落地 |

删除前均完成 exact path、caller/diff 或 workspace 引用确认；没有建立 backup/legacy2 目录。

## 15. Remaining Moderation Dependencies

Moderation 没有被本阶段伪装成已删除，仍有以下明确边界：

- `moderation-agent/` 独立服务代码、配置和测试仍存在。
- 前端 Admin Moderation/review 页面仍存在。
- root compose 的 Moderation DB/schema/profile 仍存在。
- `.env` 中的 Moderation URL/secret 仍被部分 Java 启动环境读取。
- `scripts/setup-dev.ps1` 保留显式 `-IncludeModeration`，但默认 GreenBook startup/verify/smoke/CI 不再启动或验证它。

Phase6B 必须先处理数据保留、API callback、表和产品开关，再删除这些内容。Execution security 的 permission、approval、ToolPolicy 不属于 Moderation 删除范围。

## 16. Test Results

本阶段及 owner 切换后的本地验证：

| Check | Result |
| --- | --- |
| root `uv run pytest -q` | **663 passed, 2 skipped, 2 warnings** |
| launcher/worker contract tests | **8 passed** |
| `apps/backend` `mvn -q test` | **passed** |
| root Creator `uv run --frozen python -m pytest -q` | **64 passed** |
| `zhiguang-fe` `npm run lint` | **passed** |
| `zhiguang-fe` `npm run build` | **passed** |
| `uv lock --check` | **passed** |
| `uv sync --frozen` | **passed** |
| `compileall packages apps services tests scripts/run_p0_e2e.py` | **passed** |
| PowerShell parser for touched launch/verify scripts | **passed** |
| workflow YAML parse | **passed** |
| `docker compose config --quiet` | **passed** |
| touched-module Ruff | **passed** |

两个 pytest warning 是已有的 unknown `integration` marker 和 Windows pytest cache permission warning。完整 Creator Ruff 基线仍有约 240 个历史错误，Agent/MCP 全量 Ruff 仍有约 85 个历史错误；本阶段没有把无关 lint 基线混入 owner cutover，touched-module checks 已通过。

## 17. Final Deployment Matrix

| Owner | Canonical path | Entry point | Default status |
| --- | --- | --- | --- |
| Java Community Backend | `apps/backend` | `scripts/start-be.ps1` / Maven | ACTIVE, 唯一 Java owner |
| GreenBook Creator Service | `creator-agent` | `scripts/start-creator.ps1` / `run_service.py` | ACTIVE, 唯一 Creator owner |
| Agent API | `apps/assistant_api` + `packages/assistant_core` | `scripts/start-assistant.ps1 -ApiOnly` | ACTIVE |
| Agent Worker | `apps/assistant_worker` | `scripts/start-assistant-worker.ps1` | ACTIVE in queue mode |
| MCP runtime | `services/greenbook_mcp` | imported by API/Worker | ACTIVE, in-process |
| Frontend | `zhiguang-fe` | `scripts/start-fe.ps1` | ACTIVE |
| Moderation | `moderation-agent` and related DB/UI | explicit/optional only | OUT OF DEFAULT TOPOLOGY |
| Old Community Assistant | `community-assistant-agent` | no default entry | RETIREMENT READY |

## 18. Remaining Risk

1. `community-assistant-agent` 的历史 `assistant_runs`/migration 数据还没有完成删除或迁移证明。
2. `/api/v1/assistant-tools` 仍作为 canonical Java 内的兼容 endpoint 存在；仓库内无 caller，但外部 client 未被证明为空。
3. Moderation DB、前端和独立服务仍需产品确认后处理。
4. root Creator 和 Assistant 的历史包名仍保留，Phase6A 没有做 package-level breaking rename。
5. 全量 Ruff baseline 仍不干净；本阶段只保证 touched modules 和真实测试链路。
6. 旧文档/历史报告仍会提到已删除路径；它们是历史记录，不参与生产 imports，但应在后续文档清理中标注或归档。

## 19. Phase 6B Input

Phase6B 可以基于以下已收敛的前提开始，而不再处理 owner 分裂：

1. 对 `community-assistant-agent` 做数据库/API/data retention inventory，完成后删除其 workspace 和历史运行入口。
2. 删除或正式迁移 `/api/v1/assistant-tools` 及其 capability token 表/测试。
3. 处理 `moderation-agent`、Moderation Java schema/config、前端 Admin 页面和相关 DB 表。
4. 清理指向已删除 owner 的历史架构文档；保留迁移报告作为历史证据。
5. 单独评估全量 Ruff baseline 和 package naming，不与部署 owner 决策混做。

Phase6B 未在本阶段自动启动。
