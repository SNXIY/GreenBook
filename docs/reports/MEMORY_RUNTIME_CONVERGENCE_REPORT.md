# MEMORY_RUNTIME_CONVERGENCE

审计起点：18524e6  
审计期间已保存上一阶段报告，产生提交：155168e（docs: add memory v2 architecture checkpoint）  
当前 branch：feature/hybrid-search-rag  
本阶段范围：Memory Runtime 边界收敛；不实现新的 Episodic、Semantic 或 Procedural 能力。

本阶段完成：

- 移除 RuntimeAgentService 对 legacy Memory recall 的生产调用。
- 移除 RuntimeAgentService 将 terminal execution 自动写成 Episodic/Procedural 的生产调用。
- 将 ConversationRuntimeAdapter 的兼容 fallback 从通用 MemoryRetriever 收敛为 PreferenceRetriever。
- 配置 canonical retriever 时跳过 ContextBuilder 对 legacy preference provider 的重复读取。
- 补充 Memory Runtime convergence focused tests。
- 未修改 ActionLoop semantics、Durable Runtime、Task/Objectives lifecycle、RESULT_UNKNOWN/reconciliation、MCP、Hybrid Search、RAG、Search 或 Java business truth。

## Canonical Preference Path

### Production wiring

生产 main.py 创建：

- MemoryManager，并以 PostgresMemoryRepository 作为 durable repository。
- PreferenceMemoryService。
- PreferenceRetriever。
- MemoryUserPreferenceProvider。
- ContextBuilder/ContextAssembler，并将 PreferenceRetriever 注入 ConversationRuntimeAdapter 和 TurnCoordinator 使用的 ContextBuilder。

独立 Agent Worker 也会创建 MemoryManager，但 Worker 的 Runtime 不再自行 recall 或写入 Memory；它只负责既有的 durable execution path。

### WRITE

    用户输入
      -> RuntimeResult 完成
      -> API handle_run_result
      -> _extract_completed_turn_preference
      -> PreferenceMemoryService.process_completed_turn
      -> PreferenceMemoryExtractor
      -> MemoryManager.remember
      -> source idempotency / preference merge / supersede
      -> InMemory primary + durable Postgres repository

PreferenceMemoryExtractor 只接受高置信度、带长期信号的有限 Preference。普通的一次性内容请求、发布请求和时间性请求不会自动变成长期 Memory。

### READ

    TurnCoordinator
      -> ContextAssembler
      -> ContextBuilder
      -> PreferenceRetriever
      -> MemoryRelevanceGate
      -> bounded ContextSnapshot
      -> project_interpreter_context
      -> CommandInterpreter

ConversationRuntimeAdapter 的复杂路径也使用同一类 ContextBuilder；它在拿到结构化 Command 后会重建 bounded snapshot，但仍复用同一个 canonical PreferenceRetriever，不维护第二套 ranking/gate 规则。配置 canonical retriever 时，ContextBuilder 不再调用只提供未过滤 profile dump 的 legacy preference provider。

当前 canonical read contract：

- user_id 和 tenant_id 同时作用域过滤。
- 只读取 Preference 的 ACTIVE、未过期记录。
- 默认 relevance threshold 为 0.5，confidence threshold 为 0.5。
- 最多注入 5 条 Preference。
- gate 没有命中时返回空列表。
- ContextBuilder 将 canonical retriever 的空结果视为显式 no-memory，不回填未过滤的 preference dump。
- Interpreter 只收到去除 canonical identity 的安全 evidence；Memory 不能直接选择目标或执行工具。

## Legacy Episodic Path

### 原生产调用链

审计确认原调用链真实存在且可以从生产 Runtime 到达：

    API/Agent Worker
      -> ConversationRuntimeAdapter / queue execution
      -> RuntimeAgentService.submit_plan 或 execute_queued
      -> RuntimeAgentService._execute_single
      -> terminal execution result
      -> _record_episodic
      -> MemoryManager.remember_execution
      -> MemoryRecord(memory_type=EPISODIC)

RuntimeAgentService 在 apps/agent_api/main.py 和 apps/agent_worker/main.py 都被实例化；队列 handler 也持有同一 Runtime service。因此这不是只存在于孤立测试的调用点。

### 写入时机和数据来源

