# GreenBook Agent Runtime — 迁移路线图

> 日期: 2026-08-07
> 状态: 分析阶段 — 不修改代码
> 前置:
>   - `docs/reports/greenbook-agent-runtime-v1-implementation-plan.md` (v1 落地方案)
>   - `docs/reports/greenbook-agent-runtime-gap-analysis.md` (差距分析)
>
> 本文档是文件级、函数级的精确迁移路线。

---

# 1. 当前代码文件清单与命运

## 1.1 符号说明

| 标记 | 含义 |
|------|------|
| ✅ KEEP | 保留，零改动 |
| ✂️ REFACTOR | 保留文件但内部重构 |
| 🗑️ DELETE | Phase 5 删除（Phase 1-4 保留为 fallback） |
| 🆕 NEW | 新增文件 |

## 1.2 apps/assistant_api/ — HTTP 层

| 文件 | 行数 | 命运 | 说明 |
|------|------|------|------|
| `__init__.py` | 1 | ✅ KEEP | — |
| `main.py` | 231 | ✂️ REFACTOR | 新增 DB 连接池初始化、TaskRegistry/ExecutionEngine 注入 |
| `api/__init__.py` | 0 | ✅ KEEP | — |
| `api/routes.py` | 1238 | ✂️ REFACTOR | 核心重构对象：逐步将职责迁移到新模块 |
| `dependencies/__init__.py` | 2 | ✅ KEEP | — |
| `dependencies/assistant.py` | 4 | ✅ KEEP | — |
| `streaming/__init__.py` | 0 | ✅ KEEP | — |

**routes.py 内部函数命运（精确映射）：**

| 函数/类 | 行号 | 命运 | 迁移到 |
|---------|------|------|--------|
| `ConversationCreateRequest` | 56 | ✅ KEEP | 保留在 routes.py |
| `ConversationSummary` | 60 | ✅ KEEP | 保留在 routes.py |
| `ConversationListResponse` | 70 | ✅ KEEP | 保留在 routes.py |
| `MessageView` | 77 | ✅ KEEP | 保留在 routes.py |
| `MemorySettings` | 84 | ✅ KEEP | 保留在 routes.py |
| `MessageCreateRequest` | 89 | ✅ KEEP | 保留在 routes.py |
| `RunResponse` | 94 | ✅ KEEP | 保留在 routes.py |
| `ApprovalDecisionRequest` | 113 | ✅ KEEP | 保留在 routes.py |
| `_http_status_for_tool_error()` | 117 | ✅ KEEP | 保留在 routes.py |
| `_normalize_schedule_tool_args()` | 147 | ✂️ REFACTOR | → `task/understanding.py` (时间需求提取) + `execution/mapper.py` (参数构建) |
| `_normalize_update_schedule_tool_args()` | 168 | ✂️ REFACTOR | → `execution/mapper.py` |
| `_community_reference_items()` | 193 | ✂️ REFACTOR | → `execution/mapper.py` (注入参考到 create_draft 参数) |
| `_bind_target_tool_args()` | 220 | ✂️ REFACTOR | → `execution/mapper.py` (从 Task artifacts 绑定目标) |
| `_append_schedule_confirmation()` | 242 | ✅ KEEP | 保留在 routes.py (前端展示逻辑) |
| `_get_auth()` | 276 | ✅ KEEP | 保留在 routes.py |
| `_now_iso()` | 283 | ✅ KEEP | 保留在 routes.py |
| `_conversation_belongs_to()` | 287 | ✅ KEEP | 保留在 routes.py |
| `_get_session()` | 294 | ✂️ REFACTOR | SessionContext 从 DB 加载 |
| `_save_session()` | 313 | ✂️ REFACTOR | SessionContext 持久化到 DB |
| `_conversation_summary()` | 324 | ✅ KEEP | 保留在 routes.py |
| `_auth_store_put()` | 335 | ✂️ REFACTOR | → `db/repositories.py` |
| `_auth_store_get()` | 346 | ✂️ REFACTOR | → `db/repositories.py` |
| `_build_tool_schemas()` | 360 | ✂️ REFACTOR | → 动态从 `tool_registry.get_tool_definitions()` 生成 |
| `list_conversations()` | 561 | ✂️ REFACTOR | 查询逻辑 → `db/repositories.py`；路由保留 |
| `create_conversation()` | 593 | ✂️ REFACTOR | 创建逻辑 → `db/repositories.py`；路由保留 |
| `send_message()` | 629 | ✂️ REFACTOR | **核心重构** — 注入 TaskUnderstanding + TaskRegistry |
| `get_messages()` | 1023 | ✂️ REFACTOR | 查询 → `db/repositories.py` |
| `get_memories()` | 1040 | ✅ KEEP | 保持空壳 (Phase 6 实现) |
| `get_episodes()` | 1046 | ✅ KEEP | 保持空壳 |
| `get_memory_settings()` | 1052 | ✅ KEEP | 保持空壳 |
| `get_run()` | 1058 | ✂️ REFACTOR | 查询 → `db/repositories.py` |
| `get_run_events()` | 1115 | ✅ KEEP | SSE 逻辑保留 |
| `list_runs()` | 1126 | ✂️ REFACTOR | 查询 → `db/repositories.py` |
| `stream_run_events()` | 1152 | ✅ KEEP | SSE 逻辑保留 |
| `cancel_run()` | 1163 | ✅ KEEP | 保留在 routes.py |
| `interrupt_run()` | 1174 | ✅ KEEP | 保留在 routes.py |
| `approve_operation()` | 1185 | ✂️ REFACTOR | → `db/repositories.py` |
| `reject_operation()` | 1213 | ✂️ REFACTOR | → `db/repositories.py` |
| `_sse_stream()` | 1224 | ✅ KEEP | 保留在 routes.py |
| `_SYSTEM_PROMPT` | 1232 | ✂️ REFACTOR | → `prompts/system.py` |
| `tool_handler` 回调 (send_message 内部) | 671-737 | ✂️ REFACTOR | → `execution/engine.py` (通过 ExecutionEngine 间接调用 MCP) |

