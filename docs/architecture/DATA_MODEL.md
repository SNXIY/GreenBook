# Data Model

GreenBook separates durable facts, working projections, and runtime state.

| Domain | Canonical owner | Meaning |
| --- | --- | --- |
| Conversation | `ConversationRepository` / `ConversationService` | messages, summary, preferences, authenticated session facts |
| Context | `ContextBuilder` / `ContextSnapshot` | bounded decision projection; never a second database |
| Goal | `GoalTree` | desired result and semantic dependencies |
| Task | `TaskManager` / Task repositories | durable lifecycle, priority, preemption, resume, plan revision history |
| Plan | `planning/contracts.py` | `TaskPlan`, `PlanStep`, `PlanRevision`, `PlanningDecision`, `PlanGraph` |
| Execution | `ExecutionRepository` | runtime state, steps, events, queue, checkpoints, leases, recovery |
| Artifact | Artifact repositories | typed outputs and resource references |
| Memory | Memory repositories | preferences, episodes, semantic recall, and validated facts |
| Run | `assistant_runs` history projection | public history compatibility view of runtime results |

`execution_id` is the runtime identity. `run_id` is not used as a worker or
queue identity. The retained history adapter must not become a second
execution state machine.
