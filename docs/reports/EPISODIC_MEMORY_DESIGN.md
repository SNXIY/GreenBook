# EPISODIC_MEMORY_DESIGN

## Design status

- Project: D:/agent/green-book
- Branch: feature/hybrid-search-rag
- Runtime convergence checkpoint: daf5f2b5d1ec99edf5f3c24c075fea47ae5c45c1
- Scope: read-only Episodic Memory design
- Implementation status: no Episodic Memory code, table, vector collection, or repository was added in this phase

本报告建立在 Memory Runtime convergence checkpoint 之上。Preference Memory 的生产读写边界已经收敛；本报告只冻结下一阶段 Episode 的 source、candidate、policy、storage 和 evaluation contract。

## 1. Current architecture conclusion

当前生产 Preference path：

    Completed turn
      -> API completed-result bookkeeping
      -> PreferenceMemoryService
      -> PreferenceMemoryExtractor
      -> MemoryManager / canonical Memory Repository

    Current turn
      -> TurnCoordinator
      -> ContextAssembler
      -> ContextBuilder
      -> PreferenceRetriever
      -> MemoryRelevanceGate
      -> bounded ContextSnapshot
      -> project_interpreter_context
      -> Interpreter

RuntimeAgentService 已不再在 terminal execution 中自动 recall、写入 Episodic 或写入 Procedural。现有 ActionObservation 仍是 Durable Runtime 的 terminal receipt 和 continuation projection，不等于 Memory。

因此当前边界结论是：

1. Memory 只能承载长期、可复用的用户偏好、稳定事实或经过抽象的历史经验。
2. Task、Objective、Execution、Run、ResourceBinding 和 Approval 继续保留各自的 current-state truth。
3. Episode 只能是从已完成、已验证历史行为派生出的摘要，不能成为 Runtime 状态的副本。
4. Memory 不得覆盖当前显式请求、当前已验证业务事实或 Task/Objectives 决策。

优先级固定为：

    Current explicit request
      > verified current business/runtime truth
      > Conversation context
      > Long-term Memory

## 2. Source audit

### 2.1 ActionObservation

ActionObservation 位于 Durable Runtime 的业务结果投影边界。当前模型包含 execution_id、task_id、conversation_id、run_id、goal_id、terminal status、resource_refs、business_result、observed_at 及恢复 payload。

ActionObservationWriter 的写入顺序是：

    terminal RuntimeResult
      -> ResultResolver 合并持久化 artifact 与结果
      -> CompletionProjectionCoordinator 完成 Task/Conversation/result projection
      -> ActionObservationWriter 保存 terminal receipt

ActionObservation 因此是最合适的 Episode event anchor，但它本身仍然不是足够条件：

- receipt 必须是 terminal；
- RESULT_UNKNOWN、RUNNING、pending 和未确认的外部结果必须拒绝；
- business_result/resource_refs 必须有 verified business outcome；
- 必须与 terminal Objective 的结果关联；
- receipt 中的 runtime identity 只用于 provenance，不得进入 Episode 的语义正文。

分类：ACTIVE 作为 Runtime evidence；未来是 Episode Candidate 的首选来源；不是 Memory truth owner。

### 2.2 Terminal Objective 和 Task

Objective 是用户意图的事实模型。Objective 只有在真实 Resource、Operation 或 Verification 满足 expected postcondition 后才进入 COMPLETED。Task 是多个 Objective 的长期聚合以及生命周期 owner。

Episode source 使用它们的方式：

- terminal Objective 是“该用户意图是否真正完成”的必要 corroboration；
- Objective 的 expected resource kind、postcondition 和 completed_at 可以帮助 Candidate Builder 判断 outcome；
- Task 的 COMPLETED 只能作为聚合结果，不能单独证明某个业务结果；
- Task/Objectives 不应整行复制到 Memory；
- Task、objective_id、execution_id 等 identity 只能进入内部 provenance/reference。

分类：ACTIVE 作为 current-state truth；可作为 Episode source evidence；不能直接作为 Episode。

### 2.3 Verified business outcome

