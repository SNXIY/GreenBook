# GreenBook Agent Runtime v1 — Implementation Plan

> 日期: 2026-08-07
> 状态: 设计阶段 — 不修改代码
> 前置阅读:
>   - `docs/reports/greenbook-agent-runtime-architecture-v3.md` (架构设计)
>   - `docs/reports/greenbook-agent-runtime-gap-analysis.md` (差距分析)
>
> 本文档是 v1 的详细落地方案：模块划分、数据模型、核心流程、数据库、开发顺序。

---

# 1. 新目录结构

## 1.1 packages/assistant_core 重构后

```
packages/assistant_core/greenbook_assistant_core/
├── __init__.py                    # 导出公共 API
│
├── agent.py                       # [保留·简化] Agent 主循环，精简到 ~200 行
│                                  #   职责：组装组件，驱动 Task → Plan → Execute
│                                  #   不保留：_turn_intents, _turn_routing_hint,
│                                  #          _turn_tool_filter, 顺序工具门控
│
├── context.py                     # [保留·精简] SessionContext
│                                  #   移除：active_draft_id/post_id/schedule_id
│                                  #   保留：user_id, tenant_id, timezone,
│                                  #          recent_entities, pending_approval
│                                  #   新增：active_task_id (快速引用，非主索引)
│
├── memory.py                      # [保留·实现] 从空壳变为真实 DB-backed 实现
│
├── middleware.py                  # [保留] TraceMiddleware
│
├── time_parser.py                 # [保留] 中文相对时间解析
│
├── prompts/                       # [保留] 系统提示词
│   └── system.py
│
├── task/                          # [新增] Task 子系统
│   ├── __init__.py
│   ├── models.py                  # Task, TaskIntent, TaskStatus, Artifact, ArtifactRef
│   ├── understanding.py           # TaskUnderstanding — LLM 意图理解
│   └── registry.py                # TaskRegistry — Task CRUD + 匹配 + 生命周期
│
├── planning/                      # [新增] 规划子系统
│   ├── __init__.py
│   ├── capability.py              # Capability 模型 + CapabilityRegistry
│   └── planner.py                 # Planner — TaskIntent → CapabilityDAG
│
├── execution/                     # [新增] 执行子系统
│   ├── __init__.py
│   ├── models.py                  # Step, StepStatus, ExecutionState
│   ├── engine.py                  # ExecutionEngine — DAG 拓扑执行
│   └── mapper.py                  # CapabilityToolMapper — Capability → Tool
│
└── db/                            # [新增] 持久化
    ├── __init__.py
    ├── connection.py              # PostgreSQL 连接池管理
    └── repositories.py            # TaskRepository, StepRepository, ArtifactRepository
```

## 1.2 各模块职责表

| 模块 | 职责 | 输入 | 输出 | 与现有代码关系 |
|------|------|------|------|--------------|
| `task/models.py` | 定义所有核心 Pydantic 模型 | — | Task, TaskIntent, Artifact, ArtifactRef 等类型 | 新增，替代 context.py 中的部分概念 |
| `task/understanding.py` | 将自然语言转为 TaskIntent | user_message + session + existing_tasks | TaskIntent | 替代 agent.py 中 `_turn_intents()` 等函数 |
| `task/registry.py` | Task 生命周期管理 + 匹配 | TaskIntent + conversation_id | Task CRUD 操作结果 | 新增，替代 context.py `active_*_id` |
| `planning/capability.py` | 定义 Capability 目录 | — | Capability 枚举，注册表 | 新增，作为 Tool 之上的抽象层 |
| `planning/planner.py` | TaskIntent → CapabilityDAG | Task + TaskIntent + capabilities | CapabilityDAG (PlanStep 列表) | 替代 agent.py 顺序工具门控 |
| `execution/models.py` | Step 状态模型 | — | Step, StepStatus | 新增 |
| `execution/engine.py` | 按 DAG 拓扑执行 Capability | CapabilityDAG + Task + session | 更新后的 Task (含 Step 状态和 Artifact) | 替代 routes.py tool_handler 中的调度逻辑 |
| `execution/mapper.py` | Capability → Tool 映射 | capability_name + session | (tool_name, tool_args) | 在现有 tool_handler 之上增加间接层 |
| `db/connection.py` | PostgreSQL 连接池 | — | AsyncSession | 新增，替代 app.state dict stores |
| `db/repositories.py` | CRUD 操作 | ORM models | 持久化的 Task/Step/Artifact | 新增 |
| `agent.py` | Agent 主循环 | user_message + session | RunResult | 大幅精简(544→~200行)，移除意图检测和工具门控 |

## 1.3 不修改的目录

```
services/greenbook_mcp/        # 零改动 — 16 个工具 + server + registry
packages/java_client/           # 零改动
packages/creator_client/        # 零改动
packages/contracts/             # 零改动
packages/security/              # 零改动
packages/assistant_core/time_parser.py  # 零改动
creator-agent/                  # 零改动
```

---

# 2. 核心 Model 设计

## 2.1 核心概念层级

```
Conversation  (会话 — 用户与助手的对话容器)
  ├── Message[]    (消息 — user/assistant 文本)
  ├── Task[]       (任务 — 用户的长期目标)
  │     ├── Step[]     (步骤 — 任务执行的单个动作)
  │     ├── Artifact[] (产物 — 步骤产生的数据)
  │     └── Plan       (计划 — CapabilityDAG)
  └── Run[]        (运行 — 一次 Agent 执行记录)
```

**关键区分：**

| 概念 | 定义 | 生命周期 | 例子 |
|------|------|---------|------|
| Conversation | 用户与助手的对话容器 | 创建→活跃→关闭 | "我和助手的Java讨论" |
| Turn | 用户的一次输入 | 瞬时的，包含在 Message 中 | POST /messages 的一次请求 |
| Task | 用户的长期目标 | PLANNING→READY→IN_PROGRESS→COMPLETED | "创建一篇Java文章并定时发布" |
| Artifact | Task 产生的数据 | 不可变，追加到 Task | 搜索结果、草稿、分析报告 |
| Step | Task 执行计划中的一个步骤 | PENDING→READY→IN_PROGRESS→COMPLETED | "搜索社区Java帖子" |
| Run | 一次 Agent 执行 | 创建→执行中→完成/失败 | 对应一次 POST /messages 的处理 |

**Turn 与 Task 的关系：**
- 一个 Turn 可能创建一个新 Task（NEW_TASK）
- 一个 Turn 可能继续一个已有 Task（CONTINUE_TASK）
- 一个 Turn 可能修改一个已有 Task（MODIFY_TASK）
- 一个 Task 可能跨多个 Turn 多轮对话才完成

## 2.2 TaskIntent

用户一轮输入的结构化理解。由 TaskUnderstanding 生成。

