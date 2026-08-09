# Phase 6.2.2-B — 接入 TaskReferenceResolver 到执行链

> 日期: 2026-08-07
> 状态: 设计阶段

---

# 1. 插入点

```
_execute_single(ctx)
  │
  ├── [1] task_id = ctx.task_id or uuid4()
  ├── [2] Capabilities check
  │
  ├── [1.3] NEW: TaskReferenceResolver    ← 插入点
  │     if ctx.task_intent.target_task_hint:
  │       result = ref_resolver.resolve(hint, ctx.recent_tasks)
  │       if result.needs_clarification: return clarification
  │       if result.best_match: ctx.task_id = result.best_match.task_id
  │
  ├── [1.5] ResourceResolver              ← 后续自动使用 ctx.task_id
  │
  └── ...
```

# 2. 关键数据流

```
TaskIntent.target_task_hint = "昨天那个文章"
    │
    ▼
TaskReferenceResolver.resolve("昨天那个文章", ctx.recent_tasks)
    │
    ├── best_match.task_id = "task-a"
    │
    ▼
ctx.task_id = "task-a"    ← 注入 RuntimeContext
    │
    ▼
ResourceResolver 构建 ResourceRequest 时:
  task_id = ctx.task_id  → "task-a"
  → 查找 task-a.artifacts → 找到 DRAFT/SCHEDULE
```

# 3. 修改文件

| 文件 | 变更 |
|------|------|
| `runtime_agent_service.py` | `_execute_single()`: 在 ResourceResolver 前插入 ReferenceResolver |
| `tests/unit/test_reference_resolver.py` | +3 个 E2E 集成测试 |

# 4. 测试

| # | 场景 | 期望 |
|---|------|------|
| 1 | 创建文章 → "修改昨天那个文章标题" | ctx.task_id = 昨天文章的 id |
| 2 | 两个今日任务 → "修改刚才那个" | needs_clarification=True |
| 3 | Group 内 "第一篇文章" 优先于历史匹配 | resolves to group task, not history |

# 5. 不修改

```
TaskResolver      — 零改动
ResourceResolver  — 零改动 (ctx.task_id 自然流入)
Worker/ToolRuntime — 零改动
GroupExecutor     — 零改动
```
