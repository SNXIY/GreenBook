# Phase 5.6 — Resource Binding 详细设计

> 日期: 2026-08-07
> 前置: `docs/reports/greenbook-agent-resource-binding-analysis.md`
> 状态: 设计阶段 — 确认后编码

---

# 1. 当前数据流完整追踪

## 1.1 TaskIntent 当前结构

```python
class TaskIntent:
    relation: "NEW_TASK" | "MODIFY_TASK" | ...     # Task 级别关系
    goal_category: "CREATE_CONTENT" | "IMPROVE_CONTENT" | ...
    goal: str                                        # "写一篇Java文章"
    target_task_id: str | None                       # 解析后的 Task ID
    target_task_hint: str | None                     # "Java文章"
    requirements: [{type: "CREATE"}, {type: "PUBLISH"}]  # 有序需求列表
    constraints: [{type: "TIME", value: "晚上8点"}]
```

## 1.2 Requirements 生成流程

```
L1 (_quick_intent):
  asks_create=True, asks_schedule=True
  → requirements = [{type: CREATE}, {type: PUBLISH}]
  → relation = NEW_TASK
  → goal_category = CREATE_CONTENT

L2 (_llm_understand):
  LLM prompt 包含 existing_tasks 上下文
  → 输出 JSON: {relation, goal_category, requirements, ...}
  → Pydantic 校验
```

## 1.3 Orchestrator 依赖字段

```
Orchestrator.generate_plan(task_id, goal_category, requirements)
    │
    ├── goal_category: 只用于 fallback (requirements 为空时)
    │
    └── requirements[].type: 核心决策依据
        has_create, has_publish, has_improve, has_update, has_cancel, ...
        → select_template()
```

**Orchestrator 不关心:**
- `relation` (NEW_TASK vs MODIFY_TASK)
- `target_task_id` / `target_task_hint`
- `constraints`

**Orchestrator 只关心 `requirements[].type`**

## 1.4 Artifact 如何关联 Task

```
Task.artifacts: list[ArtifactRef]
    ├── ArtifactRef(artifact_type="DRAFT", resource_id="draft-a")
    ├── ArtifactRef(artifact_type="SCHEDULE", resource_id="sched-a")
    └── ArtifactRef(artifact_type="SEARCH_RESULT", ...)

查询路径:
  TaskRegistry.list_tasks(conv_id) → Task → .artifacts → 按 type 过滤
```

## 1.5 Schedule 当前如何绑定

**旧路径:**
```
session.active_schedule_id = "sched-a"     ← 全局唯一, 跨轮次残留
→ _schedule_tool_for_session() → update_schedule or schedule
→ _bind_target_tool_args() → 自动填入 schedule_id
```

**新 Runtime 路径:**
```
Worker: 遍历上游 Step.output_artifact
  → 找到 DRAFT artifact → 注入 draft_id
  → 找到 SCHEDULE artifact → 注入 schedule_id
CapabilityExecutor: 构建 ToolInvocationContext
  → _build_tool_args() 使用 constraints
```

# 2. 核心问题总结

| # | 问题 | 影响 |
|---|------|------|
| 1 | TaskIntent 只有 Task 级别的 relation，无 Resource 级别操作类型 | "创建文章+新发布" vs "修改已有发布" 无法从 requirements 区分 — 两者都是 [{CREATE}, {PUBLISH}] |
| 2 | Orchestrator 只看 requirements[].type，不区分 CREATE 和 UPDATE | SINGLE_PUBLISH 和 SINGLE_MANAGE_SCHEDULE 是两个不同模板，但 requirements type 需要不同值("PUBLISH" vs "UPDATE") |
| 3 | 旧路径 session.active_* 全局单值 | 多 Task 无法区分目标 |
| 4 | 无标准化的 Resource Resolver | 每次需要 target 时，由各层自行解决(draft_id 由 Worker BFS 查找, schedule_id 同理) |

# 3. Phase 5.6 设计

## 3.1 新增数据模型

### `resource/models.py` — ResourceTarget

