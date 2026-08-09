# GreenBook Agent Runtime — 重构实施计划

> 日期: 2026-08-07
> 状态: 设计完成 — 等待执行
> 前置:
>   - `docs/reports/greenbook-agent-runtime-architecture-review.md` (架构审查·最终方案)
>   - `docs/reports/greenbook-agent-runtime-migration-roadmap.md` (迁移路线·函数映射)
>
> 本文档是最终的可执行重构计划：每个 Phase 的精确文件变更、验证用例、风险控制。

---

# 0. 架构总览（简化后）

## 0.1 目标目录结构

```
packages/assistant_core/greenbook_assistant_core/
├── __init__.py
├── agent.py                       # [Phase 2-3 渐进精简 · 543→~150 行]
├── context.py                     # [Phase 3 精简]
├── memory.py                      # [Phase 0 DB-backed]
├── time_parser.py                 # [保留·零改动]
├── middleware.py                  # [保留]
├── prompts/
│   └── system.py
│
├── task/                          # [新增·Phase 1-2]
│   ├── __init__.py
│   ├── models.py                  # Task, TaskIntent, TaskStatus, ArtifactRef
│   ├── understanding.py           # TaskUnderstanding (L1+L2)
│   └── registry.py                # TaskRegistry (CRUD + 3级匹配)
│
├── orchestration/                 # [新增·Phase 3]
│   ├── __init__.py
│   └── orchestrator.py            # Capability定义 + 3模板 + PlannerRouter + DAG执行
│
└── db/                            # [新增·Phase 0]
    ├── __init__.py
    └── connection.py              # asyncpg 连接池
```

## 0.2 不修改的文件（绝对不改）

```
services/greenbook_mcp/   — 全部 12 个 .py 文件
packages/java_client/     — 全部 3 个 .py 文件
packages/creator_client/  — 全部 2 个 .py 文件
packages/contracts/       — 全部 4 个 .py 文件
packages/security/        — 全部 5 个 .py 文件
packages/assistant_core/time_parser.py
creator-agent/            — 全部文件
```

---

# 1. Phase 0: 数据库持久化（3 天）

## 1.1 目标

将 4 个内存 dict 迁移到 PostgreSQL。功能零变化。这是后续所有 Phase 的基础。

## 1.2 新增文件

```
packages/assistant_core/greenbook_assistant_core/db/__init__.py
  内容: package marker

packages/assistant_core/greenbook_assistant_core/db/connection.py
  内容:
    - create_db_engine(db_url) → sqlalchemy.AsyncEngine
    - get_session(engine) → AsyncSession context manager
    - DB_URL 从环境变量 ASSISTANT_DB_URL 读取
    - 默认: postgresql+asyncpg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator
  约 40 行
```

## 1.3 数据库 DDL（新增 migration）

```sql
-- 4 张表，从当前内存 dict 迁移到 PostgreSQL

CREATE TABLE assistant_conversations (
    conversation_id UUID PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    title VARCHAR(120),
    active_draft_id VARCHAR(128),
    active_schedule_id VARCHAR(128),
    active_post_id VARCHAR(128),
    pending_approval JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_conv_user ON assistant_conversations(user_id, tenant_id);

CREATE TABLE assistant_messages (
    message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES assistant_conversations(conversation_id),
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    trace_id VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_msg_conv ON assistant_messages(conversation_id, created_at);

CREATE TABLE assistant_runs (
    run_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES assistant_conversations(conversation_id),
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    content TEXT,
    error_code VARCHAR(64),
    error_message TEXT,
    tool_rounds INT DEFAULT 0,
    trace_id VARCHAR(64),
    events JSONB DEFAULT '[]',
    approval_id VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_run_conv ON assistant_runs(conversation_id);

CREATE TABLE assistant_approvals (
    approval_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES assistant_conversations(conversation_id),
    run_id UUID NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    operation VARCHAR(128) NOT NULL,
    resource_id VARCHAR(128),
    description TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_approval_conv ON assistant_approvals(conversation_id);
```

## 1.4 修改文件

### `apps/assistant_api/greenbook_assistant_api/main.py`

```diff
变更点:
  1. lifespan 开头新增 DB 连接池初始化
     + from greenbook_assistant_core.db.connection import create_db_engine
     + engine = create_db_engine(db_url)
     + app.state.db = engine

  2. lifespan 结尾新增引擎释放
     + await engine.dispose()

  3. 移除 4 个内存 dict 的初始化（删除以下行）
     - app.state.conversation_store = {}
     - app.state.run_store = {}
     - app.state.approval_store = {}
     - app.state.message_store = {}

影响行数: +15 / -4
```

### `apps/assistant_api/greenbook_assistant_api/api/routes.py`

```diff
变更点:
  1. 所有 app.state.xxx_store → DB 查询
     - conversation_store[conv_id] → await db.fetch_one("SELECT ... WHERE conversation_id = $1", conv_id)
     - message_store[conv_id] → await db.fetch("SELECT role, content, ... WHERE conversation_id = $1 ORDER BY created_at")
     - run_store[run_id] → await db.fetch_one("SELECT ... WHERE run_id = $1", run_id)
     - approval_store[approval_id] → await db.fetch_one("SELECT ... WHERE approval_id = $1", approval_id)

  2. _save_session() → DB UPSERT
  3. _auth_store_put() → DB INSERT
  4. _auth_store_get() → DB SELECT
  5. list_conversations() → DB SELECT + ORDER BY updated_at

  6. 以下函数签名不变:
     - send_message() —— 输入输出不变
     - get_run() / get_run_events() / stream_run_events()
     - approve_operation() / reject_operation()
     - cancel_run() / interrupt_run()

影响行数: ~100 行改动（集中在查询函数）
```

## 1.5 风险

| 风险 | 等级 | 缓解 |
|------|------|------|
| SQL 查询性能不如内存 dict | 低 | 4 张表数据量极小（单用户 < 1000 行），索引覆盖所有查询 |
| asyncpg 连接池耗尽 | 低 | pool_size=5, max_overflow=10 |
| DB 不可用时系统无法启动 | 中 | 健康检查 `/health` 增加 DB 状态；开发环境 Docker Compose 已有 PostgreSQL |
| JSONB 列性能 | 低 | events JSONB 只整体读写，不做内部查询 |