真实业务 outcome 来自已完成的 completion projection、持久化 artifact/resource projection 以及 Java business read-back/verification。ResultResolver 负责把这些已存在的事实合并成可供 presentation 和 receipt 使用的结果。

这是 Episode 内容可信度最高的部分。未来 Candidate Builder 应从 verified resource/postcondition 中提取有限的语义事实，例如“该次发布在用户主动修订标题和时间后完成”，而不是从成功字符串、LLM 解释或工具原始返回体推断事实。

Java 继续是业务 truth owner。Java 不直接写 Agent Memory；它提供已验证业务事实，由 Runtime projection 和 Candidate Builder 在 Agent 侧产生派生候选。

分类：ACTIVE，且是 Episode outcome 的最高可信证据。

### 2.4 Conversation metadata

ConversationService/MessageRepository 保存 conversation identity、用户与 assistant message、summary、session pointers、run_id、execution_id 和 trace_id。它的作用是：

- 提供事件发生的 conversation scope；
- 提供用户显式修正或显式要求记住的语义线索；
- 提供审计 provenance；
- 支持跨 conversation 的长期检索。

Conversation summary 或 LLM intermediate text 不能单独触发 Episode 写入。summary 可能被压缩、截断或包含未经验证的解释；session 的 active_task、active_artifact 和 last_successful_run_id 也属于当前指针，不是长期经验。

分类：ACTIVE 作为上下文/provenance；不是 verified business source。

### 2.5 Source reliability decision

Canonical Episode source 采用组合，而不是单一表：

    terminal ActionObservation
      + terminal Objective result
      + verified business outcome
      + optional user correction/confirmation from Conversation

可靠性排序：

1. verified business outcome / read-back postcondition；
2. terminal ActionObservation 作为已提交事件锚点；
3. terminal Objective 作为用户意图完成的 corroboration；
4. Conversation metadata 作为语义和 provenance；
5. Task/Run/Execution status 仅作为关联或生命周期，不作为 Episode 正文来源。

## 3. Existing state boundary audit

| 数据 | 当前归属 | 是否 Memory 候选 | 原因 |
|---|---|---:|---|
| Conversation message | ConversationService / MessageRepository | 有条件 | 只有用户显式表达长期意图、修正或要求记住时，才可作为 candidate evidence；普通对话不写 |
| Conversation summary | ConversationService | 否，单独不足 | 是压缩上下文，不保证业务事实；只能提供辅助语义和 provenance |
| Task | TaskProvider / TaskManager | 否，整行不写 | Task 是长期 current objective 与生命周期 owner；可提供 candidate 的业务类别和关联 |
| Objective | Task/Objectives lifecycle | 有条件 | terminal Objective 结果是 candidate 的必要 corroboration，但不能复制成 Episode |
| Execution | Durable Execution repositories | 否 | 是一次运行实例；状态、重试、lease、checkpoint 和 delivery 不是长期语义 |
| ActionObservation | ActionObservationStore | 是，作为 source event | 是 terminal action receipt；必须再经过 verified outcome、Objective join 和 policy |
| Run | AgentRun / RunExecutionLink | 否 | 是请求/运行 envelope 和 correlation identity，不是历史经验 |
| ResourceBinding / TaskResourceRef | Task projection；业务资源由 Java truth owner | 有条件 | 只提取已验证、可抽象的 outcome；不能保存 resource snapshot 或把 resource ID 当语义主体 |
| Draft/Schedule/Post identity | Java business domain + Task resource projection | 否，作为正文 | draft_id、schedule_id、post_id 只能放在内部 trace/provenance；不能作为 Episode 内容 |
| Approval | ApprovalRuntimeService / approval store | 否 | approval 状态是当前授权事实；用户显式修正可作为独立语义 evidence，但不能保存 approval snapshot |
| Retry/Reconciliation/RESULT_UNKNOWN | Durable Runtime / operation ledger | 否 | 它们描述 delivery 和恢复状态；未完成或未知结果严禁成为长期 Episode |

该边界避免出现：

    Task + Execution + Episode

三份记录同时描述同一个当前状态。Episode 只能保留抽象后的历史意义；原始状态继续由原 owner 负责。