旧路径在 Runtime terminal projection 已经计算出 execution outcome、failed steps、presentation artifacts 和 committed facts 后调用。它的输入包括：

- ctx.execution_input.goal。
- goal_category。
- terminal status，由 completed/failed execution 推导。
- presentation/committed facts 中的 draft_id 和 schedule_id。
- execution_id 作为 source_id。
- task_id 作为记录关联。

remember_execution 写入的语义和 metadata 为：

- content：形如 [COMPLETED] goal 或 [FAILED] goal。
- memory_type：EPISODIC。
- source_type：EXECUTION_OUTCOME。
- source_id：execution_id。
- structured_metadata：goal、goal_category、status、draft_id、schedule_id 以及 extra。
- importance：成功 0.7，失败 0.5。

这不是合格的 Episodic contract：它把当前执行状态和业务资源 identity 复制到了 Memory。

### 生产可达性结论

收敛前：LEGACY_ACTIVE。  
收敛后：DEAD_CODE（私有 helper 仍保留，但已没有仓库内生产 caller）。

本次最小修改删除了 _execute_single 对 _record_episodic 的调用，没有改变 Runtime 的 Execution、Observation、Resource 或 Result projection。终态事实继续由原有 owner 持有。

### API、前端和测试

- 没有发现独立的前端或 API endpoint 直接调用 remember_execution。
- 它原来由 API/Worker 内部 Runtime terminal path 间接触发。
- tests/unit/test_agent_memory.py 直接测试 MemoryManager.remember_execution，这是 TEST_ONLY 的旧 contract 测试，不代表生产调用。
- 没有发现测试直接调用 RuntimeAgentService._record_episodic。

## Legacy Procedural Path

### 原生产调用链

    API/Agent Worker
      -> RuntimeAgentService._execute_single
      -> terminal execution result
      -> _record_procedural
      -> ProceduralMemoryExtractor.extract
      -> MemoryManager.remember
      -> MemoryRecord(memory_type=PROCEDURAL)

### 写入时机和数据来源

旧路径从 execution_input 和终态执行投影读取：

- goal_category。
- plan_source。
- completed/failed status。
- step_count。
- tool_count。
- 失败时的 error_code。

ProceduralMemoryExtractor 会生成：

- 成功描述：某类 resolved execution succeeded with N tool calls。
- 失败描述：某类 resolved execution failed with reason。
- metadata：pattern、goal_category、plan_source、success、tool_count、step_count、error_code、confidence。
- memory_type：PROCEDURAL。

该路径没有一个明确的“多次验证后才值得保存”的 candidate builder，也没有和当前 Task/Execution owner 分离的长期经验 contract。因此本阶段不扩展它为新的 Procedural 能力。

### 生产可达性结论

收敛前：LEGACY_ACTIVE。  
收敛后：DEAD_CODE（私有 helper 仍保留，但已没有仓库内生产 caller）。

本次删除 _execute_single 对 _record_procedural 的调用。Execution 完成、失败、步骤统计和运行诊断继续由 Durable Runtime、Execution projection、Observation 和 observability owner 处理。

### API、前端和测试

- 没有发现前端或 API endpoint 直接调用 remember_pattern。
- 没有发现应用代码直接调用 remember_pattern。
- tests/unit/test_agent_memory.py 直接测试 MemoryManager.remember_pattern，分类为 TEST_ONLY。
- 没有发现测试直接调用 _record_procedural 或 ProceduralMemoryExtractor 的 Runtime hook。

## Legacy Recall Path

### 原生产调用链

原来 _execute_single 在开始执行计划后调用 _recall_memories：

    RuntimeAgentService._execute_single
      -> _recall_memories
         -> MemoryManager.recall(SEMANTIC)
         -> MemoryManager.recall(EPISODIC)
         -> StrategyRetriever(PROCEDURAL)
         -> RuntimeContext.memory_context

具体语义：

1. Preference/semantic：以 user_id 和 type=SEMANTIC 查询 5 条，直接 repository search + touch。
2. Episodic：以 user_id 和 type=EPISODIC 查询最近 5 条，并映射为 recent_tasks，其中包含 goal、category、status、draft_id、schedule_id。
3. Procedural：StrategyRetriever 直接 search PROCEDURAL，按 success 和 confidence 筛选/排序。

