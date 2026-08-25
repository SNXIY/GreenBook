# L2 Fresh T13 Projection Recovery — 2026-08-25

## Outcome

T13 was recovered from safe checkpoint T12. The original two T13 failures were
not retried as business writes: both had zero Execution, Queue, Operation,
ActionObservation, and Java side effect. After the minimal continuation fix,
one real Frontend/CDP T13 confirmation produced exactly one durable Execution
and completed successfully.

T14 was then run once from the same Conversation and same draft resource. It
completed `PUBLISH_NOW`; Java/MySQL changed the T13 schedule to `CANCELLED` as
the expected consequence of immediate publication.

## FIRST BAD STATE

The confirmed semantic state was complete:

- Task: `c0abe0ec-0e03-4043-bc43-6b06446194d1`
- Confirmation version: `2`
- Confirmation snapshot hash: `8c13764c83fa498c354bb8c2205296d056bfc93134ffd8d2f94546b789e91f19`
- New Objective: `mutation-b721061d5599` / `CREATE_SCHEDULE`
- Owner: `854df72e-49ee-4839-87e1-983b088d8373`
- Target: draft `350444831992057856`, `TargetKind.DRAFT`
- Temporal: `2026-08-26T13:00:00Z`, timezone `Asia/Shanghai`
- Publication intent: `SCHEDULED_PUBLISH`

The first bad state was the lifecycle combination immediately after
confirmation:

```text
Task.status = COMPLETED
unsatisfied_objectives(Task) = [CREATE_SCHEDULE]
command = None                 # confirmed semantic continuation
active_execution_id = None
```

`ActionLoopExecutor.resume_task()` treated every terminal Task as closed and
returned `None` before TaskManager re-opened the Task and before ActionLoop or
`RuntimeAgentService.submit_plan()` admission. The runner then passed that
`None` to `handle_run_result()`, whose first projection access was
`result.run_id`; the resulting `AttributeError` was wrapped as
`RUN_RESULT_PROJECTION_FAILED`.

Relevant code locations:

- terminal continuation guard and fix:
  [action_loop_executor.py](/D:/agent/green-book/apps/agent_api/greenbook_agent_api/services/action_loop_executor.py:1084)
- projection wrapper:
  [runner.py](/D:/agent/green-book/apps/agent_api/greenbook_agent_api/runner.py:759)
- projection entry:
  [routes.py](/D:/agent/green-book/apps/agent_api/greenbook_agent_api/api/routes.py:1340)
- semantic confirmation materialization:
  [turn_coordinator.py](/D:/agent/green-book/apps/agent_api/greenbook_agent_api/services/turn_coordinator.py:1288)

This was therefore a continuation admission/lifecycle invariant failure, with
a secondary untyped `None` projection failure. It was not a missing semantic
field, target/resource reference, temporal field, objective owner, Java truth,
or `RESULT_UNKNOWN` condition.

T11 and T12 passed because neither exercised this exact path: their completed
predecessor state did not require a confirmed `command=None` continuation into
a newly appended Objective. T13 exposed the general family: a confirmed new
Objective appended to a previously terminal aggregate Task.

## Minimal fix

For `COMPLETED` or revisable `FAILED` Tasks, `resume_task()` now reopens through
TaskManager when `unsatisfied_objectives(task)` is non-empty. It still returns
without resuming when there is no continuation work, and `CANCELLED` Tasks
remain closed. No projection, retry, Java, or historical-residue behavior was
changed.

The regression contract covers three positive variants:

- `COMPLETED + UPDATE_DRAFT`
- `COMPLETED + CREATE_SCHEDULE`
- `FAILED + PUBLISH_NOW`

and two boundaries:

- terminal Task with no unsatisfied Objective remains closed;
- `CANCELLED` Task with a pending Objective remains closed.

## Verification

1. Targeted continuation/semantic/crash-resume tests: `24 passed`.
2. Expanded related regression set: `94 passed`.
3. Original T13 snapshot replay: both original failed snapshots now returned
   `COMPLETED`; each invoked ActionLoop once and performed zero physical writes.
4. Real Frontend/CDP T13 from the existing Conversation:
   `a7ca4a14-e2e4-48a6-abbc-6ed399c25514`.
5. T13 durable result:
   - Execution `be00ffbb-3bb7-450b-b0c1-e0249bc5e806`: `COMPLETED`
   - Operation `op-a0aafdd1-0ccb-5bf3-b0dd-be2bbec6af16`: `SUCCEEDED`
   - Queue message `b6de832c-4ec1-465f-abe6-895b855ae479`: `ACKED`
   - ActionObservation `efc54a35-334e-49e4-89b0-0b7d370ffdf8`: `DONE`
   - exactly one `SCHEDULE_PUBLISH` step and one schedule resource
     `350460882796548096`
6. Java and MySQL both confirmed that schedule for draft
   `350444831992057856` at `2026-08-26T13:00:00Z` with status `SCHEDULED`
   before T14.
7. T14 was run once from the same Conversation:
   - Run `f339e1b0-d723-4526-97c8-dfa5c5b5738e`
   - Execution `0b3a59b1-adab-49cb-b09f-e9df87eceaaa`: `COMPLETED`
   - same draft `350444831992057856`; no new Conversation
   - Java/MySQL schedule `350460882796548096`: `CANCELLED` after immediate publish

T13 reliability counts are all clean: duplicate physical WRITE `0`, wrong
resource `0`, false success `0`, lost state `0`. The T13 queue delivery had
multiple lease attempts during stale-worker recovery, but the durable ledger
operation attempt remained `1`; it did not create another Execution or business
write.

The reboot evidence remains separate in
[real_process_restart_recovery_20260825.md](/D:/agent/green-book/docs/reports/real_process_restart_recovery_20260825.md).
Historical `execution_control`, `WAITING_APPROVAL`, and `RESULT_UNKNOWN`
residue was not cleaned or rewritten. No reset, clean, or restore was used.
