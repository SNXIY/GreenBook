# Current Memory Capability

审计性质：只读审计。除本报告外，本阶段没有新增 Memory 代码、数据库表、ContextBuilder、Agent Runtime 或 Prompt 修改。

结论先行：项目已经存在一套可运行的 Phase 6.6 Memory capability，包括 Memory domain、repository、PostgreSQL storage、write policy、retrieval、preference adapter、episodic/procedural extraction 和 API read surface。但它不是一个已经完整接入 canonical Agent path 的生产 Memory 产品：当前生产 Turn 链路把 long-term recall 明确设为关闭，部分旧 Runtime 会写入/读取 Memory，但读取结果没有成为当前 Interpreter/ActionLoop 的可靠决策输入。

当前判定：`MEMORY_CAPABILITY_EXISTS_BUT_RUNTIME_INTEGRATION_PARTIAL`。

## Existing Components

### 数据层与责任

| 组件 / 表 | 当前字段或载荷 | 当前职责 | 生命周期 |
| --- | --- | --- | --- |
| `assistant_conversations` | `conversation_id`, `user_id`, `tenant_id`, `title`, `timezone`, `active_*_id`, `recent_entities`, `recent_tool_calls`, `pending_approval`, `last_successful_run_id`, `conversation_summary`, `version`, timestamps | Conversation 聚合、认证作用域、短期会话绑定和可压缩的对话摘要 | 创建后长期存在；每次 session/context 更新递增 version；删除 Conversation 时结束 |
| `assistant_messages` | `message_id`, `conversation_id`, `role`, `content`, `parts`, `run_id`, `execution_id`, `trace_id`, `version`, `created_at` | 用户/助手可见消息历史和结果 projection | 用户消息与助手结果追加；达到阈值后旧消息折叠进 `conversation_summary`，原始旧行被 trim，保留最近消息 |
| `assistant_tasks` | owner identity、`goal`、`status`、`phase`、`objectives`/`goals`、`revisions`、`execution_refs`、`resource_index`、`plan_history`、version/timestamps | 长生命周期的业务目标、Objective 状态、依赖、业务资源绑定和计划修订 | 跨 turn、可恢复；Task/Objectives 是业务 truth，不是 Memory |
| `agent_action_observations` | `observation_id`, unique `execution_id`, `task_id`, `conversation_id`, `run_id`, `goal_id`, capability/status、business result、resource/artifact refs、payload、`PENDING/DISPATCHED/DONE` | 一次 durable Execution 的业务收据、继续运行所需的已验证结果和幂等消费边界 | Execution 完成后写入；consumer claim/ack；可重试，不能替代 Execution/Operation truth |
| `agent_memories` | `memory_id`, `user_id`, optional `conversation_id`/`task_id`, `memory_type`, `content`, `structured_metadata`, `importance`, `confidence`, `source_type`/`source_id`, created/updated/access fields, `expires_at` | 用户级可复用的长期记忆记录；支持 `EPISODIC`、`SEMANTIC`/`PREFERENCE`、`PROCEDURAL` | 由 MemoryManager 写入；可检索、touch、forget、过期过滤；当前没有独立 purge/retention worker |
| Context projection | `ContextSnapshot`、`DerivedConversationContext`、`AssembledTurnContext`，无独立 context 表 | 把 Conversation、Task、Execution、Artifact、Observation、Preference、Memory 组合成有预算的单 turn working set | 每 turn 重建、bounded、原则上丢弃；repositories 才是 source of truth |

对应代码与 schema：

- Conversation / Message / Context 表映射在 `packages/agent_core/greenbook_agent_core/db/repositories.py`。
- Conversation summary 与 message compaction 在 `packages/agent_core/greenbook_agent_core/conversation/service.py`。
- Task/Objectives domain 在 `packages/agent_core/greenbook_agent_core/task/models.py`，持久化在 `task/registry.py` / `task/provider.py`。
- ActionObservation 在 `packages/agent_core/greenbook_agent_core/execution/action_observation.py`。
- Memory domain、retriever、policy、manager 在 `packages/agent_core/greenbook_agent_core/memory/`；`agent_memories` 由 `db/migrations/008_context_durable_memory.sql` 建立。

### 已存在的 Memory 代码能力

