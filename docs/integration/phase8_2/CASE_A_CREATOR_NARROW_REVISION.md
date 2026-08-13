# Phase 8.2 Case A — Creator Narrow Revision

## Input

1. `帮我写一篇企业级Agent架构文章。`
2. `改得更偏Java工程师实战，增加Tool Runtime、Checkpoint、Ledger。`
3. `现在只改标题，正文不要动，标题突出企业级Agent可靠执行。`

## Execution Trace

```text
Agent API
  -> Conversation / Context
  -> existing Task resolution
  -> Goal Revision
  -> Plan Revision
  -> content.create_draft / content.revise_draft
  -> Creator Service revision contract
  -> Java draft update
  -> Artifact lineage projection
```

The third turn used `TITLE_ONLY`. It did not fall back to `CREATE_CONTENT` and did not create a second GreenBook Task.

## Evidence

| Field | Value |
|---|---|
| conversation_id | `b5286fc3-4ef9-4be7-9e11-a2d6ae6a7a40` |
| turn 1 run / execution | `7bed2ad4-070b-4847-b326-c437d5beee68` / `f42d67d1-9e95-41fb-8d56-0e2824645832` |
| turn 2 run / execution | `77832f84-f086-43b0-86f1-e437a3f003d6` / `fcc2d0fd-ac5d-410d-9b44-ea6fb5204e15` |
| turn 3 run / execution | `8618ab3f-3a64-426e-a4d8-70a66d89e450` / `d9aa7be9-3661-49c8-ad92-f770473e8a1d` |
| projection revalidation | run `678a6086-31bc-4f8d-b608-2b93536fdc79`, execution `27c9cb85-bd26-482c-8e49-c49a39cab71f` |
| Creator scopes | `CONTENT_ONLY`, then `TITLE_ONLY` |
| title-only Creator artifact | `art_3f35e2240df51f860e1c6bc3e4d844a3c7da958b2e0051984ae43c42902608a4` |
| Java draft | `345881838637682688` |
| body SHA after title-only revision | `534e7e514f1db845e106ba80147c7181f9ff7da3d3ea9c60d94fc3837ca68013` |

The body length and SHA remained unchanged after the title-only revision while the title changed. Creator artifact versions increased along the same logical lineage.

## Result

**PASS — real environment.**

## Problem

Phase 8.1 returned HTTP 422 for a title-only Creator revision.

## Fix

The revision request now carries an explicit revision scope, requested title, revision instruction, and source Creator artifact reference. The Creator client and MCP content handler preserve the narrow revision boundary.

## Remaining Evidence

The API does not expose a numeric GreenBook `goal_tree_version` in the public run projection; that field is `N/A` rather than inferred. Task continuity, Creator lineage, and Java draft continuity are directly evidenced above.

