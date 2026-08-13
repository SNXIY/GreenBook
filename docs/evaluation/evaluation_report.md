# GreenBook Agent Evaluation

The canonical evaluation implementation is `packages/evaluation/greenbook_evaluation`.

## Evaluated behavior

- Command accuracy
- Target resolution accuracy
- Goal decomposition accuracy
- Tool selection accuracy
- Task completion and plan success
- Replan and recovery behavior
- Clarification precision
- Side-effect safety and idempotent recovery
- Memory retrieval precision and context continuity
- Latency and tool-call count

## Golden cases

`greenbook_evaluation.dataset.GOLDEN_CASES` covers creation, multi-goal research/create/schedule, target references, ambiguity, task preemption, replan, Creator recovery, idempotent publication, cancellation, preference recall, and long context.

Cases are multi-turn behavioral contracts. They assert commands, targets, goals, selected tools, task states, artifacts, side effects, and forbidden actions; they do not score prose with BLEU/ROUGE and do not store hidden reasoning.

## Running

```powershell
uv run pytest -q tests/unit/test_agent_evaluation_runtime.py tests/evaluation
```

`EvaluationRunner` accepts deterministic fake LLM/tool handlers for unit tests and an injected runtime for integration evaluation. Trace identifiers are shared with the runtime (`conversation_id`, `task_id`, `goal_id`, `plan_version`, `execution_id`, `step_id`, `tool_name`, `context_snapshot_id`, and memory IDs).

The retired root `evaluation/` evaluator and Phase15-F multi-agent dataset were removed in Phase6B. Historical reports remain under archive/migration documentation only.
