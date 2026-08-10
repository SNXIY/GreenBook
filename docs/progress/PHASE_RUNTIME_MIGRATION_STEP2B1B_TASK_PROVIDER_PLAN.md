# GreenBook Runtime Migration Step2-B-1-b Task Provider 设计

- 目标：为 Assistant API -> Runtime 的未来消息适配层定义 Task Provider，不修改消息入口。
- 范围：只审计和设计 Task 创建、查询、目标解析和取消边界。
- 当前分支：feature/runtime-http-migration
- 当前基线：fecbdbb（feat(runtime): add intent spec provider boundary）
- 本阶段：未修改业务代码、数据库或 Runtime 执行组件。

## 一、结论摘要

Task 领域模型、持久化 Repository、Registry 和无数据库 Resolver 都已经存在，但 Assistant API 尚未把它们接入请求生命周期。

当前实际状态：

1. Task 模型在 assistant_core/task/models.py。
2. TaskRepository 和 TaskRegistry 在 assistant_core/task/registry.py。
3. TaskResolver 在 assistant_core/task/resolver.py。
4. TaskContext 和 IntentCompiler 已存在，且 IntentCompiler 要求一个已解析的 Task。
5. main.py 只初始化 conversation_store、run_store、message_store 和 Runtime execution providers，没有 Task Provider、TaskRegistry 或数据库 session provider。
6. 当前消息路由使用内存 SessionContext 和 CommunityOperationsAssistant，不是 Task Provider。
7. TaskRepository 的查询没有按 user_id/tenant_id 过滤，所有权校验必须在 API Task Provider 边界完成。
8. assistant_runs、run_store、message_store 不能作为 Task 来源，也不能作为 Runtime 任务状态来源。

推荐的最小方向：

~~~text
Assistant API TaskProvider
    -> 请求范围 AsyncSession
    -> TaskRegistry / TaskRepository
    -> TaskResolver
    -> Task 或 ResolvedTaskTarget
    -> IntentCompiler
    -> TaskContext
~~~

TaskProvider 只负责 Task 生命周期和目标绑定；TaskContext 仍由现有 IntentCompiler 唯一创建，Planner、Worker 和 Runtime 状态模型保持不变。

## 二、当前 Task 模型

### 1. Task 模型位置

文件：

packages/assistant_core/greenbook_assistant_core/task/models.py

核心类型：

- TaskStatus
- TaskIntent
- ResolvedTaskTarget
- ArtifactRef
- Task

Task 定义在约第 111 行，表示可以跨多轮继续的业务目标，而不是一次 Runtime execution。

### 2. TaskStatus

现有状态：

~~~text
READY
IN_PROGRESS
COMPLETED
FAILED
CANCELLED
~~~

重要边界：

- TaskStatus 是业务目标生命周期。
- PlanExecution/StepExecution 才是一次执行的 Runtime 状态。
- TaskProvider 不得用 TaskStatus 替代 ExecutionStateManager。
- execution_id、step 状态和 tool 失败不能写入 assistant_runs 或由 TaskProvider 推断。

### 3. Task 字段

Task 现有字段：

|字段|用途|Task Provider 使用方式|
|---|---|---|
|task_id|长期任务唯一 ID|NEW_TASK 生成或沿用明确 ID|
|conversation_id|对话归属|必须与请求路径一致|
|user_id|用户归属|必须与 AuthContext 一致|
|tenant_id|租户归属|必须与 AuthContext 一致|
|goal|任务目标|来自已验证 IntentSpec/TaskIntent|
|goal_category|目标分类|例如 CREATE_CONTENT|
|goal_summary|可选摘要|用于目标匹配和展示|
|status|业务生命周期|NEW_TASK 初始显式设为 READY|
|phase|业务阶段|可选，不用于执行状态|
|artifacts|ArtifactRef 列表|传给后续 TaskContext 的 artifact_refs|
|depends_on|任务依赖|保留领域数据，不由 Provider 推导执行 DAG|
|last_error/retry_count/max_retries|业务重试信息|不能替代 StepExecution 重试|
|version|乐观并发版本|更新时由 Repository 校验|
|created_at/updated_at/completed_at|时间审计|由模型/Repository 维护|

Task 的 required identity 是 conversation/user/tenant 三元边界。任何只带 task_id 而未验证归属的查询都不应进入 Runtime。

