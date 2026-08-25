# GreenBook Frontend UX Upgrade — Agent Conversation Experience

## Scope

本阶段只调整 `zhiguang-fe` 的 Agent 对话层，没有修改 Agent Runtime、Execution Worker、Tool Runtime、MCP、Java Backend 或 Creator Service 的核心执行语义。

实现路径为：

```text
现有 Agent API / Execution projection
        ↓
UserFacingResult / AgentActivity projection
        ↓
Agent Result Cards / Activity / Approval UI
```

## Before

对于“明天上午八点发布一篇关于如何学好 Java 的帖子”这类请求，主对话区原先会把类似下面的内容放在结果主视觉中：

```text
Execution accepted by the durable queue.
Runtime 执行 · 已完成
任务已完成，结果已保存。
2/2 步
生成内容已完成
安排发布已完成
```

原实现把 Agent 消息、Execution 进度、步骤状态、审批状态和业务结果放在同一层展示。草稿和发布时间虽然可以从投影中取得，但没有形成以业务结果为中心的卡片，也没有根据结果类型切换 UI。

## Problems

- 普通用户会看到 `Execution`、`durable queue`、Runtime 状态和步骤计数等内部概念。
- 文章标题、草稿预览、发布时间和下一步操作不够突出。
- 执行过程和最终结果混在一张通用状态卡里，完成后仍占据主视觉。
- 草稿、定时发布、搜索结果、分析结果、审批和失败没有独立的信息结构。
- 原始助手消息仍可能带有内部执行文案，因此增加了前端消息层的技术文案降级处理。
- 多执行任务的详情表达过重，且原有实现存在折叠层级不清的问题。

## New UX Model

### UserFacingResult

新增 `src/components/agent/userFacingResult.ts`，将现有 `AgentExecutionResultPart` 和真实 artifact 字段投影为用户结果：

```typescript
type UserFacingResultType =
  | "DRAFT_CREATED"
  | "CONTENT_REVISED"
  | "SCHEDULED_POST"
  | "PUBLISHED_POST"
  | "SEARCH_RESULTS"
  | "ANALYTICS_RESULT"
  | "APPROVAL_REQUIRED"
  | "TASK_FAILED"
  | "GENERIC_RESULT";
```

投影字段包括：

- `title`、`summary`、`resourceId`
- `draft`：草稿 ID、标题、真实内容摘要
- `schedule`：定时 ID、草稿 ID、真实发布时间、时区、状态
- `post`：已发布帖子 ID和标题
- `search`：真实结果数量和帖子列表
- `analytics`：只展示 API 实际返回的统计字段
- `actions`：链接、继续输入或调用 Agent API 的操作
- `activity`：面向用户的轻量步骤文案
- `technical`：Execution ID、Task ID、步骤状态等详情数据

`SEARCH_RESULT` 使用 artifact payload 中的 `items`、`total` 等真实字段；`ANALYSIS_REPORT` 使用实际返回的发布、浏览、点赞、评论、收藏、分享、粉丝和关注字段。缺失字段不会补造统计数字。

### Message sanitization

新增 `userFacingMessage`，在主对话渲染助手普通文本前过滤 `durable queue`、`ExecutionInput`、`TaskPlan`、`MCP`、`ToolRuntime`、`StepExecution` 等内部文案。内部消息根据上下文降级为“已收到请求，正在准备内容…”、“需要你的确认…”或“这次没有完成…”。原始技术字段只在主动展开的详情区域保留。

### Three-level hierarchy

1. **业务结果**：标题、草稿、发布时间、搜索列表、统计结果和可执行按钮。
2. **轻量 Activity**：例如“正在生成文章…”、“内容已生成”、“已安排发布时间”。
3. **执行详情**：默认折叠，展开后显示 ID、步骤、状态和任务中心入口。

## Components

### 新增

- `src/components/agent/userFacingResult.ts`
  - Runtime projection 到 UserFacingResult 的映射。
  - Execution、Run 的 Activity 映射。
  - Approval 的用户文案和风险后果映射。