## 4. EpisodeCandidate contract

EpisodeCandidate 只表达未来可能复用的历史语义，不表达 Runtime identity：

| 字段 | 语义 | 约束 |
|---|---|---|
| user_id | 记忆归属用户 | 必填；写入和读取都必须 scope |
| tenant_id | 租户边界 | 必填；不得从缺省值推断 |
| category | 业务语义类别 | 例如 CONTENT_PUBLICATION_WORKFLOW；不是 tool 或 execution 名称 |
| summary | 一次已验证经历的短摘要 | 事件范围表达；不得包含 runtime/resource ID、当前状态或原始 payload |
| outcome | 抽象后的已验证结果 | 例如 VERIFIED_PUBLICATION_AFTER_REVISION；不是任意 Runtime status |
| occurred_at | 经历实际发生/完成的时间 | 来自 terminal observation 或 verified business result；不使用写入时间代替 |
| confidence | 对 candidate 语义的可信度 | 来自 source verification 和 policy；不能使用 LLM 自报置信度 |
| provenance | 内部审计与去重引用 | 可包含 source type 和 opaque source identity；不进入 model-facing context |
| source_type | candidate 的受控来源类别 | 例如 VERIFIED_ACTION_OBSERVATION；必须来自 allowlist |

建议的 candidate 语义示例：

    category: CONTENT_PUBLICATION_WORKFLOW
    summary: 在一次技术内容发布流程中，用户在最终验证前主动调整了标题和发布时间，随后修订后的内容完成发布。这是一段已验证的发布经历。
    outcome: VERIFIED_PUBLICATION_AFTER_USER_REVISION
    occurred_at: terminal ActionObservation.observed_at 或 verified completion time
    confidence: 由 verified result、Objective 关联和用户修正证据共同计算
    source_type: VERIFIED_ACTION_OBSERVATION
    provenance: 内部 source reference；仅用于审计、幂等和回溯

这段正文描述“一次经历”，不说“用户通常喜欢”或“用户总是这样做”。如果无法去除 runtime state、敏感字段或当前资源身份，则 candidate 不合格。

## 5. Worth-Remembering Policy

Policy 输出只有 KEEP、DROP、UNKNOWN。UNKNOWN 不写入 Memory，等待更强证据或显式用户确认。

### KEEP

- 用户明确要求记住，或明确确认了一项重要修正；
- 已验证的非普通、多步骤内容发布经历，存在未来规划或解释价值；
- 已验证的失败恢复经历，且恢复过程产生了可复用的历史 lesson；
- 多个独立完成事件提供一致证据，且 candidate 仍然表达为历史经历而不是未经批准的偏好；
- candidate 可以被抽象为短摘要，不需要保存当前资源或运行状态。

### DROP

- 普通一次性查询、读取或 CRUD；
- 只有“发布成功”而没有用户修正、恢复 lesson 或其他长期复用价值的普通成功；
- RUNNING、pending、WAITING 或 RESULT_UNKNOWN；
- 没有 verified business outcome 的工具返回、乐观 success 或 LLM intermediate text；
- 纯 runtime retry、queue delivery、lease、checkpoint、reconciliation 或 approval 状态；
- 只保存 draft_id、schedule_id、run_id、execution_id、operation_id 的记录；
- 可能包含 secrets、原始参数、完整内容正文或不必要个人数据的记录。

### UNKNOWN

- 单次“用户修改标题”行为，无法区分历史事件与稳定偏好；
- 结果 terminal，但缺少 terminal Objective 关联或业务 read-back；
- 只有 Conversation summary 证明事件发生；
- 事件看似复杂，但没有清晰的未来复用价值；
- 多次证据互相冲突，尚未能决定是 Preference、Episodic 还是未来的 Procedural；
- 事件需要把当前 Task/resource state 复制到 Memory 才能表达。

### Decision rules

Candidate 只有同时满足以下条件才能 KEEP：

    trusted source
      + terminal verified outcome
      + terminal Objective corroboration
      + reusable historical meaning
      + safe abstraction and redaction
      + user/tenant scope
      + deterministic dedup/provenance

