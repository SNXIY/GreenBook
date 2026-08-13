# Phase 8 Case 4: Multi-turn task update

## Intended conversation

1. `帮我写一篇Java文章`
2. `改成面试方向，明天晚上发布`

The expected behavior is one existing Task/Goal updated through Conversation and Context, not a second independent task.

## Result

**BLOCKED — no clean post-fix real run could be completed.**

Earlier real attempts exposed the exact defect this case is meant to catch: a follow-up could create another task instead of binding to the active draft/task. The active-task/draft binding path was then corrected and its persistence tests passed. However, a clean post-fix run requires a new live LLM interpretation and Creator revision. The configured live provider subsequently returned:

```text
HTTP 402: Insufficient Balance
```

An earlier Creator attempt also reached the real Creator service and failed with `MODEL_CALL_FAILED`; no mock content was substituted.

## Historical evidence and correction

- Conversation `c539af2d-ad5f-4a85-b5ce-7cec68fa014f` contains the earlier initial task `1c44d3ae-97c4-4bdd-93c1-b4db1554586f` and a follow-up task `64464224...`, demonstrating the pre-fix duplicate-task behavior.
- The runtime now resolves the active draft/task before compiling a follow-up update; related session/task binding tests pass in the repository suite.
- This report does not claim the final two-message behavior passed because the required real LLM/Creator execution was unavailable after the provider balance was exhausted.

## Required rerun

After restoring a live LLM balance, rerun both messages in a fresh conversation and record:

- one stable GreenBook Task ID;
- one Goal revision / Plan Revision;
- Creator revision artifact ID;
- one schedule ID for the updated draft;
- no second independent content task.