## 1.6 验收测试

```
测试 1: 创建会话
  POST /conversations {"title": "测试会话"}
  → 201, conversation_id
  → 重启 Assistant → GET /conversations → 会话仍存在

测试 2: 发送消息
  POST /conversations/{id}/messages {"content": "Java是什么"}
  → 202, run_id
  → GET /conversations/{id}/messages → 包含 user 和 assistant 消息
  → 重启 Assistant → 消息仍存在

测试 3: 创建帖子（使用现有 E2E 测试）
  POST /conversations/{id}/messages {"content": "帮我写一篇Java入门文章"}
  → 202, run_id
  → SSE events 包含 TOOL_CALL_STARTED + TOOL_CALL_COMPLETED
  → GET /runs/{run_id} → status=COMPLETED

测试 4: 审批流
  POST /conversations/{id}/messages {"content": "立即发布那篇草稿"}
  → 202, status=WAITING_APPROVAL
  → POST /approvals/{approval_id}/approve {"decision": "APPROVE"}
  → 重启 → GET /runs/{run_id} → approval 状态仍为 APPROVED
```

---

# 2. Phase 1: Task 旁路记录（3 天）

## 2.1 目标

引入 Task 概念，但**不改变任何执行路径**。旧代码完整保留，新代码只做"旁听"——在用户发消息时创建一个 Task 记录，在工具成功时更新 Task 的 artifacts。

## 2.2 新增文件

```
packages/assistant_core/greenbook_assistant_core/task/__init__.py    (~5 行)
packages/assistant_core/greenbook_assistant_core/task/models.py      (~100 行)
packages/assistant_core/greenbook_assistant_core/task/registry.py    (~130 行)
```

### task/models.py 内容

```python
# 只包含 Phase 1 需要的模型（不包含理解/规划/执行相关字段）

class TaskStatus(str, Enum):
    READY = "READY"             # Phase 1 所有 Task 默认为 READY
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class Task(BaseModel):
    task_id: str                # UUID
    conversation_id: str
    user_id: str
    tenant_id: str
    goal: str = ""              # Phase 1 暂时为空，Phase 2 由 TaskUnderstanding 填充
    goal_category: str = ""     # Phase 1 暂时为空
    status: TaskStatus = TaskStatus.READY
    artifacts: list[ArtifactRef] = []
    depends_on: list[str] = []
    version: int = 1
    created_at: datetime
    updated_at: datetime

class ArtifactRef(BaseModel):
    artifact_id: str
    task_id: str
    step_id: str = ""           # Phase 1 为空（没有 Step）
    artifact_type: str          # DRAFT | SEARCH_RESULT | SCHEDULE
    resource_id: str | None     # 外部资源 ID
    resource_kind: str | None   # DRAFT | POST | SCHEDULE
    summary: str | None
    created_at: datetime
```

### task/registry.py 内容

```python
class TaskRegistry:
    """Phase 1 MVP: CRUD + 简单 label 匹配"""

    def __init__(self, db: AsyncEngine): ...

    async def create_task(self, conv_id, user_id, tenant_id) -> Task: ...
    async def get_task(self, task_id) -> Task | None: ...
    async def list_tasks(self, conv_id, status=None) -> list[Task]: ...
    async def add_artifact(self, task_id, artifact: ArtifactRef) -> None: ...

    async def resolve_task(self, conv_id, user_hint: str | None) -> Task | None:
        """3级匹配（Phase 1 只用最简单的第3级）:
        1. user_hint 是 UUID → get_task(hint)
        2. user_hint 有内容 → 子串匹配 goal
        3. 无 hint → 最近更新的 Task
        """
```

## 2.3 DB DDL（新增）

```sql
CREATE TABLE assistant_tasks (
    task_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES assistant_conversations(conversation_id),
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    goal_category VARCHAR(64) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL DEFAULT 'READY',
    artifacts JSONB DEFAULT '[]',
    depends_on UUID[] DEFAULT '{}',
    version INT DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX idx_task_conv ON assistant_tasks(conversation_id, status);

CREATE TABLE assistant_artifacts (
    artifact_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES assistant_tasks(task_id) ON DELETE CASCADE,
    step_id VARCHAR(128) NOT NULL DEFAULT '',
    artifact_type VARCHAR(64) NOT NULL,
    resource_id VARCHAR(128),
    resource_kind VARCHAR(32),
    summary VARCHAR(500),
    content_ref JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_artifact_task ON assistant_artifacts(task_id);
```

## 2.4 修改文件

### `apps/assistant_api/greenbook_assistant_api/main.py`

```diff
变更点:
  1. lifespan 中新增 TaskRegistry 初始化
     + from greenbook_assistant_core.task.registry import TaskRegistry
     + app.state.task_registry = TaskRegistry(app.state.db)

影响行数: +4
```

### `apps/assistant_api/greenbook_assistant_api/api/routes.py`

```diff
变更点:
  在 send_message() 中新增旁路逻辑（不影响现有代码）:

  位置: send_message() 开头，auth 和 session 加载完成后

  + # ── NEW Phase 1: Task 旁路记录 ──
  + registry = request.app.state.task_registry
  + # 获取或创建 Task (先用最简单的"最近Task"策略)
  + active_task = await registry.resolve_task(conversation_id, None)
  + if active_task is None or active_task.status in (
  +     TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED):
  +     active_task = await registry.create_task(
  +         conversation_id, auth.user_id, auth.tenant_id)
  + elif active_task.status == TaskStatus.READY:
  +     active_task.status = TaskStatus.IN_PROGRESS
  +     # (实际更新在 Phase 2 才做，Phase 1 只读不改)

  位置: tool_handler 回调末尾，result 返回前

  + # ── NEW Phase 1: 同步 Artifact ──
  + if result.get("ok") and isinstance(result.get("data"), dict):
  +     data = result["data"]
  +     if data.get("draft_id"):
  +         await registry.add_artifact(active_task.task_id, ArtifactRef(
  +             artifact_id=str(uuid4()),
  +             task_id=active_task.task_id,
  +             artifact_type="DRAFT",
  +             resource_id=str(data["draft_id"]),
  +             resource_kind="DRAFT",
  +             summary=str(data.get("title", "")),
  +         ))
  +     if data.get("schedule_id"):
  +         await registry.add_artifact(active_task.task_id, ArtifactRef(
  +             artifact_id=str(uuid4()),
  +             task_id=active_task.task_id,
  +             artifact_type="SCHEDULE",
  +             resource_id=str(data["schedule_id"]),
  +             resource_kind="SCHEDULE",
  +             summary=str(data.get("draft_id", "")),
  +         ))

  + # 注意: 这些 artifact 记录不影响 tool_handler 的返回值

影响行数: +25
```