## 三、现有 Repository、Registry 和 Resolver

### 1. TaskRepository

文件：

packages/assistant_core/greenbook_assistant_core/task/registry.py

TaskRepository 是 AsyncSession 上的低层数据库访问层。

已有能力：

- ensure_tables()
- insert(task)
- find_by_id(task_id)
- find_by_conversation(conversation_id, status=None)
- update(task_id, **fields)

持久化表：

- assistant_tasks
- assistant_task_intents

Task.artifacts 当前作为 assistant_tasks 的 JSONB 字段保存；TaskIntent 另有 assistant_task_intents 记录。

关键事实：

1. find_by_id 只按 task_id 查询。
2. find_by_conversation 只按 conversation_id 查询，并按 updated_at DESC 排序。
3. Repository 方法没有 user_id/tenant_id 条件。
4. update 使用 version 做乐观并发控制，但调用方必须先完成权限校验。
5. insert/update 内部会 commit。
6. ensure_tables 使用自身的 SQLAlchemy metadata 幂等创建 tasks 和 task_intents 表；当前 migrations 目录没有 assistant_tasks 专用 SQL 文件。

因此，TaskProvider 必须在 Repository 上方提供 scoped access，而不是把裸 TaskRepository 暴露给 HTTP route。

### 2. TaskRegistry

TaskRegistry 仍位于同一文件，包裹 TaskRepository。

已有能力：

~~~text
ensure_tables()
create_task(...)
get_task(task_id)
list_tasks(conversation_id, status=None)
update_task(task_id, **fields)
get_most_recent(conversation_id)
save_intent(...)
resolve_task(conversation_id, hint=None)
~~~

create_task 的默认 status 是 COMPLETED。这个默认值与“新 Runtime 任务刚准备执行”的语义不一致，未来 NEW_TASK 创建时必须显式传：

~~~text
status=TaskStatus.READY
~~~

TaskRegistry.resolve_task 的现有策略：

1. 如果有 hint，按 goal 或 goal_summary 子串匹配。
2. 没有命中时返回 conversation 最近 Task。
3. 没有候选时返回 None。

它没有返回匹配置信度、候选列表或所有权信息，因此不应单独承担 CONTINUE、MODIFY 或 CANCEL 的安全目标解析。

### 3. TaskResolver

文件：

packages/assistant_core/greenbook_assistant_core/task/resolver.py

接口：

~~~text
TaskResolver.resolve(intent: TaskIntent, tasks: list[Task])
    -> ResolvedTaskTarget | None
~~~

匹配优先级：

1. TaskIntent.target_task_id 精确 ID
2. target_task_hint 对 goal/goal_summary 的 label 匹配
3. ArtifactRef.summary/resource_kind 匹配
4. 相同 goal_category 的最近任务
5. 最近任务回退

ResolvedTaskTarget 携带：

- task_id
- goal
- goal_category
- confidence
- match_reason
- match_level
- candidates
- is_ambiguous

已有 resolve_target(intent, tasks) 辅助函数会在非 NEW_TASK/DIRECT 关系中填充 intent.target_task_id。

安全注意：

- 该 Resolver 是同步、纯内存逻辑，不负责数据库查询或授权。
- 需要先由 TaskRepository/Registry 查询当前 conversation 的 Task，再调用 Resolver。
- “刚才那个”等纯时间引用在多个同类任务下会标记 is_ambiguous。
- label 多匹配时会返回 candidates。
- CANCEL 不应直接接受低置信度的最近任务回退。

## 四、Assistant API 当前接入状态

### 1. main.py 生命周期

文件：

apps/assistant_api/greenbook_assistant_api/main.py

当前 lifespan 初始化：

- JavaClient
- CreatorClient
- GreenBookMCPServer
- AuthContextResolver
- AsyncOpenAI 和 model
- conversation_store
- run_store
- approval_store
- message_store
- ExecutionRepository
- ExecutionEventStore
- ExecutionStateManager
- RuntimeManager
- RuntimeAgentService

当前没有：

- AsyncSession 或 session factory 的 app.state provider
- TaskRepository
- TaskRegistry
- TaskResolver 实例
- TaskProvider
- IntentCompiler 实例
- Task 表初始化调用

runtime_router 已注册，但它只读取 execution providers，不能代替消息入口的 Task 绑定。

### 2. 当前消息入口

