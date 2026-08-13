# GreenBook 技术债分析

## 1. 重复能力

### 1.1 多个 Tool Registry (已收敛)

- `TOOL_POLICY_CATALOG` in `packages/contracts/tool_contract.py` — **唯一权威**策略定义
- `_TOOLS` in `services/greenbook_mcp/tool_registry.py` — MCP handler 注册
- `ToolRegistry` in `packages/contracts/tool_contract.py` — metadata 投影 (LLM 可见)
- `build_tool_schemas()` in `apps/agent_api/api/tool_helpers.py` — **残留** OpenAI function 列表

**状态**: 策略已收敛到 `TOOL_POLICY_CATALOG`。`tool_helpers.py` 是 legacy 副本。

### 1.2 多个 Memory (已收敛)

- `packages/agent_core/memory/` — Agent 长期记忆 (EPISODIC/SEMANTIC/PROCEDURAL)
- `creator-agent/.../memory/` — Creator 三层记忆 (Redis/PG/Qdrant)

**状态**: 两个 Memory 系统服务于不同目的（Agent 记忆 vs Creator 创作者记忆），不是重复。

### 1.3 多个 Planner (已重构)

- `planning/dynamic.py` — DynamicPlanner (v2，运行时调整)
- `goal/compiler.py` — GoalCompiler (确定性编译)

**状态**: DynamicPlanner 和 GoalCompiler 各有职责（运行时调整 vs 确定性编译），不是重复。

---

## 2. 历史遗留

### 2.1 旧命名 (已清理)

| 旧名 | 新名 | 状态 |
|------|------|------|
| `assistant` | `agent` | 已重命名 |
| `conversation_runtime_adapter` | 保留 | 已收敛 |
| IntentSpec | Command | 已迁移 |
| TaskIntent | Task | 已迁移 |
| workflow template | GoalTree | 已迁移 |
| `agent.py` (CommunityOperationsAssistant) | AgentLoop | 已替换 |

### 2.2 兼容层 (保留待删除)

| 组件 | 位置 | 用途 | 删除时机 |
|------|------|------|----------|
| `compatibility/history/` | `packages/agent_core` | run_id ↔ execution_id 映射 | API 不再使用 run_id 时 |
| `RunExecutionAdapter` | `compatibility/history/` | 双向绑定 | 同上 |
| `run_store` (内存) | `apps/agent_api/routes.py` | 旧 Run 记录缓存 | Frontend 迁移到 execution API |
| `conversation_store` (内存) | `apps/agent_api/routes.py` | 旧 Conversation 缓存 | 全部迁移到 ConversationService |
| `approval_store` (内存) | `apps/agent_api/routes.py` | 旧 Approval 缓存 | 全部迁移到 ApprovalRuntimeService |

### 2.3 Deprecated API

| API | 位置 | 替代 | 状态 |
|-----|------|------|------|
| `POST /approvals/{id}/approve` | routes.py | `POST /executions/{id}/approve` | 旧路径仍可用 |
| `GET /runs/{run_id}` | routes.py | `GET /executions/{id}` | 已有 runtime_routes 提供 |
| `POST /runs/{run_id}/interrupt` | 已删除 | `POST /executions/{id}/pause` | 已迁移 |
| `/memories` / `/memory/episodes` | routes.py | 无 (stub 返回空) | 待实现或删除 |
| `agent-openapi.yaml` 旧端点 | contracts/ | 新 runtime 端点 | 待更新契约 |

### 2.4 残留目录

| 目录 | 内容 | 建议 |
|------|------|------|
| `packages/observability/` | 空壳 `__init__.py` | 删除或实现 |
| `apps/agent_worker/consumers/` | 空壳 | 删除或实现 |
| `apps/agent_worker/jobs/` | 空壳 | 删除或实现 |
| `packages/agent_core/skills/` | 空壳 | 删除 |
| `zhiguang-be/` | 历史 DDL | 只读, 不修改 |
| `archive/` | 历史代码 | 只读, 不修改 |

---

## 3. 架构风险

### 3.1 循环依赖风险 (已缓解)