### `packages/assistant_core/greenbook_assistant_core/agent.py`

```diff
变更点:
  零改动。Phase 1 完全不接触 agent.py。
```

## 2.5 风险

| 风险 | 等级 | 缓解 |
|------|------|------|
| Task 创建/查询失败导致 send_message 报错 | 中 | 所有 Task 操作用 try/except 包裹，失败只记日志不抛异常 |
| Artifact 写入与现有 tool_handler 回调冲突 | 低 | artifact 记录在 tool_handler return 之前，不改变任何现有变量 |

## 2.6 验收测试

```
测试 1: Task 自动创建
  POST /conversations/{id}/messages {"content": "帮我写一篇Java文章"}
  → SELECT * FROM assistant_tasks WHERE conversation_id = '{id}'
  → 至少有 1 条 Task 记录

测试 2: Artifact 自动同步
  上述请求成功后
  → SELECT * FROM assistant_artifacts WHERE task_id = '{task_id}'
  → 至少有 1 条 artifact_type='DRAFT' 的记录

测试 3: 现有功能不受影响
  所有 E2E 测试通过（传入 --scenario Direct, CreatorDraft）

测试 4: 多轮 Task 延续
  轮次1: "创建Java文章"
  轮次2: "修改标题"
  → 两次都在同一个 Task 下（如果 resolve 逻辑正确返回同一个 Task）
```

---

# 3. Phase 2: TaskUnderstanding 替代旧意图识别（4 天）

## 3.1 目标

引入 L1+L2 意图理解，**替代但不删除** agent.py 中的 `_turn_intents()` 等函数。旧函数保留为 L1 快速路径，L2 作为新增的 LLM 深度路径。

## 3.2 新增文件

```
packages/assistant_core/greenbook_assistant_core/task/understanding.py   (~170 行)
```

### task/understanding.py 内容

```python
class TaskUnderstanding:
    """双层意图理解: L1 规则 + L2 LLM"""

    def __init__(self, llm, model):
        self.llm = llm
        self.model = model

    async def understand(
        self,
        user_message: str,
        session: SessionContext,
        existing_tasks: list[Task] | None = None,
    ) -> TaskIntent:
        """
        返回 TaskIntent（包含 goal_category + relation + target_task_hint）。

        内部路由:
          1. 调用 _needs_l2(user_message)
          2. 不需要 L2 → 用 L1 规则（复用 _turn_intents 的结果）构建 TaskIntent
          3. 需要 L2 → 调用 LLM → Pydantic 校验 → fallback 到 L1

        关键: L1 是旧 agent.py 中 _turn_intents/_turn_routing_hint 的精简版。
              L2 是新增的 LLM 语义理解。
        """

    # ── L1 规则 ──
    def _quick_intent(self, user_message, session, existing_tasks) -> TaskIntent:
        """复用 _turn_intents() 的 5 元组，翻译为 TaskIntent"""
        asks_create, asks_revise, asks_schedule, asks_cancel, asks_search = \
            _turn_intents(user_message)  # 从 agent.py import

        # 翻译为 TaskIntent
        if asks_create and asks_schedule:
            goal_category = "CREATE_CONTENT"
            ...
        ...

    # ── L2 路由判断 ──
    @staticmethod
    def _needs_l2(user_message: str) -> bool:
        """是否需要 LLM 深度理解"""
        # 模糊意动词
        AMBIGUOUS = {"优化", "提升", "改进", "整理", "重构", "润色"}
        # 复合信号
        COMPOSITE = {"然后", "之后", "同时", "并且", "再"}
        # 跨引用
        CROSS_REF = {"把", "将", "加入", "合并", "参考...结果"}

        text = user_message.strip()
        if any(w in text for w in AMBIGUOUS):
            return True
        if sum(1 for w in COMPOSITE if w in text) >= 2:
            return True
        # "把...结果..." 模式
        if "把" in text and any(w in text for w in ("结果", "分析", "搜索")):
            return True
        return False

    # ── L2 LLM ──
    async def _llm_understand(self, user_message, existing_tasks) -> TaskIntent:
        """调用 LLM 生成 TaskIntent。Prompt ~200 tokens。"""
        ...

    # ── Fallback ──
    def _fallback_intent(self, user_message) -> TaskIntent:
        """L2 失败时的终极兜底"""
        return TaskIntent(relation="DIRECT", goal_category="QUERY_INFO", goal="")
```

### agent.py 中的旧函数迁移路径

```
┌─────────────────────────────────────────────────────────────────────┐
│ 旧位置                          迁移方式                            │
├─────────────────────────────────────────────────────────────────────┤
│ _CREATE_MARKERS (行 25-28)      保留在 agent.py                     │
│ _SCHEDULE_MARKERS (行 29)       保留在 agent.py                     │
│ _has_future_time_expression()   → understanding.py (import)        │
│ _is_schedule_only_retry()       → understanding.py (import)        │
│ _asks_for_community_refs()      → understanding.py (import)        │
│ _schedule_tool_for_session()    → orchestrator.py (Phase 3)        │
│ _turn_intents() (行 99-114)     → understanding.py:_quick_intent() │
│ _turn_routing_hint() (行 132)   → understanding.py LLM 生成         │
│ _turn_tool_filter() (行 193)    → 保留为 L1 工具选择               │
│ PRODUCT_DEFAULTS (行 117-129)   → prompts/system.py                │
└─────────────────────────────────────────────────────────────────────┘
```