文件：

apps/assistant_api/greenbook_assistant_api/api/routes.py

当前 POST message 逻辑：

~~~text
_get_auth
  -> _get_session
  -> conversation_store / SessionContext
  -> message_store history
  -> CommunityOperationsAssistant
  -> old run_store / old events / old response
~~~

SessionContext 目前有：

- conversation_id
- user_id
- tenant_id
- timezone
- active_draft_id
- active_post_id
- active_schedule_id
- recent_entities
- pending_approval
- last_successful_run_id

SessionContext 当前没有 active_task_id。数据库的 assistant_conversations 表和 migration 002 已经有 active_task_id 列，但当前内存 message route 没有使用数据库 ConversationRepository。

结论：

- conversation_store 不是 Task Provider。
- message_store 不是 Task Provider。
- active_draft_id 或 active_schedule_id 不能代替 task_id。
- 当前 Assistant API 没有一个可供未来 ConversationRuntimeAdapter 调用的 Task Provider 接入点。

### 3. 数据库 session 设施

文件：

packages/assistant_core/greenbook_assistant_core/db/connection.py

已有：

- create_engine()
- session_factory()
- dispose_engine()
- session_ctx()

session_ctx 已提供：

~~~text
async with session_factory() as session:
    try:
        yield session
    except BaseException:
        await session.rollback()
        raise
~~~

这适合作为 TaskProvider 的请求范围 session 边界。

不应：

- 在 app.state 中保存一个长期 AsyncSession。
- 在 Provider 构造时永久持有一次请求的 session。
- 让 streaming 或 background Runtime 任务复用已关闭的 HTTP session。
- 把 TaskRegistry 单例化为跨请求对象；它内部持有 AsyncSession。

推荐 app.state 只保存工厂或 provider factory；每次 TaskProvider 调用创建 TaskRegistry(session)，操作完成后退出 session_ctx。

## 五、TaskContext 当前字段要求

### 1. 定义位置

文件：

apps/assistant_api/greenbook_assistant_api/models/runtime_context.py

TaskContext 是 frozen dataclass，字段：

|字段|要求|
|---|---|
|task_id|string，必填|
|goal|string，必须非空才能通过 IntentCompiler|
|task_intent|已理解的 TaskIntent，必填|
|target|TargetContext 或 ResolvedTaskTarget，可选但既有任务操作应提供|
|constraints|不可变 tuple，来自已验证语义|
|active_artifact_id|可选，必须与 task/artifact 一致|
|artifact_refs|不可变 ArtifactRef tuple，必须属于 task_id|

TaskContext.__post_init__ 会：

- 深拷贝 constraints。
- 复制 TaskIntent。
- 将未带 task_id 的 ArtifactRef 补上当前 task_id。
- 拒绝显式不匹配的 artifact task_id。

### 2. IntentCompiler 依赖

文件：

apps/assistant_api/greenbook_assistant_api/services/intent_compiler.py

IntentCompiler.compile 的关键规则：

1. 接受 IntentSpec 或 TaskIntent。
2. NEW_TASK/DIRECT 可以从传入 Task 获取 task_id。
3. CONTINUE_TASK、MODIFY_TASK、CANCEL_TASK 等既有任务关系必须提供 target_context 或 conversation.active_task_id。
4. task 必须存在。
5. task.task_id 必须与解析出的 task_id 一致。
6. ArtifactRef 必须属于 task_id。
7. goal 不能为空。
8. 编译后返回 TaskContext。

现有错误码：

~~~text
INTENT_REQUIRED
TASK_CONTEXT_REQUIRED
TARGET_CONTEXT_REQUIRED
TASK_REQUIRED
TASK_CONTEXT_MISMATCH
AMBIGUOUS_TARGET
ARTIFACT_REF_INVALID
ARTIFACT_TASK_MISMATCH
GOAL_REQUIRED
~~~

TaskProvider 不应复制这些编译规则，也不应自己构造第二种 TaskContext。它应返回 Task 或 ResolvedTaskTarget，之后由 IntentCompiler 负责唯一的 TaskContext 构造。

### 3. RuntimeAgentService 前置条件

RuntimeAgentService.execute 只接受已经装配好的 RuntimeContext：

- ctx.task_context 不能为 None。
- task_context.task_id 不能为空。
- task_context.task_intent 不能为 None。