- `MemoryRecord` 支持用户、Conversation/Task provenance、类型、内容、结构化 metadata、重要性、置信度、来源、访问计数和过期时间；不包含 hidden chain-of-thought 字段。
- `InMemoryMemoryRepository` 用于测试和 memory profile；`PostgresMemoryRepository` 提供 `agent_memories` 的异步持久化、查询、touch 和 delete。
- `MemoryManager` 提供 `remember`、`recall`、`forget`、preference、execution outcome、correction、reusable strategy 等 facade，并按 `source_type/source_id` 做进程内去重。
- `MemoryWritePolicy` 只允许显式偏好、用户纠正、请求记住、任务重大结果和可复用策略等事件写入；普通事件默认跳过。
- `MemoryRetriever` 默认是关键词候选 + deterministic rerank；可注入 semantic candidate provider，但当前没有实际 memory embedding pipeline。
- `MemoryUserPreferenceProvider` 把 semantic memory 映射为稳定偏好，并尝试从 schedule 语义观察偏好发布时间。
- `ProceduralMemoryExtractor` 与 `StrategyRetriever` 已存在；旧的 `RuntimeAgentService` 在 execution completion 时写 episodic/procedural memory。
- API 只有只读的 `/memory/settings` 和 `/memory/records` surface；没有发现完整的用户显式记忆写入、删除、确认或 retention 管理 API。

## Current Data Flow

### Canonical production turn

```text
User Message
  -> POST /conversations/{conversation_id}/messages
  -> ConversationService.load()
       -> conversation_summary + bounded recent assistant_messages
  -> persist current user message + create durable AgentRun
  -> background runner
  -> TurnCoordinator.execute()
  -> ContextAssembler
  -> ContextBuilder
       -> Conversation projection
       -> TaskProvider / Task / Objective / Artifact / Resource projection
       -> Execution projection
       -> ActionObservation receipt projection
       -> preference projection
       -> optional MemoryRetriever projection
  -> CommandContext
  -> CommandInterpreter
  -> Fast Path or ActionLoopExecutor
  -> ActionLoop / Durable Runtime
  -> Completion projection
       -> assistant_messages
       -> Conversation session bindings
       -> ActionObservation / Task projections
```

具体行为如下：

1. `routes.py` 的 `_prepare_message_history` 从 `ConversationService` 取摘要和最近 user/assistant 消息，然后追加当前 user message。后台 runner 传给 `TurnCoordinator` 的 `history` 是 `None`，但 `ContextBuilder` 会再次从 durable Conversation 读取同一份 bounded history，因此不会依赖进程内 message cache。
2. `ContextBuilder` 是唯一的 join/projection 位置。它读取 Conversation、Task、Execution、Artifact、Observation、Preference，并按 `ContextBudget` 限制消息、摘要、Task、Objective、artifact、resource、verified outcome 和 memory 大小。
3. `AssembledTurnContext.to_command_context()` 把 summary/history、active tasks、unfinished goals、targets 和 verified outcomes 交给 Interpreter；`project_interpreter_context()` 在发给 provider 前递归移除 canonical identity，防止模型自行复制内部 ID。
4. `CommandInterpreter` 只产生结构化 Command 和 semantic facts；之后由 resolver、FastPathGate、Task/Objectives admission 和 ActionLoop 决定执行，不把 Conversation 或 Memory 当成业务状态源。
5. ActionLoop 的核心输入是 Task、Objective、Command、已绑定的 Resource/Execution 事实和当前 observation。它不拥有恢复 truth；恢复由 Task/Objectives、Execution、Operation 和 ActionObservation 完成。

### Memory 在这条链路上的实际状态

- `main.py` 创建了 `MemoryManager`、`MemoryRetriever` 和 `MemoryUserPreferenceProvider`，并把它们注入 ContextBuilder / ConversationRuntimeAdapter / RuntimeAgentService。
- 但 `TurnCoordinator` 的 `ContextAssembler.assemble()` 默认 `memory_recall=False`；`ConversationRuntimeAdapter._build_context_snapshot()` 也显式传 `memory_recall=False`。因此 canonical User Message → ContextBuilder → Interpreter → ActionLoop 路径默认会记录 `memory_recall_skipped`，`recalled_memories` 为空。
- `ContextSnapshot` 虽然有 `user_preferences`、`recalled_memories`、`memory_ids_used` 字段，但 `project_interpreter_context()` 没有把这些字段作为 provider-facing semantic input 输出；它主要输出 history、summary、Task/Goal/target 的去身份化语义证据。
- `ActionLoop` 注入了一个 context assembler，但其核心 `_observe()` 以 `assemble(task=task, command=command)` 调用；生产 `ContextAssembler` 的 canonical 参数是 conversation/user/tenant 等。若调用不匹配，ActionLoop 会回退到 `_project_task()`，该 fallback 只含 Task/Objectives/artifacts/resources/execution statuses，不含长期 Memory。
- 另有旧的 `RuntimeAgentService._recall_memories()`，会把 preference、recent task 和 strategy 放入 `RuntimeContext.memory_context`；搜索结果显示该字段在这条路径中主要被填充，没有形成当前 Interpreter/ActionLoop prompt 的明确消费边界。该 Runtime completion path 同时会调用 `_record_episodic()` 与 `_record_procedural()` 写 Memory。

