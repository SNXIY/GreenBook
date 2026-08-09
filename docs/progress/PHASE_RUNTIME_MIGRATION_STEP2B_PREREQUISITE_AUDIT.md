# GreenBook Runtime Migration Step2-B-1 前置能力审计

- 审计范围：ConversationRuntimeAdapter 实现前的 IntentSpec 正式产出与 Task Provider。
- 审计方式：只读检查代码、调用关系和已有运行时模型；未修改业务代码。
- 当前分支：feature/runtime-http-migration
- 审计基线：541c83e（docs(runtime): plan conversation message migration）
- 工作区说明：保留既有、与本审计无关的两个未跟踪文件，未纳入本次变更。

## 结论摘要

当前 Runtime 的 Planner、PlanExecution 和 RuntimeAgentService 已经具备接收 TaskContext 的执行能力，但消息入口尚未提供这个上下文。前置缺口不是 Planner 或 Worker，而是两个 API 边界：

1. TaskUnderstanding 的公开返回契约仍是 TaskIntent。简单、明确的 L1 请求不会产生正式 IntentSpec。
2. Task 与 TaskContext 已有领域模型、Repository 和 Registry，但 Assistant API 没有 Task Provider，也没有把请求范围内的 Task 查询或创建接入消息入口。

因此，当前不能直接实现一个可靠的 ConversationRuntimeAdapter。若强行接入，简单 CREATE_CONTENT 请求会在 IntentCompiler 处缺少正式语义或 Task，最终得到 INTENT_REQUIRED、TASK_CONTEXT_REQUIRED，而不是 execution_id。

## 一、IntentSpec 正式产出链路

### 1. TaskUnderstanding 入口与返回类型

入口文件：

packages/assistant_core/greenbook_assistant_core/task/understanding.py

核心位置：

- TaskUnderstanding 类：约第 386 行
- understand 方法：约第 414 行

公开方法签名的事实是：

~~~text
understand(user_message, existing_tasks=None) -> TaskIntent
~~~

它无论走 L1、正式 Direct L2，还是原始 L2 兼容回退，公开结果类型都是 TaskIntent。正式 IntentSpec 只可能以 TaskIntent.intent_spec 的序列化字段附带出现，不是该方法的稳定返回类型。

### 2. L1 路径

L1 由 TaskUnderstanding._quick_intent 负责：

1. 对文本执行确定性关键词和模式判断。
2. 识别 CREATE、REVISE、SCHEDULE、SEARCH、ANALYZE 等信号。
3. 组装 TaskIntent：
   - relation
   - goal
   - goal_category
   - requirements
   - constraints
   - resource_requests
   - confidence
   - source=L1
4. 不创建 IntentSpec，intent_spec 保持 None。

L1 适合明确的单一请求，但当前类型边界不能直接满足 Runtime 的正式 IntentSpec -> TaskContext -> Planner 链路。

### 3. Direct L2 路径

L2 路由由两个判断共同决定：

- TaskUnderstanding._needs_l2
- TaskUnderstanding._needs_l2_v2

条件、审批、多动作、时间变更、历史引用或较长文本会提高 L2 分数。触发后，当前正式路径为：

~~~text
TaskUnderstanding.understand
  -> _try_l2_v2
  -> _llm_understand_direct_v2
  -> _parse_intent_spec / IntentSpec.model_validate
  -> IntentValidator.validate
  -> 必要时 _llm_repair_spec
  -> 再次 IntentValidator.validate
  -> to_task_intent(IntentSpec)
  -> TaskIntent.intent_spec = spec.model_dump(...)
~~~

Direct L2 的正式模型位于：

packages/assistant_core/greenbook_assistant_core/task/intent_models.py

IntentSpec 包含：

- mode
- goal
- actions
- conditions
- constraints
- target_hint
- confidence
- source

IntentSpec 明确不包含执行计划字段，例如 step_id、seq、depends_on 或工具调用参数；这些由后续 Orchestrator 推导。

L2 失败时会回退到原始 _llm_understand，再回退到 L1。原始 _llm_understand 通过 _parse_llm_output 直接生成 TaskIntent，不能保证附带 IntentSpec。因此“触发了 L2”不等于“必然得到正式 IntentSpec”，只有 Direct L2 成功并通过验证时才成立。

### 4. 是否存在正式 IntentSpec Builder