## 3.3 修改文件

### `packages/assistant_core/greenbook_assistant_core/agent.py`

```diff
变更点:
  1. run() 方法开头新增 TaskUnderstanding 调用

  + # ── NEW Phase 2: TaskUnderstanding ──
  + if hasattr(self, 'task_understanding') and self.task_understanding:
  +     task_intent = await self.task_understanding.understand(
  +         user_message, session, existing_tasks)
  + else:
  +     task_intent = None

  2. _build_system_prompt() 注入 TaskIntent 上下文

  + if task_intent:
  +     context_lines.append(f"- 意图: {task_intent.goal_category} ({task_intent.relation})")
  +     if task_intent.target_task_hint:
  +         context_lines.append(f"- 目标任务: {task_intent.target_task_hint}")

  3. run() 使用 task_intent 替代 _turn_routing_hint:

  - routing_hint = _turn_routing_hint(user_message, session)
  + routing_hint = _build_routing_hint_from_intent(task_intent) if task_intent \
  +     else _turn_routing_hint(user_message, session)

  4. run() 接受新的可选参数:

  + task: Any = None          # Phase 1 的 Task 对象
  + task_intent: Any = None   # Phase 2 的 TaskIntent 对象

影响行数: +35 / -10
```

### `apps/assistant_api/greenbook_assistant_api/api/routes.py`

```diff
变更点:
  在 send_message() 中，Phase 1 的旁路代码之后，agent.run() 调用之前:

  + # ── NEW Phase 2: TaskUnderstanding ──
  + task_understanding = request.app.state.task_understanding
  + existing_tasks = await registry.list_tasks(conversation_id)
  + task_intent = await task_understanding.understand(
  +     body.content, session, existing_tasks)

  + # 更新 Task 的 goal/goal_category
  + if task_intent and active_task:
  +     active_task.goal = task_intent.goal
  +     active_task.goal_category = task_intent.goal_category
  +     # (写入 DB)

  + # 传给 agent.run()
  + result = await assistant.run(
  +     ...,
  +     task=active_task,
  +     task_intent=task_intent,
  + )

影响行数: +25
```

### `apps/assistant_api/greenbook_assistant_api/main.py`

```diff
变更点:
  1. lifespan 中新增 TaskUnderstanding 初始化
     + from greenbook_assistant_core.task.understanding import TaskUnderstanding
     + app.state.task_understanding = TaskUnderstanding(llm, model)

影响行数: +3
```

## 3.4 agent.py 旧函数状态（Phase 2 结束）

```
agent.py 函数                       Phase 2 状态
─────────────────────────────────────────────────────────────
_turn_intents()                     ⚠️ 保留 — L1 回退用
_turn_routing_hint()                ⚠️ 保留 — L1 回退用
_turn_tool_filter()                 ⚠️ 保留 — SIMPLE 模式工具选择
_CREATE/SCHEDULE_MARKERS            ✅ 保留
_has_future_time_expression()       ✅ 保留
_is_schedule_only_retry()           ✅ 保留
_asks_for_community_references()    ✅ 保留
_schedule_tool_for_session()        ✅ 保留
PRODUCT_DEFAULTS                    ✅ 保留 (移到 prompts/)
CommunityOperationsAssistant.run()  ✂️ 修改 — 新增 task_intent 参数
_build_system_prompt()              ✂️ 修改 — 注入 task_intent 上下文
顺序工具门控 (行 490-522)            ⚠️ 保留 — SIMPLE 模式用
```

## 3.5 风险

| 风险 | 等级 | 缓解 |
|------|------|------|
| L2 LLM 超时 (5s) | 中 | 超时后回退 L1；L1 是旧代码，已验证可靠 |
| L2 LLM 输出不符合 Schema | 中 | Pydantic 校验 + JSON 修复 + 失败回退 L1 |
| L2 增加 ~1s 延迟 | 低 | 仅 ~20% 请求触发 L2；L1 < 1ms |
| L2 与 L1 结果冲突 | 低 | L1 是 fallback；L2 优先 |

## 3.6 验收测试

```
测试 1: 语义统一 (L2)
  POST {"content": "参考优秀文章优化一下"}
  → task_intent.goal_category = "IMPROVE_CONTENT"
  → task_intent.relation = "MODIFY_TASK"

  POST {"content": "借鉴热门内容重新整理"}
  → task_intent.goal_category = "IMPROVE_CONTENT"

  POST {"content": "提升文章质量"}
  → task_intent.goal_category = "IMPROVE_CONTENT"

测试 2: 复合意图 (L2)
  POST {"content": "搜索Java帖子，然后参考热门写法生成一篇文章，五分钟后发布"}
  → task_intent.goal_category = "CREATE_CONTENT"
  → task_intent 包含 SEARCH + CREATE + PUBLISH 需求

测试 3: 跨引用 (L2)
  (先创建Task A with artifact, Task B with artifact)
  POST {"content": "把刚才分析结果加入Java文章"}
  → task_intent.relation = "MODIFY_TASK"
  → task_intent.target_task_hint = "Java文章"

测试 4: L1 快速路径（不触发 L2）
  POST {"content": "列出我的草稿"}
  → _needs_l2() = False
  → 走 L1，不调用 LLM
  → task_intent.relation = "DIRECT"

测试 5: L2 Fallback
  (模拟 LLM 不可用)
  POST {"content": "优化一下刚才那个"}
  → L2 超时 → 回退 L1
  → task_intent.relation = "MODIFY_TASK" (L1 关键词匹配)
  → 用户操作不阻断

测试 6: 现有功能不受影响
  所有 E2E 测试通过
```

---

# 4. Phase 3: TaskOrchestrator 接管复杂任务（5 天）

## 4.1 目标

引入 TaskOrchestrator（3 种社区任务模板 + DAG 执行 + Step Checkpoint）。
SIMPLE 模式仍走旧路径。PLANNED 模式走新路径。

## 4.2 新增文件

```
packages/assistant_core/greenbook_assistant_core/orchestration/__init__.py      (~5 行)
packages/assistant_core/greenbook_assistant_core/orchestration/orchestrator.py  (~250 行)
```