## 1.3 packages/assistant_core/ — Agent 核心

| 文件 | 行数 | 命运 | 说明 |
|------|------|------|------|
| `__init__.py` | 1 | ✂️ REFACTOR | 导出新模块 |
| `agent.py` | 543 | ✂️ REFACTOR | **核心重构** — 精简到 ~200 行 |
| `context.py` | 125 | ✂️ REFACTOR | 精简 `active_*_id` → `active_task_id` |
| `memory.py` | 32 | ✂️ REFACTOR | 从空壳 → DB-backed 实现 |
| `middleware.py` | 33 | ✅ KEEP | 保留（未来可启用） |
| `time_parser.py` | 203 | ✅ KEEP | 零改动 |
| `prompts/__init__.py` | 0 | ✅ KEEP | — |
| `prompts/system.py` | — | ✂️ REFACTOR | 增加 Task 上下文注入 |
| `skills/__init__.py` | 0 | 🗑️ DELETE | Phase 5 删除（从未实现） |

**agent.py 内部函数命运（精确映射）：**

| 函数/类 | 行号 | 命运 | 迁移到 |
|---------|------|------|--------|
| `_has_future_time_expression()` | 33 | ✂️ REFACTOR | → `task/understanding.py` (作为 L1 快速路径的一部分) |
| `_is_schedule_only_retry()` | 51 | ✂️ REFACTOR | → `task/understanding.py` (L1) |
| `_asks_for_community_references()` | 77 | ✂️ REFACTOR | → `task/understanding.py` (L1) |
| `_schedule_tool_for_session()` | 91 | ✂️ REFACTOR | → `execution/mapper.py` (动态工具选择) |
| `_turn_intents()` | 99 | 🗑️ DELETE | → `task/understanding.py:TaskUnderstanding.understand()` (L1+L2) |
| `_turn_routing_hint()` | 132 | 🗑️ DELETE | → `task/understanding.py` 的输出 (TaskIntent.requirements → Planner) |
| `_turn_tool_filter()` | 193 | 🗑️ DELETE | → `planning/planner.py` + `execution/engine.py` |
| `PRODUCT_DEFAULTS` | 117 | ✂️ REFACTOR | → `prompts/system.py` |
| `CommunityOperationsAssistant.__init__()` | 238 | ✂️ REFACTOR | 新增参数: planner, execution_engine |
| `CommunityOperationsAssistant._build_system_prompt()` | 253 | ✂️ REFACTOR | 注入 TaskIntent 上下文 |
| `CommunityOperationsAssistant.run()` | 273 | ✂️ REFACTOR | **核心重构** — 分流: Plan → Execution Engine / 无 Plan → 简单循环 |
| 顺序工具门控 if-else | 490-522 | 🗑️ DELETE | → `execution/engine.py:_execute_step()` 中的 DAG 拓扑执行 |

## 1.4 packages/ — 支撑层（全部保留）

