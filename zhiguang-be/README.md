# GreenBook Java Backend

GreenBook 的 Java 服务是社区业务的唯一事实源，负责身份、用户、帖子、评论、关系、
通知、存储和发布状态机。三个 Agent 只能通过受控 HTTP 契约使用社区能力，不能直接
访问 MySQL，也不能绕过 Java 的资源归属与角色校验。

## 技术栈

- Java 21、Spring Boot 3.2
- Spring Security、OAuth2 Resource Server、RS256 JWT
- MyBatis、MySQL
- Redis、Redisson、Caffeine
- Kafka API（本地使用 Redpanda）
- 阿里云 OSS / 本地文件存储
- Spring Boot Actuator

## 业务边界

```text
Browser / React
       │ user JWT
       ▼
GreenBook Java API
       ├─ Auth / User / Profile
       ├─ KnowPost / Comment / Relation / Notification
       ├─ Storage / OSS
       ├─ Publication state machine
       ├─ Creator handoff
       ├─ Moderation callback
       └─ Assistant capability tools
```

Java 负责：

- 签发同时面向 Java、Creator 和 Assistant 的用户 JWT；
- 校验 `USER`、`ADMIN` 等业务角色；
- 维护帖子所有权、可见性和发布状态；
- 为 Creator 接收带内容指纹的 `AI_ASSISTED` 草稿；
- 为手动创作提交真实 Moderation Agent，并幂等应用审核回调；
- 为 Assistant 签发绑定用户、动作、资源、Run、期限和使用次数的短时 Capability；
- 在最终写入前再次执行权限、状态和内容版本校验。

## 发布状态机

```text
MANUAL:
draft -> reviewing -> published
                  \-> rejected -> reviewing

AI_ASSISTED:
draft -> published
```

AI 内容不重复走普通发布审核，但 Creator 产物的 SHA-256 会贯穿草稿交接、人工确认、
定时任务和最终发布；内容被编辑后，旧审批与旧授权自动失效。

手动内容由 `moderation-agent` 异步审核。Java 不在线程中持续轮询，而是接收带服务
身份的幂等回调，并用定时对账修复断线或重启期间的遗漏。

## Assistant 工具边界

`/api/v1/assistant-tools/**` 使用独立的内部认证协议：

1. Assistant 以服务密钥和当前用户 access token 兑换短时 Capability；
2. Capability 只允许一个声明过的动作，并绑定资源、Run、用户和有效期；
3. 后续工具调用同时携带服务密钥与 Capability；
4. Java 在 Controller/Service 中重新检查资源归属、角色、状态和幂等键。

Spring Security 中这些端点不消费用户 Bearer JWT，因为 Capability 与用户登录令牌属于
不同认证域；`permitAll` 不代表匿名可用，内部控制器仍会强制校验服务密钥和 Capability。

## 本地启动

从仓库根目录执行：

```powershell
.\scripts\dev-up.ps1
.\scripts\start-be.ps1
```

根目录 `.env` 是本地配置来源，不要在本目录创建包含真实密钥的配置文件。

默认地址：

```text
http://127.0.0.1:8080
GET /actuator/health
GET /.well-known/jwks.json
```

本地依赖：

| 依赖 | 地址 |
| --- | --- |
| MySQL | `127.0.0.1:33306/zhiguang` |
| Redis | `127.0.0.1:26379`，DB 1 |
| Redpanda / Kafka | `127.0.0.1:39092` |

## 配置

主要环境变量由根目录启动脚本注入：

```dotenv
MYSQL_HOST=127.0.0.1
MYSQL_PORT=33306
MYSQL_DB=zhiguang
REDIS_HOST=127.0.0.1
REDIS_PORT=26379
REDIS_DATABASE=1
KAFKA_HOST=127.0.0.1
KAFKA_PORT=39092
STORAGE_PROVIDER=local
MODERATION_AGENT_BASE_URL=http://127.0.0.1:8088
```

项目间服务密钥和外部 API Key 只保存在被 Git 忽略的根 `.env` 中。

## 数据与迁移

- 新环境由 `db/schema.sql` 初始化；
- 增量 SQL 位于 `db/*_migration.sql`；
- 本地存储文件默认位于 `data/storage`；
- MySQL 数据保存在 Docker volume，停止应用不会丢失。

## 验证

```powershell
mvn test
```

或从仓库根目录运行全仓验证：

```powershell
.\scripts\verify-all.ps1
```

跨服务契约和手工发布链路见：

- [集成契约](../docs/INTEGRATION.md)
- [自动化 E2E](../scripts/e2e-test.ps1)
- [验收清单](../docs/ACCEPTANCE.md)
