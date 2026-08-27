# GreenBook Final Performance Acceptance

Date: 2026-08-27
Branch: `feature/hybrid-search-rag`

## Verdict

`STRICT_ACCEPTANCE_INCONCLUSIVE`

The BEFORE corpus is the existing V2 measurement with three samples per
admitted scenario. An equivalent AFTER run was attempted through the same
public Agent boundary. The results are recorded as completed and failed
samples separately; failed requests are never included in successful latency
percentiles.

| Scenario | BEFORE p50 | AFTER completed/total | AFTER success-only p50 | Verdict |
| --- | ---: | ---: | ---: | --- |
| Simple READ | 38,776 ms | 2/3 | 152,789 ms | Inconclusive |
| Simple WRITE | 44,143 ms | 2/3 | 65,704 ms | Inconclusive |
| Draft -> Schedule | 55,824 ms | 3/3 | 105,043 ms | Regressed or environment variance |
| Search + Creation | 61,792 ms | 1/3 | 214,151 ms | Inconclusive |
| 2 independent CREATE | Browser serial 73,859 ms | 2/3 | 99,553 ms | Inconclusive |

The AFTER run exposed request timeouts and materially higher queue/LLM
latency. This is evidence for diagnosis, not permission to immediately change
execution code. No performance optimization was introduced in this phase.

## Metrics and limits

- p95 is descriptive only or unavailable for these small/failed groups.
- Provider token timestamps are unavailable; TTFT remains `UNAVAILABLE`.
- TUF remains `UNAVAILABLE` in the controlled DOM observations.
- Java remained a small component of completed samples (approximately
  54–278 ms); it is not the critical-path target.
- Equivalent three-sample corpora for RAG, three-objective, HITL, relevant
  Memory, and irrelevant Memory were not available and are not fabricated.

Raw per-scenario artifacts are under `.runtime/` and are summarized in
[final_system_evaluation_results.json](../evaluation/final_system_evaluation_results.json).