```python
class ResourceOperation(StrEnum):
    CREATE = "CREATE"      # 创建新资源 (不需要 target)
    UPDATE = "UPDATE"      # 修改已有资源 (需要 target)
    DELETE = "DELETE"      # 删除资源
    QUERY = "QUERY"        # 查询资源

class ResourceType(StrEnum):
    CONTENT_DRAFT = "CONTENT_DRAFT"
    SCHEDULE = "SCHEDULE"
    POST = "POST"

class ResourceTarget(BaseModel):
    """解析后的资源目标."""
    operation: ResourceOperation
    resource_type: ResourceType
    resource_id: str | None = None       # 具体的资源 ID (如 draft_id)
    task_id: str | None = None           # 资源所属的 Task ID
    hint: str | None = None              # 用户引用提示
    is_ambiguous: bool = False
    candidates: list[str] = []           # 模糊时的候选 resource_id
    confidence: float = 0.0
    match_reason: str = ""
```

### `task/models.py` 扩展 — TaskIntent +`resource_targets`

```python
class TaskIntent(BaseModel):
    # ... 现有字段不变 ...
    resource_targets: list[ResourceTarget] = []  # NEW Phase 5.6
```

### `resource/models.py` — ResourceResolutionResult

```python
class ResourceResolutionResult(BaseModel):
    """一次 ResourceResolver 调用的完整结果."""
    targets: list[ResourceTarget] = []
    needs_clarification: bool = False    # 有模糊引用需要澄清
    errors: list[str] = []
```

## 3.2 新增模块

```
packages/assistant_core/greenbook_assistant_core/resource/
    __init__.py
    models.py           # ResourceTarget, ResourceOperation, ResourceType
    resolver.py         # ResourceResolver — 统一入口
```

### `resource/resolver.py` — ResourceResolver

```python
class ResourceResolver:
    """统一资源解析层.

    输入: TaskIntent + 会话中的 Tasks
    输出: ResourceResolutionResult (targets + 是否需要澄清)
    """

    def resolve(
        self,
        intent: TaskIntent,
        tasks: list[Task],
    ) -> ResourceResolutionResult:
        """
        对每个 resource_target：
        - CREATE → 不需要 target → resource_id=None
        - UPDATE → 需要 target → 从 Task.artifacts 查找
        - DELETE → 需要 target → 从 Task.artifacts 查找
        - QUERY  → 不需要 target
        """

    def _resolve_update_target(
        self,
        target: ResourceTarget,
        tasks: list[Task],
    ) -> ResourceTarget:
        """
        1. target.task_id 已知 → 在该 Task.artifacts 中按 type 查找
        2. target.hint 有值 → 匹配 Task.goal → 在 Artifacts 中查找
        3. 只有一个匹配 → 返回 resource_id
        4. 多个匹配 → is_ambiguous=True, candidates=[...]
        5. 无匹配 → error
        """

    def _resolve_create_target(
        self,
        target: ResourceTarget,
    ) -> ResourceTarget:
        """CREATE 不需要 target — resource_id 保持 None."""
```

## 3.3 修改文件

### `task/models.py`

```diff
+ from greenbook_assistant_core.resource.models import ResourceTarget

  class TaskIntent(BaseModel):
      # ... existing fields unchanged ...
+     resource_targets: list[ResourceTarget] = []
```

### `task/understanding.py`

```diff
  def _quick_intent(...) -> TaskIntent | None:
      # ... existing logic ...
+     # ADD: derive resource_targets from asks_* flags
+     resource_targets = []
+     if asks_create:
+         resource_targets.append(ResourceTarget(
+             operation=CREATE, resource_type=CONTENT_DRAFT))
+     elif asks_revise or asks_improve:
+         resource_targets.append(ResourceTarget(
+             operation=UPDATE, resource_type=CONTENT_DRAFT,
+             hint=target_hint))
+     if asks_schedule:
+         # Key fix: CREATE when NEW_TASK, UPDATE when MODIFY_TASK
+         op = CREATE if relation == "NEW_TASK" else UPDATE
+         resource_targets.append(ResourceTarget(
+             operation=op, resource_type=SCHEDULE,
+             hint=target_hint if op == UPDATE else None))
+     intent.resource_targets = resource_targets
```