```
agent_core
  ↑ 被 agent_api, agent_worker, evaluation 依赖
  ↓ 依赖 contracts, security (叶节点)

planning/__init__.py 使用 lazy __getattr__ 避免循环
task/__init__.py 使用 lazy __getattr__ 避免循环
runtime/__init__.py import-free (避免 ArtifactRegistry ↔ container 循环)
```

### 3.2 模块职责边界 (已收敛, 需持续关注)

| 边界 | 当前 | 风险 |
|------|------|------|
| Agent API ↔ Agent Core | API 不直接调用 core，通过 3 个边界服务 | 低 |
| Intelligence ↔ Execution | ExecutionInput 是唯一接缝 | 中 — 需确保 Worker 不接受 Command/raw text |
| Tool 注册 ↔ Tool 执行 | TOOL_POLICY_CATALOG ↔ MCP handlers | 低 — 已收敛 |
| Agent Memory ↔ Creator Memory | 两个独立系统 | 低 — 按业务分离 |

### 3.3 通信边界问题 (已有缓解)

| 问题 | 缓解 |
|------|------|
| Queue payload 可能暴露 tokens | `_execution_dispatch_payload` 剥离 tokens |
| 进程内 Worker 凭证丢失 | `ExecutionCredentialBroker` 内存存储 |
| 独立 Worker 凭证 | `GREENBOOK_AGENT_WORKER_ACCESS_TOKEN` |
| Execution lease 竞争 | `PostgresExecutionLeaseManager` SELECT FOR UPDATE |
| Queue claim 双消费 | PostgreSQL `rowcount` 防 double-claim |
| Agent API 双存储 (内存+PG) | 运行时优先 PG, 回退到内存 (兼容层) |

### 3.4 状态一致性问题 (已有缓解)

| 问题 | 缓解 |
|------|------|
| 执行崩溃 → 状态丢失 | Checkpoint + execution lease 回收 + CLAIMED→READY 回收 |
| 执行完成 → 投影未写 | 启动 reconcile: 恢复最近 100 条队列消息的投影 |
| 重试重复执行 tool | Ledger idempotency replay: COMPLETED → 直接重放 |
| 写操作副作用未知 | RECONCILE 后才允许重放 |
| Task 并发修改 | `TaskRepository.update` 乐观锁 (expected_version) |

### 3.5 已知功能缺口

| 缺口 | 影响 | 优先级 |
|------|------|--------|
| `observability` 包空壳 | 实际 trace/metrics 在 agent_core 内 | 低 — 功能未缺失 |
| Worker consumers/jobs 空壳 | 未实现 Kafka consumers | 低 — 未在 Roadmap |
| Memory API 端点返回 stub | `/memories` 等端点返回空 | 中 — 待实现 |
| `publish_now` handler 永远是 stub | 实际执行在 `publish_now_execute` (未注册) | 中 — 审批流已覆盖 |
| E2E 测试 `BLOCKED_BY_ENV` | 无可用的真实 USER 凭证 | 中 — 需配置测试用户 |
| Java `get_user_history` 未实现 | Creator 某些能力不可用 | 低 — 服务拒绝启动 |

---

## 4. 代码质量

### 历史债务已清理

- 所有 Deprecated intent 模块已标记 (compatibility/intent/*)
- 旧 agent.py (CommunityOperationsAssistant) 已淘汰
- 旧 workflow 模板系统 (orchestration/templates.py) 已淘汰
- 中文关键词路由 (`_CREATE_WORDS`, `_SCHEDULE_MARKERS` 等) 已被 LLM structured output 替换
- `capability.tools[0]` 固定选择已被 ToolSelector 替换

### 当前关注点

1. **Agent API 双存储**: 内存 dict + PG 服务的并存是兼容层，需在 Frontend 迁移后删除内存路径

2. **AgentLoop 复杂度**: `loop.py` (942 行) 是最大的单文件，可能需要拆分为更细粒度的 step 处理器

3. **RuntimeAgentService._execute_single**: ~500 行单方法，包含同步/队列/分离三种执行模式，复杂度较高

4. **`run_store` 投影**: 每次 Runtime 执行后都写一份 run_store 兼容投影，直到 Frontend 完全迁移到 execution API 后才能删除