否则返回 TASK_CONTEXT_REQUIRED。

所以未来消息适配层必须先完成：

~~~text
IntentSpecProvider
  -> to_task_intent
  -> TaskProvider
  -> IntentCompiler
  -> TaskContext
  -> RuntimeContext
  -> RuntimeAgentService
~~~

## 六、NEW_TASK 创建流程设计

### 1. 语义输入

前置已由 Step2-B-1-a 负责：

~~~text
message
  -> IntentSpecProvider
  -> validated IntentSpec
  -> to_task_intent
~~~

TaskProvider 不重新理解原始文本，不读取 LLM 输出。

### 2. 最小创建顺序

推荐未来 Adapter 使用以下顺序：

~~~text
1. 从 AuthContext 建立 TaskScope。
2. 从 IntentSpec 得到 TaskIntent。
3. 确认 relation=NEW_TASK。
4. 构造新的 task_id 和 Task 模型快照：
   conversation_id、user_id、tenant_id、goal、goal_category、status=READY。
5. 调用 IntentCompiler.compile：
   intent_spec、task_intent、task。
6. 确认 TaskContext.task_id == Task.task_id。
7. 使用 TaskRegistry.create_task 持久化 Task，显式 status=READY。
8. 可选地用 TaskRegistry.save_intent 保存本轮 TaskIntent。
9. 将 TaskContext 放入 RuntimeContext。
10. 才调用 RuntimeAgentService。
~~~

其中第 4 步可先构造内存 Task，再由 Registry 持久化同一个 task_id。这样可以先让 IntentCompiler 完成确定性校验，减少“Task 已写入但编译失败”的孤儿 READY 记录。

TaskRegistry.create_task 目前内部自行创建 Task 并 commit；未来实现时必须传入预生成 task_id、goal、goal_category 和 READY。若需要严格原子性，后续可以扩展 Repository 事务边界，但不属于本阶段设计，也不需要修改 Runtime 状态模型。

### 3. NEW_TASK 最小输出

TaskProvider 至少返回：

~~~text
Task(
  task_id,
  conversation_id,
  user_id,
  tenant_id,
  goal,
  goal_category,
  status=READY,
)
~~~

TaskProvider 不返回 execution_id。execution_id 只能由 PlanExecution/Runtime 产生。

### 4. NEW_TASK 约束

- 不查询或复用 assistant_runs。
- 不把 run_id 写成 task_id。
- 不把已有最近 Task 自动绑定到 NEW_TASK。
- 不使用 message_store 作为持久 Task。
- 不在 RuntimeAgentService 内隐式创建 Task。
- 不在 TaskProvider 内生成 Planner step 或 Tool 参数。

## 七、CONTINUE / UPDATE / CANCEL 设计

TaskIntent 的正式 relation 名称是：

~~~text
CONTINUE_TASK
MODIFY_TASK
CANCEL_TASK
~~~

用户层的 UPDATE 应归一到 MODIFY_TASK；Provider 不重新解释自然语言，只消费已产出的 TaskIntent。

### 1. 公共解析流程

~~~text
1. 校验 TaskScope：
   conversation_id、user_id、tenant_id 来自 URL/AuthContext。
2. 根据 conversation_id 查询候选 Task。
3. 对每个候选验证 user_id 和 tenant_id。
4. 调用 TaskResolver.resolve(intent, candidates)。
5. 处理 None、is_ambiguous、低置信度结果。
6. 得到 ResolvedTaskTarget。
7. 再通过 task_id 取回/确认 Task。
8. 检查 Task 不是跨用户、跨租户或已删除记录。
9. 将 Task + ResolvedTaskTarget 交给 IntentCompiler。
~~~

不能直接使用 TaskRegistry.resolve_task 作为唯一策略，因为它可能在 hint 未命中时返回最近任务，而且没有 ambiguity/confidence 结果。

### 2. CONTINUE_TASK

CONTINUE_TASK 表示在已有 Task 上继续完成目标。

推荐规则：

- 明确 target_task_id：精确匹配，最高优先级。
- target_task_hint 唯一命中 goal/summary/artifact：允许继续。
- 纯“刚才那个”且只有一个候选：允许继续。
- 多个同类候选：返回 TASK_TARGET_AMBIGUOUS，请用户选择。
- 无候选：返回 TASK_NOT_FOUND。
- 已 CANCELLED 的 Task：返回 TASK_NOT_ACTIVE，不重新激活。
- COMPLETED Task 是否允许继续，应由业务策略决定；默认允许以新 execution 继续同一 Task，但不能创建第二个 Task。

