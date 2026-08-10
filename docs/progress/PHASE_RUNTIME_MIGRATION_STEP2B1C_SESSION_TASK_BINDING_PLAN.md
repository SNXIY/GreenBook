# Phase Runtime Migration Step2-B-1-c：Session Task Binding Persistence 审计

## 审计范围

本阶段只读审计 SessionContext、会话 reload、Task 绑定持久化和 IntentCompiler 的依赖关系。
不修改 RuntimeAgentService、Planner、Worker、ToolRuntime、ExecutionStateManager，
不进入 ConversationRuntimeAdapter，也不改变数据库 schema。

审计基线：

- 分支：feature/runtime-http-migration
- HEAD：9d0f120 feat(runtime): add task provider boundary
- 当前发现的两个未跟踪文件未纳入本审计修改范围。

## 结论摘要

回归失败的直接原因不是 TaskProvider 的解析算法，而是会话模型的投影边界缺失：

1. packages/assistant_core/greenbook_assistant_core/context.py 中的
   SessionContext 没有声明 active_task_id。
2. 同一个模型也没有声明 active_artifact_id，但失败测试和
   IntentCompiler 都把它当作会话绑定字段使用。
3. Pydantic 默认忽略未声明输入字段。因此
   SessionContext(active_task_id="task-1", active_artifact_id="artifact-1")
   不会报错，但两个值不会出现在 model_dump() 中。
4. conversation reload 再次执行 SessionContext(**data) 时，绑定已经丢失；
   IntentCompiler 对 MODIFY_TASK 无法解析目标，返回 TARGET_CONTEXT_REQUIRED。
5. 数据库表和 migration 已预留 active_task_id、active_artifact_id，
   但当前 Assistant API 真实会话路径仍使用进程内
   app.state.conversation_store，ConversationRepository 没有接入该路径。

因此最小修复必须先补齐 SessionContext 的两个字段和 round-trip 回归测试；
仅增加 active_task_id 仍会使 artifact 绑定在 reload 后丢失。TaskProvider
接入后应以其经过 scope 校验的 TaskBinding.target 为本次请求的权威目标，
把 active_task_id/active_artifact_id 作为会话恢复和多轮 fallback，而不是
绕过 TaskResolver 的授权入口。

## 1. SessionContext 定义和字段现状

定义文件：

packages/assistant_core/greenbook_assistant_core/context.py（SessionContext，
约第 35 行）。

当前声明的会话字段包括：

|类别|字段|
|---|---|
|身份|conversation_id、冻结的 user_id、冻结的 tenant_id|
|时间|timezone|
|资源|active_draft_id、active_post_id、active_schedule_id|
|历史|recent_entities、recent_tool_calls|
|流程|pending_approval、conversation_summary、last_successful_run_id|

当前缺失：

- active_task_id
- active_artifact_id

apps/assistant_api/greenbook_assistant_api/models/runtime_context.py 中虽然有
TaskContext.active_artifact_id，那是一次 Runtime 编译/执行的上下文，不是可
reload 的 SessionContext 字段，不能替代会话绑定。

SessionContext 未设置 extra="forbid" 或 extra="allow"；Pydantic 默认的
extra 行为会忽略未知输入。因此测试中的构造不会显式失败，问题延迟到
model_dump()/reload 后才暴露：

~~~text
SessionContext(active_task_id=..., active_artifact_id=...)
        └─ 未声明字段被忽略
created.model_dump()
        └─ 不含两个绑定字段
SessionContext.model_validate(...)
        └─ reload 后没有 active task/artifact
~~~

## 2. active_task_id 的当前生命周期

当前实际上不存在一条可用的 active_task_id 生命周期：

|环节|实现|当前结果|
|---|---|---|
|会话创建|api/routes.py:create_conversation（约第 573 行）|只写入声明在 SessionContext 中的字段|
|会话读取|api/routes.py:_get_session（约第 267 行）|从 conversation_store 构造 SessionContext(**data)；未知字段无法恢复|
|会话保存|api/routes.py:_save_session（约第 286 行）|保存 session.model_dump(mode="json")；缺字段即永久丢失|
|审批 reload|api/routes.py:approve_operation（约第 1153 行）|直接从 dict 再构造 SessionContext，同样丢失|
|IntentCompiler|services/intent_compiler.py:_resolve_task_id（约第 138 行）|只能通过 getattr 尝试读取；当前模型永远没有该属性|
|数据库创建|ConversationRepository.create（db/repositories.py 约第 171 行）|列存在，但初始化为 NULL|
|数据库更新/读取|ConversationRepository.find_by_id/update|实现存在，但当前 message/session 路径未调用|

