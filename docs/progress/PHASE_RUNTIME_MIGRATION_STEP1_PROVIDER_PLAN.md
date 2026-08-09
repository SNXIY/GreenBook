# Phase Runtime Migration Step1-A：Runtime Provider 接线方案

## 状态与范围

- 分支：feature/runtime-http-migration
- 当前基线：e31fa28 docs(runtime): record step1 dependency analysis
- 本阶段性质：只读设计与文档化
- 本阶段不修改：main.py、旧 routes.py、Runtime 核心状态模型、数据库 schema、迁移脚本
- 本阶段不引入：RuntimeContainer、新的状态模型、Legacy fallback

本方案只描述将现有 Runtime 类实例放入 FastAPI app.state 的最小接线方式，为后续 Step2 注册 HTTP router 做准备。

## 1. main.py 的具体修改位置

当前文件：apps/assistant_api/greenbook_assistant_api/main.py

### 1.1 import 区域

当前 main.py 只导入旧 API router：

~~~python
from .api.routes import router
~~~

后续实现需要增加 Runtime 核心类和 RuntimeAgentService 的导入，并把 Runtime router 以别名导入，避免与旧 router 混淆：

~~~python
from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager

from .api.routes import router
from .api.runtime_routes import router as runtime_router
from .services.runtime_agent_service import RuntimeAgentService
~~~

本阶段不实际修改 import。runtime_router 的注册属于 Step2 的 HTTP 接入动作，需在确认本方案后执行。

### 1.2 lifespan 初始化区域

当前 lifespan 已按以下顺序初始化 Java、Creator、MCP、身份解析器、LLM 与旧 Assistant store：

~~~python
app.state.java = ...
app.state.creator = ...
app.state.mcp = ...
app.state.auth_resolver = ...
app.state.llm = ...
app.state.model = ...

app.state.conversation_store = {}
app.state.run_store = {}
app.state.approval_store = {}
app.state.message_store = {}
~~~

Runtime provider 应放在旧 store 初始化之后、ready 日志之前。原因是：

1. 旧路由的 provider 不被替换；
2. Runtime 的共享依赖在应用 yield 前完成；
3. RuntimeAgentService 和 HTTP runtime routes 使用同一组 repository/event store 实例；
4. 后续请求不再通过 route handler 临时创建状态对象。

建议接线顺序：

~~~python
execution_repository = ExecutionRepository()
execution_event_store = ExecutionEventStore()
execution_state_manager = ExecutionStateManager(
    repository=execution_repository,
    event_store=execution_event_store,
)
execution_runtime_manager = RuntimeManager(
    state_manager=execution_state_manager,
)
runtime_agent_service = RuntimeAgentService(
    repository=execution_repository,
    event_store=execution_event_store,
)

app.state.execution_repository = execution_repository
app.state.execution_event_store = execution_event_store
app.state.execution_state_manager = execution_state_manager
app.state.execution_runtime_manager = execution_runtime_manager
app.state.runtime_agent_service = runtime_agent_service
~~~

这里的对象均为已有类；没有新增生命周期管理抽象。

### 1.3 create_app 路由注册区域

当前 create_app 只注册旧 router：

~~~python
app.include_router(router)
~~~

Step2 在确认 provider 方案和 URL contract 后，再增加 Runtime router。推荐显式使用 API 前缀：

~~~python
app.include_router(router)
app.include_router(runtime_router, prefix="/api/v1")
~~~

如果客户端 contract 已约定无前缀，则应先完成 contract 决策，不能在 Step1-A 隐式改变路径。当前 runtime_routes.py 的路由自身没有 prefix，直接注册会暴露 /executions；这正是 Step2 需要验证的路径差异。

## 2. 当前已有 dependency/provider

| provider | 当前来源 | 当前用途 | 是否可供 Runtime 复用 |
|---|---|---|---|
| JavaClient | main.py lifespan，app.state.java | Java Backend HTTP 调用 | RuntimeContext/MCP 可间接使用 |
| CreatorClient | main.py lifespan，app.state.creator | Creator HTTP 调用 | RuntimeContext/MCP 可间接使用 |
| GreenBookMCPServer | main.py lifespan，app.state.mcp | MCP capability/tool 入口 | RuntimeAgentService 执行时需要传入 context |
| AuthContextResolver | main.py lifespan，app.state.auth_resolver | JWT/JWKS 校验 | HTTP middleware 使用 |
| AsyncOpenAI | main.py lifespan，app.state.llm | 旧 Assistant/LLM 调用 | RuntimeAgentService 当前不通过构造参数接收 |
| conversation_store/run_store/approval_store/message_store | main.py lifespan | 旧 routes.py 的内存状态 | Runtime 状态来源不应复用 |
| ExecutionRepository | 当前没有 main.py provider | PlanExecution CRUD；模块级内存 store | Step2 建立一个共享实例 |
| ExecutionEventStore | 当前没有 main.py provider | ExecutionEvent 事件内存存储 | Step2 建立一个共享实例 |
| ExecutionStateManager | 当前没有 main.py provider | PlanExecution 生命周期 | 由共享 repository/event store 构造 |
| RuntimeManager | 当前没有 main.py provider | Runtime HTTP 控制适配器 | 由共享 state manager 构造 |
| RuntimeAgentService | 当前没有 main.py provider | Intent/plan/worker 的 Runtime service | 由同一 repository/event store 构造 |
| execution_authorizer | 当前不存在生产 provider | Runtime execution 访问控制 | 不能使用默认 allow-all |