| 包 | 文件 | 命运 | 说明 |
|------|------|------|------|
| contracts | `errors.py` | ✅ KEEP | 零改动 |
| contracts | `events.py` | ✅ KEEP | 零改动 |
| contracts | `identity.py` | ✅ KEEP | 零改动 |
| contracts | `tool_result.py` | ✅ KEEP | 零改动 |
| java_client | `client.py` | ✅ KEEP | 零改动 |
| java_client | `models.py` | ✅ KEEP | 零改动 |
| creator_client | `client.py` | ✅ KEEP | 零改动 |
| security | `jwt.py` | ✅ KEEP | 零改动 |
| security | `jwks.py` | ✅ KEEP | 零改动 |
| security | `auth_context.py` | ✅ KEEP | 零改动 |
| security | `approval.py` | ✅ KEEP | 零改动 |
| security | `policy.py` | ✅ KEEP | 零改动 |
| observability | `__init__.py` | ✅ KEEP | 零改动 |

## 1.5 services/ — 服务层（全部保留）

| 服务 | 文件 | 命运 | 说明 |
|------|------|------|------|
| greenbook_mcp | `server.py` | ✅ KEEP | 零改动 |
| greenbook_mcp | `tool_registry.py` | ✅ KEEP | 零改动 |
| greenbook_mcp | `context.py` | ✅ KEEP | 零改动 |
| greenbook_mcp | `tool_schemas.py` | ✅ KEEP | 零改动 |
| greenbook_mcp | `tools/community.py` | ✅ KEEP | 零改动 |
| greenbook_mcp | `tools/content.py` | ✅ KEEP | 零改动 |
| greenbook_mcp | `tools/publication.py` | ✅ KEEP | 零改动 |
| greenbook_mcp | `tools/interaction.py` | ✅ KEEP | 零改动 |
| greenbook_mcp | `tools/analytics.py` | ✅ KEEP | 零改动 |
| greenbook_mcp | `workflows/` | 🗑️ DELETE | Phase 5 删除（已被 tools/ 取代，遗留代码） |
| creator_agent | 全部文件 | ✅ KEEP | 零改动 |

## 1.6 新增文件清单

```
packages/assistant_core/greenbook_assistant_core/
├── task/
│   ├── __init__.py                               # 🆕 ~10 行
│   ├── models.py                                 # 🆕 ~180 行 — Task, TaskIntent, Artifact, ArtifactRef
│   ├── understanding.py                          # 🆕 ~200 行 — TaskUnderstanding (L1+L2)
│   └── registry.py                               # 🆕 ~250 行 — TaskRegistry
├── planning/
│   ├── __init__.py                               # 🆕 ~10 行
│   ├── capability.py                             # 🆕 ~120 行 — Capability + 11 个目录条目
│   └── planner.py                                # 🆕 ~180 行 — Planner
├── execution/
│   ├── __init__.py                               # 🆕 ~10 行
│   ├── models.py                                 # 🆕 ~100 行 — Step, StepStatus
│   ├── engine.py                                 # 🆕 ~300 行 — ExecutionEngine
│   └── mapper.py                                 # 🆕 ~150 行 — CapabilityToolMapper
└── db/
    ├── __init__.py                               # 🆕 ~10 行
    ├── connection.py                             # 🆕 ~50 行 — PostgreSQL 连接池
    └── repositories.py                           # 🆕 ~300 行 — Task/Step/Artifact/Conversation/Run CRUD
```

**总新增代码量估算：~1,870 行**

---

# 2. 新旧架构映射表

## 2.1 完整函数/类迁移映射

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
旧位置                               → 新位置
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【意图理解】
agent.py::_turn_intents()           → task/understanding.py::TaskUnderstanding._quick_intent()
agent.py::_turn_routing_hint()      → task/understanding.py::TaskUnderstanding.understand() (L2)
agent.py::_turn_tool_filter()       → planning/planner.py::Planner.plan() + execution/engine.py
agent.py::_has_future_time_expression() → task/understanding.py::TaskUnderstanding (L1)
agent.py::_is_schedule_only_retry()  → task/understanding.py::TaskUnderstanding (L1)
agent.py::_asks_for_community_refs() → task/understanding.py::TaskUnderstanding (L1)
agent.py::PRODUCT_DEFAULTS          → prompts/system.py

【任务管理】
context.py::SessionContext
  .active_draft_id                   → task/registry.py::TaskRegistry.resolve_task()
  .active_schedule_id                → task/registry.py::TaskRegistry.resolve_task()
  .active_post_id                    → task/registry.py::TaskRegistry.resolve_task()
  .resolve_active_draft_id()         → task/registry.py::TaskRegistry._match_by_entity()
  .resolve_active_schedule_id()      → task/registry.py::TaskRegistry._match_by_entity()
  .record_entity()                   → db/repositories.py::save_artifact()
  .record_tool_call()                → execution/models.py::Step (持久化)

