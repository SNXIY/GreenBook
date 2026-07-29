from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx
from pydantic import SecretStr

from app.creator.domain.models import CreatorRunStatus, CreatorTaskStatus
from app.creator.evaluation.dataset import (
    load_evaluation_dataset,
    load_evaluation_observations,
)
from app.creator.evaluation.deterministic_judge import (
    DeterministicGenerationJudge,
)
from app.creator.evaluation.errors import (
    CreatorEvaluationConflictError,
    CreatorEvaluationDatasetError,
)
from app.creator.evaluation.exporters import (
    to_deepeval_test_case,
    to_langsmith_feedback,
    to_ragas_record,
)
from app.creator.evaluation.in_memory import InMemoryCreatorEvaluationStore
from app.creator.evaluation.judge import (
    OpenAICompatibleGenerationJudge,
    OpenAICompatibleJudgeConfig,
)
from app.creator.evaluation.models import (
    EvaluationMetricName,
    EvaluationMetricStatus,
    EvaluationOutcome,
    EvaluationSnapshotRequest,
    ObservedToolCall,
)
from app.creator.evaluation.service import CreatorEvaluationPipeline
from app.creator.infrastructure.database import CreatorDatabase
from app.creator.infrastructure.sqlalchemy import (
    CreatorArtifactRow,
    CreatorRunEventRow,
    CreatorRunRow,
    CreatorTaskRow,
)
from app.creator.runtime.models import ArtifactKind
from app.creator.tools.models import (
    CreatorToolCallStatus,
    CreatorToolRisk,
)
from app.creator.tools.sqlalchemy import CreatorToolCallRow


DATASET_PATH = "app/creator/evaluation/datasets/smoke-v1.json"
OBSERVATIONS_PATH = "app/creator/evaluation/datasets/smoke-observations-v1.json"


class CreatorEvaluationPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dataset = load_evaluation_dataset(DATASET_PATH)
        self.observations = load_evaluation_observations(OBSERVATIONS_PATH)

    async def test_smoke_pipeline_scores_and_replays_idempotently(self) -> None:
        store = InMemoryCreatorEvaluationStore()
        pipeline = CreatorEvaluationPipeline(
            store=store,
            judge=DeterministicGenerationJudge(),
        )
        first = await pipeline.evaluate(
            self.dataset,
            self.observations,
            tenant_id="tenant-eval",
            actor_id="test-suite",
            candidate_name="mindflow-creator",
            candidate_version="phase-8",
            evaluation_run_id="eval-smoke",
            persist=True,
        )
        replay = await pipeline.evaluate(
            self.dataset,
            self.observations,
            tenant_id="tenant-eval",
            actor_id="test-suite",
            candidate_name="mindflow-creator",
            candidate_version="phase-8",
            evaluation_run_id="eval-smoke",
            persist=True,
        )

        self.assertEqual(first.report.outcome, EvaluationOutcome.PASSED)
        self.assertTrue(first.report.passed)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.report.id, first.report.id)
        metrics = _case_metrics(first.report)
        self.assertEqual(
            metrics[EvaluationMetricName.RETRIEVAL_RECALL_AT_K].score,
            1.0,
        )
        self.assertEqual(metrics[EvaluationMetricName.RETRIEVAL_MRR].score, 1.0)
        self.assertEqual(
            metrics[EvaluationMetricName.RETRIEVAL_PRECISION_AT_K].score,
            0.5,
        )
        self.assertEqual(
            metrics[EvaluationMetricName.RETRIEVAL_NDCG_AT_K].score,
            1.0,
        )
        self.assertEqual(
            metrics[EvaluationMetricName.RETRIEVAL_ACL_SAFETY].score,
            1.0,
        )
        self.assertEqual(
            metrics[EvaluationMetricName.AGENT_TOOL_CALLING_ACCURACY].score,
            1.0,
        )
        self.assertEqual(
            metrics[EvaluationMetricName.GENERATION_FAITHFULNESS].score,
            1.0,
        )

        with self.assertRaises(CreatorEvaluationConflictError):
            await pipeline.evaluate(
                self.dataset,
                self.observations,
                tenant_id="tenant-eval",
                actor_id="test-suite",
                candidate_name="mindflow-creator",
                candidate_version="another-version",
                evaluation_run_id="eval-smoke",
                persist=True,
            )

    async def test_missing_generation_judge_marks_required_metrics_partial(
        self,
    ) -> None:
        result = await CreatorEvaluationPipeline().evaluate(
            self.dataset,
            self.observations,
            tenant_id="tenant-eval",
            actor_id="test-suite",
            candidate_name="mindflow-creator",
            candidate_version="no-judge",
        )

        self.assertEqual(result.report.outcome, EvaluationOutcome.PARTIAL)
        metrics = _case_metrics(result.report)
        self.assertEqual(
            metrics[EvaluationMetricName.GENERATION_FAITHFULNESS].status,
            EvaluationMetricStatus.SKIPPED,
        )
        self.assertFalse(result.report.passed)

    async def test_baseline_delta_detects_tool_call_regression(self) -> None:
        pipeline = CreatorEvaluationPipeline(judge=DeterministicGenerationJudge())
        baseline = await pipeline.evaluate(
            self.dataset,
            self.observations,
            tenant_id="tenant-eval",
            actor_id="test-suite",
            candidate_name="mindflow-creator",
            candidate_version="baseline",
        )
        original = self.observations.observations[0]
        degraded = original.model_copy(
            update={
                "tool_calls": (
                    *original.tool_calls,
                    ObservedToolCall(
                        call_id="call-unexpected-failure",
                        name="get_comments",
                        status=CreatorToolCallStatus.FAILED,
                        arguments_sha256="1" * 64,
                        error_code="UPSTREAM_TIMEOUT",
                    ),
                )
            }
        )
        candidate_observations = self.observations.model_copy(
            update={"observations": (degraded,)}
        )
        candidate = await pipeline.evaluate(
            self.dataset,
            candidate_observations,
            tenant_id="tenant-eval",
            actor_id="test-suite",
            candidate_name="mindflow-creator",
            candidate_version="candidate",
            baseline=baseline.report,
        )

        delta = candidate.report.metric_deltas[
            EvaluationMetricName.AGENT_TOOL_CALLING_ACCURACY.value
        ]
        self.assertLess(delta, 0.0)
        self.assertEqual(candidate.report.outcome, EvaluationOutcome.FAILED)

    async def test_unverified_retrieval_fails_acl_safety_gate(self) -> None:
        observation = self.observations.observations[0]
        leaked = observation.evidence[0].model_copy(
            update={"authority_verified": False}
        )
        degraded = observation.model_copy(
            update={"evidence": (leaked, *observation.evidence[1:])}
        )
        result = await CreatorEvaluationPipeline(
            judge=DeterministicGenerationJudge()
        ).evaluate(
            self.dataset,
            self.observations.model_copy(update={"observations": (degraded,)}),
            tenant_id="tenant-eval",
            actor_id="test-suite",
            candidate_name="mindflow-creator",
            candidate_version="acl-regression",
        )

        metrics = _case_metrics(result.report)
        acl_safety = metrics[EvaluationMetricName.RETRIEVAL_ACL_SAFETY]
        faithfulness = metrics[EvaluationMetricName.GENERATION_FAITHFULNESS]
        self.assertLess(acl_safety.score, 1.0)
        self.assertFalse(acl_safety.passed)
        self.assertEqual(faithfulness.score, 0.5)
        self.assertEqual(result.report.outcome, EvaluationOutcome.FAILED)

    async def test_dataset_loader_rejects_sensitive_fields(self) -> None:
        payload = json.loads(Path(DATASET_PATH).read_text(encoding="utf-8"))
        payload["metadata"]["api_key"] = "must-not-enter-eval-data"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe-dataset.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(CreatorEvaluationDatasetError):
                load_evaluation_dataset(path)

    async def test_provider_neutral_export_shapes(self) -> None:
        result = await CreatorEvaluationPipeline(
            judge=DeterministicGenerationJudge()
        ).evaluate(
            self.dataset,
            self.observations,
            tenant_id="tenant-eval",
            actor_id="test-suite",
            candidate_name="mindflow-creator",
            candidate_version="exports",
        )
        case = self.dataset.cases[0]
        observation = self.observations.observations[0]

        feedback = to_langsmith_feedback(result.report.cases[0])
        deep_eval = to_deepeval_test_case(case, observation)
        ragas = to_ragas_record(case, observation)

        self.assertTrue(
            any(
                item["key"] == EvaluationMetricName.AGENT_PLANNING_QUALITY.value
                for item in feedback
            )
        )
        self.assertEqual(deep_eval["input"], case.goal)
        self.assertEqual(
            deep_eval["tools_called"][0]["name"],
            "search_posts",
        )
        self.assertEqual(len(ragas["retrieved_contexts"]), 3)

    async def test_acl_safety_scores_without_relevance_labels(self) -> None:
        source_case = self.dataset.cases[0]
        criteria = source_case.criteria.model_copy(
            update={
                "relevant_document_ids": (),
                "required_metrics": (EvaluationMetricName.RETRIEVAL_ACL_SAFETY,),
            }
        )
        dataset = self.dataset.model_copy(
            update={"cases": (source_case.model_copy(update={"criteria": criteria}),)}
        )
        source_observation = self.observations.observations[0]
        evidence = (
            source_observation.evidence[0].model_copy(
                update={"authority_verified": False}
            ),
            *source_observation.evidence[1:],
        )
        observations = self.observations.model_copy(
            update={
                "observations": (
                    source_observation.model_copy(update={"evidence": evidence}),
                )
            }
        )

        result = await CreatorEvaluationPipeline().evaluate(
            dataset,
            observations,
            tenant_id="tenant-eval",
            actor_id="test-suite",
            candidate_name="mindflow-creator",
            candidate_version="acl-check",
        )
        metric = result.report.cases[0].metrics[0]

        self.assertEqual(metric.metric, EvaluationMetricName.RETRIEVAL_ACL_SAFETY)
        self.assertIsNotNone(metric.score)
        assert metric.score is not None
        self.assertAlmostEqual(metric.score, 2.0 / 3.0)
        self.assertEqual(result.report.outcome, EvaluationOutcome.FAILED)


class CreatorEvaluationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database = CreatorDatabase.from_url("sqlite+aiosqlite:///:memory:")
        await self.database.create_schema_for_development()

    async def asyncTearDown(self) -> None:
        await self.database.dispose()

    async def test_sql_store_persists_lists_and_replays_report(self) -> None:
        dataset = load_evaluation_dataset(DATASET_PATH)
        observations = load_evaluation_observations(OBSERVATIONS_PATH)
        pipeline = CreatorEvaluationPipeline(
            store=self.database.evaluation_store,
            judge=DeterministicGenerationJudge(),
        )
        first = await pipeline.evaluate(
            dataset,
            observations,
            tenant_id="tenant-eval",
            actor_id="operator-1",
            candidate_name="mindflow-creator",
            candidate_version="phase-8",
            evaluation_run_id="eval-sql",
            persist=True,
        )
        replay = await pipeline.evaluate(
            dataset,
            observations,
            tenant_id="tenant-eval",
            actor_id="operator-1",
            candidate_name="mindflow-creator",
            candidate_version="phase-8",
            evaluation_run_id="eval-sql",
            persist=True,
        )
        loaded = await self.database.evaluation_store.get("eval-sql")
        listed = await self.database.evaluation_store.list_for_task(
            tenant_id="tenant-eval",
            creator_id="creator-eval",
            task_id="task-eval-smoke",
        )

        self.assertTrue(first.report.passed)
        self.assertTrue(replay.replayed)
        self.assertIsNotNone(loaded)
        self.assertEqual([report.id for report in listed], ["eval-sql"])

    async def test_snapshot_reader_freezes_runtime_artifacts_events_and_tools(
        self,
    ) -> None:
        now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        async with self.database.sessions() as session:
            session.add(
                CreatorTaskRow(
                    id="task-snapshot",
                    tenant_id="tenant-a",
                    creator_id="creator-a",
                    session_id=None,
                    kind="CREATE_CONTENT",
                    goal_json={
                        "text": "Write about durable agent checkpoints",
                        "constraints": {},
                        "source_scope": {},
                    },
                    status=CreatorTaskStatus.COMPLETED.value,
                    version=3,
                    active_run_id="run-snapshot",
                    final_artifact_id="artifact-final",
                    pending_decision_id=None,
                    trace_id="trace-snapshot",
                    cancel_requested=False,
                    error_code=None,
                    error_message=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                CreatorRunRow(
                    id="run-snapshot",
                    task_id="task-snapshot",
                    thread_id="thread-snapshot",
                    attempt=1,
                    execution_attempts=1,
                    status=CreatorRunStatus.COMPLETED.value,
                    version=2,
                    lease_owner=None,
                    lease_expires_at=None,
                    checkpoint_id="checkpoint-snapshot",
                    pending_decision_id=None,
                    error_code=None,
                    error_message=None,
                    retryable=False,
                    started_at=now,
                    ended_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add_all(
                (
                    CreatorRunEventRow(
                        id="event-plan",
                        task_id="task-snapshot",
                        run_id="run-snapshot",
                        sequence=1,
                        type="supervisor.plan.created",
                        payload_json={
                            "revision": 1,
                            "reason": "Research first.",
                            "steps": [
                                {
                                    "step_id": "research",
                                    "capability": "RESEARCH_TOPIC",
                                    "dependencies": [],
                                }
                            ],
                        },
                        trace_id="trace-snapshot",
                        created_at=now,
                    ),
                    CreatorRunEventRow(
                        id="event-execution",
                        task_id="task-snapshot",
                        run_id="run-snapshot",
                        sequence=2,
                        type="agent.completed",
                        payload_json={
                            "execution_id": "execution-research",
                            "step_id": "research",
                            "agent": "ResearchAgent",
                            "capability": "RESEARCH_TOPIC",
                            "artifact_ids": ["artifact-evidence"],
                            "error_code": None,
                        },
                        trace_id="trace-snapshot",
                        created_at=now,
                    ),
                )
            )
            session.add_all(
                (
                    CreatorArtifactRow(
                        id="artifact-evidence",
                        tenant_id="tenant-a",
                        creator_id="creator-a",
                        task_id="task-snapshot",
                        run_id="run-snapshot",
                        step_id="research",
                        kind=ArtifactKind.EVIDENCE_PACK.value,
                        producer="ResearchAgent",
                        revision=1,
                        content_json={
                            "research_question": "durable checkpoints",
                            "evidence": [
                                {
                                    "id": "evidence-1",
                                    "document_id": "post-1",
                                    "summary": "Checkpoint state is durable.",
                                    "source": "community:post-1",
                                    "authority_verified": True,
                                }
                            ],
                            "search_gaps": [],
                            "data_availability": "AVAILABLE",
                        },
                        parent_ids_json=[],
                        metadata_json={},
                        confidence=0.9,
                        content_sha256="2" * 64,
                        created_at=now,
                    ),
                    CreatorArtifactRow(
                        id="artifact-final",
                        tenant_id="tenant-a",
                        creator_id="creator-a",
                        task_id="task-snapshot",
                        run_id="run-snapshot",
                        step_id="runtime:finalize",
                        kind=ArtifactKind.FINAL_CONTENT.value,
                        producer="CreatorSupervisorAgent",
                        revision=1,
                        content_json={
                            "source_artifact_id": "artifact-draft",
                            "source_kind": "DRAFT",
                            "document": {
                                "title": "Durable checkpoints",
                                "body_markdown": "A checkpoint preserves run state.",
                                "evidence_ids": ["evidence-1"],
                                "unsupported_claims": [],
                            },
                        },
                        parent_ids_json=["artifact-draft"],
                        metadata_json={"reviewed": True},
                        confidence=0.9,
                        content_sha256="3" * 64,
                        created_at=now,
                    ),
                )
            )
            session.add(
                CreatorToolCallRow(
                    call_id="call-snapshot",
                    trace_id="trace-snapshot",
                    task_id="task-snapshot",
                    run_id="run-snapshot",
                    tenant_id="tenant-a",
                    creator_id="creator-a",
                    actor_id="runtime",
                    caller="ResearchAgent",
                    tool_name="search_posts",
                    risk=CreatorToolRisk.READ.value,
                    arguments_sha256="4" * 64,
                    status=CreatorToolCallStatus.SUCCESS.value,
                    started_at=now,
                    finished_at=now,
                    latency_ms=12,
                    result_sha256="5" * 64,
                    result_size_bytes=128,
                    error_code=None,
                    reserved_json=None,
                )
            )
            await session.commit()

        observation = await self.database.evaluation_snapshot_reader.capture(
            EvaluationSnapshotRequest(
                case_id="snapshot-case",
                tenant_id="tenant-a",
                creator_id="creator-a",
                task_id="task-snapshot",
                run_id="run-snapshot",
            )
        )

        self.assertEqual(observation.task_status, CreatorTaskStatus.COMPLETED)
        self.assertEqual(observation.final_artifact_kind, ArtifactKind.FINAL_CONTENT)
        self.assertEqual(observation.evidence[0].document_id, "post-1")
        self.assertEqual(observation.plans[0].steps[0].step_id, "research")
        self.assertEqual(observation.tool_calls[0].name, "search_posts")
        self.assertIsNotNone(observation.generation)
        assert observation.generation is not None
        self.assertEqual(
            observation.generation.body_markdown,
            "A checkpoint preserves run state.",
        )


class CreatorEvaluationJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compatible_judge_requires_structured_json(self) -> None:
        dataset = load_evaluation_dataset(DATASET_PATH)
        observations = load_evaluation_observations(OBSERVATIONS_PATH)
        seen_request: httpx.Request | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_request
            seen_request = request
            content = {
                "faithfulness": {
                    "score": 1.0,
                    "reason": "All claims are supported.",
                },
                "relevance": {
                    "score": 0.9,
                    "reason": "The article addresses the goal.",
                },
                "style_consistency": {
                    "score": 0.8,
                    "reason": "The explicit style rubric is satisfied.",
                },
                "claims": [
                    {
                        "claim": "The harness owns checkpoint recovery.",
                        "verdict": "SUPPORTED",
                        "supporting_evidence_ids": ["evidence-agent-harness"],
                        "reason": "The evidence states this directly.",
                    },
                    {
                        "claim": "An unknown source supports automatic publication.",
                        "verdict": "SUPPORTED",
                        "supporting_evidence_ids": ["missing-evidence"],
                        "reason": "The judge attempted to cite an unknown source.",
                    },
                ],
                "limitations": [],
            }
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(content)}}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            judge = OpenAICompatibleGenerationJudge(
                OpenAICompatibleJudgeConfig(
                    base_url="https://judge.example/v1",
                    api_key=SecretStr("test-key"),
                    model="judge-model",
                    max_attempts=1,
                ),
                client=client,
            )
            assessment = await judge.assess(
                dataset.cases[0],
                observations.observations[0],
            )

        self.assertIsNotNone(seen_request)
        assert seen_request is not None
        self.assertEqual(seen_request.headers["x-trace-id"], "trace-eval-smoke")
        request_payload = json.loads(seen_request.content)
        self.assertEqual(request_payload["response_format"]["type"], "json_object")
        self.assertIsNotNone(assessment.faithfulness)
        assert assessment.faithfulness is not None
        self.assertEqual(assessment.faithfulness.score, 0.5)
        self.assertEqual(
            assessment.claims[0].supporting_evidence_ids,
            ("evidence-agent-harness",),
        )
        self.assertEqual(assessment.claims[1].verdict.value, "UNSUPPORTED")


def _case_metrics(report):
    return {metric.metric: metric for metric in report.cases[0].metrics}