当前没有一个面向消息入口、公开且稳定的 IntentSpecBuilder。

实际存在的三类能力需要区分：

|位置|性质|是否应作为新 Runtime 正式入口|
|---|---|---|
|TaskUnderstanding._llm_understand_direct_v2 + _parse_intent_spec|Direct L2 的正式解析边界|是|
|packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_elements.py 的 IntentSpecBuilder|历史兼容转换：IntentElements -> IntentSpec|否，不能重新成为 Direct L2 入口|
|packages/assistant_core/greenbook_assistant_core/compatibility/intent/intent_draft.py 的 IntentCompiler|历史兼容转换：IntentDraft -> IntentSpec|否，不能替代消息语义边界|
|apps/assistant_api/greenbook_assistant_api/services/intent_compiler.py 的 IntentCompiler|已经理解的 IntentSpec/TaskIntent -> TaskContext|不是 IntentSpec 生成器|

task/intent_elements.py 和 task/intent_draft.py 主要是兼容导入入口。task/intent_compat.py 提供的是 IntentSpec -> TaskIntent 的单向投影，没有正式的 TaskIntent -> IntentSpec 逆向契约。

### 5. IntentValidator 的调用方式

验证器位于：

packages/assistant_core/greenbook_assistant_core/task/intent_validator.py

核心方法：

~~~text
IntentValidator.validate(spec: IntentSpec, original_text: str)
    -> IntentValidationResult
~~~

当前只有 TaskUnderstanding._try_l2_v2 会正式调用它：

1. Direct L2 解析得到 IntentSpec 后立即验证。
2. 如果是可修复问题且存在 LLM，调用 _llm_repair_spec。
3. 修复结果再次验证。
4. 只有通过验证的 Spec 才转换成带 intent_spec 的 TaskIntent。

L1 TaskIntent 不经过 IntentValidator。原始 L2 的 TaskIntent 解析也不经过该验证器。这是 ConversationRuntimeAdapter 需要补齐的边界：进入 IntentCompiler/Planner 前必须有一个已验证的 IntentSpec 或明确失败，而不能把未验证的 TaskIntent 当作正式 Spec。

### 6. 简单 CREATE_CONTENT 请求的实测结果

输入：

~~~text
帮我写一篇AI Agent学习路线帖子
~~~

直接调用 TaskUnderstanding.understand 的结果为：

~~~json
{
  "relation": "NEW_TASK",
  "goal": "帮我写一篇AI Agent学习路线帖子",
  "goal_category": "CREATE_CONTENT",
  "target_task_id": null,
  "target_task_hint": null,
  "target_entity_refs": [],
  "requirements": [
    {"type": "CREATE"}
  ],
  "constraints": [],
  "resource_requests": [
    {"operation": "CREATE", "resource_type": "CONTENT_DRAFT"}
  ],
  "confidence": 0.85,
  "source": "L1",
  "intent_spec": null
}
~~~

该请求是明确的单一 CREATE 请求，L2-v2 分数为 0，因此不会进入 Direct L2。结论是：当前公开 understanding 入口对这个真实用例不会产出正式 IntentSpec。

### 7. 当前真实消息链路

当前 Assistant 消息 endpoint 仍在：

apps/assistant_api/greenbook_assistant_api/api/routes.py

主要位置：

- POST 消息路由：约第 597 行
- send_message：约第 602 行
- CommunityOperationsAssistant 实例化：约第 810 行

当前实际链路：

~~~text
POST /api/v1/assistant/conversations/{conversation_id}/messages
  -> routes.send_message
  -> _get_auth / _get_session
  -> conversation_store、message_store、旧历史/运行记录
  -> CommunityOperationsAssistant
  -> 旧 Assistant 响应链路
~~~

该链路没有调用 TaskUnderstanding、IntentCompiler 或 RuntimeAgentService。因此对于上述 CREATE_CONTENT 输入，当前真实请求不会产生：

- 正式 IntentSpec
- TaskContext
- TaskPlan
- PlanExecution
- Runtime execution_id

RuntimeAgentService 只有在调用方已经构造出带 task_context 的 RuntimeContext 后才执行；它本身不是原始用户文本理解器。它在 execute 入口会拒绝缺少 task_id 或 task_intent 的上下文，并返回 TASK_CONTEXT_REQUIRED。

## 二、Task Provider 链路

### 1. Task 模型

Task 定义位于：

