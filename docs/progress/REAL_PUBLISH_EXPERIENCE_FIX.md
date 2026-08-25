# GreenBook Conversational Agent Experience

本次以 `zhiguang-fe` 的 Agent Chat 展示边界为主，并在对话投影边界增加了只读检索结果的结构化消息 part；没有修改 AgentLoop 的执行语义、Execution、Task、Tool Runtime、MCP、Creator Service 或 Java 业务行为。

## Natural Language First Interaction Model

### UX Contract

```text
自然语言 = 主要操作入口
UI = 状态与结果确认
链接 = 页面导航
按钮 = 明确审批 / 高风险授权
折叠区域 = 技术执行详情
```

Agent Chat 不再把结果卡当作 CRUD 操作面板。用户可以继续用自然语言表达修改、控制、查询和分析请求；前端只负责展示最新业务事实、内容产出、审批请求和必要的导航。

### Interaction Types

统一投影入口位于 `src/components/agent/userFacingResult.ts`：

- `CONTENT_RESULT`：创建内容、内容已生成或发布结果。
- `QUERY_RESULT`：社区内容、草稿和帖子列表，使用真实资源链接导航。
- `ANALYSIS_RESULT`：真实指标、结论和重点，不展示 provenance 枚举。
- `CHANGE_CONFIRMATION`：内容、标题或发布时间的增量更新，显示目标、变化和保持不变的事实。
- `CONTROL_CONFIRMATION`：暂停、恢复、取消或其他业务状态变化，显示最新状态和保留内容。
- `APPROVAL_REQUEST`：高风险操作的明确授权卡，只保留确认/拒绝/返回修改按钮。
- `ASK_USER`：多个合理目标并存时，请用户选择目标，不在前端猜测。
- `FAILURE_RESULT`：用户可理解的失败原因和已有内容是否保留；没有明确、低歧义的恢复动作时不堆叠按钮。

### UserFacingInteraction

结果组件消费 `UserFacingInteraction`，不直接消费 `Execution`、`Task`、`Goal`、`StepExecution` 或 `ToolResult` 作为默认展示数据：

```typescript
type UserFacingInteraction =
  | { kind: "CONTENT_RESULT"; result: UserFacingResult }
  | { kind: "QUERY_RESULT"; result: UserFacingResult }
  | { kind: "SYNTHESIS_RESULT"; synthesis: SynthesisResult }
  | { kind: "ANALYSIS_RESULT"; result: UserFacingResult }
  | { kind: "CHANGE_CONFIRMATION"; result: UserFacingResult; change: ChangeConfirmation }
  | { kind: "CONTROL_CONFIRMATION"; result: UserFacingResult; control: ControlConfirmation }
  | { kind: "APPROVAL_REQUEST"; approval: UserFacingApprovalRequest }
  | { kind: "ASK_USER"; clarification: UserFacingTargetClarification }
  | { kind: "FAILURE_RESULT"; result: UserFacingResult };
```

Runtime 标识符和内部 prompt 只保存在 `technical` / 交互解析所需的不可见数据中，并通过“查看技术详情”折叠入口访问。默认 DOM 不包含 UUID、内部目标描述或 Tool 参数。

## Button Policy

结果卡中的默认动作现在只承担页面导航：查看草稿、查看帖子、进入任务中心。修改标题、修改内容、调整时间、取消定时、重新安排、重新生成和分析等动作显示自然语言提示，由用户继续输入完成。

审批是例外：公开发布、删除公开内容等高风险副作用仍然显示明确的“确认 / 拒绝 / 返回修改”按钮。执行暂停、恢复和重试能力保留在技术详情或任务中心，不再占据 Chat 主视觉。

所有显示的链接和审批按钮都使用已有前端路由或真实 API；没有后端能力的操作不会伪造按钮。

## Multi-turn behavior

- AgentPanel 继续发送用户原始自然语言，不在 React 中根据“刚才”“上一个”或特定业务词做 keyword router。
- 后端现有 Conversation Context、TargetResolver、TaskManager 和 Artifact lineage 负责目标解析与同一逻辑任务的连续绑定。
- 修改结果用 `ChangeConfirmation` 轻量确认，不重复渲染完整内容大卡片。
- 最新业务 artifact / schedule / publication 事实优先于历史消息和旧快照；历史技术信息仍可在折叠详情中追踪。
- 任务处理期间保留输入框；审批等待期间锁定普通输入，避免绕过明确授权流程。

