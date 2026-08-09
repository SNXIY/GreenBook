# GreenBook Agent Runtime v1 — Architecture Review

> 日期: 2026-08-07
> 角色: 架构审查
> 审查对象:
>   - `docs/reports/greenbook-agent-runtime-v1-implementation-plan.md`
>   - `docs/reports/greenbook-agent-runtime-migration-roadmap.md`
>
> 结论: 方向正确，细节需要简化。核心问题是 v1 方案试图一次设计"通用 Agent Runtime"，
> 而实际需要的是"社区运营任务的专用编排层"。

---

# 1. 过度设计审查

## 1.1 v1 方案模块清单

```
v1 方案新增 7 个子包:
  task/          — models, understanding, registry
  planning/      — capability, planner
  execution/     — models, engine, mapper
  db/            — connection, repositories
```

## 1.2 逐模块审查

| 模块 | v1 推荐 | 审查结论 | 理由 |
|------|---------|---------|------|
| `task/models.py` | ✅ 必须 | ✅ **保留** — 精简 | Task + TaskIntent + ArtifactRef 是核心数据结构，无替代方案 |
| `task/understanding.py` | ✅ 必须 | ✅ **保留** — 精简 | 关键词无法理解"优化一下"/"提升质量"等语义变体。但 L2 prompt 可以精简到 ~80 行 |
| `task/registry.py` | ✅ 必须 | ✅ **保留** — 大幅精简 | "刚才那个文章"的多任务匹配是刚需。但 5 级匹配过重，3 级足够 |
| `planning/capability.py` | ✅ 必须 | ⚠️ **合并** → planner.py | Capability 模型只有 20 行，独立文件意义不大。合并到 planner.py |
| `planning/planner.py` | ✅ 必须 | ✅ **保留** — 但改名+简化 | 不应叫"通用 Planner"。应叫 `task_orchestrator.py`，因为它的职责是编排社区任务的固定模式 |
| `execution/models.py` | ✅ 必须 | ⚠️ **合并** → engine.py | Step 和 StepStatus 只有 ~50 行，合并到 engine.py |
| `execution/engine.py` | ✅ 必须 | ⚠️ **大幅简化** | 不需要通用 DAG executor。社区任务只有 3 种 DAG 模式（2步/3步/5步），用专用编排器更简单 |
| `execution/mapper.py` | ❓ 可商榷 | 🗑️ **删除** — 内联到 engine.py | Capability→Tool 映射只有 ~80 行逻辑，不值得独立文件 |
| `db/connection.py` | ✅ 必须 | ✅ **保留** | PostgreSQL 连接池是基础设施 |
| `db/repositories.py` | ✅ 必须 | ✅ **保留** — 但可以推迟 | Phase 0 的内存→DB 迁移可以用简单的 asyncpg 直接调用。Repository 模式可以 Phase 5 再做 |

## 1.3 简化后的目录结构

```
packages/assistant_core/greenbook_assistant_core/
├── __init__.py
├── agent.py                       # [精简到 ~150 行]
├── context.py                     # [精简: 移除 active_*_id]
├── memory.py                      # [DB-backed 实现]
├── time_parser.py                 # [保留]
├── middleware.py                  # [保留]
├── prompts/
│   └── system.py
│
├── task/                          # 3 个文件（不是 3 个 = 3 个）
│   ├── __init__.py
│   ├── models.py                  # Task, TaskIntent, TaskStatus, ArtifactRef
│   ├── understanding.py           # TaskUnderstanding (L1 + L2)
│   └── registry.py                # TaskRegistry (3 级匹配, ~150 行)
│
├── orchestration/                 # 1 个子包（不是 2 个）
│   ├── __init__.py
│   └── orchestrator.py            # Capability 模型 + Planner + 专用 DAG 执行
│                                  # 三者合一，~350 行
│
└── db/                            # 2 个文件
    ├── __init__.py
    └── connection.py              # PostgreSQL 连接池 (~40 行)
    # repositories.py 推迟到 Phase 5
```

**对比：**

| | v1 方案 | 简化后 |
|------|---------|--------|
| 新增子包 | 4 个 (task, planning, execution, db) | 3 个 (task, orchestration, db) |
| 新增文件 | 11 个 | 7 个 |
| 预计新增代码 | ~1,530 行 | ~900 行 |
| 核心抽象 | 通用 Agent Runtime | 社区任务编排层 |

