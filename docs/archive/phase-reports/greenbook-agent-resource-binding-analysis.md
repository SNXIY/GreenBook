# GreenBook Agent Runtime — Resource Binding 架构分析

> 日期: 2026-08-07
> 问题: CREATE 场景被错误路由到 UPDATE，因为 session.active_* 状态跨轮次污染
> 目标: 设计通用 ResourceResolver 层，区分创建/修改/删除/查询

---

# 一、当前资源绑定链路

## 1.1 旧路径 (agent.py) — 当前默认路径

```
用户: "帮我写一篇Java并发文章，晚上8点发布"
    │
    ▼
_turn_intents(user_message)
    asks_create=True  (关键词: "写一篇")
    asks_schedule=True (关键词: "晚上8点发布")
    │
    ▼
_schedule_tool_for_session(session)
    if session.active_schedule_id:    ← BUG ROOT: 上一轮的 schedule 残留
        return "publication_update_schedule"   ← 错误! 应该创建新的
    return "publication_schedule"
    │
    ▼
_turn_routing_hint()
    "INTERNAL TURN ROUTING: This is a create-and-schedule request.
     Call content_create_draft first, then publication_schedule..."
     ↑ 路由提示说 "publication_schedule"
    │
    ▼
_turn_tool_filter()
    第一轮: {content_create_draft}
    第二轮(创建成功后): {_schedule_tool_for_session()}
    │
    ▼
LLM 看到 system prompt:
    "当前活跃定时任务: sched-123"   ← 污染! 旧 schedule ID 在 context 中
    │
    ▼
LLM tool call: publication_update_schedule(schedule_id="sched-123", ...)
                ↑ 错误! 应该调用 publication.schedule (无 schedule_id)
```

## 1.2 污染链条

```
轮次 1: "创建 Java 文章，明天 8 点发布"
  → content.create_draft → draft_id = draft-a
  → publication.schedule(draft_id=draft-a) → schedule_id = sched-a
  → session.active_schedule_id = "sched-a"
  → _save_session() → DB: active_schedule_id = "sched-a"

轮次 2: "帮我写一篇 Python 文章，晚上 8 点发布"  ← 全新任务!
  → _load_session() → active_schedule_id = "sched-a"  ← 残留!
  → _schedule_tool_for_session() → "publication_update_schedule"
  → LLM 调用 update_schedule(schedule_id="sched-a")  ← 错误!
```

## 1.3 新 Runtime 路径 — 不同的设计

```
RuntimeAgentService (ASSISTANT_RUNTIME_MODE=on 时启用)
    │
TaskUnderstanding → TaskIntent
    goal_category = "CREATE_CONTENT"
    relation = "NEW_TASK"
    requirements = [{type: CREATE}, {type: PUBLISH}]
    │
Orchestrator._select_template()
    has_create=True, has_publish=True
    → CREATE_AND_PUBLISH template
    → Step 1: GENERATE_CONTENT → content.create_draft
    → Step 3: SCHEDULE_PUBLISH → publication.schedule  ← 正确! 创建新 schedule
```

**关键区别:** Runtime 路径基于 `TaskIntent.requirements` 做决策，不依赖 session state。

---

# 二、分析当前设计缺陷

## 2.1 CREATE 场景错误复用历史资源

**缺陷位置:** `agent.py:_schedule_tool_for_session()` (行 91-96)

```python
def _schedule_tool_for_session(session: SessionContext | None) -> str:
    if session is not None and session.active_schedule_id:
        return "publication_update_schedule"    # ← 只要存在就修改
    return "publication_schedule"
```

**根因:** 工具选择基于 session state (`active_schedule_id`)，而非用户意图。

**影响范围:**
- 任何带有 schedule 关键字的请求
- 即使语义上是全新创作 + 全新发布
- 只要 session 中有残留的 schedule_id

**同样的问题存在于:**
- `_bind_target_tool_args()` → 绑定 `session.active_draft_id` 到 `content.revise_draft`
- System prompt 中的 "当前活跃定时任务: {id}" → LLM 被误导

## 2.2 UPDATE 场景如何识别目标

**当前机制:** 依赖 `session.active_schedule_id`，只有一个。