## Ambiguity handling

当后端返回多个候选目标时，前端只展示用户友好的候选标题和状态，并要求用户选择。候选的 task/resource/artifact/execution 标识只用于提交真实 command，不展示给用户，也不由前端自行猜测目标。

## Validation scenarios

代码路径覆盖以下通用行为，而不是某个 Redis、Java 或时间表达式特例：

| 场景 | User-facing projection | 验收重点 |
| --- | --- | --- |
| Create | `CONTENT_RESULT` | 内容标题、正文预览和真实导航 |
| Modify content | `CHANGE_CONFIRMATION` | 同一目标的增量确认，不重复大卡片 |
| Modify metadata | `CHANGE_CONFIRMATION` | 标题/发布时间展示最新事实 |
| Control | `CONTROL_CONFIRMATION` | 暂停、恢复、取消后的状态和保留内容 |
| Query | `QUERY_RESULT` | 列表、摘要和真实资源链接 |
| Analyze | `ANALYSIS_RESULT` | 真实指标、结论和自然语言提示 |
| Ambiguous reference | `ASK_USER` | 多候选时询问，不自动猜目标 |
| Approval | `APPROVAL_REQUEST` | 只有高风险授权保留主操作按钮 |
| Failure | `FAILURE_RESULT` | 原因、数据保留情况和自然语言恢复入口 |

## Tests

在 `zhiguang-fe` 执行：

- `npm run lint` — 通过。
- `npm run build` — 通过，Vite 生产构建完成。
- `npm run test:execution` — 通过，Execution API contract test 成功。
- `npm run test:agent-ux` — 通过，覆盖部分成功、内部 prompt / reflection 过滤、Runtime 标识隐藏、修改确认、控制确认、查询、分析、失败、审批、歧义目标和综合结果。
- `pytest -q tests/unit/test_retrieval_synthesis_projection.py tests/unit/test_phase17_conversation_runtime.py tests/unit/test_agent_loop.py tests/e2e/test_execution_presentation.py` — 通过，16 个用例。

构建输出的 baseline-browser-mapping / Browserslist 过期提示属于仓库依赖提示，本阶段没有升级依赖。

## Scope and remaining UX debt

本阶段没有新增业务 API，也没有改变 Runtime 语义。任务中心仍然可以展示完整的 Task、Execution、Goal、Plan、Step、Timeline 和重试控制。后续如果需要更细的“前后值”展示，应由真实 artifact projection 提供字段，前端不会从自然语言或内部 prompt 猜测。

## Retrieval and Synthesis Experience

### Root cause

只读检索没有进入结构化消息投影：`AgentLoop` 的 `Reflection.reason` 被复制到 `RuntimeResult.content`，对话接口随后直接保存这段文本；当消息没有 `execution_result` 时，`AgentPanel` 又把它当普通 Markdown 渲染。结果是搜索结果、正文证据和综合结论没有形成同一个用户层结果，且内部的 goal satisfaction / execution summary 可能泄露。

### Final response source before / after

旧路径是：

```text
Reflection.reason → RuntimeResult.content → assistant message Markdown
```

现在是：

```text
Search / detail tool evidence
        ↓
retrieval_synthesis_projection
        ↓
user_facing_interaction part
        ↓
UserFacingProjection
        ↓
GreenBook Chat
```

对话适配层只把一条短业务摘要写入兼容 `content`，完整查询或综合结果放在结构化 part 中；前端只要发现该 part，就不再渲染原始 assistant 文本。Reflection、goal satisfaction、execution summary 和 raw tool result 不再作为最终回答来源。

### Evidence model

- 纯搜索保持 `QUERY_RESULT`，显示真实匹配数量、有限的代表性搜索项和可验证的帖子链接。
- 搜索后成功读取正文的内容形成 `sources`；总匹配数与实际阅读数分别保留，避免把“找到 18 篇”误写成“阅读了 18 篇”。
- 代表性内容沿现有搜索顺序去重并限制数量；没有新增 ranking Agent，也没有为每篇内容增加一次模型调用。
- `source_refs` 只用于内部追溯，来源标题、摘要和真实链接才进入用户界面；UUID、tool call 和执行标识不展示。
- `common_patterns` 在前端和投影边界都要求至少两个独立来源支撑；单一来源观点不会冒充共同点。

### Synthesis projection

