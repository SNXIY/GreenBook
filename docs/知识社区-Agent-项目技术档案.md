# 知识社区 Agent 项目技术档案

> 审计基线：2026-08-22，工作树 feature/runtime-http-migration。
>
> 本文基于当前工作树源码、数据库定义、配置、测试和架构文档整理。当前工作树存在大量未提交修改、删除和新增文件；本文描述的是当前工作树实现，不是某个干净 Git commit 的历史快照。

## 0. 结论先行

这个项目不是一个“Java Agent 单体”，而是由 Python Agent Runtime 和 Java 知识社区业务后端组成的单仓库系统：

~~~text
前端 zhiguang-fe
        │ HTTP/SSE
        ▼
Python Agent API
  语义理解、上下文、目标/时间解析、HITL、Fast Path
        │ ExecutionInput / durable queue
        ▼
Python Agent Worker / Runtime
  ActionLoop、执行租约、检查点、重试、Operation Ledger、Reconciliation
        │ in-process MCP Tool Runtime
        ▼
Java Spring Boot Community Backend
  Draft/Post/Schedule/Comment/User/Analytics 的事实与业务规则
~~~

当前架构的主要优点：

- Agent 与 Java 业务事实分离。
- 写操作有 durable execution、queue、lease、fencing、幂等键和 postcondition verification。
- 外部写入结果不确定时会进入 RESULT_UNKNOWN，不会直接盲目重复写。
- Task/Objective、Execution、ToolResult、ResourceRef 的边界相对清楚。

当前最大的风险：

1. 当前语义 long-tail benchmark 的质量很差：60 个 primary case 的重算 exact accuracy 为 5/60，即 8.33%；primary unsafe rate 为 41/60，即 68.33%。报告文件按全部 78 个 utterance 统计为 exact 8/78，即 10.26%，unsafe 56/78，即 71.79%。
2. Python ExternalOperationRecord 已有 objective_id 字段，但 external_operation SQL 表、字段映射和反序列化没有保存它；重启后可能丢失 Objective 归属。
3. 简单 READ 走同步 Fast Path，不进入 Operation Ledger，也不总是绑定到 Task 的 resource_index；上一个 Turn 的搜索结果不能稳定地作为下一个 Turn 的自然语言引用事实。
4. HITL 有 Clarification、Task-level semantic confirmation、Tool-level approval 三套概念；生产 approval 模型没有明确的 expires_at 和一致的过期 worker，通用 HumanInteractionManager 也明确标记为尚未接入 Runtime。
5. 旧 GoalTree、GoalCompiler、DynamicPlanner、AgentLoop 仍在代码和文档中，但当前活动面以 TurnCoordinator → Objective → ActionLoop → Runtime 为准。

## 1. 项目概览

### 1.1 根目录与主要模块

~~~text
green-book/
├─ apps/
│  ├─ backend/                 Java Spring Boot 知识社区后端
│  ├─ agent_api/               FastAPI Agent API、Turn Runner、HTTP/SSE
│  ├─ agent_worker/            独立 durable worker 进程
│  └─ ...
├─ packages/
│  ├─ agent_core/              Agent Runtime 核心
│  ├─ contracts/               ToolResult、ToolContract、SemanticAction 等共享协议
│  ├─ java_client/             Python → Java Agent Facade 客户端
│  ├─ security/                身份与权限工具
│  └─ evaluation/              语义评测与 benchmark
├─ services/
│  └─ greenbook_mcp/           MCP Tool Registry、Tool Policy、Java Tool Adapter
├─ zhiguang-fe/                React/Vite 前端
├─ contracts/
│  └─ java-openapi.yaml         Agent → Java Facade 的 HTTP 合同
├─ docs/                       架构、配置、测试、审计报告
├─ evaluation/                 long-tail benchmark 数据和运行器
├─ docs/archive/evaluations/artifacts/ benchmark 历史结果产物
├─ infra/                      基础设施辅助文件
├─ scripts/                    本地运行/检查脚本
├─ tests/                      Python 单元、契约、集成测试
├─ pyproject.toml              uv workspace 与 Python 依赖
├─ docker-compose.yml          基础设施
└─ .env.example                开发环境配置样例
~~~

重要源文件：

- README.md：项目总览、端口、运行方式和边界。
- docs/architecture/CURRENT_ARCHITECTURE.md：当前活动架构，优先级高于旧架构文档。
- PROJECT_CONTEXT.md：架构意图和迁移背景，仍包含旧 Goal/Planner 路径描述。
- apps/agent_api/greenbook_agent_api/main.py：FastAPI 入口。
- apps/agent_worker/greenbook_agent_worker/main.py：独立 Worker 入口。
- apps/backend/src/main/java/com/tongji/ZhiGuangApplication.java：Java Spring Boot 入口。
- services/greenbook_mcp/greenbook_mcp_server/server.py：in-process MCP Server。

### 1.2 技术栈

| 层次 | 当前技术 | 作用 |
|---|---|---|
| Agent API | Python 3.12、FastAPI、Pydantic | 对话接口、认证、Run 接受、SSE/轮询 |
| Agent Core | Python、Pydantic、SQLAlchemy、asyncio | Context、Command、Target/Temporal、Task/Objective、ActionLoop |
| LLM | OpenAI-compatible Async SDK；开发配置默认 DeepSeek | 语义结构化输出和草稿内容生成 |
| Agent 持久化 | PostgreSQL，memory profile 可用于测试 | Conversation、Task、Execution、Queue、Ledger、Approval、Observation |
| Agent 队列 | PostgreSQL durable queue | Execution submission、lease、重试、Worker 消费 |
| Java 后端 | Java 21、Spring Boot 3.2.x、MyBatis/JDBC、Flyway | 社区业务事实、权限、草稿、发布、评论、分析 |
| Java 数据库 | MySQL | users、know_posts、comments、scheduled_publications |
| Java 基础设施 | Redis、Redisson、Kafka/Redpanda、OSS、Caffeine | 缓存、锁、消息/业务基础设施、内容对象存储 |
| 检索/记忆 | Qdrant 配置已存在 | 记忆/检索扩展；不是当前写操作事实源 |
| Tool 层 | 自定义 typed MCP registry，当前 in-process | Tool schema、policy、handler、Java adapter |
| 前端 | React + TypeScript + Vite | Conversation、Run/Activity、HITL 交互 |
| 工具链 | uv、pytest、ruff、mypy、Docker Compose、Maven | 构建、测试、开发基础设施 |

Java 配置中也有 Spring AI DeepSeek 依赖，但当前 Agent 主语义路径由 Python CommandInterpreter 使用 OpenAI-compatible client 完成；没有发现当前主路径使用 Claude 或本地模型。

### 1.3 入口点和部署形态

| 进程 | 入口 | 默认端口 | 说明 |
|---|---|---:|---|
| Agent API | apps/agent_api/greenbook_agent_api/main.py | 8094 | FastAPI；可内置 Worker |
| Agent Worker | apps/agent_worker/greenbook_agent_worker/main.py | 无 HTTP 业务端口 | queue/retry/reconciliation consumer |
| Java Backend | apps/backend/src/main/java/com/tongji/ZhiGuangApplication.java | 8080 | Spring Boot 社区后端 |
| Frontend | zhiguang-fe | 5173 | React/Vite |
| MCP | services/greenbook_mcp/greenbook_mcp_server/server.py | 无独立端口 | Agent API 进程内创建，不是独立网络 MCP |

因此系统是“多个可独立运行进程 + 共享数据库”的拆分式单仓库，而不是完全独立的微服务网格。Agent API 可以在本地 profile 内启动 in-process Worker；生产或严格部署可使用独立 agent_worker。拓扑检查要求同一环境不能同时启动两个 queue consumer。

docker-compose.yml 当前主要启动 MySQL、PostgreSQL、Redis、Redpanda/Kafka、Qdrant 等基础设施；Agent API、Worker、Java、FE 通常由宿主机单独运行。

## 2. 业务域与 Java API 映射

### 2.1 Agent 支持的业务操作

当前共享协议中的 SemanticAction 包含 18 个业务动作：