```python
class TaskIntent(BaseModel):
    """一轮用户输入的结构化理解"""

    # ── 本轮与已有 Task 的关系 ──
    relation: Literal[
        "NEW_TASK",          # 创建全新任务
        "CONTINUE_TASK",     # 继续已有任务
        "MODIFY_TASK",       # 修改已有任务的目标/参数
        "QUERY_TASK",        # 查询任务状态
        "CANCEL_TASK",       # 取消任务
        "RESUME_TASK",       # 恢复暂停的任务
        "DIRECT",            # 简单操作，不需要 Task（如"列出我的草稿"）
    ] = "NEW_TASK"

    # ── 核心目标 ──
    goal: str = ""                                    # 一句话目标
    goal_category: str = ""                           # CREATE_CONTENT | IMPROVE_CONTENT |
                                                      # ANALYZE_COMMUNITY | PUBLISH_CONTENT |
                                                      # MANAGE_SCHEDULE | INTERACT |
                                                      # QUERY_INFO | COMPOSITE

    # ── 目标任务引用 ──
    target_task_id: str | None = None                  # 明确引用的 Task ID
    target_task_hint: str | None = None                # 用户描述："刚才那篇"、"之前的Java任务"
    target_entity_refs: list[EntityHint] = []          # 引用的实体：[{kind:"DRAFT", label:"Java文章"}]

    # ── 结构化需求（有序） ──
    requirements: list[Requirement] = []               # [{type:"SEARCH", params:{...}}, ...]

    # ── 约束 ──
    constraints: list[Constraint] = []                 # [{type:"TIME", value:"5分钟后"}, ...]

    # ── 预期输出 ──
    expected_output: OutputSpec | None = None           # {format:"POST", publish:true, ...}

    # ── 置信度 ──
    confidence: float = 0.0                            # LLM 理解置信度


class EntityHint(BaseModel):
    """用户消息中对某个实体的引用"""
    kind: str                                          # DRAFT | POST | SCHEDULE | TASK | ARTIFACT
    label: str | None = None                           # 用户描述："Java文章"、"刚才那个"
    id: str | None = None                              # 如果用户明确提到了 ID

class Requirement(BaseModel):
    """一个执行需求"""
    type: str                                          # SEARCH | ANALYZE | CREATE | IMPROVE |
                                                       # VALIDATE | PUBLISH | REPLY | QUERY
    params: dict[str, Any] = {}                        # {"topic":"Java", "sort":"hot", ...}
    depends_on: list[str] = []                         # 依赖的 requirement index

class Constraint(BaseModel):
    """执行约束"""
    type: str                                          # TIME | REFERENCE | STYLE | AUDIENCE |
                                                       # LENGTH | FORMAT | PLATFORM
    value: Any

class OutputSpec(BaseModel):
    """预期输出规格"""
    format: str = "POST"                               # POST | COMMENT | REPLY | ANALYSIS
    publish: bool = False
    schedule_at: str | None = None                     # ISO-8601
```

## 2.3 Task

用户长期目标的核心模型。

```python
class TaskStatus(str, Enum):
    PLANNING = "PLANNING"                  # Planner 正在生成 CapabilityDAG
    READY = "READY"                        # 计划就绪，可以执行
    IN_PROGRESS = "IN_PROGRESS"            # Execution Engine 执行中
    WAITING_APPROVAL = "WAITING_APPROVAL"  # 等待用户审批
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"  # 等待依赖 Task 完成
    PAUSED = "PAUSED"                      # 用户暂停
    COMPLETED = "COMPLETED"                # 成功完成
    PARTIAL_COMPLETED = "PARTIAL_COMPLETED" # 部分步骤完成
    FAILED = "FAILED"                      # 执行失败
    CANCELLED = "CANCELLED"                # 用户取消

class Task(BaseModel):
    task_id: str                                        # UUID
    conversation_id: str
    user_id: str                                        # frozen from AuthContext
    tenant_id: str

    # ── 目标 ──
    goal: str                                           # 自然语言目标
    goal_category: str                                  # 任务类别
    goal_summary: str | None = None                     # 列表展示用摘要

    # ── 状态 ──
    status: TaskStatus = TaskStatus.PLANNING
    phase: str | None = None                            # 当前阶段描述

    # ── 需求与约束（从 TaskIntent 继承） ──
    requirements: list[Requirement] = []
    constraints: list[Constraint] = []

    # ── 产物 ──
    artifacts: list[ArtifactRef] = []                   # 按时间排序

    # ── 计划 ──
    plan: CapabilityDAG | None = None                   # Planner 输出

    # ── 依赖 ──
    depends_on: list[str] = []                          # 依赖的 task_id 列表（Task 间依赖）
    parent_task_id: str | None = None                   # 父 Task

    # ── 执行追踪 ──
    current_step_index: int = 0
    total_steps: int = 0
    last_error: str | None = None
    retry_count: int = 0
    max_retries: int = 3

    # ── 时间 ──
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    # ── 乐观锁 ──
    version: int = 1
```

## 2.4 Step 和 Artifact

```python
class StepStatus(str, Enum):
    PENDING = "PENDING"                    # 等待依赖步骤完成
    READY = "READY"                        # 依赖已满足，可执行
    IN_PROGRESS = "IN_PROGRESS"            # 执行中
    COMPLETED = "COMPLETED"                # 成功
    FAILED_RETRYABLE = "FAILED_RETRYABLE"  # 可重试失败
    FAILED = "FAILED"                      # 不可恢复失败
    SKIPPED = "SKIPPED"                    # 因上游失败跳过

class Step(BaseModel):
    step_id: str                                        # UUID
    task_id: str
    ordinal: int                                        # 步骤序号

    # ── Capability ──
    capability: str                                     # Capability 名称
    capability_description: str = ""                    # 步骤描述

    # ── 状态 ──
    status: StepStatus = StepStatus.PENDING

    # ── 依赖 ──
    depends_on: list[str] = []                          # 依赖的 step_id

    # ── 工具调用 ──
    tool_name: str | None = None                        # 实际调用的 MCP 工具
    tool_args: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None

    # ── 产物 ──
    artifact: ArtifactRef | None = None                 # 步骤产生的产物

    # ── 重试 ──
    retry_count: int = 0
    max_retries: int = 3

    # ── 错误 ──
    error_code: str | None = None
    error_message: str | None = None

    # ── Checkpoint ──
    checkpoint_data: dict[str, Any] | None = None       # 恢复所需状态

    # ── 时间 ──
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

class ArtifactRef(BaseModel):
    """指向一个 Artifact 的轻量引用"""
    artifact_id: str                                    # UUID
    task_id: str
    step_id: str
    artifact_type: str                                  # SEARCH_RESULT | DRAFT | ANALYSIS_REPORT |
                                                        # SCHEDULE | POST | VALIDATION_REPORT
    resource_id: str | None = None                      # 外部资源 ID (draft_id, schedule_id...)
    resource_kind: str | None = None                    # DRAFT | POST | SCHEDULE | CREATOR_ARTIFACT
    summary: str | None = None                          # 简短摘要
    created_at: datetime

class Artifact(BaseModel):
    """完整 Artifact — 包含内容引用"""
    artifact_id: str
    task_id: str
    step_id: str
    artifact_type: str
    resource_id: str | None = None
    resource_kind: str | None = None
    summary: str | None = None
    content_ref: dict[str, Any] | None = None           # JSON 摘要（不存完整内容）
    created_at: datetime
```

## 2.5 Capability 模型

```python
class Capability(BaseModel):
    """一个可执行的能力单元"""
    name: str                                           # SEARCH_COMMUNITY
    description: str                                    # "搜索 GreenBook 社区内容"
    category: str                                       # SEARCH | ANALYZE | CREATE | VALIDATE | PUBLISH

    # ── 工具映射 ──
    requires_tool: bool = True                          # 是否需要 MCP 工具
    default_tool: str | None = None                     # 默认工具名

    # ── Artifact ──
    produces_artifact: bool = False                     # 是否产生中间产物
    artifact_type: str | None = None                    # 产物类型

    # ── 执行特性 ──
    is_llm_step: bool = False                           # 是否为纯 LLM 推理步骤
    parallelizable: bool = False                        # 可否与其他步骤并行

class CapabilityDAG(BaseModel):
    """Planner 输出的执行计划"""
    task_id: str
    steps: list[PlanStep]
    generated_at: datetime

class PlanStep(BaseModel):
    """DAG 中的一个步骤"""
    step_id: str                                        # 规划时分配的 ID
    capability: str                                     # Capability 名称
    description: str                                    # 自然语言描述
    depends_on: list[str] = []                          # 依赖的 step_id
    constraints: dict[str, Any] = {}                    # 步骤级约束
```

