# Phase 6.6 — Agent Memory Runtime 设计

> 日期: 2026-08-07
> 状态: 设计阶段

---

# 1. 三类 Memory 定义

| 类型 | 内容 | 生命周期 | 用途 |
|------|------|---------|------|
| Episodic | 用户历史任务、artifact、execution 结果、tool 调用记录 | 随 conversation 积累 | "昨天那个文章" → 找到对应 Task; "上次用的什么标题风格" |
| Semantic | 用户长期偏好、习惯 | 跨 conversation 持久 | "默认发布到哪个平台"、"喜欢什么写作风格" |
| Procedural | Agent 执行经验、workflow 模式 | 跨 conversation 累积 | "CREATE+IMPROVE 比直接 CREATE 效果好"、"用户经常拒绝第一次标题" |

# 2. 数据模型

## 2.1 MemoryRecord

```python
class MemoryType(StrEnum):
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PROCEDURAL = "PROCEDURAL"

class MemoryRecord(BaseModel):
    """One memory entry — stored in-memory (Phase 1), DB (Phase 2)."""

    memory_id: str                          # UUID
    user_id: str                            # owner
    type: MemoryType

    # ── content ──
    content: str                            # human-readable summary
    embedding: list[float] | None = None    # vector (Phase 2 — semantic search)

    # ── metadata ──
    metadata: dict = {}                     # flexible context
    # episodic: {task_id, execution_id, tool_name, artifact_ids, …}
    # semantic: {preference_type, confidence, source, …}
    # procedural: {pattern_type, success_count, failure_count, …}

    # ── lifecycle ──
    importance: float = 0.5                 # 0.0–1.0 (higher = keep longer)
    access_count: int = 0
    last_accessed_at: str = ""
    created_at: str = ""
    expires_at: str | None = None           # TTL for low-importance records
```

## 2.2 MemoryQuery

```python
class MemoryQuery(BaseModel):
    """Search/filter parameters."""
    type: MemoryType | None = None          # filter by type
    user_id: str = ""
    keywords: list[str] = []                # simple keyword match
    metadata_filters: dict = {}             # {task_id: "xxx"}
    min_importance: float = 0.0
    limit: int = 10
    sort_by: str = "importance"             # importance | created_at | access_count
```

---

# 3. Store 设计

## 3.1 MemoryStore (in-memory Phase 1)

```python
class MemoryStore:
    """In-memory CRUD for MemoryRecords."""

    def __init__(self):
        self._records: dict[str, MemoryRecord] = {}

    def save(self, record: MemoryRecord) -> MemoryRecord: ...
    def find_by_id(self, memory_id: str) -> MemoryRecord | None: ...
    def search(self, query: MemoryQuery) -> list[MemoryRecord]: ...
    def update(self, memory_id: str, **fields) -> MemoryRecord | None: ...
    def delete(self, memory_id: str) -> None: ...
    def count(self, user_id: str | None = None) -> int: ...
```

## 3.2 搜索策略

```
Phase 1 (关键词):
  keyword in record.content (case-insensitive)
  + metadata filter match
  + importance threshold
  → sorted by importance desc, access_count desc

Phase 2 (语义):
  embedding similarity (cosine) × importance weight
  + hybrid: keyword boost for exact matches
```

---

# 4. Manager 设计

## 4.1 MemoryManager

