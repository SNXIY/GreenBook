# Memory V2 Architecture Checkpoint

审计日期：2026-08-26  
审计基线：18524e603cf02591dbb4143021f75cc1b7ca0bb8（短 hash：18524e6）  
审计性质：只读架构审计与 Episodic Memory 设计提案  
本阶段：未修改生产代码、未新增表/Schema/Vector Collection、未运行测试、未 commit、未 push。

## 1. 执行结论

当前代码存在一个清晰的 V2 主链：

用户已完成的 Turn
→ PreferenceMemoryService
→ PreferenceMemoryExtractor
→ MemoryManager
→ MemoryRepository
→ PreferenceRetriever
→ MemoryRelevanceGate
→ ContextBuilder
→ Interpreter 的安全上下文投影。

这条主链承担的是用户长期、可复用的 Preference Memory。ContextBuilder 将它作为有界的证据投影，Interpreter 可以参考它，但它不拥有 Task、Execution、Resource 或 Approval 的状态写权限，也不能直接选择执行目标。

但当前仓库还不能被描述为“Memory 实际上只存 Preference”。代码中仍保留一条旧的通用 Memory 运行时链：

- RuntimeAgentService._record_episodic 会从执行结果写入 EPISODIC 记录。
- RuntimeAgentService._record_procedural 会从执行结果写入 PROCEDURAL 记录。
- RuntimeAgentService._recall_memories 会直接读取 SEMANTIC、EPISODIC，并通过 StrategyRetriever 读取 PROCEDURAL。
- 旧的 episodic 投影会暴露 goal、status、draft_id、schedule_id。

因此，当前最重要的架构结论是：

1. Preference V2 的主链已经具备明确的边界、租户隔离、相关性门控和 no-memory 结果。
2. Episodic/Procedural 的类型和旧 API 已存在，但它们不是一个已经冻结的、边界安全的 Episodic vertical slice。
3. 下一阶段不能直接再增加一条 Episode 写入/召回链，否则会产生 Task + Execution + Episode 三套事实描述。
4. 在实现 Episodic 之前，必须先把“当前状态”和“长期可复用经验”的所有权、字段和注入路径冻结下来。

## 2. 当前 Memory 数据模型

### 2.1 MemoryRecord

当前统一记录模型位于 packages/agent_core/greenbook_agent_core/memory/models.py，主要字段如下：

|字段|当前含义|边界判断|
|---|---|---|
|memory_id|Memory 记录身份|仅为 Memory 身份，不是 Task/Execution 身份|
|user_id、tenant_id|隔离范围|检索和写入必须同时按 user 与 tenant 作用域|
|memory_type|EPISODIC、SEMANTIC、PROCEDURAL|类型是长期知识分类，不应承担当前运行状态|
|PREFERENCE|兼容标签|当前序列化值仍是 SEMANTIC，不是独立存储类型|
|content|有界的自然语言摘要|应是偏好、事实、经验或策略摘要，不应是原始运行快照|
|structured_metadata|结构化证据和分类字段|必须区分语义字段与 provenance 字段|
|importance、confidence|重要性与置信度|参与排序和 relevance gate，不等同于业务成功状态|
|conversation_id|历史兼容字段|在 Preference contract 中表示 source conversation provenance|
|task_id|历史关联字段|只可作为来源关联，不能让 Memory 成为 Task 的状态所有者|
|source_type、source_id|写入来源和幂等来源|用于审计/去重，不应变成模型可执行的当前实体|
|status|ACTIVE、INACTIVE、SUPERSEDED|Memory 自己的生命周期，不是 Task 或 Execution 生命周期|
|created_at、updated_at、last_accessed_at、access_count、expires_at|Memory 生命周期和访问信息|用于留存、淘汰和审计|
|embedding|兼容读取字段|当前 canonical retriever 不生成、不使用 embedding；Postgres 当前表也没有可用向量检索链|

当前 MemoryType.PREFERENCE 是 MemoryType.SEMANTIC 的兼容别名，持久化值为 SEMANTIC。这对现有 Preference vertical slice 保持兼容，但会让未来“Preference”和“Semantic Fact”共用同一个存储值。实现更广义 Semantic Memory 前，必须先冻结 subtype/metadata 契约，不能只依赖字符串别名。