### Capability 目录（11 个）

```python
CAPABILITY_CATALOG = {
    # ── SEARCH ──
    "SEARCH_COMMUNITY": Capability(
        name="SEARCH_COMMUNITY",
        description="搜索 GreenBook 社区内容",
        category="SEARCH",
        default_tool="community.search_public_posts",
        produces_artifact=True,
        artifact_type="SEARCH_RESULT",
    ),
    "GET_POST_DETAIL": Capability(
        name="GET_POST_DETAIL",
        description="获取单个帖子详情",
        category="SEARCH",
        default_tool="community.get_post",
    ),

    # ── ANALYZE ──
    "ANALYZE_CONTENT_PATTERNS": Capability(
        name="ANALYZE_CONTENT_PATTERNS",
        description="分析已获取内容的模式、风格、特点",
        category="ANALYZE",
        requires_tool=False,
        is_llm_step=True,
        produces_artifact=True,
        artifact_type="ANALYSIS_REPORT",
    ),
    "ANALYZE_PERFORMANCE": Capability(
        name="ANALYZE_PERFORMANCE",
        description="分析帖子/账号的互动数据",
        category="ANALYZE",
        default_tool="analytics.get_post_performance",
    ),

    # ── CREATE ──
    "GENERATE_CONTENT": Capability(
        name="GENERATE_CONTENT",
        description="基于指令和参考资料生成内容",
        category="CREATE",
        default_tool="content.create_draft",
        produces_artifact=True,
        artifact_type="DRAFT",
    ),
    "IMPROVE_CONTENT": Capability(
        name="IMPROVE_CONTENT",
        description="改进已有内容",
        category="CREATE",
        default_tool="content.revise_draft",
        produces_artifact=True,
        artifact_type="DRAFT",
    ),

    # ── VALIDATE ──
    "VALIDATE_QUALITY": Capability(
        name="VALIDATE_QUALITY",
        description="校验内容质量（格式、完整性、约束满足度）",
        category="VALIDATE",
        requires_tool=False,
        is_llm_step=True,
        produces_artifact=True,
        artifact_type="VALIDATION_REPORT",
    ),

    # ── PUBLISH ──
    "SCHEDULE_PUBLISH": Capability(
        name="SCHEDULE_PUBLISH",
        description="定时发布内容",
        category="PUBLISH",
        default_tool="publication.schedule",
        produces_artifact=True,
        artifact_type="SCHEDULE",
    ),
    "PUBLISH_NOW": Capability(
        name="PUBLISH_NOW",
        description="立即发布内容（需用户确认）",
        category="PUBLISH",
        default_tool="publication.publish_now",
    ),
    "MANAGE_SCHEDULE": Capability(
        name="MANAGE_SCHEDULE",
        description="管理定时任务（修改时间、取消）",
        category="PUBLISH",
        default_tool="publication.update_schedule",
    ),

    # ── INTERACT ──
    "REPLY_USER": Capability(
        name="REPLY_USER",
        description="回复用户评论",
        category="INTERACT",
        default_tool="interaction.send_reply",
    ),
}
```

---

# 3. 核心流程图

## 3.1 主流程

```
POST /api/v1/assistant/conversations/{id}/messages
    │
    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ routes.py: send_message()                                           │
│                                                                     │
│  1. Auth → AuthContext                                              │
│  2. Session → SessionContext                                        │
│  3. History → messages[]                                            │
│  4. NEW: TaskIntent = await task_understanding.understand(...)      │
│  5. NEW: Task = await task_registry.resolve_or_create(TaskIntent)   │
│  6. agent.run(user_message, session, Task)                          │
└─────────────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ agent.py: CommunityOperationsAssistant.run()                        │
│                                                                     │
│  if Task.status == PLANNING:                                        │
│      Task.plan = await planner.plan(Task, capabilities)             │
│      Task.status = READY                                            │
│                                                                     │
│  if Task.plan is not None:                                          │
│      # 有 Plan → 走 Execution Engine                                │
│      Task = await execution_engine.execute(Task)                    │
│  else:                                                              │
│      # 无 Plan (简单单步) → 走旧路径（直接 LLM tool calling）       │
│      result = await self._simple_loop(user_message, session, Task)  │
│                                                                     │
│  return RunResult(content, tool_rounds, task_id)                    │
└─────────────────────────────────────────────────────────────────────┘
```

## 3.2 Execution Engine 详细流程

```
execution_engine.execute(Task)
    │
    ├── 1. 初始化 Steps（从 Task.plan 构建）
    │      for plan_step in Task.plan.steps:
    │          Step(step_id, capability, depends_on, status=PENDING)
    │
    ├── 2. 标记 READY（依赖全部满足的 Step）
    │      for step in steps:
    │          if all(dep in completed_step_ids for dep in step.depends_on):
    │              step.status = READY
    │
    ├── 3. 执行循环
    │      while not all_complete(steps):
    │          ready = [s for s in steps if s.status == READY]
    │          parallel = [s for s in ready if s.capability.parallelizable]
    │          sequential = [s for s in ready if not s.capability.parallelizable]
    │
    │          # 并行执行
    │          if parallel:
    │              await gather(*[_execute_step(s) for s in parallel])
    │
    │          # 串行执行
    │          for s in sequential:
    │              await _execute_step(s)
    │              # 串行执行时，每完成一步检查是否有新 READY
    │              _update_ready_status(steps)
    │
    ├── 4. _execute_step(step)
    │      step.status = IN_PROGRESS
    │      await save_step(step)     # ── Checkpoint 1: 开始执行
    │
    │      capability = get_capability(step.capability)
    │
    │      if capability.is_llm_step:
    │          # 纯 LLM 推理（如 ANALYZE_PATTERNS）
    │          result = await _llm_reasoning(step, Task)
    │      else:
    │          # MCP 工具调用
    │          tool_name = capability.default_tool
    │          tool_args = mapper.build_args(capability, step, Task)
    │          result = await mcp.execute_tool(tool_name, ...)
    │
    │      if result.ok:
    │          step.status = COMPLETED
    │          step.artifact = _extract_artifact(step, result)
    │          Task.artifacts.append(step.artifact)
    │      else if result.retryable and step.retry_count < step.max_retries:
    │          step.status = FAILED_RETRYABLE
    │          step.retry_count += 1
    │      else:
    │          step.status = FAILED
    │          # 标记下游 Step 为 SKIPPED
    │
    │      await save_step(step)     # ── Checkpoint 2: 完成/失败
    │
    └── 5. 返回更新后的 Task
```

## 3.3 Capability → Tool 映射流程

```
Execution Engine: 需要执行 SEARCH_COMMUNITY
    │
    ▼
CapabilityToolMapper.map("SEARCH_COMMUNITY", context)
    │
    ├── 1. 查找 Capability: capability.default_tool = "community.search_public_posts"
    │
    ├── 2. 构建 tool_args:
    │      - 从 Task.requirements 中提取 params
    │      - 从上游 Artifact 中注入引用
    │      - 从 Task.constraints 中提取约束
    │
    ├── 3. 特殊情况处理:
    │      SCHEDULE_PUBLISH:
    │          if Task has existing schedule → "publication.update_schedule"
    │          else → "publication.schedule"
    │      IMPROVE_CONTENT:
    │          注入 current_draft_id → Task.artifacts 中的最新 DRAFT
    │
    └── 4. 返回 (tool_name, tool_args)
```

---

# 4. Task Registry 设计

