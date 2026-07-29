# Community Assistant Agent 架构

## 目标

Community Assistant 是知光社区的任务型入口，不复制帖子、用户或 Creator 的业务能力。
它负责理解自然语言、规划步骤、调用受控工具，并让长运行任务能够恢复、审计和重放。

典型任务：

- “明天上午八点发布一篇关于如何学好 Java 的帖子”
- 在帖子评论区“@助手 总结这个帖子”
- “找几篇关于减肥的帖子，再生成一份可执行的减肥指南”

## 项目边界

```text
zhiguang-fe
    │ Bearer JWT + typed run polling
    ▼
community-assistant-agent (Supervisor / Harness)
    ├── capability exchange ─────────► zhiguang-be / JWT + MySQL
    ├── community.search_posts ──────► zhiguang-be / MySQL
    ├── community.get_post ──────────► zhiguang-be / OSS
    ├── creator.create_draft ────────► creator-agent
    └── publication.schedule/publish ► zhiguang-be

moderation-agent ◄── MANUAL 帖发布审核 ── zhiguang-be
```

Java 是用户、角色、帖子、评论、OSS 和发布状态的唯一事实源。Creator 是内容创作的
唯一执行者。Assistant 不直连 MySQL，也不生成伪造的帖子结果。

## Agent Harness

PostgreSQL 中的 `assistant_*` 表保存：

- `conversations/messages`：跨会话上下文；
- `runs/run_steps`：类型化状态和工具边界；
- `events`：可重放的追加事件；
- `scheduled_actions`：定时任务、租约、退避重试和最终结果；
- `scheduled_action_attempts`：每次 Scheduler 认领的 Worker、开始/结束时间、结果与错误；
- `side_effects`：外部写入的请求哈希、稳定操作键、执行状态、尝试次数和远端资源；
- `idempotency`：浏览器重发不会创建第二个任务。

Worker 用 `SELECT ... FOR UPDATE SKIP LOCKED` 认领 Run 和定时任务。认领带租约；
进程中断后新 Worker 可重新认领过期任务。已完成 Step 会被复用，Creator 调用和 Java
发布又各自带幂等键。写入工具执行前先记账为 `PREPARED/IN_FLIGHT`，响应丢失时记为
`UNKNOWN`；新 Worker 使用同一 operation key 重放并核对类型化输出，因此恢复不会重复
产生副作用。旧 Worker 提交结果前必须再次校验租约所有者，不能覆盖新状态。

本地默认同时运行 4 个 Run 和 2 个 Scheduler action，并用相关子查询限制同一用户
最多占用 1 个活跃 Run。Creator 创建不是 Worker 内的长轮询：首次调用只提交任务，
Run 随后进入 `WAITING_DEPENDENCY`、保存远端 task ID 并释放租约；到期后重新认领并
执行一次状态查询。用户取消时先尽力取消远端 Creator task，再原子地清除本地租约、
待审批和待执行步骤。每个 Run 累计记录模型、工具和依赖等待耗时。

Run 创建时还会保存一份运行协议身份：模型名、三个 Prompt 版本、Harness schema 和
完整 Tool I/O Schema 指纹。没有执行过任何步骤的旧任务可以清空旧计划后重新规划；
一旦已有完成步骤或外部副作用，协议不一致就会被确定性拒绝并写入事件流，避免升级后
把旧 Checkpoint 与新工具语义混用。查询对话时先取最近 30 条，再按时间正序送入
独立上下文预算；工具结果和帖子正文同样只生成模型视图，数据库中的完整证据不改变。

帖子正文、搜索摘要和评论都是不可信数据，不会与系统指令拼成同一权限层。工具输出进入
模型前会移除隐藏 HTML 注释，并标记角色覆盖、密钥窃取、伪造工具调用等注入信号；
真正的工具白名单、参数、资源绑定和批准仍由代码校验，模型无法通过帖子文字扩权。

