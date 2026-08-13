# Phase Runtime Tool Contract Audit

## 审计范围

- 用户请求：`明天上午八点发布一篇关于如何学好 Java 的帖子`
- Execution：`abeccffe-cf38-4584-bceb-0f37f73dedb5`
- 观测结果：`GENERATE_CONTENT=FAILED`，后续 `VALIDATE_QUALITY`、`SCHEDULE_PUBLISH` 被跳过
- 本次只进行了源码读取和纯内存参数复算，没有启动服务、没有调用外部 Creator/Java API，也没有修改运行时代码

## 结论

失败发生在 GreenBook MCP 的执行前 handler 签名校验，而不是 Planner、PlanExecution 状态机、Worker、ToolRuntime 或 Creator Agent HTTP 接口。

当前链路实际是：

```text
IntentSpec
  -> TaskOrchestrator
  -> CREATE_AND_PUBLISH TaskPlan
  -> ArgumentBinder
  -> CapabilityExecutor
  -> ToolRuntime
  -> GreenBookMCPServer.execute_tool
  -> inspect.Signature.bind 失败
  -> PRE_EXECUTION_VALIDATION_FAILED
```

根因是同一个工具存在两个不一致的输入契约：

| 层 | 当前契约 |
| --- | --- |
| `CapabilityRegistry.GENERATE_CONTENT` | 必填 `title`、`content` |
| `ArgumentBinder` 在当前 MCP schema 缺失时的实际输出 | `title`、`content`、可选 `summary` |
| `content.create_draft` handler | 必填 `title`、`instruction`，可选 `references`、`summary` |
| Creator HTTP API | `kind`、`goal`、`constraints`、`source_scope` 等；由 MCP handler 将 `instruction` 映射为 `goal` |

因此本次调用在进入 Creator 之前就已经失败，符合“本次尚未执行任何修改”。

## 1. Planner 生成的 `GENERATE_CONTENT` step

### 模板和步骤依赖

`packages/assistant_core/greenbook_assistant_core/orchestration/templates.py:107-128` 的
`CREATE_AND_PUBLISH` 模板生成：

| ordinal | capability | description | depends_on | input | output |
| ---: | --- | --- | --- | --- | --- |
| 1 | `GENERATE_CONTENT` | Generate content based on user instructions | `[]` | `[]` | `DRAFT` |
| 2 | `VALIDATE_QUALITY` | Validate content quality... | 第 1 步 | `DRAFT` | `VALIDATION_REPORT` |
| 3 | `SCHEDULE_PUBLISH` | Schedule the validated draft... | 第 2 步 | `DRAFT` | `SCHEDULE` |

`TaskOrchestrator._select_template()` 在检测到 `CREATE + PUBLISH` 时选择
`CREATE_AND_PUBLISH`，该逻辑位于
`packages/assistant_core/greenbook_assistant_core/orchestration/orchestrator.py:136-146`。

对本次输入的确定性复算结果为：

```json
{
  "capability": "GENERATE_CONTENT",
  "description": "Generate content based on user instructions",
  "depends_on": [],
  "input_artifact_types": [],
  "output_artifact_type": "DRAFT",
  "constraints": {}
}
```

这一步本身没有错误。`TIME` 只会被 Planner 附加到 `SCHEDULE_PUBLISH`：

```json
{
  "capability": "SCHEDULE_PUBLISH",
  "constraints": {
    "time": "明天上午八点发布一篇关于如何学好 Java 的帖子"
  }
}
```

## 2. PlanExecution 保存的 tool 和 arguments

### PlanExecution 没有 `tool_name` 或 `arguments` 字段

`packages/assistant_core/greenbook_assistant_core/execution/models.py` 中：

- `PlanExecution` 只保存 `plan_id`、`task_id`、总体状态和 `steps`
- `StepExecution` 保存 `capability`、状态、依赖、产物和 `checkpoint_data`
- 没有正式的 `tool_name` 字段
- 没有正式的 `arguments` 字段

Runtime 在 `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py:1048-1061`
将已绑定参数放入：

```text
StepExecution.checkpoint_data["constraints"]
```

因此本次第 1 步在执行前可重建为：

```json
{
  "capability": "GENERATE_CONTENT",
  "checkpoint_data": {
    "constraints": {
      "title": "如何学好Java",
      "content": "根据用户目标生成如何学好 Java学习路线文章：覆盖核心概念、实践路径和常见问题。",
      "summary": "根据用户目标生成如何学好 Java学习路线文章：覆盖核心概念、实践路径和常见问题。"
    }
  }
}
```

