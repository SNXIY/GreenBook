# Execution Runtime

Reliable Execution is the canonical runtime state machine. It receives typed
`ExecutionInput` after planning has resolved capability, tool, arguments,
dependencies, policy snapshot, and idempotency data.

```text
ExecutionInput
  -> ExecutionSubmissionService
  -> ExecutionRepository + ExecutionEventStore
  -> ExecutionQueue
  -> Agent Worker / ExecutionWorker
  -> ToolRuntime -> MCP handler or external client
  -> checkpoint, ledger/evidence, artifact, recovery, completion projection
```

Execution owns queue leases, retry decisions, checkpoints, idempotency,
side-effect evidence, artifact lifecycle, human approval pauses, recovery, and
resume. A worker never consumes raw user messages, Command, Goal text, or a
planner request.

`execution_id` is the canonical runtime identity. `run_id` is retained only
for the public history projection and the `assistant_runs` compatibility table.
`RunExecutionAdapter` remains at that history boundary until its external
history consumers are retired.
