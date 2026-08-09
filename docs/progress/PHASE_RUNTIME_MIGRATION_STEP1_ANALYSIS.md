# Runtime Migration Step 1 — Dependency Analysis

审计基线：649e5f7
当前 checkpoint：c24151e
当前分支：feature/runtime-http-migration

本报告只记录 Runtime HTTP 接入前的依赖分析。本阶段没有修改 Python、Java、前端或数据库代码，也没有执行迁移和测试。

## 1. 当前 main.py 生命周期结构

入口文件：apps/assistant_api/greenbook_assistant_api/main.py

### 模块加载阶段

1. 计算仓库根目录的 .env 路径。
2. 如果存在 .env，调用 load_dotenv。
3. 导入旧的 api.routes.router。

当前没有导入或注册 api.runtime_routes.router。

### create_app() 阶段

create_app() 当前执行：

1. 创建 FastAPI 应用，绑定 lifespan。
2. 安装 _JwtAuthMiddleware。
3. 安装 CORS middleware。
4. 注册旧的 api.routes.router。
5. 注册 /health。

当前没有在 create_app() 中注册 Runtime router，也没有创建 Runtime 容器或 Runtime Service。

### lifespan() 启动阶段

启动顺序如下：

1. 读取 Java、Creator、JWKS、issuer、audience、LLM 配置。
2. 要求存在 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。
3. 创建 JavaClient，写入 app.state.java。
4. 创建 CreatorClient，写入 app.state.creator。
5. 创建 GreenBookMCPServer，写入 app.state.mcp。
6. 创建 AuthContextResolver，写入 app.state.auth_resolver。
7. 创建 AsyncOpenAI，写入 app.state.llm。
8. 写入 app.state.model。
9. 创建旧的进程内 store：
   - conversation_store
   - run_store
   - approval_store
   - message_store
10. yield 运行应用。
11. 关闭 Java、Creator、LLM client。

当前 lifespan 没有初始化：

- RuntimeAgentService
- RuntimeManager
- ExecutionStateManager
- ExecutionRepository
- ExecutionEventStore
- ArtifactStore
- RetryManager
- execution_authorizer
- 数据库 engine/session 生命周期

## 2. 当前已有 dependency/provider

### Request 级 provider

_JwtAuthMiddleware 会尝试设置：

~~~text
request.state.auth_context
~~~

认证来源：

- 测试时可以使用 app.state.auth_validator
- 生产时使用 app.state.auth_resolver
- 最终调用 validate_access_token()

### Application state provider

当前 app.state 中已有：

~~~text
java
creator
mcp
auth_resolver
llm
model
auth_validator   # 可选测试 provider
conversation_store
run_store
approval_store
message_store
~~~

### 旧 API provider

api/routes.py 直接依赖：

- request.app.state.conversation_store
- request.app.state.run_store
- request.app.state.approval_store
- request.app.state.message_store
- request.app.state.llm
- request.app.state.mcp

dependencies/assistant.py 当前只有说明性模块，没有提供 Runtime dependency factory。

## 3. runtime_routes.py 所需依赖

Runtime 路由通过 Request.app.state 读取可选 provider。

### RuntimeManager 选择顺序

_manager(request) 的优先级是：

1. app.state.execution_runtime_manager
2. app.state.execution_state_manager
3. app.state.execution_repository + app.state.execution_event_store
4. 如果以上都不存在，则临时创建：

~~~python
ExecutionStateManager(
    ExecutionRepository(),
    event_store=None,
)
~~~

再包装为新的 RuntimeManager。

### 具体依赖

| Provider | 使用位置 | 当前状态 |
|---|---|---|
| execution_runtime_manager | 查询、控制、steps/events | 未初始化 |
| execution_state_manager | RuntimeManager fallback | 未初始化 |
| execution_repository | PlanExecution 查询 | 未初始化，使用内存默认值 |
| execution_event_store | 历史事件和 SSE | 未初始化，使用默认 EventStore |
| execution_checkpoint_store | checkpoint 恢复 | 可选，未初始化 |
| execution_retry_manager | step retry | 可选，未初始化 |
| execution_authorizer | execution list 和控制授权 | 未初始化 |
| request.state.auth_context | 用户认证 | Middleware 会尝试设置 |

### 路由注册前的两个重要问题

1. 当前 router 的路径是 /executions，没有 /api/v1 prefix。若产品契约要求 /api/v1/executions，注册时需要确定 prefix，而不是修改 Runtime 状态模型。

2. GET /executions 会要求 execution_authorizer。没有 authorizer 时会 fail-closed 返回 403；不能通过默认 allow-all 绕过授权。

另外，当前 status、steps、events、stream handler 对认证/资源授权的显式检查不一致。注册 Runtime API 前需要单独确认读取接口的授权策略，避免只注册路由就暴露 execution 数据。

## 4. RuntimeAgentService 构造参数

文件：apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py

构造函数只有两个可选参数：

~~~python
RuntimeAgentService(
    repository: ExecutionRepository | None = None,
    event_store: ExecutionEventStore | None = None,
)
~~~

服务内部自行创建：

