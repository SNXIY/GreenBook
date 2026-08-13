> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.5-C Approval Migration

## 1. Approval 链路审计

当前 approval 链路存在两类请求：

- Runtime 请求由 `RuntimeAgentService` 创建，Human Interaction 使用 `execution_id`。
- Legacy 请求仍可仅使用 `run_id`，并通过 `RunExecutionAdapter` 查找对应 execution。

Runtime 暂停审批时，`ExecutionStateManager.pause_for_approval()` 产生唯一的 canonical `ExecutionEvent.APPROVAL_REQUIRED`。审批 API 不直接修改执行状态；统一决策服务在 Runtime approval 被接受或拒绝后调用 Runtime resume boundary。

涉及位置：

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py`: `assistant_approvals` repository 和 approval 持久化字段。
- `packages/assistant_core/greenbook_assistant_core/human/`: `HumanInteractionRequest` 使用 `execution_id`。
- `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py`: 创建 execution approval、暂停以及审批恢复。
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`: 暴露 Runtime approval resume boundary。
- `apps/assistant_api/greenbook_assistant_api/services/approval_service.py`: Legacy 与 Runtime 共用的 `ApprovalDecisionService`。
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`: approval、run approval 和 execution approval API 入口。

## 2. Approval 数据结构

Approval record 现在支持：

```text
approval_id
run_id          nullable
execution_id    nullable
status
payload         JSON
```

新 Runtime approval 必须保存 `execution_id`。Legacy-only approval 可以只保存 `run_id`。`assistant_approvals` 仍是 approval 记录存储，不是 Execution 状态源，也不保存复制的 ExecutionEvent 或 ExecutionStatus。

生产数据库中已存在的表不会被 `create_all()` 自动补列，因此新增字段需要后续独立数据库 migration；本阶段没有修改 migration，也没有删除或改写既有表。

## 3. 统一 API 边界

支持以下入口：

- `POST /api/v1/assistant/approvals/{approval_id}/approve`
- `POST /api/v1/assistant/approvals/{approval_id}/reject`
- `POST /api/v1/assistant/runs/{run_id}/approve`，Legacy compatibility 入口
- `POST /api/v1/assistant/executions/{execution_id}/approve`，canonical Runtime 入口

两个 ID 入口都解析到同一 approval record，并调用 `ApprovalDecisionService`。Runtime approval 的恢复通过 `AssistantService.resume_runtime_approval()` 进入既有 Runtime service；Legacy-only approval 只更新 approval 记录并保留旧行为。

状态变化不由 adapter 或 route 直接写入。Runtime 状态仍必须经由既有 Runtime 状态边界处理，approval service 不拥有第二套状态。

## 4. ID 与事件映射

```text
Legacy run_id
    -> RunExecutionAdapter
    -> execution_id
    -> ApprovalDecisionService
    -> Runtime resume boundary
```

Runtime approval 只产生 `ExecutionEvent.APPROVAL_REQUIRED` 作为 canonical approval event。没有向 `assistant_approvals.events` 或旧 Agent event stream 双写事件。Legacy API 查询时通过 adapter 定位同一 execution/event 语义。

## 5. 修改文件

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py`
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
- `apps/assistant_api/greenbook_assistant_api/services/approval_service.py`
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
- `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py`
- `tests/unit/test_approval_execution_reference.py`
- `docs/architecture/PHASE_11_5_C_APPROVAL_MIGRATION.md`

未修改 `Worker`、`Planner`、`ToolRuntime`、`ExecutionStateManager` 核心状态逻辑、`PlanExecution` schema 或 `StepExecution` schema；未删除 `run_id`、`assistant_approvals` 或 Legacy API。

## 6. 测试与验证

- `pytest -q tests/compat/runtime`: 21 passed
- `pytest -q tests/unit/test_approval_execution_reference.py`: 4 passed
- Python `compileall`: passed
- `git diff --check`: passed

## 7. 后续工作

下一阶段应提供正式数据库 migration，为既有 `assistant_approvals` 表增加 nullable `execution_id` 和 JSON `payload` 字段，并制定历史 approval 回填策略。在 migration 完成前，生产数据库 schema 变更不能视为已部署。
