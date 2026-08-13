> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 8.2 Workspace Consolidation Move Plan

## Scope

将三个仍处于活动开发/部署边界的应用统一放入 `apps/`。本阶段只改变目录路径和引用路径，不改变 Python 包名、Java 包名、前端包名、数据库结构或业务逻辑。

## Planned Moves

| Current path | Target path | Classification |
| --- | --- | --- |
| `greenbook-backend/` | `apps/backend/` | ACTIVE Java backend |
| `greenbook-frontend/` | `apps/frontend/` | ACTIVE frontend |
| `creator-agent/` | `apps/creator-agent/` | ACTIVE Creator service |
| `community-assistant-agent/` | unchanged | COMPATIBILITY / LEGACY |

目标目录在执行前均确认不存在。

## Reference Audit

### `greenbook-backend`

引用于：

- `.github/workflows/verify.yml` 的 working directory、密钥生成路径和缓存/构建步骤；
- `docker-compose.yml` 的数据库初始化脚本挂载；
- `scripts/dev-up.ps1`、`scripts/ensure-jwt-keys.ps1`、`scripts/smoke-test.ps1`、`scripts/start-be.ps1`、`scripts/verify-all.ps1`；
- `README.md` 和 `infra/docker-compose.dev.yml` 的说明文本。

这些引用需要更新为 `apps/backend/`。

### `greenbook-frontend`

引用于：

- `.github/workflows/verify.yml`；
- `scripts/setup-dev.ps1`、`scripts/smoke-test.ps1`、`scripts/start-fe.ps1`、`scripts/verify-all.ps1`；
- `README.md`；
- 前端自身的 `/creator-agent` 代理配置保持不变。

这些引用需要更新为 `apps/frontend/`。前端内部 API 路径不变。

### `creator-agent`

引用于：

- `.github/workflows/verify.yml`；
- `scripts/run_p0_e2e.py`、`scripts/setup-dev.ps1`、`scripts/smoke-test.ps1`、`scripts/start-creator.ps1`、`scripts/verify-all.ps1`；
- `README.md`、`greenbook-frontend/.env.example` 及前端代理配置中的服务 URL 语义。

这些引用需要更新为 `apps/creator-agent/`。`creator-agent` 的 Python 项目名、前端路径 `/creator-agent`、身份 audience 和 API contract 不变。

### Workspace and package configuration

- 根 `pyproject.toml` 的 uv workspace 当前不包含这三个目录；本计划不新增 workspace member。
- 三个应用的 Python/Node 包名不改。
- `uv.lock` 不需因目录移动而重写；如验证工具产生锁文件变化，应丢弃该无关变化。
- `package.json`、`pom.xml` 和应用内部 import 不做逻辑改动。

## Execution Order

1. 确认目标目录不存在，并保存本计划。
2. 移动三个目录。
3. 更新仅涉及旧目录路径的 CI、Docker、脚本和说明文档。
4. 用 `rg` 检查旧路径残留，排除历史归档文档和依赖字符串。
5. 运行可用的后端、前端、Creator 和 Python 测试。

## Rollback

按相反方向移动即可恢复：

- `apps/backend/` -> `greenbook-backend/`
- `apps/frontend/` -> `greenbook-frontend/`
- `apps/creator-agent/` -> `creator-agent/`

恢复后将本次更新的路径引用反向替换。由于不改包名和业务逻辑，回滚不需要数据库迁移。

