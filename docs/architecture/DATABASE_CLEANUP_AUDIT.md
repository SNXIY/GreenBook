# Database Cleanup Audit

## Scope

只读扫描了 `packages/assistant_core`、`apps/assistant_api`、`apps/assistant_worker`、`apps/backend`、`apps/creator-agent`、`services` 和 migrations/SQL 定义。

## ACTIVE 数据模型

`packages/assistant_core/greenbook_assistant_core/execution/persistence.py` 定义了 Runtime 持久化表：

- `execution`
- `execution_step`
- `execution_event`
- `checkpoint`
- `execution_lease`

这些表由 `PostgresExecutionRepository`、persistent event/checkpoint stores 和 lease manager 使用，属于 KEEP。它们分别保存 `PlanExecution`、步骤、canonical events、非权威 checkpoint 和 worker lease。

`assistant_conversations`、`assistant_messages`、`assistant_approvals` 仍被 Assistant API、Human approval 和历史对话读取使用，当前属于 KEEP/COMPATIBILITY，不能删除。

## Legacy 表与字段

`packages/assistant_core/greenbook_assistant_core/db/repositories.py` 定义：

- `assistant_runs`
- `RunRepository`
- `run_id`

`assistant_runs` 仍由 `routes.py::send_message()` INSERT，并由 Legacy-only cancel/interrupt 路径 UPDATE；`/runs`、Legacy fallback、旧前端和 E2E 仍读取它。结论为 MIGRATE，暂不删除。

`assistant_runs.events` 对 Runtime-backed 请求不应继续作为事件源，但 Legacy-only 历史查询仍需要。后续应先完成 projection/read API 迁移，再冻结为只读历史字段。

## Schema / migration 风险

扫描未发现独立的 Assistant Runtime Alembic migration 覆盖 `assistant_runs` 或新的 execution tables；当前部分 schema 通过 SQLAlchemy `metadata.create_all()` 创建。`create_all()` 不会修改已存在生产表，因此不能把 Python table definition 变更当作生产 migration。

删除 `assistant_runs` 前必须完成：

1. 生产流量确认无 Legacy-only run。
2. `/runs` API 和旧 SSE/approval/cancel contract 完成退役。
3. 历史数据导出、保留期限和恢复演练完成。
4. 独立数据库 migration、回滚脚本和索引影响评估完成。

## 分类

| 资源 | 分类 | 原因 |
|---|---|---|
| `execution`, `execution_step`, `execution_event`, `checkpoint`, `execution_lease` | KEEP | ACTIVE Runtime canonical persistence |
| `assistant_conversations`, `assistant_messages` | KEEP | API conversation/history |
| `assistant_approvals` | KEEP / MIGRATE | approval contract 已支持 execution reference，Legacy 仍需兼容 |
| `assistant_runs` | MIGRATE | 当前仍有生产 API、fallback、历史查询引用 |
| `RunRepository` | MIGRATE | 仍是 `assistant_runs` 的唯一访问边界之一 |
| Legacy `run_id` columns | MIGRATE / HISTORY | 兼容 contract、TaskIntent、trace、Creator 和 Java 仍使用 |
| 未引用 migration | DELETE CANDIDATE | 需逐个确认部署历史、回滚和 CI 引用后再处理 |

## 本阶段决定

不删除表、不删除 migration、不修改 schema。数据库清理只能在 Runtime consumer migration 和 Legacy retirement 完成后执行。