多实例 Worker 的 Run 认领临界区使用 PostgreSQL advisory transaction lock，因此全局
并发和单用户并发上限是数据库级约束，不只是单进程 Semaphore。每次模型调用还经过共享
Redis 的全局/用户固定窗口；命中限流后按 Redis TTL 恢复，而不是立即热重试。

## 定时发布为何不保存用户 JWT

Java access token 只有 15 分钟有效期，不能把“明天发布”实现为保存 Token 后睡眠。
Assistant 在收到请求时把当前短期 Token 加密后暂存，只用于调用 Creator `AUTO` 任务
以及向 Java 兑换最小权限 Capability，Run 完成或最终失败后立即清除。定时任务只保存
草稿 ID、用户 ID、执行时间，以及一枚加密的 Capability；它只允许发布该草稿，有明确
期限和最多 5 次消费额度，不是用户登录 Token。

Java 工具调用必须同时携带服务密钥和 Capability。服务密钥只证明调用方是 Assistant，
不能单独获得业务权限。Java 再次检查：

1. Capability 的签名、受众、动作、资源、Run、期限和数据库使用次数；
2. Capability 用户、草稿所有者与计划用户一致；
3. `content_origin` 必须是 `AI_ASSISTED`；
4. 状态必须可发布；
5. 已发布请求按幂等重放返回，人工草稿永远不能绕过审核。

此外，Creator handoff 的 `source_content_sha256` 会固化到发布参数及定时任务。Java
发布前再次比对帖子当前 `content_sha256`，用户若在等待确认或等待定时期间编辑正文，
旧授权会安全失败，必须重新确认。Scheduler 为每次执行保存 attempt；只有网络异常、
超时、429 与 5xx 会进入退避重试，内容版本、权限和参数错误不会消耗无意义重试。

取消定时任务是一条补偿事务：Assistant 持有任务行锁，使用用户当前 access token
撤销 Java Capability；撤销成功后才把任务提交为 `CANCELLED` 并清除密文。若 Java
暂时不可达，取消事务回滚并提示重试，避免界面显示已取消但授权仍可用。

## 工具与模型的责任

DeepSeek Supervisor 只输出符合 Pydantic 契约的计划。工具名采用
`domain.capability`：

- `community.search_posts`
- `community.get_post`
- `community.summarize_post`
- `creator.create_draft`
- `publication.schedule`
- `publication.publish_now`

每个工具同时有 Pydantic 输入/输出模型。代码会校验搜索结果不重复、返回帖子必须等于
请求帖子、发布结果必须等于获批草稿、评论来源必须等于当前 Run。权限、参数边界、
超时、幂等、租约和状态迁移由代码实现，不能由模型决定。UI 展示简短执行轨迹，不展示
内部思维链。

## 参考设计

- CopilotKit / AG-UI：采用“聊天 + 结构化工具 UI + shared state/HITL”的交互模型；
- assistant-ui：借鉴可组合 Thread、Message、Composer、Action Bar，以及流式状态、
  重试和无障碍约束；
- Restate AI examples / Restate：借鉴 crash-safe steps、durable retry 和稳定幂等键；
- OpenAI Agents SDK：借鉴类型化 Tool Output、handoff 与 guardrail 的分层；
- Cedar：借鉴 principal/action/resource/context 与默认拒绝的授权模型；
- Temporal SDK：借鉴 Schedule、租约式 Worker、可恢复工作流和活动幂等；
- FastAPI LangGraph production-ready template：借鉴有状态对话、工具调用、持久化、
  鉴权、超时和可观测性边界。
- Pico：借鉴运行身份指纹、Checkpoint freshness 和恢复时拒绝过期执行环境；
- nanobot：借鉴最近会话窗口、工具结果压缩、Cron attempt 记录和 orphan repair。

这里参考的是协议与工程模式，没有复制第三方 UI，也没有为了“技术栈好看”增加新的
Docker 服务。nanobot 的自动 Dream memory、通用文件系统工具和子 Agent，以及 Pico
面向代码工作区的 shell 能力不符合社区权限边界，因此没有引入。