【规划】
agent.py 顺序工具门控 (行 490-522)    → execution/engine.py::ExecutionEngine.execute()
  search→create 分支                  → Planner 输出 DAG: SEARCH_COMMUNITY → GENERATE_CONTENT
  create→schedule 分支                → Planner 输出 DAG: GENERATE_CONTENT → SCHEDULE_PUBLISH
  revise→schedule 分支                → Planner 输出 DAG: IMPROVE_CONTENT → MANAGE_SCHEDULE

【工具调度】
routes.py::tool_handler (行 671-737) → execution/engine.py::_execute_step() + mapper.py
routes.py::_bind_target_tool_args()  → execution/mapper.py::CapabilityToolMapper.build_tool_call()
routes.py::_community_reference_items() → execution/mapper.py (作为上游 Artifact 注入)
routes.py::_normalize_schedule_tool_args() → execution/mapper.py (时间参数构建)
agent.py::_schedule_tool_for_session() → execution/mapper.py (动态工具选择)

【工具Schema】
routes.py::_build_tool_schemas()     → tool_registry.py::get_tool_definitions() (已存在，直接使用)

【存储】
routes.py::conversation_store (dict) → db/repositories.py::ConversationRepository
routes.py::run_store (dict)          → db/repositories.py::RunRepository
routes.py::message_store (dict)      → db/repositories.py::MessageRepository
routes.py::approval_store (dict)     → db/repositories.py::ApprovalRepository
context.py::recent_entities (list)   → db/repositories.py::TaskRepository (通过 task.artifacts)
context.py::recent_tool_calls (list) → db/repositories.py::StepRepository (通过 step.tool_result)

【持久化（新增）】
无                                    → db/models.py 或直接在 repositories.py 使用 SQL 列
无                                    → DB 表: assistant_tasks
无                                    → DB 表: assistant_task_steps
无                                    → DB 表: assistant_artifacts

