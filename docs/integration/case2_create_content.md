# Phase 8 Case 2: Create a content draft

## Input

`帮我写一篇关于Agent的文章`

## Result

**PASS — real Agent, Worker, Creator, and Java draft handoff.**

| Entity | ID / result |
|---|---|
| Conversation | `5a6cd6d2-8076-4446-aa3f-e589c837fa44` |
| Agent run | `8ffd3e66-5045-4134-8e80-c45855e81475` |
| Execution | `9c86372a-b42d-4a48-9725-55a1643cda09` |
| GreenBook Task | `f4cf9386-34b0-4faa-8b56-ede2b4e606f2` |
| Creator Task | `c8013d3d-8ea1-4684-a632-03fd9df06ec8` |
| Creator Task status | `COMPLETED` |
| Creator Artifact | `art_68673ad11306c32c0e8829857a092c81f51bb1b0a09b08bf675cd2f298043d59` |
| Creator Artifact kind | `FINAL_CONTENT`; real artifact endpoint returned content |
| Java Draft | `345776621132845056` |
| Java Draft status | `draft`; real GET returned HTTP 200 |
| Final Execution | `COMPLETED` |

## Real chain

```text
Frontend
  -> Agent API / Conversation
  -> Command -> GoalTree -> Plan
  -> Postgres Execution Queue
  -> Worker claim
  -> Checkpoint / Ledger
  -> Creator API: create task
  -> Creator real Research / Writing / Artifact runtime
  -> Creator artifact query
  -> Java Agent Facade: create draft
  -> MySQL draft
  -> completion projection / frontend run status
```

## External call evidence

- Agent API accepted the message with HTTP 202.
- Worker logs show real Creator task creation and polling, then real Java draft creation and read-back.
- Creator task and artifact were queried through Creator HTTP endpoints, not through Creator internals.
- Java draft creation returned HTTP 201 and the draft read-back returned HTTP 200.
- The artifact and draft were not published as part of this case.
