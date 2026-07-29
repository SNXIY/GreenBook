# CreatorAgentHarness Phase 2 实现说明

| 项目 | 内容 |
| --- | --- |
| 状态 | Implemented |
| 架构依据 | `docs/architecture/creator-intelligence-phase-1.md` |
| 实现范围 | Harness、Task/Run 生命周期、异步持久化、幂等、Outbox、lease、恢复 |
| 暂不包含 | Multi-Agent、LangGraph 图、HITL Resume、Memory、RAG、MCP Creator Tools、API |

## 1. 阶段目标

Phase 2 建立独立的 `CreatorAgentHarness`。Harness 作为 Creator 控制面，
为后续 Runtime 和 Agent 提供稳定生命周期。

本阶段解决：

- Creator Task 和 Run 的持久化生命周期。
- 创建与显式重试接口的幂等处理。
- Task、Run、Event、Idempotency、Outbox 的原子写入。
- Runtime 与 Harness 之间的类型安全端口。
- Worker lease、续租、过期接管和陈旧结果拒绝。
- 临时错误自动重试和失败后新 Run 重试。
- 排队取消和运行中协作取消请求。
- Runtime 输出大小、事件数量和 JSON 可序列化约束。

## 2. 模块边界

```text
app/creator/
├── domain/
│   ├── errors.py            # 稳定错误码
│   └── models.py            # Task、Run、Runtime Outcome、命令和值对象
├── application/
│   ├── ports.py             # Runtime 和 Unit of Work Protocol
│   └── harness.py           # CreatorAgentHarness
└── infrastructure/
    ├── database.py          # AsyncEngine、Session 和 UoW 工厂
    └── sqlalchemy.py        # ORM、Repository 和 UoW
```

依赖方向：

```text
domain <- application <- infrastructure
```

Harness 只依赖 Protocol，不依赖 LangGraph、具体 Agent、MCP 或模型供应商。

## 3. 命令边界

| Harness 方法 | 行为 |
| --- | --- |
| `create_task` | 原子创建 Task、Run、初始 Event、Start Outbox 和幂等记录 |
| `get_task` | 按 tenant 和 creator 范围读取 Task |
| `start_run` | 获取 Run lease，在事务外调用 Runtime，再投影结果 |
| `recover_run` | lease 过期后使用相同 thread ID 接管执行 |
| `renew_run_lease` | 长任务 Worker 续租 |
| `request_cancel` | 排队任务直接取消，运行中任务写入协作取消命令 |
| `retry_task` | FAILED Task 创建新的 Run 和 thread，保留旧 Run |

`start_run` 不由 HTTP 请求直接调用。生产环境中由消费
`creator.run.start` Outbox 的 Runtime Worker 调用。

## 4. 事务边界

### 4.1 创建任务

以下数据在同一事务提交：

```text
creator_tasks
+ creator_runs
+ creator_run_events(task.created)
+ creator_outbox_events(creator.run.start)
+ creator_idempotency_records
```

任意写入失败都会回滚。唯一键竞争被转换为
`CreatorPersistenceConflictError`，Harness 随后重新读取幂等记录并安全重放。

### 4.2 执行 Runtime

```text
事务 1：锁定 Task/Run -> 获取 lease -> RUNNING -> commit
事务外：调用 CreatorRuntimePort.start()
事务 2：验证 lease owner -> 投影 Outcome -> Event/Outbox -> commit
```

Runtime 调用期间不持有数据库事务或行锁。

### 4.3 乐观并发

- Task 与 Run 均包含 `version`。
- Repository 更新使用 `WHERE id = ? AND version = ?`。
- 更新行数不是 1 时返回持久化冲突。
- 人工操作使用 `expected_version`，后续 Phase 4 可直接复用。

## 5. 控制面数据表

| 表 | 责任 |
| --- | --- |
| `creator_tasks` | 用户可见聚合状态、目标、当前 Run、错误和最终 Artifact |
| `creator_runs` | 单次执行、内部 thread ID、执行次数、checkpoint 引用和 lease |
| `creator_run_events` | 按 Run 单调递增的持久化事件 |
| `creator_idempotency_records` | 请求哈希和原始响应 |
| `creator_outbox_events` | Start、Retry、Cancel 等待投递命令 |

Creator 表使用独立 `CreatorBase.metadata`。测试和本地开发可调用
`CreatorDatabase.create_schema_for_development()`；生产环境必须使用后续版本化迁移。

## 6. Runtime 协议

Runtime 必须实现：

```python
class CreatorRuntimePort(Protocol):
    name: str

    async def start(self, request: RuntimeStartRequest) -> RuntimeOutcome:
        ...
```

`RuntimeStartRequest` 包含内部 thread ID、Task/Run、租户、创作者、目标、Trace
和当前执行次数。thread ID 不通过客户端 API 接收。

允许的 Outcome：