## 4.1 核心问题

用户说 "刚才那个文章"、"之前的Java任务"、"把搜索结果加入刚才的任务"。

**当前方案：** `session.active_draft_id`（只能存一个，模糊匹配靠时间排序）

**新方案：** TaskRegistry 维护 Conversation 内所有 Task，支持多维匹配。

## 4.2 Task 匹配机制

```python
class TaskRegistry:
    """管理一个 Conversation 内的所有 Task"""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ── CRUD ──

    async def create_task(self, intent: TaskIntent, conversation_id: str,
                          user_id: str, tenant_id: str) -> Task:
        """从 TaskIntent 创建新 Task"""

    async def get_task(self, task_id: str) -> Task | None:
        """按 ID 获取"""

    async def list_tasks(self, conversation_id: str,
                         status: TaskStatus | None = None) -> list[Task]:
        """列出会话的所有 Task"""

    # ── 匹配 ──

    async def resolve_task(
        self,
        conversation_id: str,
        intent: TaskIntent,
    ) -> Task | None:
        """根据 TaskIntent 找到用户引用的 Task。

        匹配策略（按优先级）：

        1. 精确 ID 匹配
           if intent.target_task_id:
               return await self.get_task(intent.target_task_id)

        2. 标签匹配（用户说"Java文章那个"）
           if intent.target_task_hint:
               tasks = await self.list_tasks(conversation_id)
               match = self._match_by_label(tasks, intent.target_task_hint)
               if match:
                   return match

        3. 实体引用匹配（用户说"刚才那个草稿"）
           if intent.target_entity_refs:
               tasks = await self.list_tasks(conversation_id)
               match = self._match_by_entity(tasks, intent.target_entity_refs)
               if match:
                   return match

        4. 最近活跃 Task（"刚才的" = 最近创建或修改的）
           if intent.relation in ("CONTINUE_TASK", "MODIFY_TASK"):
               return await self._get_most_recent_active(conversation_id)

        5. 语义匹配
           if intent.goal:
               tasks = await self.list_tasks(conversation_id)
               return await self._match_by_semantic(tasks, intent.goal)

        return None
        """

    def _match_by_label(self, tasks: list[Task], hint: str) -> Task | None:
        """
        匹配规则：
        - "刚才"/"最近"/"上次" 的 → 按 updated_at 排序取最新
        - 包含关键词的 → goal 中包含相同关键词的 Task
        - "第一个"/"第二个" 的 → 按 created_at 排序取序号
        """

    def _match_by_entity(self, tasks: list[Task], refs: list[EntityHint]) -> Task | None:
        """
        遍历所有 Task 的 artifacts，找到包含匹配实体引用的 Task
        例：refs=[{kind:"DRAFT", label:"Java文章"}] →
            找到 artifacts 中包含 DRAFT 且 summary 匹配 "Java" 的 Task
        """

    async def _match_by_semantic(self, tasks: list[Task], goal: str) -> Task | None:
        """
        用 LLM 做语义匹配（仅在 label/hint 匹配失败时使用）
        Prompt: "以下 Task 列表中，哪个最匹配用户意图 '{goal}'？"
        返回最匹配的 task_id 或 None（置信度 < 阈值时）
        """

    async def _get_most_recent_active(self, conversation_id: str) -> Task | None:
        """获取最近更新且未完成的 Task"""
        tasks = await self.list_tasks(conversation_id)
        active = [t for t in tasks if t.status in (
            TaskStatus.READY, TaskStatus.IN_PROGRESS,
            TaskStatus.WAITING_APPROVAL, TaskStatus.PAUSED,
            TaskStatus.COMPLETED,  # 最近完成的也可能是"刚才的"
        )]
        return max(active, key=lambda t: t.updated_at) if active else None

    # ── 关系判断 ──

    async def resolve_relation(
        self,
        conversation_id: str,
        intent: TaskIntent,
    ) -> tuple[str, Task | None]:
        """决定本轮 Turn 与已有 Task 的关系。

        返回: (relation, resolved_task)

        决策逻辑：
        1. intent.relation 如果 LLM 已经明确给出了关系 → 直接用
        2. 如果 resolve_task 找到了匹配 → CONTINUE_TASK 或 MODIFY_TASK
           - goal_category 相同 → CONTINUE_TASK
           - goal_category 不同 → MODIFY_TASK
        3. 如果没找到匹配的 Task → NEW_TASK

        特殊规则：
        - "取消" + 找到了 schedule → CANCEL_TASK
        - "查看" / "怎么样了" + 找到了 Task → QUERY_TASK
        """
```

## 4.3 多任务冲突解决

```python
# 场景：Conversation 中有 3 个活跃 Task
# Task A: "创建Java文章" (COMPLETED, 5 分钟前)
# Task B: "分析Python热门内容" (IN_PROGRESS, 1 分钟前)
# Task C: "回复用户的评论" (READY, 30 分钟前)

# 用户说 "刚才那个任务"
# → _match_by_label: hint="刚才" → 按 updated_at 排序
# → Task B 最新 → 匹配 Task B ✓

# 用户说 "修改标题"
# → _match_by_entity: refs 为空
# → _get_most_recent_active → Task B
# → 但 Task B 是 ANALYZE，不支持"修改标题"
# → 向前查找 → Task A 是 CREATE_CONTENT，且 artifacts 中有 DRAFT
# → 匹配 Task A ✓

# 用户说 "把分析结果加入Java文章"
# → _match_by_label: hint 含 "Java" → Task A
# → 同时识别 "分析结果" → 查找所有 Task 的 artifacts
# → Task B 有 ANALYSIS_REPORT artifact
# → 建立依赖：Task A depends_on Task B
# → 注入 Task B 的 artifact 到 Task A 的下一步执行
```

---

# 5. Task Understanding 设计

## 5.1 LLM 驱动的意图理解

```python
class TaskUnderstanding:
    """双层意图理解：快速路径 + LLM 深度路径"""

    def __init__(self, llm, model: str):
        self.llm = llm
        self.model = model

    async def understand(
        self,
        user_message: str,
        session: SessionContext,
        existing_tasks: list[Task] | None = None,
    ) -> TaskIntent:
        """
        主入口：将自然语言转为 TaskIntent。

        双层策略：
        L1 — 确定性快速路径（< 1ms，不走 LLM）
        L2 — LLM 深度路径（~500ms，一次 API 调用）
        """

        # L1: 快速路径
        quick = self._quick_path(user_message, session, existing_tasks)
        if quick is not None:
            return quick

        # L2: LLM 深度路径
        return await self._llm_path(user_message, session, existing_tasks)
```

## 5.2 L1 — 快速路径（确定性）

保留当前 `_turn_intents()` 中最可靠的部分，但输出格式改为 `TaskIntent`：

```python
def _quick_path(self, user_message, session, existing_tasks) -> TaskIntent | None:
    """
    以下情况走快速路径（不需要 LLM）：
    1. 明确的简单查询（"列出我的草稿"、"查看定时任务"）
    2. 明确的取消操作（"取消定时发布"）
    3. 明确的单步创建（不含"参考"、"分析"、"然后" 等复合信号）

    快速路径不处理：
    - 复合意图（"搜索 + 分析 + 创建"）
    - 模糊引用（"优化一下"、"提升质量"）
    - 跨任务引用（"把A的结果用到B"）
    """

    # 如果检测到复合信号 → 返回 None，升级到 L2
    if self._has_composite_signal(user_message):
        return None

    # 如果检测到模糊意图 → 返回 None，升级到 L2
    if self._has_ambiguous_intent(user_message):
        return None

    # 明确单步操作 → 构建确定性 TaskIntent
    ...
```

