# Phase15-F Final Multi-Agent E2E + Evaluation

## Result

Final business E2E status: **BLOCKED_BY_ENV**.

The live infrastructure health gate passed:

| Service | Result |
|---|---|
| Java Backend | READY |
| Creator Agent | READY |
| Assistant API | READY |
| Assistant Worker | READY |
| PostgreSQL | READY |
| MySQL | READY |

The worker was started with the existing `scripts/start-assistant-worker.ps1`
and reported `storage=postgres`, `queue_consumer=true`, and `READY`.

The real user login prerequisite was not configured: neither
`GREENBOOK_E2E_ACCESS_TOKEN` nor `GREENBOOK_E2E_IDENTIFIER` +
`GREENBOOK_E2E_PASSWORD` was available. The runner therefore did not submit a
conversation or create any draft/schedule. No mock Java/Creator response was
used.

## Final expected TaskGraph

```text
Task A: Java post retrieval
  SearchAgent -> POST_COLLECTION
        |
Task B: Java content analysis
  AnalyticsAgent -> POST_ANALYSIS
        |
Task C: Java article creation
  CreatorAgent -> CONTENT_DRAFT
        |
Task D: Java scheduled publication
  PublishAgent -> SCHEDULE / PUBLISHED_POST

Task E: Redis interview content
  CreatorAgent -> CONTENT_DRAFT
```

Task E is independent and must not consume Java artifacts.

## Agent and Artifact contracts

The runtime implementation contains the expected routing and contract checks:

- SearchAgent accepts the community query and produces `POST_COLLECTION`.
- AnalyticsAgent consumes `POST_COLLECTION` and produces `POST_ANALYSIS`.
- CreatorAgent consumes analysis and produces `CONTENT_DRAFT`.
- PublishAgent consumes `CONTENT_DRAFT` and produces `SCHEDULE` or
  `PUBLISHED_POST`.
- Artifact lifecycle and schema checks run before an input is consumed.

The live business execution could not reach these stages because user
authentication was blocked by environment configuration.

## Cross-turn validation status

The evaluation dataset includes the required five-round conversation:

1. Java schedule/title update without touching Redis.
2. Redis content revision without changing Java.
3. Weak reference to the title-modified Java article.
4. Read-only status query for both articles.

These cases are dataset expectations only in this run; they are not marked as
live PASS because no authenticated conversation was submitted.

## Java / Creator actual calls

- Health endpoints were called successfully.
- Java login was not called because credentials were absent.
- Creator task creation was not called.
- Java draft creation, draft update, and schedule creation were not called.

This distinction is intentional: service readiness is not business E2E success.

## Database side effects

- PostgreSQL TCP readiness: passed.
- MySQL TCP readiness: passed.
- No new draft, schedule, or Creator task was created by Phase15-F.
- Artifact persistence and lifecycle behavior remain covered by the Phase15-E
  SQLite/PostgreSQL adapter tests.

## Evaluation framework

Added:

- `evaluation/runtime_eval.py`
- `evaluation/task_graph_eval.py`
- `evaluation/tool_eval.py`
- `evaluation/artifact_eval.py`
- `scripts/run-agent-evaluation.py`
- `evaluation/datasets/*.jsonl`

Supported metrics:

- Task decomposition accuracy
- Target resolution accuracy
- Planner accuracy
- Tool success rate
- Runtime success rate
- Recovery rate
- Artifact resolution accuracy

The live runner output was:

```text
Status: BLOCKED_BY_ENV
Reason: GREENBOOK_E2E_ACCESS_TOKEN or login credentials are missing
```

All business accuracy metrics are therefore `BLOCKED_BY_ENV`, not numeric
PASS values. The report contains 12 badcases corresponding to the cases with
no live observations.

## Tests

- Phase15-F evaluator tests: `4 passed`
- Phase15-B/C/D/E, Artifact persistence, Timeline, Queue/Worker regression:
  `61 passed`

The initial test attempt using an external `D:\tmp` basetemp was denied by
the environment; rerunning with a workspace basetemp passed.

## Remaining gaps

1. Configure a dedicated real USER E2E account or access token.
2. Re-run the five-round conversation against the already healthy services.
3. Capture execution IDs, Artifact records/events, Creator task IDs, draft IDs,
   and schedule IDs from that authenticated run.
4. Perform the requested Worker restart and verify Artifact recovery without
   regenerating upstream outputs.

No Runtime architecture expansion, Kafka migration, Docker migration, or
Legacy Cleanup was performed.