packages/assistant_core/greenbook_assistant_core/task/models.py

Task 是跨轮次的长任务模型，主要字段包括：

- task_id
- conversation_id
- user_id
- tenant_id
- goal
- goal_category
- status
- phase
- artifacts
- depends_on
- retry_count、version、时间戳

TaskContext 定义位于：

apps/assistant_api/greenbook_assistant_api/models/runtime_context.py

它是传入 Runtime 的不可变执行快照，至少要求：

- task_id
- goal
- task_intent

同时携带 target、constraints、active_artifact_id 和 artifact_refs。

### 2. Repository 和 Registry

实现位于：

packages/assistant_core/greenbook_assistant_core/task/registry.py

已有组件：

- TaskRepository：基于 AsyncSession 的 assistant_tasks 低层 CRUD。
- TaskRegistry：对 TaskRepository 的业务封装。
- TaskRegistry.create_task：创建 Task，可指定 task_id、goal、goal_category、phase 和 status。
- TaskRegistry.get_task、list_tasks、get_most_recent。
- TaskRegistry.resolve_task：按 conversation_id 和 hint 查询，当前策略是命中目标提示，否则取最近 Task。
- TaskIntentRepository：保存 TaskIntent 记录。

这说明领域能力已经存在，不需要新增 Task 模型或 Legacy RunRepository。缺口是 Assistant API 没有把这些已有类作为消息处理依赖提供出来。

注意：当前 TaskRepository.find_by_conversation 主要按 conversation_id 查询；Task Provider 在 API 边界还必须校验 user_id 和 tenant_id，不能仅凭 conversation_id 作为授权依据。

### 3. 当前 TaskContext 创建依赖

IntentCompiler 位于：

apps/assistant_api/greenbook_assistant_api/services/intent_compiler.py

它不是理解器，而是一个纯语义编译器。compile 需要：

- IntentSpec 或 TaskIntent
- 对 NEW_TASK，提供一个真实 Task
- 对 CONTINUE/MODIFY_TASK，提供 target_context 或 conversation.active_task_id
- Task 与解析出的 task_id 必须一致
- ArtifactRef 必须属于该 task_id
- goal 不能为空

典型错误码已经定义：

- INTENT_REQUIRED
- TASK_CONTEXT_REQUIRED
- TARGET_CONTEXT_REQUIRED
- TASK_REQUIRED
- TASK_CONTEXT_MISMATCH
- AMBIGUOUS_TARGET
- ARTIFACT_TASK_MISMATCH

因此，TaskContext 不是由 RuntimeAgentService 自动创建的；必须由消息适配层先完成 Task 创建/查询和 IntentCompiler.compile。

### 4. NEW_TASK 最小创建方案

最小的语义闭环可以复用现有模型和编译器：

~~~text
1. TaskUnderstanding 取得 TaskIntent。
2. 正式语义边界取得并验证 IntentSpec。
3. 生成新的 task_id。
4. 构造 Task：
   conversation_id、user_id、tenant_id、goal、goal_category、status=READY。
5. 调用 IntentCompiler.compile：
   intent_spec、task_intent、task、conversation。
6. 得到带 task_id 的 TaskContext。
7. 将 TaskContext 放入 RuntimeContext。
8. 调用 RuntimeAgentService.execute，后续才会进入 Planner/PlanExecution。
~~~

如果在 Task Provider 接入数据库前只构造内存 Task 快照，该方案可以作为单次 NEW_TASK 的前置测试，但不具备跨请求恢复和多轮绑定能力。生产方案应使用已有 TaskRegistry.create_task，并使用请求生命周期内的 AsyncSession。

TaskRegistry.create_task 的默认 status 是 COMPLETED；运行中的新任务必须显式传入 READY 或项目认可的运行前状态，不能依赖默认值。

### 5. CONTINUE/MODIFY_TASK 查询方案

现有任务操作需要按以下顺序：

~~~text
1. TaskUnderstanding 产出 relation=CONTINUE 或 MODIFY_TASK、target_task_id/target_task_hint。
2. Task Provider 按 conversation_id 查询候选 Task。
3. 同时校验 user_id、tenant_id 所属关系。
4. 使用 TaskResolver.resolve(intent, tasks)，或由 TaskRegistry.resolve_task 处理已有的提示/最近任务策略。
5. 只有一个明确目标时构造 ResolvedTaskTarget / TargetContext。
6. 将 Task 与 target_context 交给 IntentCompiler.compile。
7. 若无目标、目标不存在或多个候选，返回明确的 TARGET_CONTEXT_REQUIRED、TASK_NOT_FOUND 或 AMBIGUOUS_TARGET，不得猜测。
~~~