这里的 `constraints` 实际承载了已经绑定的工具参数，但字段名仍是通用 checkpoint 字段。

### tool_name 的解析位置

工具名不是从 PlanExecution 读取的，而是在执行时重新解析：

1. `CapabilityExecutor` 取 `CapabilityRegistry.GENERATE_CONTENT.tools[0]`
2. 得到 `content.create_draft`
3. `ToolInvocationContext` 携带该工具名和参数进入 `ToolRuntime`

对应代码：

- `packages/assistant_core/greenbook_assistant_core/execution/capability_executor.py:95`
- `packages/assistant_core/greenbook_assistant_core/execution/capability_executor.py:100-110`
- `packages/assistant_core/greenbook_assistant_core/execution/worker.py:177-207`

所以这次失败不是 PlanExecution 中保存了错误 tool 名称，而是运行时根据 capability 选出了正确的工具名后，绑定参数仍使用了错误字段名。

## 3. RuntimeAgentService 调用 ToolRuntime 的入参

`RuntimeAgentService` 在 `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py:179-191`
创建 `ArgumentBinder` 并先绑定整个 plan。随后 Worker 每次执行步骤时又在
`CapabilityExecutor._bound_tool_args()` 处进行最后一次绑定。

当前真实 MCP server 的 `get_tool_definitions()` 对 `content.create_draft` 只返回：

```json
{
  "name": "content.create_draft",
  "description": "Create a new draft via Creator Agent and Java Facade",
  "category": "content",
  "risk": "medium"
}
```

没有 `parameters`。因此 `ArgumentBinder._schema_for()` 找不到字段 schema，回退到
`CapabilityRegistry` 的 capability inputs，最终产生：

```json
{
  "title": "如何学好Java",
  "content": "根据用户目标生成如何学好 Java学习路线文章：覆盖核心概念、实践路径和常见问题。",
  "summary": "根据用户目标生成如何学好 Java学习路线文章：覆盖核心概念、实践路径和常见问题。"
}
```

`RuntimeAgentService.raw_handler()` 在
`apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py:220-234`
将其展开为：

```python
await mcp.execute_tool(
    "content.create_draft",
    auth=ctx.auth,
    session=ctx.session,
    trace_id=ctx.trace_id,
    agent_run_id=ctx.run_id,
    tool_call_id=..., 
    title="如何学好Java",
    content="根据用户目标生成如何学好 Java学习路线文章：覆盖核心概念、实践路径和常见问题。",
    summary="根据用户目标生成如何学好 Java学习路线文章：覆盖核心概念、实践路径和常见问题。",
)
```

## 4. ToolRegistry 中 `GENERATE_CONTENT` 的 schema

### Capability schema

`packages/assistant_core/greenbook_assistant_core/capability/registry.py:77-85` 当前定义：

```python
Capability(
    name="GENERATE_CONTENT",
    tools=["content.create_draft"],
    inputs=CapabilityInput(
        required=["title", "content"],
        optional=["references", "summary"],
    ),
)
```

### MCP ToolRegistry schema

`services/greenbook_mcp/greenbook_mcp_server/tool_registry.py:71-77` 注册
`content.create_draft` 时没有传入 `argument_model`：

```python
_register(
    "content.create_draft",
    content.create_draft,
    description="Create a new draft via Creator Agent and Java Facade",
    category="content",
    risk="medium",
)
```

`ToolDefinition.argument_model` 因此为 `None`。这造成两个后果：

1. `GreenBookMCPServer.get_tool_definitions()` 不导出参数 schema；
2. `validate_registered_tool_contracts()` 遇到 `None` 时直接跳过 handler/schema 校验。

这解释了为什么 capability 元数据中的 `content` 可以一路传到执行边界，而没有在启动时被发现。

## 5. Creator Agent 实际接口

### MCP content handler

`services/greenbook_mcp/greenbook_mcp_server/tools/content.py:62-68` 的实际 handler 是：

```python
async def create_draft(
    ctx: ToolContext,
    title: str,
    instruction: str,
    references: list[dict[str, Any]] | None = None,
    summary: str | None = None,
) -> ToolResult[Any]:
```

因此 handler 期望的工具参数是：

```json
{
  "title": "如何学好Java",
  "instruction": "根据用户目标生成如何学好 Java学习路线文章：覆盖核心概念、实践路径和常见问题。",
  "summary": "根据用户目标生成如何学好 Java学习路线文章：覆盖核心概念、实践路径和常见问题。"
}
```