ExecutionRepository 当前是 packages/assistant_core/greenbook_assistant_core/execution/repository.py 的内存实现，模块级 store 使同一进程内的不同实例仍可看到相同 PlanExecution，但 app.state 只保留一组显式共享实例，便于保证依赖关系和测试可控。PostgresExecutionRepository 等持久化适配器存在，但当前 main.py 没有数据库配置或初始化接线，本阶段不改变持久化策略。

ExecutionEventStore 当前由 packages/assistant_core/greenbook_assistant_core/execution/event_store.py 提供，事件存放在实例级内存字典中。因此 RuntimeAgentService、ExecutionWorker、ExecutionStateManager 和 runtime_routes 必须共享 app.state.execution_event_store，不能让 runtime_routes 回退到临时 EventStore。

## 3. 每个 app.state 字段的来源与装配关系

### app.state.execution_repository

来源：greenbook_assistant_core.execution.repository.ExecutionRepository。

装配：在 lifespan 中创建一次：

~~~python
execution_repository = ExecutionRepository()
~~~

用途：保存和读取 canonical PlanExecution/StepExecution。禁止从 assistant_runs、CommunityOperationsAssistant 或 Legacy RunRepository 取 Runtime 状态。

注意：这是当前实现的进程内内存存储；服务重启或多进程部署不会提供跨进程持久化。Step2 不应借此声称完成生产持久化。

### app.state.execution_event_store

来源：greenbook_assistant_core.execution.event_store.ExecutionEventStore。

装配：

~~~python
execution_event_store = ExecutionEventStore()
~~~

用途：ExecutionStateManager/ExecutionWorker 发布事件，runtime_routes 的 events 和 stream 读取同一事件源。

### app.state.execution_state_manager

来源：greenbook_assistant_core.execution.state_manager.ExecutionStateManager。

装配：

~~~python
execution_state_manager = ExecutionStateManager(
    repository=execution_repository,
    event_store=execution_event_store,
)
~~~

用途：唯一的 PlanExecution 生命周期管理入口。其内部 repository/event_store 必须是上面两个共享对象，而不是省略参数后自动创建的默认实例。

### app.state.execution_runtime_manager

来源：greenbook_assistant_core.execution.runtime_manager.RuntimeManager。

装配：

~~~python
execution_runtime_manager = RuntimeManager(
    state_manager=execution_state_manager,
)
~~~

用途：runtime_routes 的查询、pause/resume/cancel 等 HTTP 控制适配。传入现有 state manager 可避免 route helper 创建另一套状态依赖。

checkpoint_store 目前可选且默认使用 RuntimeManager 内存 checkpoint；Step2 不新增持久化 checkpoint provider，除非后续阶段明确配置。

### app.state.runtime_agent_service

来源：apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py 的 RuntimeAgentService。

其构造函数当前只声明两个 Runtime 持久化依赖：

~~~python
RuntimeAgentService(
    *,
    repository: ExecutionRepository | None = None,
    event_store: ExecutionEventStore | None = None,
)
~~~

装配：

~~~python
runtime_agent_service = RuntimeAgentService(
    repository=execution_repository,
    event_store=execution_event_store,
)
~~~

RuntimeAgentService 在执行过程中构造 ExecutionWorker，并将这两个共享对象继续传入。Java、Creator、MCP、auth、用户信息、时区等属于每次 RuntimeContext 的请求级数据，不应伪装成当前 service 构造参数；Step3 的消息入口适配器负责组装 RuntimeContext。

### app.state.execution_authorizer

当前没有可复用的生产实现。runtime_routes 对 execution list 要求 auth_context 和 execution_authorizer；控制接口在存在 authorizer 时会调用它。PlanExecution 当前字段没有 user_id/tenant_id 所有权字段，无法安全地从执行对象推导租户归属。

因此方案是：

1. 不创建 allow-all 默认 authorizer；
2. 不把缺失 authorizer 当作已完成的安全接线；
3. Step2 在注册对外 Runtime API 前明确访问策略；
4. 若暂时没有 ownership mapping，list/控制 API 应保持 fail-closed，或只在受控测试注入 request/app.state authorizer；
5. 后续应在不改变 PlanExecution schema 的前提下建立 execution_id 到 AuthContext 的受控映射，再提供真实 authorizer。

## 4. 是否需要新增 RuntimeContainer

结论：Step1-A/Step2 不需要新增 RuntimeContainer，可以复用现有类和显式 app.state provider。

理由：

