# 内容审核 Agent 本地开发

GreenBook 联调阶段只用 Docker 运行中间件，Moderation Agent 作为本机前台进程运行，
以便直接查看日志、断点和热重载。

## 启动

```powershell
cd D:\agent\green-book
.\scripts\dev-up.ps1
.\scripts\start-moderation.ps1
```

根启动脚本负责：

1. 读取根 `.env`；
2. 拒绝空密钥、占位密钥和模拟模型配置；
3. 连接 `127.0.0.1` 上的 PostgreSQL、Redis 与 Qdrant；
4. 执行 Alembic 迁移；
5. 在 `127.0.0.1:8088` 启动 FastAPI。

可访问：

- OpenAPI：<http://127.0.0.1:8088/docs>
- 健康检查：<http://127.0.0.1:8088/health>
- GreenBook 管理员审核页：<http://127.0.0.1:5173/admin/moderation>

审核服务根路径 `/` 返回 404，这是预期行为。旧的独立模拟控制台与
`/community/*` 演示接口已删除，真实社区数据一律通过 Java 平台接入。

## 单项目运行

需要单独开发审核服务时：

```powershell
cd D:\agent\green-book\moderation-agent
uv sync --frozen
$env:PYTHONPATH = "src"
uv run alembic upgrade head
uv run python src\run_service.py
```

Windows 下应使用 `src/run_service.py`，它会配置 psycopg 所需的 Selector 事件循环。

## Worker

默认 `MODERATION_EMBEDDED_WORKER_ENABLED=true`，API 进程内嵌 Worker。关闭内嵌 Worker
后需另开终端：

```powershell
uv run python src\run_moderation_worker.py
```

Worker 使用数据库租约和 fencing token 拒绝过期执行结果，避免旧 Worker 覆盖新状态。

## 验证

```powershell
uv run pytest
uv run ruff check src tests
uv run mypy src
```
