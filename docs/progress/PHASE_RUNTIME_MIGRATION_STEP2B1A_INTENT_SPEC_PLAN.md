# GreenBook Runtime Migration Step2-B-1-a IntentSpec 正式产出边界设计

- 目标：在不改变 Planner、Worker、ToolRuntime、ExecutionStateManager、PlanExecution 或旧路由的前提下，为 ConversationRuntimeAdapter 定义唯一、可验证的 IntentSpec 入口。
- 范围：只设计语义产出边界；本阶段不修改代码。
- 当前基线：feature/runtime-http-migration，HEAD 3ff68db。
- 关联审计：PHASE_RUNTIME_MIGRATION_STEP2B1_PREREQUISITE_AUDIT.md。

## 一、设计结论

推荐新增一个位于 assistant_core task 领域内的窄接口 IntentSpecProvider。

它的职责只有：

~~~text
用户文本 + 可选任务候选
    -> 形式化 IntentSpec
    -> Pydantic 结构校验
    -> IntentValidator 语义校验
    -> 返回已验证 IntentSpec
~~~

它不负责：

- 创建或查询 Task
- 生成 TaskContext
- 生成 TaskPlan
- 选择工具
- 执行 Runtime
- 生成用户回复

Assistant API 只负责注入该 Provider，并将已验证的 IntentSpec 交给 to_task_intent 和现有 IntentCompiler。这样既保留现有 TaskUnderstanding 的兼容返回类型，又让 Runtime 消息适配层拥有稳定的正式语义契约。

## 二、当前 L1/L2 结构事实

### 1. 当前入口

文件：

packages/assistant_core/greenbook_assistant_core/task/understanding.py

类和方法：

- TaskUnderstanding
- TaskUnderstanding.understand

当前公开返回类型：

~~~text
understand(user_message, existing_tasks=None) -> TaskIntent
~~~

因此目前的理解器不是一个以 IntentSpec 为正式返回值的 Provider。

### 2. 当前 L1

TaskUnderstanding._quick_intent 使用确定性规则识别：

- CREATE
- REVISE
- SCHEDULE
- SEARCH
- QUERY
- ANALYZE
- 条件和历史引用信号

L1 直接返回 TaskIntent，并写入：

- relation
- goal
- goal_category
- requirements
- constraints
- resource_requests
- confidence
- source=L1

L1 不写入 intent_spec。

### 3. 当前 Direct L2

复杂请求由 _needs_l2 和 _needs_l2_v2 路由到：

~~~text
_try_l2_v2
  -> _llm_understand_direct_v2
  -> _parse_intent_spec
  -> IntentSpec.model_validate
  -> IntentValidator.validate
  -> 可选的 targeted repair
  -> 再次 IntentValidator.validate
  -> to_task_intent
~~~

TaskUnderstanding 在 Direct L2 成功时会把 IntentSpec 的 JSON 快照写入 TaskIntent.intent_spec。

### 4. 当前原始 L2 回退

原始 _llm_understand 和 _parse_llm_output 直接返回 TaskIntent。它不保证附带正式 IntentSpec。

因此：

- L2 被触发，不代表一定得到正式 IntentSpec。
- Direct L2 成功并通过验证，才是当前可靠的正式 Spec 来源。
- 兼容 L2 回退不应成为 Runtime 的新入口。

## 三、为什么需要 IntentSpecProvider

### 方案比较

|方案|问题|结论|
|---|---|---|
|直接把 TaskUnderstanding.understand 改成返回 IntentSpec|会破坏现有 TaskIntent 调用者、兼容测试和旧路由|不采用|
|在 assistant_api 里自己解析 TaskIntent 并拼 IntentSpec|语义规则进入 HTTP 层，容易复制 L1/L2 和重新形成 Legacy 分支|不采用|
|直接从 Adapter 调用 _llm_understand_direct_v2 等私有方法|绕过公开契约，后续难以维护和测试|不采用|
|新增 assistant_core/task/IntentSpecProvider|保留旧 API，同时建立唯一正式输出边界|采用|

IntentSpecProvider 不是新的 Agent，也不是新的状态模型。它只是把现有理解能力封装成一个“必须返回已验证 IntentSpec”的领域服务。

## 四、Provider 的推荐位置和边界

### 推荐位置