`SYNTHESIS_RESULT` 包含 `sources`、`commonPatterns`、可选 `differences`、`conclusion` 和 `evidenceNote`。界面按“重点参考 → 共同点 / 差异 → 综合来看”的阅读顺序呈现，而不是堆叠执行卡片。纯搜索不触发综合；只有存在正文证据且任务语义需要综合时，才使用一次结构化 synthesis call。

### Reflection boundary and language

综合阶段的输入只有用户请求、已读取的来源标题/摘要/正文片段和来源引用，不包含完整 Execution trace。结构化 synthesis prompt 要求沿用用户请求的语言，并禁止输出 Agent、Tool、Runtime、Goal 或完成判断。现有 `userFacingMessage` 仍保留 reflection 文案过滤作为最后防线，但不再承担生成答案的职责。

### Partial retrieval and insufficient evidence

- 搜索为空时只显示“没有找到足够相关的内容”，不会让模型根据主题补写总结。
- 只有一篇正文或没有可追溯来源时，展示证据不足提示，不生成共同点。
- 部分正文读取失败时保留成功读取的来源，并标记“部分正文未能读取”；结果仍是可理解的部分结果，而不是把已有证据丢弃。

### Validation

新增 `tests/unit/test_retrieval_synthesis_projection.py` 覆盖纯搜索、跨来源综合、单来源共同点过滤、证据不足、部分读取失败和 reflection 不进入结果；`zhiguang-fe/tests/agent_projection.test.ts` 覆盖 `SYNTHESIS_RESULT`、来源保留、共同点支撑、语言层安全文本和纯搜索投影。

## Synthesis Activity & Evidence Presentation

### Activity mapping

AgentRun / Execution 的阶段字段先经过前端业务语义投影，再进入 Activity UI：

```text
搜索能力 / 社区搜索 → 正在查找相关内容…
详情读取 / 阅读能力 → 正在阅读代表性内容…
综合能力 / 跨文档分析 → 正在整理共同观点…
```

工具名、工具调用次数、Agent step 和百分比不会进入主对话。没有可识别阶段时只显示“正在处理你的请求”，发送后的即时占位文案为“正在理解你的请求…”。最终结构化结果出现时，AgentPanel 会隐藏仍在轮询的运行 Activity，避免结果与旧 loading 同时存在。

### Evidence ref boundary

后端 synthesis prompt 明确要求 `source_refs` 只能写入结构化数组，不能出现在标题、解释、引言或结论中。投影层会删除正文含内部引用、UUID 或不完整证据的 point；前端再次校验可用来源、去除正文中的内部引用，并保留结构化 `sourceRefs` 供技术追溯。普通用户看不到 `source-1`、`artifact-*` 等 token。

### Grounded patterns and differences

`common_patterns` 和 `differences` 都必须引用当前成功读取的来源；共同点和跨来源差异至少需要两个独立来源。只有一个来源、metadata-only 来源或带“可能分为具体阶段”这类无证据推测的 point 会被丢弃，空的共同点/差异区块不会被模板强行补齐。结论也不会在没有 grounded point 时单独冒充事实。

### Read status and source presentation

来源投影统一提供 `read_status`：`FULL`、`PARTIAL` 或 `METADATA_ONLY`。详情读取尝试数量、实际成功读取数量和搜索匹配数量分别以 `selected_count`、`read_count`、`total_matched` 传递；综合输入只接收有正文的 `FULL` / `PARTIAL` 来源。来源摘要先清理 Markdown 标记并限制长度，部分读取显示“已读取部分内容”，只有标题的来源明确标记为未参与综合。

### Tests

- `pytest -q tests/unit/test_retrieval_synthesis_projection.py` — 通过，6 个用例覆盖引用隔离、共同点/差异 grounding、无依据差异删除、数量口径、部分读取和 metadata-only 来源。
- `npm run test:agent-ux` — 通过，覆盖检索 Activity 的语义映射、终端结果前的综合阶段、来源读取状态、内部引用防御和可选差异区块。
- `npm run lint` — 通过。

构建阶段仍可能显示仓库已有的 baseline-browser-mapping / Browserslist 依赖提示；本阶段没有升级依赖或引入额外 LLM 调用。

## Approval & Composer Interaction

### Root cause

`AgentPanel` 原先在三个位置把审批状态当成 Composer 锁定条件：发送入口直接拒绝请求，`composerLocked` 直接绑定 `WAITING_APPROVAL / WAITING_HUMAN`，输入框和发送按钮再消费这个锁定值。与此同时，审批卡只从 `run.approval` 生成；当运行状态已经等待审批但审批投影暂时缺失时，页面会出现“草稿已完成 + 输入框不可用 + 没有审批卡”。