### orchestrator.py 内容

```python
# ── 11 个 Capability 定义 ──
CAPABILITIES = {
    "SEARCH_COMMUNITY":      Capability(name=..., default_tool="community.search_public_posts", produces_artifact=True, artifact_type="SEARCH_RESULT"),
    "ANALYZE_CONTENT":       Capability(name=..., is_llm_step=True, produces_artifact=True, artifact_type="ANALYSIS_REPORT"),
    "GENERATE_CONTENT":      Capability(name=..., default_tool="content.create_draft", produces_artifact=True, artifact_type="DRAFT"),
    "IMPROVE_CONTENT":       Capability(name=..., default_tool="content.revise_draft", produces_artifact=True, artifact_type="DRAFT"),
    "VALIDATE_QUALITY":      Capability(name=..., is_llm_step=True, produces_artifact=True, artifact_type="VALIDATION_REPORT"),
    "SCHEDULE_PUBLISH":      Capability(name=..., default_tool="publication.schedule", produces_artifact=True, artifact_type="SCHEDULE"),
    "PUBLISH_NOW":           Capability(name=..., default_tool="publication.publish_now"),
    "MANAGE_SCHEDULE":       Capability(name=..., default_tool="publication.update_schedule"),
    "CANCEL_SCHEDULE":       Capability(name=..., default_tool="publication.cancel_schedule"),
    "GET_POST_DETAIL":       Capability(name=..., default_tool="community.get_post"),
    "REPLY_USER":            Capability(name=..., default_tool="interaction.send_reply"),
}

# ── 3 种社区任务模板 ──
TEMPLATES = {
    "CREATE_WITH_RESEARCH": [
        ("s1", "SEARCH_COMMUNITY",   []),
        ("s2", "ANALYZE_CONTENT",    ["s1"]),
        ("s3", "GENERATE_CONTENT",   ["s2"]),
    ],
    "CREATE_AND_PUBLISH": [
        ("s1", "GENERATE_CONTENT",   []),
        ("s2", "VALIDATE_QUALITY",   ["s1"]),
        ("s3", "SCHEDULE_PUBLISH",   ["s2"]),
    ],
    "FULL_PIPELINE": [
        ("s1", "SEARCH_COMMUNITY",   []),
        ("s2", "ANALYZE_CONTENT",    ["s1"]),
        ("s3", "GENERATE_CONTENT",   ["s2"]),
        ("s4", "VALIDATE_QUALITY",   ["s3"]),
        ("s5", "SCHEDULE_PUBLISH",   ["s4"]),
    ],
}

class PlannerRouter:
    """判断 Task 走哪条执行路径"""

    @staticmethod
    def route(task_intent: TaskIntent) -> ExecutionMode:
        """
        DIRECT:  relation == DIRECT 或 goal_category == QUERY_INFO
                 → 直接 LLM 回答，不调工具

        SIMPLE:  1 个 requirement，不含 ANALYZE/PUBLISH
                 → 旧路径 tool calling (agent.py 现有逻辑)

        PLANNED: 3+ requirements，含 ANALYZE 或 PUBLISH
                 → TaskOrchestrator 执行 DAG
        """

    @staticmethod
    def select_template(task_intent: TaskIntent) -> str | None:
        """
        根据 requirements 匹配模板:
        - SEARCH + ANALYZE + CREATE + PUBLISH → "FULL_PIPELINE"
        - SEARCH + ANALYZE + CREATE → "CREATE_WITH_RESEARCH"
        - CREATE + PUBLISH → "CREATE_AND_PUBLISH"
        - 其他 → None (LLM fallback)
        """

class TaskOrchestrator:
    """社区任务编排器"""

    def __init__(self, mcp: GreenBookMCPServer, llm, db): ...

    async def execute(self, task: Task, session: SessionContext) -> Task:
        """
        1. 模板匹配 (select_template)
        2. 初始化 Steps (从模板生成)
        3. 按拓扑顺序执行 (串行，社区任务无并行需求)
        4. 每步完成后 Checkpoint 到 DB
        5. 成功→提取 Artifact→保存到 Task
        6. 失败→判断 retryable→重试或标记失败
        7. 全部完成→更新 Task.status=COMPLETED
        """

    async def _execute_step(self, step: Step, task: Task, session) -> Step:
        """
        1. step.status = IN_PROGRESS; await save_step(step)  # Checkpoint 1
        2. 获取 Capability
        3. 如果 is_llm_step → LLM 推理
           否则 → mcp.execute_tool(capability.default_tool, ...)
        4. 成功 → step.status = COMPLETED; 提取 Artifact
           失败 → retryable? → retry : FAILED
        5. await save_step(step)  # Checkpoint 2
        """

    # Capability → Tool 参数构建（内联，不独立 mapper 模块）
    def _build_tool_args(self, capability, step, task) -> dict:
        """从 Task 的 requirements/constraints + 上游 artifacts 构建工具参数"""
        ...
```

## 4.3 DB DDL（新增）

```sql
CREATE TABLE assistant_task_steps (
    step_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES assistant_tasks(task_id) ON DELETE CASCADE,
    ordinal INT NOT NULL,
    capability VARCHAR(64) NOT NULL,
    capability_description TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    depends_on UUID[] DEFAULT '{}',
    tool_name VARCHAR(128),
    tool_args JSONB,
    tool_result JSONB,
    artifact_id UUID,
    error_code VARCHAR(64),
    error_message TEXT,
    retry_count INT DEFAULT 0,
    max_retries INT DEFAULT 3,
    checkpoint_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
CREATE INDEX idx_step_task ON assistant_task_steps(task_id, ordinal);
```

## 4.4 修改文件

### `packages/assistant_core/greenbook_assistant_core/agent.py`

