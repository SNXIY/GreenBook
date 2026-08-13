# Phase 9：Creator/下游执行失败只读审计

## 1. 审计范围与结论

审计对象：`execution_id=70e41538-fe59-47fc-bb10-fd6d237170ef`，用户输入“写一篇 Redis 缓存三大机制文章”。本阶段只读取代码、运行中服务的健康端点和受保护 Execution 路由；没有修改 Runtime、Worker、Planner、ToolContract 或数据库。

直接结论：

1. 失败不是 `TOOL_ARGUMENT_VALIDATION_FAILED`，而是 `content.create_draft` 通过 ToolContract/MCP 前置校验后，在 Java Agent Facade 下游返回/产生了 `JAVA_BACKEND_UNAVAILABLE`。
2. `CreatorClient` 已被调用且正常走过“提交 Creator 任务 → 等待完成 → 读取最终 Artifact”分支；否则用户文案应是 Creator 不可用、超时或 Artifact 不存在，而不是“社区服务暂时不可用，尚未确认本次操作结果”。
3. Java 客户端确实尝试执行 `POST /api/v1/agent/drafts`，但仅凭当前文案无法区分“连接未建立/请求未发出”和“请求已到达并返回 5xx”。两种情况目前都可能映射到 `JAVA_BACKEND_UNAVAILABLE`。
4. Side-effect ledger 只在本次 Runtime 调用内存中存在，未写入 ExecutionEventStore/ExecutionRepository，也没有 HTTP 查询接口；因此无法用该 `execution_id` 从外部取得精确的 invocation_id、请求状态或 Creator/Java 子调用明细。
5. ToolContract 的重试元数据包含 `JAVA_BACKEND_UNAVAILABLE`，但 Worker 的 `RecoveryPolicy.DEFAULT_RETRYABLE_CODES` 没有该代码。因此本错误会被 Worker 当作永久失败，不会产生 Runtime retry 事件，后续步骤会被标记为 `SKIPPED`。

当前运行环境的只读健康检查：Assistant `/health` 为 `200`，且报告 Java/Creator 可达；Creator `/actuator/health` 为 `200`。这是审计时刻的健康状态，不能证明失败发生时下游没有短暂网络/5xx 故障。

## 2. 端到端调用链

```text
POST /api/v1/assistant/conversations/{conversation_id}/messages
  └─ routes._send_runtime_message
     └─ ConversationRuntimeAdapter.execute
        └─ RuntimeAgentService.execute
           └─ Intent/Planner/ArgumentBinder/PlanValidator
              └─ ExecutionWorker._execute_one_step
                 └─ CapabilityExecutor(GENERATE_CONTENT)
                    └─ ArgumentBinder → ToolInvocationContext
                       └─ ToolRuntime.invoke
                          └─ mcp.execute_tool("content.create_draft", ...)
                             └─ MCP input schema + handler signature validation
                                └─ tools.content.create_draft
                                   ├─ CreatorClient.create_task (POST /api/v1/creator/tasks)
                                   ├─ CreatorClient.wait_for_completion (GET task，轮询)
                                   ├─ CreatorClient.get_artifact (GET artifact)
                                   ├─ JavaClient.create_draft (POST /api/v1/agent/drafts)
                                   └─ JavaClient.get_draft (GET /api/v1/agent/drafts/{id})
```

相关入口：

| 层 | 文件/函数 | 事实 |
|---|---|---|
| HTTP | `apps/assistant_api/greenbook_assistant_api/api/routes.py::_send_runtime_message` | Runtime 开关打开时调用 `ConversationRuntimeAdapter`；旧 `run_id` 仍只做兼容投影。 |
| 适配 | `services/conversation_runtime_adapter.py::execute` | 生成 RuntimeContext 并调用 `RuntimeAgentService`，不从 `assistant_runs` 推导状态。 |
| 执行 | `services/runtime_agent_service.py::_execute_single` | 创建 `ToolExecutionLedger`、`ToolRuntime`、`CapabilityExecutor` 和 `ExecutionWorker`。 |
| 状态 | `packages/assistant_core/.../execution/repository.py` | 当前是进程内 `PlanExecution` 存储。 |
| 事件 | `packages/assistant_core/.../execution/event_store.py` | 当前是进程内 `ExecutionEventStore`；Runtime API 的 `/events` 和 `/stream` 从这里读取。 |

