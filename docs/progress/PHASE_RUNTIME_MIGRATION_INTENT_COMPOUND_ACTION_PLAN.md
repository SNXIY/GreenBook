# Runtime L1 复合动作投影分析

## 结论摘要

真实请求：

```text
明天上午八点发布一篇关于如何学好 Java 的帖子
```

当前失败不是 Planner、Worker 或 ToolRuntime 的问题，而是
`IntentSpecProvider` 的 L1 投影仍然只支持“单一 CREATE_CONTENT”。

L1 已经识别出了两个业务意图：创建内容、定时发布；但投影层在生成
`IntentSpec` 之前主动拒绝了第二个动作，并且没有把 L1 的时间信号写入
`IntentSpec.constraints`。

本报告只做审计和方案设计，没有修改 Runtime 代码。

## 1. 当前调用链

```text
ConversationRuntimeAdapter.execute()
  -> IntentSpecProvider.resolve()
    -> TaskUnderstanding.understand()
      -> TaskUnderstanding._quick_intent()       # L1
    -> IntentSpecProvider._project_l1_create_content()
    -> IntentSpecProvider._validate()
  -> TaskProvider
  -> IntentCompiler
  -> RuntimeAgentService
```

具体位置：

|职责|文件|关键函数|
|---|---|---|
|L1 关键词理解|`packages/assistant_core/greenbook_assistant_core/task/understanding.py`|`TaskUnderstanding._quick_intent`|
|L1 需求生成|同上|`_derive_requirements`|
|L1 资源请求生成|同上|`_derive_resource_requests`|
|L1 正式投影|`packages/assistant_core/greenbook_assistant_core/task/intent_spec_provider.py`|`_project_l1_create_content`|
|正式校验|同上|`IntentSpecProvider._validate`、`IntentValidator.validate`|
|复合计划选择|`packages/assistant_core/greenbook_assistant_core/orchestration/orchestrator.py`|`_intent_spec_requirement_types`、`_select_template`|

## 2. L1 当前支持范围

`_project_l1_create_content` 当前要求：

1. `goal_category == CREATE_CONTENT`
2. `relation == NEW_TASK`
3. `goal` 非空
4. `requirements` 中除 `CREATE` 外不能出现其他类型
5. `resource_requests` 中除 `CONTENT_DRAFT` 外不能出现其他资源

通过后固定生成：

```text
mode: SIMPLE
actions:
  - action: CREATE
    resource: CONTENT
conditions: []
constraints: _project_constraints(intent)
source: L1
```

因此它实际只覆盖类似：

```text
帮我写一篇 AI Agent 学习路线帖子
```

不覆盖创建后发布、创建后定时发布、创建后审批等复合动作。

这里的 `SCHEDULE_PUBLISH` 不是 IntentSpec 的 action 名称。正式语义应为：

```text
IntentSpec actions:
  CREATE + CONTENT
  PUBLISH + CONTENT
constraint:
  TIME
```

之后 Planner 才把这组语义映射为：

```text
GENERATE_CONTENT
VALIDATE_QUALITY
SCHEDULE_PUBLISH
```

## 3. actions 字段在哪里生成

### L1

L1 的 `TaskUnderstanding._quick_intent` 不直接生成 `IntentSpec.actions`，而是生成旧的
`TaskIntent` 字段：

```python
requirements = [{"type": "CREATE"}, ...]
resource_requests = [{"operation": ..., "resource_type": ...}, ...]
```

随后 `IntentSpecProvider._project_l1_create_content` 硬编码生成唯一的：

```python
IntentAction(action=ActionType.CREATE, resource=ResourceType.CONTENT)
```

所以 L1 的 PUBLISH 信息即使已经存在于 `TaskIntent.requirements`，也从未被投影成
`IntentSpec.actions`。

### Direct L2

Direct L2 在 `TaskUnderstanding._llm_understand_direct_v2` 中解析 LLM 返回的正式
IntentSpec JSON，直接得到 `actions`、`conditions` 和 `constraints`，随后由
`_try_l2_v2` 做校验和定向修复。

### 兼容性 Elements/Draft 路径

`compatibility/intent/intent_elements.py` 中的 `IntentSpecBuilder` 也有
`CREATE`/`PUBLISH` 映射，但它不是当前 L1 投影路径。不能通过恢复旧兼容转换来绕过
`IntentSpecProvider` 的正式边界。

## 4. 为什么该请求无法投影

对该请求，L1 的布尔信号是：

```text
asks_create   = True
asks_schedule = True
```

`_quick_intent` 因此产生的核心结果为：

```json
{
  "relation": "NEW_TASK",
  "goal_category": "CREATE_CONTENT",
  "requirements": [
    {"type": "CREATE"},
    {"type": "PUBLISH"}
  ],
  "resource_requests": [
    {"operation": "CREATE", "resource_type": "CONTENT_DRAFT"},
    {"operation": "CREATE", "resource_type": "SCHEDULE"}
  ],
  "source": "L1"
}
```

失败发生在 `_project_l1_create_content` 的第一道复合动作保护：

```python
if requirement_types - {"CREATE"}:
    raise IntentSpecProviderError(
        "INTENT_UNSUPPORTED",
        "L1 CREATE_CONTENT contains actions not covered by the simple projection.",
    )
```

因此用户看到的错误正是这条保护产生的结果。即使只删除这道保护，后续仍会遇到
第二个问题：

```python
if resource_types - {"CONTENT_DRAFT"}:
    raise IntentSpecProviderError(...)
```

更重要的是，当前 L1 没有时间约束生成器。`_derive_requirements` 和
`_derive_resource_requests` 只生成需求/资源，不填充 `TaskIntent.constraints`；
该字段仍为空。`_project_constraints` 因此也生成空列表。