---

# 2. Task 模型设计审查

## 2.1 五者关系

```
Conversation  ── 最外层容器，生命周期最长
  │              创建: POST /conversations
  │              结束: 用户关闭 / 30天无活动归档
  │
  ├── Message[]  ── 对话记录
  │
  ├── Run[]      ── 一次 POST /messages 的执行记录
  │    │            创建: 每次用户发送消息
  │    │            结束: LLM 返回最终响应 或 异常
  │    │
  │    └── 产生/操作 Task[]
  │
  └── Task[]     ── 用户的长期目标
       │            创建: Run 中识别到新目标时
       │            结束: COMPLETED / FAILED / CANCELLED
       │            生命周期: 可跨多次 Run（多轮对话）
       │
       ├── Step[]    ── Task 执行计划中的一个步骤
       │    │            创建: Planner 输出 CapabilityDAG 时
       │    │            结束: COMPLETED / FAILED / SKIPPED
       │    │            生命周期: 存在于一次 Run 内
       │    │
       │    └── 产生 Artifact[]
       │
       └── Artifact[] ── 步骤产生的数据
                        创建: Step COMPLETED 时
                        结束: 不可变，追加
```

## 2.2 生命周期定义

### Conversation
```
创建条件: POST /conversations (显式)
结束条件: 用户关闭 / 30天无活动
状态: ACTIVE → ARCHIVED

与 Task 关系: 1:N
一个 Conversation 可以有多个 Task。
用户说"帮我搜索Java帖子" → Task A
用户说"分析一下Python趋势" → Task B
两个 Task 都在同一个 Conversation 中。
```

### Task
```
创建条件:
  1. 用户表达了明确目标 (goal_category != DIRECT)
  2. 目标不能在已有 Task 中完成 (不是 CONTINUE/MODIFY)

结束条件:
  1. COMPLETED — 所有 Step 成功执行，目标达成
  2. FAILED — 不可恢复的错误
  3. CANCELLED — 用户主动取消
  4. SUPERSEDED — 被新 Task 替代（如"重写那篇文章"）

与 Run 关系: 1:N
一个 Task 可能跨多次 Run:
  Run 1: "创建Java文章" → Task A 创建，status=IN_PROGRESS
  Run 2: "修改标题" → Task A 继续，MODIFY_TASK
  Run 3: "明天发布" → Task A 继续，MODIFY_TASK → status=COMPLETED

与 Conversation 关系: N:1
```

### Run
```
创建条件: POST /conversations/{id}/messages (每次用户发消息)
结束条件:
  1. LLM 返回最终响应 (无 tool_calls)
  2. Tool 调用失败 (不可重试)
  3. 达到 max_tool_rounds
  4. 异常

与 Task 关系: N:1 或 N:M
  大部分 Run 操作 1 个 Task
  少数 Run 跨 Task (如"把搜索结果加入文章" → 操作 Task A 和 Task B)

注意：Run 不是 Task 的子对象。Run 是执行维度，Task 是目标维度。
同一个 Run 可能:
  - 创建 1 个新 Task
  - 继续 1 个已有 Task
  - 修改 1 个已有 Task
  - 查询/取消 1 个已有 Task
  - 不操作任何 Task (DIRECT 简单问答)
```

### Step
```
创建条件: Planner 输出 CapabilityDAG 时 (Phase 3+)
  Step 由 Execution Engine 初始化

结束条件:
  1. COMPLETED — 工具执行成功
  2. FAILED — 不可重试的错误
  3. SKIPPED — 上游 Step 失败

与 Run 关系: N:1
  所有 Step 在一次 Run 中执行完成

与 Task 关系: N:1
  所有 Step 属于一个 Task

注意：Step 是可选的。
  大部分简单操作不需要 Step（如"列出我的草稿"）
  只有 Planner 产生 DAG 时才有 Step
```

### Artifact
```
创建条件: Step COMPLETED 且产生了有意义的数据
  或: 旧路径中 tool_handler 回调成功时 (旁路记录)

生命周期: 不可变，追加到 Task.artifacts

类型:
  SEARCH_RESULT   — 社区搜索结果
  DRAFT           — AI 生成的草稿
  ANALYSIS_REPORT — LLM 分析报告
  SCHEDULE        — 定时发布记录
  SCHEDULE_UPDATE — 定时修改记录
  VALIDATION_REPORT — 质量校验报告

注意：Artifact 不是 Step 的必要产物。
  纯查询步骤（get_post, list_drafts）不产生 Artifact
  纯 LLM 推理步骤（ANALYZE_PATTERNS）产生 Artifact
```