### Composer policy before / after

现在 Composer 使用独立的 `ComposerState`：

```text
READY       → 输入框和发送可用
SUBMITTING  → 本次自然语言请求提交中，短暂防重复发送
UNAVAILABLE → 会话或认证不可用
```

`WAITING_APPROVAL` 和 `WAITING_HUMAN` 不属于 ComposerState。审批按钮提交时仍可短暂禁用按钮本身，但自然语言输入不会因为审批状态被锁死。用户可以继续输入修改、查询或控制请求；前端不自行判断审批是否失效。

### Approval projection path

审批展示继续沿用：

```text
Run / Execution
↓
projectAgentRunToUserFacingInteraction
↓
UserFacingApprovalRequest
↓
AgentApprovalCard
```

真实 `PENDING` 审批显示确认/拒绝/返回修改按钮；`APPROVED`、`REJECTED`、`EXPIRED` 或 `CANCELLED` 不再显示可操作按钮。运行状态等待审批但审批记录缺失时，展示“确认信息暂时无法加载”的安全降级卡，同时保持 Composer 可用，不伪造确认 API。

审批卡会优先从真实草稿和 Schedule artifact 展示标题、正文摘要和计划发布时间；技术 ID 仍只在折叠详情中出现。创建内容与后续发布不再只显示“草稿已完成”，已有的 `draft + schedule` 组合投影继续展示整体业务状态。

### Multi-turn behavior during approval

审批等待期间发送自然语言会保留当前审批卡直到新请求有真实结果；提交期间审批按钮暂时不可点，Composer 只为防重复发送短暂进入 `SUBMITTING`。请求失败后恢复 `READY`，原审批如果仍是 `PENDING` 继续可操作。审批确认或拒绝后，前端依据后端返回的最新状态移除旧操作按钮，不自行创建或失效审批。

当前对话消息接口没有发现审批等待期间禁止新消息的前置拦截，因此本阶段未修改 AgentLoop、Execution Runtime 或 Approval Runtime。若后端业务拒绝这类多轮修改，真实拒绝结果会作为普通 Agent 消息返回，而不会让输入框进入永久禁用状态。

### Tests

- `zhiguang-fe/tests/agent_projection.test.ts` 覆盖 `READY / SUBMITTING / UNAVAILABLE` Composer policy、待审批卡可用性、审批投影缺失降级、计划发布时间展示、审批完成后按钮移除和草稿摘要保留。
- `npm run lint` — 通过。
- `npm run build` — 通过。
- `npm run test:execution` — 通过。
- `npm run test:agent-ux` — 通过。

## Multi-goal Result Aggregation

### Root cause

The Runtime execution container already retained all presentation artifacts for an execution. The loss happened at the user-facing boundary: the old projection used the first Draft/Schedule/Post match (`find`/scalar `draft` and `schedule` fields), so one execution with several business goals was rendered as one result card. The relation needed to join a Schedule back to its Draft (`draft_id`) was also not preserved in the body-free artifact projection.

### Projection and grouping

The projection now keeps the minimum read-only relation fields `draft_id`, `step_id`, and semantic step capability. `UserFacingResultGroup` is created from business targets, not Runtime step order:

```text
Draft A + Schedule A (schedule.draft_id = Draft A)
Draft B + Schedule B (schedule.draft_id = Draft B)
        ↓
one RESULT_GROUP with two independent user-facing items
```

Array position is never used to pair results. When an older payload has no explicit `draft_id`, the projection only falls back to a unique task/goal/step-scope match; an ambiguous schedule is not silently assigned to another Draft. Each item is projected independently, so a failed schedule or a pending approval does not hide another goal's successful Draft/Schedule result. A failed schedule step without an artifact is associated through its real step scope/capability and remains a partial result when the Draft exists.

The Chat now renders one readable group containing the per-goal title, Draft preview, current business status, that goal's schedule, navigation, and its own folded technical details. It does not add per-goal CRUD buttons or expose task/goal/execution IDs in the default view.

### Before / after

Before, a multi-goal execution was reduced to:

```text
one Draft + one latest/first Schedule
```

After, the same conversation can render:

```text
two posts are ready
  - Goal A: Draft A, Schedule A, time A
  - Goal B: Draft B, Schedule B, time B
```