单次普通成功默认 DROP。无法明确判断时默认 UNKNOWN，而不是为了填充 Episodic 数据强行写入。

## 6. Consolidation boundary

未来写入必须遵守以下唯一边界：

    Execution completes
      -> completion projection commits
      -> verified ActionObservation
      -> join terminal Objective and verified business outcome
      -> EpisodeCandidateBuilder
      -> WorthRememberingPolicy
      -> canonical MemoryManager
      -> canonical Memory Repository

约束：

- ActionLoop 不直接写 Episode；
- ToolRuntime 不直接写 Episode；
- RuntimeAgentService 不直接成为 Memory truth owner；
- Java 不直接写 Agent Memory；
- candidate builder 可以异步消费已提交 projection，但写入失败不得改变 Task、Execution、Observation 或 Java business truth；
- source replay 必须幂等，不能因为 completion callback/reconciliation 重复生成多个 Episode；
- Memory write 只能通过 user_id、tenant_id、lifecycle、provenance、policy 和 canonical repository。

## 7. Retrieval integration

未来 Episodic read 必须复用同一条 model-facing path：

    Current Turn
      -> canonical MemoryRetriever
      -> MemoryRelevanceGate
      -> bounded Memory Context
      -> ContextBuilder
      -> project_interpreter_context
      -> Interpreter

不得创建：

    EpisodicRetriever -> Prompt

也不得在 RuntimeAgentService、ActionLoop 或某个 Memory type 中维护第二套 ranking、threshold、touch 或 injection semantics。

Episode retrieval 的最小要求：

- user_id 与 tenant_id 双重 scope；
- memory_type=EPISODIC 的显式类型过滤；
- ACTIVE、未过期和 confidence/relevance gate；
- no-match 返回 0 条；
- bounded item count 和 content length；
- provenance 可审计但不向模型暴露 runtime identity；
- Memory 只提供 evidence/lesson，不负责 target resolution、approval 或 tool selection。

当前生产只启用 PreferenceRetriever；未来开启 Episode retrieval 应扩展 canonical retriever/gate，而不是恢复 RuntimeAgentService 的 legacy recall。

## 8. Minimum vertical slice: meaningful content publication history

只选择一个场景：用户在一次技术内容发布中主动完成标题和发布时间修订，最终发布结果通过业务 read-back 验证。

### Eligibility

全部条件必须成立：

1. 同一个已完成 Task 中，内容目标和发布目标均有 terminal Objective result；
2. ActionObservation 是 terminal，且不是 RESULT_UNKNOWN；
3. final draft/schedule/post 的业务 outcome 已被现有 projection/Java read-back 验证；
4. 标题或发布时间的修改来自用户显式修正，或该多步骤过程有明确的未来复用价值；
5. candidate summary 只描述一次历史经历，不推断稳定偏好；
6. 无需将 draft_id、schedule_id、run_id 或执行状态写入语义正文。

### Candidate

    category:
      CONTENT_PUBLICATION_WORKFLOW

    summary:
      在一次技术内容发布流程中，用户在最终验证前主动调整了标题和发布时间，随后修订后的内容完成发布。这是一段已验证的发布经历。

    outcome:
      VERIFIED_PUBLICATION_AFTER_USER_REVISION

    source:
      terminal ActionObservation + terminal Objective + verified business outcome

### Why this is Episodic

candidate 明确指向“一次已经发生的流程”和其 outcome，而不是用户的一般规则。它没有写成“用户喜欢先改标题”或“用户总是调整发布时间”，因此不会从一次行为直接推导 Preference。

如果实际事件只是普通编辑，没有显式用户修正、没有多步骤 lesson、也没有未来复用价值，则同一个 fixture 的 policy 结果应为 DROP 或 UNKNOWN，不应为了实现 Episodic 而写入。

## 9. Preference vs Episodic boundary

同一历史行为的分类依赖语义稳定性和证据数量：