```diff
变更点:
  1. run() 中新增 PlannerRouter 分流:

  + # ── NEW Phase 3: PlannerRouter ──
  + mode = PlannerRouter.route(task_intent) if task_intent else ExecutionMode.SIMPLE
  +
  + if mode == ExecutionMode.PLANNED and task is not None:
  +     # 走 TaskOrchestrator
  +     orchestrator = self.orchestrator
  +     task = await orchestrator.execute(task, session)
  +     final_content = self._render_completion(task)
  +     return {"run_id": rid, "content": final_content, "task_id": task.task_id, ...}
  +
  + elif mode == ExecutionMode.DIRECT:
  +     # 直接 LLM 回答（不调工具）
  +     ...

  + # SIMPLE 模式: 保留现有 LLM tool calling 循环
  + # (以下是不变的旧代码)
    turn_tools = ...

  2. run() 签名新增参数:

  + orchestrator: Any = None

  3. SIMPLE 模式中的工具门控保留（行 490-522 不变）

影响行数: +40 / -0
```

### `apps/assistant_api/greenbook_assistant_api/main.py`

```diff
变更点:
  1. lifespan 中新增 TaskOrchestrator 初始化
     + from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
     + app.state.orchestrator = TaskOrchestrator(mcp, llm, db)

影响行数: +3
```

### `apps/assistant_api/greenbook_assistant_api/api/routes.py`

```diff
变更点:
  1. send_message() 中注入 TaskOrchestrator 到 agent:

  + assistant = CommunityOperationsAssistant(
  +     llm=llm, model=model,
  +     tools_schema=_build_tool_schemas(),
  +     system_prompt=_SYSTEM_PROMPT,
  +     orchestrator=request.app.state.orchestrator,  # NEW
  + )

影响行数: +2
```

## 4.5 agent.py 旧函数状态（Phase 3 结束）

```
agent.py 函数                      Phase 3 状态
─────────────────────────────────────────────────────────────
_turn_intents()                    ⚠️ 保留 — L1 + SIMPLE 模式使用
_turn_routing_hint()               ⚠️ 保留 — L1 + SIMPLE 模式使用
_turn_tool_filter()                ⚠️ 保留 — SIMPLE 模式使用
顺序工具门控 (行 490-522)            ⚠️ 保留 — SIMPLE 模式使用

# PLANNED 模式不走这些旧函数，直接从 TaskOrchestrator.execute() 执行
```

## 4.6 风险

| 风险 | 等级 | 缓解 |
|------|------|------|
| TaskOrchestrator 工具调用参数错误 | 🔴 高 | 先用最简单模板（CREATE_WITH_RESEARCH 3步），验证通过再加 FULL_PIPELINE |
| Step Checkpoint 未正确持久化 | 中 | 每步执行前后各写一次 DB；恢复时检查已完成 Step |
| 模板不匹配 → LLM fallback 生成的 DAG 不合理 | 中 | DAG 校验：无环、Capability 存在、依赖指向正确 |
| 新路径与旧路径 tool_handler 行为不一致 | 中 | TaskOrchestrator 通过同一 mcp.execute_tool() 调用，工具执行逻辑完全一致 |

## 4.7 验收测试

```
测试 1: FULL_PIPELINE 模板执行
  POST {"content": "搜索Java热门帖子，分析写作方式，生成一篇新文章，校验质量，五分钟后发布"}
  → PlannerRouter.route() → PLANNED
  → select_template() → "FULL_PIPELINE"
  → 5 个 Step 按序执行:
     s1(SEARCH_COMMUNITY) → s2(ANALYZE_CONTENT) → s3(GENERATE_CONTENT)
     → s4(VALIDATE_QUALITY) → s5(SCHEDULE_PUBLISH)
  → 每个 Step status 正确流转
  → Artifact 产生: SEARCH_RESULT → ANALYSIS_REPORT → DRAFT → VALIDATION_REPORT → SCHEDULE
  → Task.status = COMPLETED

测试 2: SIMPLE 模式仍走旧路径
  POST {"content": "修改刚才文章标题为Java入门指南"}
  → PlannerRouter.route() → SIMPLE
  → 走 agent.py 现有 LLM tool calling 循环
  → tool_filter → {content_revise_draft}
  → 成功修改标题

测试 3: DIRECT 模式
  POST {"content": "Java是什么"}
  → PlannerRouter.route() → DIRECT
  → LLM 直接回答，不调用工具

测试 4: Step 重试
  (模拟 community.search_public_posts 暂时不可用)
  → Step retry_count 递增
  → retry_count < 3 → 重试
  → retry_count >= 3 → FAILED → 下游 SKIPPED

测试 5: 现有 E2E 测试全部通过
```

---

# 5. Phase 4: 收敛（2 天）

## 5.1 目标

删除 agent.py 旧代码。统一到新架构。

## 5.2 删除的代码

```python
# agent.py 中删除:
_CREATE_MARKERS          (行 25-28)   # 常量移到 understanding.py
_SCHEDULE_MARKERS        (行 29)      # 常量移到 understanding.py
_NUMBER_RE_FOR_TIME      (行 30)      # 常量移到 understanding.py
_has_future_time_expression() (行 33-48)  # 移到 understanding.py
_is_schedule_only_retry()     (行 51-74)  # 移到 understanding.py
_asks_for_community_refs()    (行 77-88)  # 移到 understanding.py
_schedule_tool_for_session()  (行 91-96)  # 移到 orchestrator.py
_turn_intents()          (行 99-114)  # L1 已由 understanding.py 覆盖
_turn_routing_hint()     (行 132-190) # L1+L2 已由 understanding.py 覆盖
_turn_tool_filter()      (行 193-224) # SIMPLE 模式不再需要工具过滤
PRODUCT_DEFAULTS         (行 117-129) # 移到 prompts/system.py
顺序工具门控 if-else      (行 490-522) # PLANNED 模式由 TaskOrchestrator 接管
                                     # SIMPLE 模式不需要工具门控
```

## 5.3 简化后的 agent.py

