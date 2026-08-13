# Phase 8.2 Case D — Human Approval and Long-Task Recovery

## Input

`帮我生成一篇Java Agent文章，最终立即发布，但发布前必须让我确认。`

## Execution Trace

```text
Creator
  -> Java draft
  -> WAITING_APPROVAL
  -> Agent API / Worker stopped and restarted
  -> durable approval and checkpoint restored
  -> approve
  -> resume same Execution
  -> Java publication
```

## Evidence

| Field | Value |
|---|---|
| conversation_id | `22e315f9-da9a-4051-922d-aaebb292cbc7` |
| run_id | `c3f96d9b-2526-43cd-8065-58197c91022d` |
| execution_id | `7cd4238c-d06a-4edc-a55b-5df637f1720d` |
| approval_id | `43f237ed-abfc-4da6-8523-b7ae08e2de54` |
| draft artifact | `035e48d2-ed24-4ef6-a266-2685ba595e24` |
| Java draft | `345924025047977984` |
| Creator task / artifact | `6a52681c-9b19-443b-8097-e3e7a7c9aa94` / `art_634bf63d33d4c0e8b276aa008615429233fce01c91df556521e2bfab67ec13f0` |
| final publication artifact | `88885a32-f32d-4f27-a58c-593b7d7f7bc6` |
| publication tool call | `c5238855-1a2e-42e3-b8b4-8cb1861e75b3` |

The Creator task and Java draft were not duplicated after restart. The same Execution resumed from durable state and the publication operation occurred once.

## Result

**PASS — real approval persistence and recovery.**

## Problem

Phase 8.1 exposed incomplete final publication evidence projection.

## Fix

Completion projection now preserves external resource references and approval-linked artifact references. This case contains the Java publication post reference in the final evidence.

