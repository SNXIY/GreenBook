# Phase 8 Case 3: Schedule publication

## Input

`帮我明天上午9点发布这篇文章`

The active draft was the draft produced by Case 2.

## Result

**PASS — real time parsing, schedule persistence, and Java publication API.**

| Entity | ID / result |
|---|---|
| Conversation | `5a6cd6d2-8076-4446-aa3f-e589c837fa44` |
| Agent run | `b34e232b-878a-4090-a3a0-ec2f0947131b` |
| Execution | `929cf550-2813-418b-8746-fb969b734370` |
| Task | `65e49fb8-a0dd-45ac-a91b-dd1e71ffa20e` |
| Schedule | `345780237461753856` |
| Draft | `345776621132845056` |
| Execution status | `COMPLETED` |
| Schedule status | `SCHEDULED` |
| Stored UTC time | `2026-08-13T01:00:00Z` |
| Requested timezone | `Asia/Shanghai` |

## Real chain

```text
Agent API
  -> Command / active draft resolution
  -> Plan and durable Execution
  -> Worker
  -> publication.schedule tool
  -> MCP-compatible Tool Runtime
  -> Java Agent Facade publication schedule API
  -> MySQL schedule state
```

The Java schedule endpoint returned HTTP 201 and a subsequent real GET returned `SCHEDULED`, the draft ID, UTC `2026-08-13T01:00:00Z`, and timezone `Asia/Shanghai`. The case validates scheduling; it intentionally does not wait until the future publication time.

## Findings

- Relative time was converted to the expected Asia/Shanghai wall-clock meaning.
- The schedule remained attached to the intended draft.
- The schedule operation completed through the Worker and Java service; no direct database write was used.