现有部分 projection 测试通过给会话对象设置 task/artifact 绑定来表达预期，
但这不能证明持久化有效；失败的 reload 测试正好揭示了模型声明和存储 round-trip
之间的断层。

## 3. Conversation reload 流程

当前 Assistant API 的真实流程是：

~~~text
POST /api/v1/assistant/conversations
  -> request.app.state.conversation_store[conversation_id] = session.model_dump(...)

后续请求
  -> _get_session()
  -> 读取进程内 conversation_store
  -> SessionContext(**data)
  -> 旧 message route 使用 session
  -> _save_session()
  -> 再次写回 session.model_dump(...)
~~~

审批确认路径没有复用 _get_session，而是在 api/routes.py:1151-1159
直接读取 store、构造 SessionContext、清理 pending_approval 后重新 dump。
它会重复同一字段丢失问题。

这说明当前的“reload”是应用进程内字典 round-trip，不是从
assistant_conversations 表恢复。进程重启后，当前内存 conversation store
本身也不会恢复；这属于后续持久化接线问题，不应在本阶段通过 Legacy
assistant_runs 解决。

## 4. 会话持久化存储位置

### 当前实际来源

apps/assistant_api/greenbook_assistant_api/main.py 初始化：

~~~text
app.state.conversation_store = {}
~~~

api/routes.py 的 _get_session、_save_session、会话列表和 approval reload
都依赖这个进程内字典。该字典的注释也只列出 draft/post/schedule 等字段，
没有 task/artifact 绑定。

### 已存在但尚未接入的数据库来源

packages/assistant_core/greenbook_assistant_core/db/repositories.py 的
_conversations 表已声明：

- active_task_id
- active_artifact_id

packages/assistant_core/greenbook_assistant_core/db/migrations/002_conversation_task_artifact_binding.sql
也已添加这两列。ConversationRepository.find_by_id 可以读出列，
update 可以更新列；但当前 Assistant API main/lifespan 没有把
ConversationRepository 作为 session provider 接入 routes，所以不能把
数据库列误判为当前活跃会话存储。

TaskProvider 使用 session_ctx() 创建 request-scoped session 来读写
assistant_tasks；这只能保证 Task 自身的 scope 和状态，不会自动更新
assistant_conversations.active_task_id。

## 5. IntentCompiler 为什么依赖 active_task_id

文件：apps/assistant_api/greenbook_assistant_api/services/intent_compiler.py。

_resolve_task_id 的目标优先级是：

1. 已校验的 target_context.task_id；
2. conversation.active_task_id；
3. 对需要已有任务的关系返回 TARGET_CONTEXT_REQUIRED；
4. 对允许新建的关系再使用传入的 task。

因此 CONTINUE_TASK、MODIFY_TASK 等多轮操作在没有显式
ResolvedTaskTarget 时，需要会话的 active task 作为 fallback。这个 fallback
不是授权机制，随后仍会执行 task/artifact 一致性校验。

_compile_target（约第 238 行）还只有在
conversation.active_task_id == resolved_task_id 时才采信
conversation.active_artifact_id。这保证 artifact 不会被错误地从另一个
Task 带入，但也意味着两个字段必须一起 round-trip。

失败测试
tests/unit/test_intent_compiler.py::test_reloaded_conversation_keeps_task_and_artifact_binding
的实际过程是：

~~~text
创建 SessionContext(active_task_id="task-1", active_artifact_id="artifact-1")
  -> model_dump()
  -> model_validate()
  -> IntentCompiler.compile(MODIFY_TASK, task=task, conversation=reloaded)
  -> active_task_id 为空
  -> TARGET_CONTEXT_REQUIRED
~~~

即使只补 active_task_id，active_artifact_id 仍不会被恢复，后续目标
artifact 断言仍可能失败。

## 6. TaskProvider 引入后的正确恢复边界