这条路径没有使用 PreferenceRetriever 的 tenant scope、relevance threshold 或 canonical MemoryRelevanceGate。MemoryManager.recall 本身也只是 repository search + touch。

### 是否进入 canonical TurnCoordinator/Interpreter

审计结果：

- 调用发生在 Runtime execution boundary，而不是 TurnCoordinator 的 ContextAssembler/ContextBuilder boundary。
- RuntimeContext.memory_context 在仓库中没有被 ContextBuilder、Interpreter 或 ActionLoop 读取。
- Interpreter 的实际 Memory 输入来自 ContextBuilder 的 user_preferences/recalled_memories，再经过 project_interpreter_context。
- 因此旧 recall 是 production-reachable 的内部 legacy read，但不是当前 Interpreter 的 canonical prompt input。
- 它仍然造成第二套 Memory retrieval semantics 和额外 touch side effect，不能保留为生产路径。

### 收敛结果

收敛前：LEGACY_ACTIVE。  
收敛后：DEAD_CODE（私有 helper 保留为 DEPRECATE_CANDIDATE，无生产 invocation）。

本次移除 _execute_single 对 _recall_memories 的调用。生产模型-facing recall 现在只经过 ContextBuilder 配置的 canonical PreferenceRetriever。ConversationRuntimeAdapter 的无 retriever fallback 也已经改为 PreferenceRetriever，避免 test-safe fallback 在生产 composition 缺失时扩大到所有 MemoryType。ContextBuilder 同时跳过 canonical retriever 存在时的 legacy preference provider 读取，避免被丢弃的第二次 unfiltered recall。

### 其他 legacy retrieval symbols

|符号|状态|证据|
|---|---|---|
|PreferenceRetriever|ACTIVE|main.py wiring、ContextBuilder/ContextAssembler production path、focused tests|
|MemoryRelevanceGate|ACTIVE|PreferenceRetriever canonical path|
|MemoryRetriever|TEST_ONLY / 外部注入 UNKNOWN|仓库内使用点为 Memory-focused tests；production fallback 已改为 PreferenceRetriever；ContextBuilder 仍允许显式注入 generic retriever|
|StrategyRetriever|DEAD_CODE|仓库内只由已停用的 _recall_memories 使用|
|MemoryManager.recall|TEST_ONLY / compatibility API|仓库内 Runtime caller 已移除；旧单元测试仍覆盖 manager recall|

## Production Reachability

状态标签定义：

- ACTIVE：当前生产主链会调用。
- LEGACY_ACTIVE：收敛前有真实生产调用，但不属于 canonical path。
- TEST_ONLY：只有测试直接使用，未发现生产 caller。
- DEAD_CODE：仓库内无生产 caller；通常是私有旧 helper 或停用适配器。
- UNKNOWN：仓库内无 caller，但 public/外部 embedder 可能调用，不能据此删除。

|路径/入口|收敛前|当前|真实入口和结论|
|---|---|---|---|
|API completed Turn -> PreferenceMemoryService|ACTIVE|ACTIVE|routes.handle_run_result -> _extract_completed_turn_preference；这是当前 Preference write owner|
|TurnCoordinator/ContextAssembler -> PreferenceRetriever|ACTIVE|ACTIVE|main.py 将同一 retriever 注入 canonical ContextBuilder|
|ContextBuilder -> MemoryUserPreferenceProvider|ACTIVE（结果被丢弃）|DEAD_CODE / compatibility fallback|canonical retriever 存在时已跳过；仅无 canonical retriever 的兼容组合可用|
|ConversationRuntimeAdapter fallback -> PreferenceRetriever|UNKNOWN/潜在 legacy|ACTIVE（canonical gate）|只有 composition 缺失时才进入 fallback；现在仍经过 Preference gate|
|RuntimeAgentService._record_episodic|LEGACY_ACTIVE|DEAD_CODE|原来由 _execute_single terminal path 调用；当前无生产 caller|
|RuntimeAgentService._record_procedural|LEGACY_ACTIVE|DEAD_CODE|原来由 _execute_single terminal path 调用；当前无生产 caller|
|RuntimeAgentService._recall_memories|LEGACY_ACTIVE|DEAD_CODE|原来由 _execute_single 调用；当前无生产 caller|
|MemoryManager.remember_execution|LEGACY_ACTIVE（通过 Runtime hook）|TEST_ONLY / DEPRECATE_CANDIDATE|应用代码无直接 caller，旧 unit test 直接调用|
|MemoryManager.remember_pattern|TEST_ONLY|TEST_ONLY / DEPRECATE_CANDIDATE|仅旧 unit test 直接调用|
|MemoryManager.remember_correction|UNKNOWN|UNKNOWN / DEPRECATE_CANDIDATE|仓库内没有应用 caller；保留 public compatibility API，不在本阶段删除|
|ProceduralMemoryExtractor|LEGACY_ACTIVE（通过 Runtime hook）|DEAD_CODE / DEPRECATE_CANDIDATE|当前无 Runtime caller；定义仍保留|
|StrategyRetriever|LEGACY_ACTIVE（通过 Runtime hook）|DEAD_CODE / DEPRECATE_CANDIDATE|当前无 production caller|