- CapabilityRegistry
- CapabilityMapper
- TaskOrchestrator
- PlanValidator
- HumanInteractionManager
- MemoryManager
- detached background task registry

执行时从 RuntimeContext 获取：

- task_context
- task_intent
- mcp
- session
- auth
- user_message
- timezone

因此 RuntimeAgentService 不需要在构造函数中接收 Java/Creator/LLM；这些依赖应该在消息入口组装 RuntimeContext 时注入。

当前 _finish_execution() 内部还会直接创建 ArtifactStore()，ArtifactRepository 尚未成为可注入的 app-level provider。

## 5. ExecutionRepository 当前来源

默认实现：

~~~text
packages/assistant_core/greenbook_assistant_core/execution/repository.py
~~~

它使用模块级字典：

~~~python
_store: dict[str, PlanExecution] = {}
~~~

特点：

- 所有同一 Python 进程中的 ExecutionRepository 实例共享该字典。
- 不具备进程重启持久性。
- 未绑定用户、租户或应用生命周期。
- RuntimeAgentService(repository=None) 时由 ExecutionWorker 自动创建。
- runtime_routes 没有 provider 时也会自动创建。

PostgreSQL 适配器存在：

~~~text
packages/assistant_core/greenbook_assistant_core/execution/postgres_repository.py
~~~

但当前 main.py 没有创建或注入该适配器。它的构造接口也不是 ExecutionRepository 的直接子类，而是独立的结构兼容实现，因此后续统一持久化时需要明确 adapter/protocol 边界。

## 6. ExecutionEventStore 当前来源

默认实现：

~~~text
packages/assistant_core/greenbook_assistant_core/execution/event_store.py
~~~

它是对象级内存字典：

~~~python
self._events: dict[str, list[ExecutionEvent]]
~~~

ExecutionStateManager 还定义了模块级默认实例：

~~~python
_default_event_store = ExecutionEventStore()
~~~

当调用方不传 event_store 时，StateManager 使用这个默认实例。

当前可能出现的两种路径：

~~~text
RuntimeAgentService
  -> ExecutionWorker(event_store=None)
  -> ExecutionStateManager
  -> module-level _default_event_store
~~~

以及：

~~~text
runtime_routes
  -> app.state 没有 event_store
  -> 临时 ExecutionStateManager
  -> module-level _default_event_store
~~~

虽然单进程内可能“碰巧”看到相同默认事件，但这不是明确的应用级共享依赖，也无法覆盖自定义 EventStore 或多进程场景。

PostgreSQL EventStore 适配器存在：

~~~text
packages/assistant_core/greenbook_assistant_core/execution/persistent_stores.py
~~~

同样尚未由 Assistant lifespan 注入。

## 7. 是否需要新增 RuntimeContainer

### 结论

Step 2 不是必须新增 RuntimeContainer 类，但必须增加 lifespan 级 Runtime provider 初始化。

最小接入方式是复用现有 app.state：

~~~text
app.state.execution_repository
app.state.execution_event_store
app.state.execution_state_manager
app.state.execution_runtime_manager
app.state.runtime_agent_service
app.state.execution_retry_manager
app.state.execution_authorizer
~~~

统一关系应为：

~~~text
ExecutionRepository
        │
        ├── ExecutionStateManager
        │       │
        │       └── RuntimeManager
        │
        ├── ExecutionWorker
        │
        └── RuntimeAgentService

ExecutionEventStore
        ├── ExecutionStateManager
        ├── RuntimeManager
        ├── ExecutionWorker
        └── runtime_routes SSE/history
~~~

### 推荐顺序

1. Step 2 先用现有 app.state 完成最小共享实例接入。
2. 不立即新增 RuntimeContainer 抽象，避免为一次路由注册扩大改动面。
3. Step 3 消息入口适配器依赖变多后，再评估是否引入轻量 Container/dataclass。
4. RuntimeContainer 只能作为依赖聚合对象，不能成为新的执行状态模型。

### Step 2 的必要 provider

至少需要明确创建：

~~~text
ExecutionRepository
ExecutionEventStore
ExecutionStateManager(repository, event_store)
RuntimeManager(state_manager)
RuntimeAgentService(repository, event_store)
~~~

还需要明确：

- execution list 的 authorizer
- retry manager 是否复用同一个 StateManager
- checkpoint store 是否暂时为空
- /executions 是否挂载 /api/v1 prefix
- status/steps/events/stream 的读取授权

## 8. Step 1 结论

当前缺失的不是 Runtime 核心类，而是应用生命周期接线：

~~~text
main.py lifespan
  缺少 Runtime provider 初始化

main.py create_app
  缺少 runtime_routes 注册

runtime_routes
  缺少显式共享 RuntimeManager

RuntimeAgentService
  缺少应用级实例注册

Repository/EventStore
  默认走内存 fallback
~~~

因此下一步最小修改范围应限制在：

~~~text
apps/assistant_api/greenbook_assistant_api/main.py
~~~

并优先完成共享 provider 和 Runtime router 注册，不改 Planner、Worker、ToolRuntime、PlanExecution 或数据库迁移。