CONTINUE 的目标通常传入：

~~~text
task=resolved_task
target_context=resolved_target
~~~

### 3. MODIFY_TASK / UPDATE

MODIFY_TASK 表示修改已有 Task 或其 Artifact。

解析与 CONTINUE 相同，但需要额外验证：

- target 必须明确。
- 目标 ArtifactRef 若存在，必须属于 Task。
- active_draft_id、active_schedule_id 只能作为 Artifact/资源辅助信息，不能替代 Task 授权。
- TaskResolver 的 artifact match 可以帮助定位，但最终仍需 Task 所有权校验。
- 编译得到的 TaskContext.goal 默认来自已有 Task.goal；当前用户的修改语义保留在 task_intent/constraints 中。

如果多个目标都匹配，不得按最近任务猜测；返回 TASK_TARGET_AMBIGUOUS。

### 4. CANCEL_TASK

CANCEL_TASK 是破坏性更强的操作，应采用更严格的目标策略：

- 允许明确 task_id。
- 允许唯一 label/artifact 匹配。
- 纯最近任务回退不应自动取消。
- 多候选必须返回 TASK_TARGET_AMBIGUOUS。
- 跨用户/租户的 task_id 必须表现为 TASK_NOT_FOUND 或 TASK_ACCESS_DENIED，不能泄露存在性。

TaskProvider 的取消操作只更新 Task 业务状态：

~~~text
TaskRegistry.update_task(task_id, status=TaskStatus.CANCELLED)
~~~

它不直接修改 PlanExecution，也不替代 ExecutionStateManager 的 cancel API。如果该 Task 有运行中的 execution，未来 Adapter/Runtime Control 层需要显式调用 execution cancellation；两者不能用一个状态字段互相冒充。

### 5. 已有 Task 目标结果

TaskProvider 不应只返回 task_id，建议返回：

~~~text
TaskBinding:
  task: Task
  target: ResolvedTaskTarget
~~~

其中 target 可直接作为 IntentCompiler.compile 的 target_context。这样可以保留匹配原因、置信度、候选和歧义信息，不必让 API 层重新构造。

## 八、最小 TaskProvider 接口

### 1. Scope

建议定义一个不可由用户文本覆盖的范围对象：

~~~text
TaskScope:
  conversation_id
  user_id
  tenant_id
~~~

来源：

- conversation_id：路由路径，并校验 conversation 所属。
- user_id/tenant_id：AuthContext。
- 不接受 body 或 LLM 提供的身份字段。

### 2. 推荐接口

Provider 可以位于：

apps/assistant_api/greenbook_assistant_api/services/task_provider.py

原因是它需要同时处理：

- 请求范围 AsyncSession
- AuthContext 所有权
- Conversation 边界
- core TaskRegistry/TaskResolver
- API 错误映射

核心领域模型和匹配逻辑继续复用 assistant_core，不需要在本阶段新增 core 状态模型。

建议协议：

~~~python
class TaskProvider(Protocol):
    async def create_new_task(
        self,
        scope: TaskScope,
        *,
        task_id: str,
        goal: str,
        goal_category: str,
        goal_summary: str | None = None,
    ) -> Task:
        ...

    async def get_task(
        self,
        scope: TaskScope,
        task_id: str,
    ) -> Task | None:
        ...

    async def list_tasks(
        self,
        scope: TaskScope,
        *,
        statuses: Sequence[TaskStatus] | None = None,
    ) -> list[Task]:
        ...

    async def resolve_existing_task(
        self,
        scope: TaskScope,
        intent: TaskIntent,
    ) -> TaskBinding:
        ...

    async def cancel_task(
        self,
        scope: TaskScope,
        intent: TaskIntent,
    ) -> Task:
        ...
~~~

说明：

- create_new_task 只创建 Task，不创建 TaskContext。
- get/list 必须在返回前完成 scope 校验。
- resolve_existing_task 复用 TaskResolver，返回 TaskBinding；无目标/歧义以稳定异常结束。
- cancel_task 只做 TaskStatus.CANCELLED 更新，不控制 execution。
- TaskProvider 不接受原始 message，不调用 IntentSpecProvider，不调用 Planner/Worker/ToolRuntime。

