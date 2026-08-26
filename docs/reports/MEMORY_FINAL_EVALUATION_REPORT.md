# Memory Final Evaluation Report

Evaluation-only run on the completed Preference Memory vertical slice at
production HEAD `bc8adca`. No production Memory logic was modified and no Git
commit or push was performed by this harness.

## Dataset Summary

| Evaluation | Cases |
|---|---:|
| Extraction | 114 |
| Retrieval | 100 |
| Isolation | 100 |
| Lifecycle | 5 |
| Injection analysis | 30 |

## Metrics Summary

- Extraction precision: **0.8889**;
  recall: **0.7619**; false-positive rate:
  **0.1176**.
- Retrieval Recall@1/3/5:
  **1.0000** /
  **1.0000** /
  **1.0000**.
- Retrieval Precision@1/3/5:
  **1.0000** /
  **0.3333** /
  **0.2000**.
- Cross-user leakage: **0**;
  cross-tenant leakage: **0**.
- Lifecycle cases passed: **5/5**.
- Positive injection alignment: **1.0000**;
  harmful injection candidates: **0.2000**.

## Failure Cases

- Extraction boundary failures are listed in `MEMORY_EXTRACTION_EVALUATION.md`.
- Retrieval misses and irrelevant-memory returns are listed in
  `MEMORY_RETRIEVAL_EVALUATION.md`.
- Isolation has no cross-user or cross-tenant leak in this fixture; same-scope
  irrelevant returns are separately recorded.
- Injection analysis identifies unrelated-request memory injection as the main
  quality risk.

## Architecture Findings

- Preference Memory is read and written through MemoryRecord/repository contracts; it does not become a second Conversation, Task, Objective, Execution, or Observation truth source.
- The source Conversation ID is provenance only; cross-Conversation recall is intentional for reusable preferences.
- ContextBuilder/ContextAssembler add bounded preference evidence, while the Interpreter projection strips internal identities.
- No evaluation asset changes ActionLoop, TaskManager, MCP, RAG, or Java business code.

Evaluation scope check: **PASS**.
Out-of-scope dirty paths: `[]`.

## Recommended Next Step

Add a retrieval relevance threshold or explicit no-match policy, then rerun the
retrieval and injection evaluations. This is the highest-value follow-up
because unrelated same-scope requests currently receive bounded but irrelevant
preference evidence. Keep that change separate from this evaluation-only run.