```python
# agent.py (~150 行)

class CommunityOperationsAssistant:
    MAX_TOOL_ROUNDS = 30

    def __init__(self, *, llm, model, tools_schema, system_prompt="",
                 max_tool_rounds=30, orchestrator=None):
        ...

    def _build_system_prompt(self, session, task=None, task_intent=None):
        """从 prompts/system.py 加载，注入 Task 上下文"""
        ...

    async def run(self, user_message, session, *,
                  tool_handler, task=None, task_intent=None,
                  conversation_history=None, ...):
        """
        三种执行路径:
        1. PLANNED → TaskOrchestrator.execute(task)
        2. SIMPLE  → _simple_loop(messages, session, tool_handler, ...)
        3. DIRECT  → _direct_answer(messages)
        """

    async def _simple_loop(self, messages, session, tool_handler, **callbacks):
        """精简的 LLM 循环: LLM → tool_handler → observation → 重复"""
        # 只保留 LLM 调用 + 工具去重 + observation 构建
        # 不保留: _turn_intents, _turn_routing_hint, _turn_tool_filter,
        #         顺序工具门控

    async def _direct_answer(self, messages):
        """LLM 直接回答"""
```

## 5.4 修改文件

```
packages/assistant_core/greenbook_assistant_core/agent.py    - ~300 行
packages/assistant_core/greenbook_assistant_core/context.py   - ~30 行 (移除 active_*_id)
packages/assistant_core/greenbook_assistant_core/__init__.py  + ~5 行 (导出新模块)
```

---

# 6. agent.py 迁移详细对照

```
┌─────────────────────────────────────────────────────────────────────────┐
│ agent.py 函数 (543 行)           → 新位置                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│ _CREATE_MARKERS (25-28)          → task/understanding.py (L1 常量)      │
│ _SCHEDULE_MARKERS (29)           → task/understanding.py (L1 常量)      │
│ _NUMBER_RE_FOR_TIME (30)         → task/understanding.py (L1 常量)      │
│ _has_future_time_expression(33)  → task/understanding.py (L1 helper)    │
│ _is_schedule_only_retry(51)      → task/understanding.py (L1 helper)    │
│ _asks_for_community_refs(77)     → task/understanding.py (L1 helper)    │
│ _schedule_tool_for_session(91)   → orchestration/orchestrator.py        │
│                                                                         │
│ _turn_intents(99)                → task/understanding.py:_quick_intent()│
│   返回 (bool×5)                     返回 TaskIntent                      │
│                                                                         │
│ _turn_routing_hint(132)          → task/understanding.py (L2 LLM 生成)  │
│   返回 "INTERNAL TURN ROUTING:..."  不再注入路由提示，由 PlannerRouter   │
│   字符串 (注入 system prompt)         决定走哪条执行路径                  │
│                                                                         │
│ _turn_tool_filter(193)           → SIMPLE 模式不再需要                   │
│   返回 {tool_name}                 PLANNED 模式由 TaskOrchestrator 决定  │
│                                    DIRECT 模式不调工具                   │
│                                                                         │
│ PRODUCT_DEFAULTS(117)            → prompts/system.py                    │
│                                                                         │
│ 顺序工具门控 (490-522):                                               │
│   search → create 分支           → "CREATE_WITH_RESEARCH" 模板          │
│   create → schedule 分支         → "CREATE_AND_PUBLISH" 模板            │
│   revise → schedule 分支         → TaskOrchestrator 动态处理            │
│                                                                         │
│ CommunityOperationsAssistant:                                           │
│   __init__ (238)                 → [保留·精简]: +orchestrator 参数      │
│   _build_system_prompt (253)     → [保留·精简]: +TaskIntent 注入        │
│   run() (273)                    → [保留·重构]: PlannerRouter 分流      │
│   LLM 循环 (326-530)             → _simple_loop() (SIMPLE 模式使用)     │
│                                                                         │
│ 结果:                                                                    │
│   agent.py: 543 行 → ~150 行                                            │
│   understanding.py: ~170 行 (新增)                                      │
│   orchestrator.py: ~250 行 (新增)                                       │
│   净增: ~30 行                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

# 7. 5 个测试场景验证矩阵

## 场景 1: 创建 Java 文章

```
输入: "帮我写一篇Java入门文章"

Phase 0-1 行为:
  L1 _turn_intents → asks_create=True
  → tool_filter → {content_create_draft}
  → LLM → content_create_draft → Creator → Java → 返回 draft_id
  → [旁路] Task 创建 + Artifact(DRAFT) 记录

Phase 2 行为:
  L1 _quick_intent → goal_category=CREATE_CONTENT, relation=NEW_TASK
  → Task.goal = "创建Java入门文章"
  → 注入 context: "意图: CREATE_CONTENT (NEW_TASK)"
  → 其余同 Phase 1

Phase 3 行为:
  PlannerRouter.route() → SIMPLE (1 requirement, 无 ANALYZE/PUBLISH)
  → 走旧路径 tool calling → 同 Phase 2

✅ 所有 Phase 保持一致的用户体验
```

## 场景 2: 修改刚才文章标题

```
前提: 场景 1 已完成，Task A 有 DRAFT artifact (draft_123)

输入: "修改刚才文章标题为Java从入门到精通"

Phase 1 行为:
  TaskRegistry.resolve_task(conv_id, hint=None)
  → 返回最近 Task (Task A)
  → [旁路] 记录 Task A 继续使用
  → agent.py 行为不变: _turn_intents → asks_revise=True
  → tool_filter → {content_revise_draft}
  → LLM → content_revise_draft(draft_id=session.active_draft_id)
  → 成功

Phase 2 行为:
  TaskUnderstanding: L1 _quick_intent
  → goal_category=IMPROVE_CONTENT, relation=MODIFY_TASK
  → TaskRegistry.resolve: 匹配 Task A (最近 + 有 DRAFT artifact)
  → task_intent 注入 agent context
  → 其余同 Phase 1

Phase 3 行为:
  PlannerRouter.route() → SIMPLE
  → 走旧路径 → 同 Phase 2

✅ 所有 Phase 都能正确找到并修改草稿
```

## 场景 3: 参考热门 Java 帖子优化文章

```
前提: 场景 1 已完成，Task A 有 DRAFT artifact (draft_123)

输入: "参考社区热门Java帖子，优化一下刚才那篇文章"