- `src/components/agent/AgentResultCards.tsx`
  - `DraftCard` 形态：草稿标题、预览、查看和继续修改。
  - `ScheduleCard` 形态：发布时间、时区、查看草稿、修改时间、取消定时。
  - Search result list。
  - Analytics metric grid。
  - Failure result card。
  - 默认折叠的执行详情。
- `src/components/agent/AgentResultCards.module.css`
  - 结果优先的 GreenBook 卡片样式、按钮反馈和 reduced-motion 处理。
- `src/components/agent/AgentActivityCards.tsx`
  - 单个 Execution 的轻量 Activity。
  - 多 Execution 的简化 Activity 列表。
  - 兼容无 Execution projection 的 Run Activity。
  - 用户确认卡片。
- `src/components/agent/AgentActivityCards.module.css`
  - Activity、审批风险提示、详情折叠和移动端布局。

### 修改

- `src/components/agent/AgentPanel.tsx`
  - 主消息有 execution result 时不再直接展示原始内部结果文本。
  - 完成的 Execution 在对应结果消息出现后从主区域移除，避免重复显示进度卡。
  - 运行中只展示轻量 Activity；Execution 技术信息迁移到“查看技术详情”。
  - 审批改为目标感知的“需要你的确认”卡片。
  - 保留现有 pause、resume、retry、cancel、approval decision、Agent message API 和任务中心链接。
  - 重新打开对话时恢复待确认或兼容 Run 状态。

## Result Mapping

| 真实 projection | User-facing type / UI | 默认展示 | 真实操作 |
| --- | --- | --- | --- |
| `POST_DRAFT` / `DRAFT` / `CONTENT_DRAFT` | `DRAFT_CREATED` 或 `CONTENT_REVISED` / Draft card | 标题、内容摘要、草稿已保存 | `/create/manual?draftId=...`；继续修改；安排发布 |
| `PUBLICATION_SCHEDULE` / `SCHEDULE` + draft | `SCHEDULED_POST` / Schedule card | 标题、日期、时间、时区、自动发布说明 | 继续使用 Agent message API 修改时间或取消定时；查看草稿 |
| `PUBLISHED_POST` / `POST` / `PUBLICATION` | `PUBLISHED_POST` / Published result | 已发布、帖子标题 | `/post/:id`；通过 Agent message API 请求分析 |
| `SEARCH_RESULT` | `SEARCH_RESULTS` / Search result card | 真实数量和前五条结果 | 通过 Agent message API 请求查看全部 |
| `ANALYSIS_REPORT` / `PERFORMANCE_DATA` | `ANALYTICS_RESULT` / Analytics card | 实际返回的指标和重点 | 通过 Agent message API 请求详细分析 |
| `AgentRun.approval` / `WAITING_APPROVAL` / `WAITING_HUMAN` | `APPROVAL_REQUIRED` / Approval card | 即将执行的动作、目标资源、影响和风险 | 现有 `decideApproval` API；拒绝或返回修改 |
| Execution `FAILED` | `TASK_FAILED` / Failure card | “这次没有完成”、已有内容不被覆盖、重试和继续修改 | 现有 retry / Agent message API |
| 无可识别 artifact 的完成结果 | `GENERIC_RESULT` | 简短完成说明和下一步 | 任务中心详情 |

技术映射仍保留在 `technical` 和详情组件中，但不会在默认结果标题、摘要或按钮中显示 UUID、状态枚举、工具参数或内部队列术语。

## Screens / Flows

### Case 1 — 查询最近的帖子

```text
找到 N 篇相关内容

1. 帖子标题
2. 帖子标题
3. 帖子标题

[查看全部]
```

数量取自 `SEARCH_RESULT` 的 `total` 或真实 items 数量；没有结果时不显示虚构数量。

### Case 2 — 写一篇 Java 文章

```text
草稿已完成

《如何学好 Java》
Java 学习不能只停留在……

[查看草稿] [继续修改] [安排发布]

查看技术详情 >
```

结果区默认只展示草稿业务信息，Execution ID、Task ID 和步骤状态在折叠区。