因此，当前真实状态是：Memory 写入存在，独立检索存在，Context 结构预留存在，但 canonical Agent decision path 尚未默认消费长期 Memory。这是“已有能力但未完整产品化”，不是“Memory 已经完整上线”。

## Existing Storage

### Conversation / short-term memory

- `assistant_conversations.conversation_summary` 是当前唯一的 durable conversation summary 字段，不是独立 summary 表。
- `ConversationService` 默认保留最近 12 条 user/assistant message；达到 24 条时，将旧消息交给 summary builder 或 deterministic line merge，限制到 6000 字符，再 trim 原始旧 message。
- `ContextBuilder` 再按本 turn budget 把 summary 限到 4000 字符；`TurnBudget` 的 provider-facing summary 默认限到 2000 字符。
- `recent_entities`、`recent_tool_calls`、`active_*` 和 `pending_approval` 是会话导航/绑定状态，不是用户长期 Memory，也不应被抽成第二套事实表。

### Task / Objective / Execution / Observation

- Task/Objectives 记录当前和历史业务目标、Objective 状态、依赖、verified resource binding 和 plan revision。
- Execution / Operation / checkpoint / lease / queue 记录运行状态、重试、未知结果和副作用边界。
- ActionObservation 是完成后的 receipt/read model，用来恢复下一轮，但不是长期偏好或用户画像。
- `assistant_runs` 在 migration 001 后被限制为 legacy history/projection；Runtime status 不应写回它。真正的 execution truth 仍由现有 Runtime persistence 负责。

### Long-term Memory

- PostgreSQL 模式下，`main.py` 启动时创建 `PostgresMemoryRepository` 并调用 `ensure_storage()`；`ConversationService.ensure_storage()` 随后通过 migration runner 应用 migration 008。两处都使用 `IF NOT EXISTS`，现在可以工作，但 schema ownership 分散在 repository DDL 和 migration 两套位置。
- `MemoryManager` 默认仍持有一个 `InMemoryMemoryRepository`；生产 wiring 把 PostgreSQL repository 作为 `durable_repository` write-through shadow，而不是 MemoryManager 的 primary repository。
- 这导致 preference provider 读取的是 MemoryManager 的进程内 store，而 canonical `MemoryRetriever` 在 PostgreSQL 模式读取的是 durable repository。重启、durable write 延迟或 durable write 失败时，两者可能不一致。
- `MemoryRecord.embedding` 字段存在，但当前 repository/retriever 不生成、不查询 embedding；实际 Memory retrieval 是 lexical keyword candidate + deterministic scoring，不是 semantic memory retrieval。
- `agent_memories` 有 `expires_at` 读取过滤，但没有独立过期清理任务；也没有观察到以 `(user_id, source_type, source_id)` 为唯一键的数据库约束。当前去重主要由 `MemoryManager` 的应用层查找完成。

## Missing Capability

当前已存在的骨架之外，以下能力仍缺失或不完整：

1. **Canonical durable write owner**：需要明确 PostgreSQL 是否是唯一 Memory truth，避免 in-process primary + durable shadow 的双写分叉。
2. **统一的 memory event/write pipeline**：`remember_correction()`、`observe()`、procedural extraction 等存在，但没有一个覆盖显式记忆请求、纠正、偏好确认和执行后提炼的统一 durable event contract。
3. **真正的 production recall boundary**：当前 canonical turn 默认跳过 recall；Memory 是否参与某个 capability 仍没有 feature flag、policy、reason、provenance 和可审计的消费协议。
4. **语义检索能力**：现有 `embedding` 是 contract 预留，不是已实现的 encoder/vector index；当前 Memory 检索不支持可靠的语义 paraphrase。
5. **用户画像/偏好治理**：没有独立 profile domain、冲突合并、用户确认、撤回、删除 API 和完整 retention/consent workflow；`/memory/records` 目前主要是展示。
6. **租户边界**：`MemoryRecord` 与 `MemoryQuery` 只有 `user_id`，没有 `tenant_id`。如果 user ID 不是全局唯一，跨 tenant 的长期 Memory 隔离没有显式 schema 约束。
7. **结果质量与安全评估**：没有看到完整的 memory precision/recall、错误记忆、过期、prompt injection、跨 Conversation/tenant leakage 和 restart recovery benchmark。
8. **事实/记忆区分的强制约束**：现有 episodic record 会携带 execution outcome、draft_id、schedule_id 等历史 metadata。这可以作为不可变 provenance/历史摘要，但不能被解释成当前 Task、Execution 或 Resource 状态；目前这个边界主要靠约定和调用位置维护。

## Boundary Design

