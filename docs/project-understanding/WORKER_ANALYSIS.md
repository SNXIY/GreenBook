# GreenBook Agent Worker Analysis

## 1. 定位

Agent Worker (`apps/agent_worker`) 是一个**轻量级的组装层**，负责将 Agent Core 的执行组件组装成一个独立部署的后台进程。

Worker 本身不包含任何业务逻辑——所有队列消费、重试、租约、执行逻辑都来自 `packages/agent_core/execution/`。

---

## 2. 项目结构

```
apps/agent_worker/
├── pyproject.toml
└── greenbook_agent_worker/
    ├── main.py                  # 进程入口 + 组装
    ├── execution_handler.py     # 重新导出 RuntimeExecutionQueueHandler
    ├── consumers/               # (空壳)
    └── jobs/                    # (空壳)
```

---

## 3. Worker 进程结构

```
main()
  │
  ├─ 1. 加载 .env
  ├─ 2. JavaClient (默认 http://127.0.0.1:8080)
  ├─ 3. RuntimeContainer.from_env() (决定 memory/postgres 持久化)
  ├─ 4. 重试栈:
  │     MemoryMetricsCollector
  │     ExecutionStateManager
  │     RuntimeManager
  │     RetryManager
  │     RetryScheduler
  │     RetryBackgroundWorker
  │
  ├─ 5. 执行队列消费者 (如启用):
  │     CreatorClient (默认 http://127.0.0.1:8092)
  │     GreenBookMCPServer
  │     AsyncOpenAI (DeepSeek)
  │     ConversationService
  │     MemoryManager
  │     TaskProvider
  │     ExecutionCompletionPublisher
  │     RuntimeExecutionQueueHandler
  │     ExecutionQueueWorker
  │
  ├─ 6. 启动前 reconcile: 恢复最近 100 条队列消息的投影
  │
  ├─ 7. 健康心跳 (文件, 每 15s)
  │
  ├─ 8. 并发运行:
  │     retry_worker.run()
  │     execution_queue_worker.run()
  │
  └─ 9. shutdown: 释放所有 claim + lease + 关闭连接
```

---

## 4. Queue: 执行队列

### 队列消息模型

```python
ExecutionQueueMessage:
  message_id: str         # UUID
  execution_id: str       # 唯一, 一个 execution 一条消息
  status: READY|CLAIMED|ACKED|FAILED
  claimed_by: str         # worker_id
  claim_until: datetime   # 租约过期时间
  attempt: int            # 消费尝试次数
  last_error: str         # 最后一次失败原因
  payload: dict           # 执行上下文 (无 secrets!)
  trace_id: str           # 追踪 ID
  available_at: datetime  # 可用时间 (延迟消息)
  created_at: datetime
```

### 队列语义

```
enqueue(execution_id, payload, requeue=False)
  │
  ├─ 首次: INSERT message (READY)
  ├─ requeue=True: UPSERT (重置为 READY)
  └─ 幂等: 相同 execution_id 返回已有

claim(now, worker_id, lease_seconds, batch_size)
  │
  ├─ 回收过期 CLAIMED (→ READY)
  ├─ 选择 READY (按 available_at, created_at, message_id 排序)
  └─ 原子 UPDATE: READY → CLAIMED (PostgreSQL: rowcount 防双 claim)

ack(message_id, worker_id)
  └─ CLAIMED → ACKED

fail(message_id, worker_id, error)
  └─ CLAIMED → FAILED (记录 last_error)

release(message_id, worker_id)
  └─ CLAIMED → READY (保留 attempt 计数)
```

---

## 5. Worker: 执行队列消费者

### ExecutionQueueWorker 循环

```
run():
  while not stopped:
    run_once()
    await wait(poll_interval, or stop_event)

run_once():
  messages = queue.claim(now, worker_id, lease, batch_size)
  for message in messages:
    1. 记录 claim
    2. 如果 stopping → release
    3. 获取 execution lease (防跨进程并发)
       失败 → release
    4. handler(message)  # RuntimeExecutionQueueHandler
       成功 → ack
       ExecutionHandlerDeferredError → release  # 凭证就绪后重试
       其他异常 → fail + 释放 lease
    5. 释放 execution lease
```

**关键设计**：Worker 只负责队列投递语义，永远不直接调用 Tool/Planner/MCP/Java。

---

## 6. Execution: 执行生命周期

### Execution 状态机

```
                         ┌─────────────────────────┐
                         │         PENDING           │
                         └───────────┬───────────────┘
                                     │ init_execution
                                     ▼
                         ┌─────────────────────────┐
                         │         RUNNING           │
                         └───────────┬───────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              │                      │                      │
              ▼                      ▼                      ▼
    ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
    │    COMPLETED     │   │ WAITING_APPROVAL│   │     FAILED      │
    └─────────────────┘   └────────┬────────┘   └────────┬────────┘
                                   │                      │
                                   ▼                      ▼
                          ┌─────────────────┐   ┌─────────────────┐
                          │  approve_resume │   │  RETRY (retry)  │
                          │  → RUNNING      │   │  → RUNNING      │
                          └─────────────────┘   └─────────────────┘
                                                         │
                                                         ▼
                                                ┌─────────────────┐
                                                │    CANCELLED    │
                                                └─────────────────┘
```

### Step 状态机

```
PENDING → READY → RUNNING → COMPLETED
                         → FAILED_RETRYABLE → (Retry) → PENDING
                         → FAILED (permanent)
                         → WAITING_APPROVAL → RUNNING (approved)
                         → SKIPPED (upstream failed)
                         → WAITING_ASYNC (long-running tool)
```

### 队列消息 + Execution 双生命周期