注意：`RuntimeAgentService` 直接使用 `ExecutionWorker.init_from_plan()`，没有调用 `RuntimeManager.create_execution()`；后者才会追加 `EXECUTION_CREATED`。因此真实消息链路的 EventStore 可能从 `EXECUTION_STARTED` 开始，而不是保证存在 `EXECUTION_CREATED`。

## 3. Execution Event 完整链路

由于 Execution API 要求登录，本次未使用绕过授权的方式读取受保护的 `/steps`、`/events`。对该 ID 的无 Authorization 请求返回 `401` 而不是 `404`；按照 `runtime_routes.py` 的顺序，这说明请求已找到对应 Execution 后在 ownership/auth 检查处被拒绝。以下序列是由当前源代码确定的失败路径：

| 顺序 | EventStore/Trace | 产生位置 | 预计结果 |
|---:|---|---|---|
| 1 | `EXECUTION_CREATED`（可能缺失） | 仅 `RuntimeManager.create_execution` 追加；真实消息路径绕过该函数 | 不应假设存在 |
| 2 | `EXECUTION_STARTED` | `ExecutionStateManager.start_execution` | execution 进入 `RUNNING` |
| 3 | `STEP_STARTED` | `ExecutionWorker._execute_one_step` | `GENERATE_CONTENT` 进入 `RUNNING` |
| 4 | `TOOL_INVOKED` | `ToolRuntime` 的 `AgentTrace` | 仅 TraceCollector，不在 ExecutionEventStore |
| 5 | `TOOL_FAILED` | `ToolRuntime` 收到 MCP 失败结果 | 仅 TraceCollector，不在 ExecutionEventStore |
| 6 | `STEP_FAILED` | Worker | `retryable=false`（见第 7 节的 RecoveryPolicy 差异）及错误码/文案 |
| 7 | `EXECUTION_FAILED` | `ExecutionStateManager._update_execution_status` | execution 进入 `FAILED` |
| 8 | 后续步骤 `SKIPPED` | scheduler 标记下游步骤 | 当前没有 `SKIPPED` EventType，通常只在 `/steps` 快照体现 |

若后续存在完整重试，EventStore 还可能包含 `STEP_RETRY_*`；但针对当前 `JAVA_BACKEND_UNAVAILABLE`，按当前 Worker 代码不会进入重试分支。

SSE `/api/v1/executions/{id}/stream` 只是以 100ms 轮询同一个 EventStore，并在终态且游标追平后结束；它不会补充 ToolRuntime 的 Trace 或 ledger 事件。

## 4. ToolContract/MCP 校验结果

### 4.1 契约边界

`services/greenbook_mcp/greenbook_mcp_server/tool_registry.py` 将：

- capability：`GENERATE_CONTENT`
- tool：`content.create_draft`
- input schema：`CreateDraftArguments`
- 必填字段：`title`, `instruction`
- 可选字段：`references`, `summary`
- side effect：`has_side_effect=true`, `idempotent=true`
- external systems：`creator`, `java`

`GreenBookMCPServer.__init__()` 启动时执行全量注册契约校验；当前 Assistant 进程已正常启动，说明启动时没有发现 handler/schema 漂移。

### 4.2 本次调用是否通过

`server.py::execute_tool` 依次执行 Pydantic input 校验、handler signature bind，再调用 `content.create_draft`。如果任一步失败，返回的是 `TOOL_ARGUMENT_VALIDATION_FAILED` 或 `PRE_EXECUTION_VALIDATION_FAILED`，并带有 `downstream_called=false`。

