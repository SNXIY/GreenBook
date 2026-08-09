# Phase 6.5 — Human-in-the-Loop 设计

> 日期: 2026-08-07
> 状态: 设计阶段

---

# 1. 当前三种暂停机制分析

## 1.1 现状

| 类型 | 触发位置 | 状态 | 恢复方式 |
|------|---------|------|---------|
| Approval | `CapabilityExecutor` → `APPROVAL_REQUIRED` → Worker `pause_for_approval()` | Step: WAITING_APPROVAL, Execution: WAITING_APPROVAL | `Worker.resume_after_approval()` |
| Clarification | `ResourceResolver` → `needs_clarification` → `RuntimeAgentService._clarification_result()` | HTTP 202 + RuntimeResult(status=WAITING_APPROVAL) | **无恢复机制!** 用户重新发消息 |
| Input | **不存在** | — | — |

## 1.2 问题

1. **澄清无恢复** — 用户澄清后需要重新发消息，系统重新理解，可能产生不同的 Intent
2. **三种暂停不统一** — Approval 在 Worker 层，Clarification 在 Service 层，Input 不存在
3. **无超时机制** — 暂停后无限等待
4. **无交互审计** — 用户决策没有记录

---

# 2. 统一 HumanInteraction 设计

## 2.1 核心思路

```
任何需要用户决策的暂停 → HumanInteractionRequest
                          ├── interaction_id (唯一)
                          ├── type: APPROVAL | CLARIFICATION | INPUT
                          ├── execution_id (关联当前执行)
                          ├── question (问题描述)
                          ├── options (可选答案)
                          └── expires_at (超时时间)

用户响应 → HumanInteractionResponse
            ├── interaction_id
            ├── decision: ACCEPT | REJECT | SELECT | INPUT
            ├── selected_option (CLARIFICATION 的选择)
            └── content (INPUT 的自定义内容)
```

## 2.2 状态机

```
Execution 状态:
  RUNNING ──► WAITING_HUMAN ──► RUNNING    (用户响应后继续)
             (统一替代 WAITING_APPROVAL)

Step 状态 (不变):
  RUNNING ──► WAITING_APPROVAL ──► RUNNING  (保持兼容)

新增 HumanInteraction 状态:
  PENDING ──► EXPIRED (超时)
         ──► RESPONDED (用户响应)
```

### 状态映射

| 旧状态 | 新统一状态 |
|--------|----------|
| `ExecutionStatus.WAITING_APPROVAL` | `ExecutionStatus.WAITING_HUMAN` (新增, 兼容旧值) |
| `StepStatus.WAITING_APPROVAL` | 保留不变 (Worker 内部使用) |
| (不存在) | `HumanInteractionStatus.PENDING/RESPONDED/EXPIRED` |

## 2.3 数据模型

```python
class InteractionType(StrEnum):
    APPROVAL = "APPROVAL"           # 工具执行确认
    CLARIFICATION = "CLARIFICATION"  # 歧义澄清
    INPUT = "INPUT"                 # 用户输入 (如发布平台选择)

class HumanInteractionRequest(BaseModel):
    """用户决策请求."""
    interaction_id: str
    execution_id: str               # 关联执行
    task_id: str = ""
    type: InteractionType
    question: str                   # "找到2个匹配任务,请选择"
    options: list[dict] = []        # [{value: "task-a", label: "Java文章"}]
    context: dict = {}              # 前端渲染需要的额外信息
    expires_at: str = ""            # ISO datetime, 超时后自动过期
    created_at: str = ""

class HumanInteractionResponse(BaseModel):
    """用户决策响应."""
    interaction_id: str
    decision: str = ""              # ACCEPT | REJECT | SELECT | INPUT
    selected_value: str = ""        # CLARIFICATION: 选中的 option value
    content: str = ""               # INPUT: 用户自定义内容
    responded_at: str = ""
```

## 2.4 HumanInteractionManager

```python
class HumanInteractionManager:
    """管理暂停/恢复的完整生命周期."""

    def __init__(self, store: InteractionStore):
        self._store = store

    async def pause(
        self, execution_id: str, request: HumanInteractionRequest,
    ) -> None:
        """暂停执行, 等待用户响应."""
        await self._store.save(request)
        # Execution 状态由 StateManager 更新

    async def resume(
        self, interaction_id: str, response: HumanInteractionResponse,
    ) -> HumanInteractionRequest | None:
        """用户响应后恢复执行."""
        request = await self._store.find_by_id(interaction_id)
        if request is None or self._is_expired(request):
            return None
        # 注入响应数据到 Execution context
        return request

    async def expire_stale(self) -> list[str]:
        """清理超时的交互请求."""
        ...
```

## 2.5 与现有模块集成

### 类型 1: APPROVAL (现有 → 统一)