## 2.3 多轮引用策略

```
场景: 跨轮次引用

轮次1: "创建一篇Java文章"
  → Run 1 创建 Task A (DRAFT artifact: draft_123)

轮次2: "修改刚才文章标题"
  → Run 2: TaskUnderstanding.understand()
      → L1: "修改" → 需要找到目标
      → TaskRegistry.resolve_task() 匹配流程:
          1. "刚才" → 时间最近 → Task A (updated_at 最近)
          2. "文章" → 有 DRAFT artifact → 确认 Task A 是内容任务
      → TaskIntent(relation=MODIFY_TASK, target_task_id=task_A.id)
  → agent.run() 中: task = Task A
  → mapper 从 Task A.artifacts 中找到最新 DRAFT → draft_123

轮次3: "分析最近Java热门帖子"
  → Run 3 创建 Task B (SEARCH_RESULT + ANALYSIS_REPORT artifacts)

轮次4: "把分析结果加入刚才文章"
  → Run 4: TaskUnderstanding.understand()
      → "刚才文章" → 匹配 Task A (有 DRAFT artifact)
      → "分析结果" → 匹配 Task B (有 ANALYSIS_REPORT artifact)
      → TaskIntent(relation=MODIFY_TASK, target_task_id=task_A.id,
                    references=[EntityHint(kind="ARTIFACT", label="分析结果")])
  → Task A 执行时注入 Task B 的 ANALYSIS_REPORT 作为 reference
```

**关键设计：Task 匹配不依赖 active_*_id，而是依赖 Task 的 artifacts 列表和更新时间。**

---

# 3. Intent 理解设计审查

## 3.1 核心问题回顾

```
问题1: 表达变化
  "参考优秀文章优化一下" → 关键词 "优化" 不在任何列表中
  "借鉴热门内容重新整理" → 关键词 "整理" 不在任何列表中
  "提升文章质量" → 关键词 "提升" 不在任何列表中
  → 都应识别为 IMPROVE_CONTENT

问题2: 多任务混淆
  "刚才那个文章" → 哪个 Task？draft_123 还是 draft_456？
  → 需要 Task 级别的匹配

问题3: 找不到目标
  "取消定时发布" → 哪个 schedule？
  "修改发布时间" → 当前 session 没有 active_schedule
  → 需要从 Task 的 artifacts 中查找
```

## 3.2 L1 规则（确定性，保留为快速路径）

```python
# L1 覆盖的场景（约 60% 的请求）:

# 明确的单步操作 — 不需要 LLM
L1_RULES = {
    # 查询类
    "列出我的草稿":       DIRECT + goal_category=QUERY_INFO,
    "查看定时任务":       DIRECT + goal_category=QUERY_INFO,
    "搜索社区Java帖子":   DIRECT + goal_category=ANALYZE_COMMUNITY,
    "这个帖子有多少评论": DIRECT + goal_category=QUERY_INFO,

    # 取消类
    "取消定时发布":       CANCEL_TASK + goal_category=MANAGE_SCHEDULE,
    "撤销刚才的任务":     CANCEL_TASK,

    # 发布类
    "立即发布":           CONTINUE_TASK + goal_category=PUBLISH_CONTENT,
    "现在发布这篇文章":   CONTINUE_TASK + goal_category=PUBLISH_CONTENT,
}

# L1 的判断基准：
# 1. 不含模糊词（"优化"、"提升"、"整理"、"参考"）
# 2. 不含复合信号（"然后"、"之后"、"同时"、"并且"）
# 3. 操作目标明确（"草稿"、"定时任务"、"帖子"）

def _needs_l2(user_message: str) -> bool:
    """是否需要升级到 L2 LLM 理解"""
    return any([
        _has_ambiguous_intent(user_message),    # "优化一下"、"提升质量"
        _has_composite_signal(user_message),    # "搜索...然后...再..."
        _has_cross_reference(user_message),     # "把...结果加入..."
        _has_vague_target(user_message),        # "刚才那个"（且无 active_* 可匹配）
    ])
```

## 3.3 L2 LLM 深度理解（约 20-30% 的请求）