| 语义动作 | MCP Tool | Java Facade HTTP API | Agent 是否 durable |
|---|---|---|---|
| SEARCH_POSTS | community.search_public_posts | GET /api/v1/agent/posts/search | READ Fast Path；复杂检索可进 ActionLoop |
| GET_POST | community.get_post | GET /api/v1/agent/posts/{postId} | READ |
| LIST_OWN_POSTS | community.list_own_posts | GET /api/v1/agent/me/posts | READ |
| CREATE_DRAFT | content.create_draft | POST /api/v1/agent/drafts | WRITE，进入 Runtime |
| GET_DRAFT | content.get_draft | GET /api/v1/agent/drafts/{draftId} | READ |
| LIST_DRAFTS | content.list_drafts | GET /api/v1/agent/me/drafts | READ |
| UPDATE_DRAFT | content.update_draft | PUT /api/v1/agent/drafts/{draftId} | WRITE，进入 Runtime |
| DELETE_DRAFT | content.delete_draft | DELETE /api/v1/agent/drafts/{draftId} | WRITE + approval |
| DELETE_POST | community.delete_post | DELETE /api/v1/agent/posts/{postId} | WRITE + approval |
| CREATE_SCHEDULE | publication.schedule | POST /api/v1/agent/publications/schedules | WRITE，进入 Runtime |
| GET_SCHEDULE | publication.get_status | GET /api/v1/agent/publications/schedules/{scheduleId} | READ |
| UPDATE_SCHEDULE | publication.update_schedule | PUT /api/v1/agent/publications/schedules/{scheduleId} | WRITE，进入 Runtime |
| CANCEL_SCHEDULE | publication.cancel_schedule | DELETE /api/v1/agent/publications/schedules/{scheduleId} | WRITE，进入 Runtime |
| PUBLISH_NOW | publication.publish_now | POST /api/v1/agent/publications/publish-now | WRITE + approval |
| LIST_COMMENTS | interaction.list_comments | GET /api/v1/agent/posts/{postId}/comments | READ |
| REPLY_COMMENT | interaction.send_reply | POST /api/v1/agent/comments/{commentId}/replies | WRITE + approval |
| GET_POST_PERFORMANCE | analytics.get_post_performance | GET /api/v1/agent/posts/{postId}/analytics | READ |
| GET_ACCOUNT_SUMMARY | analytics.get_account_summary | GET /api/v1/agent/me/analytics/summary | READ |

Java Facade 合同在 contracts/java-openapi.yaml；实现位于 AgentFacadeController.java、AgentFacadeService.java 和 ScheduledPublicationService.java。

“发帖”不是单一动作：通常是 CREATE_DRAFT 先创建草稿；用户明确要求“现在发布”时走 PUBLISH_NOW；用户要求未来发布时走 CREATE_SCHEDULE，到点后的真正发布由 Java Scheduled Publication 逻辑负责。

### 2.2 业务实体归属

| 实体 | 事实归属 | 当前实现 |
|---|---|---|
| Conversation/Message/Run | Agent Runtime | PostgreSQL assistant_* 表 |
| SessionContext | Agent Runtime 的跨轮次绑定 | Conversation JSON 字段持久化 |
| Task | Agent Runtime | assistant_tasks |
| Objective | Agent Runtime | assistant_tasks.objectives JSONB 元素，不是独立表 |
| Goal/GoalTree | Agent 兼容层/旧规划面 | packages/agent_core/.../goal/models.py |
| Draft | Java 业务事实 | know_posts 中 status=draft；正文在 OSS |
| Post | Java 业务事实 | know_posts |
| User | Java 业务事实 | users |
| Comment | Java 业务事实 | comments |
| Schedule | Java 业务事实 | scheduled_publications |
| Execution/Step | Agent Runtime | execution、execution_step 等 |
| Operation Ledger | Agent Runtime | 表名是 external_operation |
| ResourceRef/TaskResourceRef | Agent 侧资源投影 | 不能替代 Java 事实表 |

Agent Python 代码没有独立的 Post、Draft、User、Comment 领域实体；它使用 Java 返回 DTO、ToolResult、ResourceRef 和资源 ID。

## 3. 请求处理链路

~~~text
用户消息
  │
  ▼
POST /api/v1/agent/conversations/{conversation_id}/messages
  │ routes.py：认证、scope 校验、写 Message、建立/复用 Run
  │ 同一会话中旧的 WAITING_HUMAN/WAITING_USER Run 会被新消息 supersede
  ▼
AgentRun(status=ACCEPTED)
  │
  ▼
AgentRunner.claim(run)
  │
  ▼
TurnCoordinator.execute(turn_request)
  │
  ├─ ContextAssembler.assemble()
  │    └─ Conversation + SessionContext + Task + Artifact + Execution + Memory
  │
  ├─ CommandInterpreter.interpret(message, ContextSnapshot)
  │    └─ StructuredCommandOutput → Pydantic → Command
  │       → deterministic semantic derivation → ResolvedSemanticState
  │
  ├─ TargetResolver.resolve(command, context)
  │    └─ RESOLVED / AMBIGUOUS / NOT_FOUND
  │
  ├─ TemporalResolver.resolve_result(...)
  │    └─ NONE / NOW / FUTURE / UNRESOLVED
  │
  ├─ SemanticConfirmationPolicy
  │    └─ 必要时 Task → CONFIRMATION_PENDING
  │
  └─ FastPathGate
       ├─ CHAT / CLARIFY / 简单 QUERY
       │    └─ FastPathExecutor → MCP READ → Java READ
       ├─ 简单 WRITE
       │    └─ FastPathExecutor → Policy → ExecutionInput → Runtime
       └─ COMPLEX
            └─ ActionLoopExecutor → ActionLoop
                 ├─ 规则决定 deterministic action
                 ├─ 必要时 action decision LLM
                 ├─ READ 直接执行
                 └─ WRITE 进入 Runtime/OperationLedger/Queue

ExecutionInput
  │ 不包含原始用户文本
  ▼
PostgreSQL execution_queue_message
  │ lease / fencing / heartbeat
  ▼
Agent Worker
  │ PlanExecution / StepExecution / checkpoint
  ▼
ToolRuntime → GreenBookMCPServer → JavaClient
  │
  ├─ Java response + postcondition verification
  ├─ ToolResult
  ├─ ActionObservation / ResourceRef / ObjectiveReducer
  └─ completion 或 RESULT_UNKNOWN → Reconciliation

assistant_messages / assistant_runs projection / activity / SSE
  │
  ▼
前端展示最终答复、等待确认、等待外部结果或失败原因
~~~

关键入口：

- apps/agent_api/greenbook_agent_api/api/routes.py
- apps/agent_api/greenbook_agent_api/runner.py
- apps/agent_api/greenbook_agent_api/services/turn_coordinator.py
- packages/agent_core/greenbook_agent_core/turn/context_assembler.py
- packages/agent_core/greenbook_agent_core/turn/fast_path_gate.py

旧文档中常见的 ConversationService → ContextBuilder → CommandInterpreter → GoalDecomposer → TaskManager → AgentLoop → DynamicPlanner → ToolSelector → PolicyGate → GoalCompiler → ExecutionInput 链路仍有兼容代码，但不应当被当作当前主入口。

### 3.1 状态转换

~~~text
Run/Execution:
ACCEPTED → RUNNING
             ├→ WAITING_HUMAN
             ├→ WAITING_APPROVAL
             ├→ WAITING_EXTERNAL
             ├→ COMPLETED
             ├→ FAILED
             ├→ CANCELLED
             └→ SUPERSEDED

Task:
CREATED → PLANNING → READY → RUNNING
                         ├→ WAITING_HUMAN
                         ├→ WAITING_EXTERNAL
                         ├→ PAUSED
                         ├→ COMPLETED
                         ├→ FAILED
                         └→ CANCELLED

Objective:
PENDING → IN_PROGRESS → COMPLETED
                    ├→ WAITING
                    ├→ FAILED
                    ├→ CANCELLED
                    └→ SUPERSEDED
~~~

Objective 只有在真实 Resource、Operation 或 Artifact 证据满足预期后置条件时才完成；不能凭 LLM 的 FINISH 声明完成。

## 4. 语义理解层

### 4.1 LLM、Prompt、Schema

实现位置：packages/agent_core/greenbook_agent_core/command/interpreter.py。

当前 Python LLM 是 OpenAI-compatible Async client。DeepSeek provider 或不支持 strict schema 的 provider 会使用 json_object，并把完整 Pydantic schema 放进 system prompt；支持时使用 strict JSON Schema。

Prompt 位于 interpreter.py 的 COMMAND_SYSTEM_PROMPT；草稿正文生成 prompt 位于 services/greenbook_mcp/greenbook_mcp_server/tools/content.py。

Prompt 约束包括：

- 粗粒度 command 只有 CREATE、MODIFY、CANCEL、QUERY、CONTROL。
- 业务操作用 canonical semantic action 表达。
- 生成内容对应 GENERATE_CONTENT；未来发布对应 SCHEDULE_PUBLISH。
- 删除、立即发布、回复必须保留目标和风险信息。
- 模型只产生自然语言时间和引用证据，不能自行生成权威 UTC 时间。
- 一个连接的流水线是一个业务 item；多个独立最终交付物才拆多个 item。
- needs_clarification、confidence、ambiguity 都是证据，不是执行授权。
- prompt 明确要求不要输出计划或 GoalTree；计划由运行时生成。

### 4.2 当前语义模型

~~~python
class CommandType(StrEnum):
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    CANCEL = "CANCEL"
    QUERY = "QUERY"
    CONTROL = "CONTROL"


