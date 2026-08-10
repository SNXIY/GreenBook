# Phase15-D Artifact Lifecycle + Agent Registry Audit

## Scope

This audit covers the existing Artifact, execution result, tool result, query
handler, Creator boundary, and task-graph integration. Runtime queue,
Assistant Worker, ToolRuntime, retry/failure handling, Java, and Creator
implementation are intentionally out of scope.

## Existing capability before Phase15-D

GreenBook already had several related but separate representations:

- `ExecutionResult.artifact` and `StepExecution.output_artifact` represented a
  lightweight execution-local `ArtifactHandle`.
- `ArtifactStore` converted completed execution results into `Artifact` records.
- `ArtifactRepository` stored and queried those records by task, execution,
  step, or type.
- `ToolResult` exposed MCP data and `ResourceRef` values for a tool call.
- Phase15-C `QueryHandler` returned `ArtifactRef` values and the conversation
  adapter passed those references between graph nodes.

This was enough for basic same-execution presentation and Phase15-C query
handoff, but it was not yet one lifecycle contract. The records had no
standard lifecycle state, owning agent, content hash, storage location, size,
version, or independent registry API. `ArtifactStore` also remained an
execution-result projection rather than the authoritative artifact boundary.

## Gaps found

1. There was no unified model covering identity, metadata, reference, and
   lifecycle in one object.
2. An artifact could be found, but there was no explicit validation or state
   transition for available, consumed, archived, or failed output.
3. Cross-task references existed in task context, but registration and
   consumption were not centrally validated.
4. Agent routing was capability/branch based. There was no registry declaring
   the artifact contract and side-effect level of the selected agent.
5. Timeline data exposed execution/task IDs, but not the selected agent and
   artifact inputs/outputs as a stable projection.
6. Query results were returned through the read-only handler, while Creator
   still receives its normal compact instruction/reference boundary. The new
   path keeps large query data out of planner prompts and carries only
   `ArtifactReference` metadata across tasks.

## Phase15-D design and implementation

`Artifact` now has a compatibility-preserving lifecycle contract:

- identity: `artifact_id`, `artifact_type`, task/execution owners,
  `created_by_agent`;
- metadata: schema, size, created time, and version;
- reference: storage type, location, and content hash;
- lifecycle: `CREATED`, `AVAILABLE`, `CONSUMED`, `ARCHIVED`, `FAILED`.

`ArtifactReference` is the small cross-agent payload. It contains identity and
storage metadata, never the artifact body. `ArtifactRegistry` wraps the
existing `ArtifactRepository` and owns registration, lookup, reference
validation, consumption, archival, and failure transitions. It is deliberately
separate from execution persistence.

`AgentRegistry` exposes `AgentMetadata` with capabilities, accepted input and
output artifact types, and side-effect level. The planner resolves and
validates an agent for every goal. The task graph records the selected agent,
dependencies, and artifact inputs/outputs. The API run projection now includes
the graph nodes as an agent timeline for later UI use.

## Current end-to-end boundary

```text
Query/SearchAgent
    -> ArtifactRegistry: POST_COLLECTION / POST_ANALYSIS
    -> ArtifactReference
    -> CreatorAgent: CONTENT_DRAFT
    -> ArtifactReference
    -> PublishAgent: SCHEDULE / PUBLISHED_POST
```

Query nodes remain read-only and do not create an Execution. Action nodes keep
using the existing Execution Queue and Worker path; only agent resolution and
artifact metadata exchange were added around it.

## Remaining limitations

- The default registry is process-local and backed by the existing repository;
  durable artifact storage and content-addressed blobs are future work.
- Query/Creator/Publish adapters still depend on the existing MCP and external
  service contracts; this phase does not replace those integrations.
- Artifact schema negotiation is type/alias based, not a full JSON Schema
  compatibility engine.
- The frontend has not been changed to render the timeline; the API now
  exposes the data needed for that follow-up.