## 5.3 L2 — LLM 深度路径

```python
async def _llm_path(self, user_message, session, existing_tasks) -> TaskIntent:
    """
    用 LLM 将自然语言转为结构化 TaskIntent。

    关键设计:
    - 使用严格的 JSON Schema 约束输出
    - few-shot examples 覆盖常见社区场景
    - 上下文包含已有 Task 列表（用于匹配）
    """

    prompt = f"""你是 GreenBook 社区助手的意图理解模块。

## 用户消息
{user_message}

## 会话上下文
- 当前时区: {session.timezone}
- 已有任务:
{self._format_existing_tasks(existing_tasks)}

## 任务类别
- CREATE_CONTENT: 创建新内容（帖子、文章）
- IMPROVE_CONTENT: 改进已有内容（修改、优化、润色）
- ANALYZE_COMMUNITY: 分析社区内容、趋势
- PUBLISH_CONTENT: 发布内容
- MANAGE_SCHEDULE: 管理定时发布
- INTERACT: 回复评论
- QUERY_INFO: 查询信息
- COMPOSITE: 包含多个步骤的复合任务

## 关系类型
- NEW_TASK: 全新目标
- CONTINUE_TASK: 继续已有任务
- MODIFY_TASK: 修改已有任务
- CANCEL_TASK: 取消任务
- QUERY_TASK: 查询任务状态
- DIRECT: 简单操作，无需创建任务

## 输出格式（严格遵守 JSON Schema）
{json.dumps(TASK_INTENT_SCHEMA, indent=2)}

## 示例
[3-5 个 few-shot examples，覆盖常见场景]
"""

    response = await self.llm.chat.completions.create(
        model=self.model,
        messages=[{"role": "system", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )

    # Pydantic 校验 + 修复
    try:
        return TaskIntent.model_validate_json(response.choices[0].message.content)
    except ValidationError:
        # 回退到快速路径（安全网）
        return self._fallback_intent(user_message, session)
```

## 5.4 如何减少 LLM 调用

| 策略 | 说明 |
|------|------|
| L1 快速路径 | 约 60% 的用户请求是简单单步操作，不需要 LLM |
| TaskIntent 缓存 | 同一 Turn 内不重复调用（目前一个 Turn 只调用一次） |
| 会话级缓存 | 如果用户连续发送相似消息（如追问），可以复用上次的 TaskIntent 骨架 |
| 降级机制 | L1 失败 → L2；L2 失败 → fallback（保守的关键词检测） |

---

# 6. Planner 设计

## 6.1 Planner 输入/输出

```python
class Planner:
    """将 Task + TaskIntent 转为 CapabilityDAG"""

    def __init__(self, llm, model: str):
        self.llm = llm
        self.model = model

    async def plan(
        self,
        task: Task,
        available_capabilities: list[Capability],
    ) -> CapabilityDAG | None:
        """
        返回 None 表示任务太简单，不需要 Plan（走旧路径）。

        需要 Planner 的场景：
        - 3 个以上 Requirement
        - 有跨 Task 依赖
        - 有 ANALYZE 步骤（需要中间产物）
        - goal_category == COMPOSITE
        """

        # 简单任务跳过
        if self._is_simple(task):
            return None

        # LLM 生成 DAG
        prompt = self._build_plan_prompt(task, available_capabilities)
        result = await self._call_llm(prompt)
        dag = self._parse_dag(result)

        # 校验
        validated = self._validate_dag(dag, available_capabilities)
        return validated
```

## 6.2 Planner Prompt 设计

```
你是 GreenBook 社区运营任务的规划器。

## 任务目标
{task.goal}

## 任务需求（有序）
{task.requirements}

## 任务约束
{task.constraints}

## 可用能力
{format_capabilities(available_capabilities)}
# 每个 Capability 只列出 name + description，不暴露 tool 名

## 已有产物（可从上游 Task 获取）
{format_available_artifacts(task)}

## 规划规则
1. SEARCH 类必须在 ANALYZE 类之前
2. ANALYZE 类必须在 CREATE 类之前
3. CREATE 类必须在 VALIDATE 类之前
4. VALIDATE 类必须在 PUBLISH 类之前
5. 没有依赖的步骤标记 parallelizable=true
6. 纯查询步骤不需要 VALIDATE
7. 如果用户没有明确要求发布，不要加 PUBLISH 步骤
8. 每个步骤必须明确 consumes（需要的输入 Artifact）
9. 步骤数量 2-8 个（1 个的话不需要 Planner）

## 输出格式
{
  "steps": [
    {
      "step_id": "step-1",
      "capability": "SEARCH_COMMUNITY",
      "description": "搜索社区Java热门帖子",
      "depends_on": [],
      "constraints": {"topic": "Java", "sort": "hot"}
    },
    ...
  ]
}
```

## 6.3 Plan 校验机制

```python
def _validate_dag(self, dag: CapabilityDAG, capabilities: list[Capability]) -> CapabilityDAG:
    """校验 Planner 输出的合法性"""

    capability_names = {c.name for c in capabilities}

    for step in dag.steps:
        # 1. Capability 存在性
        if step.capability not in capability_names:
            raise PlanValidationError(f"未知能力: {step.capability}")

        # 2. 依赖存在性
        for dep in step.depends_on:
            if dep not in {s.step_id for s in dag.steps}:
                raise PlanValidationError(f"步骤 {step.step_id} 依赖不存在的步骤 {dep}")

        # 3. 无环检查
        if self._has_cycle(dag):
            raise PlanValidationError("Plan 包含循环依赖")

        # 4. 顺序约束检查
        self._check_ordering_constraints(dag)  # SEARCH 必须在 ANALYZE 之前等

        # 5. Capability 使用频率（防止 Planner 滥用）
        search_steps = [s for s in dag.steps if s.capability.startswith("SEARCH")]
        if len(search_steps) > 3:
            raise PlanValidationError("搜索步骤过多")

    return dag
```

---

# 7. Execution Engine 设计

## 7.1 Step 状态机

```
PENDING ──────► READY ──────► IN_PROGRESS ──────► COMPLETED
   ▲               ▲                │                  │
   │               │                ├──► FAILED_RETRYABLE ──► READY (重试)
   │               │                │
   │               │                └──► FAILED ──► 下游 SKIPPED
   │               │
   └── RESUME 时恢复 ────────────────┘
```

## 7.2 核心执行循环

```python
class ExecutionEngine:
    def __init__(self, mcp: GreenBookMCPServer, llm, mapper: CapabilityToolMapper,
                 db: AsyncSession):
        self.mcp = mcp          # 现有 MCP Server（无改动）
        self.llm = llm
        self.mapper = mapper
        self.db = db

    async def execute(self, task: Task) -> Task:
        """执行一个 Task 的 CapabilityDAG"""

        dag = task.plan
        # 1. 初始化 Steps
        steps = self._initialize_steps(task, dag)

        # 2. 执行循环
        while not self._is_terminal(steps):
            # 更新 READY 状态
            completed_ids = {s.step_id for s in steps
                           if s.status == StepStatus.COMPLETED}
            for step in steps:
                if step.status == StepStatus.PENDING:
                    if all(d in completed_ids for d in step.depends_on):
                        step.status = StepStatus.READY
                elif step.status == StepStatus.FAILED_RETRYABLE:
                    step.status = StepStatus.READY  # 重试

            ready = [s for s in steps if s.status == StepStatus.READY]
            if not ready:
                break  # 没有可执行的步骤

            # 3. 并行执行
            parallel = [s for s in ready if self._is_parallelizable(s)]
            if parallel:
                results = await asyncio.gather(
                    *[self._execute_step(s, task) for s in parallel],
                    return_exceptions=True,
                )

            # 4. 串行执行
            sequential = [s for s in ready if not self._is_parallelizable(s)]
            for s in sequential:
                await self._execute_step(s, task)
                # 每完成一步，更新 task 状态
                self._update_task_progress(task, steps)

        # 5. 更新 Task 状态
        task = self._finalize_task(task, steps)
        return task
```