- RuntimeAgentService、RuntimeManager、ExecutionStateManager 都已有稳定构造函数；
- 依赖图只有一组 repository/event store/state manager/runtime manager/service；
- FastAPI lifespan 已是当前应用资源初始化边界；
- 新 Container 会增加一层生命周期和测试复杂度，但不能解决当前缺失的 ownership/auth contract；
- 旧 routes.py 仍使用自己的 conversation/run/approval/message stores，显式 app.state 字段可以隔离两套状态。

当后续出现数据库 repository、持久化 event store、checkpoint store、retry manager、authorizer、shutdown hook 等多个环境相关依赖时，再评估 RuntimeContainer。那是后续收敛工作，不属于本步骤。

## 5. 对旧 routes.py 的影响

不会影响。

当前旧链路位于 apps/assistant_api/greenbook_assistant_api/api/routes.py，main.py 通过：

~~~python
from .api.routes import router
...
app.include_router(router)
~~~

注册。它读取 app.state.conversation_store、run_store、approval_store、message_store，并直接调用旧 Assistant 逻辑。Provider 方案：

- 不删除旧 router；
- 不重命名旧 store；
- 不把 execution_repository 绑定到 run_store；
- 不让 Runtime routes 读取 assistant_runs；
- 仅新增独立的 execution_* 和 runtime_agent_service app.state 字段。

因此，在 Step2 同时注册 Runtime router 时，旧 HTTP contract 仍可保留，回滚只需撤回 Runtime router/provider 改动，不需要恢复 Legacy 目录。

需要注意：如果未来把 conversation message 入口切换到 RuntimeAgentService，应通过独立 RuntimeAdapter；不能在本步骤直接改 routes.py。

## 6. 修改后的 main.py 结构示意

以下是确认方案后的结构示意，不是本阶段对 main.py 的实际修改：

~~~python
# existing imports
from .api.routes import router
from .api.runtime_routes import router as runtime_router
from .services.runtime_agent_service import RuntimeAgentService
from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # existing Java/Creator/MCP/Auth/LLM initialization
    app.state.java = ...
    app.state.creator = ...
    app.state.mcp = ...
    app.state.auth_resolver = ...
    app.state.llm = ...
    app.state.model = ...

    # existing legacy stores remain unchanged
    app.state.conversation_store = {}
    app.state.run_store = {}
    app.state.approval_store = {}
    app.state.message_store = {}

    # new Runtime providers, one shared graph
    execution_repository = ExecutionRepository()
    execution_event_store = ExecutionEventStore()
    execution_state_manager = ExecutionStateManager(
        repository=execution_repository,
        event_store=execution_event_store,
    )
    execution_runtime_manager = RuntimeManager(
        state_manager=execution_state_manager,
    )
    runtime_agent_service = RuntimeAgentService(
        repository=execution_repository,
        event_store=execution_event_store,
    )

    app.state.execution_repository = execution_repository
    app.state.execution_event_store = execution_event_store
    app.state.execution_state_manager = execution_state_manager
    app.state.execution_runtime_manager = execution_runtime_manager
    app.state.runtime_agent_service = runtime_agent_service

    # execution_authorizer is intentionally not assigned until ownership policy exists

    try:
        yield
    finally:
        # existing Java/Creator/LLM shutdown
        ...

def create_app(...):
    app = FastAPI(..., lifespan=lifespan)
    # existing middleware
    app.include_router(router)
    # Step2, after URL/auth decision:
    app.include_router(runtime_router, prefix="/api/v1")
    return app
~~~

## 7. Step2 前必须确认的决策

| 决策 | 当前事实 | Step2 建议 |
|---|---|---|
| Runtime URL | runtime_routes.py 自身无 prefix | 明确使用 /api/v1/executions 或兼容路径，再注册 router |
| Provider 共享 | 目前 route helper 可临时 fallback | lifespan 注入完整 provider，生产路径不依赖 fallback |
| Execution 状态来源 | PlanExecution/ExecutionStateManager | 保持 canonical Runtime 来源，不读取 assistant_runs |
| Event 来源 | ExecutionEventStore | RuntimeAgentService/Worker/routes 共用同一实例 |
| Authorizer | 无生产实现，PlanExecution 无 owner 字段 | 不 allow-all；先定义受控 ownership/auth 策略 |
| 持久化 | 当前 repository/event store 为内存实现 | 本步骤不接数据库、不做 migration |
| 旧 API | routes.py 仍在使用 | 保留并行注册，RuntimeAdapter 另行审批 |
| RuntimeContainer | 当前依赖图较小 | 暂不新增 |

## 结论

可以在现有 FastAPI lifespan 中以显式 app.state provider graph 接入 Runtime，不需要新建 RuntimeContainer，也不会影响旧 routes.py。最小安全接线是：

ExecutionRepository
→ ExecutionEventStore
→ ExecutionStateManager
→ RuntimeManager
并由同一 repository/event store 构造 RuntimeAgentService。

唯一不能在当前代码中“自动补齐”的字段是 execution_authorizer：没有现成生产实现，也没有 PlanExecution ownership 字段。注册 Runtime HTTP API 前必须单独确认这一安全边界和 URL prefix。

本报告完成的是设计，不代表 main.py 已修改；下一步应在确认后执行 Step2 的小范围接线与验证。
