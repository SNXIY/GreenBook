# GreenBook Java Backend Analysis

## 1. 项目定位

Java Backend (`apps/backend`) 是 GreenBook 社区的**业务事实层**。它拥有：
- 用户身份和认证
- 帖子（KnowPost）的完整生命周期
- 评论系统
- 定时发布状态机
- 数据分析（计数/热度/趋势）
- 关注关系
- 通知系统
- 文件存储（OSS）

**Agent 不能直接访问数据库，必须通过 Java Backend 提供的 Agent API 操作所有社区业务资源。**

---

## 2. 项目结构

```
apps/backend/
├── Dockerfile
├── pom.xml                              # Spring Boot 3.2.4, Java 21, Maven
├── db/                                  # SQL 迁移脚本
│   ├── schema.sql                       # 初始表结构
│   ├── comments_migration.sql
│   └── notifications_migration.sql
├── src/main/java/com/tongji/
│   ├── ZhiGuangApplication.java         # 入口
│   ├── agentfacade/                     # ★ Agent API 层
│   │   ├── api/AgentFacadeController.java   # REST Controller
│   │   ├── service/AgentFacadeService.java  # 业务编排
│   │   ├── service/IdempotencyService.java  # 幂等写入
│   │   └── service/ScheduledPublicationService.java  # 定时发布
│   ├── auth/                            # 认证 + JWT 签发
│   ├── knowpost/                        # 帖子 CRUD
│   ├── comment/                         # 评论
│   ├── counter/                         # 点赞/收藏计数 (Redis Bitmap + Kafka)
│   ├── relation/                        # 关注/粉丝
│   ├── notification/                    # 通知
│   ├── storage/                         # OSS 文件存储
│   ├── user/                            # 用户实体
│   ├── llm/                             # DeepSeek 描述生成
│   └── config/                          # Spring 配置
└── src/main/resources/
    ├── application.yml                  # 本地开发配置
    ├── db/migration/                    # Flyway 迁移 V1-V5
    └── keys/private.pem, public.pem     # RS256 JWT 密钥
```

---

## 3. 技术栈

| 组件 | 选型 |
|------|------|
| 框架 | Spring Boot 3.2.4 |
| 语言 | Java 21 |
| 数据库 | MySQL 8.0 |
| ORM | MyBatis 3.0.3 (XML Mapper) |
| 缓存 | Redis (Caffeine L2) |
| 消息队列 | Kafka (counter events, outbox) |
| 文件存储 | Aliyun OSS (local fallback) |
| ID 生成 | Snowflake (EPOCH 2024-01-01) |
| 认证 | RS256 JWT + Redis Session |

---

## 4. Agent API 接口

基路径：`/api/v1/agent/**`，需 `ROLE_USER`

### 4.1 帖子检索

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/posts/search?query&sort&page&size` | 搜索帖子 (MySQL LIKE) |
| GET | `/posts/{postId}` | 获取帖子详情 (含 body，最大 512KB) |
| GET | `/me/posts` | 我的帖子列表 |

### 4.2 草稿管理

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/drafts` | 创建草稿 (幂等，Idempotency-Key) |
| GET | `/drafts/{draftId}` | 获取草稿 |
| GET | `/me/drafts` | 我的草稿列表 |
| PUT | `/drafts/{draftId}` | 更新草稿 (乐观锁 expectedVersion) |

### 4.3 发布管理

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/publications/schedules` | 创建定时发布 |
| GET | `/publications/schedules/{scheduleId}` | 查询定时 |
| PUT | `/publications/schedules/{scheduleId}` | 修改定时 |
| DELETE | `/publications/schedules/{scheduleId}` | 取消定时 |
| POST | `/publications/publish-now` | 立即发布 |

### 4.4 评论互动

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/posts/{postId}/comments?cursor&size` | 获取评论 (游标分页) |
| POST | `/comments/{commentId}/replies` | 回复评论 |

### 4.5 数据分析

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/posts/{postId}/analytics` | 单帖分析 |
| GET | `/me/analytics/summary` | 账号总览 |

---

## 5. Agent 如何访问社区能力

### 搜索帖子
```
Python Agent → JavaClient.search_posts(query, sort, page, size)
  → HTTP GET /api/v1/agent/posts/search
    → KnowPostMapper.searchPublicForAgent (MySQL LIKE)
    → 补充计数数据 (likes, comments)
    → 计算 hotScore = log(1 + like×2 + fav + comments×1.5) / log(ageMinutes + 2)
    → 按 sort (hot/latest/relevant) 排序返回
