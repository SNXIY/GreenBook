# GreenBook 社区智能体平台

GreenBook 是一个知识社区与多 Agent 协作项目。Java/React 提供社区基础平台，
Creator Agent 负责内容创作，Moderation Agent 负责真实内容审核，
Community Assistant Agent 负责意图理解、任务规划、工具路由、长任务和定时执行。

联调阶段采用“Docker 只运行中间件，五个应用在本机前台运行”的方式，便于查看日志、
热更新和断点调试。

## 项目结构

| 目录 | 职责 | 主要技术 |
| --- | --- | --- |
| `zhiguang-fe` | GreenBook Web 前端与管理员审核台 | React、TypeScript、Vite |
| `zhiguang-be` | 认证、用户、帖子、评论、发布、存储与 Agent 网关 | Java 21、Spring Boot、MyBatis |
| `creator-agent` | 持久化 AI 创作任务、草稿版本和发布交接 | FastAPI、LangGraph、PostgreSQL |
| `moderation-agent` | 内容审核、证据检索、多 Agent 裁决与人工复核 | FastAPI、LangGraph、PostgreSQL |
| `community-assistant-agent` | Intent、Planner、Supervisor、工具调用、记忆与长任务 | FastAPI、LangGraph、MCP |
| `scripts` | 环境安装、启动、停止、密钥轮换和验证脚本 | PowerShell |
| `infra` | 中间件初始化资源 | PostgreSQL SQL |

旧心理服务产物和历史数据库快照不再放在代码仓库中。当前机器上的可恢复归档位于
`D:\agent\green-book-legacy-archive-20260729`。

## 环境要求

- Docker Desktop
- JDK 21
- Maven 3.9+
- Node.js 20+
- Python 3.12
- [uv](https://docs.astral.sh/uv/)

三个 Python Agent 都使用各自的 `.venv`，但统一由 `pyproject.toml + uv.lock`
管理依赖；前端使用 `package-lock.json`，Java 使用 Maven。

## 首次准备

```powershell
cd D:\agent\green-book
Copy-Item .env.example .env
.\scripts\setup-dev.ps1
.\scripts\rotate-dev-secrets.ps1
```

如果 `.env` 已存在，不要用 `.env.example` 覆盖它。真实 API Key 只写入根 `.env`；
该文件已被 Git 忽略。`rotate-dev-secrets.ps1` 只轮换 JWT 与项目内部服务间密钥，
不会改动 DeepSeek、OSS 等外部凭据，也不会改数据库密码。Java JWT RSA 密钥对在缺失时由
`setup-dev.ps1` 在本机生成，密钥文件不进入 Git。

必须配置真实模型：

```dotenv
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=<你的真实密钥>
DEFAULT_MODEL=deepseek-v4-flash
```

Creator、Moderation 和 Assistant 均不支持 mock 模型。启动脚本也会拒绝空密钥和
已知的 `change-me-*` 占位密钥。

## 启动中间件

```powershell
.\scripts\dev-up.ps1
```

也可以直接执行：

```powershell
docker compose up -d --wait
```

Compose 只启动 5 个中间件容器，所有主机端口仅绑定 `127.0.0.1`：

| 容器 | 用途 | 主机端口 |
| --- | --- | --- |
| `greenbook-mysql` | Java 业务数据库 | `33306` |
| `greenbook-postgres` | Creator/Assistant 与 Moderation 的两个独立数据库 | `25432` |
| `greenbook-redis` | DB0 Creator/Assistant、DB1 Java、DB2 Moderation | `26379` |
| `greenbook-kafka` | Java 事件流（Redpanda） | `39092` |
| `greenbook-qdrant` | Agent 向量记忆与策略检索 | `26333`、`26334` |

PostgreSQL 中：

- `mindflow_creator`：Creator 与 Assistant；两者使用独立 Alembic 版本表。
- `content_moderation`：Moderation Agent。

MySQL、PostgreSQL、Redis、Kafka 和 Qdrant 数据仍使用独立 Docker volume。

## 启动应用

先启动中间件，再在五个独立 PowerShell 终端中运行：

```powershell
.\scripts\start-be.ps1
.\scripts\start-creator.ps1
.\scripts\start-moderation.ps1
.\scripts\start-assistant.ps1
.\scripts\start-fe.ps1
```

建议顺序为 Java → Creator → Moderation → Assistant → Frontend。

| 应用 | 地址 |
| --- | --- |
| Java 健康检查 | <http://127.0.0.1:8080/actuator/health> |
| Creator Studio | <http://127.0.0.1:8092/creator.html> |
| Moderation OpenAPI | <http://127.0.0.1:8088/docs> |
| Assistant 健康检查 | <http://127.0.0.1:8094/actuator/health> |
| GreenBook 前端 | <http://127.0.0.1:5173> |

Creator、Moderation、Assistant 的启动脚本会先执行各自 Alembic 迁移。应用日志留在
当前终端，按 `Ctrl+C` 停止对应应用。

## 常用中间件命令

```powershell
.\scripts\dev-up.ps1 status
.\scripts\dev-up.ps1 logs
.\scripts\dev-up.ps1 stop
.\scripts\dev-up.ps1 start
.\scripts\dev-up.ps1 down
```

`down` 删除容器和网络但保留数据卷，之后应使用 `dev-up.ps1` 重新创建容器。
日常操作不要添加 `-v`，否则会删除持久化数据。

## 创作、审核与助手边界

- 手动创作帖子在发布向导中提交真实 Moderation Agent 审核。
- Creator 生成的内容以 `AI_ASSISTED` 草稿交接 Java 发布向导，避免重复审核。
- Creator 产物通过版本号和 SHA-256 绑定，旧 Worker 或被编辑的草稿不能越权发布。
- Moderation 仅提供真实 `/moderation/*` API；旧模拟社区控制台和测试数据接口已删除。
- 管理员在 GreenBook `/admin/moderation` 审核，普通用户进入社区界面。
- Assistant 执行 Intent → Planner → Supervisor → Tool/Agent → Verify/Replan。
- 删除、批量发布和管理动作需要 Human-in-the-loop 审批。
- 长任务具备 checkpoint、lease、fencing、retry、interrupt、resume 与 SideEffect Ledger。
- 评论区 `@助手` 会携带受控帖子上下文，可检索、总结、创作、互动或安排发布。
- 用户偏好记忆只保存用户明确添加的内容；情景记忆和语义记忆均可关闭与清理。

## 验证

快速契约检查：

```powershell
.\scripts\smoke-test.ps1
```

提交前全量检查：

```powershell
.\scripts\verify-all.ps1
```

全量脚本依次执行：

- `docker compose config`
- Java 全部测试
- 前端类型检查与生产构建
- Creator 全部测试
- Moderation 全部测试
- Assistant 全部测试

根目录 `.github/workflows/verify.yml` 在 CI 中并行执行同一组项目检查，任何 Agent
都不会使用 mock provider。

## 相关文档

- [跨项目集成](docs/INTEGRATION.md)
- [验收清单](docs/ACCEPTANCE.md)
- [社区 Agent 编排设计](docs/community-agent-orchestration.md)
- [Moderation Agent 设计](moderation-agent/docs/moderation.md)
