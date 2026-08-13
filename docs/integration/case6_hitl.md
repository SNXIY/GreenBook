# Phase 8 Case 6: Human-in-the-loop publication approval

## Input

`立即发布这篇文章`

The active draft was `345776621132845056`.

## Result

**PARTIAL / FAIL-CLOSED — approval persistence and recovery passed; final publication failed authentication and was not replayed.**

| Entity | ID / result |
|---|---|
| Conversation | `5a6cd6d2-8076-4446-aa3f-e589c837fa44` |
| Agent run | `bbb64ee5-69b4-4ca6-8cb5-a6449f3b3060` |
| Execution | `14d4a201-a9a4-4f98-aad9-d88dec4600b7` |
| Approval request | `6b28fc9d-17bd-46ea-81df-440ecc357475` |
| Step | `publish_draft_now:1` |
| Capability | `PUBLISH_NOW` |
| Approval state | `PENDING` -> `APPROVED` |
| Final execution state | `FAILED` with `AUTHENTICATION_FAILED` |
| Java draft after failure | still `draft` on real GET |

## Observed real state transitions

```text
RUNNING
  -> WAITING_APPROVAL
  -> approval row persisted in PostgreSQL
  -> POST /api/v1/agent/executions/{execution_id}/approve
  -> APPROVED / RUNNING
  -> Worker resume and checkpoint replay
  -> Java publication request
  -> AUTHENTICATION_FAILED
  -> FAILED (fail closed; no blind retry)
```

The approval endpoint returned the execution as `RUNNING`. A service restart reconciled the durable approval and recovered the approved step. The approved queue payload contained `approval_granted=true`, and the real Worker invoked the real Java publication API.

## Safety evidence

The execution event recorded:

- `request_sent=true`;
- `side_effect_state=POSSIBLE`;
- `AUTH_FAILURE`;
- `requires_reconciliation=true`;
- retry denied because the external delivery boundary was not proven safe.

The Java draft remained in `draft` state when checked afterward. Because the request could have crossed an external side-effect boundary, the runtime correctly refused an automatic replay. This case is therefore not a successful publication, but it does validate durable approval, restart recovery, approval propagation, and fail-closed side-effect handling.

## Remaining action

Refresh the delegated Java user authorization before creating a new approval execution, then rerun this case from a fresh draft/operation. Do not manually replay the failed operation until its external operation status is reconciled.