本次用户看到的是 Java 下游的“社区服务暂时不可用，尚未确认本次操作结果”，不是上述前置校验文案。因此可以确认：

- `GENERATE_CONTENT` 的参数绑定已进入 MCP；
- `content.create_draft` handler 已被调用；
- 至少通过了 MCP input schema 和 handler signature 校验；
- 失败发生在 handler 的外部依赖阶段，而不是 Phase 8 ToolContract 漂移。

## 5. Creator Agent 调用审计

`services/greenbook_mcp/greenbook_mcp_server/tools/content.py::create_draft` 的顺序是：

1. `ctx.creator.create_task(kind="CREATE_CONTENT", goal=instruction, ...)`；
2. `wait_for_completion(..., deadline_seconds=240)` 轮询 Creator task；
3. `get_artifact(task_id, final_artifact_id)` 获取最终文稿；
4. 将 Creator 文本转换成 `AgentDraftCreateRequest`，才调用 Java。

Creator 客户端的失败映射为：

- 连接失败/非 2xx：`CREATOR_UNAVAILABLE`，用户文案是“创作服务暂时不可用……”；
- 任务未完成：`TIMEOUT`；
- 终态失败：`BUSINESS_REJECTED`；
- Artifact 缺失：`NOT_FOUND` 或 `CREATOR_UNAVAILABLE`。

因此，本次错误文案与代码路径一致地表明 CreatorClient 已被调用，并已返回足以继续 Java handoff 的成功结果。源代码能确认调用/请求尝试及 URL；由于没有该次请求的 Creator trace 日志，无法在本报告中填写具体 `task_id`、artifact_id 和 HTTP 状态码。

## 6. Java Agent HTTP 调用与下游状态

`JavaClient.create_draft()` 最终进入 `JavaClient._request("POST", "/api/v1/agent/drafts", ...)`，并携带 Bearer、`Idempotency-Key`、`X-Trace-ID`、conversation/run/tool headers。随后成功路径还会调用 `GET /api/v1/agent/drafts/{draft_id}` 验证。

错误分类如下：

| Java 结果 | ToolResult code | `request_sent` | 用户可见文案/含义 |
|---|---|---:|---|
| ConnectError/ConnectTimeout/PoolTimeout/网络协议错误 | `JAVA_BACKEND_UNAVAILABLE` | `false` | 当前中文文案；未建立可确认的请求 |
| HTTP 5xx 或 `DEPENDENCY_UNAVAILABLE` | `JAVA_BACKEND_UNAVAILABLE` | 当前实现默认仍为 `false` | 请求已收到 HTTP 响应，但 envelope 没有准确区分“已发出” |
| 写请求 ReadTimeout | `RESULT_UNKNOWN` | `true` | 另一套“可能已提交，请勿重复”的文案；与当前文案不同 |
| 读请求 ReadTimeout | `TIMEOUT` | `true` | 可安全重试的读取超时 |
| WriteTimeout | `REQUEST_NOT_SENT` | `false` | 请求体未完整发送 |

所以，当前文案最可能是 Java 连接/网络不可用；但若 Execution 事件 payload 显示 HTTP 5xx，则应改判为“请求已发出并收到 5xx”。没有授权读取该执行的 `steps/events`，不能伪造精确下游状态码。

另外，Creator 完成后 Java POST 失败时，代码不会调用 Creator 补偿/撤销；因此可能留下 Creator task/artifact，但没有 Java draft。RuntimeResult 的 `side_effect_committed` 只根据最终 draft artifact 设置，不能代表 Creator 阶段没有外部副作用。

## 7. Timeout、Retry、Unknown 状态来源