### Case 3 — 明天上午八点发布 Java 文章

```text
已安排发布

《如何学好 Java：从基础到实战的学习路线》

8月14日 08:00
中国标准时间
帖子已经生成并保存为草稿，到时间后会自动发布。

[查看草稿] [继续修改] [修改时间] [取消定时]

✓ 内容已生成 · ✓ 已安排发布时间
查看技术详情 >
```

卡片由 draft artifact 和 schedule artifact 组合得到，不依赖 Runtime 新字段。

### Case 4 — 立即发布

```text
需要你的确认

准备立即发布
《Java Agent 实战指南》
发布后，社区用户将可以看到这篇内容。

[返回修改] [拒绝] [确认发布]
```

确认和拒绝仍调用现有 approval endpoint；主卡片隐藏 `WAITING_APPROVAL`、approval ID 和 run version，详情展开后才可查看。

### Case 5 — 执行失败

```text
这次没有完成

已有内容不会被本次失败覆盖。

[重试] [继续修改]

查看技术详情 >
```

错误码、工具错误和外部服务诊断不进入普通用户主视觉，只保留在详情数据或任务中心。

## Tests

在 `zhiguang-fe` 执行：

- `npm run lint` — 通过。
- `npm run build` — 通过，Vite 生产构建完成。构建输出的 `baseline-browser-mapping` / Browserslist 数据过期提示为仓库依赖提示，不影响构建。
- `npm run test:execution` — 通过，现有 Execution API contract test 成功。

本阶段没有修改后端 contract，因此没有运行 Python / Java 全量测试集。仓库当前没有 Agent 对话的浏览器端到端测试脚手架；上述五个场景已按真实 Agent DTO、artifact projection、approval API 和现有路由完成代码路径核对。

## Remaining UX Debt

- 仍需要在保持 `CERTIFIED` 认证环境中补充真实浏览器回归，逐一点击五个场景的按钮并确认后端状态变化。
- 当前前端已有取消定时 endpoint，但没有独立的“修改定时时间” endpoint；“修改时间”和部分结果操作继续通过现有 Agent message API 完成，后续可以在已有 API 稳定后增加更直接的 action binding。
- 搜索卡片默认展示前五条，分页和筛选仍由后续“查看全部”流程处理。
- 分析卡片只展示已有字段；如果后端未来新增指标，需要在 projection alias 中显式加入，避免未经确认的统计展示。
- 任务中心仍保留完整的 Execution、Task、Step、Artifact、Approval 和 Timeline 技术视图；本阶段只把它们从主对话默认视觉层移到详情入口，没有重做任务中心。

## UserFacingProjection Hardening

### Root causes

- **Leak source 1 — raw assistant message**：`AgentPanel` 原先在没有强制经过投影的情况下直接把 `AgentMessage.content` 交给 `AgentMarkdown`，因此 `durable queue`、Runtime 和 Creator instruction 可以进入对话。
- **Leak source 2 — execution state retained beside result**：结果消息出现后，`execution` / `executions` 仍保留完成快照，Activity 卡和结果卡同时渲染，造成“这次没有完成”与另一个“已完成”状态并存。
- **Leak source 3 — secondary Agent surface bypassed the projection**：`CommentSection` 直接渲染 `Execution`、`AgentRun.steps` 和 approval description，绕过了用户 Activity 文案。
- **Leak source 4 — projection race**：执行终态先于业务结果消息落库；旧的短轮询超时后直接把 `FAILED` Execution 快照渲染成最终失败卡。旧逻辑还会把任意新增 assistant 消息（例如“已收到请求”）误认为 execution result，导致结果卡与执行卡之间出现短暂状态冲突。
- **Task Center boundary**：`TaskCenterPage` 的 Goal、Plan、Step 和 Runtime 详情属于 `/tasks` 技术工作台，仍保留在那里；它不再由 Agent Chat 组件嵌入。

### Leaks removed