```python
class MemoryManager:
    """Business logic for Agent Memory."""

    def __init__(self, store: MemoryStore | None = None):
        self._store = store or MemoryStore()

    # ── CRUD ──

    def remember(self, record: MemoryRecord) -> MemoryRecord:
        """Save a new memory."""
        return self._store.save(record)

    def recall(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Search memories matching query."""
        results = self._store.search(query)
        for r in results:
            r.access_count += 1
            r.last_accessed_at = _now_iso()
        return results

    def forget(self, memory_id: str) -> None:
        """Delete a memory."""
        self._store.delete(memory_id)

    # ── episodic ──

    def remember_execution(
        self, user_id: str, task: Task, result: RuntimeResult,
    ) -> MemoryRecord:
        """Record a completed execution as episodic memory."""
        return self.remember(MemoryRecord(
            user_id=user_id, type=MemoryType.EPISODIC,
            content=f"Executed: {task.goal} → {result.status}",
            metadata={
                "task_id": task.task_id,
                "goal_category": task.goal_category,
                "status": result.status,
                "draft_id": result.draft_id,
                "schedule_id": result.schedule_id,
                "tool_rounds": result.tool_rounds,
            },
            importance=self._compute_importance(task, result),
        ))

    # ── semantic ──

    def remember_preference(
        self, user_id: str, preference_type: str, value: str,
        confidence: float = 0.5,
    ) -> MemoryRecord:
        """Store a user preference."""
        return self.remember(MemoryRecord(
            user_id=user_id, type=MemoryType.SEMANTIC,
            content=f"User prefers {preference_type}: {value}",
            metadata={
                "preference_type": preference_type,
                "value": value,
                "confidence": confidence,
            },
            importance=confidence * 0.8,
        ))

    # ── procedural ──

    def remember_pattern(
        self, user_id: str, pattern: str,
        success: bool, context: dict | None = None,
    ) -> MemoryRecord:
        """Store an execution pattern observation."""
        return self.remember(MemoryRecord(
            user_id=user_id, type=MemoryType.PROCEDURAL,
            content=pattern,
            metadata={
                "pattern_type": pattern,
                "success": success,
                "context": context or {},
            },
            importance=0.3,  # start low, increase with repetition
        ))

    # ── helpers ──

    @staticmethod
    def _compute_importance(task: Task, result: RuntimeResult) -> float:
        """Importance based on execution outcome."""
        base = 0.5
        if result.success:
            base += 0.2  # Successful executions are more important
        if result.side_effect_committed:
            base += 0.2  # Side effects (drafts, schedules) → important
        if result.tool_rounds > 2:
            base += 0.1  # Complex executions → more valuable to remember
        return min(base, 1.0)
```

---

# 5. 与 Runtime 集成点 (Phase 2)

```
RuntimeAgentService._finish_execution()
    ↓
    MemoryManager.remember_execution(user_id, task, result)
    → episodic memory recorded

TaskUnderstanding.understand()
    ↓
    MemoryManager.recall(query) → relevant memories
    → injected into L2 prompt context: "User prefers: ..."

TaskResolver.resolve()
    ↓
    MemoryManager.recall(episodic, keywords=["Java文章"])
    → finds past task with Java article

Orchestrator._select_template()
    ↓
    MemoryManager.recall(procedural, pattern="CREATE_AND_IMPROVE")
    → "This user often needs quality improvement → prefer CREATE_AND_IMPROVE"
```

---

# 6. 修改文件 (Phase 1)

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `packages/assistant_core/greenbook_assistant_core/memory/__init__.py` | package marker |
| **新增** | `packages/assistant_core/greenbook_assistant_core/memory/models.py` | MemoryType, MemoryRecord, MemoryQuery |
| **新增** | `packages/assistant_core/greenbook_assistant_core/memory/store.py` | MemoryStore (in-memory CRUD) |
| **新增** | `packages/assistant_core/greenbook_assistant_core/memory/manager.py` | MemoryManager |
| **新增** | `tests/unit/test_memory.py` | 测试 |

### 不修改

```
Worker, ToolRuntime, HumanInteraction, agent.py — 零改动
```

---

# 7. 测试方案

| # | 场景 | 内容 |
|---|------|------|
| 1 | 存储 episodic 记录 | remember_execution() → recall by user_id |
| 2 | 存储 semantic 偏好 | remember_preference() → recall by type + keywords |
| 3 | 存储 procedural 模式 | remember_pattern() → recall by metadata |
| 4 | 关键词搜索 | recall(keywords=["Java"]) → 匹配 content |
| 5 | 重要性排序 | 3 records with different importance → recall 按 importance 降序 |
| 6 | 类型过滤 | recall(type=EPISODIC) → 只返回 EPISODIC |
| 7 | metadata 过滤 | recall(metadata_filters={task_id: "t1"}) |
| 8 | 访问计数更新 | recall() → access_count += 1 |
| 9 | 删除 | forget(id) → recall returns empty |
| 10 | 过期清理 | expires_at in past → auto-filtered |

### 10 个测试, 预计 490 → 500 passed