结论：当前没有发现 production Memory read/write invocation 绕过 canonical Preference path。旧 helper 的实现仍可通过显式私有调用触发，但这不是 normal production route；它们已列入后续弃用候选，不在本阶段大量删除。

## Duplicate Truth Audit

### 收敛前

旧 Episodic record 同时保存：

- Task/goal 文本。
- Execution status。
- draft_id/schedule_id。
- execution_id。

旧 Runtime recall 再把这些字段映射为 recent_tasks。这样同一个业务事实同时存在于 Task、Execution/Observation/Resource 和 Memory 中，且 Memory 的 status 不具备当前业务状态的权威性。

旧 Procedural record 还把 step_count、tool_count、plan_source 和 success 写入 Memory，但没有明确的重复验证和经验抽象边界。

### 当前

当前状态的 authority 保持不变：

- Conversation：ConversationService/MessageRepository。
- Task/Objective：TaskProvider/TaskManager/Objective reducer。
- Execution/Step：Durable Execution Runtime。
- ActionObservation：ActionObservationStore，负责 terminal receipt 和 continuation。
- Run：AgentRunStore/RunExecutionLink，仅负责 run envelope 和映射。
- ResourceBinding/Draft/Schedule/Post：Task typed binding 及 Java business truth。
- Approval：ApprovalRuntimeService 和 durable approval store。
- Preference Memory：MemoryManager/Repository/PreferenceRetriever。

Runtime terminal path 现在只完成上述 current-state projection，不再自动生成 Episodic 或 Procedural Memory。因此没有新增 Task + Execution + Episode 的生产写入链。

### 历史数据风险

已有旧 EPISODIC 行不会因本次收敛自动删除或迁移。它们可能含有 status、draft_id、schedule_id，且旧写入没有完整 tenant contract。当前 canonical PreferenceRetriever 不会读取 EPISODIC，因此它们不进入正常 Preference Context；直接调用 legacy helper 仍可能看到它们。

这属于后续 retention/quarantine/migration 决策，不在本阶段删除数据。不得把历史行清理误认为当前 Runtime 收敛已经完成。

## Canonical Memory Contract

所有未来 Memory type 必须服从同一个运行时契约：

### Write contract

    Verified / allowed source
            ↓
    Memory Candidate
            ↓
    Memory Policy
            ↓
    canonical Memory Repository

写入必须具备：

- user_id scope。
- tenant_id scope。
- Memory 自己的 lifecycle status。
- provenance/source identity。
- candidate 与 current-state 的明确区分。
- 敏感字段和 metadata allowlist。
- source idempotency 或 semantic dedup。
- 写入失败不影响 Task/Execution convergence。

RuntimeAgentService 可以产生 verified source，但不能成为 Memory truth owner，也不能直接把运行状态包装成 Memory。

### Read contract

    Current Turn
            ↓
    canonical Memory Retriever
            ↓
    MemoryRelevanceGate
            ↓
    bounded Memory Context
            ↓
    ContextBuilder
            ↓
    Interpreter

在当前实现中，ContextBuilder 是检索和所有 durable projections 的编排点；实际执行顺序是 ContextBuilder 调用 canonical Retriever，接收 gate 后的 bounded result，再生成 ContextSnapshot 并交给 Interpreter。两种表示表达的是同一边界：Memory 不可直接写 Prompt。

读取必须具备：