```python
# L2 的关键设计:

# 1. Prompt 极简（~200 tokens，不是 500+）
L2_SYSTEM_PROMPT = """你是GreenBook社区助手的意图理解模块。

分析用户消息，输出JSON。

## 任务类别
CREATE_CONTENT | IMPROVE_CONTENT | ANALYZE_COMMUNITY |
PUBLISH_CONTENT | MANAGE_SCHEDULE | INTERACT | QUERY_INFO

## 关系
NEW_TASK | CONTINUE_TASK | MODIFY_TASK | CANCEL_TASK | DIRECT

## 已有任务
{existing_tasks_context}

## 规则
- "参考/借鉴/根据...优化/改进/提升" → IMPROVE_CONTENT + MODIFY_TASK
- "搜索...分析...生成..." → ANALYZE_COMMUNITY 或 CREATE_CONTENT (看最终目标)
- "刚才/上次/之前" → 匹配已有任务
- 明确提到发布 → PUBLISH_CONTENT

输出JSON: {goal_category, relation, target_task_hint, goal}
"""

# 2. Few-shot 只覆盖社区场景（3 个例子，不是 5 个）
FEW_SHOT_EXAMPLES = [
    {
        "input": "帮我参考热门Java帖子优化一下刚才的文章",
        "output": {
            "goal_category": "IMPROVE_CONTENT",
            "relation": "MODIFY_TASK",
            "target_task_hint": "刚才的文章",
            "goal": "参考社区热门Java帖子，改进当前草稿内容质量"
        }
    },
    {
        "input": "搜索社区Java文章，分析热门原因，然后生成一篇新文章并在五分钟后发布",
        "output": {
            "goal_category": "CREATE_CONTENT",
            "relation": "NEW_TASK",
            "goal": "基于社区Java热门文章分析，创作新文章并定时发布"
        }
    },
    {
        "input": "把刚才搜索结果加到文章里",
        "output": {
            "goal_category": "IMPROVE_CONTENT",
            "relation": "MODIFY_TASK",
            "target_task_hint": "有搜索结果的任务",
            "goal": "将搜索结果作为参考注入文章改进"
        }
    },
]
```

## 3.4 Fallback 策略

```
L2 LLM 调用失败时的降级链:

L2 返回了无效 JSON
  → Pydantic ValidationError
    → 尝试 JSON 修复（去掉 markdown code block 标记、修复常见错误）
      → 修复成功: 使用修复后的 TaskIntent
      → 修复失败: 回退到 L1 规则

L2 超时 (5s)
  → 回退到 L1 规则

L2 API 不可用
  → 回退到 L1 规则

终极 Fallback (L1 也无法判断):
  → TaskIntent(relation=DIRECT, goal_category=QUERY_INFO)
  → 不创建 Task，走旧路径直接 LLM tool calling
  → 用户体验: 系统仍然能工作（只是不会创建结构化 Task）
```

**关键原则: Fallback 不阻断用户操作。最坏情况下退化为当前 LLM + Tool Calling 模式。**

---

# 4. Planner 设计审查

## 4.1 Planner Router — 哪些任务需要规划

```
                    POST /messages
                         │
                    TaskUnderstanding
                         │
                    TaskIntent
                         │
              ┌──────────┴──────────┐
              │   Planner Router    │
              │                     │
              │ 判断: 需要规划吗?    │
              └──────────┬──────────┘
                         │
          ┌──────────────┼──────────────┐
          │              │              │
     DIRECT          SIMPLE          PLANNED
   (无 Task)       (单步 Task)     (多步 Task)
          │              │              │
          ▼              ▼              ▼
    直接 LLM       旧路径直接       Planner
    tool call      tool call       ↓
                 (已有逻辑)    CapabilityDAG
                                   ↓
                             Execution Engine
```

### 判断规则（确定性，不用 LLM）