```

### 创建草稿
```
Python Agent → JavaClient.create_draft(title, content, idempotency_key)
  → HTTP POST /api/v1/agent/drafts
    → IdempotencyService 检查幂等
    → KnowPostService.createDraft(userId, "AI_ASSISTED")
      → 写入 OSS knowposts/{id}/content.md
      → 写入 MySQL know_posts 表
    → 返回 DraftResponse {id, status: "draft"}
```

### 定时发布
```
Python Agent → JavaClient.create_schedule(draftId, runAt, timezone)
  → HTTP POST /api/v1/agent/publications/schedules
    → ScheduledPublicationService 创建定时记录 (status=SCHEDULED)
    → @Scheduled 每 30s 扫描到期定时
    → SCHEDULED → PROCESSING → PUBLISHED (调用 KnowPostService.publish)
    → 失败 → FAILED (超时恢复 → WORKER_TIMEOUT)
```

---

## 6. 为什么 Agent 不能直接访问数据库？

1. **所有权边界**：用户身份、帖子内容、评论是社区的业务数据，属于 Java Backend 所有。Agent 是参与者，不是数据所有者。

2. **一致性保护**：乐观锁 (expectedVersion)、幂等写入 (Idempotency-Key)、状态机 (SCHEDULED→PUBLISHED) 这些业务约束在 Java 层强制执行。Agent 直接写 DB 会绕过这些保护。

3. **安全隔离**：JWT 承载用户身份和 scope。Agent 以特定用户的身份调用 API，权限模型在 Java 层控制。

4. **事件驱动**：Java Backend 通过 Kafka 发布 counter events 和 outbox events，驱动缓存、通知、feed 等子系统。Agent 直接写 DB 不会触发这些事件。

5. **存储抽象**：帖子 body 存 OSS，不在 MySQL。Agent 不需要知道 OSS 的访问方式。

6. **运维独立**：Java 和 Python 服务可以独立扩缩容、部署、回滚。

---

## 7. 其他 Agent 相关能力

### Creator Handoff
```
POST /api/v1/knowposts/ai-drafts
Header: X-Creator-Handoff-Secret
Body: AiDraftCreateRequest(creatorId, title, bodyMarkdown, sourceTaskId, contentSha256)
```

这是 Creator Agent 的直接投递端点——一跳创建 AI 辅助草稿。需要共享密钥验证。

### 幂等服务
所有 Agent 写操作必须携带 `Idempotency-Key` header：
- 基于 `(user_id, operation, idempotency_key)` 唯一约束
- 状态流：IN_PROGRESS → COMPLETED | FAILED
- 相同 key+body hash → 重放返回存储结果
- 相同 key+不同 body → 409 IDEMPOTENCY_CONFLICT
- 24h 过期自动清理

### 定时发布状态机
```
SCHEDULED → PROCESSING → PUBLISHED
                       → FAILED
         → CANCELLED
```
- 30s 周期扫描到期定时
- `claimForExecution` 原子抢占
- Stale PROCESSING → FAILED (WORKER_TIMEOUT)
- publishNow 即时发布（跳过定时）

### 结构化错误协议
所有 API 错误统一返回 `AgentErrorResponse`：
```json
{
  "code": "DRAFT_VERSION_CONFLICT",
  "message": "草稿已被修改",
  "userMessage": "草稿版本冲突，请刷新后重试",
  "retryable": false,
  "requestCommitted": false,
  "traceId": "abc123"
}
```

Agent 依赖 `requestCommitted` 和 `retryable` 字段决定重试/对账策略。

---

## 8. 关键实体关系

```
users 1──N know_posts
users 1──N comments
know_posts 1──N comments
users ↔ users (following/follower)
scheduled_publications → users, know_posts
```

- Snowflake ID 全部以 **String** 暴露给 Agent (避免 JS Number 精度丢失)
- `content_origin` 字段标记 `AI_ASSISTED` vs `MANUAL`
- `agent_idempotency_record` 表独立存储幂等记录

---

## 9. 与 Python Agent 的交互协议

| 协议层 | 说明 |
|--------|------|
| 传输 | HTTP/1.1 (httpx AsyncClient) |
| 认证 | Bearer RS256 JWT (Java 签发) |
| 幂等 | Idempotency-Key header (所有写) |
| 追踪 | X-Trace-ID, X-Conversation-Id, X-Agent-Run-Id, X-Tool-Call-Id |
| 错误 | AgentErrorResponse JSON |
| 契约 | contracts/java-openapi.yaml (唯一权威) |
