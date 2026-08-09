# Phase 6.7 — 复杂运营场景诊断

> 日期: 2026-08-07
> 状态: 诊断完成

---

# 1. 实际调用链

```
用户: "帮我运营一个Java并发专题。要求: 搜索社区热门帖子，分析原因，
       检查已有虚拟线程文章，有则修改无则创建，发布前确认，确认后定时发布"

    │
    ▼
TaskUnderstanding (L1, source=L1, confidence=0.85)
    │
    ├── relation = MODIFY_TASK          ❌ 应为 NEW_TASK
    ├── goal_category = IMPROVE_CONTENT  ❌ 应为 CREATE_CONTENT 或 COMPOSITE
    ├── requirements = [SEARCH, IMPROVE, PUBLISH]  ❌ 缺少 ANALYZE, CREATE
    └── resource_requests = [UPDATE DRAFT, UPDATE SCHEDULE, QUERY POST]
                              ❌ 应为 [CREATE DRAFT, CREATE SCHEDULE]
    │
    ▼
Orchestrator._select_template(requirements=[SEARCH, IMPROVE, PUBLISH])
    │
    ├── has_search + has_improve → IMPROVE_WITH_RESEARCH... but has_analyze=False
    ├── has_improve=True → SINGLE_IMPROVE  ❌ 只有 1 步!
    │
    └── Plan: [IMPROVE_CONTENT] → content.revise_draft
              ❌ 用户要创建新文章，不是修改已有文章!
    │
    ▼
ResourceResolver.resolve()
    │
    └── UPDATE DRAFT with no hint → find_across_tasks → no artifact → error
        ❌ 没有 draft 可修改，返回失败
```

# 2. 缺失节点（4 个）

## 缺失 1: "运营" 未被识别为创建意图

```
当前位置: task/understanding.py L1 _CREATE_WORDS
  现有: "写一篇", "创建一篇", "生成一篇", "发一篇", "发布一篇", "草稿", "保存"
  缺失: "运营"

影响: asks_create=False → relation=MODIFY_TASK 而非 NEW_TASK
```

## 缺失 2: "有则修改，无则创建" 被解释为直接修改

```
当前位置: task/understanding.py L1 _REVISE_WORDS
  现有: "修改", "改成", "改得", "改一下", "润色", "重写", "完善", "打磨"...
  
  问题: "有则修改" 是对条件的描述（"如果有就修改，没有就创建"），
        不是直接的"请修改这个文章"指令。
        L1 看到 "修改" 就设 asks_revise=True → MODIFY_TASK。
        
  影响: 整个 operation 被错误路由到 IMPROVE_CONTENT
```

## 缺失 3: "分析原因" 未产生 ANALYZE 需求

```
当前位置: task/understanding.py L1
  asks_search: "搜索" → True ✓
  asks_improve: "优化/提升/改进" → True (from "修改" signal)
  asks_analyze: 不存在! "分析" 不在任何 keyword 列表中
  
  影响: 缺少 ANALYZE 需求 → Orchestrator 无法选择 FULL_PIPELINE
```

## 缺失 4: 无复合任务检测

```
当前位置: task/understanding.py L1 _needs_l2()
  检测: AMBIGUOUS_VERBS + COMPOSITE_MARKERS + CROSS_REF_PATTERNS
  
  问题: "运营" + "要求：" + 多个子目标 → 这明显是一个复合任务，
        但 _needs_l2() 返回 False（没有 "然后/之后/同时/并且" 等标记）
        
  影响: L1 直接输出，不升级到 L2（LLM 可能正确理解）
```

# 3. 修复方案

## Fix 1: L1 新增识别信号 (~5 行)

```python
# _CREATE_WORDS 新增:
"运营",           # "帮我运营一个Java专题"
"策划",           # "策划一个内容专题"

# 新增 _ANALYZE_WORDS:
"分析", "总结", "归纳"
  → asks_analyze: bool → requirements 中添加 ANALYZE

# _REVISE_WORDS 不变，但增加上下文判断:
# "有则修改" ≠ 直接修改指令
# 通过检测 conditional pattern: "有则...无则..."
```

## Fix 2: L1 检测 conditional pattern (~10 行)

```python
_CONDITIONAL_PATTERN = re.compile(
    r"有则|无则|如果.{0,10}有|如果.{0,10}没有"
)

# 当检测到 conditional pattern 时:
# - 不触发 asks_revise（"有则修改"不是直接修改指令）
# - 转为 NEW_TASK + CREATE_CONTENT
```

## Fix 3: L1 增强 _needs_l2 触发 (~5 行)

```python
# 新增触发条件:
# 消息长度 > 80 字符 AND 包含 "要求" 或 "需求" → escalate to L2
# L2 更容易正确理解复合任务意图
```

## Fix 4: L1 添加 "分析" 到 requirement 生成 (~3 行)

```python
# _derive_requirements 新增:
if asks_analyze:
    reqs.append({"type": "ANALYZE"})
```

# 4. 修复后的预期调用链

```
用户消息 (同上)
    │
    ▼
TaskUnderstanding (L1, 修复后)
    ├── relation = NEW_TASK                ✅
    ├── goal_category = CREATE_CONTENT      ✅
    ├── requirements = [SEARCH, ANALYZE, CREATE, PUBLISH]  ✅
    └── resource_requests = [CREATE DRAFT, CREATE SCHEDULE]  ✅
    │
    ▼
Orchestrator._select_template([SEARCH, ANALYZE, CREATE, PUBLISH])
    ├── has_search + has_analyze + has_create + has_publish
    └── FULL_PIPELINE (5 steps) ✅
        SEARCH_COMMUNITY → ANALYZE_CONTENT_PATTERNS
        → GENERATE_CONTENT → VALIDATE_QUALITY → SCHEDULE_PUBLISH
    │
    ▼
Worker 执行 5 步 DAG ✅
```

# 5. 修改文件

| 文件 | 变更 |
|------|------|
| `task/understanding.py` | L1: +"运营"到_CREATE_WORDS; +_ANALYZE_WORDS; +conditional pattern检测; +_needs_l2触发条件; +_derive_requirements for ANALYZE |
| `tests/unit/` | +复杂运营场景 L1 测试 |

### 不修改

```
Orchestrator      — 零改动 (模板选择正确,问题在输入)
ResourceResolver  — 零改动
Worker            — 零改动
agent.py          — 零改动
```