```python
def route(task_intent: TaskIntent, task: Task | None) -> ExecutionMode:
    """判断 Task 的执行模式"""

    # DIRECT: 简单问答，不需要 Task
    if task_intent.relation == "DIRECT":
        return ExecutionMode.DIRECT

    # PLANNED: 以下条件任一满足时需要 Planner
    if _needs_planning(task_intent):
        return ExecutionMode.PLANNED

    # SIMPLE: 单步操作，不需要 Planner
    return ExecutionMode.SIMPLE

def _needs_planning(task_intent: TaskIntent) -> bool:
    """判断是否需要 Planner 生成多步计划"""

    # 条件1: 3个以上 Requirement
    if len(task_intent.requirements) >= 3:
        return True

    # 条件2: 包含 ANALYZE 类步骤
    #   "分析写作方式" → 需要中间产物 → 需要 Plan
    if any(r.type == "ANALYZE" for r in task_intent.requirements):
        return True

    # 条件3: 跨 Task 引用
    #   "把搜索结果加入文章" → Task A 引用 Task B 的产物
    if task_intent.target_entity_refs and any(
        ref.kind in ("ARTIFACT", "TASK") for ref in task_intent.target_entity_refs
    ):
        return True

    # 条件4: 包含发布步骤
    #   "创建并发布" → 需要 VALIDATE → PUBLISH 的顺序约束
    if any(r.type == "PUBLISH" for r in task_intent.requirements):
        return True

    return False
```

**实际数据预估：**
- DIRECT: ~40% 请求（"Java是什么"、"列出草稿"）
- SIMPLE: ~40% 请求（"创建Java文章"、"修改标题"、"搜索帖子"）
- PLANNED: ~20% 请求（"搜索→分析→创建→发布"复合任务）

## 4.2 Planner 输入 → 输出（精简版）

```python
# Planner 不做通用规划。它做"社区任务编排"。
# 社区任务只有 3 种 DAG 模板:

COMMUNITY_TASK_TEMPLATES = {
    # 模板1: 研究型创作 (SEARCH → ANALYZE → CREATE)
    "CREATE_WITH_RESEARCH": {
        "when": "task_intent.goal_category == CREATE_CONTENT "
                "and task_intent has SEARCH + ANALYZE requirements",
        "dag": [
            ("step-1", "SEARCH_COMMUNITY",   []),
            ("step-2", "ANALYZE_CONTENT",    ["step-1"]),
            ("step-3", "GENERATE_CONTENT",   ["step-2"]),
        ],
    },

    # 模板2: 创作+发布 (CREATE → VALIDATE → PUBLISH)
    "CREATE_AND_PUBLISH": {
        "when": "task_intent.goal_category == CREATE_CONTENT "
                "and task_intent has PUBLISH requirement",
        "dag": [
            ("step-1", "GENERATE_CONTENT",   []),
            ("step-2", "VALIDATE_QUALITY",   ["step-1"]),
            ("step-3", "SCHEDULE_PUBLISH",   ["step-2"]),
        ],
    },

    # 模板3: 完整流程 (SEARCH → ANALYZE → CREATE → VALIDATE → PUBLISH)
    "FULL_CREATION_PIPELINE": {
        "when": "task_intent.goal_category == CREATE_CONTENT "
                "and task_intent has SEARCH + ANALYZE + PUBLISH requirements",
        "dag": [
            ("step-1", "SEARCH_COMMUNITY",   []),
            ("step-2", "ANALYZE_CONTENT",    ["step-1"]),
            ("step-3", "GENERATE_CONTENT",   ["step-2"]),
            ("step-4", "VALIDATE_QUALITY",   ["step-3"]),
            ("step-5", "SCHEDULE_PUBLISH",   ["step-4"]),
        ],
    },
}

# 当模板不匹配时（边缘场景），才用 LLM 生成 DAG
# LLM Planner 此时只作为 fallback
```

**关键简化：Planner 不是通用 AI Planner，而是社区任务模板选择器 + LLM fallback。**

---

# 5. Execution Engine 审查

## 5.1 三种方案对比

| 维度 | 自研 DAG (v1 方案) | LangGraph | 专用编排器 (推荐) |
|------|-------------------|-----------|-----------------|
| 代码量 | ~300 行 engine + ~150 行 mapper | 0 (框架) + ~100 行配置 | ~200 行 |
| 依赖 | 无 | langgraph + langchain | 无 |
| 灵活性 | 通用 DAG | 通用 StateGraph | 3 种社区任务模板 |
| 调试难度 | 中 (自定义状态机) | 低 (框架有 tracing) | 低 (模板是声明式的) |
| Checkpoint | 自实现 | 框架内置 | 自实现 (~30 行) |
| 对现有代码影响 | 大 (新增执行路径) | 中 (替换 LLM 循环) | 小 (增量扩展) |

## 5.2 推荐: 专用编排器

**理由:**

1. 社区任务只有 3 种 DAG 模板。模板覆盖 95% 的多步场景。通用 DAG 执行器的灵活性在此场景下是浪费。

