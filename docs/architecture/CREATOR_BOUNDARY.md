# Creator Boundary Audit

本报告记录 GreenBook Agent Runtime 中 Creator、内容生成和发布相关实现的边界。它是架构审计文档，不改变运行时行为，也不代表任何目录已经可以删除。

## 1. Creator 实现清单

| 路径 | 职责 | 入口 | 调用方 | 是否生产使用 |
| --- | --- | --- | --- | --- |
| `packages/creator_client/` | Creator HTTP 客户端；提交 Creator task、轮询结果、读取 artifact，并处理 publication handoff | `CreatorClient.create_task()` 及相关客户端方法 | Assistant API、GreenBook MCP workflows、集成测试 | 是，当前 Assistant 到 Creator 的正式客户端边界 |
| `services/greenbook_mcp/greenbook_mcp_server/` | 将 Assistant capability 映射为 MCP 工具；封装草稿创建、修改和发布工具 | `GreenBookMCPServer`、`workflows/create_draft.py`、`workflows/revise_draft.py` | Assistant API 的 ToolRuntime / CapabilityExecutor | 是，当前工具适配边界 |
| `packages/assistant_core/greenbook_assistant_core/capability/registry.py` | 定义 `GENERATE_CONTENT`、`IMPROVE_CONTENT`、`VALIDATE_QUALITY`、`SCHEDULE_PUBLISH`、`PUBLISH_NOW` 等语义能力、输入和风险属性 | `CapabilityRegistry` | Planner、CapabilityExecutor | 是，属于 Runtime 的能力目录 |
| `packages/assistant_core/greenbook_assistant_core/execution/capability_executor.py` | 将已规划的 capability 解析为工具调用并交给执行运行时 | capability execution entrypoint | Worker / Execution Runtime | 是，但不实现 Creator 业务逻辑 |
| `services/creator_agent/` | workspace 中注册的 Creator Agent 服务包 | 其服务入口和 HTTP API | 部署系统或外部调用方，当前源码直接 import 较少 | 部署归属需要进一步确认 |
| `creator-agent/` | 独立 Creator Agent 工程，包含自身 graph、worker、memory、retrieval、evaluation 和 persistence | 其独立服务 API | `CreatorClient` 通过 HTTP 间接调用 | 作为候选部署实现保留，不能仅依据源码引用判定为非生产 |
| `community-assistant-agent/app/tools.py` | 历史社区 Assistant 的内容和发布工具适配 | 旧 turn pipeline / tool handler | `community-assistant-agent` 内部流程 | 历史或兼容路径，非 GreenBook Runtime 的 Creator 主入口 |
| `community-assistant-agent/app/turn_pipeline.py`、`worker.py` 等 | 历史 Assistant 的任务编排和执行实现，可能包含 `creator`、`content_creation`、`publish` 相关逻辑 | 旧 turn pipeline | 历史 Agent API | 非当前 Creator 主路径 |
| `creator-agent/app/` 内的 graph、worker、memory、retrieval 等模块 | Creator 内部内容研究、生成、审校和 artifact 流程 | Creator Agent 自身 API | Creator 服务内部 | 是否由当前部署使用需由部署配置确认 |

审计关键词还覆盖了 `mindbridge`、`social_media`、`generate_content` 和 `publish`。其中 `publish` 不应直接归 Creator 所有：发布和定时发布由 capability/MCP/社区业务工具完成，Creator 负责内容创作及其 artifact 产出。

## 2. ACTIVE Creator

当前 GreenBook Runtime 的正式 Creator 边界为：

```text
IntentSpec
  -> Planner
  -> TaskPlan
  -> Capability
  -> CapabilityExecutor
  -> ToolRuntime / MCP
  -> CreatorClient
  -> Creator Agent HTTP service
```

具体职责如下：