### 2.2 存储

当前存在两种 Repository：

- InMemoryMemoryRepository：测试和本地环境使用的进程内存储。
- PostgresMemoryRepository：生产使用的 agent_memories 持久化存储，按 user_id、tenant_id、status、memory_type 和 expiry 过滤，并支持 source 去重、touch 和生命周期更新。

生产 main.py 当前构造 MemoryManager(durable_repository=PostgresMemoryRepository)。因此：

- MemoryManager 的 primary repository 默认仍是 InMemoryMemoryRepository。
- durable repository 作为写入的持久化影子。
- 生产 PreferenceRetriever 直接使用 durable repository。

这形成了“写入主存储 + durable shadow + durable retrieval”的双存储拓扑。它能支持当前 vertical slice，但也带来短暂不一致、重启后的 primary 缓存差异以及旧 provider 读取不同数据源的风险。后续 Episodic 不能复制更多存储副本。

当前 agent_memories 表包含 tenant、source conversation、status、类型、内容、结构化 metadata、置信度、重要性、访问和过期字段。没有独立 episode table，也没有 vector collection；本次审计没有新增任何存储结构。

## 3. Memory 生命周期

### 3.1 Preference 生命周期

1. 用户消息完成，并且 RuntimeResult.status 为 COMPLETED。
2. API route 调用 PreferenceMemoryService.process_completed_turn。
3. PreferenceMemoryExtractor 只识别高置信度、带长期信号的有限词汇；一次性任务、时间性请求和普通发布请求会被跳过。
4. 生成带 user、tenant、source conversation、source hash 的 MemoryRecord。
5. MemoryManager.remember 执行 source 幂等和 Preference identity 合并。
6. 相同 preference key + value 的证据计数增加；相同 key 的新值会将旧 ACTIVE 记录标记为 SUPERSEDED。
7. Repository 保存；生产环境同步/异步持久化到 durable repository。
8. PreferenceRetriever 只读取 ACTIVE、同 user、同 tenant 的 Preference 记录。
9. 命中记录在需要时 touch，更新 access_count 和 last_accessed_at。
10. forget 删除记录；deactivate 或 supersede 保留审计记录但停止正常 recall；expires_at 过期后不参与查询。

### 3.2 当前代码中其他 Memory 类型的生命周期

旧的 MemoryManager.remember_execution、remember_pattern 和 remember_correction 已经可以生成 EPISODIC、PROCEDURAL 或 correction-derived record。它们的策略允许 TASK_COMPLETED、TASK_FAILED_MAJOR、USER_CORRECTION 和 REUSABLE_STRATEGY 等事件。

这些 API 说明当前 Memory domain 已预留长期经验/策略能力，但不代表 Episodic 边界已经完成。尤其是 remember_execution 以执行结果作为直接写入事实，并将执行状态、draft_id、schedule_id 放入 metadata；这与本 checkpoint 期望的“Episode 不是当前状态快照”存在冲突。

## 4. Memory 写入链路

### 4.1 当前 Preference V2 主链

链路：

    completed RuntimeResult
      -> routes._extract_completed_turn_preference
      -> PreferenceMemoryService
      -> PreferenceMemoryExtractor
      -> MemoryManager.remember
      -> source idempotency / preference merge / supersede
      -> InMemory primary + Postgres durable shadow

该链路的关键安全点：

- 只在完成的 Turn 边界抽取，不把每个中间执行步骤自动变成 Memory。
- 抽取器拒绝普通的一次性 task request。
- 记录同时带 user_id 与 tenant_id。
- source_id 使用 conversation + message hash，支持重试幂等。
- MemoryManager 负责同一 Preference key 的替换和 supersede。

### 4.2 旧的运行时写入链

链路：

    RuntimeAgentService terminal path
      -> _record_episodic
      -> MemoryManager.remember_execution
      -> EPISODIC record

    RuntimeAgentService terminal path
      -> _record_procedural
      -> ProceduralMemoryExtractor
      -> MemoryManager.remember
      -> PROCEDURAL record