class TargetKind(StrEnum):
    TASK = "TASK"
    DRAFT = "DRAFT"
    SCHEDULE = "SCHEDULE"
    POST = "POST"
    EXECUTION = "EXECUTION"
    APPROVAL = "APPROVAL"


class TargetReferenceType(StrEnum):
    NONE = "NONE"
    ACTIVE = "ACTIVE"
    IDENTIFIER = "IDENTIFIER"
    ORDINAL = "ORDINAL"
    PROPERTY = "PROPERTY"
    TEMPORAL = "TEMPORAL"
    FAILED = "FAILED"


class CommandItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    topic: str = ""
    requirements: list[str] = Field(default_factory=list)
    operation: str = "CREATE"
    capabilities: list[str] = Field(default_factory=list)
    temporal_text: str = ""
    constraints: dict[str, Any] = Field(default_factory=dict)


class ResolvedSemanticItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    topic: str = ""
    requirements: list[str] = Field(default_factory=list)
    operation: str = "CREATE"
    capabilities: list[str] = Field(default_factory=list)
    publication_intent: str = ""
    temporal_text: str = ""
    temporal_kind: str = "NONE"
    run_at: str | None = None
    temporal_resolved: bool = False
    constraints: dict[str, Any] = Field(default_factory=dict)
    target_reference: dict[str, Any] = Field(default_factory=dict)


class ResolvedSemanticState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_command_id: str = ""
    operation: str = ""
    semantic_operation: str = ""
    capabilities: list[str] = Field(default_factory=list)
    publication_intent: str = ""
    target_type: str = ""
    target_reference: dict[str, Any] = Field(default_factory=dict)
    resolved_target: dict[str, Any] = Field(default_factory=dict)
    target_candidates: list[dict[str, Any]] = Field(default_factory=list)
    temporal_kind: str = "NONE"
    run_at: str | None = None
    temporal_resolved: bool = False
    constraints: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    clarification_required: bool = False
    clarification_reason: str = ""
    risk: str = ""
    requires_approval: bool = False
    items: list[ResolvedSemanticItem] = Field(default_factory=list)
    objectives: list[dict[str, Any]] = Field(default_factory=list)
~~~

TaskDelta 的关键结构：

~~~python
class TaskDeltaOperation(StrEnum):
    CREATE_TASK = "CREATE_TASK"
    ADD_GOAL = "ADD_GOAL"
    UPDATE_GOAL = "UPDATE_GOAL"
    CANCEL_GOAL = "CANCEL_GOAL"
    CANCEL_TASK = "CANCEL_TASK"
    CONTINUE_TASK = "CONTINUE_TASK"
    NO_CHANGE = "NO_CHANGE"
    ASK_USER = "ASK_USER"


class TaskDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: TaskDeltaOperation = TaskDeltaOperation.NO_CHANGE
    change_id: str = ""
    target_reference: dict[str, Any] = Field(default_factory=dict)
    desired_changes: dict[str, Any] = Field(default_factory=dict)
    dependency_reference: list[str] = Field(default_factory=list)
    source_reference: dict[str, Any] = Field(default_factory=dict)
    needs_target_resolution: bool = False
~~~

源码：packages/agent_core/greenbook_agent_core/command/models.py。

### 4.3 Goal 类完整定义

Goal 是旧 GoalTree/Compiler 兼容模型：

~~~python
class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    goal_id: str = Field(min_length=1)
    description: str = ""
    goal_type: str = "TASK"
    parent_goal: str | None = None
    children: list[Goal] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    semantic_operation: str = ""
    target: dict[str, Any] = Field(default_factory=dict)
    temporal_constraint: dict[str, Any] = Field(default_factory=dict)
    publication_intent: str = ""
    expected_outputs: list[str] = Field(default_factory=list)
~~~

完整源码：packages/agent_core/greenbook_agent_core/goal/models.py。

### 4.4 TargetReference、TemporalExpression、Constraint 是否存在

用户问题中的三个类名在当前源码中不存在：

- 没有独立的 TargetReference 类；当前使用 TargetReferenceType、CommandTarget、TaskDelta.target_reference 字典。
- 没有独立的 TemporalExpression 类；当前使用 CommandItem.temporal_text、constraints 字典和 TemporalResolution。
- 没有独立的 Constraint 类；约束是 Command/CommandItem/Objective 的 dict，时间约束由 TemporalResolver 规范化。

语义层没有 CommandType.UNKNOWN。空输入、不可修复 schema、无法安全解析的引用/时间会进入错误或 clarification，不会执行。执行层 RESULT_UNKNOWN 是完全不同的“外部写入结果不确定”。

## 5. Resolution 层

### 5.1 TargetResolver

实现位置：packages/agent_core/greenbook_agent_core/command/target.py。

~~~python
class TargetResolver:
    def resolve(
        self,
        command: Any,
        context: Any | None = None,
    ) -> TargetResolution:
        ...

    def resolve_task_delta(
        self,
        delta: Any,
        candidates: Sequence[Mapping[str, Any]],
        *,
        active_task_id: str = "",
        conversation_focus_task_id: str = "",
    ) -> TargetResolution:
        ...
~~~

结果结构：

~~~python
class TargetCandidate(BaseModel):
    kind: TargetKind = TargetKind.TASK
    id: str
    task_id: str | None = None
    resource_id: str | None = None
    artifact_id: str | None = None
    execution_id: str | None = None
    label: str | None = None
    status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TargetResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"


class TargetResolution(BaseModel):
    status: TargetResolutionStatus
    target: TargetCandidate | None = None
    candidates: list[TargetCandidate] = Field(default_factory=list)
    reason: str = ""

    @property
    def is_resolved(self) -> bool:
        return self.status == TargetResolutionStatus.RESOLVED and self.target is not None

    @property
    def is_ambiguous(self) -> bool:
        return self.status == TargetResolutionStatus.AMBIGUOUS
~~~

候选来源：

- active_tasks 和未完成 Objective。
- artifacts、resource_index、execution refs。
- SessionContext.active_*、recent_entities、conversation focus。
- ContextBuilder 限制后的 bounded candidates。

TargetResolver 不调用 Java，也没有缓存。若资源没有进入 ContextSnapshot，就不能主动发现它。

引用行为：

| 用户表达 | 当前处理 |
|---|---|
| “Java 那篇” | 在 bounded candidate 的 label/goal/description 中做规范化匹配；0 个 NOT_FOUND，多个 AMBIGUOUS，不随便选择最近目标。 |
| “上一篇” | ORDINAL；按候选创建顺序处理，不等价于无条件取数据库最新记录。 |
| draft_123 | explicit identifier，并结合 DRAFT 类型约束。 |
| ACTIVE | 只有 active binding 或唯一候选时解析；多个候选仍然歧义。 |
| “失败的那次” | FAILED reference，只看失败执行/Task 范围。 |

### 5.2 TemporalResolver

实现位置：packages/agent_core/greenbook_agent_core/execution/temporal_resolver.py；底层解析器在 packages/agent_core/greenbook_agent_core/time_parser.py。

~~~python
@dataclass(frozen=True, slots=True)
class TemporalResolution:
    intent: str = "NONE"       # NONE | NOW | FUTURE
    resolved: bool = False
    run_at: str | None = None  # UTC ISO timestamp
    source_text: str = ""
    timezone: str = "Asia/Shanghai"
    unresolved_reason: str = ""

    @property
    def temporal_kind(self) -> str:
        if self.intent == "NOW":
            return "NOW"
        if self.intent == "FUTURE":
            return "FUTURE" if self.resolved else "UNRESOLVED"
        return "NONE"


class TemporalResolver:
    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now

    def resolve(
        self,
        text: str,
        *,
        constraints: Iterable[Any] = (),
        timezone: str = "Asia/Shanghai",
        now: datetime | None = None,
        immediate: bool = False,
    ) -> str | None:
        return self.resolve_result(
            text,
            constraints=constraints,
            timezone=timezone,
            now=now,
            immediate=immediate,
        ).run_at
~~~

解析顺序是 constraint value 优先，再尝试完整时间文本：

- 明天下午三点：按会话时区解释，再转 UTC。
- 五分钟后：使用注入的 now 加五分钟。
- ISO timestamp：规范化。
- 今天/现在：在 immediate 条件下可解析为 NOW。
- 非空但无法解析的未来表达式：FUTURE + resolved=False + UNRESOLVED，必须澄清。

测试示例，假设 now 为 2026-08-20 10:00 Asia/Shanghai：

~~~text
明天下午 2 点发布 → 2026-08-21T06:00:00Z
五分钟后发布     → 2026-08-20T02:05:00Z
~~~

## 6. Objective、Task 和 Goal

### 6.1 Objective 完整定义

~~~python
class ObjectiveStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    WAITING = "WAITING"