| 观察 | 分类 | 原因 |
|---|---|---|
| 一次流程中用户修改了标题 | Episodic candidate 或 UNKNOWN | 只能说明这次发生过；只有在有复用意义且已验证时才可 KEEP |
| 用户明确说“以后发布前都先调整标题和时间” | Preference candidate | 是用户显式表达的稳定规则 |
| 多个独立 conversation 中反复出现同一选择 | Preference candidate | 多次证据支持稳定偏好，但仍须经过 Preference policy |
| 一次成功发布后推断“用户喜欢这种流程” | 不允许 | 单次行为不足以证明偏好 |
| 多次验证的操作顺序形成可复用步骤 | 未来可能是 Procedural | 它是方法/策略，不应伪装成 Preference 或单次 Episode |
| 一次已验证的特殊失败恢复 | Episodic candidate | 记录历史经历和 lesson；只有经多次验证才考虑 Procedural |

因此，Episode 可以承载一次有价值的历史经历；Preference 需要更高的稳定性证据或用户显式表达。Candidate Builder 必须先判断 event-scoped summary 是否足够，不能把任何 action verb 自动改写成偏好句式。

## 10. Storage proposal

优先复用现有 Memory schema，不创建新表、新 vector collection 或第二个 repository。

现有 MemoryRecord/agent_memories 已能表达：

- user_id、tenant_id；
- memory_type=EPISODIC；
- content 作为 bounded summary；
- structured_metadata 作为受控的 category、outcome、occurred_at 和 policy evidence；
- confidence、importance；
- source_type/source_id 作为内部 provenance 和幂等 key；
- status、created_at、updated_at、expires_at、access_count 作为生命周期；
- 现有 InMemoryMemoryRepository 与 PostgresMemoryRepository 的 canonical persistence。

建议映射：

| EpisodeCandidate | 现有 MemoryRecord | 备注 |
|---|---|---|
| user_id | user_id | 必须保留 |
| tenant_id | tenant_id | 必须保留 |
| category | structured_metadata.category | allowlist |
| summary | content | bounded、去敏、无 runtime ID |
| outcome | structured_metadata.outcome | verified semantic outcome |
| occurred_at | structured_metadata.occurred_at | 不能用 repository write time 替代 |
| confidence | confidence | 来自 verified source/policy |
| provenance | source_type/source_id + restricted metadata | internal only |
| source_type | source_type | allowlist |

实现前必须增加 contract-level allowlist 和 sanitizer，但这不是本阶段的 schema change。MemoryRecord 的 task_id、conversation_id 等兼容字段不能被用来把 Episode 重新变成 Task snapshot；Episode 默认不把 task_id 作为语义字段，conversation_id 也只作为 provenance scope。

已有 MemoryType.PREFERENCE 使用 SEMANTIC storage value 的兼容设计，说明未来区分 Preference 与 Episodic 时必须显式按 memory_type 和 metadata contract 过滤，不能依赖字符串内容猜测类型。

历史 legacy EPISODIC rows 可能包含 status、draft_id、schedule_id 或缺失 tenant scope。它们在 quarantine/迁移/保留策略确定前，不应直接作为新 Episode 或 model-facing context 使用。

## 11. Evaluation proposal

本阶段不运行大规模 evaluation。下一阶段实现前准备以下 focused benchmark：

| Fixture | 预期 |
|---|---|
| 已验证的标题/发布时间修订并最终发布 | 写入 1 个 EPISODIC candidate |
| 普通一次性发布成功 | 写入 0 个 |
| RUNNING、pending 或 RESULT_UNKNOWN | 写入 0 个 |
| 未经 business read-back 的乐观 success | 写入 0 个 |
| 同一 terminal observation 重放 | 不能产生重复 row |
| 不同 user 的同类经历 | 互不可见 |
| 同一 user 不同 tenant 的经历 | 互不可见 |
| 相关 query 来自另一 conversation | 可以召回相关 Episode |
| 无关 query | no-memory，返回 0 条 |
| 一次修改标题 vs 显式稳定偏好 | Episode 与 Preference 不混淆 |
| Episode content/metadata 含 runtime ID 的输入 | sanitizer 拒绝或移除 |
| Memory feature flag OFF | baseline 行为不变，0 次注入 |

至少记录以下指标：

