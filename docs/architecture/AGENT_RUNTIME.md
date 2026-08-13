# Agent Runtime

The GreenBook Agent Runtime is the single active Python intelligence surface:
`packages/agent_core`, composed by `apps/agent_api` and `apps/agent_worker`.

## Decision pipeline

1. `ConversationService` loads durable conversation facts.
2. `ContextBuilder` joins bounded Task, Goal, Execution, Artifact, and Memory projections into `ContextSnapshot`.
3. `CommandInterpreter` produces a typed `Command`.
4. `GoalDecomposer` produces a `GoalTree`.
5. `TaskManager` owns durable Task lifecycle and preemption/resume.
6. `AgentLoop` observes state, selects a decision, and reflects on the result.
7. `DynamicPlanner` may revise a typed plan. `ToolSelector` selects from `ToolMetadata`.
8. `ToolPolicyGate` evaluates the selected tool's canonical policy.
9. `GoalCompiler` produces `TaskPlan` and `PlanStep`; `ExecutionInput` crosses into Reliable Execution.

The Agent Runtime does not contain a second Intent router, workflow-template
planner, or product-specific specialist runtime.

## Contract rules

- Capability names are semantic catalog entries such as `SEARCH_COMMUNITY` and `GENERATE_CONTENT`.
- Tool names are `domain.operation`, for example `community.search_public_posts`.
- Tool policy is read from `packages/contracts` and is not stored on Capability.
- Agent Core never imports API route modules.
- Creator internals are reached through `CreatorClient` and the Creator API only.