最终如果只放宽两个集合校验，Provider 仍会产生只有 CREATE、没有 TIME 的 Spec，随后
`IntentValidator.validate` 会根据原始文本报：

```text
Missing TIME constraint
```

所以问题不是单一白名单，而是三个缺口同时存在：

|缺口|当前表现|
|---|---|
|复合 requirement 被拒绝|`CREATE + PUBLISH` 触发当前错误|
|SCHEDULE 资源被拒绝|放宽 requirement 后仍会失败|
|时间没有进入 formal Spec|放宽前两项后会触发 `Missing TIME constraint`|

## 5. Direct L2 是否支持该场景

支持，但当前请求通常不会自动走 Direct L2。

Direct L2 的 `_L2_SYSTEM_V2` 已明确规定：

- “发布/定时”映射为 `PUBLISH + CONTENT`
- 有时间表达时必须产生 `TIME` constraint
- 输出 schema 允许多个 action
- `COMPOSITE` mode 用于多个动作

因此一个合法的 L2 结果应类似：

```json
{
  "mode": "COMPOSITE",
  "goal": "创建 Java 学习文章并定时发布",
  "actions": [
    {"action": "CREATE", "resource": "CONTENT"},
    {"action": "PUBLISH", "resource": "CONTENT"}
  ],
  "conditions": [],
  "constraints": [
    {"type": "TIME", "value": "明天上午八点"}
  ],
  "source": "L2"
}
```

但 `TaskUnderstanding.understand` 的 L2 路由主要依赖：条件词、审批词、复合连接词、
历史引用或“修改发布时间”等时间变更词。单句“明天上午八点发布一篇……”不一定命中
`_needs_l2_v2`/`_needs_l2`，所以会留在 L1，最终进入上述严格投影并失败。

一旦 Direct L2 实际返回 `intent_spec`，`IntentSpecProvider.resolve` 会直接使用该快照，
再调用 `model_validate` 和 `IntentValidator`，不会经过 `_project_l1_create_content`。

## 6. 最小修复方案（设计，不执行）

### 推荐：只扩展正式 L1 投影边界

优先修改范围应限定为：

```text
packages/assistant_core/greenbook_assistant_core/task/intent_spec_provider.py
tests/unit/test_intent_spec_provider.py
```

建议步骤：

1. 让 `_project_l1_create_content` 接收原始 `user_message`，或使用已有的完整
   `intent.goal` 作为时间解析输入。
2. 将 L1 的无损组合识别为：
   - requirements：`CREATE + PUBLISH`
   - resources：`CONTENT_DRAFT + SCHEDULE`
3. 对该组合生成 `IntentMode.COMPOSITE`。
4. 生成两个正式 actions：
   - `CREATE / CONTENT`
   - `PUBLISH / CONTENT`
5. 将时间表达保留为 `IntentConstraint(type=TIME, value=...)`，禁止静默丢弃。
6. 对未识别的 requirement/resource 继续 fail-closed；不要把所有 L1 组合都强行投影成
   CREATE_CONTENT。
7. 让现有 `IntentValidator` 继续作为最终语义闸门。

时间归一化不应写入 IntentSpec 的 Planner 字段。现有执行边界已经具备：

```text
IntentSpec TIME constraint + user_message
  -> ArgumentBinder
  -> TemporalResolver
  -> PlanStep.constraints["run_at"]
  -> SCHEDULE_PUBLISH tool
```

也就是说，IntentSpec 保留“明天上午八点”这一语义约束，`TemporalResolver` 在参数绑定
阶段产生带时区的 canonical `run_at`，满足时间必须进入 TaskPlan/工具参数的要求。

### 不建议作为唯一修复：强制改走 Direct L2

可以扩大 `_needs_l2_v2` 让“CREATE + future time + publish”触发 L2，但这会把本来可
确定解析的请求绑定到 LLM 可用性、输出质量和重试路径；即使 LLM 不可用，Runtime 仍会
失败。它最多作为后续增强，不应替代确定性的 L1 compound projection。

### 不需要修改的模块

以下模块已有复合计划能力，本问题不需要改动：

- `RuntimeAgentService`
- `Planner` / `TaskOrchestrator`
- `Worker`
- `ToolRuntime`
- `ExecutionStateManager`

`TaskOrchestrator._select_template` 已支持 `CREATE + PUBLISH`，并选择
`CREATE_AND_PUBLISH` 模板；该模板本身已包含：

```text
GENERATE_CONTENT
VALIDATE_QUALITY
SCHEDULE_PUBLISH
```

## 7. 建议回归测试

在 `tests/unit/test_intent_spec_provider.py` 增加精确输入覆盖：

```text
明天上午八点发布一篇关于如何学好 Java 的帖子
```

至少验证：

1. Provider 不抛出 `INTENT_UNSUPPORTED`。
2. `spec.mode == COMPOSITE`。
3. actions 同时包含 `CREATE/CONTENT` 与 `PUBLISH/CONTENT`。
4. constraints 包含 `TIME`。
5. source 仍为 `L1`。

再用现有 Planner/ArgumentBinder 测试验证：

```text
IntentSpec
  -> CREATE_AND_PUBLISH TaskPlan
  -> GENERATE_CONTENT
  -> VALIDATE_QUALITY
  -> SCHEDULE_PUBLISH
  -> canonical run_at
```

应保留已有纯创建 L1、Direct L2、非法 Spec 和禁止 compatibility fallback 测试。

## 8. 当前状态

- 代码修改：无
- 删除/移动：无
- Runtime 核心修改：无
- 新增分析文档：本文件
- 推荐下一步：先确认本方案，再只实现 L1 compound projection 与对应单元测试

