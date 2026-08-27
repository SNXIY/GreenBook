# REAL_PROCESS_RESTART_RECOVERY

日期：2026-08-25（Asia/Shanghai）  
项目：`D:\agent\green-book`

## RECOVERY_AUDIT

1. **reboot 前最后 evidence 时间**：Windows shutdown `04:48:35`，startup `04:48:50`。最后一个 evaluation JSON 是 `03:07:18`；最后 overnight stdout 是 `03:23:48`；Java durable log 写到 `04:45:36`。这次重启由 Kernel-General/EventLog 12、13、6005、6006 及 `LastBootUpTime` 交叉证明。

2. **已完成测试**：L1 `20/20`；既有 L2 debug/resume `14` turns；L3 fresh runtime/business `8/8`；Semantic `60` primary / `78` utterances；Performance `55` samples；既有 safety aggregate 已完成。L1、Semantic、Performance 均未重跑。

3. **未完成测试**：L2 fresh 在 T13 的结果投影故障处停止，T14 未执行；L3 业务 turn 已完成，重启后的 reload-only 观察已补证。既有 UX review 保留，未做无关重跑。

4. **L2 checkpoint**：conversation `b4ca6a80-cd2e-4760-98aa-9bceb970c265`。T1-T8 保留；T8 已通过 execution/ledger/ActionObservation/Java truth reconcile 为完成；恢复执行 T9，T10 仅发生安全的只读 clarification blocker，T11/T12 完成。最后安全 durable checkpoint 是 **T12**。

5. **L3 checkpoint**：conversation `32f88547-86c2-4226-8b1f-a7af5473fc1c`。T1-T8 保留，没有重新发送业务 turn；reload-only 恢复后同一 conversation 有 `14` 条 API message、`19` 个可见消息节点、AgentPanel 已打开，结果为 PASS。

6. **pending executions**：overnight 新增范围内为 `0`；全局 `execution` 仅有历史 `WAITING_APPROVAL=5`，没有 RUNNING/QUEUED/RESULT_UNKNOWN execution。L2 T13 两次都没有创建 execution。

7. **RESULT_UNKNOWN**：全局有 `5` 条历史记录，均为 8 月 22–24 日旧记录，`reconciliation_needed=true`、资源 ID 为空、已耗尽 reconciliation budget；重启/overnight 新增 `0`。这些记录未被猜测性改写，也未盲重试。

8. **pending approvals**：全局 `5` 条，全部为重启前旧 conversation；overnight 新增 `0`。没有批准任何旧 approval。

9. **queue state**：`execution_queue_message` 全局 `882/882 ACKED`，unacked `0`；`execution_lease` `0` 行。L2 T11/T12 的 queue message 也均为 ACKED。

10. **canonical runtime health**：Frontend `:5173`、Agent API `:8094`、Java `:8080`、PostgreSQL `:25432`、MySQL `:33306`、Redis `:26379`、Kafka `:39092`、Qdrant `:26333/:26334`、Browser/CDP `:9222` 均使用原端口并监听；Frontend/API/Java/CDP HTTP 均返回 `200`。Agent health 报告 `queue + postgres + in_process + javaReachable`。未使用 mock、替代端口或第二套 runtime。

11. **crash/recovery safety risk**：overnight 没有发现 side effect 不确定、重复 physical WRITE、wrong resource 或 false success；计数均为 `0`。风险仅保留在历史 5 条 RESULT_UNKNOWN、历史 non-terminal residue，以及 L2 T13 的产品结果投影故障。`execution_control` 的历史 `RUNNING=871` 与 terminal execution/ACK queue 不一致，作为 stale projection 记录，未直接修改。

12. **resume starting point**：L2 为 **T13（修复结果投影故障后）**，T14 必须等 T13 产生并由 Java/Scheduler 证实 schedule 后再继续；L3 无剩余业务起点，仅保留 reload recovery evidence。

## Authoritative reconciliation