## 7.3 单步执行

```python
async def _execute_step(self, step: Step, task: Task) -> Step:
    # ── Checkpoint: 开始执行 ──
    step.status = StepStatus.IN_PROGRESS
    step.started_at = datetime.now(UTC)
    await self._save_step(step)

    try:
        capability = CAPABILITY_CATALOG[step.capability]

        if capability.is_llm_step:
            # 纯 LLM 推理步骤
            result = await self._execute_llm_step(step, task)
        else:
            # MCP 工具步骤
            tool_name, tool_args = self.mapper.build_tool_call(
                capability, step, task
            )
            step.tool_name = tool_name
            step.tool_args = tool_args

            result = await self.mcp.execute_tool(
                tool_name,
                auth=task.auth,          # 从 Task 获取
                session=task.session,    # 从 Task 获取
                **tool_args,
            )

        if result.get("ok"):
            step.status = StepStatus.COMPLETED
            # 提取 Artifact
            step.artifact = self._extract_artifact(step, result)
            if step.artifact:
                task.artifacts.append(step.artifact)
        else:
            if result.get("retryable") and step.retry_count < step.max_retries:
                step.status = StepStatus.FAILED_RETRYABLE
                step.retry_count += 1
            else:
                step.status = StepStatus.FAILED
                step.error_code = result.get("code")
                step.error_message = result.get("user_message")
                # 标记下游为 SKIPPED
                self._skip_downstream(step, steps)

    except Exception as e:
        if step.retry_count < step.max_retries:
            step.status = StepStatus.FAILED_RETRYABLE
            step.retry_count += 1
        else:
            step.status = StepStatus.FAILED
        step.error_message = str(e)

    # ── Checkpoint: 完成/失败 ──
    step.completed_at = datetime.now(UTC)
    await self._save_step(step)

    return step
```

## 7.4 Checkpoint 与 Resume

```python
async def resume(self, task_id: str) -> Task:
    """从 Checkpoint 恢复 Task 执行"""

    task = await self.db.get_task(task_id)
    steps = await self.db.get_steps(task_id)

    # 跳过已完成的步骤
    # 恢复 FAILED_RETRYABLE → READY
    # 恢复 IN_PROGRESS（崩溃）→ READY（依赖幂等重放保证安全）
    for step in steps:
        if step.status == StepStatus.COMPLETED:
            continue
        if step.status in (StepStatus.FAILED_RETRYABLE, StepStatus.IN_PROGRESS):
            step.status = StepStatus.PENDING  # 重置，让主循环重新调度

    task.status = TaskStatus.IN_PROGRESS
    return await self.execute(task)  # 从当前状态继续
```

## 7.5 人工介入

```python
# 审批流集成
if capability.name == "PUBLISH_NOW":
    # 规则引擎检查（保留现有 security/policy.py）
    if requires_approval(tool_name):
        # 创建 PendingApproval
        approval = PendingApproval(
            approval_id=str(uuid4()),
            operation=tool_name,
            resource_id=step.artifact.resource_id,
            description=f"确认发布 {step.artifact.summary}",
        )
        task.status = TaskStatus.WAITING_APPROVAL
        await self._save_task(task)
        # 暂停执行，等待用户决策
        return task
```

## 7.6 如何调用现有 MCP

```python
# CapabilityToolMapper 是 Execution Engine 与 MCP 之间的桥梁
class CapabilityToolMapper:
    """将 Capability 映射为具体的 MCP 工具调用"""

    def build_tool_call(
        self,
        capability: Capability,
        step: Step,
        task: Task,
    ) -> tuple[str, dict[str, Any]]:
        """返回 (tool_name, tool_args)"""

        tool_name = capability.default_tool

        # 从 Task 的 requirement 和上游 Artifact 构建参数
        args = {}
        args.update(step.constraints)  # 步骤级约束

        # 注入上游 Artifact
        for dep_id in step.depends_on:
            dep_step = self._get_step(dep_id)
            if dep_step.artifact:
                self._inject_artifact(args, dep_step.artifact, capability)

        # 特殊情况
        if capability.name == "SCHEDULE_PUBLISH":
            existing_schedule = self._find_artifact(task, "SCHEDULE")
            if existing_schedule:
                tool_name = "publication.update_schedule"
                args["schedule_id"] = existing_schedule.resource_id

        if capability.name == "IMPROVE_CONTENT":
            draft = self._find_artifact(task, "DRAFT")
            if draft:
                args["draft_id"] = draft.resource_id

        return tool_name, args
```

---

# 8. 数据库设计

## 8.1 表结构

```sql
-- 会话表（从内存迁移到 DB）
CREATE TABLE assistant_conversations (
    conversation_id UUID PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    title VARCHAR(120),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_conv_user ON assistant_conversations(user_id, tenant_id);

-- 消息表（从内存迁移到 DB）
CREATE TABLE assistant_messages (
    message_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES assistant_conversations(conversation_id),
    role VARCHAR(16) NOT NULL,           -- user | assistant
    content TEXT NOT NULL,
    trace_id VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_msg_conv ON assistant_messages(conversation_id, created_at);

-- Run 表（从内存迁移到 DB）
CREATE TABLE assistant_runs (
    run_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES assistant_conversations(conversation_id),
    task_id UUID,                        -- 关联 Task（可为空）
    user_id VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,         -- IN_PROGRESS | COMPLETED | FAILED | ...
    content TEXT,
    error_code VARCHAR(64),
    error_message TEXT,
    tool_rounds INT DEFAULT 0,
    trace_id VARCHAR(64),
    events JSONB DEFAULT '[]',           -- SSE 事件列表
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_run_conv ON assistant_runs(conversation_id);

-- ── 新增表 ──

-- Task 表
CREATE TABLE assistant_tasks (
    task_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES assistant_conversations(conversation_id),
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,

    -- 目标
    goal TEXT NOT NULL,
    goal_category VARCHAR(64) NOT NULL,
    goal_summary VARCHAR(500),

    -- 状态
    status VARCHAR(32) NOT NULL DEFAULT 'PLANNING',
    phase VARCHAR(64),

    -- 结构化数据
    requirements JSONB DEFAULT '[]',
    constraints JSONB DEFAULT '[]',

    -- 依赖
    depends_on UUID[] DEFAULT '{}',
    parent_task_id UUID,

    -- 执行追踪
    current_step_index INT DEFAULT 0,
    total_steps INT DEFAULT 0,
    last_error TEXT,
    retry_count INT DEFAULT 0,
    max_retries INT DEFAULT 3,

    -- 乐观锁
    version INT DEFAULT 1,

    -- 时间
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX idx_task_conv ON assistant_tasks(conversation_id, status);
CREATE INDEX idx_task_user ON assistant_tasks(user_id, tenant_id);

-- Step 表
CREATE TABLE assistant_task_steps (
    step_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES assistant_tasks(task_id) ON DELETE CASCADE,
    ordinal INT NOT NULL,

    -- Capability
    capability VARCHAR(64) NOT NULL,
    capability_description TEXT,

    -- 状态
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',

    -- 依赖
    depends_on UUID[] DEFAULT '{}',

    -- 工具调用
    tool_name VARCHAR(128),
    tool_args JSONB,
    tool_result JSONB,

    -- 错误
    error_code VARCHAR(64),
    error_message TEXT,

    -- 重试
    retry_count INT DEFAULT 0,
    max_retries INT DEFAULT 3,

    -- Checkpoint
    checkpoint_data JSONB,

    -- 时间
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);
CREATE INDEX idx_step_task ON assistant_task_steps(task_id, ordinal);

-- Artifact 表
CREATE TABLE assistant_artifacts (
    artifact_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES assistant_tasks(task_id) ON DELETE CASCADE,
    step_id UUID NOT NULL REFERENCES assistant_task_steps(step_id) ON DELETE CASCADE,

    -- 类型
    artifact_type VARCHAR(64) NOT NULL,      -- SEARCH_RESULT | DRAFT | ANALYSIS_REPORT | ...

    -- 外部资源
    resource_id VARCHAR(128),               -- 外部 ID (draft_id, schedule_id)
    resource_kind VARCHAR(32),              -- DRAFT | POST | SCHEDULE

    -- 摘要
    summary VARCHAR(500),
    content_ref JSONB,                      -- JSON 摘要，不存完整内容

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_artifact_task ON assistant_artifacts(task_id);

-- 审批表（从内存迁移到 DB）
CREATE TABLE assistant_approvals (
    approval_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES assistant_conversations(conversation_id),
    run_id UUID NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    operation VARCHAR(128) NOT NULL,
    resource_id VARCHAR(128),
    description TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | REJECTED
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);
```

