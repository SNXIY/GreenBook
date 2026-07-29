# Creator Studio Phase 11 实现说明

## 目标

Phase 11 把原先以 Agent 轨迹为中心的工作台，调整为创作者日常使用的内容工作区。
Agent 仍负责分析、研究、规划、写作和评审，但主要界面围绕作品、素材和正文组织。

## 消费者主路径

```text
建立项目 -> 添加/选择素材 -> 提交创作简报
         -> 自适应选题与大纲 -> 审阅正文
         -> 选择局部文本 -> 查看 AI 差异 -> 接受或忽略
         -> 保存版本/创建分支 -> 生成渠道稿 -> 反馈
```

工作区采用三栏和运行抽屉：

- 左栏：作品、项目、素材。
- 中栏：Tiptap 正文编辑器和版本操作。
- 右栏：AI 协作、引用、质量、反馈。
- 运行详情：计划、事件、Artifact 和恢复信息。

交互结构参考 Author 与 LobeHub 的内容组织、assistant-ui 与 CopilotKit 的生成式
交互、AnythingLLM 与 Open WebUI 的工作区/素材模式、Dify 的运行详情。实现沿用本项目
原生 JavaScript、FastAPI、SQLAlchemy 与 LangGraph 边界，没有直接复制其代码。

## Studio 数据模型

新增持久化实体：

- `creator_projects`
- `creator_materials`
- `creator_project_tasks`
- `creator_task_materials`
- `creator_suggestions`
- `creator_draft_branches`
- `creator_channel_variants`
- `creator_feedback`

所有查询均绑定 `tenant_id + creator_id`。任务只使用创作者显式选择的素材，素材内容以
`[素材 material:{id}]` 注入任务引用说明，便于审计和后续引用映射。

## 建议与版本

局部 AI 修改不再创建完整 Agent Task：

1. 建议绑定 `draft_id + base_version`。
2. 服务端使用正文、前缀和后缀定位唯一选区。
3. 模型只返回替换文本、理由、证据 ID 和风险提示。
4. 用户接受后，服务端在事务边界内创建新的 Draft Version。
5. 正文版本变化后，未处理的旧建议转为 `STALE`，不能误应用。

分支从指定历史版本复制出独立 Draft；渠道稿绑定主稿版本，不覆盖正文。

## 自适应 Human-in-the-loop

`ADAPTIVE` 为默认交互模式：

- 选题置信度不低于 `0.75` 且证据置信度不低于 `0.50` 时自动推进。
- 大纲置信度不低于 `0.80` 时自动推进。
- 正文始终保留人工审阅。

`GUIDED` 保留全部确认点；`AUTO` 沿用原有自动流程。

## 引用和模型路由

Writer 返回结构化 citation sidecar。服务端只保留：

- `evidence_id` 属于当前授权 Evidence Pack；
- `claim_text` 是正文中的精确子串；
- 相同 claim/evidence 组合未重复。

引用标题和 URL 始终由可信 Evidence Pack 回填，不信任模型自行生成的来源元数据。

模型按 operation 路由到 analysis、writer、critic 或 assist 配置，单个 Agent 无需感知
提供商或具体模型。

## 可观测与隐私

可选 OpenTelemetry 记录 HTTP 与模型操作 Span，包括 operation、model、Token 估算、
预算和错误状态。Prompt、响应、素材和正文不进入 Span attribute，避免内容通过遥测
外泄。

## 验证

自动化测试覆盖项目/素材绑定、建议接受与过期保护、分支、渠道稿、反馈、自适应确认点、
模型路由和引用约束。前端构建通过 esbuild 输出 Tiptap 编辑器 bundle。