**问题:** 当存在多个 Task 时（Java文章有 schedule-a，Python文章有 schedule-b），session 只能指向最近创建的那个。用户说"修改Java文章发布时间"时，系统可能找错 schedule。

## 2.3 模糊引用处理

**当前机制:** `_turn_intents()` 无法区分 "创建新文章+新发布" vs "修改已有发布"。两者 keywords 相同。

**Runtime 路径:** `TaskUnderstanding` 可以区分（通过 `relation=NEW_TASK vs MODIFY_TASK`），但 Runtime 默认未启用。

---

# 三、设计 ResourceResolver 层

## 3.1 核心概念

```
TaskIntent (用户意图)
    │
    ▼
IntentOperation (操作分解)
    │
    ▼
ResourceResolver (资源解析)
    │
    ├── DraftResolver      → draft_id or None (create new)
    ├── ScheduleResolver   → schedule_id or None (create new)
    └── PostResolver       → post_id or None (create new)
    │
    ▼
CapabilityMapper → Plan → Execution
```

## 3.2 IntentOperation 模型

```python
class ResourceOperation(StrEnum):
    CREATE = "CREATE"     # 创建新资源
    UPDATE = "UPDATE"     # 修改已有资源
    DELETE = "DELETE"     # 删除资源
    QUERY = "QUERY"       # 查询资源

class ResourceType(StrEnum):
    CONTENT_DRAFT = "CONTENT_DRAFT"
    SCHEDULE = "SCHEDULE"
    POST = "POST"
    COMMENT = "COMMENT"

class IntentOperation(BaseModel):
    operation: ResourceOperation
    resource_type: ResourceType
    target_hint: str | None = None       # "Java文章", "刚才那个"
    target_id: str | None = None         # 明确 target
    is_ambiguous: bool = False
    candidates: list[str] = []           # 模糊时的候选
    confidence: float = 1.0
```

## 3.3 语义 → Operation 映射

| 用户输入 | operations |
|---------|-----------|
| "帮我写一篇Java文章，晚上8点发布" | `[CREATE(CONTENT_DRAFT), CREATE(SCHEDULE)]` |
| "修改刚才文章标题" | `[UPDATE(CONTENT_DRAFT, hint="刚才")]` |
| "把Java文章发布时间改晚上9点" | `[UPDATE(SCHEDULE, hint="Java文章")]` |
| "取消定时发布" | `[DELETE(SCHEDULE, hint="最近")]` |
| "搜索社区Java帖子" | `[QUERY(POST)]` |

## 3.4 ResourceResolver 决策树

```
IntentOperation(operation=CREATE, resource_type=CONTENT_DRAFT)
    → 不需要 target → 返回 None (create new) ✓

IntentOperation(operation=UPDATE, resource_type=SCHEDULE, hint="Java文章")
    → 需要 target → Resolver 查找:
      1. 按 hint 匹配 Task ("Java文章")
      2. 在该 Task 的 artifacts 中找 SCHEDULE 类型
      3. 找到 → 返回 schedule_id
      4. 未找到 → 返回 error: "Java文章没有定时发布"

IntentOperation(operation=UPDATE, resource_type=SCHEDULE, hint="刚才那个")
    → 需要 target → 模糊引用:
      1. 查找所有 SCHEDULE artifacts
      2. 多个匹配 → is_ambiguous=True, candidates=[...]
      3. → clarification 流程
```

---

# 四、现有代码复用分析

| 模块 | 状态 | 说明 |
|------|------|------|
| `task/understanding.py` | ✅ 复用 | L1+L2 已能区分 NEW_TASK vs MODIFY_TASK。增加 `resource_operations` 字段输出 |
| `task/resolver.py` | ✅ 扩展 | 已有 5 级匹配。增加 `resolve_operation()` 方法 |
| `task/models.py` | ✅ 扩展 | TaskIntent 已有 requirements、constraints。新增 `resource_operations` |
| `capability/registry.py` | ✅ 复用 | Capability 映射已就绪 |
| `capability/mapper.py` | ✅ 扩展 | 增加 operation→requirement 映射 |
| `orchestration/orchestrator.py` | ✅ 复用 | 模板选择基于 requirements，已正确区分 CREATE/PUBLISH vs UPDATE |
| `execution/worker.py` | ✅ 复用 | 已支持传递 artifact 到 downstream |
| `execution/capability_executor.py` | ✅ 复用 | 已支持 invoke_fn |
| `artifact/store.py` | ✅ 扩展 | 增加 `find_by_type_and_task()` 查询 |
| `agent.py` | 🗑️ 逐步废弃 | `_schedule_tool_for_session()` + `_bind_target_tool_args()` 基于 session state — 将被 Runtime 路径替代 |