- `GENERATE_CONTENT` 通过 `content.create_draft` 创建新草稿。
- `IMPROVE_CONTENT` 通过 `content.revise_draft` 修改已有草稿。
- `VALIDATE_QUALITY` 是内容质量检查能力，可以是 Runtime 内的 LLM step，不等同于 Creator HTTP 调用。
- `SCHEDULE_PUBLISH` 和 `PUBLISH_NOW` 是发布能力。它们经过 MCP/社区业务服务，不应把发布状态或 Java 中的内容事实源复制到 Creator 内部。
- `CreatorClient` 是跨服务协议边界。Assistant Core 不应直接 import Creator Agent 的 graph、worker、memory 或数据库模块。

因此，当前 GreenBook Runtime 使用的是“能力目录 + MCP 工具 + HTTP Creator 服务”的组合，而不是把 Creator Agent 内部实现嵌入 Planner 或 Worker。

## 3. COMPATIBILITY

### 历史内容创建实现

`community-assistant-agent` 中的 `creator`、`content_creation`、`generate_content`、`publish` 相关模块应视为兼容边界。它们保留的原因是：

1. 仍可能被旧 API、旧 turn pipeline 或旧集成测试使用。
2. 其中包含既有协议、幂等、审批和发布交接行为，直接删除会扩大回归范围。
3. 当前 GreenBook Runtime 已有替代路径，但尚未完成所有调用方和部署入口的迁移证明。

迁移目标是：旧入口最终只通过稳定的 capability/MCP contract 访问 `CreatorClient` 或社区业务工具，不再新增旧 Creator 实现能力。

### 双 Creator 服务目录

`creator-agent/` 与 `services/creator_agent/` 的职责和部署 owner 需要对照 workspace、Docker、CI、健康检查和实际环境变量确认。在确认前，两者都属于 COMPATIBILITY/DEPLOYMENT REVIEW，不应移动或删除。

## 4. LEGACY / ARCHIVE

以下项目是归档候选，而不是本次可直接删除项：

| 候选 | 当前判断 | 删除或归档条件 |
| --- | --- | --- |
| `community-assistant-agent` 中只服务旧 turn pipeline 的 Creator/tool 实现 | LEGACY 候选 | 无生产入口、无测试引用，且所有调用方已迁移到 MCP contract |
| 未被部署配置引用的重复 Creator Agent 服务目录 | ARCHIVE 候选 | 明确唯一 deployment owner，并完成 API、Docker、CI、health check 和数据迁移对照 |
| 旧 `mindbridge` / `social_media` / `generate_content` 适配 | LEGACY 候选 | 证明没有 active capability、旧 API 或回归测试依赖 |
| 直接在旧 Agent 中操作发布或内容生成的实现 | ARCHIVE 候选 | 发布与生成均已由 capability/MCP contract 覆盖，并通过 E2E 和 contract 测试 |

本次审计没有满足删除条件的对象。特别是不能因为 `rg` 看不到某个模块的直接 Python import，就认定它没有部署、脚本或 HTTP 依赖。

## 5. Runtime 集成方式

```text
用户请求
  -> IntentSpec
  -> Planner
  -> TaskPlan
  -> PlanExecution / Worker
  -> Capability
  -> CapabilityExecutor
  -> ToolRuntime / GreenBook MCP
  -> CreatorClient 或社区业务工具
  -> Creator Agent / Community Service
```

边界规则：

- IntentSpec 只表达用户意图，不包含 Creator 内部 graph、prompt 或 HTTP 细节。
- Planner 只把内容创作需求映射为 capability 和计划，不直接调用 Creator。
- Worker 只负责执行 TaskPlan，并由 Execution Runtime 管理状态、重试、审批和事件。
- ToolRuntime/MCP 负责工具注册、参数契约、风险和外部调用隔离。
- Creator 负责内容研究、生成、修订和 artifact 产出；社区后端负责草稿事实、发布和定时发布等业务事实。
- Creator Agent 的内部实现必须通过 HTTP/API contract 使用，不能成为 Assistant Core 的隐式 import 依赖。

## 审计结论

当前应保留的 ACTIVE 边界是 `capability registry`、`capability executor`、`greenbook_mcp` 和 `creator_client`。`creator-agent` 与 `services/creator_agent` 的部署关系尚需确认；`community-assistant-agent` 中的 Creator 逻辑属于兼容/Legacy 范围。后续清理应先完成调用方、部署入口和 contract 测试审计，再执行移动或删除。