- `COMPLETED`：必须包含 `final_artifact_id`。
- `WAITING_HUMAN`：预留给 Phase 4 的 checkpoint interrupt。
- `RETRYABLE_ERROR`：必须包含 retryable error。
- `FAILED`：必须包含非重试错误。

Harness 还会限制：

- Runtime Event 数量。
- 单个 Event 和 State Summary 的 JSON 字节数。
- Payload 必须可 JSON 序列化。
- 非法协议结果直接记为 `RUNTIME_CONTRACT_ERROR`，不进入重试循环。

## 7. Lease 与恢复

1. Worker 获取 Run 时写入 `lease_owner` 和 `lease_expires_at`。
2. 有效 lease 会拒绝其他 Worker，错误码为 `RUN_LEASE_CONFLICT`。
3. 长任务使用 `renew_run_lease` 续租。
4. lease 过期后新 Worker 可以调用 `recover_run`。
5. 恢复继续使用原 Run 的稳定 thread ID。
6. 新 Worker 接管后，旧 Worker 的结果因 owner 不匹配而返回
   `STALE_WORKER_RESULT`，不能覆盖新状态。

真正的 checkpoint 内容由 Phase 3/4 Runtime 管理，Harness 只持有 thread 和
checkpoint 引用。

## 8. 错误恢复

| 错误类型 | Harness 行为 |
| --- | --- |
| `CreatorRuntimeRetryableError` | RETRYING，同一 Run/thread 写入延迟 Start Outbox |
| 未预期 Runtime 异常 | 转换为 `RUNTIME_UNEXPECTED_ERROR` 后按临时错误处理 |
| Runtime 协议错误 | 直接 FAILED |
| 达到执行次数上限 | Task 和 Run FAILED |
| 显式 `retry_task` | 创建新 Run/thread，旧 Run 保持 FAILED |
| Worker 在接管后回写 | 拒绝陈旧结果 |
| 运行期间取消 | Outcome 投影时优先转换为 CANCELLED |

自动重试复用同一个 Run 和 thread。用户显式重试创建新的 Run 和 thread。

## 9. 数据库配置

本地默认：

```dotenv
CREATOR_DATABASE_URL=sqlite+aiosqlite:///./data/mindflow-creator.db
```

PostgreSQL 示例：

```dotenv
CREATOR_DATABASE_URL=postgresql+psycopg://mindflow:mindflow@127.0.0.1:5432/mindflow
```

Creator 业务只使用 `CREATOR_DATABASE_URL`。Phase 6 接入 Qdrant 作为 Creator
语义记忆和检索后端。

## 10. 测试矩阵

`tests/test_creator_harness.py` 使用真实异步 SQLAlchemy Repository 和 SQLite
数据库，Runtime 使用可控测试替身。

| 场景 | 断言 |
| --- | --- |
| 创建任务 | 五类记录原子生成 |
| 相同幂等请求 | 返回原 Task/Run，不重复写入 |
| 幂等键复用不同请求 | 明确冲突 |
| Runtime 成功 | 最终 Artifact、checkpoint 和事件正确投影 |
| 临时失败 | 相同 thread 自动重试 |
| 有效 lease | 阻止并发 Runtime 调用 |
| lease 过期接管 | 新 Worker 完成，旧结果被拒绝 |
| Runtime 协议非法 | 直接失败，不产生 Retry Outbox |
| 排队取消 | Runtime 不被调用 |
| 显式重试 | 新 Run attempt，旧失败 Run 保留 |

执行：

```bash
uv sync --frozen
python -m unittest discover -s tests -p "test_creator_harness.py" -v
ruff check app/creator tests/test_creator_harness.py
black --check app/creator tests/test_creator_harness.py
mypy app/creator --ignore-missing-imports
```

## 11. 后续阶段接口

- Phase 3 实现 `CreatorRuntimePort` 的 LangGraph Supervisor/Multi-Agent Runtime。
- Phase 4 增加 `resume_run`、Decision Repository 和 `Command(resume=...)`。
- Phase 5 将 Memory Gateway 注入 Runtime，不修改 Harness 生命周期。
- Phase 6/7 将 Retriever 和 MCP Tool Gateway 注入 Runtime。
- Phase 9 将持久化 Event 映射为 REST/SSE API。
- Phase 10 增加 Alembic、PostgreSQL Compose、Outbox Worker 和生产部署。

## 12. Phase 2 验收

- [x] Creator Harness 生命周期与 Runtime 端口保持独立。
- [x] 生命周期与 Runtime 解耦。
- [x] Task/Run/Event/Idempotency/Outbox 可持久化。
- [x] 创建、取消、自动重试、显式重试和恢复已实现。
- [x] tenant/creator scope 和乐观版本已校验。
- [x] 异步集成测试覆盖主要并发与失败路径。
- [x] Ruff、Black、Mypy 和 Compileall 可通过。
- [ ] LangGraph Runtime 由 Phase 3 实现。