- 新增内部 Runtime / prompt 文案过滤，原始助手消息会转为简短的业务状态提示。
- `AgentPanel` 在结果 projection 成功后清空已完成 Execution state，避免结果卡与 Activity 卡重复。
- `CommentSection` 改用 `projectExecutionActivity`、`projectRunActivity` 和 `approvalPresentation`，不再显示 Runtime、进度百分比、Execution ID、事件数量或原始步骤/审批文本。
- Draft、Schedule、Search、Analytics、Approval 和 Failure 的默认内容只来自 artifact、schedule、activity 和 action projection；不再 fallback 到 raw goal / task description / execution summary。
- 所有主对话技术入口统一为“查看技术详情”。技术字段仅在用户打开 disclosure 后挂载到 DOM；默认折叠状态不会渲染 Execution ID、Task ID、Run ID 或事件明细。
- 结果同步期间不再展示终态失败墙；前端会按 execution id 等待对应的 `UserFacingResult`，并在业务结果消息到达后自动替换临时 Activity，不需要刷新助手。

### Status projection

`UserFacingResult.status` 现在只使用四种 Chat 顶层状态：

```typescript
type UserFacingStatus =
  | "SUCCESS"
  | "PARTIAL_SUCCESS"
  | "NEEDS_ACTION"
  | "FAILED";
```

当 Execution 总状态为失败但已有草稿 artifact，或发布时间 artifact 失败而草稿仍然存在时，映射为 `PARTIAL_SUCCESS`，而不是把整个业务目标标记为失败。

### Business progress

业务 Activity 只展示 `内容已生成`、`未能安排发布时间` 等用户步骤；Runtime 的 `progress`、`completed_steps / total_steps`、事件数量和内部 step id 仅保留在技术详情。

对于“内容成功、定时失败”的真实路径，结果卡现在使用：

```text
帖子已经准备好了，但发布安排没有成功
草稿已经保存，你的内容不会丢失。

✓ 内容已生成
✕ 未能安排发布时间

[重新安排发布] [继续编辑] [查看草稿]
```

在业务结果消息尚未到达的短暂窗口，Chat 只显示“正在整理结果… / 正在确认草稿和发布时间”，不会把 Runtime 的失败快照当成最终结论。

### Technical details

`AgentResultCards`、`AgentActivityCards`、审批卡和工具详情都使用受控 disclosure。默认状态只渲染“查看技术详情”入口；用户打开后才挂载 Execution ID、Task ID、Run ID、步骤、事件和任务中心链接。`/tasks` 页面仍然保留完整 Task / Execution / Goal / Plan / Timeline 能力。

### Tests

新增 `tests/agent_projection.test.ts` 和 `npm run test:agent-ux`，覆盖：

- draft success + schedule failure → `PARTIAL_SUCCESS`、业务化标题和“重新安排发布 / 继续编辑 / 查看草稿”。
- 内部 Creator prompt 过滤。
- 默认用户字段不包含 Execution/Task/Plan ID、Runtime 进度或 `g3:1`，而技术 projection 仍保留可展开所需 ID。
- 业务 Activity 只显示业务步骤，不显示 Runtime `2/4` 或 `50%`。
- 即使调度失败时暂时没有 Schedule artifact，只要草稿已生成，仍映射为 `PARTIAL_SUCCESS` 并提供“重新安排发布 / 继续编辑 / 查看草稿”。
- 结果 projection 延迟到达时按对应 execution id 自动对账，覆盖“刷新后才恢复”的竞态路径。

### Real case result

对“明天上午八点发布一篇关于如何学好 Java 的帖子”，以及“五分钟之后发布一条关于如何学习 Redis 高并发的帖子”这类调度路径，主 Chat 会先显示轻量的结果整理状态，随后自动切换到草稿 / 定时发布结果卡；即使调度失败，也会保留草稿并进入 `PARTIAL_SUCCESS`。不再需要刷新助手。`Agent任务`、`g3:1`、Runtime 执行标题、Execution ID、Task ID、Plan ID、`2/4`、Creator raw prompt 不再进入 Chat 默认展示；只有主动打开“查看技术详情”才会看到技术字段。后端 Runtime、调度语义和现有审批/任务接口没有修改。