| 层 | 超时/失败来源 | 当前行为 |
|---|---|---|
| CreatorClient | 单次 HTTP 客户端 timeout；完成轮询 deadline 240s | 返回 `TIMEOUT` 或 Creator 错误；轮询连接/超时会继续轮询至 deadline |
| JavaClient | connect 5s、read 30s、write 30s、pool 5s | 写 ReadTimeout → `RESULT_UNKNOWN`；连接/5xx → `JAVA_BACKEND_UNAVAILABLE` |
| ToolRuntime | `CapabilityExecutor` 传入 120s | `asyncio.wait_for` 超时 → ledger `TIMEOUT`、InvocationResult `TIMEOUT`、Trace `TOOL_FAILED` |
| Worker | `RecoveryPolicy.DEFAULT_RETRYABLE_CODES` | 仅 `TIMEOUT/NETWORK_ERROR/RATE_LIMIT/TEMPORARY_UNAVAILABLE`；不包含 `JAVA_BACKEND_UNAVAILABLE`/`CREATOR_UNAVAILABLE` |
| ToolContract | `content.create_draft` max_attempts=2，retryable codes 包含 Creator/Java unavailable | 只作为注册/导出元数据；当前 Worker 没有读取该 retry_policy |

这解释了“ToolResult 标记 retryable=true，但 Execution 仍立即 FAILED”：Worker 根据错误码白名单而不是 ToolResult 的 `retryable` 字段或 ToolContract 的 `retry_policy` 做分支判断。

## 8. Side Effect Ledger 当前状态

`RuntimeAgentService._execute_single()` 每次执行创建一个新的 `ToolExecutionLedger`，并注入 `ToolRuntime`。`ToolRuntime` 生命周期是：

```text
record_start(RUNNING)
  → raw MCP result
  → record_complete(COMPLETED) 或 record_failure(FAILED)
  → 超时则 record_timeout(TIMEOUT)
```

对本次同步失败路径，代码层预期 ledger 至少有一条 `content.create_draft` invocation，最终为：

```text
status=FAILED
error_code=JAVA_BACKEND_UNAVAILABLE
execution_id=70e41538-fe59-47fc-bb10-fd6d237170ef
```

但以下信息当前不可从 API 取得：invocation_id、idempotency_key、started/finished 时间、duration、`request_sent`、Creator task/artifact 子调用状态。ledger 也不会在 Assistant 进程重启后保留。故该状态是基于代码路径的审计推断，不是从持久化记录读取的事实。

## 9. 根因分层与后续核验

### 已确认

- MCP/ToolContract 入口通过；不是参数契约漂移。
- CreatorClient 调用链已被走到 Java handoff 之前。
- 用户文案来自 `ToolResult.java_backend_unavailable`。
- Worker 将该错误作为永久失败，导致 `GENERATE_CONTENT FAILED`、后续步骤跳过和 execution `FAILED`。

### 仍需凭授权运行数据确认

1. 使用任务所属用户的有效 JWT 读取：
   - `GET /api/v1/executions/70e41538-fe59-47fc-bb10-fd6d237170ef/steps`
   - `GET /api/v1/executions/70e41538-fe59-47fc-bb10-fd6d237170ef/events`
2. 用同一个 `trace_id` 检索 Assistant、Creator（8092）和 Java（8080）日志，确认 Creator `task_id/artifact_id`、Java POST 状态码和连接异常。
3. 若 Java 事件是 `RESULT_UNKNOWN` 或 `request_sent=true`，必须按“结果未知/可能已写入”处理，不能按当前“未确认且可安全重试”文案处理。
4. 若下游确认 5xx/连接失败，需在后续阶段统一对齐 ToolContract retry policy 与 Worker RecoveryPolicy；本报告阶段不修改它们。

## 10. 修改与验证记录

- 修改文件：仅新增本报告 `docs/progress/PHASE9_CREATOR_EXECUTION_FAILURE_AUDIT.md`。
- 未修改：`RuntimeAgentService`、`ExecutionWorker`、`ToolRuntime`、`Planner`、`ToolContract`、数据库及 Legacy 状态源。
- 只读验证：Assistant `/health`=200；Creator `/actuator/health`=200；未使用无授权方式读取受保护 Execution 明细。

