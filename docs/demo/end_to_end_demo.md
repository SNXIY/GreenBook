# GreenBook Agent Runtime End-to-End Demo

本演示展示一条完整的社区运营 Agent 链路。它使用现有 IntentSpec、Planner、PlanExecution、ExecutionStateManager、RetryManager 和 EventStore；不新增 Runtime 能力，也不依赖真实 LLM 或外部社区服务。

## 1. User Request

```text
帮我运营一个 Agent 学习专题：搜索最近热门文章，分析为什么受欢迎，如果有旧稿就优化，没有就创建，发布前让我确认，确认后五分钟发布
```

## 2. IntentSpec

IntentSpec 只表达用户意图，不包含 step、依赖、DAG 或工具选择：

```json
{
  "mode": "CONDITIONAL",
  "goal": "运营一个 Agent 学习专题",
  "actions": [
    {"action": "SEARCH", "resource": "CONTENT"},
    {"action": "ANALYZE", "resource": "CONTENT"},
    {"action": "UPDATE_OR_CREATE", "resource": "DRAFT"},
    {"action": "PUBLISH", "resource": "POST"}
  ],
  "conditions": [
    {
      "type": "IF_EXISTS",
      "resource": "DRAFT",
      "then_action": "UPDATE",
      "else_action": "CREATE"
    }
  ],
  "constraints": [
    {"type": "APPROVAL", "value": "BEFORE_PUBLISH"},
    {"type": "TIME", "value": "5分钟后"}
  ],
  "source": "L2"
}
```

## 3. Planner TaskPlan

`PlanningContext` 同时携带 legacy `TaskIntent` 和 richer `IntentSpec`。Planner 根据 action 集合选择 `FULL_PIPELINE` 模板，并把约束透传到发布步骤：

| Order | Capability | Depends on | Output / constraint |
| ---: | --- | --- | --- |
| 1 | `SEARCH_COMMUNITY` | - | `SEARCH_RESULT` |
| 2 | `ANALYZE_CONTENT_PATTERNS` | 1 | `ANALYSIS_REPORT` |
| 3 | `GENERATE_CONTENT` | 2 | `DRAFT` |
| 4 | `VALIDATE_QUALITY` | 3 | quality result |
| 5 | `SCHEDULE_PUBLISH` | 4 | approval=`BEFORE_PUBLISH`, time=`5分钟后` |

这里的顺序、依赖和 capability 属于 `TaskPlan`，不是 IntentSpec。

## 4. Execution Steps

`PlanValidator` 验证 capability、工具映射、依赖、artifact flow 和 approval 要求后，`ExecutionStateManager` 创建唯一执行状态源 `PlanExecution`：

1. `SEARCH_COMMUNITY` 执行成功，产生热门文章搜索结果。
2. `ANALYZE_CONTENT_PATTERNS` 因临时 `TIMEOUT` 失败，状态为 `FAILED_RETRYABLE`。
3. `RetryManager` 分类错误并将该 step 重置为 `PENDING`，已完成的搜索 step 保持 `COMPLETED`。
4. 用户暂停执行，随后恢复；已完成 step 不重新执行。
5. 分析、生成和质量校验继续完成。
6. 发布 step 触发 `APPROVAL_REQUIRED`，用户确认后继续。
7. `SCHEDULE_PUBLISH` 按 `5分钟后` 约束完成，Execution 进入 `COMPLETED`。

## 5. Event Timeline

实际 event id 和时间戳由 Runtime 生成。核心事件顺序如下：

```text
EXECUTION_CREATED
EXECUTION_STARTED
STEP_STARTED              SEARCH_COMMUNITY
STEP_COMPLETED            SEARCH_COMMUNITY
STEP_STARTED              ANALYZE_CONTENT_PATTERNS
STEP_FAILED               ANALYZE_CONTENT_PATTERNS / TIMEOUT
STEP_RETRY_REQUESTED      ANALYZE_CONTENT_PATTERNS
STEP_RETRY_STARTED        ANALYZE_CONTENT_PATTERNS
EXECUTION_PAUSED
EXECUTION_RESUMED
STEP_COMPLETED            ANALYZE_CONTENT_PATTERNS
STEP_STARTED              GENERATE_CONTENT
STEP_COMPLETED            GENERATE_CONTENT
STEP_STARTED              VALIDATE_QUALITY
STEP_COMPLETED            VALIDATE_QUALITY
STEP_STARTED              SCHEDULE_PUBLISH
APPROVAL_REQUIRED         SCHEDULE_PUBLISH
STEP_COMPLETED            SCHEDULE_PUBLISH
EXECUTION_COMPLETED
```

EventStore 是可观测事件记录，不取代 `PlanExecution`。页面通过 Runtime API 查询快照，并通过 SSE 接收这些事件。

## 6. Pause / Resume / Retry

### Retry

```text
ANALYZE_CONTENT_PATTERNS: FAILED_RETRYABLE / TIMEOUT
        |
        v
RetryManager.retry_step()
        |
        +--> STEP_RETRY_REQUESTED
        +--> checkpoint 保存
        +--> StepExecution: PENDING
        +--> STEP_RETRY_STARTED
```

Retry 不重新生成 IntentSpec 或 TaskPlan，也不递归调用 Worker。后续由既有 `Worker.run()` 执行下一轮。

### Pause / Resume

```text
RUNNING --pause_execution()--> PAUSED
PAUSED  --resume_execution()--> RUNNING
```

暂停只改变 Execution 生命周期状态，不杀死 Worker；Worker 下一步执行前通过 RuntimeGuard 检查状态。

### Approval

发布步骤进入 `WAITING_APPROVAL` 后，人工确认调用既有 human interaction 流程恢复该 step。它与用户主动 `PAUSED` 是不同语义。

## 7. Run the Demo Test

从仓库根目录运行：

```powershell
pytest tests/e2e/test_greenbook_runtime_demo.py
```

该测试固定构造 IntentSpec，真实调用 Planner、PlanValidator、PlanExecution、RetryManager 和 EventStore，适合面试演示和回归验证。