The aggregate status is `SUCCESS` when every item succeeds, `PARTIAL_SUCCESS` when at least one item is incomplete while another produced a result, `NEEDS_ACTION` when an item is awaiting approval, and `FAILED` only when every item fails.

## Schedule Timezone Presentation

### Verified time path

The existing temporal resolver emits an offset-aware UTC instant. The Runtime and persisted artifact projection now preserve the Schedule's `run_at`, `timezone`, and Draft relation. The Chat boundary then formats the instant through one shared utility:

```text
relative/absolute user request
  → UTC ISO instant from Runtime
  → API/artifact projection keeps run_at + timezone
  → formatBusinessDateTime(value, displayTimezone)
  → Intl.DateTimeFormat(timeZone)
```

This fixes the previous presentation error where a UTC clock was displayed as if it were China Standard Time. `2026-08-13T05:25:00Z` with `Asia/Shanghai` is rendered as `8/13 13:25`; an already offset-aware `2026-08-13T13:25:00+08:00` is rendered at the same local time without a second conversion. No ISO substring extraction or hard-coded `+8 hours` conversion is used. An explicit resource timezone wins; otherwise the browser/conversation display timezone is used, with `Asia/Shanghai` only as the centralized environment fallback.

Technical tool details use the same formatter, so the folded view cannot reintroduce the UTC display bug. The client also sends the resolved display timezone with a new Agent request, keeping relative-time interpretation and result presentation aligned.

### Focused validation

- `zhiguang-fe/tests/agent_projection.test.ts` covers two Drafts/two Schedules with interleaved completion order, explicit Draft-Schedule binding, partial schedule failure, missing failed Schedule artifact, mixed approval plus success, UTC `Z` conversion, and offset-aware conversion without double shifting.
- `tests/e2e/test_execution_presentation.py` verifies Schedule `draft_id` survives the user-facing presenter.
- `tests/unit/test_phase17c_result_projection.py` verifies the persisted body-free artifact projection retains the Schedule-to-Draft relation.
- `pytest -q tests/e2e/test_execution_presentation.py tests/unit/test_phase17c_result_projection.py` — 10 passed.
- `npm run test:agent-ux` — passed after the multi-goal and timezone regression cases.

## Dynamic Multi-Goal Result Projection

### Goal cardinality source

目标数量现在只来自已验证的 `GoalTree` / `GoalCompiler` / `TaskPlan` 结果。`GoalTree.executable_goals()` 产生的业务叶目标被编译成带有稳定 `PlanStep.goal_id` 的步骤；该字段沿 `ExecutionStepInput`、`RuntimeResult.steps`、`AgentResponse.steps` 和前端 execution projection 只读透传。前端没有解析用户文本、主题或“一篇/另一篇”来计算数量。

父级 composite goal 只作为树容器，不单独渲染；同一个 Goal 的多个步骤通过相同 `goal_id` 合并为一个用户结果。因而 cardinality 是：

```text
GoalTree executable goals = N
        ↓
distinct projected step.goal_id = N
        ↓
UserFacingGoalResult items = N
```

### Root cause and ownership

执行层原本已经能保留多个 Goal，但进入用户界面时，`projectArtifactGroups` 以 Draft artifact 为起点，随后只用 Draft/Schedule 关系拼卡片。没有 Draft 的查询、分析、控制目标，或没有 artifact 的失败/等待目标，因此会从 Chat 消失；单结果 `find`/latest 式投影也无法表达异构目标。

现在的 ownership 优先级是：

```text
GoalTree / PlanStep.goal_id
        ↓
ExecutionStepInput.goal_id
        ↓
Runtime result step.goal_id
        ↓
Goal-aware result group
        ↓
artifacts attached by goal/step/task relation
```

Schedule 仍通过真实 `draft_id` 关联 Draft；artifact 的数组顺序、标题、主题和“第一个/第二个”都不参与配对。旧的无 `goal_id` 响应仅保留显式 task/goal/step-scope 的兼容回退，不扩大为文本猜测。

### Projection algorithm

`build` 逻辑现在是列表模型而非固定槽位：

- 先为每个 distinct executable `goal_id` 建立组，并收集该 Goal 的全部步骤；
- 再把 Draft、Schedule、Post、Search、Analysis 等已知业务 artifact 按 goal、step、task 或 `draft_id` 归属挂入；
- 没有 artifact 的 Goal 仍根据其步骤生成 `GENERIC_RESULT`、失败或需要确认的结果项；
- 一个 Goal 的多个 artifact/revision 仍只生成一个逻辑结果，默认呈现最新业务事实；
- 一个 Goal 失败、部分完成或等待审批时，其他 Goal 的结果不被顶层状态吞掉；
- 只有一个 item 时继续使用原有单结果体验，超过一个 item 才渲染 `RESULT_GROUP.items`。