### 3. 推荐结果和错误

TaskBinding：

~~~text
task: Task
target: ResolvedTaskTarget
~~~

建议错误码：

~~~text
TASK_SCOPE_INVALID
TASK_NOT_FOUND
TASK_ACCESS_DENIED
TASK_TARGET_REQUIRED
TASK_TARGET_AMBIGUOUS
TASK_NOT_ACTIVE
TASK_STATE_CONFLICT
TASK_PROVIDER_UNAVAILABLE
TASK_CREATE_FAILED
TASK_CANCEL_FAILED
~~~

错误必须区分：

- 没有候选：TASK_NOT_FOUND
- 有多个候选：TASK_TARGET_AMBIGUOUS
- 有候选但不属于 scope：对外可统一为 TASK_NOT_FOUND，避免泄露存在性
- 已取消/不可继续：TASK_NOT_ACTIVE
- 数据库/连接异常：TASK_PROVIDER_UNAVAILABLE

### 4. session 生命周期

推荐实现方式：

~~~text
TaskProvider operation
  -> async with session_ctx() as session
  -> TaskRegistry(session)
  -> TaskRepository/TaskResolver
  -> return detached Task/TaskBinding
  -> session closes
~~~

返回的 Task 是 Pydantic 快照，不应持有 AsyncSession 或 ORM lazy relationship。

不建议：

- app.state.task_registry = TaskRegistry(singleton_session)
- provider 持有请求 session 到 background execution
- SSE/streaming 使用已经退出的 session_ctx
- 在请求异常时跳过 rollback

## 九、Conversation active task 的处理

数据库层已有：

- assistant_conversations.active_task_id
- migration 002 对该列的添加

但当前 Assistant API 使用内存 conversation_store，SessionContext 没有 active_task_id，ConversationRepository 也没有在当前 message route 中接入。

最小接入策略：

1. NEW_TASK 产生 Task 后，未来 Adapter 可以把 task_id 作为当前 conversation binding 的候选。
2. CONTINUE/MODIFY/CANCEL 优先使用 IntentSpec/TaskIntent 的显式目标和 TaskResolver。
3. 不为本阶段临时把 active_task_id 加进 SessionContext 或旧 routes。
4. 若需要使用 active_task_id，未来应从受授权的 ConversationRepository 读取，并作为 target_context 的辅助，不作为绕过 TaskResolver/所有权校验的后门。
5. IntentCompiler 对既有 Task 优先使用 target_context；这样不依赖当前 SessionContext 的缺失字段。

## 十、与 IntentCompiler 的边界

未来 Adapter 的最小组合应是：

~~~text
IntentSpecProvider
  -> validated IntentSpec
  -> to_task_intent
  -> TaskProvider.create_new_task / resolve_existing_task
  -> IntentCompiler.compile
  -> TaskContext
~~~

NEW_TASK：

~~~text
task = TaskProvider.create_new_task(scope, ...)
task_context = IntentCompiler.compile(
    intent_spec=spec,
    task_intent=task_intent,
    task=task,
    conversation=conversation_binding,
)
~~~

CONTINUE/MODIFY：

~~~text
binding = TaskProvider.resolve_existing_task(scope, task_intent)
task_context = IntentCompiler.compile(
    intent_spec=spec,
    task_intent=task_intent,
    task=binding.task,
    target_context=binding.target,
    conversation=conversation_binding,
)
~~~

CANCEL：

- 如果只取消业务 Task，可以由 TaskProvider.cancel_task 处理。
- 如果还需要停止运行中的 execution，必须另外调用 Runtime execution control。
- 不要把 CANCEL_TASK 伪装成普通 Planner 任务。

TaskProvider 不应调用 IntentCompiler；两者由未来 ConversationRuntimeAdapter 按顺序组合，保持职责单一。

## 十一、最小实现范围

本阶段只设计。下一阶段实现时建议限制在：

|文件|用途|
|---|---|
|apps/assistant_api/greenbook_assistant_api/services/task_provider.py|TaskScope、TaskBinding、TaskProvider 协议和 SQL-backed 实现|
|apps/assistant_api/greenbook_assistant_api/main.py|只注入 session/provider factory，不修改消息路由|
|packages/assistant_core/greenbook_assistant_core/task/registry.py|如有必要增加 scoped query；不改变 Task 模型和 Runtime 状态|
|tests/unit/test_task_provider.py|创建、查询、目标解析、所有权、取消和错误边界|