## 8.2 迁移说明

- `assistant_conversations`, `assistant_messages`, `assistant_runs`, `assistant_approvals` 是从当前内存 dict 迁移到 PostgreSQL
- `assistant_tasks`, `assistant_task_steps`, `assistant_artifacts` 是新增表
- 使用 `ON DELETE CASCADE` 保证 Task 删除时关联数据一起清理
- JSONB 列存储灵活的 Requirement、Constraint、Artifact 等数据
- 索引覆盖最常见的查询模式

---

# 9. routes.py 和 agent.py 如何拆分

## 9.1 当前问题

| 文件 | 行数 | 问题 |
|------|------|------|
| `routes.py` | ~1240 | 同时包含：路由定义、会话管理、工具 Schema 构建、工具调度、审批流、SSE 事件、错误映射、时间标准化 |
| `agent.py` | ~544 | 同时包含：LLM 循环、意图检测、路由提示、工具过滤、顺序门控、上下文构建 |

## 9.2 拆分后

### routes.py（精简到 ~300 行）

```python
# routes.py — 只负责 HTTP 层

@router.post("/conversations/{conversation_id}/messages")
async def send_message(conversation_id, body, request):
    # 1. Auth（不变）
    auth = _get_auth(request)
    session = _get_session(request, conversation_id)

    # 2. History（迁移到 DB）
    history = await memory.load_history(conversation_id)
    await memory.save_message(conversation_id, "user", body.content)

    # 3. Task Understanding（NEW — 调用新模块）
    task_intent = await task_understanding.understand(
        body.content, session, existing_tasks)

    # 4. Task Registry（NEW — 调用新模块）
    task = await task_registry.resolve_or_create(
        task_intent, conversation_id, auth)

    # 5. Agent 执行（精简后的 agent.run）
    result = await agent.run(
        user_message=body.content,
        session=session,
        task=task,                    # NEW: 注入 Task
        tool_handler=tool_handler,    # 保留: MCP 调度
        conversation_history=history,
        on_tool_start=on_tool_start,  # SSE 事件
        on_tool_complete=on_tool_complete,
        on_assistant_delta=on_assistant_delta,
    )

    # 6. 持久化结果 + 返回响应（迁移到 DB）
    await memory.save_message(conversation_id, "assistant", result.content)
    await _save_run(run_id, result)
    return RunAcceptedResponse(...)
```

### agent.py（精简到 ~200 行）

```python
# agent.py — 只负责组装和驱动

class CommunityOperationsAssistant:
    MAX_TOOL_ROUNDS = 30

    def __init__(self, llm, model, mcp, memory, planner, execution_engine):
        self.llm = llm
        self.model = model
        self.mcp = mcp
        self.memory = memory
        self.planner = planner
        self.execution_engine = execution_engine

    async def run(self, user_message, session, task, **callbacks) -> RunResult:
        """
        两种执行路径：
        1. 有 Plan → Execution Engine
        2. 无 Plan → 直接 LLM Tool Calling（旧路径，简单任务）
        """

        # Path A: 有 Plan
        if task.plan is not None:
            return await self._planned_execution(task)

        # Path B: 无 Plan — 简单 LLM 循环（保留当前逻辑但大幅简化）
        messages = self._build_messages(user_message, session, task)
        return await self._simple_loop(messages, session, task, **callbacks)

    async def _planned_execution(self, task: Task) -> RunResult:
        """Path A: 通过 Execution Engine 执行 CapabilityDAG"""
        task = await self.execution_engine.execute(task)
        final = self._render_final_response(task)
        return RunResult(content=final, task_id=task.task_id, ...)

    async def _simple_loop(self, messages, session, task, **callbacks) -> RunResult:
        """Path B: 直接 LLM + Tool Calling（保留旧路径，Fallback）"""
        # 简化版：不再有 _turn_intents, _turn_routing_hint, _turn_tool_filter
        # 只保留 LLM 调用循环 + tool_handler 回调
        ...
```

## 9.3 逐步迁移

Phase 4 之前：
- `routes.py` 保留完整旧逻辑
- `agent.py` 保留 `_turn_intents` 等函数
- 新模块（task_understanding, registry, planner, execution_engine）作为**旁路**存在
- 通过 feature flag 或 task.plan 是否为 None 决定走新旧路径

Phase 5（收敛）：
- 删除 `agent.py` 中的 `_turn_intents`, `_turn_routing_hint`, `_turn_tool_filter`
- 删除顺序工具门控的 if-else 块
- 简化 `routes.py`，将会话/审批/消息管理移到独立模块或 DB repository

---

# 10. 分阶段开发计划

## Phase 0: 基础设施（1 周）

### 目标
建立 DB 持久化和测试基础设施，零功能变更。

### 新增文件
```
packages/assistant_core/greenbook_assistant_core/db/__init__.py
packages/assistant_core/greenbook_assistant_core/db/connection.py
packages/assistant_core/greenbook_assistant_core/db/repositories.py
```

### 数据库迁移
```sql
-- 从内存迁移到 PostgreSQL
CREATE TABLE assistant_conversations (...);
CREATE TABLE assistant_messages (...);
CREATE TABLE assistant_runs (...);
CREATE TABLE assistant_approvals (...);
```

### 修改文件
```
apps/assistant_api/greenbook_assistant_api/main.py
    # lifespan 中增加: app.state.db = create_async_engine(...)
    # 新增: app.state.memory = ConversationMemory(db)

apps/assistant_api/greenbook_assistant_api/api/routes.py
    # routes.py 中的 conversation_store → db.conversations
    # message_store → db.messages
    # run_store → db.runs
    # approval_store → db.approvals
    # 接口签名不变，只改存储后端
```

### 风险
- **低** — 仅存储后端变更，接口不变
- 需要确保本地开发环境有 PostgreSQL（已有 Docker Compose）

### 验收标准
- 现有 E2E 测试全部通过
- 进程重启后会话和消息不丢失

---

## Phase 1: Task Model + Task Registry（1.5 周）

### 目标
引入 Task 概念，TaskRegistry 管理 Task 生命周期。

### 新增文件
```
packages/assistant_core/greenbook_assistant_core/task/__init__.py
packages/assistant_core/greenbook_assistant_core/task/models.py         # Task, TaskStatus, Artifact, ArtifactRef
packages/assistant_core/greenbook_assistant_core/task/registry.py       # TaskRegistry
```

