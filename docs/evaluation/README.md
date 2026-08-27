# GreenBook Evaluation Index

This directory contains durable evaluation inputs and final summaries. Runtime
JSONL output, browser profiles, temporary logs, and checkpoint patches stay
outside the source surface or under the recovery archive.

## Evaluation areas

- Semantic decomposition: reusable code and datasets under
  `evaluation/semantic_longtail/`.
- Multi-Objective: `apps/backend/scripts/run_overnight_multi_objective_matrix.py`
  and the retained MO-01..MO-12 evidence index.
- RAG: canonical admission and fail-closed/grounding reports; current
  limitation is `RAG_CURRENT_LIMIT_ACCEPTED`.
- Memory: `scripts/memory_evaluation_harness.py` and memory authority tests.
- Performance: `agent_performance_baseline_v2_results.json`, the baseline
  report, and the focused AFTER artifact under `.runtime/`.
- Final acceptance: `greenbook_final_acceptance_precommit.json` and
  `docs/reports/GREENBOOK_FINAL_ACCEPTANCE_PRECOMMIT.md`.

## Test and measurement policy

Use focused regression first, then the affected failure family, then broad
regression at a phase boundary. Do not turn a one-sample observation into a
p50 claim. Provider token timestamps are unavailable, so TTFT is recorded as
`UNAVAILABLE`.

Historical generated snapshots are archived under
`docs/archive/evaluations/artifacts/`. They are evidence, not production data
or an active runtime source.