这条链目前属于 legacy/dormant capability 与运行时遗留路径的混合体。它不是本次 Preference V2 的同一条 ContextBuilder 主链。它必须在 Episodic 实现前被明确归属、收敛或停用，否则新 Episode 会和旧执行记忆重复写入。

## 5. Memory 检索链路

### 5.1 生产 canonical Preference 检索

链路：

    ContextAssembler
      -> ContextBuilder
      -> PreferenceRetriever.retrieve
      -> tenant/user + ACTIVE + PREFERENCE(SEMANTIC) filter
      -> deterministic ranking
      -> MemoryRelevanceGate
      -> bounded selected memories / empty no-memory
      -> ContextSnapshot.user_preferences
         ContextSnapshot.recalled_memories

当前约束：

- PreferenceRetriever 缺少 user 或 tenant 时 fail closed。
- 默认最多返回 5 条 Preference。
- 先取得有界候选，再按 content、preference_type、value 计算 lexical relevance。
- 默认 relevance threshold 为 0.5，confidence threshold 为 0.5。
- 没有满足 gate 的候选时返回空列表，而不是把所有 Preference 注入。
- ContextBuilder 将 canonical retriever 的空结果视为显式 no-memory，不会被未过滤的 legacy preference provider 覆盖。
- ContextSnapshot 最多保留 5 条 memory evidence，并限制 content 和 metadata 长度。

### 5.2 Generic/legacy 检索

MemoryRetriever 提供通用的 command/goal/context 词项提取、排序、relevance gate 和 touch。它默认允许更低的 relevance threshold，并能处理多种 MemoryType。

RuntimeAgentService._recall_memories 不走 PreferenceRetriever，而是直接调用 MemoryManager.recall 读取 SEMANTIC 和 EPISODIC，并调用 StrategyRetriever 读取 PROCEDURAL。MemoryManager.recall 本身是 repository search + touch，不包含 Preference V2 的 relevance gate。因此当前仓库实际上有两套检索语义：

|检索路径|当前角色|边界风险|
|---|---|---|
|PreferenceRetriever|生产 ContextBuilder 的 V2 canonical Preference 路径|有 relevance gate、tenant scope、no-memory|
|MemoryManager.recall + StrategyRetriever|旧 RuntimeAgentService 路径|可能绕过 gate，把 execution history/status 作为 memory_context 注入|

这也是后续 Episode 不能直接“再加一个 retriever”而不做整合的原因。

## 6. Context 注入位置与 Interpreter 集成

ContextBuilder 是当前唯一的 Conversation、Task、Execution、Artifact、Resource、Memory 汇合点。它并不把 Memory 写入 Task 或 Execution，而是生成一次性的 ContextSnapshot。

当前注入过程：

1. ContextBuilder 加载 Conversation、当前可解析 Task、Objective、Artifact、Resource、Execution 和 ActionObservation 投影。
2. 同时调用 PreferenceRetriever 获取相关 Memory。
3. 将 Memory 压缩为 bounded evidence，放入 recalled_memories、user_preferences 和 memory_ids_used。
4. ContextAssembler 使用当前 user_input 作为 target_query，并生成任务范围内的工作集。
5. CommandInterpreter 调用 project_interpreter_context。
6. Interpreter 只接收去除 canonical ID 的安全投影，包括 user_preferences 和 recalled_memories。
7. Memory evidence 只能帮助 Interpreter 理解用户偏好和长期背景；目标解析、ResourceBinding 和执行授权仍由 deterministic resolver、Task/Objective、ActionLoop、Durable Runtime 和 Java 业务事实负责。

因此 ContextBuilder 的正确定位是：

    durable current-state owners + selected long-term evidence
                       -> bounded ContextSnapshot
                       -> sanitized Interpreter input

它不是 Memory store，也不是新的事实数据库。

## 7. 当前 Memory 边界

### 7.1 Memory 应承担的内容

当前架构意图允许 Memory 承担：

- 用户长期偏好。
- 用户稳定事实，但必须先经过明确的 durable fact 分类和置信度策略。
- 经过抽象、验证、具备复用价值的历史经验。
- 未来经过多次验证或明确确认的可复用策略。

这些内容必须是“可复用的长期证据”，而不是“某一次运行现在是什么状态”。