---

# 五、修改方案

## 5.1 目标

1. **旧路径急修:** 修复 `_schedule_tool_for_session()` — 只在明确的 modify 场景才用 update_schedule
2. **新 Runtime 增强:** TaskIntent 增加 `resource_operations`，Orchestrator 使用它决定模板
3. **ResourceResolver:** 统一 CREATE/UPDATE/DELETE/QUERY 的资源解析
4. **启用 Runtime 路径:** 将 ASSISTANT_RUNTIME_MODE 默认改为 `dual`

## 5.2 新增模块

```
packages/assistant_core/greenbook_assistant_core/resource/
    __init__.py
    models.py          # IntentOperation, ResourceOperation, ResourceType
    resolver.py        # ResourceResolver — 统一资源解析
```

## 5.3 修改文件

| 文件 | 变更 |
|------|------|
| `task/models.py` | TaskIntent +`resource_operations: list[IntentOperation]` |
| `task/understanding.py` | L1+L2 输出 `resource_operations` |
| `agent.py` | `_schedule_tool_for_session()` — 只在 `_turn_intents` 检测到 modify 意图时才用 update |
| `orchestration/orchestrator.py` | `_select_template()` 使用 operations 而非 session state |
| `artifact/store.py` | +`resolve_schedule_for_task()`, +`resolve_draft_for_task()` |
| `main.py` | 默认 `ASSISTANT_RUNTIME_MODE=dual` |

## 5.4 旧路径最小修复 (agent.py)

```python
# Before (BUG):
def _schedule_tool_for_session(session):
    if session.active_schedule_id:
        return "publication_update_schedule"  # ALWAYS updates
    return "publication_schedule"

# After (FIXED):
def _schedule_tool_for_session(session, user_message=""):
    # Only use update when the user explicitly says "修改"/"调整" schedule time
    asks_modify = any(w in user_message for w in ("修改.*时间", "调整.*时间", "改.*时间"))
    if session.active_schedule_id and asks_modify:
        return "publication_update_schedule"
    return "publication_schedule"  # default: create new
```

## 5.5 测试方案

```python
# Case 1: CREATE new when old schedule exists
已有: Java文章 + schedule-a
输入: "帮我写Python文章，晚上8点发布"
期望: publication.schedule (创建 schedule-b, 不修改 schedule-a)

# Case 2: UPDATE when explicitly modifying
已有: Java文章 + schedule-a
输入: "把Java文章发布时间改晚上9点"
期望: publication.update_schedule(schedule_id=schedule-a)

# Case 3: Ambiguity when multiple targets
已有: Java文章 + Python文章 (both have schedules)
输入: "修改刚才那个发布时间"
期望: 返回 ambiguity, candidates=[schedule-a, schedule-b]

# Case 4: CREATE always when NEW_TASK
输入: "创建一篇新文章，明天发布" (session 有旧 schedule)
期望: 创建新 draft + 创建新 schedule (不修改旧 schedule)
```

---

# 六、实施顺序

| Phase | 内容 | 风险 |
|-------|------|------|
| 5.6a | agent.py 最小修复: `_schedule_tool_for_session` | 低 — 单函数改动 |
| 5.6b | TaskIntent + resource_operations 模型 | 低 — 新增字段 |
| 5.6c | ResourceResolver | 中 — 新增模块 |
| 5.6d | ASSISTANT_RUNTIME_MODE=dual 默认 | 中 — Runtime 路径覆盖 3 个场景 |
| 5.6e | 移除 agent.py session state 依赖 | 低 — 仅清理 |