### `services/runtime_agent_service.py`

```diff
  async def execute(self, ctx: RuntimeContext) -> RuntimeResult:
      # ...
+     # NEW: resolve resource targets before orchestration
+     if ctx.task_intent and ctx.task_intent.resource_targets:
+         resolver = ResourceResolver()
+         resolution = resolver.resolve(ctx.task_intent, recent_tasks)
+         if resolution.needs_clarification:
+             return self._clarification_result(ctx, resolution)
+         # Inject resolved resource_ids into constraints
+         for target in resolution.targets:
+             if target.operation == UPDATE and target.resource_id:
+                 _inject_target(ctx, target)
```

### `agent.py` — 最小修复

```diff
- def _schedule_tool_for_session(session: SessionContext | None) -> str:
-     if session is not None and session.active_schedule_id:
-         return "publication_update_schedule"
-     return "publication_schedule"

+ def _schedule_tool_for_session(
+     session: SessionContext | None,
+     user_message: str = "",
+ ) -> str:
+     # Only use update when intent is explicitly to modify existing schedule
+     if session is not None and session.active_schedule_id:
+         modify_signals = ("修改.*时间", "调整.*时间", "改.*时间",
+                          "推迟", "提前", "延后")
+         if any(re.search(p, user_message) for p in modify_signals):
+             return "publication_update_schedule"
+     return "publication_schedule"
```

## 3.4 数据流

### CREATE 场景

```
用户: "帮我写Python文章，晚上8点发布"
    │
    ▼
TaskUnderstanding (L1)
    relation = NEW_TASK
    goal_category = CREATE_CONTENT
    requirements = [{CREATE}, {PUBLISH}]
    resource_targets = [
        ResourceTarget(CREATE, CONTENT_DRAFT),     ← CREATE, no target needed
        ResourceTarget(CREATE, SCHEDULE),           ← CREATE, no target needed
    ]
    │
    ▼
ResourceResolver.resolve(intent, tasks)
    → CREATE targets → resource_id = None ✓
    │
    ▼
Orchestrator.generate_plan(requirements=[CREATE, PUBLISH])
    → CREATE_AND_PUBLISH template
    → GENERATE_CONTENT → VALIDATE_QUALITY → SCHEDULE_PUBLISH
    → publication.schedule (新 schedule) ✓
```

### UPDATE 场景

```
用户: "把Java文章发布时间改晚上9点"
    │
    ▼
TaskUnderstanding (L1)
    relation = MODIFY_TASK
    goal_category = MANAGE_SCHEDULE
    requirements = [{UPDATE}]
    resource_targets = [
        ResourceTarget(UPDATE, SCHEDULE, hint="Java文章"),
    ]
    │
    ▼
ResourceResolver.resolve(intent, tasks)
    1. hint="Java文章" → TaskResolver → task_id = task-a
    2. task-a.artifacts 中找 SCHEDULE → schedule-a
    → resource_id = "schedule-a" ✓
    │
    ▼
Orchestrator.generate_plan(requirements=[UPDATE])
    → SINGLE_MANAGE_SCHEDULE template
    → MANAGE_SCHEDULE → publication.update_schedule(schedule_id="schedule-a") ✓
```

### Ambiguity 场景

```
用户: "修改刚才那个发布时间"  (已有 Java+Python 两个 Task, 各有 schedule)
    │
    ▼
TaskUnderstanding (L1)
    relation = MODIFY_TASK
    resource_targets = [
        ResourceTarget(UPDATE, SCHEDULE, hint="刚才那个"),
    ]
    │
    ▼
ResourceResolver.resolve(intent, tasks)
    1. hint="刚才那个" → temporal → 查找所有 SCHEDULE artifacts
    2. task-a.schedule + task-b.schedule → 两个匹配
    → is_ambiguous = True
    → candidates = ["schedule-a", "schedule-b"]
    │
    ▼
RuntimeAgentService → clarification_result
    content = "当前有两个定时任务：
              1. Java文章 (schedule-a)
              2. Python文章 (schedule-b)
              请问您要修改哪一个？"
```