Phase 1 行为:
  _turn_intents → asks_search=True (社区 + 参考)
  → tool_filter → {community_search_public_posts}
  → LLM → search → 返回结果
  → 然后 agent.py 继续 → tool_filter 展开 → {content_revise_draft}
  → 但 community_references 被注入 create_draft/revise_draft
  → 成功

  问题: "优化" 关键词不在 _CREATE_MARKERS 中
  → _turn_intents 可能无法正确识别 revise 意图
  → 依赖 "参考" + LLM 自行判断

Phase 2 行为:
  TaskUnderstanding: _needs_l2 → True ("优化" 是模糊词)
  → L2 LLM 理解:
    goal_category=IMPROVE_CONTENT, relation=MODIFY_TASK
    target_task_hint="刚才那篇文章"
  → TaskRegistry: 匹配 Task A
  → 注入 context: "意图: IMPROVE_CONTENT (MODIFY_TASK), 目标: Task A"
  → LLM 清楚知道要修改已有草稿 + 先搜索社区参考

Phase 3 行为:
  L2 → requirements: [SEARCH, ANALYZE, IMPROVE]
  → PlannerRouter.route() → PLANNED (含 ANALYZE)
  → select_template() → "CREATE_WITH_RESEARCH" (适配: IMPROVE 替代 GENERATE)
  → TaskOrchestrator 执行:
    s1: SEARCH_COMMUNITY("Java", sort=hot)
    s2: ANALYZE_CONTENT (LLM 分析热门帖子的写作方式)
    s3: IMPROVE_CONTENT (用分析结果改进 draft_123)
  → 3 个 Step 状态完整追踪

✅ Phase 2 解决了 "优化" 无法识别的问题
✅ Phase 3 增加了结构化分析和可追踪的中间产物
```

## 场景 4: 搜索 + 分析 + 生成 + 五分钟发布

```
输入: "搜索社区Java热门帖子，分析他们为什么受欢迎，
      然后参考写法生成一篇新Java学习文章，加代码示例，五分钟后发布"

Phase 1 行为:
  _turn_intents → asks_search=True, asks_create=True, asks_schedule=True
  → tool_filter → {community_search_public_posts}
  → LLM → search → 结果
  → tool_filter 展开 → {content_create_draft}
  → LLM → create_draft("Java学习文章", references=community_references)
  → tool_filter 展开 → {publication_schedule}
  → LLM → schedule(run_at=5分钟后)

  问题:
  - "分析" 步骤被跳过（搜索结果直接作为 references 传给 Creator）
  - 没有 VALIDATE 步骤（"加代码示例" 是否满足未校验）
  - 如果 schedule 失败，前面 search+create 的中间状态不可恢复

Phase 2 行为:
  L2 LLM 理解:
    goal_category=CREATE_CONTENT, relation=NEW_TASK
    requirements: [SEARCH, ANALYZE, CREATE, PUBLISH]
    constraints: [{TIME: "5分钟后"}, {STYLE: "include_code_examples"}]

Phase 3 行为:
  PlannerRouter.route() → PLANNED
  select_template() → "FULL_PIPELINE"
  → TaskOrchestrator 执行:
    s1: SEARCH_COMMUNITY("Java", sort=hot)           → 搜索结果
    s2: ANALYZE_CONTENT (LLM 分析受欢迎原因)          → 分析报告
    s3: GENERATE_CONTENT (基于分析报告创作)            → 草稿
    s4: VALIDATE_QUALITY (LLM 校验: 标题新颖? 有代码示例?)  → 校验报告
    s5: SCHEDULE_PUBLISH (run_at=now+5min)           → 定时发布
  → 如果 s5 失败 → s1-s4 的 Step 状态已 Checkpoint，可恢复重试

✅ Phase 3 完全解决了多步任务的结构化执行和可恢复性问题
```

## 场景 5: 两个任务交替

```
轮次1: "创建一篇Java入门文章"
  → Task A (CREATE_CONTENT, status=COMPLETED, artifact: draft_java)

轮次2: "创建一篇Python入门文章"
  → Task B (CREATE_CONTENT, status=COMPLETED, artifact: draft_python)

轮次3: "修改刚才Java文章的标题为Java从零到一"

Phase 1 行为:
  问题: session.active_draft_id = draft_python（最近创建的）
  → resolve_task() 返回 Task B（最近 Task）
  → tool_handler: _bind_target_tool_args → active_draft_id = draft_python
  → 修改了 Python 文章而不是 Java 文章 ❌

Phase 2 行为:
  TaskUnderstanding:
    "修改" → relation=MODIFY_TASK
    "刚才Java文章" → target_task_hint="Java文章"
  → TaskRegistry.resolve_task(conv_id, hint="Java文章")
    匹配策略:
    1. label 子串匹配 task.goal → Task A.goal 含 "Java" → 匹配 ✓
    2. (如果 label 匹配不明确) entity 匹配 → Task A.artifacts 中 DRAFT summary 含 "Java"
  → resolved_task = Task A ✓
  → agent.run(task=Task A): mapper 从 Task A.artifacts 中取最新 DRAFT → draft_java
  → 修改正确的文章 ✅

Phase 3 行为:
  与 Phase 2 一致（SIMPLE 模式）
  → PLANNED 模式仅在多步任务触发，此处不触发

✅ Phase 2 的 TaskRegistry 匹配机制解决了多任务混淆问题
```

---

# 8. 变更汇总

| Phase | 新增文件 | 修改文件 | 删除 | 净增行数 | 风险 |
|-------|---------|---------|------|---------|------|
| 0 | 2 (db) | 2 (main, routes) | 0 | ~60 | 🟡 中 |
| 1 | 3 (task) | 2 (main, routes) | 0 | ~180 | 🟢 低 |
| 2 | 1 (understanding) | 3 (main, routes, agent) | 0 | ~210 | 🟡 中 |
| 3 | 2 (orchestration) | 3 (main, routes, agent) | 0 | ~300 | 🔴 高 |
| 4 | 0 | 3 (agent, context, __init__) | ~300 | ~-250 | 🟢 低 |
| **合计** | **8** | — | **~300** | **~500** | — |

> agent.py: 543 → ~150 行
> routes.py: 1238 → ~750 行
> 净增代码: ~500 行（比 v1 方案的 ~1,330 行减少了 62%）