- user_id + tenant_id 作用域。
- type-specific filter。
- ACTIVE/expiry filter。
- relevance/confidence filtering。
- no-match allowed。
- bounded item count and content length。
- provenance 可审计但不成为模型执行身份。
- Memory 只是 evidence/lesson，不覆盖 current user instruction、Task/Objectives、verified Observation 或 Java business truth。

优先级冻结为：

    Current explicit request
      > verified current business/runtime truth
      > Conversation context
      > Long-term Memory

禁止：

- 某种 Memory 自己直接写 Prompt。
- RuntimeAgentService 维护第二套 retrieval semantics。
- Memory 选择当前 target、ResourceBinding、tool、approval 或恢复 Run。

## Write Convergence

本阶段采用的最小修改：

1. 从 RuntimeAgentService._execute_single 移除 _recall_memories 调用。
2. 从同一 terminal path 移除 _record_episodic 调用。
3. 从同一 terminal path 移除 _record_procedural 调用。
4. 保留 Runtime 的 execution/result/observation/resource 状态处理不变。
5. 将 ConversationRuntimeAdapter 的无 retriever fallback 改为 PreferenceRetriever。
6. canonical retriever 存在时，ContextBuilder 跳过 legacy preference provider 的重复读取。

没有做以下操作：

- 没有实现 Episode Candidate Builder。
- 没有增加 Episodic/Semantic/Procedural 功能。
- 没有修改 Memory schema、storage schema、表或 vector collection。
- 没有重写 MemoryManager。
- 没有删除旧 helper、旧 public compatibility API 或旧 unit tests。

Write convergence verdict：当前生产 Runtime 不再自动把 Current State 写成 Memory；Preference write 仍由 PreferenceMemoryService/MemoryManager/Repository 负责。

## Recall Convergence

当前模型-facing Memory recall 只有 canonical ContextBuilder path：

- PreferenceRetriever 负责类型、scope、排序和 relevance gate。
- ContextBuilder 负责上限、压缩、no-memory 传播和 Snapshot。
- project_interpreter_context 负责安全投影。
- Interpreter 不接收 RuntimeContext.memory_context。

旧 Runtime recall 的生产调用已移除，未复制第二个 Gate。未来若开放 Episodic/Procedural read，必须在 canonical ContextBuilder boundary 增加 type-specific Retriever/Gate policy，不能重新启用 _recall_memories 或让 StrategyRetriever 直接进入 RuntimeContext。

## Episodic Source Boundary

本阶段不实现 Episode，只冻结 source contract：

    ActionObservation
      + terminal Task/Objective evidence
      + verified business outcome
            ↓
    Episode Candidate Builder
            ↓
    Worth remembering?
            ↓
    Episodic Memory

### 允许的来源

- 已提交且可验证的 terminal ActionObservation。
- terminal Task/Objective 结果作为候选判断依据，而不是复制的状态快照。
- verified business outcome。
- 明确的用户 correction/confirmation。

### Episode 的语义

Episode 是 verified history 的摘要/派生物，表达发生过且未来可能有帮助的经历或 lesson。它不是 Task snapshot、Execution snapshot 或 Run snapshot。

第一版应使用有界的去 ID 摘要和白名单 metadata，例如 event_kind、domain、outcome_class、lesson、reusability、confidence。source_type/source_id 只能作为 opaque provenance/idempotency reference。

### 禁止内容

以下字段不能成为 Episode 的长期语义主体、model-facing metadata 或检索理由：

- execution_id、operation_id、run_id。
- schedule_id、draft_id、post_id。
- execution/step/lease/checkpoint/retry/queue/reconciliation status。
- approval_id、approval decision、human-wait state。
- raw ActionObservation payload、工具参数、外部响应正文、秘密。

如果为了审计保留来源 reference，它只能证明“来源在哪里”，不能让 Episode 等于 Runtime 状态，也不能让 Episode 参与当前资源选择。

## Tests

只运行 Memory focused tests，未运行 L1/L2/L3、全量昂贵 evaluation、RAG/Search 大矩阵或外部 E2E。

最终 focused suite：

    uv run pytest -q tests/unit/test_memory_runtime_convergence.py tests/unit/test_preference_memory_retrieval.py tests/unit/test_memory_lifecycle.py tests/unit/test_memory_retriever.py tests/unit/test_agent_memory.py tests/unit/test_preference_memory_extractor.py tests/unit/test_preference_memory_storage.py tests/integration/test_context_memory_runtime.py

