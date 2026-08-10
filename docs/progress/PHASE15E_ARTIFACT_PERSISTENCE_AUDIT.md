# Phase15-E Artifact Persistence + Schema Validation Audit

## Current state

The Phase15-D Artifact model contains identity, owner, metadata, reference
fields, and lifecycle state. However, the default storage path is still the
process-local `ArtifactRepository`, whose backing dictionary disappears when
the process restarts.

`ArtifactRegistry` currently wraps that repository directly. It can validate
IDs and perform lifecycle transitions, but it cannot recover artifacts from a
second process or share state between Assistant API and Worker processes.

`ArtifactStore` currently projects `ExecutionResult` into Artifact records and
resolves same-execution inputs. It is not yet a storage abstraction and the
projection metadata is not written to the Runtime PostgreSQL persistence
aggregate.

## Evidence, timeline, and graph relationship

- `ExecutionEvidence` is persisted inside execution events and external
  operation records. It records request/side-effect evidence, not Artifact
  metadata or Artifact lifecycle.
- `StepExecution` persists lightweight `ArtifactHandle` input/output values.
  This links an execution step to an artifact identity/type, but does not make
  the Artifact record recoverable by itself.
- `ExecutionTimelineService` reads canonical execution events and operation
  records. It has no Artifact event source yet.
- `ConversationTaskGraph` and the API agent timeline expose task/agent and
  artifact-reference information in the run projection, but that projection
  is not a durable Artifact store.

## Gaps

1. Artifact metadata and lifecycle do not survive process restart.
2. ArtifactRegistry is coupled to the in-memory repository instead of a Store
   boundary.
3. There is no `artifact_record` table with owner, schema, reference, hash,
   lifecycle, and updated-at fields.
4. A reference has no durable storage location contract backed by the same
   record as its metadata.
5. Agent contracts validate artifact type aliases, but not schema versions or
   required metadata fields before planning.
6. Lifecycle transitions do not have a central validator that rejects
   CREATED/ARCHIVED inputs and duplicate consumption.
7. Artifact lifecycle changes are not emitted as timeline facts.

## Phase15-E boundary

This phase adds a synchronous Store interface compatible with the existing
Runtime persistence adapters:

```text
ArtifactRegistry
        |
ArtifactStore contract
        |
MemoryArtifactStore / PostgresArtifactStore
```

PostgreSQL stores metadata and references only. Artifact bodies remain outside
the database. The existing Queue, Worker, ExecutionWorker, ToolRuntime,
retry/reconciliation, Java, and Creator core paths remain unchanged.

## Target persistence record

`artifact_record` stores `artifact_id`, type, task/execution/agent ownership,
lifecycle, schema version, metadata JSON, storage type/reference, content
hash, Artifact version, consumed-task IDs, and created/updated timestamps.
The Store maps the record back to the existing `Artifact` model so callers do
not need a second domain representation.

## Remaining future work

- A durable content backend (OSS/S3/Qdrant) is not part of this phase.
- Schema compatibility currently validates declared required fields and
  versions; it is not a full JSON Schema or migration engine.
- The existing execution event persistence remains the source of execution
  evidence; Artifact events are an additive timeline projection.
