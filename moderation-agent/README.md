# GreenBook 内容审核 Agent

GreenBook 的独立内容审核服务。Java 平台提交真实帖子、评论与举报内容，审核 Agent
通过 LangGraph 编排预检、动态取证、策略检索、多 Agent 裁决和人工复核。服务只暴露
审核 API；管理员工作台统一位于 `zhiguang-fe`，不再维护独立的模拟社区控制台。

## 核心能力

- L0 确定性预检与 L1 可选安全模型预检
- FAST / STANDARD / DEEP 风险路由
- 动态工具取证、Policy RAG 与历史案例检索
- Risk / Safe / Judge 多 Agent 裁决
- 异步任务、租约 fencing、失败重试和人工复核
- PostgreSQL 持久化，Redis 加速队列，Qdrant 辅助向量检索
- Bearer 服务认证、Trace ID、结构化审核证据和操作日志

## 本地联调

在仓库根目录启动中间件，再以前台进程启动服务：

```powershell
cd D:\agent\green-book
.\scripts\dev-up.ps1
.\scripts\start-moderation.ps1
```

服务地址：

- OpenAPI：<http://127.0.0.1:8088/docs>
- 健康检查：<http://127.0.0.1:8088/health>
- 管理员审核界面：<http://127.0.0.1:5173/admin/moderation>

根启动脚本会执行 Alembic 迁移、校验真实模型配置和服务间密钥，然后在当前终端输出日志。

## 主要 API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/moderation/tasks` | 提交真实内容审核任务 |
| `GET` | `/moderation/tasks` | 查询审核任务 |
| `GET` | `/moderation/tasks/{task_id}` | 查询任务、证据和裁决详情 |
| `GET` | `/moderation/tasks/{task_id}/logs` | 查询审核日志 |
| `POST` | `/moderation/tasks/{task_id}/review` | 提交人工复核 |
| `GET` | `/moderation/statistics` | 查询审核统计 |

不存在 `/community/*` 模拟接口，也不生成演示帖子或测试用户。

## 验证

```powershell
cd D:\agent\green-book\moderation-agent
uv sync --frozen
uv run pytest
uv run ruff check src tests
uv run mypy src
```

更完整的架构说明见 [docs/moderation.md](docs/moderation.md)，跨项目调用关系见
[GreenBook 集成文档](../docs/INTEGRATION.md)。