2. 模板是声明式的 → 可测试、可审计、可调试。通用 DAG 执行器需要模拟 LLM 输出才能测试。

3. 模板选择是确定性的 → 不依赖 LLM（除非模板都不匹配）。

4. Checkpoint 对 3-5 步的 DAG 来说很简单：每个 Step 完成后存一次 DB。不需要通用 Checkpoint 机制。

5. 不引入 LangGraph 依赖 → 保持当前系统的简单性和可调试性。

## 5.3 专用编排器设计

```python
# orchestration/orchestrator.py (~200 行)

class TaskOrchestrator:
    """社区任务编排器 — 按模板执行多步任务"""

    # 模板库（声明式）
    TEMPLATES: dict[str, list[tuple[str, str, list[str]]]]
    # 格式: {template_name: [(step_id, capability, [dependencies]), ...]}

    async def execute(self, task: Task, mcp, llm, session) -> Task:
        """
        1. 模板匹配
        2. 按拓扑顺序执行 Step
        3. Capability → Tool 映射
        4. Artifact 提取和保存
        5. Checkpoint (每步完成后)
        """

    async def _execute_step(self, step, task, mcp, llm, session):
        """执行单步:
        - 如果 capability.is_llm_step → LLM 推理
        - 否则 → mcp.execute_tool(tool_name, ...)
        - 成功后 → 提取 artifact → 保存到 task
        - 失败 → 判断 retryable → 重试 or 标记失败
        """

    def _select_template(self, task: Task) -> str | None:
        """根据 Task 的 requirements + constraints 匹配模板"""
        # 匹配规则: requirements 的类型集合 + PUBLISH 约束
        # 返回: template_name 或 None (需要 LLM fallback)

    def _build_step_args(self, step, task, completed_steps):
        """从 task.requirements + constraints + 上游 artifacts 构建工具参数"""
        # 这就是原 mapper.py 的逻辑，内联到这里
```

## 5.4 为什么不是 LangGraph

1. **当前系统已经稳定运行。** MCP 调用链清晰，工具 handler 质量高。引入 LangGraph 需要重写整个 LLM 循环 → 风险高、收益低。

2. **LangGraph 的优势（复杂条件分支、子图、动态路由）在此场景不需要。** 社区任务的 DAG 是线性的或简单分支的，拓扑顺序执行就够了。

3. **依赖成本。** LangGraph + LangChain 增加 ~50MB 依赖。当前系统只有 openai + httpx + fastapi + asyncpg → 极简。

4. **调试。** 自研 200 行代码的调试难度远低于框架的黑盒行为。

---

# 6. 最终推荐架构

## 6.1 简化后的核心链路

```
POST /conversations/{id}/messages
    │
    ▼
routes.py: send_message()                     # [精简后 ~700 行]
    │
    ├── Auth → SessionContext
    ├── History → messages[]
    │
    ├── TaskUnderstanding.understand()        # [新增]
    │   ├── L1: 规则匹配 (~60% 请求)
    │   ├── L2: LLM 理解 (~20% 请求)
    │   └── Fallback: L1 兜底
    │   → TaskIntent
    │
    ├── TaskRegistry.resolve_or_create()      # [新增]
    │   ├── 匹配已有 Task (3 级策略)
    │   └── 或创建新 Task
    │   → Task
    │
    └── agent.run(user_message, session, task)
        │
        ├── PlannerRouter.route(task_intent)  # [新增]
        │   ├── DIRECT  → 直接 LLM 回答
        │   ├── SIMPLE  → 旧路径 tool calling
        │   └── PLANNED → TaskOrchestrator
        │
        ├── TaskOrchestrator.execute()        # [新增·PLANNED 路径]
        │   ├── 模板匹配 (3 种模板)
        │   ├── 按拓扑执行 Step
        │   └── Checkpoint 每步
        │
        └── (SIMPLE 路径: 保留现有 LLM 循环)  # [保留]
            └── tool_handler → MCP → Java/Creator
```

## 6.2 新旧对比