结果：59 passed。

覆盖内容：

- Preference canonical recall through MemoryRelevanceGate。
- Irrelevant Preference -> no-memory。
- user/tenant isolation。
- feature flag off 不读取、不 touch。
- Runtime terminal entrypoint 不再调用 legacy recall/Episodic/Procedural hooks。
- fallback 不再使用 generic MemoryRetriever，改走 PreferenceRetriever。
- 旧 MemoryManager compatibility tests 仍可运行，但被归类为 TEST_ONLY。

最终合计：59 passed。测试命令有一个 PytestCacheWarning，原因是当前环境无法写入 .pytest_cache；没有测试失败。

## Git Diff

保存审计报告的提交已经推送：

- commit：155168e
- message：docs: add memory v2 architecture checkpoint
- branch：feature/hybrid-search-rag

当前未提交修改：

- apps/agent_api/greenbook_agent_api/services/conversation_runtime_adapter.py
  - fallback generic MemoryRetriever -> PreferenceRetriever。
- packages/agent_core/greenbook_agent_core/execution/runtime_agent_service.py
  - 移除 legacy recall、Episodic write、Procedural write 的生产调用。
- tests/unit/test_memory_runtime_convergence.py
  - 新增 focused convergence contract tests。
- docs/reports/MEMORY_RUNTIME_CONVERGENCE_REPORT.md
  - 本报告。

git diff --check：通过。  
没有修改 main，没有 merge、reset、commit 或 push 本阶段代码修改。

## Remaining Legacy Candidates

|项目|分类|下一步|
|---|---|---|
|PreferenceMemoryService、PreferenceRetriever、MemoryRelevanceGate、ContextBuilder|KEEP|继续作为唯一 Preference production contract|
|RuntimeAgentService 的 _recall_memories|DEPRECATE_CANDIDATE|保持无 production caller；后续确认无外部 compatibility 后删除或改为明确 no-op|
|RuntimeAgentService 的 _record_episodic|DEPRECATE_CANDIDATE|不再从 Runtime 调用；未来 Episode 必须使用 Candidate Builder + Policy|
|RuntimeAgentService 的 _record_procedural|DEPRECATE_CANDIDATE|不再从 Runtime 调用；未来 Procedural 另行设计|
|StrategyRetriever|DEPRECATE_CANDIDATE|不能直接进入 RuntimeContext；未来统一到 canonical typed retrieval|
|MemoryManager.remember_execution|TEST_ONLY / DEPRECATE_CANDIDATE|旧测试仍保留；后续应迁移测试，不在本阶段删除|
|MemoryManager.remember_pattern|TEST_ONLY / DEPRECATE_CANDIDATE|同上|
|MemoryManager.remember_correction|UNKNOWN|仓库内无 caller；需先确认外部 compatibility|
|MemoryRetriever generic class|ADAPT_TO_CANONICAL|仅允许显式、受控的非模型-facing使用；模型-facing fallback 已收敛|
|历史 EPISODIC 行|BLOCKED/DEFERRED|等待 retention/quarantine/migration 决策；本阶段不删除数据|

删除策略结论：没有对 legacy 做大规模删除。当前保留项均有明确分类；后续只有在无 production caller、无 required compatibility 且有 targeted tests 证明安全时才删除。

## Verdict

MEMORY_RUNTIME_CONVERGED

生产 Memory Runtime 已收敛到唯一 canonical Preference read/write boundary：

- Preference 写入：PreferenceMemoryService -> MemoryManager -> Repository。
- Preference 读取：ContextBuilder -> PreferenceRetriever -> MemoryRelevanceGate -> bounded Context -> Interpreter。
- RuntimeAgentService 不再维护第二套 Memory retrieval semantics。
- RuntimeAgentService 不再把 Current State 自动复制成 Episodic/Procedural Memory。
- no-memory、user/tenant scope、lifecycle、provenance 和 bounded injection 保持有效。

旧 helper、compatibility API、旧测试和历史记录仍存在，但已不属于当前生产 Memory invocation。它们是后续明确的 DEPRECATE_CANDIDATE/TEST_ONLY，而不是新的生产 Memory Runtime。