可能需要的数据库启动动作：

- 复用现有 TaskRegistry.ensure_tables，或由部署迁移负责 assistant_tasks/assistant_task_intents。
- 不新增 Legacy 表。
- 不把 assistant_runs 作为 fallback。
- 不修改 Planner、Worker、ToolRuntime、ExecutionStateManager、PlanExecution 或 ExecutionEventStore。

## 十二、验收测试设计

### NEW_TASK

输入已由 IntentSpecProvider 处理：

~~~text
帮我写一篇AI Agent学习路线帖子
~~~

断言：

1. TaskProvider 创建一个新的 task_id。
2. conversation_id、user_id、tenant_id 来自 scope。
3. goal 和 goal_category 正确。
4. status=READY，而不是 Registry 默认的 COMPLETED。
5. TaskContext.task_id 与 Task.task_id 相等。
6. 不读取 assistant_runs、run_store 或 message_store。

### CONTINUE_TASK

覆盖：

- 明确 task_id 精确命中。
- “Java文章”唯一 label 命中。
- Artifact summary 命中。
- “刚才那个”单候选命中。
- 多任务时返回 TASK_TARGET_AMBIGUOUS。
- 无任务时返回 TASK_NOT_FOUND。
- 已 CANCELLED 任务返回 TASK_NOT_ACTIVE。

### MODIFY_TASK / UPDATE

覆盖：

- 目标 Task 所有权正确。
- 目标 ArtifactRef 属于 Task。
- 多目标不猜测。
- TaskContext.target 保存 ResolvedTaskTarget。

### CANCEL_TASK

覆盖：

- 明确 ID 可以取消。
- 唯一 label 可以取消。
- 最近任务回退不能静默取消。
- 跨 user/tenant 不能泄露存在性。
- Task 状态变为 CANCELLED。
- 不修改 PlanExecution 状态。

### 生命周期

覆盖：

- 每次 provider 操作退出 session_ctx。
- 正常完成后连接归还池。
- 异常触发 rollback。
- provider 返回的是 detached Pydantic Task 快照。
- 不在 app.state 持有 AsyncSession。

## 十三、当前仍存在的明确缺口

1. Assistant API 没有 TaskProvider 接线。
2. main.py 没有 DB session factory/provider factory。
3. 当前 message route 仍使用 conversation_store 和 CommunityOperationsAssistant。
4. SessionContext 没有 active_task_id，数据库绑定列未被当前入口使用。
5. TaskRepository 查询缺少 user/tenant scoped 条件。
6. TaskRegistry.resolve_task 的最近任务回退不适合 CANCEL 或歧义场景。
7. TaskRegistry.create_task 默认 COMPLETED，不能直接用于 Runtime NEW_TASK。
8. TaskProvider 尚未把 Task 绑定交给 IntentCompiler。
9. RuntimeAgentService 仍会在缺 TaskContext 时返回 TASK_CONTEXT_REQUIRED；这是正确保护，不应绕过。
10. 本阶段没有执行 ConversationRuntimeAdapter，也没有改变 Legacy 路由。

## 十四、最终设计判断

最小、可回滚的 Task Provider 应该是 Assistant API 层的请求范围服务：

~~~text
AuthContext + conversation_id
  -> TaskScope
  -> session_ctx
  -> TaskRegistry
  -> TaskResolver
  -> Task / TaskBinding
  -> IntentCompiler
  -> TaskContext
~~~

复用现有 core Task 模型和 Resolver，新增的只是 API 边界和 session 生命周期封装。TaskProvider 不负责理解、计划、执行或呈现，也不读取 Legacy 状态。

下一步实现顺序建议：

1. 先实现 TaskScope、TaskBinding 和 provider 单元测试。
2. 接入请求范围 session/provider factory，不改 message route。
3. 先验证 NEW_TASK。
4. 再验证 CONTINUE/MODIFY/CANCEL 的 scoped resolution。
5. 最后才让 ConversationRuntimeAdapter 使用 TaskProvider。

本文件只完成 Step2-B-1-b 设计分析；完成后应等待确认，不执行 Task Provider 或消息迁移。
