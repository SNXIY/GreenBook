# Phase15-C Natural Language Task Decomposition Audit

日期：2026-08-10

## 当前缺口

Phase15-B 已能处理显式序号、少量句式拆分和独立 Task dispatch，但原链路仍有明显边界：

1. `TaskDecomposer` 是确定性文本切分器，历史正则主要面向显式标记，不能可靠判断“分析结果供后续写作使用”这类隐式依赖。
2. `IntentSpecProvider` 原先一次只返回一个 `IntentSpec`；`COMPOSITE` 只能表达同一 Task 内的多个动作，不能表达同一 Conversation 的多个业务目标。
3. `TaskProvider` 能创建/解析 Task，但不负责从一轮自然语言中生成 Goal DAG。
4. Planner/Orchestrator 已能为一个 Task 生成 DAG，却没有 Conversation-level graph 输入。
5. Query 已禁止创建 Execution，但缺少真正的只读 ToolRuntime handler 和 Query artifact 输出。

## Phase15-C Graph 设计

```text
Conversation
    |
    v
TaskGraphBuilder
    |
    +-- GoalNode A: QUERY / ANALYZE
    |       |
    |       +-- QUERY_RESULT artifact
    |               |
    +---------------v
    +-- GoalNode B: CREATE / PUBLISH -> Task -> existing Planner -> Queue
    |
    +-- GoalNode C: independent CREATE -> Task -> existing Planner -> Queue
```

`ConversationGoalNode` 包含：

- `IntentSpec`
- semantic `goal`
- `depends_on`
- `read_only`
- `create_task`
- artifact input/output handles

`ConversationTaskGraph` 只负责图结构和环检测。每个 ACTION Goal 仍通过现有
`TaskProvider`、`IntentCompiler`、`RuntimeAgentService` 生成独立 Execution；没有改变
Queue、Worker、Persistence、ToolRuntime 基础架构。

## 语义边界策略

`TaskGraphBuilder` 不根据“然后”“另外”等连接词决定边界。优先调用
`IntentSpecProvider.resolve_graph()`，由语义模型判断：

- 是否是一个可交付物的连续动作；
- 是否是多个独立业务目标；
- 哪个 Goal 依赖哪个 Goal；
- 哪些 Goal 是只读 Query；
- 哪些 Goal 需要创建 Task。

旧的显式 Task splitter 仅作为兼容 fallback，确保 Phase15-B 的“第一/第二”输入不回退。

## QueryHandler

新增 `QueryHandler` 抽象和 `ReadOnlyQueryHandler`：

- 只允许 QUERY/SEARCH/ANALYZE；
- 通过现有 `community.search_public_posts` 读取社区数据；
- 支持 search/analyze/list 语义操作映射到只读数据源；
- 生成 `QUERY_RESULT` ArtifactRef；
- CREATE/UPDATE/DELETE/PUBLISH/UPDATE_OR_CREATE 会被拒绝；
- 不创建 Task，不创建 Execution。

后续若 MCP 提供专用 `community.search_posts`、`community.analyze_posts`、
`community.list_posts` 工具，只需替换 QueryHandler 映射，不需要改变 Runtime 图协议。

## 复杂案例

已覆盖：

- Java 帖子分析 -> 学习指南生成（A -> B）+ Redis 独立目标（C）；
- UPDATE 与 QUERY 同轮共存的图分类；
- Query Artifact 传入依赖 Goal；
- 多候选目标必须保持 clarification，而不是猜测。

## 当前仍存在的缺口

1. 无 LLM/Graph provider 时只能保持单 Task 或走 Phase15-B 显式兼容 splitter；不会伪造隐式依赖。
2. 当前 MCP 实际已有的公共只读入口是 `community.search_public_posts`，专用分析/list 工具仍需外部工具契约后续补齐；本阶段没有修改 ToolRuntime。
3. Query 结果的自然语言总结仍由上层 presentation/QueryAgent 负责，Handler 只保证真实只读数据和 Artifact handle。
