# GreenBook 融合契约

## 信任边界

GreenBook Java API 是用户身份和帖子来源的唯一权威。

1. 前端登录后取得 GreenBook JWT。
2. AI 创作 iframe 通过 URL fragment 接收 JWT；fragment 不发送到 HTTP 服务，
   Creator 读取后立即从地址栏移除。
3. Creator 使用 Java 的 `/.well-known/jwks.json` 校验签名、issuer、audience、
   用户 ID、租户和 `CREATOR` 角色。
4. Creator 使用服务间密钥调用 Java `POST /api/v1/knowposts/ai-drafts`。
5. `ai-drafts` 不接受普通用户 JWT 作为可信 AI 来源。
6. Community Assistant 校验同一 Java JWT 的
   `community-assistant-agent` audience，只允许 `USER` 角色。
7. Assistant 先用“服务密钥 + 当前用户 access token”向 Java 兑换短期 Capability；
   后续工具调用同时携带服务密钥和 Capability。前端永远不会获得服务密钥。
8. Capability 默认拒绝未声明权限，只允许一个动作，并绑定资源、Run、期限和使用次数；
   共享密钥本身不能搜索、读帖、发布或回复评论。

## AI 草稿交接

请求：

```http
POST /api/v1/knowposts/ai-drafts
X-Creator-Handoff-Secret: <service secret>
Content-Type: application/json
```

主体包含 `creatorId`、`title`、`bodyMarkdown`、`sourceTaskId` 和
`contentSha256`。成功返回 Java 草稿 ID；Creator 不允许在真实 Java 配置下
静默生成本地占位草稿。

## 发布状态机

```text
MANUAL:      draft -> reviewing -> published
                                \-> rejected -> reviewing (修改后重试)

AI_ASSISTED: draft -------------> published
```

人工内容只有在正文上传、摘要/标题/标签等元数据保存后才提交审核。审核通过前
不得增加发布计数、写入 Feed 或建立公开索引。

查询当前用户自己的发布状态：

```http
GET /api/v1/knowposts/{id}/publish-status
Authorization: Bearer <greenbook JWT>
```

返回 `status`、`moderationTaskId` 和最新 `reason`。

审核采用异步结果闭环：

1. Java 将帖子置为 `reviewing`，向 Moderation Agent 提交带内容指纹幂等键的任务；
2. Agent 接收任务后 Java 后台调用立即返回，不在线程池中轮询；
3. Moderation Worker 完成自动审核后回调：

```http
POST /api/v1/internal/moderation/tasks/{taskId}/result
X-Moderation-Service-Secret: <MODERATION_AGENT_AUTH_SECRET>
```

4. Java 按 `taskId` 幂等应用 PASS、REJECT/LIMIT 或 WAITING_REVIEW；回调早于任务 ID
   落库时使用请求中的 `content_id` 完成关联；
5. Java 定时扫描只对停留过久的任务执行一次 `GET` 对账，不再阻塞式轮询。

服务密钥使用独立请求头，不作为用户 Bearer JWT 发送，避免被 Spring OAuth2 登录过滤器
误判。管理员人工复审也会触发同一结果应用逻辑。

Moderation 不直接在审核事务中调用 Java。终态和待人工复审状态会同时写入
`moderation_callback_outbox`，Dispatcher 通过租约、attempt fencing、指数退避和最大
尝试次数完成投递。可通过 Java 管理员代理查询投递状态：

```http
GET /api/v1/admin/moderation/callbacks?status=RETRYING&limit=50
Authorization: Bearer <admin JWT>
```

浏览器生成或传入的合法 `X-Trace-ID` 会由 Java 继续传给 Moderation；任务、回调及
管理员详情使用同一追踪号。非法格式会在边界处替换为 UUID，避免日志注入。

## 存储

- 本机开发环境：`STORAGE_PROVIDER=local`，数据保存在
  `zhiguang-be/data/storage`。
- 生产环境：`STORAGE_PROVIDER=aliyun`，使用 OSS 预签名上传。
- 两种实现使用同一套对象键和 `OssStorageService` API。

## 本地模型与 OIDC

- 本机开发时 Java JWT issuer 与 Creator 校验端统一为
  `http://127.0.0.1:8080`，JWKS 由 Java 的
  `/.well-known/jwks.json` 提供。
- 生产环境启用真实模型提供商时，应通过 Secret Manager 注入模型密钥；所有默认共享密钥
  也必须替换。

## 幂等与恢复

- Creator publication handoff 使用幂等键和最终产物版本。
- Moderation 任务幂等键包含帖子 ID 与待审内容指纹。
- Java 定时扫描长时间处于 `reviewing` 的帖子；进程重启后会继续查询或提交同一
  审核任务。
- Assistant 等待 Creator 时将 Run 持久化为 `WAITING_DEPENDENCY` 并释放 Worker
  租约；正常情况下 Creator SSE 终态事件会立即唤醒 Run，30 秒查询仅作为断线和重启兜底，
  等待期间不占用长运行并发槽。
- Community Assistant 的 Run、Step、Event、IdempotencyRecord 和 ScheduledAction
  都保存在 Creator PostgreSQL 的 `assistant_*` 表中。过期租约可由新 Worker 接管，
  已完成步骤不会重复执行。
- Assistant 外部写入使用 SideEffect Ledger；`UNKNOWN` 表示请求可能已到达远端，
  恢复时必须以原 operation key 幂等重放，不能换键重复执行。
- Assistant 的浏览器请求使用 `Idempotency-Key`；Creator 任务、publication handoff
  与 Java 发布分别派生稳定幂等键。
- 多个 Assistant Worker 使用 PostgreSQL advisory transaction lock 保护认领临界区，
  全局与单用户并发计数不会因并行事务同时读取旧快照而超限；Redis 再限制每分钟全局和
  单用户真实模型调用量，Redis 可配置为故障开放或生产环境强制可用。

## Community Assistant 工具契约

前端只调用：

```http
POST /api/v1/assistant/conversations/{conversationId}/messages
Authorization: Bearer <greenbook JWT>
Idempotency-Key: <uuid>
```

Assistant 可调用的 Java 内部工具：

```text
POST /api/v1/assistant-tools/capabilities
DELETE /api/v1/assistant-tools/capabilities/{capabilityId}
GET  /api/v1/assistant-tools/posts/search?q=...
GET  /api/v1/assistant-tools/posts/{id}
POST /api/v1/assistant-tools/posts/{id}/publish
POST /api/v1/assistant-tools/comments/replies
```

Capability 兑换/撤销接口携带 `X-Assistant-Service-Secret` 和用户 Bearer access
token；其余内部接口携带服务密钥和 `X-Assistant-Capability`。两类令牌使用不同请求
头，避免 Spring 登录会话过滤器把 Capability 误判成用户 JWT。写入接口还必须携带
`Idempotency-Key`。发布接口只接受 `AI_ASSISTED`、Capability 用户与所有者一致且
状态可发布的草稿；已经发布的同一草稿返回幂等重放。
完整设计见 [COMMUNITY_ASSISTANT.md](COMMUNITY_ASSISTANT.md)。