## 3.5 调用链

```
routes.py: send_message()
    │
    ├── TaskUnderstanding.understand() → TaskIntent
    │     └── resource_targets 已填充
    │
    ├── ResourceResolver.resolve(intent, tasks)
    │     ├── CREATE targets → resource_id = None
    │     ├── UPDATE targets → 从 Task.artifacts 查找
    │     ├── DELETE targets → 从 Task.artifacts 查找
    │     └── 模糊 → needs_clarification = True
    │
    ├── [needs_clarification] → clarification_result (不执行)
    │
    └── AssistantService.execute(ctx)
          │
          ├── [legacy] → agent.run() (旧路径, 仅修复 _schedule_tool_for_session)
          │
          └── [runtime] → RuntimeAgentService.execute(ctx)
                │
                ├── Orchestrator.generate_plan(requirements, resource_targets)
                │     └── 模板选择使用 requirements[].type
                │
                ├── Worker 执行
                │     └── 传递上游 Artifact → 下游 constraints
                │
                └── RuntimeResult
```

## 3.6 测试案例

```python
class TestResourceBinding:
    """Phase 5.6 核心测试."""

    # ── Case 1: CREATE when old schedule exists ──
    def test_create_new_article_ignores_old_schedule(self):
        """
        已有: Java文章 (task-a) + schedule-a
        输入: "帮我写Python文章，晚上8点发布"
        期望:
          resource_targets = [CREATE(DRAFT), CREATE(SCHEDULE)]
          → publication.schedule (新 schedule-b, 不修改 schedule-a)
        """

    # ── Case 2: UPDATE explicit target ──
    def test_update_java_schedule_by_label(self):
        """
        已有: Java文章 (task-a) + schedule-a, Python文章 (task-b)
        输入: "把Java文章发布时间改晚上9点"
        期望:
          resource_targets = [UPDATE(SCHEDULE, resource_id=schedule-a)]
          → publication.update_schedule(schedule_id=schedule-a)
        """

    # ── Case 3: Ambiguity ──
    def test_ambiguous_temporal_hint_returns_clarification(self):
        """
        已有: Java文章 + schedule-a, Python文章 + schedule-b
        输入: "修改刚才那个发布时间"
        期望:
          needs_clarification = True
          candidates = [schedule-a, schedule-b]
        """

    # ── Case 4: CREATE always when NEW_TASK ──
    def test_new_task_always_creates_new_schedule(self):
        """
        session 有旧 schedule, 但用户说"创建一篇新文章,明天发布"
        期望:
          relation = NEW_TASK
          → CREATE(SCHEDULE), NOT UPDATE
        """

    # ── Case 5: DELETE schedule ──
    def test_cancel_schedule_finds_target(self):
        """
        已有: Java文章 + schedule-a
        输入: "取消Java文章的定时发布"
        期望:
          resource_targets = [DELETE(SCHEDULE, resource_id=schedule-a)]
        """
```

## 3.7 不变的文件

```
agent.py                    — 仅 _schedule_tool_for_session() 最小修复
LegacyAgentService          — 零改动
MCP 全部                    — 零改动
Java / Creator              — 零改动
packages/assistant_core/    — 只新增 resource/ 子包, 已有模块最小改动
```

## 3.8 实施步骤

| Step | 内容 | 风险 | 时间 |
|------|------|------|------|
| 1 | `resource/models.py` + `resource/resolver.py` | 低 — 纯新增 | 1h |
| 2 | `task/models.py` +`resource_targets` | 低 — 新增字段 | 30m |
| 3 | `task/understanding.py` L1 填充 resource_targets | 中 — 改动意图生成 | 1h |
| 4 | `agent.py` `_schedule_tool_for_session()` 修复 | 低 — 单函数 | 30m |
| 5 | `runtime_agent_service.py` 集成 ResourceResolver | 中 — 执行路径 | 1h |
| 6 | 测试 (5 cases + 现有 365 回归) | — | 1h |