因此 1、2、3、5 个 Goal 都走同一条 `map`/分组路径，Goal 类型可以混合，状态也可以独立。聚合状态只负责摘要：全部成功为 `SUCCESS`，混合完成/失败为 `PARTIAL_SUCCESS`，存在待确认项为 `NEEDS_ACTION`，全部失败才为 `FAILED`。

### Replan and technical details

技术字段仍留在折叠详情中；默认 Chat 不展示 `goal_id`、Task ID、Execution ID 或 Step ID。重新规划产生的新步骤只要沿用已有逻辑 Goal 关系，就会继续归入同一结果项；历史 revision 不会因为 artifact 数量增加而制造重复业务卡片。

### Tests and evidence

- `zhiguang-fe/tests/agent_projection.test.ts` 新增 1 / 2 / 3 / 5 Goal fixture。Goal ID 使用无序的 `g-17`、`g-04`、`g-92` 等稳定标识，覆盖异构查询/内容/控制、无 artifact、部分失败、待确认和失败目标，并断言结果项数量与独立状态。
- `tests/unit/test_execution_input_contract.py` 验证 `PlanStep.goal_id → ExecutionStepInput.goal_id → rebuilt PlanStep.goal_id`，并用五个叶 Goal 验证动态 cardinality 不被压缩。
- `pytest -q tests/e2e/test_execution_presentation.py tests/unit/test_phase17c_result_projection.py tests/unit/test_execution_input_contract.py tests/unit/test_goal_decomposer.py` — 25 passed。
- `npm run test:agent-ux` — passed；`npm run test:execution` — passed；`npm run lint` — passed；`npm run build` — passed。

本轮未修改 `GoalDecomposer`、`AgentLoop`、`DynamicPlanner` 或执行状态机；修复限于既有 Goal identity 的执行结果透传和 UserFacingProjection。未进行带真实服务数据的在线多目标请求，因此线上 `conversation_id/run_id/execution_id` 映射仍应在部署环境用一次 3+ Goal 请求复核，但本地契约与投影回归已覆盖 cardinality、ownership 和 completion-order independence。

## Dynamic Multi-Goal Semantic Isolation

### Root cause

The first semantic divergence was in `GoalCompiler._constraints_for_goal`.
Before this fix it merged request-level `Command.parameters`, `entities`, and
`constraints` into every Goal. A multi-goal Command containing one `run_at` or
publication mode therefore made that value look like a fact of every Goal.
The schedule argument binder also parsed the whole execution request as a
fallback for every schedule step. Finally, durable `StepExecution` and the
approval envelope did not consistently carry the owning Goal and target.

This was semantic state bleed, not an Agent Activity or ResultGroup wording
problem. `GoalCompiler` did not intentionally convert `SCHEDULE_PUBLISH` to
`PUBLISH_NOW`; an upstream or leaked capability could still reach the
immediate-publish policy gate, which explains the misleading approval card.

The follow-up plan audit found a second concrete boundary issue: a partial
LLM `TaskNode` hint could contain `SCHEDULE_PUBLISH` while omitting its
preceding `GENERATE_CONTENT` node. The old completion order then materialized
the missing generator after the schedule node, leaving the schedule with no
owned Draft dependency. That was an unsafe opportunity for a later runtime
to resolve a request-global active/latest draft.

### Semantic ownership audit