- verified candidate write precision；
- 不值得记事件的 write rate；
- unverified/RESULT_UNKNOWN write rate，目标为 0；
- relevant cross-conversation recall；
- irrelevant query 的 no-memory rate；
- cross-user leakage，目标为 0；
- cross-tenant leakage，目标为 0；
- duplicate source write rate；
- harmful/unnecessary injection rate；
- bounded injection item count 和 content size。

Retrieval benchmark 必须验证 Episode 和 Preference 都经过 canonical MemoryRetriever、MemoryRelevanceGate 及 ContextBuilder；不能仅测试 repository search。

## 12. Blockers and risks

1. 当前 ActionObservation 有 task/goal/resource evidence，但没有一个统一、明确的 objective join contract。实现前需要定义 ActionObservation、terminal Objective、result projection 之间的稳定关联方式。
2. RuntimeResult 的 terminal execution status 不等于 Task 全部 Objectives 已完成；Candidate Builder 不能只看 status=COMPLETED。
3. 现有 MemoryWritePolicy 对 TASK_COMPLETED、TASK_FAILED_MAJOR 和有 artifact 的 execution outcome 较宽松，不能直接当作新的 Episode worth-remembering policy。
4. MemoryType.PREFERENCE 与 SEMANTIC 共享 storage value；Episode/Preference 的类型过滤和 schema allowlist 必须先冻结。
5. 现有 legacy EPISODIC records 可能复制 Runtime status 和 resource identity，需先决定 quarantine、迁移或不读取策略。
6. 现有 runtime helper 和 MemoryManager compatibility API 仍可被显式调用；它们不能重新成为 Episode production writer。
7. Conversation summary、LLM text 和原始 tool payload 需要明确 redaction/sanitization 边界，避免把未验证或敏感内容长期保存。
8. 当前 canonical model-facing recall 主要是 Preference；Episode 开启前必须在同一 gate 下增加类型和 relevance benchmark。

## 13. Next implementation scope

下一阶段只允许按以下顺序实现：

1. 冻结 EpisodeCandidate、provenance allowlist、redaction 和 objective verification contract；
2. 实现只消费已提交 ActionObservation 的 EpisodeCandidateBuilder；
3. 只实现本报告的一个 CONTENT_PUBLICATION_WORKFLOW event kind；
4. 通过现有 MemoryManager/Memory Repository 写入 EPISODIC，保留 user/tenant/lifecycle/idempotency；
5. 先运行 focused candidate/policy/isolation/no-memory benchmark；
6. 将 Episodic retrieval 接入已有 canonical MemoryRetriever、MemoryRelevanceGate 和 ContextBuilder；
7. 在开启 model-facing injection 前完成历史 legacy row quarantine 决策；
8. 再评估是否存在足够证据把多个 Episode consolidate 为 Preference 或未来 Procedural。

禁止的下一步：

- 从 ActionLoop 或 ToolRuntime 直接写 Episode；
- 将 Task/Execution/Run snapshot 复制到 Memory；
- 新建 episode_table、vector collection 或第二 repository；
- 恢复 RuntimeAgentService legacy recall；
- 让 Episode 自己直接写 Prompt；
- 修改 ActionLoop、Durable Runtime、Task/Objectives lifecycle、RESULT_UNKNOWN/reconciliation、MCP、RAG、Search 或 Java business truth。

## 14. Final decision

本阶段的设计决策：

- Canonical source: terminal ActionObservation + terminal Objective result + verified business outcome；
- Candidate: 只保存 event-scoped、去敏、可复用的历史摘要；
- Policy: 默认保守，KEEP/DROP/UNKNOWN，UNKNOWN 不写；
- Vertical slice: 一次有用户主动修订且最终业务结果已验证的技术内容发布经历；
- Storage: 复用现有 MemoryRecord/agent_memories 的 EPISODIC 类型，不新增 schema；
- Retrieval: 未来复用唯一 canonical Retriever -> RelevanceGate -> bounded ContextBuilder；
- Status: 设计完成，Episodic Memory 未实现。

EPISODIC_MEMORY_DESIGN_READY
