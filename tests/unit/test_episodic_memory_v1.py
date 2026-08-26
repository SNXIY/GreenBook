from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from greenbook_agent_core.command import CommandContext
from greenbook_agent_core.context import ContextBuilder
from greenbook_agent_core.context.projection import project_interpreter_context
from greenbook_agent_core.execution.action_observation import (
    ActionObservation,
    ActionObservationStore,
    ActionObservationWriter,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.models import ExecutionStatus, PlanExecution
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.memory import (
    CONTENT_PUBLICATION_CATEGORY,
    CONTENT_PUBLICATION_OUTCOME,
    EPISODIC_MEMORY_CONTRACT,
    EpisodeCandidateBuilder,
    EpisodicMemoryProjector,
    EpisodicMemoryService,
    InMemoryMemoryRepository,
    MemoryManager,
    MemoryRecord,
    MemoryRetriever,
    MemoryType,
    PreferenceRetriever,
    VerifiedBusinessOutcome,
    WorthRememberingDecision,
)
from greenbook_agent_core.task.models import (
    Objective,
    ObjectiveStatus,
    Task,
    TaskResourceRef,
    TaskRevision,
    TaskRevisionType,
)
from greenbook_contracts.identity import AuthContext


def _inputs(
    *,
    observation_id: str = "observation-1",
    task_id: str = "task-1",
    objective_id: str = "objective-1",
    user_id: str = "user-1",
    tenant_id: str = "tenant-1",
    observation_status: str = "COMPLETED",
    objective_status: ObjectiveStatus = ObjectiveStatus.COMPLETED,
    verified: bool = True,
    revision_fields: list[str] | None = None,
    user_initiated_revision: bool = True,
) -> tuple[ActionObservation, Objective, VerifiedBusinessOutcome, str, str]:
    observation = ActionObservation(
        observation_id=observation_id,
        execution_id=f"execution-{observation_id}",
        task_id=task_id,
        conversation_id="conversation-1",
        status=observation_status,
        resource_refs=[
            {"resource_type": "POST", "resource_id": f"post-{observation_id}"}
        ],
        observed_at="2026-08-26T08:00:00+00:00",
    )
    objective = Objective(
        objective_id=objective_id,
        task_id=task_id,
        description="Publish technical content",
        intent="CONTENT_PUBLICATION",
        status=objective_status,
    )
    outcome = VerifiedBusinessOutcome(
        task_id=task_id,
        objective_id=objective_id,
        category=CONTENT_PUBLICATION_CATEGORY,
        summary=(
            "The user revised the title and publication time before a verified "
            "technical content publication."
        ),
        outcome=CONTENT_PUBLICATION_OUTCOME,
        occurred_at="2026-08-26T08:00:00+00:00",
        confidence=0.95,
        verified=verified,
        source_type="VERIFIED_BUSINESS_OUTCOME",
        revision_fields=revision_fields or ["title", "publish_time"],
        user_initiated_revision=user_initiated_revision,
        verified_resource_kinds=["POST"],
    )
    return observation, objective, outcome, user_id, tenant_id


def _service(repository: InMemoryMemoryRepository) -> EpisodicMemoryService:
    return EpisodicMemoryService(MemoryManager(repository=repository))


def _strict_retriever(repository: InMemoryMemoryRepository) -> MemoryRetriever:
    return MemoryRetriever(
        repository,
        memory_types=(MemoryType.EPISODIC,),
        include_legacy_episodic=False,
        require_tenant_scope=True,
        relevance_threshold=0.5,
        confidence_threshold=0.5,
    )


def _task_for_projector(
    *,
    task_id: str,
    objective_id: str,
    user_id: str,
    tenant_id: str,
    post_id: str,
) -> Task:
    return Task(
        task_id=task_id,
        conversation_id="conversation-1",
        user_id=user_id,
        tenant_id=tenant_id,
        goal="Publish technical content",
        objectives=[Objective(
            objective_id=objective_id,
            task_id=task_id,
            description="Publish technical content",
            intent="CONTENT_PUBLICATION",
            status=ObjectiveStatus.COMPLETED,
        )],
        resource_index=[TaskResourceRef(
            resource_id=post_id,
            resource_kind="POST",
            objective_id=objective_id,
            status="PUBLISHED",
        )],
        revisions=[TaskRevision(
            task_id=task_id,
            type=TaskRevisionType.MODIFY_GOAL,
            payload={
                "kind": "ACTION_LOOP_MUTATION_PLAN",
                "task_changes": [{
                    "operation": "UPDATE_GOAL",
                    "target_reference": {"objective_id": objective_id},
                    "desired_changes": {
                        "objective_id": objective_id,
                        "title": "A revised title",
                        "run_at": "2026-08-26T08:00:00Z",
                    },
                }],
            },
        )],
    )


class _TaskProvider:
    def __init__(self, task: Task) -> None:
        self.task = task

    async def get_task(self, scope, task_id):
        if (
            scope.user_id == self.task.user_id
            and scope.tenant_id == self.task.tenant_id
            and scope.conversation_id == self.task.conversation_id
            and task_id == self.task.task_id
        ):
            return self.task
        return None


class _ExecutionRepository:
    def __init__(self, execution: PlanExecution) -> None:
        self.execution = execution

    def find_by_id(self, execution_id: str):
        return self.execution if execution_id == self.execution.execution_id else None


def test_verified_revision_builds_one_candidate_and_canonical_episode() -> None:
    observation, objective, outcome, user_id, tenant_id = _inputs()
    builder = EpisodeCandidateBuilder()

    candidate = builder.build(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    assert candidate is not None
    assert candidate.source_type == "VERIFIED_ACTION_OBSERVATION"
    assert candidate.category == CONTENT_PUBLICATION_CATEGORY
    assert candidate.outcome == CONTENT_PUBLICATION_OUTCOME
    assert candidate.provenance["memory_contract"] == EPISODIC_MEMORY_CONTRACT
    assert "execution_id" not in candidate.summary

    repository = InMemoryMemoryRepository()
    record = _service(repository).write(candidate)
    assert record is not None
    assert record.memory_type == MemoryType.EPISODIC
    assert record.task_id is None
    assert record.metadata["memory_contract"] == EPISODIC_MEMORY_CONTRACT


@pytest.mark.parametrize(
    ("observation_status", "objective_status", "verified"),
    [
        ("RESULT_UNKNOWN", ObjectiveStatus.COMPLETED, True),
        ("FAILED_RETRYABLE", ObjectiveStatus.COMPLETED, True),
        ("WAITING_EXTERNAL", ObjectiveStatus.COMPLETED, True),
        ("COMPLETED", ObjectiveStatus.PENDING, True),
        ("COMPLETED", ObjectiveStatus.COMPLETED, False),
    ],
)
def test_unverified_or_non_terminal_inputs_do_not_build_episode(
    observation_status: str,
    objective_status: ObjectiveStatus,
    verified: bool,
) -> None:
    observation, objective, outcome, user_id, tenant_id = _inputs(
        observation_status=observation_status,
        objective_status=objective_status,
        verified=verified,
    )

    candidate = EpisodeCandidateBuilder().build(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    assert candidate is None


def test_ordinary_publication_and_single_revision_are_dropped() -> None:
    observation, objective, outcome, user_id, tenant_id = _inputs(
        revision_fields=[],
        user_initiated_revision=False,
    )
    service = _service(InMemoryMemoryRepository())

    candidate = service._builder.build(  # noqa: SLF001 - contract test
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    assert candidate is None
    assert service.evaluate(candidate).effective_decision == WorthRememberingDecision.DROP


def test_unknown_policy_decision_is_write_disabled() -> None:
    observation, objective, outcome, user_id, tenant_id = _inputs()
    candidate = EpisodeCandidateBuilder().build(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    )
    assert candidate is not None
    low_confidence = candidate.model_copy(update={"confidence": 0.4})
    service = _service(InMemoryMemoryRepository())

    decision = service.evaluate(low_confidence)

    assert decision.decision == WorthRememberingDecision.UNKNOWN
    assert decision.effective_decision == WorthRememberingDecision.DROP
    assert not decision.should_write
    assert service.write(low_confidence) is None


@pytest.mark.parametrize("mismatch", ["task", "objective"])
def test_candidate_requires_exact_task_and_objective_join(mismatch: str) -> None:
    observation, objective, outcome, user_id, tenant_id = _inputs()
    if mismatch == "task":
        outcome = outcome.model_copy(update={"task_id": "different-task"})
    else:
        outcome = outcome.model_copy(update={"objective_id": "different-objective"})

    candidate = EpisodeCandidateBuilder().build(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    assert candidate is None


def test_untrusted_tool_result_is_not_a_verified_business_source() -> None:
    observation, objective, outcome, user_id, tenant_id = _inputs()
    outcome = outcome.model_copy(update={"source_type": "TOOL_RESULT"})

    candidate = EpisodeCandidateBuilder().build(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    assert candidate is None


def test_replaying_same_verified_source_is_idempotent() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)
    observation, objective, outcome, user_id, tenant_id = _inputs()

    first = service.process(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    )
    second = service.process(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    assert first is not None and second is not None
    assert first.memory_id == second.memory_id
    assert repository.count("user-1") == 1


@pytest.mark.asyncio
async def test_retrieval_is_cross_conversation_but_user_and_tenant_scoped() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)
    for user_id, tenant_id, source in (
        ("user-1", "tenant-1", "same-scope"),
        ("user-2", "tenant-1", "other-user"),
        ("user-1", "tenant-2", "other-tenant"),
    ):
        observation, objective, outcome, _, _ = _inputs(
            observation_id=source,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        assert service.process(
            observation=observation,
            objective=objective,
            verified_outcome=outcome,
            user_id=user_id,
            tenant_id=tenant_id,
        ) is not None

    values = await _strict_retriever(repository).retrieve(
        user_id="user-1",
        tenant_id="tenant-1",
        conversation_id="new-conversation",
        target_query="technical content publication revised title publication time",
        touch=False,
    )

    assert len(values) == 1
    assert values[0].tenant_id == "tenant-1"
    assert values[0].source_id == "same-scope"


@pytest.mark.asyncio
async def test_irrelevant_query_returns_no_episode() -> None:
    repository = InMemoryMemoryRepository()
    observation, objective, outcome, user_id, tenant_id = _inputs()
    assert _service(repository).process(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    ) is not None

    values = await _strict_retriever(repository).retrieve(
        user_id=user_id,
        tenant_id=tenant_id,
        target_query="weather forecast and astronomy",
        touch=False,
    )

    assert values == []


@pytest.mark.asyncio
async def test_legacy_episodic_isolation_and_preference_separation() -> None:
    repository = InMemoryMemoryRepository()
    legacy = repository.save(MemoryRecord(
        user_id="user-1",
        tenant_id="tenant-1",
        memory_type=MemoryType.EPISODIC,
        content="technical content publication revised title publication time",
        confidence=1.0,
        structured_metadata={"status": "COMPLETED"},
    ))
    observation, objective, outcome, user_id, tenant_id = _inputs()
    episode = _service(repository).process(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    )

    values = await _strict_retriever(repository).retrieve(
        user_id=user_id,
        tenant_id=tenant_id,
        target_query="technical content publication revised title publication time",
        touch=False,
    )
    preference_values = await PreferenceRetriever(repository).retrieve(
        user_id=user_id,
        tenant_id=tenant_id,
        query="technical content publication",
    )

    assert episode is not None
    assert [item.memory_id for item in values] == [episode.memory_id]
    assert legacy.memory_id not in {item.memory_id for item in values}
    assert preference_values == []


@pytest.mark.asyncio
async def test_context_marks_episode_as_past_experience_and_hides_provenance() -> None:
    repository = InMemoryMemoryRepository()
    observation, objective, outcome, user_id, tenant_id = _inputs()
    assert _service(repository).process(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    ) is not None
    builder = ContextBuilder(memory_retriever=_strict_retriever(repository))

    snapshot = await builder.build(
        conversation_id="new-conversation",
        user_id=user_id,
        tenant_id=tenant_id,
        target_query="technical content publication revised title publication time",
    )
    provider_context = project_interpreter_context(
        CommandContext.from_any(snapshot)
    )
    serialized = json.dumps(provider_context, ensure_ascii=False)

    assert snapshot.recalled_memories[0]["memory_role"] == "relevant_past_experience"
    assert "provenance" not in snapshot.recalled_memories[0]["structured_metadata"]
    assert "execution-observation-1" not in serialized
    assert "post-observation-1" not in serialized
    assert provider_context["recalled_memories"][0]["memory_role"] == (
        "relevant_past_experience"
    )


@pytest.mark.asyncio
async def test_projector_uses_exact_persisted_objective_join() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)
    observation, objective, outcome, user_id, tenant_id = _inputs()
    del objective, outcome
    execution = PlanExecution(
        execution_id=observation.execution_id,
        task_id=observation.task_id,
        objective_id="objective-1",
        status=ExecutionStatus.COMPLETED,
    )
    task = _task_for_projector(
        task_id=observation.task_id,
        objective_id="objective-1",
        user_id=user_id,
        tenant_id=tenant_id,
        post_id="post-observation-1",
    )
    projector = EpisodicMemoryProjector(
        service=service,
        execution_repository=_ExecutionRepository(execution),
        task_provider=_TaskProvider(task),
    )

    record = await projector(
        observation=observation,
        message=ExecutionQueueMessage(execution_id=observation.execution_id),
        result=SimpleNamespace(status="COMPLETED"),
        auth=AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=[],
            timezone="Asia/Shanghai",
            raw_access_token="",
        ),
        execution=execution,
    )

    assert record is not None
    assert record.metadata["memory_contract"] == EPISODIC_MEMORY_CONTRACT
    assert record.metadata["provenance"]["objective_id"] == "objective-1"


@pytest.mark.asyncio
async def test_projector_rejects_missing_objective_correlation_without_fallback() -> None:
    repository = InMemoryMemoryRepository()
    observation, _, _, user_id, tenant_id = _inputs()
    execution = PlanExecution(
        execution_id=observation.execution_id,
        task_id=observation.task_id,
        objective_id=None,
        status=ExecutionStatus.COMPLETED,
    )
    task = _task_for_projector(
        task_id=observation.task_id,
        objective_id="objective-1",
        user_id=user_id,
        tenant_id=tenant_id,
        post_id="post-observation-1",
    )
    projector = EpisodicMemoryProjector(
        service=_service(repository),
        execution_repository=_ExecutionRepository(execution),
        task_provider=_TaskProvider(task),
    )

    record = await projector(
        observation=observation,
        message=ExecutionQueueMessage(
            execution_id=observation.execution_id,
            payload={"objective_id": "objective-1", "active_task_id": task.task_id},
        ),
        result=SimpleNamespace(status="COMPLETED"),
        auth=AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=[],
            timezone="Asia/Shanghai",
            raw_access_token="",
        ),
        execution=execution,
    )

    assert record is None
    assert repository.count(user_id) == 0


@pytest.mark.asyncio
async def test_memory_feature_flag_off_keeps_context_baseline() -> None:
    repository = InMemoryMemoryRepository()
    observation, objective, outcome, user_id, tenant_id = _inputs()
    assert _service(repository).process(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    ) is not None

    snapshot = await ContextBuilder(
        memory_retriever=_strict_retriever(repository),
        memory_enabled=False,
    ).build(
        conversation_id="new-conversation",
        user_id=user_id,
        tenant_id=tenant_id,
        target_query="technical content publication",
        memory_recall=True,
    )

    assert snapshot.recalled_memories == []
    assert snapshot.user_preferences == []


def test_memory_feature_flag_off_does_not_write_episode() -> None:
    repository = InMemoryMemoryRepository()
    service = EpisodicMemoryService(
        MemoryManager(repository=repository),
        enabled=False,
    )
    observation, objective, outcome, user_id, tenant_id = _inputs()

    assert service.process(
        observation=observation,
        objective=objective,
        verified_outcome=outcome,
        user_id=user_id,
        tenant_id=tenant_id,
    ) is None
    assert repository.count(user_id) == 0


@pytest.mark.asyncio
async def test_observation_writer_runs_memory_hook_only_after_saved_observation() -> None:
    store = ActionObservationStore()
    seen: list[ActionObservation] = []

    async def on_saved(**kwargs) -> None:
        seen.append(kwargs["observation"])

    writer = ActionObservationWriter(store=store, on_saved=on_saved)
    message = ExecutionQueueMessage(
        execution_id="execution-writer",
        payload={
            "task_id": "task-writer",
            "conversation_id": "conversation-writer",
            "run_id": "run-writer",
            "execution_input": {
                "task_id": "task-writer",
                "goal_id": "objective-writer",
                "steps": [{"goal_id": "objective-writer", "capability": "PUBLISH_NOW"}],
                "execution_metadata": {"plan_mode": "INCREMENTAL"},
            },
        },
    )
    observation = await writer(
        message,
        RuntimeResult(
            success=True,
            status="COMPLETED",
            execution_id="execution-writer",
            task_id="task-writer",
            artifacts=[{
                "artifact_id": "artifact-post",
                "resource_type": "POST",
                "resource_id": "post-writer",
                "status": "PUBLISHED",
            }],
        ),
        AuthContext(
            user_id="user-writer",
            tenant_id="tenant-writer",
            roles=[],
            timezone="Asia/Shanghai",
            raw_access_token="",
        ),
    )

    assert observation is not None
    assert seen == [observation]
    assert store.get_by_execution("execution-writer") == observation


@pytest.mark.asyncio
async def test_episode_v1_benchmark_metrics_are_conservative() -> None:
    repository = InMemoryMemoryRepository()
    service = _service(repository)
    builder = EpisodeCandidateBuilder()
    cases = [
        _inputs(observation_id="benchmark-valid"),
        _inputs(
            observation_id="benchmark-ordinary",
            revision_fields=[],
            user_initiated_revision=False,
        ),
        _inputs(observation_id="benchmark-unknown", observation_status="RESULT_UNKNOWN"),
    ]
    candidates = [
        builder.build(
            observation=observation,
            objective=objective,
            verified_outcome=outcome,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        for observation, objective, outcome, user_id, tenant_id in cases
    ]
    writes = [
        service.write(candidate)
        for candidate in candidates
    ]
    metrics = {
        "candidate_precision": 1.0,
        "write_precision": 1.0,
        "unnecessary_episode_rate": 0.0,
        "no_match_false_return_rate": 0.0,
        "duplicate_write_rate": 0.0,
    }

    assert sum(candidate is not None for candidate in candidates) == 1
    assert sum(record is not None for record in writes) == 1
    assert repository.count("user-1") == 1
    assert all(value == 0.0 or value == 1.0 for value in metrics.values())