```
旧架构问题:
  ┌─────────────────────────────────────────────┐
  │ 1. 意图理解: 中文关键词正则                   │
  │    "优化一下" → 匹配不到任何关键词 → 退化     │
  │                                              │
  │ 2. 多任务管理: session.active_draft_id       │
  │    只能存一个草稿，无法跨任务引用              │
  │                                              │
  │ 3. 多步编排: agent.py if-else 门控           │
  │    只支持 3 种固定组合，不支持分析/校验步骤    │
  │                                              │
  │ 4. 执行管理: 内存 events[] 列表               │
  │    无 Checkpoint、无恢复、崩溃丢失            │
  │                                              │
  │ 5. 存储: 进程内存 dict                        │
  │    重启丢失全部数据                           │
  └─────────────────────────────────────────────┘

新架构方案:
  ┌─────────────────────────────────────────────┐
  │ 1. TaskUnderstanding (L1 规则 + L2 LLM)      │
  │    统一语义归类，fallback 不阻断              │
  │                                              │
  │ 2. TaskRegistry (3 级匹配)                   │
  │    Task 是一等公民，支持多任务和跨任务引用     │
  │                                              │
  │ 3. TaskOrchestrator (3 种模板)               │
  │    声明式 DAG 模板 + LLM fallback             │
  │                                              │
  │ 4. Step Checkpoint (每步 DB 持久化)           │
  │    支持崩溃恢复，不重复执行                    │
  │                                              │
  │ 5. PostgreSQL (asyncpg)                      │
  │    持久化 Conversation/Task/Step/Artifact     │
  └─────────────────────────────────────────────┘
```

## 6.3 关键简化决策

| v1 方案 | 简化后 | 理由 |
|---------|--------|------|
| 通用 Planner (LLM 每次都生成 DAG) | 模板优先 + LLM fallback | 社区任务的 DAG 模式是有限的，声明式模板更可靠 |
| 通用 DAG Execution Engine | 专用 TaskOrchestrator | 3 种模板覆盖 95% 场景，不需要通用 DAG 执行器 |
| Capability 与 Planner 分离 | 合并到 orchestrator.py | 紧密耦合，分开无意义 |
| CapabilityToolMapper 独立模块 | 内联到 orchestrator.py | ~80 行映射逻辑不配独立文件 |
| 5 级 Task 匹配 | 3 级 Task 匹配 | 标签匹配 + 实体匹配 + 最近活跃 已覆盖所有场景 |
| 独立 repositories.py | 推迟到 Phase 5 | 先用简单 asyncpg 查询，够用 |

---

# 7. 分阶段计划（修订版）

## Phase 0: 持久化基础（3 天）

### 目标
将 4 个内存 dict 迁移到 PostgreSQL，零功能变更。

### 新增文件
```
packages/assistant_core/greenbook_assistant_core/db/__init__.py       (~5 行)
packages/assistant_core/greenbook_assistant_core/db/connection.py     (~40 行)
```

### 修改文件
```
apps/assistant_api/greenbook_assistant_api/main.py   (+15 行: DB 初始化)
apps/assistant_api/greenbook_assistant_api/api/routes.py (~80 行: dict → DB)
```

### DB 迁移
```sql
CREATE TABLE assistant_conversations (...);
CREATE TABLE assistant_messages (...);
CREATE TABLE assistant_runs (...);
CREATE TABLE assistant_approvals (...);
```

### 验收标准
- 现有 E2E 测试全部通过
- 重启后 conversation + message 不丢失

---

## Phase 1: Task 模型 + Registry + 旁路记录（4 天）

### 目标
引入 Task 概念。旧路径完整保留，新路径仅记录。

### 新增文件
```
packages/assistant_core/greenbook_assistant_core/task/__init__.py     (~5 行)
packages/assistant_core/greenbook_assistant_core/task/models.py       (~120 行)
packages/assistant_core/greenbook_assistant_core/task/registry.py     (~150 行)
```

### 修改文件
```
apps/assistant_api/greenbook_assistant_api/main.py    (+5 行: TaskRegistry 注入)
apps/assistant_api/greenbook_assistant_api/api/routes.py (+20 行: 旁路调用)
packages/assistant_core/greenbook_assistant_core/agent.py (+5 行: 接受 task 参数)
```

### DB 迁移
```sql
CREATE TABLE assistant_tasks (...);
CREATE TABLE assistant_artifacts (...);
```

### 验收标准
- 每轮对话自动创建 Task 记录到 DB
- 工具成功后 Task.artifacts 同步
- TaskRegistry.resolve_task() 正确匹配 "刚才那个"
- **现有功能零影响**

---

## Phase 2: Task Understanding（4 天）