【MCP 层（零改动）】
GreenBookMCPServer.execute_tool()    → 不变，ExecutionEngine 通过它调用工具
tool_registry.get_tool()             → 不变
ToolContext                          → 不变（auth + session + java + creator 注入）
tools/*.py (16 handlers)             → 不变
```

## 2.2 概念映射速查

| 旧概念 | 旧位置 | 新概念 | 新位置 |
|--------|--------|--------|--------|
| "Run" (一次 LLM 循环) | routes.py run_store | Run (保留，关联到 Task) | db/repositories.py |
| "Conversation Goal" | 无（隐含在 session） | Task | task/models.py |
| "active draft" | context.py active_draft_id | Task.artifacts[最新DRAFT] | task/registry.py |
| "intent" (5元组布尔) | agent.py _turn_intents | TaskIntent (结构化) | task/models.py |
| "tool call sequence" | agent.py if-else 门控 | CapabilityDAG | planning/planner.py |
| "recent entity" | context.py RecentEntity | ArtifactRef | task/models.py |
| "tool result" | routes.py tool_handler 回调 | Step.tool_result | execution/models.py |

---

# 3. 第一阶段 MVP：最小可运行版本

## 3.1 MVP 目标

Phase 1 完成后，系统必须支持以下核心功能（与当前完全一致）：

| 功能 | 用户输入示例 | 旧路径 | Phase 1 路径 |
|------|------------|--------|-------------|
| 创建帖子 | "帮我写一篇Java文章" | agent._turn_intents → LLM → tool | ✅ 保留旧路径 + 记录 Task |
| 修改帖子 | "修改刚才文章标题" | agent._turn_intents → LLM → tool | ✅ 保留旧路径 + TaskRegistry 匹配 |
| 查询帖子 | "搜索社区Java帖子" | agent._turn_intents → LLM → tool | ✅ 保留旧路径 + 记录 Task |
| 定时发布 | "明天8点发布" | agent._turn_intents → LLM → tool | ✅ 保留旧路径 + 记录 Task |
| 简单问答 | "Java是什么" | LLM 直接回答 | ✅ 不变 |

## 3.2 Phase 1 执行路径（双轨并行）

```
POST /conversations/{id}/messages
    │
    ├── [NEW PATH — 仅记录，不改变执行]
    │   ├── TaskUnderstanding.understand() → TaskIntent (只记录到日志)
    │   └── TaskRegistry.create_or_get() → Task (只记录到 DB)
    │
    └── [OLD PATH — 实际执行，完整保留]
        └── agent.run() → _turn_intents → LLM → tool_handler → MCP
              │
              └── 工具成功后，通过回调更新 Task.artifacts
                 (draft_id, schedule_id 等同步到 Task)
```

**关键设计：旧路径一字不改，新路径只是"旁听"。**

新路径做的事情：
1. 用户消息到达时 → TaskUnderstanding 生成 TaskIntent → 存 DB
2. LLM tool call 成功时 → 回调更新 Task.artifacts（draft_id/schedule_id 同步）
3. 下一轮对话时 → TaskRegistry 能找到上一轮创建的 Task

**不改变的行为：**
- LLM 看到的工具 Schema 不变
- 工具调用顺序不变
- 会话历史不变
- 前端 API 不变

## 3.3 Phase 1 需新增的最小代码

```
只新增 3 个文件:

1. task/models.py           (~180 行)
   只包含: Task, TaskStatus, TaskIntent, ArtifactRef, EntityHint
   （不包含: Requirement, Constraint — 这些 Phase 3 才需要）

2. task/registry.py         (~200 行，MVP 版本)
   只包含: TaskRegistry
     - create_task()
     - get_task()
     - list_tasks()
     - resolve_task_by_id()
     - resolve_task_by_hint() (简单的 label 子串匹配，不做 LLM 语义匹配)

3. db/connection.py         (~50 行)
   只包含: PostgreSQL async engine + session factory

数据库迁移（3 张表）:
   - assistant_tasks
   - assistant_artifacts
   (assistant_task_steps 留到 Phase 4)
```

## 3.4 Phase 1 修改的文件（最小变更集）

```
main.py:
   +5 行: lifespan 中初始化 DB engine
   +2 行: 创建 TaskRegistry(db)

routes.py:
   +3 行: send_message() 开头调用 TaskRegistry.create_or_get()
   +2 行: tool_handler 回调成功后更新 task.artifacts
   (总共约 10 行改动)

agent.py:
   +2 行: run() 签名新增 task 参数（可选，默认 None）
   (零行为变更)
```

---

# 4. 开发顺序

## Phase 0: DB 基础设施（2 天）

### 任务
1. 新增 `packages/assistant_core/greenbook_assistant_core/db/connection.py`
2. 新增 PostgreSQL migration: `assistant_conversations`, `assistant_messages`, `assistant_runs`, `assistant_approvals`
3. 修改 `main.py` lifespan 初始化连接池
4. 修改 `routes.py` 将 4 个内存 dict store 改为 DB 读写

### 改动量
- 新增: ~50 行
- 修改: main.py (+15行), routes.py (~60 行改动)
- SQL: ~80 行 DDL

### 风险
- **低** — 只换存储后端，不改业务逻辑
- 需确保 Docker Compose 已有 PostgreSQL（已有）

### 验收
- 现有 E2E 测试通过
- 进程重启后 conversation/message 不丢失

---

## Phase 1: Task Model + Task Registry（3 天）

### 任务
1. 新增 `task/models.py` — Task, TaskStatus, TaskIntent（MVP 精简版）, ArtifactRef
2. 新增 `task/registry.py` — TaskRegistry MVP（CRUD + 简单 label 匹配）
3. 新增 DB migration: `assistant_tasks`, `assistant_artifacts`
4. 修改 `main.py` — 注入 TaskRegistry
5. 修改 `routes.py` send_message() — 旁路调用 TaskRegistry（仅记录）
6. 修改 `agent.py` run() — 接受可选 task 参数

### 改动量
- 新增: ~430 行
- 修改: main.py (+3行), routes.py (+15行), agent.py (+3行)
- SQL: ~60 行 DDL

### 风险
- **低** — 新路径只是记录，不参与执行决策
- Task 表为空时不报错（所有查询返回 None 即可）

### 验收
- 每轮对话自动创建 Task 记录到 DB
- 工具调用成功后 Task.artifacts 有对应记录
- 下一轮对话可通过 `target_task_hint` 匹配到上一轮的 Task
- **现有功能完全不受影响**

---

## Phase 2: Task Understanding（3 天）

### 任务
1. 新增 `task/understanding.py` — TaskUnderstanding (L1+L2)
2. 修改 `routes.py` send_message() — 调用 TaskUnderstanding.understand()
3. 将 `agent.py` 的 `_turn_routing_hint()` 逻辑迁移到 L1 快速路径
4. 实现 L2 LLM 深度理解（带 fallback）
5. 将 TaskIntent 注入 system prompt（作为工作上下文）

### 改动量
- 新增: ~200 行
- 修改: routes.py (+15行), agent.py (+20行: 注入 TaskIntent 到 _build_system_prompt)

### 风险
- **中** — L2 LLM 调用增加延迟和成本
- 缓解: L1 处理 60%+ 请求，L2 仅用于模糊/复合意图
- L2 失败时 fallback 到 L1（安全网）

### 验收
- 语义相似意图正确归类（"优化"、"改进"、"提升" → IMPROVE_CONTENT）
- 复合意图识别（"搜索+分析+创建+发布" → COMPOSITE，含 4 个 Requirement）
- TaskIntent 准确率 > 80%

---

## Phase 3: Capability + Planner（3 天）

### 任务
1. 新增 `planning/capability.py` — Capability 模型 + 11 个目录条目
2. 新增 `planning/planner.py` — Planner.plan()
3. 修改 `agent.py` run() — 分流: COMPOSITE → Planner → 记录 Plan / 简单 → 旧路径
4. 新增 Task.plan 字段的 DB 存储

### 改动量
- 新增: ~300 行
- 修改: agent.py (+20行: 分流逻辑)
- SQL: 无（plan 字段已在 Phase 1 的 JSONB 中）

### 风险
- **中** — Planner LLM 调用增加延迟
- 缓解: 仅 COMPOSITE 任务触发 Planner（< 20% 请求）
- Plan 当前只记录（observability），不实际执行

### 验收
- COMPOSITE 任务生成合理的 CapabilityDAG
- DAG 通过校验（无环、依赖正确）
- SEARCH → ANALYZE → CREATE → VALIDATE → PUBLISH 5 步链正确生成

---

## Phase 4: Execution Engine（5 天）

### 任务
1. 新增 `execution/models.py` — Step, StepStatus
2. 新增 `execution/mapper.py` — CapabilityToolMapper
3. 新增 `execution/engine.py` — ExecutionEngine
4. 新增 DB migration: `assistant_task_steps`
5. 修改 `agent.py` run() — 有 Plan → ExecutionEngine.execute()
6. 修改 `routes.py` — 注入 ExecutionEngine

### 改动量
- 新增: ~550 行
- 修改: agent.py (+30行), routes.py (+10行)
- SQL: ~40 行 DDL

### 风险
- **高** — 实际执行路径改变
- 缓解:
  - 先从最简单 DAG（2 步: SEARCH → CREATE）开始验证
  - 保留旧路径作为 fallback
  - 在 `agent.py` 中用 feature flag 控制走新旧路径
- 需要 3-5 个集成测试覆盖核心场景

### 验收
- 2 步 DAG 正确执行
- 3 步 DAG（SEARCH → CREATE → SCHEDULE）正确执行
- 工具失败时正确重试
- Checkpoint 持久化到 DB
- 从 Checkpoint 恢复执行
- **旧路径仍然可用（通过 feature flag）**

---

## Phase 5: 收敛（2 天）

### 任务
1. 删除 `agent.py` 中 `_turn_intents()`, `_turn_routing_hint()`, `_turn_tool_filter()`
2. 删除 `agent.py` 行 490-522 的 if-else 顺序工具门控
3. 简化 `routes.py` 中的 `_build_tool_schemas()` — 改为动态生成
4. 删除 `services/greenbook_mcp/greenbook_mcp_server/workflows/`
5. 更新 `__init__.py` 导出

### 改动量
- 删除: ~200 行
- 修改: agent.py (-200行), routes.py (-80行)

### 风险
- **低** — 此时新旧路径已充分验证

### 验收
- 全功能回归测试通过
- 代码行数净减少（agent.py: 543→~200, routes.py: 1238→~900）

---

# 5. 风险分析：哪些地方容易破坏现有功能

## 5.1 MCP 工具调用链

```
受影响: ❌ 否
─────────────────
GreenBookMCPServer, ToolContext, tool_registry, 16 handlers — 全部零改动。
ExecutionEngine 通过 mapper.py 调用 mcp.execute_tool()，参数构建逻辑
从 routes.py 迁移到 mapper.py 时需要确保:

风险点 1: time_parser 时间参数构建
  - 旧: routes.py _normalize_schedule_tool_args() 在 tool_handler 回调中
  - 新: mapper.py build_tool_call() 中调用
  - 保障: 保留 time_parser.py 不变，mapper.py 直接 import 使用

风险点 2: community_references 注入
  - 旧: routes.py 用 nonlocal 保存搜索结果，在 create_draft 时注入
  - 新: mapper.py 从 Step.artifact 中读取上游搜索结果
  - 保障: Phase 4 中需要验证 search→create artifact 传递正确

风险点 3: idempotency_key 生成
  - 旧: ToolContext.idempotency_key() 使用 conversation_id + operation + scope
  - 新: 不变，mapper 直接使用 ToolContext
  - 保障: 零风险
```

## 5.2 Java Backend 通信

```
受影响: ❌ 否
─────────────────
JavaClient — 零改动。
所有 Java REST API 调用仍在 MCP handler 中完成。

风险点 1: Capability 兑换
  - 旧: routes.py 中处理
  - 新: 不变，仍在 tool_handler 中（通过 MCP → handler → JavaClient）
  - 保障: 零风险

风险点 2: GET-after-write 验证
  - 旧: content.py/publication.py handler 中实现
  - 新: 不变，handler 完全不改
  - 保障: 零风险
```

## 5.3 Creator Agent 通信

```
受影响: ❌ 否
─────────────────
CreatorClient — 零改动。
create_task() / wait_for_completion() / get_artifact() — 全部在 MCP handler 中。

风险点: 无。Creator Agent 的 LangGraph pipeline 对 Assistant 完全透明。
```

## 5.4 前端调用

```
受影响: ❌ 否（Phase 1-4）
─────────────────────────
所有 HTTP API 接口签名不变:
  POST /conversations/{id}/messages → 202 RunAcceptedResponse
  GET  /runs/{run_id}/events → SSE Stream
  GET  /conversations → ConversationListResponse

Phase 5 新增可选 API（向后兼容）:
  GET  /conversations/{id}/tasks → Task 列表（新增）
  GET  /tasks/{id} → Task 详情 + Steps（新增）

风险点: Phase 5 中 RunResponse.steps[] 格式可能变化（从 events 派生 → 从 Step 表派生）
  - 保障: 保持 JSON 格式向后兼容
```

## 5.5 数据库迁移

```
受影响: ⚠️ 中
─────────────────
从内存 dict → PostgreSQL 是最容易出问题的地方。

风险点 1: 现有数据丢失
  - Phase 0 迁移时，旧内存数据无法迁移到 DB（重启即丢失）
  - 影响: 低。开发环境可接受，生产环境尚无持久化需求

风险点 2: 连接池耗尽
  - asyncpg 连接池默认 10 个连接
  - 保障: 设置 pool_size=5, max_overflow=10，够用

风险点 3: 事务边界
  - Task 创建和 Step 更新应在同一事务中
  - 保障: repositories.py 使用同一 AsyncSession

风险点 4: JSONB 列查询
  - requirements, constraints, artifacts 使用 JSONB
  - 保障: 不在 JSONB 列上做复杂查询（只做 = 匹配），索引在 (conversation_id, status)
```

## 5.6 并发与竞态

```
受影响: ⚠️ 低（当前单进程，风险低）

当前系统是单进程 FastAPI，不存在并发竞态。
如果将来多 Worker：
  - Task 更新使用 version 字段乐观锁（UPDATE ... WHERE version = :old_version）
  - Step 更新使用 CAS（status = PENDING → IN_PROGRESS）
  - 两者都在 Phase 1 的 Task 模型中预留了字段
```

## 5.7 总体风险矩阵

| 组件 | Phase 0 | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 5 |
|------|---------|---------|---------|---------|---------|---------|
| MCP 工具 | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 | 🟢 |
| Java Backend | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 |
| Creator Agent | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 |
| 前端 API | 🟢 | 🟢 | 🟢 | 🟢 | 🟢 | 🟡 |
| 数据库 | 🟡 | 🟢 | 🟢 | 🟢 | 🟡 | 🟢 |
| LLM 延迟 | 🟢 | 🟢 | 🟡 | 🟡 | 🟢 | 🟢 |
| 执行正确性 | 🟢 | 🟢 | 🟢 | 🟢 | 🔴 | 🟡 |

🟢 无风险 | 🟡 低风险 | 🔴 需关注

---

# 附录 A: Phase 1 详细改动清单（文件 diff 预览）

## A.1 新增文件

### `packages/assistant_core/greenbook_assistant_core/task/__init__.py`
```python
"""Task subsystem — models, understanding, and registry."""
```

### `packages/assistant_core/greenbook_assistant_core/task/models.py`
```python
"""Task, TaskIntent, ArtifactRef — core domain models."""
# 包含: TaskStatus(enum), Task, TaskIntent, ArtifactRef, EntityHint
# ~180 行 Pydantic 模型定义
```

### `packages/assistant_core/greenbook_assistant_core/task/registry.py`
```python
"""TaskRegistry — CRUD + matching for Tasks within a Conversation."""
# 包含: class TaskRegistry
#   - create_task(intent, conv_id, user_id) -> Task
#   - get_task(task_id) -> Task | None
#   - list_tasks(conv_id, status=None) -> list[Task]
#   - resolve_task(conv_id, intent) -> Task | None
#   - add_artifact(task_id, artifact) -> None
# ~200 行 MVP 版本
```

### `packages/assistant_core/greenbook_assistant_core/db/__init__.py`
```python
"""Database connection and repositories."""
```

### `packages/assistant_core/greenbook_assistant_core/db/connection.py`
```python
"""PostgreSQL async connection pool management."""
# create_async_engine, async_sessionmaker
# ~50 行
```

## A.2 修改文件

### `apps/assistant_api/greenbook_assistant_api/main.py`
```diff
+ from greenbook_assistant_core.db.connection import create_db_engine
+ from greenbook_assistant_core.task.registry import TaskRegistry

  @asynccontextmanager
  async def lifespan(app):
      ...
+     # Database
+     db_url = os.getenv("ASSISTANT_DB_URL", "postgresql+asyncpg://...")
+     engine = create_db_engine(db_url)
+     app.state.db = engine
+
+     # Task Registry (NEW)
+     app.state.task_registry = TaskRegistry(engine)

      yield
+
+     await engine.dispose()
```

### `apps/assistant_api/greenbook_assistant_api/api/routes.py` — send_message()
```diff
  async def send_message(conversation_id, body, request):
      auth = _get_auth(request)
      session = _get_session(request, conversation_id)
+
+     # ── NEW: Task Registry (旁路，仅记录) ──
+     task_registry = request.app.state.task_registry
+     existing_tasks = await task_registry.list_tasks(conversation_id)
+     # MVP: 简单推断 TaskIntent（用旧关键词逻辑结果）
+     intent = _quick_intent_from_legacy(body.content, session)
+     task = await task_registry.resolve_or_create(intent, conversation_id, auth)
+     logger.info("task_resolved task_id=%s relation=%s", task.task_id, intent.relation)

      # ... 现有逻辑不变 ...

      # tool_handler 回调成功后，同步 artifact 到 Task
+     if result.get("ok") and isinstance(result.get("data"), dict):
+         data = result["data"]
+         if data.get("draft_id"):
+             await task_registry.add_artifact(task.task_id, ArtifactRef(
+                 artifact_id=str(uuid4()),
+                 task_id=task.task_id,
+                 step_id="legacy",  # 旧路径没有 Step
+                 artifact_type="DRAFT",
+                 resource_id=str(data["draft_id"]),
+                 resource_kind="DRAFT",
+                 summary=str(data.get("title", "")),
+             ))
```

### `packages/assistant_core/greenbook_assistant_core/agent.py`
```diff
  class CommunityOperationsAssistant:
      async def run(
          self,
          user_message: str,
          session: SessionContext,
          *,
          tool_handler,
+         task: Any = None,  # NEW: Task 对象（可选，Phase 1-3 为 None）
          ...
      ):
+         # NEW: 注入 Task 信息到 system prompt
+         if task:
+             task_context = f"\n## 当前任务\n- 任务ID: {task.task_id}\n- 目标: {task.goal}\n"
+         else:
+             task_context = ""

          # ... 现有逻辑不变，task_context 附加到 messages[0] ...
```

---

# 附录 B: Phase 各阶段改动量汇总

| Phase | 新增文件 | 新增行数 | 修改文件 | 修改行数 | 删除行数 | DB 迁移 |
|-------|---------|---------|---------|---------|---------|---------|
| 0 | 2 | ~50 | 2 | ~80 | 0 | 4 张表 |
| 1 | 3 | ~430 | 3 | ~20 | 0 | 2 张表 |
| 2 | 1 | ~200 | 2 | ~35 | 0 | 0 |
| 3 | 2 | ~300 | 1 | ~20 | 0 | 0 |
| 4 | 3 | ~550 | 2 | ~40 | 0 | 1 张表 |
| 5 | 0 | 0 | 4 | -280 | ~200 | 0 |
| **合计** | **11** | **~1,530** | — | **~-85** | **~200** | **7 张表** |

> 注：Phase 5 删除 ~200 行旧代码（agent.py _turn_* 函数族），净增约 1,330 行。