建议文件：

packages/assistant_core/greenbook_assistant_core/task/intent_spec_provider.py

理由：

1. IntentSpec、IntentValidator、ActionType、ResourceType 都属于 assistant_core。
2. Provider 需要读取原始用户文本并产出领域语义，不应依赖 FastAPI Request、数据库 session 或 Runtime 服务。
3. 统一放在 task 领域可以防止 assistant_api 重新实现关键词、动作和资源映射。
4. assistant_api 只负责依赖注入、Task 绑定和调用 IntentCompiler。

### 建议接口

正式接口应保持窄且可测试：

~~~text
class IntentSpecProvider:
    async def resolve(
        self,
        user_message: str,
        *,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> IntentSpec:
        ...
~~~

契约：

- 成功：只返回已经通过 IntentSpec.model_validate 和 IntentValidator.validate 的 IntentSpec。
- 失败：返回稳定的语义错误，例如 INTENT_SPEC_INVALID、INTENT_SPEC_UNAVAILABLE 或 INTENT_UNSUPPORTED。
- 不能返回未验证的 dict。
- 不能把 TaskIntent 当成正式成功结果。
- 不产生 TaskPlan 或执行参数。

为避免 L1/L2 重复理解，TaskUnderstanding 后续可以增加一个共享的正式结果内部方法，例如：

~~~text
understand_formal(...) -> IntentSpec 或明确的理解失败
~~~

现有 understand(...) -> TaskIntent 继续保留，作为兼容包装。Provider 不应读取或依赖私有方法名。

## 五、简单 L1 请求如何获得正式 IntentSpec

### 1. L1 投影原则

对于明确、单一、无歧义的 L1 结果，Provider 使用 assistant_core/task 内的新显式投影表，将 TaskIntent 转为 IntentSpec。

这不是 compatibility/intent 下的历史 IntentSpecBuilder，也不是逆向恢复旧转换链。投影表应是正式语义边界的一部分，使用已有的领域枚举，不使用 HTTP 层关键词。

### 2. 最小映射

对本阶段必须支持的 CREATE_CONTENT：

~~~text
TaskIntent:
  relation=NEW_TASK
  goal_category=CREATE_CONTENT
  requirement=CREATE
  resource_request=CONTENT_DRAFT
      |
      v
IntentSpec:
  mode=SIMPLE
  action=CREATE
  resource=CONTENT
~~~

IntentSpec 中的 CONTENT 表示用户要生成的文章/帖子正文。下游 to_task_intent 会将它映射为 CONTENT_DRAFT 资源请求；草稿是产物/执行层语义，不应把“生成草稿”误当成用户意图层的另一个动作。

建议生成：

~~~json
{
  "mode": "SIMPLE",
  "goal": "帮我写一篇AI Agent学习路线帖子",
  "actions": [
    {
      "action": "CREATE",
      "resource": "CONTENT",
      "confidence": 0.85
    }
  ],
  "conditions": [],
  "constraints": [],
  "target_hint": null,
  "confidence": 0.85,
  "source": "L1"
}
~~~

### 3. 投影校验

投影必须执行以下检查：

1. goal 非空。
2. 至少有一个动作。
3. relation、goal_category、requirements 和 resource_requests 之间有明确映射。
4. constraints 中的 type/value 可以转换为 ConstraintType。
5. target_hint 原样保留。
6. 不把执行步骤、工具名、step_id 或 depends_on 写入 IntentSpec。
7. 构造后先执行 IntentSpec.model_validate。
8. 再使用 IntentValidator.validate(spec, original_text)。

对简单 CREATE_CONTENT 文本，Validator 应确认：

- mode=SIMPLE 与单一 CREATE 动作一致。
- 没有缺失条件。
- 没有错误的发布时间或审批约束。
- actions 非空。

如果投影结果无法安全表达某个 L1 类别，Provider 应返回 INTENT_UNSUPPORTED，而不是猜测或降级为未结构化 TaskIntent。CREATE_CONTENT 是本阶段的最小验收类别，其他类别应通过同样的显式表和单元测试逐步加入。

## 六、L2 的正式处理规则

### Direct L2 成功

Direct L2 返回 IntentSpec 后：

1. 通过 Pydantic schema 校验。
2. 调用 IntentValidator.validate。
3. 对可修复的确定性问题复用现有 _llm_repair_spec。
4. 修复结果再次通过 Pydantic 和 IntentValidator。
5. 返回最终 Spec。

### Direct L2 失败或原始 L2 回退

对于需要 L2 的复杂文本，如果 Direct L2 失败而原始 L2 只返回 TaskIntent：

- 不能把这个 TaskIntent 当作正式 Runtime 成功。
- 不能调用 compatibility/intent/intent_elements.py 的 IntentSpecBuilder。
- 不能调用 compatibility/intent/intent_draft.py 的旧 IntentCompiler。
- 应返回 INTENT_SPEC_UNAVAILABLE，或在明确策略下重新执行正式 Direct L2。
- 只有被确认是清晰 L1 的请求，才允许走 L1 显式投影。

这样可以防止“新 Runtime 名义上接入、实际重新进入历史 IntentDraft/IntentElements 链路”。

## 七、IntentValidator 的复用方式

IntentValidator 位于：

packages/assistant_core/greenbook_assistant_core/task/intent_validator.py

统一规则：

~~~text
任何进入 IntentCompiler 或 Planner 的 IntentSpec
    必须先经过 IntentSpec.model_validate
    再经过 IntentValidator.validate
~~~

Validator 继续负责：

- SIMPLE/COMPOSITE/CONDITIONAL 一致性
- actions 非空
- 条件与 UPDATE_OR_CREATE 的一致性
- 发布时间文本与 TIME 约束
- 审批文本与 PUBLISH/APPROVAL 约束
- 结构化 issue 和可修复建议

Provider 不复制这些规则。Direct L2 的 targeted repair 继续留在现有 TaskUnderstanding/正式理解层；L1 投影如果触发 Validator 错误，应视为映射缺陷并失败，不应让 LLM 自由修改一个确定性投影。

## 八、避免兼容链重新成为入口

必须保持以下边界：

~~~text
正式 Runtime：
User Message
  -> IntentSpecProvider
  -> IntentSpec
  -> to_task_intent（单向兼容投影）
  -> IntentCompiler
  -> TaskContext
  -> RuntimeAgentService

历史兼容：
User Message
  -> IntentDraft / IntentElements
  -> compatibility adapter
  -> 仅兼容测试或旧调用者
~~~

约束：

1. 新 Adapter 只能依赖 task.intent_models、task.intent_validator、task.intent_compat 的单向 Spec -> TaskIntent。
2. 不从 assistant_api 导入 compatibility/intent 下的 IntentSpecBuilder。
3. 不在 compatibility 模块增加新字段或新规则。
4. 不让 Planner 反向理解自然语言。
5. 不让 RuntimeAgentService 猜测缺失的 IntentSpec。
6. TaskIntent.intent_spec 只能作为已验证 Spec 的快照；不能以任意 dict 作为新的来源。

## 九、目标输入的完整设计链路

输入：

~~~text
帮我写一篇AI Agent学习路线帖子
~~~

### 1. 消息进入 Provider

ConversationRuntimeAdapter 接收原始 message 和必要的 existing_tasks 摘要，调用：

~~~text
IntentSpecProvider.resolve(message, existing_tasks=...)
~~~

Provider 调用现有 TaskUnderstanding 的 L1 识别，得到：

~~~text
relation=NEW_TASK
goal_category=CREATE_CONTENT
requirements=[CREATE]
resource_requests=[CREATE, CONTENT_DRAFT]
source=L1
confidence=0.85
~~~

### 2. L1 投影和验证

Provider 生成：

~~~text
IntentSpec(
  mode=SIMPLE,
  goal="帮我写一篇AI Agent学习路线帖子",
  actions=[IntentAction(CREATE, CONTENT)],
  conditions=[],
  constraints=[],
  target_hint=None,
  confidence=0.85,
  source="L1",
)
~~~

然后：

~~~text
IntentSpec.model_validate
  -> IntentValidator.validate
  -> valid
~~~

### 3. Spec 到 TaskIntent

Adapter 调用：

~~~text
task_intent = to_task_intent(intent_spec)
~~~

预期得到：

~~~text
relation=NEW_TASK
goal_category=CREATE_CONTENT
requirements=[{"type": "CREATE"}]
resource_requests=[
  {"operation": "CREATE", "resource_type": "CONTENT_DRAFT"}
]
~~~

当前 to_task_intent 是单向转换函数，本身不会自动填充 intent_spec 快照。Adapter 应把 Spec 作为独立参数传给现有 IntentCompiler；IntentCompiler 当前在编译边界会设置已验证 Spec 快照。不要手工写入未经验证的 dict。

### 4. 进入 IntentCompiler

在 Task Provider 已创建 NEW_TASK 的 Task 后，Adapter 调用：

~~~text
IntentCompiler.compile(
    intent_spec=intent_spec,
    task_intent=task_intent,
    task=new_task,
    conversation=conversation,
)
    -> TaskContext
~~~

IntentCompiler 负责：

- 校验 Task 存在
- 校验 task_id 一致
- 校验目标和 ArtifactRef
- 复制 constraints
- 生成 TaskContext

它不重新理解原始文本，也不需要知道 L1/L2 来源。

### 5. 后续 Runtime

最终边界为：

~~~text
message
  -> IntentSpecProvider
  -> validated IntentSpec
  -> to_task_intent
  -> IntentCompiler
  -> TaskContext
  -> RuntimeContext
  -> RuntimeAgentService.execute
  -> Planner / TaskPlan / PlanExecution
~~~

Provider 到 IntentCompiler 之间没有执行计划生成；Planner 仍是唯一计划生成者。

## 十、建议的最小实现文件范围

本计划只设计，不修改代码。后续实施时建议限制在：

|文件/目录|用途|
|---|---|
|packages/assistant_core/greenbook_assistant_core/task/intent_spec_provider.py|正式 Spec Provider 和显式 L1 投影|
|packages/assistant_core/greenbook_assistant_core/task/understanding.py|必要时抽取共享正式理解结果；保留旧 understand 返回类型|
|packages/assistant_core/greenbook_assistant_core/task/intent_models.py|仅在接口需要时补充结果类型，不改变现有字段语义|
|apps/assistant_api/greenbook_assistant_api|仅注入 Provider，并由未来 Adapter 调用|
|tests/unit/test_intent_spec_provider.py|L1、Direct L2、Validator、失败降级边界|

不应修改：

- Planner/TaskOrchestrator
- PlanExecution
- ExecutionWorker
- ToolRuntime/CapabilityExecutor
- ExecutionStateManager
- ExecutionEventStore
- compatibility/intent 的历史实现
- 数据库 schema

## 十一、验收标准

在实现阶段至少验证：

1. 对“帮我写一篇AI Agent学习路线帖子”，Provider 返回非空 IntentSpec。
2. 返回 Spec 的 source 为 L1，actions 含 CREATE/CONTENT。
3. IntentValidator.validate 返回 is_valid=true。
4. 原始 goal、confidence 和 target_hint 保留。
5. to_task_intent 后 goal_category 为 CREATE_CONTENT，resource_request 为 CONTENT_DRAFT。
6. IntentCompiler.compile 可以使用该 Spec 和 NEW_TASK Task 生成 TaskContext。
7. Direct L2 成功时仍保留现有 repair/validation 行为。
8. Direct L2 失败时不会静默进入 compatibility/intent。
9. Provider 不生成 PlanExecution、工具参数或执行状态。
10. 旧 TaskUnderstanding 调用者仍可继续得到 TaskIntent。

## 十二、最终判断

IntentSpecProvider 应放在 assistant_core，而不是 assistant_api。它是正式语义边界，不是兼容层。

推荐的最小改动方向是：

~~~text
保留：
TaskUnderstanding.understand -> TaskIntent

新增正式边界：
IntentSpecProvider.resolve -> 已验证 IntentSpec

Runtime Adapter：
IntentSpec
  -> to_task_intent
  -> IntentCompiler
  -> TaskContext
~~~

对于简单 CREATE_CONTENT，使用明确的 L1 TaskIntent -> IntentSpec 投影并立即复用 IntentValidator，即可补齐当前缺口；对于复杂请求，坚持 Direct L2 正式 Spec，拒绝无 Spec 的兼容回退。这样不会重新引入历史 IntentElements/IntentDraft 作为 Runtime 主入口，也不会改变下游 Runtime 状态模型。

本文件只记录设计方案；本阶段未修改业务代码。