### 7.2 Memory 不应承担的内容

Memory 不应成为以下事实的所有者：

- 当前 Task 状态、当前 Objective 状态和 Task admission/confirmation。
- Execution、Step、lease、checkpoint、retry、queue 或 reconciliation 状态。
- ActionObservation 的原始 receipt、continuation payload 或 resumable state。
- ResourceBinding、Draft、Schedule、Post 的当前业务状态和身份关系。
- Run 的 accepted/running/terminal 状态。
- Approval request、approval decision 和 human-wait 状态。

历史事实可以作为 Memory 的输入，但必须先转换成不依赖当前 ID 和运行状态的可复用摘要。原始事实仍由其 canonical repository 持有。

## 8. Existing State Boundary Audit

|数据|当前归属|是否 Memory 候选|原因|
|---|---|---|---|
|Conversation、Session、Message、Summary|ConversationService、ConversationRepository、MessageRepository|原始数据不是；派生稳定偏好/事实可以|Conversation 是对话连续性和短期历史的来源。摘要不能自动等同于长期 Memory；必须由 extractor/classifier 提取可复用内容|
|Task|TaskProvider、TaskManager、Task 持久化模型|当前 Task 不是；重复目标模式可以|Task 是用户长期业务目标及其当前生命周期，包含 status、phase、confirmation、active execution 和 resource index。不能复制成 Episode|
|Objective|Task/Objectives、Objective reducer|当前 Objective 不是；已验证的长期结果/经验可以|Objective 描述本次用户意图和完成条件。PENDING、COMPLETED、FAILED 等状态是 current/business state，不是 Memory|
|Execution、PlanExecution、StepExecution|Durable Execution Runtime、ExecutionRepository、StateManager|当前状态不是；终态结果可作为候选来源|Execution 负责运行、步骤、重试、暂停、审批等待、checkpoint 和恢复。Episode 只能记录从它派生出的 reusable lesson|
|ActionObservation|ActionObservationStore/PostgresActionObservationStore|不是 Memory；是高价值候选的输入事件|它是 terminal durable action 的验证 receipt，包含 execution_id、business_result、error、resource_refs 等，用来驱动 continuation 和 ContextBuilder 的 verified outcome 投影|
|Run、AgentRun、Run event|AgentRunStore、agent_runs、RunExecutionAdapter|不是；不应以 Run 为 Episode|Run 是接受、claim、lease、SSE/activity 和幂等的编排 envelope。run_id 不能成为经验语义或检索依据|
|ResourceBinding、Task.resource_index、Objective.related_resource_ids|Task 资源索引/typed binding；Draft、Schedule、Post 的业务事实由 Java/Resource owner 持有|当前 binding 不是；抽象的业务经验可以|ResourceBinding 是资源身份、所有权和 Objective lineage 的 canonical 关系。不得把 draft_id、schedule_id 或当前 resource status 复制到 Episode|
|Artifact|Artifact store、Task artifact projection、Java 业务资源|当前 Artifact 不是；artifact 内容经过脱敏和抽象后可以成为候选来源|Artifact 是本次结果或业务对象的投影。Memory 不应复制正文、外部 ID 或可写目标|
|Approval request/decision|ApprovalRuntimeService、durable approval store、Runtime control state|不是；稳定的用户审批偏好只有在明确表达时才是 Preference|WAITING_APPROVAL、批准/拒绝和恢复权限是安全控制状态，不能由 Memory 推断或替代|
|Preference Memory|MemoryManager、MemoryRepository、PreferenceRetriever|是|它是用户长期偏好的专门证据，当前 V2 已有 extractor、scope、merge、supersede、gate 和 no-memory|
|稳定用户事实|当前可能散落在 Conversation/业务事实中|有条件地是|只有在明确来源、置信度、租户范围和冲突策略完成后才可写入 Semantic Memory；不能把所有摘要自动升级成事实|
|可复用历史经验|当前不存在唯一 canonical owner；来源可为 verified ActionObservation/完成结果/用户纠正|是未来 Episodic 候选|它必须是派生的、摘要化的、可复用的 lesson，而不是 Task/Execution 的第二份状态快照|