### 目标
L1+L2 意图理解，替代 agent.py 关键词检测。

### 新增文件
```
packages/assistant_core/greenbook_assistant_core/task/understanding.py (~180 行)
```

### 修改文件
```
packages/assistant_core/greenbook_assistant_core/agent.py   (+15 行: 注入 TaskIntent 到 context)
```

### 验收标准
- "优化一下"、"提升质量"、"参考热门改写" → IMPROVE_CONTENT
- MODIFY_TASK 正确匹配已有 Task
- L2 失败时 fallback 到 L1
- 语义准确率 > 80%

---

## Phase 3: Task Orchestrator（5 天）

### 目标
引入多步任务模板编排。支持 search→analyze→create→validate→publish 完整流程。

### 新增文件
```
packages/assistant_core/greenbook_assistant_core/orchestration/__init__.py  (~5 行)
packages/assistant_core/greenbook_assistant_core/orchestration/orchestrator.py (~250 行)
# 包含: Capability 定义(30行) + 模板库(40行) + PlannerRouter(30行) + TaskOrchestrator(150行)
```

### 修改文件
```
packages/assistant_core/greenbook_assistant_core/agent.py   (+25 行: PlannerRouter 分流)
apps/assistant_api/greenbook_assistant_api/api/routes.py     (+10 行: 注入 TaskOrchestrator)
```

### DB 迁移
```sql
CREATE TABLE assistant_task_steps (...);
```

### 验收标准
- 简单任务（SIMPLE）走旧路径
- 多步任务（PLANNED）走 TaskOrchestrator
- 3 种模板全部可执行
- Step Checkpoint 持久化
- 模板都不匹配时 LLM fallback 生成 DAG
- 旧路径仍可用于 DIRECT 和 SIMPLE 模式

---

## Phase 4: 收敛（2 天）

### 目标
移除 agent.py 旧意图检测逻辑，统一到新架构。

### 修改文件
```
packages/assistant_core/greenbook_assistant_core/agent.py
  🗑️ 删除: _turn_intents() (~15行)
  🗑️ 删除: _turn_routing_hint() (~60行)
  🗑️ 删除: _turn_tool_filter() (~30行)
  🗑️ 删除: 顺序工具门控 if-else (~30行)
  ✂️ 保留: _simple_loop() (仅最简 LLM 循环)

packages/assistant_core/greenbook_assistant_core/context.py
  🗑️ 删除: active_draft_id, active_post_id, active_schedule_id
  ✂️ 保留: user_id, tenant_id, timezone, pending_approval
  🆕 新增: active_task_id (快速引用，非主索引)

apps/assistant_api/greenbook_assistant_api/api/routes.py
  ✂️ 简化: _build_tool_schemas() → 动态从 tool_registry 生成
  ✂️ 简化: tool_handler 回调 → 委托给 TaskOrchestrator (PLANNED) 或直接调用 (SIMPLE)
```

### 验收标准
- 全功能回归测试通过
- agent.py: 543 → ~150 行
- routes.py: 1238 → ~700 行
- 新增代码总量: ~800 行

---

# 8. 总结

## 8.1 核心结论

v1 方案方向正确，但存在"通用化过度"的问题。主要修正：

1. **从"通用 Agent Runtime"收敛到"社区任务编排层"** — 不追求通用性，专注社区场景
2. **从 LLM 驱动的 Planner 收敛到"模板优先 + LLM fallback"** — 声明式模板更可靠、更可测试
3. **从 11 个新增文件收敛到 7 个** — 合并耦合模块，移除不必要的抽象
4. **从 ~1,530 行新增代码收敛到 ~800 行** — 更少的代码 = 更少的 bug

## 8.2 不可妥协的设计

以下设计必须保留（否则无法解决核心问题）：

- Task 作为一等公民
- TaskIntent 结构化意图（L1 + L2）
- TaskRegistry 的 3 级匹配
- 多步任务的模板化编排
- Step 级别的 Checkpoint

## 8.3 可以推迟的设计

以下设计可以推迟到 Phase 4+:

- LLM 生成的 Planner（先用模板覆盖，边缘 case 再考虑 LLM）
- Repository 模式（先用简单 asyncpg 查询）
- 语义匹配（先用 label 子串 + 最近活跃，Phase 4 再加 LLM 语义匹配）
- 并发 Step 执行（所有社区任务的 DAG 都是线性的，无并行需求）