```
API:
  submit_plan → init_execution(PENDING)
  → enqueue(READY)

Worker:
  claim → READY→CLAIMED
  → start_execution(PENDING→RUNNING)
  → run() → COMPLETED/FAILED/...
  → ack → CLAIMED→ACKED
```

---

## 7. Checkpoint: 执行检查点

### 检查点内容

```python
ExecutionCheckpoint:
  execution_id: str
  plan_id: str
  completed_steps: list[str]       # 已完成步骤 ID
  current_step: str                # 当前步骤 ID
  step_results: dict[str, dict]    # 步骤 tool_result (仅 COMPLETED)
  snapshot: dict                   # 可序列化状态快照
  created_at: datetime
```

### 检查点用途

1. **崩溃恢复**：Worker 重启后 replay `completed_tool_result`，不重复执行已完成步骤
2. **审批恢复**：暂停在 WAITING_APPROVAL，审批通过后从检查点恢复
3. **重试安全**：重试前将步骤重置为 PENDING，但保留已完成步骤的结果

---

## 8. Ledger: 幂等执行账本

```python
ToolExecutionLedger:
  entries: dict[idempotency_key, ToolLedgerEntry]
  
ToolLedgerEntry:
  idempotency_key: str            # invoke:{tool_name}:{sha256}
  status: COMPLETED|FAILED|TIMEOUT|IN_PROGRESS
  result: dict                    # tool_result
  started_at: datetime
  completed_at: datetime
```

账本只重放 COMPLETED 状态的条目，FAILED/TIMEOUT 不重放（需要重新执行）。

---

## 9. Retry: 重试机制

### 两阶段重试

```
执行失败
  │
  ├─ FailureDecisionEngine: 分析失败原因
  │   ├─ 可重试 → FAILED_RETRYABLE
  │   └─ 不可重试 → FAILED (permanent)
  │
  ├─ RetryDecisionEngine: 决定重试策略
  │   ├─ 检查 evidence.request_sent (是否已发送)
  │   ├─ 检查 evidence.side_effect_state (副作用状态)
  │   ├─ 检查 retry budget (剩余次数)
  │   └─ 计算 backoff (指数退避, 上限 10min)
  │
  └─ RetryScheduler.schedule_decision()
      └─ 持久化 RetryTask (READY)

RetryBackgroundWorker:
  claim_due(tasks) → claim RETRYABLE tasks
    for each:
      1. 验证队列消息状态
      2. RetryManager.retry_step()
         → ExecutionStateManager.retry_step (FAILED_RETRYABLE → PENDING)
         → 保存 checkpoint
      3. execution_queue.enqueue(requeue=True)
         → 消息重置为 READY
      4. complete(retry_task)
```

### 重试安全原则

- **Fail-closed**: 不确定时拒绝重试
- **request_sent 不为 False**: 请求已发出，需对账
- **副作用已开始**: 强制 RECONCILE 后才能重试
- **只有 transient 类别可重试**: TIMEOUT, NETWORK_ERROR, RATE_LIMIT, TEMPORARY_UNAVAILABLE

---

## 10. Recovery: 崩溃恢复

```
Worker crash / restart:
  │
  ├─ 1. CLAIMED 队列消息 → 租约过期 → 回收 → READY (其他 Worker 可领取)
  ├─ 2. CLAIMED 重试任务 → 租约过期 → 回收 → READY
  ├─ 3. Execution lease → 过期 → 其他 Worker 可获取
  ├─ 4. 新 Worker 领取消息:
  │     resume_execution(execution_id)
  │       → RUNNING/FAILED_RETRYABLE steps → PENDING
  │       → COMPLETED steps 保留 (checkpoint replay)
  │     → run() 继续执行
  │
  ├─ 5. Checkpoint replay:
  │     已完成步骤的 completed_tool_result → 直接应用
  │     避免重复调用 tool (幂等保护)
  │
  └─ 6. 启动 reconcile:
       恢复最近 100 条队列消息的 CompletionProjection
       确保已完成但未 ACK 的执行不会丢失投影
```

---

## 11. Lease: 执行租约

```
Execution Leash:
  key: execution_id
  holder: worker_id
  expires_at: now + lease_seconds

acquire(execution_id, worker_id, ttl):
  PostgreSQL: SELECT ... FOR UPDATE + INSERT/UPDATE
  内存: RLock + dict
  失败 → 其他进程持有该执行

release(execution_id, worker_id):
  仅持有者可释放
```

防跨进程重复执行。Queue claim 是第一层防护，Execution lease 是第二层。

---

## 12. 健康检查

```
写入文件: .runtime/agent-worker-health.json

{
  "status": "READY",
  "updated_at": "2026-08-12T10:00:00Z",
  "pid": 12345,
  "storage": "postgres",
  "queue_consumer": true,
  "worker_id": "agent-retry-worker"
}

心跳间隔: 15s (可配置)
就绪检查: status == "READY" && updated_at < 90s 前
```

---

## 13. 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `GREENBOOK_AGENT_RETRY_POLL_INTERVAL_SECONDS` | 1 | 重试轮询间隔 |
| `GREENBOOK_AGENT_RETRY_BATCH_SIZE` | 20 | 每次重试批量大小 |
| `GREENBOOK_AGENT_RETRY_LEASE_SECONDS` | 60 | 租约 TTL |
| `GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER` | true (postgres) | 启用队列消费 |
| `GREENBOOK_AGENT_WORKER_ACCESS_TOKEN` | 必填 | Worker 服务凭证 |
| `GREENBOOK_AGENT_WORKER_HEALTH_FILE` | `.runtime/agent-worker-health.json` | 健康文件 |