### 8.1 防止 Task + Execution + Episode 三重描述

未来 Episode 只应回答：

> 这次已经验证过的经历，是否形成了对未来请求有帮助的、抽象且可复用的经验？

它不应回答：

- 这个 Task 目前处于什么状态？
- 这个 Execution 是否仍在运行？
- 某个 draft/schedule 当前是什么状态？
- 某个 Run 是否可以恢复？

因此，Episode 的 provenance 可以指向一个已验证来源，用于审计和幂等；但 provenance ID、当前 status 和可恢复 payload 不能成为 Episode 的语义内容，也不能替代原始 owner。

## 9. Episodic Memory 最小 Vertical Slice 设计

本节只提出设计，不实现代码，不新增 episode table、memory schema 或 vector collection。

### 9.1 最小边界

最小 Episodic slice 应复用当前 MemoryRecord/agent_memories 的持久化能力，但只允许一个受控的事件分类：

    verified terminal ActionObservation
      + optional user correction/confirmation
      + reusable-value classifier
      -> one bounded EPISODIC summary
      -> typed episodic retrieval
      -> optional ContextBuilder evidence

第一版不应从每一个 Task、每一个 Step 或每一个 Run 自动生成 Episode。

### 9.2 值得长期保存的 GreenBook 事件

|事件|建议分类|建议保存的抽象|不保存的部分|
|---|---|---|---|
|内容发布成功且流程具有可复用价值|Episodic，若形成稳定步骤则再转 Procedural|例如“该类内容经过草稿确认、发布时间解析和发布后验证后完成；这条流程对类似请求有参考价值”|draft_id、schedule_id、post_id、run_id、一次执行的完整状态和 payload|
|发布失败后经过验证的恢复|Episodic 或 Procedural|例如“外部写入结果不确定时，先 reconciliation/query，再决定是否重试，避免重复发布”|某次 error payload、某个 execution 的 retry 次数、当前 reconciliation 状态|
|用户在多个 Turn 中反复采用同一操作方式|优先 Preference 或 Procedural|稳定的时间、格式或步骤习惯；按重复证据分类，不按单次记录|每次对应的 Task/Run/资源 ID|
|用户明确纠正了已有行为|Preference；如果纠正的是操作策略则可成为 Procedural 候选|新偏好或明确的可复用策略，并 supersede 旧 Preference|原始完整对话、旧资源状态、审批结论|
|一次高价值、可解释的业务结果|Episodic|短摘要、结果类别、可复用 lesson、置信度和来源类型|原始资源正文、私密参数、可执行命令和当前状态|

需要特别区分：重复操作习惯通常不是 Episodic。它若描述“用户喜欢什么”，属于 Preference；若描述“任务应如何完成”，属于 Procedural。Episodic 只保留经过验证的“发生过且未来可能有帮助的经历”。

### 9.3 不应保存的内容

以下内容禁止成为 Episodic 的语义字段、metadata 或注入上下文：

- schedule_id、draft_id、post_id 等业务资源 ID。
- run_id、execution 状态、step 状态、lease、checkpoint、retry 和 queue 状态。
- approval_id、approval decision、human-wait 状态。
- 原始 ActionObservation payload、工具参数、外部响应正文和秘密。
- 一次性用户请求、未验证的 LLM 推断、单次普通成功。
- 用于恢复当前运行的 continuation 数据。

当前旧的 remember_execution 已将 status、draft_id、schedule_id 写入 EPISODIC metadata；这应被视为实施前必须清理的边界风险，而不是新设计应继续沿用的 contract。

### 9.4 建议的 Episode 记录语义

在不新增表的前提下，概念上可使用现有 MemoryRecord：

- memory_type：EPISODIC。
- content：有界、去 ID、面向复用的事件摘要。
- structured_metadata：event_kind、domain、outcome_class、lesson、reusability、confidence 等白名单字段。
- source_type/source_id：只作为 opaque provenance 和幂等依据；不能在检索文本中作为业务语义。
- user_id/tenant_id：强制隔离。
- status/importance/confidence/expiry：使用现有 Memory 生命周期，但要增加 Episodic 的 retention 和 supersede 规则。