T8 的原始 run `6c108a4d-53f2-42de-8b12-1998ecc8a792` 显示 `ACTION_LOOP_ITERATION_BUDGET/FAILED`，但 authoritative execution `9726ff03-cd37-4f73-a863-00ff704d9076`、operation `op-f8152607-79a4-5296-a90a-38a967f1bf00`、ActionObservation 和 Java/MySQL 均显示发布成功；post `350329167449034752` 为 `PUBLISHED`。因此标记为 COMPLETED，未重复发布。

恢复后的 T11/T12 写入也完成了完整闭环：

- T11 execution `8bbda72f-8827-4b3e-a1e5-7abaa6d1cdae` 创建 draft `350444831992057856`。
- T12 execution `038a23dd-86dc-4d63-8744-2260638af81d` 更新同一个 draft。
- 两个 operation 均 `SUCCEEDED`，两个 observation 均 `COMPLETED`，两个 queue message 均 `ACKED`。
- MySQL authoritative truth 为该 draft 仍是 `DRAFT`；没有对应 `scheduled_publications` row。

T13 的 semantic confirmation 曾发生真实前端点击，但两次都只得到 `RUN_RESULT_PROJECTION_FAILED`，没有 execution、queue、operation、observation 或 Java/MySQL publish/schedule。故将它停止为产品 blocker，而不是 RESULT_UNKNOWN；没有盲 retry T14。

## REAL_PROCESS_RESTART_RECOVERY result

这次真实电脑重启保存了以下可靠性证据：

| 维度 | 重启后结果 |
|---|---|
| Execution durable state | terminal state 保留；无 overnight pending execution |
| OperationLedger | 新增 T11/T12 均 SUCCEEDED；无新增 RESULT_UNKNOWN |
| Queue | 全部 ACKED；无 unacked message |
| ActionObservation | 新增写入均 COMPLETED |
| Java/MySQL truth | T8 publish 保留；T11/T12 draft truth 一致；T13 无副作用 |
| Resume | L2 从已有 T8/T9 checkpoint 继续；未重跑 L1；L3 仅 reload-only |
| Duplicate physical WRITE | 0 |
| Wrong resource | 0 |
| False success | 0 |
| Lost durable state | 0 |
| Frontend projection | 重新登录后恢复同一 canonical conversation；reload 后 AgentPanel 和历史消息可见 |

## Remaining evaluation status

Semantic、Safety、Performance、UX 的昨夜 evidence 已保留并未重跑：Semantic exact `55/78`、core `68/78`；provider unsafe `8/78` 全部被 containment，system unsafe `0/78`；Performance 55 samples 的 product P50/P95 为 `32.815s/66.832s`。

当前唯一阻断长会话完整收尾的是 L2 fresh T13 的重复结果投影故障。其证据、T13 两次 reconcile 结果及 T14 未执行原因均已保存；独立的 L3 reload recovery 已完成。

## Evidence

- [recovery JSON](../../.runtime/round1-final-v2/real-process-restart-recovery-20260825.json)
- [L2 T9–T14 recovery evidence](../../.runtime/round1-final-v2/l2-fresh-resume-after-reboot-t9-t14-20260825.json)
- [L2 T10 retry evidence](../../.runtime/round1-final-v2/l2-fresh-resume-after-reboot-t10-t14-20260825.json)
- [L2 T11/T12 evidence](../../.runtime/round1-final-v2/l2-fresh-resume-after-reboot-t11-t14-20260825.json)
- [L2 T13 reconcile evidence](../../.runtime/round1-final-v2/l2-fresh-resume-after-reboot-t13-t14-20260825.json)
- [L3 reload recovery evidence](../../.runtime/round1-final-v2/l3-fresh-reload-recovery-after-reboot-direct-20260825.json)
- [overnight semantic report](../archive/evaluations/artifacts/overnight_semantic_20260825/report.json)
- [overnight final report](./overnight_final_report_20260825.md)

`git status` 已检查；worktree 原本存在大量用户变更。恢复过程没有执行 `reset`、`clean`、`restore`，也没有修改 production source。