当前 SessionContext 只有 active_draft_id、active_schedule_id 等资源绑定字段，没有完整的 active_task_id 保障。因此 CONTINUE/MODIFY 不能只依赖旧 conversation_store；需要 Task Provider 查询和显式目标解析。

## 三、Step2-B 实现前必须补齐的最小范围

### 必须补齐

1. 正式 IntentSpec 边界
   - 在 TaskUnderstanding 或紧邻的 task 语义层增加公开的“取得已验证 IntentSpec”契约。
   - Direct L2 复用现有解析和 IntentValidator。
   - L1 的简单请求需要一个受控的 TaskIntent -> IntentSpec 投影，并对原始文本执行 IntentValidator。
   - 不要把 compatibility/intent 下的历史 IntentSpecBuilder 重新作为新入口。
   - 不要让 Adapter 调用私有方法后自行拼接未验证字典。

2. Task Provider
   - 复用现有 Task、TaskRepository、TaskRegistry、TaskResolver。
   - 为 Assistant API 提供请求范围的 session/provider 获取方式；不要把一个长期 AsyncSession 放入 app.state。
   - 至少支持 NEW_TASK 的 create，并显式设置 READY。
   - CONTINUE/MODIFY_TASK 必须支持 scoped query、目标解析、所有权校验和歧义错误。

3. Adapter 的最小依赖注入
   - TaskUnderstanding 或正式 IntentSpec provider
   - IntentCompiler
   - Task Provider/TaskResolver
   - 已有 RuntimeAgentService
   - RuntimeContext/TaskContext 构造器
   - 原消息/旧链路切换所需的可回滚开关或薄适配层

4. 验证用例
   - 简单 CREATE_CONTENT 输入必须断言 IntentSpec 非空且通过 IntentValidator。
   - NEW_TASK 必须断言 TaskContext.task_id、Task.task_id 一致。
   - CONTINUE/MODIFY_TASK 必须覆盖精确命中、无命中和多候选。
   - 适配器调用 RuntimeAgentService 前必须已经具备 TaskContext；不能用 TASK_CONTEXT_REQUIRED 作为正常流程分支。

### 不需要修改的范围

下列组件已经是可复用的下游能力，不属于本次前置缺口：

- Planner/TaskOrchestrator
- PlanExecution
- ExecutionWorker
- ToolRuntime、CapabilityExecutor
- ExecutionStateManager
- ExecutionEventStore
- RuntimeAgentService 的执行核心
- Task 数据模型和已有 assistant_tasks 表结构
- Legacy Agent 删除或迁移

## 四、建议的实现顺序

~~~text
Step2-B-1-a  建立公开、已验证的 IntentSpec 产出边界
Step2-B-1-b  接入请求范围的 Task Provider；先完成 NEW_TASK
Step2-B-1-c  接入 CONTINUE/MODIFY_TASK 的 TaskResolver 和所有权校验
Step2-B-1-d  为三类前置能力增加单元测试
Step2-B-2    才实现 ConversationRuntimeAdapter
~~~

若当前阶段不允许马上接入数据库 session，则只能先实现 NEW_TASK 的临时 Task 快照测试，并对 CONTINUE/MODIFY_TASK 明确返回 TARGET_CONTEXT_REQUIRED；不能把 conversation_store 或 assistant_runs 伪装成 Task Provider。

## 五、审计结论

现状属于“Runtime 执行层已具备、消息语义和任务绑定尚未接线”的中间状态。

对目标输入“帮我写一篇AI Agent学习路线帖子”：

- TaskUnderstanding 当前产出 L1 TaskIntent，intent_spec=null。
- 当前真实 POST 消息入口仍调用 CommunityOperationsAssistant。
- 因此当前真实链路不会产生 Runtime execution_id。
- ConversationRuntimeAdapter 前必须先补齐正式 IntentSpec 契约和 Task Provider。
- 补齐后才可以安全复用现有 IntentCompiler 与 RuntimeAgentService，而不需要修改 Planner、Worker 或 Runtime 状态模型。

本报告只记录审计结果；未修改业务代码、数据库或旧路由。