task_id、execution_id、run_id、draft_id、schedule_id 如果未来为审计而保留，必须被视为不可注入的 provenance，而不是 Episodic 内容。第一版建议以 ActionObservation 作为候选来源，并让原始 Observation 继续拥有完整 ID 和运行事实；Memory 只保存摘要。

### 9.5 写入门槛

Episode writer 在架构上应满足：

1. 来源必须是已提交且可验证的 terminal ActionObservation，或明确的用户纠正/确认。
2. 事件必须具备复用价值、异常恢复价值或用户明确要求长期记住的价值。
3. 单次普通成功默认不写。
4. 必须经过 tenant/user scope、敏感信息过滤和结构化字段白名单。
5. 使用语义 fingerprint 去重，不仅依赖 run_id 或 execution_id。
6. 新经验应能 supersede/合并旧经验，不能无限追加同义记录。
7. 写入异步发生在当前事实提交之后，不能阻塞或改变 Task/Execution 收敛。
8. 写入失败不能改变原有业务结果，也不能让 Runtime 进入 Memory-owned state。

### 9.6 检索和注入门槛

建议单独定义 EpisodicRetriever，或在统一 retriever 中执行严格的 type policy：

- 强制 type=EPISODIC、user、tenant 和 ACTIVE/expiry 过滤。
- 使用比 Preference 更严格的 relevance/confidence gate。
- 结果为空时返回 no-memory。
- 限制数量、字符数和每种事件的注入预算。
- 返回的是历史 evidence/lesson，不是当前 Task/Execution state。
- 不允许 Episode 直接选择 target、ResourceBinding、tool 或 approval action。
- 需要记录 selected/rejected/harmful/unnecessary injection 指标。

在 ContextBuilder 中，Episodic 应作为显式的可选证据区，而不是塞入 active_tasks、execution_states 或 available_resources。Interpreter 可以看到抽象 lesson，但 ActionLoop 和 Resolver 仍必须只使用当前 canonical state 做执行决定。

## 10. Memory Roadmap

|阶段|目标|数据来源|存储方式|检索方式|与现有 Runtime 关系|
|---|---|---|---|---|---|
|Preference Memory V2（当前）|保存用户稳定偏好，支持合并、supersede、tenant isolation、relevance gate 和 no-memory|完成 Turn 的明确长期表达、显式设置、受控纠正|现有 agent_memories；PREFERENCE 当前兼容序列化为 SEMANTIC|PreferenceRetriever + MemoryRelevanceGate；最多 5 条|通过 ContextBuilder 作为有界证据；不修改 Task/Execution/Resource/Approval|
|Episodic Memory（下一阶段）|保存已验证且可复用的历史经历/lesson，不复制当前状态|terminal ActionObservation、已验证业务结果、用户纠正/确认|优先复用现有 MemoryRecord/agent_memories；白名单 metadata；本阶段不新增表/Schema/vector|typed EpisodicRetriever + relevance/confidence/retention gate|只作为可选历史证据；不能恢复 Run、选择 Resource 或替代 ActionObservation|
|Semantic Memory（后续）|保存稳定用户事实或经验证的领域事实|明确声明、重复证据、受信业务事实|仍需先解决 PREFERENCE=SEMANTIC 的兼容别名；再决定同表 subtype 还是独立契约|按事实类型、scope、有效期和冲突版本检索|提供事实证据；事实必须经过当前业务 owner 验证，不能成为当前资源状态|
|Procedural Memory（后续）|保存可复用的任务策略、流程模式和失败恢复方法|多次成功 Episode、明确用户纠正、经过验证的 workflow|可复用现有 PROCEDURAL record；先收敛已有 ProceduralMemoryExtractor/StrategyRetriever|按 goal category、capability、成功率和 gate 检索|只能作为规划 hint；不能绕过 ActionLoop、policy、approval 或 Durable Runtime 直接执行|

Roadmap 的依赖关系应保持：

    Preference Memory V2
            |
            v
    Episodic Memory
            |
            v
    Semantic Memory
            |
            v
    Procedural Memory

但在技术实施上，Episodic 之前必须先解决现有 legacy episodic/procedural runtime hook；否则路线图会变成并行实现，而不是逐层演进。

## 11. 边界风险清单