class Objective(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    description: str = ""
    intent: str = ""
    status: ObjectiveStatus = ObjectiveStatus.PENDING
    expected_resource_kind: str = ""
    result_requirement: str = "DIRECT_RESULT"
    min_sources: int = 1
    constraints: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    expected_postcondition: dict[str, Any] = Field(default_factory=dict)
    related_resource_ids: list[str] = Field(default_factory=list)
    related_artifact_ids: list[str] = Field(default_factory=list)
    related_operations: list[str] = Field(default_factory=list)
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
~~~

result_requirement 的含义：

- DIRECT_RESULT：查询结果本身就是回答。
- RESOURCE_MUTATION：必须观察到资源后置条件改变。
- GROUNDED_SYNTHESIS：必须基于真实证据生成新的总结或比较结果。

### 6.2 Task 完整字段

~~~python
class TaskStatus(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    PAUSED = "PAUSED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskConfirmationState(StrEnum):
    RESOLVED = "RESOLVED"
    AUTO_ADMITTED = "AUTO_ADMITTED"
    CONFIRMATION_PENDING = "CONFIRMATION_PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class TaskExecutionRef(BaseModel):
    execution_id: str
    task_id: str
    goal_id: str | None = None
    status: str = "PENDING"
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class TaskResourceRef(BaseModel):
    resource_id: str
    resource_kind: str = ""
    objective_id: str | None = None
    title: str | None = None
    status: str | None = None
    scheduled_at: str | None = None
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Task(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    user_id: str
    tenant_id: str

    goal: str = ""
    goal_category: str = ""
    goal_summary: str | None = None

    status: TaskStatus = TaskStatus.CREATED
    phase: str | None = None
    priority: int = 0
    task_type: str = "GOAL_DRIVEN"
    execution_mode: str = "AUTO"

    requires_confirmation: bool = False
    confirmation_state: TaskConfirmationState = TaskConfirmationState.RESOLVED
    confirmation_version: int = 0
    confirmed_version: int | None = None
    confirmation_snapshot_hash: str | None = None
    confirmation_resume_run_id: str | None = None

    root_goal_id: str | None = None
    goal_tree_version: int = 0
    goal_tree_snapshot: dict[str, Any] = Field(default_factory=dict)
    plan_version: int = 0
    plan_history: list[Any] = Field(default_factory=list)
    active_execution_id: str | None = None

    artifacts: list[Any] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    goals: list[Any] = Field(default_factory=list)
    objectives: list[Objective] = Field(default_factory=list)
    revisions: list[Any] = Field(default_factory=list)
    execution_refs: list[TaskExecutionRef] = Field(default_factory=list)
    resource_index: list[TaskResourceRef] = Field(default_factory=list)
    last_action: str | None = None
    action_history: list[str] = Field(default_factory=list)

    last_error: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    version: int = 1
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
~~~

源码：packages/agent_core/greenbook_agent_core/task/models.py。

一个 Task 可以有多个 Objective：

- “发三篇独立帖子” → 一个 Task，三个 Objective。
- “写一篇 Java 教程，明天下午三点发布” → 一个 Objective，多个 required capabilities，例如 GENERATE_CONTENT + SCHEDULE_PUBLISH。
- “搜索并总结” → 一个 GROUNDED_SYNTHESIS Objective，搜索结果是证据而非最终答案。

当前没有 SubObjective 类；依赖通过 Objective.dependencies、Task.depends_on、ActionStepPlan.depends_on 和 TaskExecutionRef.goal_id 表达。

## 7. HITL：Clarify、Confirmation、Approval

### 7.1 三种机制

| 机制 | 触发位置 | 典型条件 | 恢复方式 |
|---|---|---|---|
| Clarification | TurnCoordinator 或 ActionLoop CLARIFY | 目标 0 个/多个、时间无法解析、参数缺失 | 用户发新消息；旧 waiting run 通常被 supersede |
| Semantic Confirmation | TurnCoordinator write admission 前 | bulk mutation、多 non-draft write、多独立 item、依赖型多写 | POST /api/v1/agent/tasks/{task_id}/semantic-confirmation |
| Runtime Approval | ToolPolicyGate/MCP pre-execution | 删除、立即发布、回复等 destructive tool | POST /api/v1/agent/executions/{execution_id}/approve 或 run approval endpoint |

单纯 CREATE_DRAFT 通常自动准入；删除、立即发布、回复会在 Tool policy 层要求 approval。

### 7.2 Confirmation 状态机

~~~text
RESOLVED
   ├─ no confirmation needed → AUTO_ADMITTED → RUNNING
   └─ confirmation needed → CONFIRMATION_PENDING
                              ├─ CONFIRM → CONFIRMED → RUNNING
                              ├─ CANCEL  → CANCELLED
                              └─ MODIFY  → SUPERSEDED → new compilation
~~~

确认接口不是重新解释用户文本。它会把 task_id、confirmation_id、confirmation_version、snapshot hash 放入 typed resume marker；Runner 调用 resume_task(command=None)，使用已冻结的 canonical semantic state。

确认 identity 基于：

~~~text
SHA256(task_id + confirmation_version + semantic_snapshot_hash)
~~~

生产代码没有 FROZEN enum；实际等价物是 confirmation snapshot/hash/version。确认后可以 Cancel；Modify 会 supersede 旧版本并重新编译。

### 7.3 Approval 和超时缺口

相关文件：

- packages/agent_core/greenbook_agent_core/human/approval_request.py
- packages/agent_core/greenbook_agent_core/human/approval_runtime.py
- apps/agent_api/greenbook_agent_api/api/routes.py

持久化 approval 表是 assistant_approvals，状态为 PENDING/APPROVED/REJECTED，使用 CAS 防止重复决定。批准后恢复等待的 Execution step 并重新入队；拒绝取消 Execution。

通用 human/manager.py 的 HumanInteractionManager 是 infrastructure-only：使用内存 InteractionStore，默认五分钟过期，文件明确标记尚未和 Runtime 集成。生产语义确认和 approval 不依赖这个通用 manager。

当前生产 approval 模型没有 expires_at 和统一的自动过期 worker。即使 .env.example 有 approval TTL，也不能替代 durable expiration transition。Clarification 主要通过新消息 supersede/resume，没有完整的独立回答请求实体。

## 8. ActionLoop

实现位置：

- packages/agent_core/greenbook_agent_core/actionloop/loop.py
- packages/agent_core/greenbook_agent_core/actionloop/models.py
- apps/agent_api/greenbook_agent_api/services/action_loop_executor.py

### 8.1 ActionDecision

~~~python
class ActionDecisionType(StrEnum):
    CALL_TOOL = "CALL_TOOL"
    GENERATE_CONTENT = "GENERATE_CONTENT"
    COMPOSE_RESULT = "COMPOSE_RESULT"
    CLARIFY = "CLARIFY"
    WAIT = "WAIT"
    REPLAN = "REPLAN"
    FINISH = "FINISH"


class ActionDecision(BaseModel):
    decision: ActionDecisionType
    reason: str = ""
    task_id: str = ""
    semantic_action: str = ""
    capability: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    plan_steps: list[ActionStepPlan] = Field(default_factory=list)
    needs_clarification: bool = False
~~~

### 8.2 循环核心逻辑

~~~python
for iteration in range(max_iterations):
    context = observe_bounded_task_context(task_id)

    if has_in_flight_or_result_unknown_write(context):
        return WAITING_EXTERNAL

    if all_objectives_satisfied_by_verified_facts(context):
        return FINISH_COMPLETED

    if deterministic_evidence_step_is_required(context):
        decision = CALL_TOOL(read_evidence_action)
    elif deterministic_required_write_is_ready(context):
        decision = CALL_TOOL(canonical_write_action)
    else:
        decision = one_structured_action_decision_llm(context)

    if decision == FINISH and not verified_satisfaction(context):
        record_finish_blocked()
        continue
    if decision == CLARIFY:
        return WAITING_HUMAN
    if decision == WAIT:
        return WAITING_EXTERNAL
    if decision == REPLAN:
        replan_with_budget()
        continue

    guard_action_against_latest_objective_and_policy(decision)
    observation = execute_read_or_submit_durable_write(decision)
    persist_observation_and_resource_binding(observation)

    if observation.outcome in {"SUBMITTED", "RESULT_UNKNOWN"}:
        return WAITING_EXTERNAL
    if repeated_read_signature(observation):
        return FAILED_NO_PROGRESS
~~~

### 8.3 规则还是 LLM

两者混合，但 Tool 选择不是第二个自由规划 LLM：

- semantic action → capability → tool 的映射是确定性的。
- 证据获取、依赖顺序、写入边界、Objective 完成判断、no-progress 是规则/状态机。
- 没有明确 deterministic 下一步时才调用 action decision LLM。
- 模型返回的 capability/tool 会被 canonical semantic resolver 校正，不能越权。

### 8.4 READ、WRITE、终止条件

- READ：同步 MCP → Java；没有 external side-effect operation。
- WRITE：经过 ActionGuard、policy、ExecutionInput、Operation Ledger、queue、worker 和 Java postcondition verification。
- 内容生成 LLM 输出不等于完成；必须由 Java 创建并 GET 验证。

终止条件：

- 所有 Objective 满足且没有 non-terminal execution → COMPLETED。
- in-flight 或 RESULT_UNKNOWN write → WAITING_EXTERNAL。
- Clarify/approval → WAITING_HUMAN/WAITING_APPROVAL。
- 同一 READ signature 重复约 2 次，或总体 no-progress streak 约 3 次 → FAILED。
- iteration、tool、LLM、replan、failure、compose 都有预算。

## 9. Durable Runtime、Operation Ledger、Reconciliation

### 9.1 OperationLedger 数据

~~~python
class OperationStatus(StrEnum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    NOT_FOUND = "NOT_FOUND"


class RetryClassification(StrEnum):
    SAFE_RETRY = "SAFE_RETRY"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"


class ExternalOperationRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    execution_id: str
    step_id: str
    tool_name: str = ""
    status: OperationStatus = OperationStatus.CREATED
    external_operation_id: str | None = None
    receipt_id: str | None = None
    idempotency_key: str | None = None
    runtime_idempotency_key: str | None = None
    external_idempotency_key: str | None = None
    created_at: str = ""
    updated_at: str = ""
    evidence: Any | None = None
    trace_id: str = ""
    conversation_id: str = ""
    semantic_action: str = ""
    resource_type: str = ""
    resource_id: str = ""
    objective_id: str | None = None
    expected_postcondition: dict[str, Any] = Field(default_factory=dict)
    attempt: int = 0
    claim_owner: str = ""
    claim_version: int = 0
    fencing_token: str = ""
    lease_expires_at: str = ""
    side_effect_started: bool = False
    reconciliation_needed: bool = False
    retry_classification: str = ""
    reconcile_attempts: int = 0
    verified_status: str = ""
    verified_reason: str = ""
    next_reconcile_at: str = ""
~~~

### 9.2 幂等键和 fencing

实现位置：packages/agent_core/greenbook_agent_core/execution/operation_ledger.py。

~~~python
def stable_operation_id(idempotency_key: str) -> str:
    material = "greenbook:operation:" + idempotency_key
    return "op-" + str(uuid.uuid5(uuid.NAMESPACE_URL, material))


def begin_operation(*, idempotency_key: str, ...):
    operation_id = stable_operation_id(idempotency_key)
    existing = store.get(operation_id)
    if existing is not None:
        return existing
    return store.create(PENDING_RECORD)


def claim(operation_id, owner, lease_seconds=60):
    # status=PENDING + version CAS；记录 owner、lease、fencing token
    return store.claim_if_version(...)


def mark_side_effect_started(record, request_sent):
    if request_sent is not False:
        return mark_result_unknown(record)
    return record
~~~

幂等链路有三层：

1. ToolContext 根据 conversation、operation、scope 生成 greenbook operation sha256 key。
2. Runtime 根据稳定 key 生成 UUID5 operation_id，重复投递得到同一 ledger row。
3. Java IdempotencyService 使用 user_id、operation、idempotency_key 唯一约束和 request hash replay。

### 9.3 Retry

只有以下情况允许普通 retry：

- request_sent 明确为 False。
- side_effect_state 明确为 NOT_STARTED。
- 错误是 transient dependency unavailable、timeout before send、rate limit 等。
- 没有超过 step/run retry budget。

如果 request 可能已发送、side effect 已启动或客户端不知道请求是否到达，RetryDecisionEngine 拒绝普通 retry，改走 RESULT_UNKNOWN reconciliation。

安全 retry 的队列 backoff 是 exponential 并有限制；reconciliation 短 backoff：

~~~text
10s → 30s → 90s → 300s
~~~

MAX_RECONCILE_ATTEMPTS 为 4；超过后仍保持 RESULT_UNKNOWN，约每小时查询，不自动当作失败或重新写入。

### 9.4 Reconciliation

核心流程：

~~~python
for operation in ledger.find_reconciliation_needed(now=utcnow()):
    observed = java_adapter.reconcile(
        semantic_action=operation.semantic_action,
        external_operation_id=operation.external_operation_id,
        receipt_id=operation.receipt_id,
        resource_id=operation.resource_id,
        expected_postcondition=operation.expected_postcondition,
    )

    if observed == VERIFIED_COMPLETED:
        execution.reconcile_step_succeeded(operation)
        ledger.reconcile_unknown(operation, SUCCEEDED, evidence=...)
    elif observed == VERIFIED_FAILED:
        execution.reconcile_step_failed(operation)
        ledger.reconcile_unknown(operation, FAILED, evidence=...)
    elif observed == NOT_FOUND:
        ledger.mark_manual_intervention(operation)
    else:
        ledger.keep_result_unknown_with_backoff(operation)
~~~

Java adapter 用资源 GET 和 expected postcondition 比较：

- draft create/update：GET draft 比 title/content/summary/status/version。
- schedule create/update：GET schedule 比 run_at/status/version。
- delete draft：404 或 deleted 视为成功。
- publish now：GET post 或 schedule 的 published_post_id。

### 9.5 关键 persistence 缺口

ExternalOperationRecord 有 objective_id，OperationLedger 也能接收 Objective 归属；但 execution/persistence.py 的 SQL Table、persistent_stores.py 的 column list、serializer/loader 都没有 objective_id。内存 profile 正常，PostgreSQL 重启后可能无法把 reconciliation 成功的资源重新绑定到原 Objective。

## 10. ToolRuntime 和 MCP

### 10.1 Tool 定义

Tool 不是通过 Java annotation 自动发现，而是 typed registry 注册。协议定义在：

- packages/contracts/greenbook_contracts/tool_contract.py
- packages/contracts/greenbook_contracts/tool_result.py
- services/greenbook_mcp/greenbook_mcp_server/tool_registry.py

~~~python
class ToolContract(BaseModel):
    name: str
    description: str
    category: str
    capability: str
    handler: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    operations: list[str] = Field(default_factory=list)
    policy: ToolPolicyMetadata
    semantic_action: str


class ToolResult[T](BaseModel):
    ok: bool
    code: str = "OK"
    message: str = ""
    user_message: str = ""
    retryable: bool = False
    request_sent: bool | None = False
    state: dict[str, Any] | None = None
    data: T | None = None
    provenance: list[DataProvenance] = Field(default_factory=list)
    trace_id: str | None = None
    receipt_id: str | None = None
    resource_refs: list[ResourceRef] = Field(default_factory=list)
    operation_receipt: OperationReceipt | None = None
~~~

### 10.2 Tool policy

| Tool | side effect | approval | max attempts |
|---|---:|---:|---:|
| content.create_draft | 是 | 否 | 2 |
| content.update_draft | 是 | 否 | 2 |
| content.delete_draft | 是 | 是 | 1 |
| community.delete_post | 是 | 是 | 1 |
| publication.schedule | 是 | 否 | 2 |
| publication.update_schedule | 是 | 否 | 2 |
| publication.cancel_schedule | 是 | 否 | 2 |
| publication.publish_now | 是 | 是 | 1 |
| interaction.send_reply | 是 | 是 | 1 |
| READ tools | 否 | 否 | 调用层控制 |

### 10.3 create_draft 典型实现

~~~python
async def create_draft(ctx: ToolContext, args: CreateDraftArguments):
    generated = await structured_draft_llm(
        title=args.title,
        instruction=args.instruction,
        references=args.references,
    )

    created = await java.create_draft(
        title=generated.title,
        content=generated.content,
        summary=generated.summary,
        idempotency_key=ctx.idempotency_key("CREATE_DRAFT", scope),
    )

    verified = await java.get_draft(created.draft_id)
    if not matches_expected_draft(verified, generated):
        return ToolResult.result_unknown(
            code="DRAFT_POSTCONDITION_UNVERIFIED",
            request_sent=True,
        )

    return ToolResult.success(
        data=verified,
        resource_refs=[ResourceRef(
            ref="draft",
            kind="DRAFT",
            resource_id=verified.draft_id,
            source="java",
            tool="content.create_draft",
        )],
        operation_receipt=OperationReceipt(
            operation_id=ctx.idempotency_key(...),
            semantic_action="CREATE_DRAFT",
            request_sent=True,
            downstream_accepted=True,
            side_effect_started=True,
            result_known=True,
            status="COMPLETED",
        ),
    )
~~~

其他 write Tool 通常遵循：读取当前版本 → 调 Java mutation → GET 验证 → 返回 ToolResult。READ Tool 直接调用 Java GET/search，并带 COMMUNITY_DATA provenance。

### 10.4 ToolRuntime 的职责和限制

ToolRuntime：

- 校验 Tool input。
- 使用 asyncio.wait_for 执行；默认 invocation timeout 约 60 秒，create draft policy 可配置为 120 秒。
- 记录 invocation start/result/evidence。
- 进程内缓存已完成 invocation。
- 把 ToolResult 转成 InvocationResult，再由 ActionLoop 转成 ActionObservation。

ToolRuntime 的 invocation cache 是进程内的，不是 durable dedupe。真正跨进程/重启的写幂等依赖 OperationLedger 和 Java IdempotencyService。当前 invocation fingerprint 主要依赖 tool_name、task_id、execution_id、step_id，没有把全部 arguments 放入 key；它要求 step identity 不可变。

## 11. 数据模型与数据库

### 11.1 Conversation、SessionContext、ContextSnapshot

当前没有名为 WorkingContext 的类。它的功能由三层共同完成：

1. SessionContext：身份、会话时区、active task/draft/post/schedule/execution、最近实体、最近 Tool、pending approval。
2. ContextSnapshot：从 durable repo 组装的有上限决策上下文。
3. ConversationContextSnapshot：ConversationService 的兼容快照。

SessionContext 主要字段：

~~~python
class SessionContext(BaseModel):
    conversation_id: str
    user_id: str = Field(frozen=True)
    tenant_id: str = Field(frozen=True)
    timezone: str = "Asia/Shanghai"
    active_task_id: str | None = None
    active_artifact_id: str | None = None
    active_draft_id: str | None = None
    active_post_id: str | None = None
    active_schedule_id: str | None = None
    active_execution_id: str | None = None
    recent_entities: list[RecentEntity] = Field(default_factory=list)
    recent_tool_calls: list[RecentToolCall] = Field(default_factory=list)
    pending_approval: PendingApproval | None = None
    conversation_summary: str | None = None
    last_successful_run_id: str | None = None
~~~

源码：packages/agent_core/greenbook_agent_core/context/__init__.py。

ContextBuilder 会加载 Conversation、bounded messages、Task/Objectives、Artifacts、Execution refs、resource index、preferences/memory，并限制候选数量。它不是事实源；事实仍归 Task、Execution、Artifact、Java 业务库。

### 11.2 ActionObservation、ResourceRef、StepOutcome

当前没有 ResourceFact 类，也没有 StepOutcome 类。对应概念是 ResourceRef、TaskResourceRef、ArtifactRef、ActionObservation、OperationReceipt、TaskExecutionRef。

~~~python
class ResourceRef(BaseModel):
    ref: str
    kind: str
    resource_id: str
    version: int | None = None
    title: str | None = None
    label: str | None = None
    source: str | None = None
    tool: str | None = None


class ActionObservation(BaseModel):
    iteration: int = 0
    action: str = ""
    tool_name: str = ""
    task_id: str = ""
    objective_id: str = ""
    query: str = ""
    input_fingerprint: str = ""
    outcome: str = "PENDING"
    ok: bool = False
    resource_id: str | None = None
    resource_kind: str | None = None
    resource_refs: list[ResourceRef] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    verified_facts: dict[str, Any] = Field(default_factory=dict)
    error_code: str = ""
    execution_id: str | None = None
    artifact_id: str | None = None
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str = ""

    @property
    def verified(self) -> bool:
        return bool(self.ok and self.outcome == "SUCCESS" and self.resource_id)
~~~

### 11.3 持久化关系

~~~text
assistant_conversations 1 ── * assistant_messages
assistant_conversations 1 ── * assistant_runs
assistant_conversations 1 ── * assistant_approvals
assistant_conversations 1 ── * assistant_tasks
assistant_tasks         1 ── * assistant_artifacts
assistant_tasks         1 ── * objectives(JSONB)
assistant_tasks         1 ── * execution_refs(JSONB)
assistant_tasks         1 ── * resource_index(JSONB)
execution               1 ── * execution_step
execution               1 ── * execution_event
execution_step          1 ── * external_operation
~~~

assistant_runs 是历史/界面投影，不是当前 runtime execution truth。Runtime-backed run 写入被限制为 run_id、conversation、user、tenant、content、trace 等字段；Execution status、checkpoint、retry state 应从 execution 表读取。

### 11.4 关键 DDL

Agent 的部分表由 create_all 和 additive migration 建立。以下为当前 schema 的核心等价定义。

~~~sql
CREATE TABLE assistant_conversations (
    conversation_id UUID PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    title TEXT,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Shanghai',
    active_task_id VARCHAR(128),
    active_artifact_id VARCHAR(128),
    active_draft_id VARCHAR(128),
    active_post_id VARCHAR(128),
    active_schedule_id VARCHAR(128),
    recent_entities JSONB NOT NULL DEFAULT '[]',
    recent_tool_calls JSONB NOT NULL DEFAULT '[]',
    pending_approval JSONB,
    last_successful_run_id VARCHAR(128),
    conversation_summary TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE assistant_tasks (
    task_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    goal_category VARCHAR(128) NOT NULL DEFAULT '',
    goal_summary TEXT,
    status VARCHAR(32) NOT NULL,
    phase VARCHAR(64),
    priority INTEGER NOT NULL DEFAULT 0,
    task_type VARCHAR(64) NOT NULL DEFAULT 'GOAL_DRIVEN',
    execution_mode VARCHAR(64) NOT NULL DEFAULT 'AUTO',
    requires_confirmation BOOLEAN NOT NULL DEFAULT FALSE,
    confirmation_state VARCHAR(32) NOT NULL DEFAULT 'RESOLVED',
    confirmation_version INTEGER NOT NULL DEFAULT 0,
    confirmed_version INTEGER,
    confirmation_snapshot_hash VARCHAR(128),
    confirmation_resume_run_id VARCHAR(128),
    root_goal_id VARCHAR(128),
    goal_tree_version INTEGER NOT NULL DEFAULT 0,
    goal_tree_snapshot JSONB NOT NULL DEFAULT '{}',
    plan_version INTEGER NOT NULL DEFAULT 0,
    plan_history JSONB NOT NULL DEFAULT '[]',
    active_execution_id VARCHAR(128),
    artifacts JSONB NOT NULL DEFAULT '[]',
    depends_on JSONB NOT NULL DEFAULT '[]',
    goals JSONB NOT NULL DEFAULT '[]',
    objectives JSONB NOT NULL DEFAULT '[]',
    revisions JSONB NOT NULL DEFAULT '[]',
    execution_refs JSONB NOT NULL DEFAULT '[]',
    resource_index JSONB NOT NULL DEFAULT '[]',
    last_action TEXT,
    action_history JSONB NOT NULL DEFAULT '[]',
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);
~~~

当前没有独立 objectives 表；Objective 是 assistant_tasks.objectives JSONB 元素。

~~~sql
CREATE TABLE execution (
    execution_id VARCHAR(128) PRIMARY KEY,
    plan_id VARCHAR(128) NOT NULL,
    task_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    current_step_index INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    completed_at VARCHAR(64) NOT NULL DEFAULT '',
    requires_approval BOOLEAN NOT NULL DEFAULT FALSE,
    has_side_effects BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE execution_step (
    step_execution_id VARCHAR(128) PRIMARY KEY,
    step_id VARCHAR(128) NOT NULL,
    execution_id VARCHAR(128) NOT NULL,
    capability VARCHAR(256) NOT NULL,
    ordinal INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    error_code VARCHAR(128) NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    checkpoint_data JSON NOT NULL,
    input_artifact_types JSON NOT NULL,
    output_artifact_type VARCHAR(128) NOT NULL DEFAULT '',
    depends_on JSON NOT NULL,
    output_artifact JSON
);

CREATE TABLE external_operation (
    operation_id VARCHAR(128) PRIMARY KEY,
    execution_id VARCHAR(128) NOT NULL,
    step_id VARCHAR(128) NOT NULL,
    tool_name VARCHAR(256) NOT NULL DEFAULT '',
    status VARCHAR(32) NOT NULL,
    external_operation_id VARCHAR(256),
    receipt_id VARCHAR(256),
    idempotency_key VARCHAR(256),
    runtime_idempotency_key VARCHAR(256),
    external_idempotency_key VARCHAR(256),
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    evidence JSON,
    trace_id VARCHAR(128),
    conversation_id VARCHAR(128),
    semantic_action VARCHAR(128),
    resource_type VARCHAR(64),
    resource_id VARCHAR(256),
    expected_postcondition JSON,
    attempt INTEGER DEFAULT 0,
    claim_owner VARCHAR(256),
    claim_version INTEGER DEFAULT 0,
    fencing_token VARCHAR(256),
    lease_expires_at VARCHAR(64),
    side_effect_started BOOLEAN DEFAULT FALSE,
    reconciliation_needed BOOLEAN DEFAULT FALSE,
    retry_classification VARCHAR(32),
    reconcile_attempts INTEGER DEFAULT 0,
    verified_status VARCHAR(32),
    verified_reason VARCHAR(512),
    next_reconcile_at VARCHAR(64)
);
-- 当前实际 DDL 缺少 objective_id；这是 schema gap。
~~~

Java 业务表核心字段：

~~~sql
CREATE TABLE know_posts (
    id BIGINT PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    description VARCHAR(50),
    content_object_key VARCHAR(512),
    content_etag VARCHAR(128),
    content_size BIGINT,
    content_sha256 CHAR(64),
    creator_id BIGINT NOT NULL,
    status VARCHAR(32) NOT NULL,
    content_origin VARCHAR(32),
    create_time DATETIME,
    update_time DATETIME,
    publish_time DATETIME
);

CREATE TABLE scheduled_publications (
    schedule_id BIGINT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    draft_id BIGINT NOT NULL,
    run_at DATETIME NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    version BIGINT NOT NULL DEFAULT 1,
    idempotency_key VARCHAR(128) NOT NULL,
    published_post_id BIGINT,
    failure_code VARCHAR(128),
    failure_message TEXT,
    created_at DATETIME,
    updated_at DATETIME
);

CREATE TABLE agent_idempotency_record (
    id BIGINT UNSIGNED PRIMARY KEY,
    user_id BIGINT UNSIGNED NOT NULL,
    operation VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    request_hash CHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'IN_PROGRESS',
    response_status INT,
    response_body JSON,
    resource_type VARCHAR(32),
    resource_id VARCHAR(64),
    created_at TIMESTAMP(3),
    completed_at TIMESTAMP(3),
    expires_at TIMESTAMP(3),
    UNIQUE KEY uq_agent_idempotency (user_id, operation, idempotency_key)
);
~~~

Java migration：apps/backend/src/main/resources/db/migration/V1__baseline.sql、V2__agent_idempotency_record.sql、V3__scheduled_publications.sql。

## 12. 典型场景追踪

### 12.1 “写一篇 Java 教程”

1. 前端调用 POST /api/v1/agent/conversations/{id}/messages。
2. routes 认证用户，写 assistant_messages，创建 assistant_runs。
3. Runner 抢占 Run，TurnCoordinator 组装 bounded context。
4. Interpreter 产生 CREATE + topic=Java + publication_intent=DRAFT_ONLY + 无 target + 无 temporal。
5. TargetResolver 不需要运行；TemporalResolver 返回 NONE。
6. 单个 draft-only write 通常自动准入，不触发 semantic confirmation。
7. CREATE_DRAFT 不是普通 Fast READ，进入 ActionLoop/复杂执行路径。
8. ActionLoop 选择 content.create_draft。
9. MCP content.create_draft：
   - Host LLM 生成 title/content/summary。
   - Java POST /api/v1/agent/drafts。
   - AgentFacadeService.createDraft。
   - KnowPostService 创建 know_posts draft，并把正文写入 OSS。
   - Java GET draft 验证。
10. 返回 ToolResult.success、DRAFT ResourceRef、OperationReceipt。
11. ObjectiveReducer 看到真实 draft resource 后完成 Objective。
12. SessionContext 更新 active_draft_id 和 recent entity；Run/Message/SSE 返回草稿 ID 和结果。

如果正文生成、Java 调用或后置条件验证失败，应该返回失败或 WAITING_EXTERNAL，不能把 LLM 生成文本直接当成已创建草稿。

### 12.2 跨轮次引用

Turn 1 成功后会留下：

~~~text
assistant_conversations.active_task_id
assistant_conversations.active_draft_id
assistant_conversations.recent_entities += DRAFT(draft_123)
assistant_tasks.objectives[0].related_resource_ids += draft_123
assistant_tasks.resource_index += {resource_id: draft_123, kind: DRAFT, objective_id: obj_1}
assistant_messages / assistant_runs / execution records
~~~

Turn 2 “那篇明天下午三点发”：

1. 读取同一 conversation 的 SessionContext 和 Task resource index。
2. Interpreter 提取 ACTIVE/label/ordinal 证据和 temporal_text。
3. TargetResolver 将“那篇”解析到 draft_123；不调用 Java 搜索。
4. TemporalResolver 生成 UTC run_at。
5. Delta owner resolver 确认目标属于同一 Task/Objective。
6. ActionLoop 执行 publication.schedule。
7. Java 创建 scheduled_publications row。
8. Agent 保存 schedule resource 和 Objective 归属。

如果 Turn 1 只是直接 READ 搜索，搜索结果没有被 ActionLoop 写入 Artifact/TaskResourceRef，那么 Turn 2 的“那篇”不保证能够解析。

### 12.3 “Java 那篇明天下午三点发”

- 如果 Context 有唯一 Java draft/resource，TargetResolver 返回 RESOLVED。
- 如果有两个 Java 候选，返回 AMBIGUOUS，并让用户选择。
- 如果没有候选，不能凭 label 直接发布，应澄清或要求先创建草稿。
- TemporalResolver 生成 canonical UTC run_at。
- CREATE_SCHEDULE 进入 durable write。
- Java ScheduledPublicationService 使用 Spring @Scheduled，默认 fixed delay 约 30 秒；当前不是 Quartz，也不是 Agent Worker 在时间点主动唤醒 Java。
- Agent retry 负责排期创建失败；到点发布失败主要由 Java schedule 状态、PROCESSING recovery 和 FAILED 状态负责。

### 12.4 CREATE_DRAFT 网络超时，RESULT_UNKNOWN

~~~text
Agent Worker
  → ToolRuntime / content.create_draft
  → Java POST 可能已发出
  → HTTP timeout
~~~

1. ToolResult 设置 request_sent=None 或 side_effect_started=True。
2. OperationLedger 标记 RESULT_UNKNOWN 和 reconciliation_needed。
3. ActionLoop 记录 ActionObservation(outcome=RESULT_UNKNOWN)，Task/Run 进入 WAITING_EXTERNAL，停止重复 CREATE。
4. ReconciliationWorker GET draft，并比较 expected postcondition。
5. 找到匹配 draft：完成 Step、Objective、Ledger。
6. 明确证实未创建且 operation identity 可靠：才可 SAFE_RETRY。
7. 找不到但无法证明请求未到达：保持 UNKNOWN 或人工介入。
8. 4 次短 reconciliation 后约每小时查询，当前不会自动转换为最终 FAILED。

## 13. 配置和部署

关键配置文件：

- .env.example
- docs/development/CONFIGURATION.md
- pyproject.toml
- docker-compose.yml
- apps/backend/src/main/resources/application.yml

~~~dotenv
GREENBOOK_JAVA_BASE_URL=http://127.0.0.1:8080
GREENBOOK_AGENT_API_PORT=8094

GREENBOOK_AGENT_RUNTIME_STORAGE=postgres
GREENBOOK_AGENT_DATABASE_URL=postgresql+asyncpg://...
GREENBOOK_AGENT_RUNTIME_DATABASE_URL=postgresql+asyncpg://...
GREENBOOK_AGENT_EXECUTION_DISPATCH=queue
GREENBOOK_AGENT_IN_PROCESS_WORKER=true
GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER=true

GREENBOOK_AGENT_MAX_CONCURRENT_RUNS=4
GREENBOOK_AGENT_MAX_CONCURRENT_PER_CONVERSATION=2
GREENBOOK_AGENT_WORKER_CONCURRENCY=4

DEEPSEEK_API_KEY=...
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=...

GREENBOOK_AGENT_IDENTITY_ISSUER=...
GREENBOOK_AGENT_IDENTITY_AUDIENCE=...
GREENBOOK_AGENT_IDENTITY_JWKS_URL=...
~~~

还包括 Java HTTP timeout、queue lease、heartbeat、retry attempts、run timeout、最大模型调用数、最大 Tool 调用数、最大 replan 数、publication lead time、Qdrant/Redis/MySQL/Kafka 连接参数。

当前配置风险是多份默认值并存：.env.example、ActionLoop 构造默认值、旧架构文档和历史报告不完全一致。应建立单一 settings source，并在启动时打印最终生效配置摘要。

## 14. 测试、Benchmark 和质量指标

### 14.1 当前测试

TESTING.md 提供：

~~~text
uv run pytest -q
uv run pytest --collect-only -q
uv run ruff check .
uv run python -m compileall packages apps services
docker compose config
~~~

当前工作树执行 pytest collect-only 收集到约 1521 个测试；针对语义、confirmation、RESULT_UNKNOWN、ActionLoop approval 的 targeted tests 为 29 passed。本次审计没有运行完整测试套件。

Java 使用 Maven test；前端执行 lint/build；真实 E2E 需要运行 Java、Agent、数据库和有效模型账号。

### 14.2 评测集

当前仓库有多组评测：

1. evaluation/semantic_longtail：60 primary cases，paraphrase 展开后 78 utterances。
2. packages/evaluation/.../semantic_baseline.py：16 类 × 5 条，共 80 条的另一套语义 fixture。
3. GOLDEN_CASES/BASELINE_CASES：小型快速回归集。

因此“60 primary”存在，但与 80-case baseline 不是同一数据集。

### 14.3 Long-tail 结果

产物：

- evaluation/semantic_longtail/cases.json
- docs/archive/evaluations/artifacts/semantic_longtail_20260822/dataset_manifest.json
- docs/archive/evaluations/artifacts/semantic_longtail_20260822/report.json
- docs/archive/evaluations/artifacts/semantic_longtail_20260822/results.json
- evaluation/semantic_longtail/run_benchmark.py

Benchmark 只调用生产语义解析器、TargetResolver、TemporalResolver，不创建真实 Task、不调用 Java、不修改生产状态。

| 指标 | 78 utterances report | 60 primary 重算 |
|---|---:|---:|
| exact semantic accuracy | 8/78 = 10.26% | 5/60 = 8.33% |
| unsafe semantic error rate | 56/78 = 71.79% | 41/60 = 68.33% |
| model calls | 109 | — |
| provider errors | 0 | — |
| 平均 model call 延迟 | 约 2.13 秒 | — |

主要错误：

- missing goal：60
- wrong target：46
- wrong publication：29
- wrong time：28
- wrong goal split：22
- constraint lost/violation：16/16
- target ambiguity missed：2
- missing clarification：4

### 14.4 历史运行时指标

docs/reports/assistant-runtime-baseline.json 是 2026-07-30 的 30-run observed report，不是当前版本 benchmark：

- 30 runs；20 completed、8 failed、2 waiting approval。
- E2E 平均约 42.8 秒。
- p50 约 12.3 秒。
- p95 约 105.4 秒。
- 平均 model duration 约 14.1 秒。
- 平均 tool duration 约 0.82 秒。
- 4 个 retried runs 全部最终 completed。
- 没有 p99。
- 样本太小，不能推导真实吞吐和线上可靠性。

当前配置并发通常是 Agent run 4、单会话 2、Worker 4，但没有实测并发吞吐、队列等待分位数或数据库池饱和曲线。

## 15. 当前问题审计

### 15.1 直接回答关键问题

#### 是否有 WorkingContext / SessionContext？

有 SessionContext，没有名为 WorkingContext 的类。SessionContext 保存 active bindings 和最近实体；ContextSnapshot/ContextAssembler 是 bounded working projection。它不是事实源。

#### READ 操作是否持久化？

ActionLoop 内 READ 会通过 ActionObservation、Artifact、TaskResourceRef/resource binding 持久化足够证据；简单 Fast Path READ 通常只返回 RuntimeResult/run/message projection，不进入 OperationLedger，也不总是绑定 Task/resource index。因此上一个 Turn 的搜索结果不能稳定支持下一个 Turn 的自然语言引用。

#### 一个复杂任务是一个 Objective 还是多个？

一个 Task 可以包含多个 Objective。每个独立最终交付物一个 Objective；一个交付物的生成、排期等能力属于同一 Objective 的 required capabilities。没有 SubObjective 类。

#### RESULT_UNKNOWN 最多重试几次？

它不走普通 retry。Reconciliation 短周期最多 4 次：10/30/90/300 秒；之后约每小时继续保持 UNKNOWN。ToolRuntime 默认 invocation timeout 约 60 秒，create draft policy 可是 120 秒；Java HTTP timeout 由环境变量配置。

#### Freeze 在 Confirmation 前还是之后？

没有生产 FROZEN 状态。实际等价物是在 confirmation pending 时保存 canonical snapshot/hash/version；Confirm 后用 typed marker resume，不再重新解释文本。用户仍能 Cancel；Modify 会 supersede 并重新编译。

### 15.2 高风险问题

| 优先级 | 问题 | 影响 |
|---|---|---|
| P0 | 60-case 语义 exact 极低、unsafe 极高 | 目标/时间错误可能进入安全执行前 |
| P0 | external_operation 缺少 objective_id 持久化 | 多 Objective 重启后无法稳定归属 |
| P0 | 多目标/异步完成收敛风险 | 可能提前 terminal 或运行时间过长 |
| P1 | Fast Path READ 无统一资源投影 | 跨轮次引用不稳定 |
| P1 | 三套 HITL 无统一 expiry | approval 可长期悬挂 |
| P1 | GoalTree 与 Objective 并存 | 维护者容易走错活动路径 |
| P1 | 长 approval 后 Java delegated authorization 可能过期 | 批准后 publish 仍失败 |
| P1 | ToolRuntime key 不包含完整 arguments | step 重用时有 replay/cache 风险 |
| P2 | ActionLoop 与配置预算不统一 | 线上行为与文档不一致 |
| P2 | 硬编码 D:\agent\green-book\.tmp_*.log | 污染工作树，不适合生产 |
| P2 | 只有 p50/p95，无 p99 | 无法回答尾延迟/容量 |
| P2 | 没有 clarification/cancel/RESULT_UNKNOWN 聚合报表 | 无法量化 UX 和可靠性改进 |

已知问题审计：docs/audit/KNOWN_ISSUES.md；真实集成限制：docs/integration/REAL_INTEGRATION_REPORT.md。

## 16. 改进建议、成本与收益

### A. 先修语义安全边界

方案：把 long-tail 60 primary 分成 schema、target、temporal、multi-objective、clarification 五个独立 gate；任何 gate 未通过不得进入 write path。每个 case 保存 expected Command/ResolvedSemanticState。

成本：中高。

收益：极高，直接降低 wrong target、wrong time、unsafe error。

### B. 修复 Objective ownership 持久化

方案：external_operation 增加 objective_id，更新 SQLAlchemy Table、column list、serializer、loader，补充重启后 reconciliation → ResourceBinding → Objective completion 集成测试。

成本：低到中。

收益：高，直接修复多目标 durable correctness。

### C. 统一 READ 结果投影

方案：所有可能被后续引用的 READ 生成统一 ResourceFact；可以先复用 ResourceRef + verified_facts，不必立即新建类。结果写入 assistant_artifacts 和 Task/Conversation resource index，并带 TTL。

成本：中。

收益：高，改善跨轮次引用、审计和结果复用。

### D. 合并 HITL 状态机

方案：统一 durable HumanInteraction，字段包含 interaction_type、status、expires_at、resume_token、snapshot_hash、version、superseded_by；Clarification、semantic confirmation、approval 只是类型/策略差异。

成本：中高。

收益：高，消除三套等待/恢复逻辑。

### E. 多目标改为编译计划

方案：Interpreter 输出多个 canonical item 后先生成 typed ObjectivePlan；ActionLoop 接受 dependency graph 和 per-objective bindings，READ 可批量，WRITE 仍逐 operation durable。

成本：高。

收益：高，改善“发三篇”延迟、上下文污染、重复模型调用和 no-progress。

### F. 增强 RESULT_UNKNOWN 运维终态

方案：保持不盲目 retry，但增加 MANUAL_INTERVENTION_REQUIRED/EXPIRED_UNKNOWN、操作员 API、告警、dead-letter 统计和用户可见提示；记录发生率、恢复时间、最终状态。

成本：中。

收益：高，避免 UNKNOWN 长期静默悬挂。

### G. 建立真实指标

记录 queue wait、semantic latency、tool latency、Java latency、p50/p95/p99、clarification rate、confirmation rate、approval expiry、cancel rate、duplicate delivery、RESULT_UNKNOWN rate、reconciliation recovery rate、per-objective completion latency。

成本：中。

收益：中高，可以回答平均响应、P99、并发和可靠性。

### H. 清理活动面和配置

- CURRENT_ARCHITECTURE.md 作为唯一活动架构；旧文档标记 legacy。
- 删除硬编码临时日志，改用结构化 observability。
- 统一 settings model。
- 建立 clean checkout CI，禁止 debug artifacts 进入仓库。

成本：低到中。

收益：中，减少维护和发布误判。

## 17. 最终判断

项目已经具备较完整的 Agent Runtime 骨架：语义和执行分离、Task/Objective 有事实边界、写操作有 durable queue/lease/ledger/fencing、Java 业务层做最终事实和后置条件验证。这些设计方向是正确的。

但当前不能据此判断“已经可靠支持自然语言知识社区写操作”。最大阻塞不是缺少更多 Tool，而是语义 grounding 和状态投影：benchmark 表明目标、时间、拆分和澄清仍有大量错误；Objective ownership 的数据库缺口又削弱了 durable runtime 的核心保证。

建议交付顺序：

~~~text
语义安全 gate
  → Objective ownership 持久化修复
  → READ/跨轮次结果投影
  → HITL 统一与过期
  → 多目标编译计划
  → 性能/可靠性指标和容量压测
~~~

在这些 gate 完成前，不建议仅通过增加模型调用、扩大 ActionLoop 预算或新增 Tool 来扩展生产写操作范围。