```
CapabilityExecutor → result.code == "APPROVAL_REQUIRED"
  │
  ▼
Worker._execute_one_step()
  │
  ├── [旧] result.approval_required → Worker.pause_for_approval()
  ├── [新] HumanInteractionManager.pause(
  │         type=APPROVAL,
  │         question="确认立即发布?",
  │         options=[{value:"ACCEPT",label:"确认发布"}])
  │
  └── ExecutionStatus → WAITING_HUMAN

用户响应:
  POST /interactions/{id}/respond {"decision": "ACCEPT"}
  → HumanInteractionManager.resume(id, response)
  → Worker.approve_and_resume(execution_id, step_id)
  → 继续执行
```

### 类型 2: CLARIFICATION (新统一)

```
ResourceResolver → needs_clarification=True
  │
  ▼
RuntimeAgentService._execute_single()
  │
  ├── [旧] return _clarification_result(ctx, resolution)  → RuntimeResult(AMBIGUOUS)
  ├── [新] HumanInteractionManager.pause(
  │         type=CLARIFICATION,
  │         question="找到2个匹配任务,请选择:",
  │         options=[
  │           {value:"task-a", label:"Java文章"},
  │           {value:"task-b", label:"Python文章"}
  │         ])
  │
  └── RuntimeResult(status=WAITING_HUMAN, interaction_id=...)

用户响应:
  POST /interactions/{id}/respond {"decision": "SELECT", "selected_value": "task-a"}
  → HumanInteractionManager.resume()
  → 将 task-a 注入 ctx.task_id
  → 继续执行 ResourceResolver → Orchestrator → Worker
```

### 类型 3: INPUT (新增)

```
场景: 用户说"发布文章"但没有指定平台
  → Orchestrator 检测到需要 PUBLISH 但缺少 platform 参数
  → HumanInteractionManager.pause(
       type=INPUT,
       question="请选择发布平台:",
       options=[
         {value:"greenbook", label:"GreenBook社区"},
         {value:"wechat", label:"微信公众号"}
       ])

用户响应:
  POST /interactions/{id}/respond {"decision": "INPUT", "content": "发布到GreenBook和微信公众号"}
  → 注入到 step.constraints["platform"]
  → 继续执行
```

---

# 3. ExecutionStatus 兼容方案

## 3.1 新增 WAITING_HUMAN

```python
class ExecutionStatus(StrEnum):
    # ... existing ...
    WAITING_APPROVAL = "WAITING_APPROVAL"    # 保留 (向后兼容)
    WAITING_HUMAN = "WAITING_HUMAN"          # 新增 (统一状态)
```

## 3.2 过渡策略

```
Phase 6.5:
  → 新增 WAITING_HUMAN
  → APPROVAL + CLARIFICATION 统一走 WAITING_HUMAN
  → 旧 WAITING_APPROVAL 保留 (worker/state_manager 不修改)

Phase 7:
  → 移除 WAITING_APPROVAL
  → Worker 改为使用 WAITING_HUMAN
```

---

# 4. 修改文件

| 操作 | 文件 | 变更 |
|------|------|------|
| **新增** | `packages/assistant_core/greenbook_assistant_core/human/models.py` | HumanInteractionRequest, Response, InteractionType |
| **新增** | `packages/assistant_core/greenbook_assistant_core/human/store.py` | InteractionStore (in-memory) |
| **新增** | `packages/assistant_core/greenbook_assistant_core/human/manager.py` | HumanInteractionManager |
| **修改** | `execution/models.py` | ExecutionStatus +WAITING_HUMAN |
| **修改** | `services/runtime_agent_service.py` | clarification → pause代替直接返回 |
| **新增** | `tests/unit/test_human_interaction.py` | 测试 |

### 不修改

```
Worker 核心             — 零改动 (StepStatus 不变)
ToolRuntime             — 零改动
agent.py                — 零改动
MCP / Java / Creator    — 零改动
```

---

# 5. 测试案例

| # | 场景 | 期望 |
|---|------|------|
| 1 | APPROVAL: publish_now 触发暂停 → 用户 ACCEPT → 继续执行 | publish_now 成功调用 |
| 2 | CLARIFICATION: 两个匹配任务 → 暂停 → 用户 SELECT task-a → 继续 | task-a 被选中 |
| 3 | INPUT: 缺少平台 → 暂停 → 用户输入 "公众号" → 继续 | platform 注入 |
| 4 | 超时: 暂停后 5 分钟未响应 → 自动过期 | status=EXPIRED |
| 5 | 旧 WAITING_APPROVAL 仍然兼容 | 现有测试全部通过 |

---

# 6. 实施步骤

| Step | 内容 |
|------|------|
| 1 | `human/models.py` — 数据模型 |
| 2 | `human/store.py` — 存储层 |
| 3 | `human/manager.py` — 暂停/恢复逻辑 |
| 4 | `execution/models.py` — +WAITING_HUMAN |
| 5 | `runtime_agent_service.py` — clarification 改用 pause |
| 6 | 测试 (5 cases) |