| 领域 | 应负责 | 不应负责 |
| --- | --- | --- |
| Conversation | 用户/tenant/conversation identity、消息时间线、短期 history、摘要、active binding 和 UI session continuity | 不拥有 Task lifecycle、Execution status、Resource truth；`active_*` 只是导航指针 |
| Task / Objective | 用户长期业务目标、Objective 状态、依赖、desired state、verified resource binding、plan revision | 不把 Memory 作为完成依据；不把当前状态复制到 Memory |
| Execution / Operation | 运行、队列、重试、lease、side effect、unknown/reconciliation 和 execution lifecycle | 不负责用户长期偏好或跨 Conversation reusable knowledge |
| ActionObservation | 一次 Execution 的已验证业务 receipt、continuation claim、bounded recent outcome projection | 不作为第二套 Execution truth；不提炼成当前 Task status |
| Future Memory | 用户级、可复用、可解释、可撤回的 preference/fact/episodic/procedural learning；保留来源和置信度，作为可选 decision input | 不存储或裁决当前 task/execution/resource 状态；不替代 Task/Objective/Observation/Operation；不直接选择 target |

Memory 中允许出现 `task_id`、`execution_id`、`draft_id` 等字段时，应只作为来源/历史 provenance 或审计引用。任何“现在是否完成、当前是否运行、资源当前是什么状态”的判断必须回到 canonical Task/Execution/Java business truth。

Conversation summary 与 Memory 也要分开：summary 是某个 Conversation 的压缩历史；Memory 是跨 Conversation、用户级、经过 policy 的可复用记录。不能把 summary 当 profile，也不能把 Memory 反写成 Conversation 的当前状态。

## Recommended Implementation Plan

以下是下一阶段建议，不是本阶段实施内容：

1. **先冻结边界 contract**：写一份 Memory ownership ADR，明确 `agent_memories` 的 canonical owner、allowed memory types、provenance 字段和“不得存当前状态”的测试规则。
2. **收敛持久化**：以 durable repository 为生产 primary；如果保留本地 cache，必须是明确的 read-through/cache，而不是另一份可检索 truth。把 source idempotency 变成数据库可保证的约束或事务性 upsert。
3. **定义 durable write events**：仅接入用户显式记住、用户确认/纠正、完成后的可复用 outcome/strategy 等事件；把 Task/Execution/Resource 只作为 source reference，不复制其 live state。
4. **单独开启 recall**：在 ContextBuilder 设立显式 feature flag/capability policy，先只注入经过 compact、带 `memory_id/source/confidence` 的 semantic evidence；Resolver 仍只使用 canonical target projection，Memory 不能直接提供 target identity 或执行许可。
5. **先做 preference/profile vertical slice**：提供读取、确认、撤回、删除和审计；验证跨 Conversation、跨 restart、跨 tenant isolation，再扩展 episodic/procedural memory。
6. **再评估 semantic memory**：如果 lexical recall 不够，再单独设计 encoder/index；不要把当前 RAG 的 `posts_dense` 或 `post_chunks_dense` 当作 Memory storage，也不要改变现有 RAG 架构。
7. **建立 quality/recovery gate**：覆盖 write idempotency、restart、durable failure、expiry/delete、duplicate/stale event、cross-tenant isolation、prompt injection 和 decision usefulness；在 recall 没有证明收益前保持关闭。

## Risks

- **双存储不一致**：`MemoryManager` 的 in-process repository、durable repository 和 preference provider 的读取路径不同，可能造成“刚写入可见、重启后不可见”或偏好与 durable records 不一致。
- **Recall 实际未接入 canonical path**：未来直接把 `recalled_memories` 填入 prompt 可能形成未经评估的新决策输入；必须先定义注入位置、预算、来源和失败策略。
- **ActionLoop 边界混淆**：把 Memory 放入 ActionLoop 的任务状态会制造第二套 truth，破坏恢复和幂等；Memory 只能是可选的语义 evidence。
- **历史 outcome 被误读为 live state**：episodic metadata 中的 status/resource IDs 只能表示过去某次结果，不能用于当前状态判断或 target resolution。
- **隔离不足**：Memory schema 没有 tenant_id，retrieval 也以 user_id 为主要 scope；未来必须显式验证租户隔离。
- **schema ownership 重复**：migration 008 与 `PostgresMemoryRepository.ensure_storage()` 同时声明表结构，后续字段演进可能只更新一处。
- **检索质量有限**：当前是关键词检索；`keywords`、metadata filter、expiry 和 optional provider 的行为并不完全一致，不能将单元测试通过等同于生产 Memory recall 已证明。
- **治理能力不完整**：目前缺少完整的用户控制、删除/撤回入口、purge job、retention/consent 和 memory quality audit。
- **旧路径重复设计**：`AgentLoop`/`RuntimeAgentService` 仍保留 `memory_snapshot`/`memory_context` 等兼容字段；新增 Memory 功能时必须复用现有 Memory contract，不能再建立第二个 summary/profile/fact store。