### 数据库迁移
```sql
CREATE TABLE assistant_tasks (...);
CREATE TABLE assistant_artifacts (...);
```

### 修改文件
```
apps/assistant_api/greenbook_assistant_api/api/routes.py
    # send_message() 中新增：
    #   task_intent = TaskUnderstanding._quick_path(user_message)  # 只用 L1
    #   task = await task_registry.resolve_or_create(task_intent, ...)
    #   session.active_task_id = task.task_id
    #   仅做记录，不影响执行路径

apps/assistant_api/greenbook_assistant_api/main.py
    # lifespan 中新增: app.state.task_registry = TaskRegistry(db)
```

### 风险
- **低** — Task 目前只做记录（observability），不改变执行路径
- Task 表是新增的，不影响现有数据

### 验收标准
- 每轮对话自动创建 Task 记录
- `GET /conversations/{id}/tasks` 返回 Task 列表（新增 API，可选）

---

## Phase 2: Task Understanding（1.5 周）

### 目标
LLM 驱动的意图理解，替代关键词检测。

### 新增文件
```
packages/assistant_core/greenbook_assistant_core/task/understanding.py  # TaskUnderstanding
```

### 修改文件
```
packages/assistant_core/greenbook_assistant_core/agent.py
    # run() 开头调用:
    #   intent = await task_understanding.understand(msg, session, tasks)
    #   将 intent 注入 system prompt（作为上下文，不改变行为）

apps/assistant_api/greenbook_assistant_api/main.py
    # lifespan 中新增: app.state.task_understanding = TaskUnderstanding(llm, model)
```

### 风险
- **低** — TaskIntent 只作为上下文字段注入，不参与执行决策
- LLM 理解可能不准确，但有 fallback 机制

### 验收标准
- TaskIntent 生成准确率 > 80%（通过标注的测试集）
- 语义相似意图正确归类（"优化"、"提升"、"改进" → IMPROVE_CONTENT）

---

## Phase 3: Capability + Planner（2 周）

### 目标
引入 Capability 层和 Planner，生成 CapabilityDAG。

### 新增文件
```
packages/assistant_core/greenbook_assistant_core/planning/__init__.py
packages/assistant_core/greenbook_assistant_core/planning/capability.py  # Capability + Catalog
packages/assistant_core/greenbook_assistant_core/planning/planner.py     # Planner
```

### 修改文件
```
packages/assistant_core/greenbook_assistant_core/agent.py
    # run() 中新增:
    #   if task_intent.goal_category == "COMPOSITE" or len(task_intent.requirements) >= 3:
    #       task.plan = await planner.plan(task, capabilities)
    #   else:
    #       task.plan = None  # 走旧路径

    # 有 Plan 时: 目前只记录到 task.plan，不实际执行
    # 无 Plan 时: 走旧路径（不变）
```

### 风险
- **中** — 新增 Planner LLM 调用，影响延迟和成本
- 只对复杂任务使用 Planner（~20% 的用户请求）
- 旧路径作为 fallback

### 验收标准
- 复杂任务生成合理的 CapabilityDAG
- DAG 通过校验（无环、依赖正确、Capability 存在）
- 简单任务不触发 Planner（节约成本）

---

## Phase 4: Execution Engine（2 周）

### 目标
按 CapabilityDAG 执行任务，管理 Step 状态和 Checkpoint。

### 新增文件
```
packages/assistant_core/greenbook_assistant_core/execution/__init__.py
packages/assistant_core/greenbook_assistant_core/execution/models.py      # Step, StepStatus
packages/assistant_core/greenbook_assistant_core/execution/engine.py      # ExecutionEngine
packages/assistant_core/greenbook_assistant_core/execution/mapper.py      # CapabilityToolMapper
```

### 数据库迁移
```sql
CREATE TABLE assistant_task_steps (...);
```

### 修改文件
```
packages/assistant_core/greenbook_assistant_core/agent.py
    # _planned_execution() 实际调用 ExecutionEngine.execute(task)
    # _simple_loop() 保留作为 fallback

apps/assistant_api/greenbook_assistant_api/main.py
    # lifespan 中新增:
    #   mapper = CapabilityToolMapper()
    #   app.state.execution_engine = ExecutionEngine(mcp, llm, mapper, db)
```

### 风险
- **高** — 这是最大的变更，涉及执行路径的实质性改变
- 需要充分的集成测试
- 建议先对简单 DAG（2-3 步）验证，逐步增加复杂度
- 保留旧路径作为 fallback

### 验收标准
- 3 步 DAG（SEARCH → CREATE → SCHEDULE）正确执行
- Step 状态正确流转
- 工具失败时正确重试/跳过
- Checkpoint 可恢复

---

## Phase 5: 收敛与清理（1 周）

### 目标
移除旧代码，统一到新架构。

### 删除/大幅精简
```
packages/assistant_core/greenbook_assistant_core/agent.py
    # 删除: _turn_intents(), _turn_routing_hint(), _turn_tool_filter()
    # 删除: 顺序工具门控 if-else 块 (行 490-522)
    # 删除: PRODUCT_DEFAULTS 常量（移到 prompts/system.py）
    # 精简: _simple_loop → 仅保留最简 LLM 循环

apps/assistant_api/greenbook_assistant_api/api/routes.py
    # 删除: _build_tool_schemas() 中的硬编码 Schema
    #     → 改为从 mcp.get_tool_definitions() 动态生成
    # 删除: _normalize_schedule_tool_args 中的重复逻辑
    #     → 移到 execution/mapper.py
```

### 风险
- **低** — 此时新旧路径已验证，收敛只是清理

---

# 11. 风险汇总

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| Planner 输出不合理 | 中 | Plan 校验机制 + 旧路径 fallback |
| LLM 意图理解错误 | 中 | L1 快速路径 + Pydantic 校验 + fallback |
| 执行路径变更引入 Bug | 高 | 新旧路径双轨并行 + 充分集成测试 |
| PostgreSQL 依赖增加部署复杂度 | 低 | Docker Compose 已有 PostgreSQL |
| LLM 调用次数增加导致延迟 | 中 | 仅复杂任务走 Planner (~20%)；简单任务走旧路径 |
| Capability 目录膨胀 | 低 | 硬性限制 12 个，新增需 review |

---

# 附录 A: 关键设计决策

1. **为什么 Capability 是 11 个不是 20 个？**
   保持简单。旧版的 Capability Graph 和 Agent Registry 膨胀到难以维护。11 个覆盖当前所有场景，新增需经过设计 review。

2. **为什么保留旧路径？**
   渐进式迁移的安全网。每个 Phase 都是新旧双轨并行，验证稳定后才在 Phase 5 移除旧路径。

3. **为什么不直接用 LangGraph？**
   当前系统已经稳定运行，MCP 调用链清晰。引入 LangGraph 会增加框架依赖和学习成本。Task CapabilityDAG 的拓扑执行用 ~200 行纯 Python 即可实现。

4. **为什么 TaskIntent 要 LLM 生成？**
   中文社区运营场景中，"优化一下"、"改进内容"、"参考优秀文章重新整理" 语义相近但关键词完全不同。LLM 是唯一能统一理解这些变体的方式。但保留快速路径处理明确的单步操作。

5. **为什么 Task 和 Run 是分开的？**
   Task 是用户的长期目标（跨多个 Turn），Run 是一次 Agent 执行（一个 Turn）。一个 Task 可能对应多个 Run（如：第一轮创建草稿，第二轮修改标题），但每个 Run 都属于一个 Task。