TaskProvider（apps/assistant_api/greenbook_assistant_api/services/task_provider.py）
当前职责边界是正确的：

- create_task 对经过验证的 IntentSpec 创建 READY Task；
- resolve_task 通过 TaskResolver 查找 CONTINUE_TASK/
  MODIFY_TASK/CANCEL_TASK；
- 所有查询按 TaskScope(user_id, tenant_id, conversation_id) 过滤；
- 返回 TaskBinding(task, target)，不会修改 Runtime execution。

但它目前没有做 conversation binding persistence。后续接线应遵循以下顺序：

1. NEW_TASK：TaskProvider 创建 Task 后，由上层编排边界把
   task.task_id 写入当前会话的 active_task_id；若已有确定 artifact，
   同时写入 active_artifact_id。
2. CONTINUE/UPDATE/CANCEL：先调用 TaskProvider 的 scope-checked
   resolve_task，再把返回的 TaskBinding.target 传给 IntentCompiler。
   不把用户提交的 task_id 直接当成可信绑定，也不直接取“最近任务”。
3. 多轮 reload：优先重新运行 TaskProvider 的授权解析；只有在意图没有明确
   target 时，才把持久化的 active task 作为候选/提示，并再次做 scope 和
   artifact 所有权校验。
4. CANCEL_TASK 只更新 Task 为 CANCELLED；若要清除会话 active binding，
   只能在确认它指向被取消 Task 后清除，不触碰 ExecutionStateManager 或
   PlanExecution。
5. Runtime 路由迁移时，显式 TaskBinding.target 应是本请求权威目标，
   SessionContext 绑定只负责跨轮次恢复和兼容已有会话，不应成为新的
   compatibility/Legacy 入口。

## 7. 最小修改方案（本阶段只设计，不实施）

### 必须修复的模型投影

在 SessionContext 中增加两个可选字段：

~~~python
active_task_id: str | None = None
active_artifact_id: str | None = None
~~~

不需要新增状态模型或数据库列。增加后，现有 _save_session 的
model_dump(mode="json") 和 _get_session 的构造会自动实现内存 store
round-trip；审批 reload 也会保留绑定。

### 必须补的回归验证

最小测试范围：

1. 构造含 task/artifact 的 SessionContext，断言 model_dump() 包含两个字段；
2. SessionContext.model_validate(created.model_dump()) 后两个字段保持不变；
3. 重新运行失败测试，确认 MODIFY_TASK 可解析 task 和 artifact；
4. 覆盖 _save_session/_get_session 的 conversation store round-trip；
5. 覆盖不同 user/tenant/conversation 不能借 active id 读取他人 Task。

### 后续接入（不属于本次审计修改）

当 ConversationRuntimeAdapter 获准实施时，才在它的事务边界中：

- 调用 TaskProvider；
- 更新 SessionContext 的 active task/artifact；
- 若启用数据库会话存储，使用受 scope 校验的
  ConversationRepository.update 写入现有列；
- 使用 request-scoped session_ctx()，异常 rollback，禁止复用全局
  AsyncSession。

当前不应为了修复测试而修改 RuntimeAgentService、Planner、Worker、
ToolRuntime、ExecutionStateManager，也不应读取 assistant_runs 作为绑定来源。

## 8. 验收清单

实施下一小步后，应满足：

- 新会话的 task/artifact 绑定可以序列化；
- 同一进程内 reload 后 IntentCompiler 保留 task/artifact；
- TaskProvider 创建的 Task 是 READY，且绑定写入同一 conversation scope；
- 明确 target 时使用 TaskBinding.target，不会依赖“最近任务”；
- 未授权 task_id 返回 not found/scope 错误，不泄露 Task 信息；
- 取消 Task 不改变 Runtime execution 状态；
- 未引入 assistant_runs fallback 或新的状态模型。

## 当前阶段结论

这是一个“会话模型字段缺失 + 当前存储路径未接入 DB”的持久化边界问题。
最小、可回滚的下一步是只扩展 SessionContext 的两个声明字段并添加
round-trip 回归测试；随后再在获准的 ConversationRuntimeAdapter 中把
TaskProvider 的绑定结果写入会话。除此之外，本阶段不应改动任何 Runtime
执行核心或消息入口。