`references` 是可选字段，本次没有搜索步骤，因此可以省略。

### Creator HTTP API

MCP handler 成功通过签名校验后，会调用
`packages/creator_client/greenbook_creator_client/client.py:44-86`：

```python
CreatorClient.create_task(
    kind="CREATE_CONTENT",
    goal=instruction,
    constraints={...},
    ...,
)
```

目标接口是 `POST /api/v1/creator/tasks`。服务端模型
`apps/creator-agent/app/creator/api/models.py:89-105` 的必需业务字段是：

```text
kind
goal
constraints
source_scope (有默认值)
```

Creator 接口并不接收 `title`、`content` 或 `instruction` 作为顶层请求字段；`instruction` 由 MCP handler 正确转换成 Creator 的 `goal`。本次错误发生在该转换之前，因此 Creator Agent 尚未被调用。

## 6. 偏差定位

| 阶段 | 实际 | 期望 | 结论 |
| --- | --- | --- | --- |
| Planner | `GENERATE_CONTENT`，空 constraints | 同样 | 无偏差 |
| ArgumentBinder schema lookup | MCP 没有 create schema，回退 capability | 应读取实际工具 schema | 缺少 schema 暴露 |
| CapabilityRegistry inputs | `title`、`content` | `title`、`instruction` | 字段名偏差 |
| MCP handler bind | 收到 `title`、`content`、`summary` | `title`、`instruction`、`summary` | 直接触发 TypeError |
| CreatorClient | 尚未调用 | `goal=instruction` | 不是根因 |
| Creator HTTP API | 尚未请求 | `POST /api/v1/creator/tasks` | 未到达 |

MCP server 在 `services/greenbook_mcp/greenbook_mcp_server/server.py:121-149`
调用 `Signature.bind()`。由于缺少 `instruction` 且多出 `content`，返回：

```json
{
  "ok": false,
  "code": "PRE_EXECUTION_VALIDATION_FAILED",
  "request_sent": false,
  "state": {
    "phase": "PRE_EXECUTION_VALIDATION_FAILED",
    "downstream_called": false,
    "side_effect_started": false,
    "safe_to_retry": true
  }
}
```

Worker 随后将第 1 步标记为 `FAILED`，并按照依赖关系将第 2、3 步标记为 `SKIPPED`，所以用户观察到的步骤状态与该失败位置一致。

## 7. 最小修复位置建议（本次未执行）

### 最小联调修复

当前真实 Runtime 因 MCP schema 缺失而使用 capability fallback。只为解除本次阻塞，最小改动位置是：

```text
packages/assistant_core/greenbook_assistant_core/capability/registry.py
GENERATE_CONTENT.inputs.required: ["title", "content"]
    -> ["title", "instruction"]
```

`ArgumentBinder` 已经在 `_semantic_values()` 中准备了 `instruction` 值，因此该改动会使当前 fallback 绑定出 handler 能接受的参数。

### 推荐的契约收口修复

更稳妥的单一事实源应位于 MCP ToolRegistry：

1. 在 `services/greenbook_mcp/greenbook_mcp_server/tool_schemas.py` 增加
   `CreateDraftArguments`，字段为 `title`、`instruction`、`references`、`summary`；
2. 在 `services/greenbook_mcp/greenbook_mcp_server/tool_registry.py` 为
   `content.create_draft` 注册 `argument_model=CreateDraftArguments`；
3. 同步将 capability fallback 的字段名改为 `instruction`，避免 MCP schema 不可用时再次漂移。

这样可以同时保证：

- `get_tool_definitions()` 导出真实参数 schema；
- `ArgumentBinder` 按真实 schema 绑定；
- MCP server 在 handler 执行前做 Pydantic 校验；
- `validate_registered_tool_contracts()` 能检查 handler/schema drift。

不需要修改以下模块：

- `IntentSpecProvider`
- Planner / `TaskOrchestrator`
- `ExecutionWorker`
- `ToolRuntime`
- `ExecutionStateManager`

## 当前状态

- 审计结论：已完成
- 代码修改：无（仅新增本审计文档）
- 外部调用：无
- 测试：未执行服务级测试；仅进行源码读取和纯内存绑定复算
- 下一步：确认采用“最小 capability 字段修复”还是“ToolRegistry schema + fallback 同步”的契约收口方案后，再单独进入实现阶段