|风险|证据|影响|处理原则|
|---|---|---|---|
|旧 RuntimeAgentService 仍写/读 Episodic 和 Procedural|_record_episodic、_record_procedural、_recall_memories|新 Episode 会重复写入或绕过 V2 gate|先收敛单一 writer/retriever owner|
|旧 episodic metadata 包含 status、draft_id、schedule_id|MemoryManager.remember_execution 和 recent_tasks projection|把当前状态伪装成长期 Memory|新 contract 禁止这些语义字段；旧记录需隔离或迁移策略|
|MemoryManager.recall 绕过 relevance gate|generic recall 直接 repository search + touch|无关注入或完整执行历史可能被注入|所有模型-facing recall 必须经过 typed gate|
|Preference alias 与 Semantic 共用 SEMANTIC|MemoryType.PREFERENCE = MemoryType.SEMANTIC|未来 Semantic Fact 与 Preference 发生类型混淆|先定义 subtype/metadata/检索契约|
|primary InMemory 与 durable Postgres 并存|main.py 的 MemoryManager wiring|重启、一致性和旧 provider 读取路径可能分叉|未来不得增加第三个存储副本；明确 canonical read/write|
|ContextBuilder 同时支持 retriever 和 legacy provider|canonical path 已有 no-memory 防回填，但 fallback 仍存在|不同调用方可能得到不同的 Memory 视图|逐步统一 provider，保留兼容适配而非第二事实源|
|Postgres keyword filter 在 SQL limit 后执行|PostgresMemoryRepository.search|候选不足时可能漏召回，影响后续类型扩展|后续单独做检索质量设计，不在本阶段改生产代码|
|MemoryRecord 结构足够宽|content、metadata、task_id、source_id 可装任意信息|调用方容易把 Task/Execution 快照塞进 Memory|按 MemoryType 建立字段白名单和写入 policy|
|embedding 字段存在但没有实现|模型有 embedding，canonical retriever 不使用|误以为已有语义检索，导致设计假设错误|在没有明确方案前不启用 vector 语义|

## 12. 下一阶段实施建议

本 checkpoint 不实施代码。后续若获准进入 Episodic 实现，建议按以下顺序：

1. 先冻结 Architecture/ADR：明确 Current State owner、Memory candidate、provenance、禁止字段和注入边界。
2. 先处理 legacy RuntimeAgentService 的 Episodic/Procedural 写入和召回归属，确保只有一个 canonical Memory writer 和一个模型-facing typed retriever。
3. 为现有 MemoryRecord 建立按类型的 metadata allowlist；明确 PREFERENCE 与 Semantic Fact 的兼容方案。
4. 只选一个 GreenBook 事件做最小 slice，优先选择“已验证的发布失败恢复 lesson”或“经用户确认的发布流程经验”，不从全部执行结果泛化。
5. 复用现有 agent_memories 和 MemoryRelevanceGate；不新增 episode table、memory schema 或 vector collection。
6. 在默认注入前完成离线评估：relevance、useful recall、unnecessary injection、harmful injection、duplicate rate、tenant isolation、restart consistency。
7. 只有评估达标后，才将 Episodic 作为 ContextBuilder 的显式可选 evidence 打开；它仍不能进入 Task/Execution/Resource/Approval 的 canonical state。

## 13. 最终架构判断

当前 Preference Memory V2 的边界和注入位置基本成立：Memory 是长期证据层，ContextBuilder 是唯一的有界汇合点，Interpreter 只得到安全投影，相关性门控能够产生 no-memory 结果。

当前尚未完成的是 Memory domain 的“类型收敛”。EPISODIC 和 PROCEDURAL 的代码能力已经存在，但旧运行时路径仍会把执行结果和资源 ID 写入/读取为 Memory。若直接进入 Episodic 开发，最可能出现的不是新能力缺失，而是事实重复、状态泄漏、无关注入和两个 Retriever 互相绕过。

因此本 checkpoint 的结论是：

> 可以为 Episodic Memory 做设计准备；暂不应直接新增 Episodic 实现。下一步先冻结边界并收敛 legacy Memory 路径，再实现一个不含当前状态和业务 ID 的最小经验 slice。