| Layer | Expected | Actual before | Actual after |
| --- | --- | --- | --- |
| Command | Aggregate capabilities only; per-target facts remain scoped | Request-wide time/publication fields were available for reuse | Multi-goal compilation ignores non-shared global target/action/time fields |
| GoalTree | Each user-visible Goal owns target, operation, time, publication intent | The Goal model had no typed fields for these facts | `semantic_operation`, `target`, `temporal_constraint`, and `publication_intent` are preserved and included in the structured decomposition contract |
| GoalCompiler | One independent semantic plan per Goal | Global `run_at` and publication fields could leak into sibling Goals; a partial schedule hint could precede its missing generator | Goal-owned values plus step-specific `TaskNode.inputs` compile into each PlanStep; capability order and explicit Draft dependency are restored deterministically |
| ExecutionInput / StepExecution | Preserve Goal ownership across the queue/process boundary | `PlanStep.goal_id` was not durable on `StepExecution` | `goal_id` is copied through input, worker reconstruction, replan, invocation context, and checkpoint persistence |
| Temporal binding | Each schedule step binds its own instant | Binder could parse the whole request/first goal for every step | A step’s own `run_at` is authoritative; request-text fallback is legacy single-step only |
| Tool selection | Capability/action comes from the current Goal | A leaked capability could reach the immediate publication gate | Schedule time and a scoped Draft relationship are required; missing time or an unowned multi-goal publication step fails closed and never falls back to immediate publication |
| Approval | Goal + step + target + real operation are auditable | Approval could use the first global Draft and an execution-only context | Approval payload includes Goal, Task, Step, operation, resource, target title, run_at, and timezone; reconciliation uses the same scoped arguments |

### Semantic invariants

For a generic three-goal fixture:

```text
CREATE + SCHEDULE(T1)
CREATE + SCHEDULE(T2)
CREATE + DRAFT_ONLY
```

the compiled plan now contains exactly three create steps, two schedule
steps, zero `PUBLISH_NOW` steps, and no schedule argument on the draft-only
Goal. Reordering the Goals does not change this mapping. A schedule without a
time is rejected by `PlanValidator` with `SCHEDULE_TIME_REQUIRED`; it is not
normalized into an immediate publication. A `DRAFT_ONLY` Goal containing a
publication capability is rejected before execution. In a multi-goal plan,
`SCHEDULE_PUBLISH` and `PUBLISH_NOW` must also carry an explicit `draft_id`
or an explicit upstream Draft dependency. The latter may span leaf-goal
boundaries only when the TaskPlan itself declares that relationship; the
runtime never substitutes a session-global latest Draft.

### Tests

Added `tests/unit/test_multi_goal_semantic_isolation.py` covering independent
three-goal semantics, reordered completion/ownership, explicit step inputs,
durable Goal propagation, draft-only protection, missing schedule time,
step-local relative/absolute time binding, partial TaskNode completion,
multi-goal Draft ownership, and scoped approval metadata. The frontend
projection test additionally verifies that `publication.schedule` is shown as
planned publication and that an approval with no resolved title uses the safe
`待确认草稿` fallback rather than `这篇内容`.

Focused results:

```text
pytest -q tests/unit/test_multi_goal_semantic_isolation.py \
  tests/unit/test_goal_decomposer.py tests/unit/test_argument_binder.py \
  tests/unit/test_execution_input_contract.py tests/unit/test_plan_validation.py
47 passed

pytest -q tests/unit/test_execution_worker.py tests/unit/test_capability_executor.py \
  tests/unit/test_tool_policy_gate.py tests/unit/test_dynamic_planner.py \
  tests/unit/test_execution_persistence.py
38 passed

npm run lint
npm run build
npm run test:execution
npm run test:agent-ux
all passed
```

### Real environment evidence and remaining limitation

The local Agent API and Java health endpoints responded with HTTP 200. The
Creator Service was unavailable locally, and the repository has no configured `GREENBOOK_E2E_IDENTIFIER` or
`GREENBOOK_E2E_PASSWORD`, so an authenticated live three-goal conversation
could not be submitted without inventing credentials or bypassing the real
authentication boundary. The deterministic GoalTree/TaskPlan/ExecutionInput
path above is the reproducible evidence for the fix; the deployed environment
still needs one authenticated request to record its `conversation_id`, `run_id`,
and `execution_id` and verify the exact production LLM decomposition.

## Live Multi-Goal Cardinality Regression

### Root cause

The real failed execution `f03971ef-7bdc-49fe-9d17-774b654a247c` was read
directly from the durable execution queue. Its persisted GoalTree already had
three executable leaves with the correct independent semantics:

```text
post_redis             CREATE + SCHEDULE_PUBLISH, relative +20 minutes
post_agent_memory      CREATE + SCHEDULE_PUBLISH, tomorrow 15:00 +08:00
post_java_concurrency  CREATE + DRAFT_ONLY
```

The first cardinality collapse was therefore not in the real LLM
GoalDecomposer or GoalTree filtering. The LLM selected a single side-effecting
`TOOL_CALL`, and `ConversationRuntimeAdapter.submit_tool` turned that action
into the one-step `AGENT_TOOL_SUBMISSION` plan:

```text
GoalTree.executable_goals() = 3
TOOL_CALL -> submit_tool -> TaskPlan.steps = 1
ExecutionInput.steps = 1
Persisted StepExecution = 1 (agent-tool-4, goal_id = null)
```

`GoalCompiler` was bypassed completely, so its existing deterministic
completion of every executable Goal never ran. Earlier multi-goal success was
therefore contingent on the model choosing `CREATE_TASK`; it was not protected
against a valid-looking single `TOOL_CALL` action.

### Fix

- `AgentLoop` now upgrades a selected side-effecting, approval-required, or
  destructive `TOOL_CALL` to `CREATE_TASK` internally when the current
  `GoalTree` has more than one executable leaf. Read-only observations remain
  on the normal in-loop tool path.
- The upgrade submits the complete `GoalCompiler` plan, preserving every
  `PlanStep.goal_id`, capability, dependency, and Goal-local constraint.
- `ConversationRuntimeAdapter` now checks Goal coverage immediately before
  `RuntimeAgentService.submit_plan`. If any executable Goal is missing from
  the PlanStep goal IDs, it raises `PLAN_GOAL_COVERAGE_REQUIRED` and no
  Execution is created.
- The remaining single-goal direct-tool path writes its sole leaf `goal_id`
  into the generated PlanStep, so the guard does not regress one-goal work.
- `GoalCompiler.compile_plan` has the same final coverage invariant as a
  compiler-level defense.

No frontend component, result-group renderer, topic rule, numeric parser, or
GoalDecomposer prompt was changed for this regression.

### Cardinality trace

| Layer | Actual before | Actual after |
| --- | --- | --- |
| Real LLM GoalTree | 3 executable Goals | unchanged: 3 executable Goals |
| Agent action | one `TOOL_CALL` | same action is safely promoted for multi-goal side effects |
| TaskPlan | 1 direct tool step, no Goal ID | complete GoalCompiler plan, all executable Goal IDs covered |
| ExecutionInput | 1 step | receives every compiled plan step |
| Persisted execution | 1 `GENERATE_CONTENT` step | cannot be created when Goal coverage is incomplete |

The exact real persisted 3-goal tree was replayed read-only through the fixed
AgentLoop branch. It produced:

```text
GoalTree executable: 3
TaskPlan steps: 5
ExecutionInput steps: 5

post_redis             GENERATE_CONTENT, SCHEDULE_PUBLISH
post_agent_memory      GENERATE_CONTENT, SCHEDULE_PUBLISH
post_java_concurrency  GENERATE_CONTENT
```

This validates the requested semantic matrix: three content-generation steps,
two schedule steps, and zero immediate-publication steps.

### Regression tests

- `tests/unit/test_tool_policy_gate.py` verifies a single side-effecting Goal
  keeps its direct submission path, while both two- and three-goal trees with
  the same LLM `TOOL_CALL` action submit one full GoalCompiler plan instead.
- `tests/unit/test_phase16a_runtime_composition.py` verifies an incomplete
  multi-goal plan is rejected with `PLAN_GOAL_COVERAGE_REQUIRED` before the
  Runtime submission boundary.
- `tests/unit/test_multi_goal_semantic_isolation.py` now explicitly verifies
  that a partial LLM TaskNode hint for one of three Goals still compiles all
  three Goal IDs and five business steps.

Focused verification:

```text
pytest -q tests/unit/test_agent_loop.py tests/unit/test_tool_policy_gate.py \
  tests/unit/test_goal_decomposer.py tests/unit/test_multi_goal_semantic_isolation.py \
  tests/unit/test_execution_input_contract.py tests/unit/test_plan_validation.py \
  tests/unit/test_phase16a_runtime_composition.py
70 passed

python -m ruff check [affected backend files]
All checks passed
```

Historical real LLM GoalTrees from durable executions were also replayed
without making any external mutation:

```text
1 Goal -> direct single-tool branch
2 Goals -> full plan, coverage 2/2
3 Goals -> full plan, coverage 3/3 and 5 steps for the real regression tree
```

### Remaining live verification boundary

The local Agent API is healthy, but no E2E access token or test-account
credentials are configured, and the local Creator Service is unavailable.
Consequently no new authenticated conversation was submitted from this
workspace. The evidence above uses real durable LLM decompositions and the
exact failed 3-goal GoalTree, replayed through the patched runtime boundary;
one fresh authenticated 2-goal and 3-goal conversation should be recorded in
the deployed environment to complete external-service verification.
