import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.creator.agents.schemas import (
    ContentAnalysisDocument,
    ContentOutlineDocument,
    CreatorProfileDocument,
    CritiqueDocument,
    CritiqueScores,
    CritiqueVerdict,
    DataAvailability,
    DraftDocument,
    EvidencePackDocument,
    OutlineSection,
    TopicOption,
    TopicOptionsDocument,
    TopicRecommendation,
)
from app.creator.domain.models import (
    CreatorGoal,
    CreatorTaskKind,
    RuntimeStartRequest,
)
from app.creator.runtime.artifacts import InMemoryCreatorArtifactStore
from app.creator.runtime.composition import build_creator_runtime
from app.creator.runtime.models import (
    BudgetLimits,
    BudgetUsage,
    CreatorGraphState,
    RuntimeControlStatus,
    RunIdentity,
)


class DeterministicModel:
    """Test-only model gateway returning schema-valid documents."""

    async def complete_structured(self, request: Any, output_type: type[Any]):
        if output_type is CreatorProfileDocument:
            document = CreatorProfileDocument.model_construct(
                creator_id="creator-r1",
                display_name="R1 probe",
                bio="",
                expertise_tags=(),
                style_traits=(),
                audience_hypotheses=(),
                preferred_formats=(),
                used_angles=(),
                data_availability=DataAvailability.NOT_CONNECTED,
                limitations=(),
            )
        elif output_type is ContentAnalysisDocument:
            document = ContentAnalysisDocument.model_construct(
                strengths=(),
                improvement_areas=(),
                reusable_patterns=(),
                data_availability=DataAvailability.NOT_CONNECTED,
                limitations=(),
            )
        elif output_type is EvidencePackDocument:
            document = EvidencePackDocument.model_construct(
                research_question="R1 probe",
                evidence=(),
                search_gaps=(),
                data_availability=DataAvailability.NOT_CONNECTED,
            )
        elif output_type is TopicOptionsDocument:
            options = tuple(
                TopicOption(
                    id=f"topic-{index}",
                    title=f"Probe topic {index}",
                    angle=f"Probe angle {index}",
                    audience_value="R1",
                    recommendation=(
                        TopicRecommendation.WRITE_NOW
                        if index == 1
                        else TopicRecommendation.WRITE_LATER
                    ),
                )
                for index in range(1, 4)
            )
            document = TopicOptionsDocument(
                options=options,
                recommended_option_id="topic-1",
                recommendation_reason="R1 probe",
            )
        elif output_type is ContentOutlineDocument:
            document = ContentOutlineDocument(
                title="R1 probe",
                thesis="A direct graph probe.",
                sections=tuple(
                    OutlineSection(
                        heading=f"Section {index}",
                        purpose="R1",
                        key_points=("probe",),
                    )
                    for index in range(1, 4)
                ),
                call_to_action="None",
            )
        elif output_type is DraftDocument:
            document = DraftDocument(
                title="R1 probe",
                body_markdown="# R1 probe\n\nDirect graph execution.",
            )
        elif output_type is CritiqueDocument:
            document = CritiqueDocument(
                reviewed_artifact_id="",
                verdict=CritiqueVerdict.ACCEPT,
                scores=CritiqueScores(
                    relevance=1.0,
                    structure=1.0,
                    evidence=1.0,
                    style=1.0,
                    overall=1.0,
                ),
                strengths=("Direct execution",),
                issues=(),
                revision_instructions=(),
            )
            reviewed_artifact_id = request.user_prompt
            # The production critic validates this reference. Its payload contains
            # the exact artifact ID, so extract only that scalar from the JSON.
            import json

            payload = json.loads(reviewed_artifact_id)
            document = document.model_copy(
                update={"reviewed_artifact_id": payload["reviewed_artifact_id"]}
            )
        else:
            raise AssertionError(f"Unexpected production output type: {output_type}")
        return document, 1, 1


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        creator_specialist_timeout_seconds=10.0,
        creator_max_supervisor_turns=24,
        creator_max_agent_dispatches=24,
        creator_max_model_calls=24,
        creator_max_output_tokens=40_000,
        creator_max_replans=4,
        creator_max_writer_revisions=2,
    )


@pytest.mark.anyio
async def test_creator_graph_direct_probe() -> None:
    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    loop_id = id(asyncio.get_running_loop())
    thread_id = threading.get_ident()
    request = RuntimeStartRequest(
        task_id="r1-task-new",
        run_id="r1-run-new",
        thread_id="r1-thread-new",
        tenant_id="r1-tenant",
        creator_id="creator-r1",
        kind=CreatorTaskKind.CREATE_CONTENT,
        goal=CreatorGoal(
            text="Create a direct LangGraph probe",
            constraints={"approval_mode": "AUTO"},
            source_scope={
                "include_community_posts": False,
                "include_creator_profile": False,
            },
        ),
        trace_id="r1-trace-new",
        execution_attempt=1,
    )
    identity = RunIdentity.from_request(request)
    initial_state: CreatorGraphState = {
        "identity": identity,
        "goal": request.goal,
        "limits": BudgetLimits(
            max_supervisor_turns=24,
            max_agent_dispatches=24,
            max_model_calls=24,
            max_output_tokens=40_000,
            max_replans=4,
            max_writer_revisions=2,
            specialist_timeout_seconds=10.0,
        ),
        "usage": BudgetUsage(),
        "plan": None,
        "plan_history": (),
        "executions": {},
        "artifacts": {},
        "facts": {},
        "progress": (),
        "errors": (),
        "decision": None,
        "control_status": RuntimeControlStatus.RUNNING,
        "final_artifact_id": None,
        "pending_decision_artifact_id": None,
        "applied_decision_id": None,
    }
    runtime = build_creator_runtime(
        settings=_settings(),
        ai_client=None,
        artifact_store=InMemoryCreatorArtifactStore(),
        checkpointer=InMemorySaver(),
        model_gateway=DeterministicModel(),
    )
    graph = runtime._graph.compiled
    print(f"GRAPH_BUILD_LOOP_ID {loop_id}", flush=True)
    print(f"GRAPH_BUILD_THREAD_ID {thread_id}", flush=True)
    print("GRAPH_MERMAID", flush=True)
    print(graph.get_graph().draw_mermaid(), flush=True)
    print(f"INTERRUPT_BEFORE {getattr(graph, 'interrupt_before', ())}", flush=True)
    print(f"INTERRUPT_AFTER {getattr(graph, 'interrupt_after', ())}", flush=True)
    print(f"INPUT_SCHEMA {getattr(graph, 'input_schema', None)}", flush=True)
    print(f"OUTPUT_SCHEMA {getattr(graph, 'output_schema', None)}", flush=True)
    print(f"INITIAL_STATE_KEYS {tuple(initial_state)}", flush=True)
    print("R1_GRAPH_INVOKE_STARTED", flush=True)
    result = await graph.ainvoke(
        initial_state,
        config={
            "configurable": {
                "thread_id": request.thread_id,
            },
            "recursion_limit": 64,
        },
    )
    print("R1_GRAPH_INVOKE_FINISHED", flush=True)
    print(f"R1_RESULT_KEYS {tuple(result)}", flush=True)
    print(
        f"R1_PLAN_HISTORY {[(plan.revision, [step.id for step in plan.steps]) for plan in result['plan_history']]}" ,
        flush=True,
    )
    print(f"R1_EXECUTION_KEYS {tuple(result['executions'])}", flush=True)
    print(
        f"R1_PROGRESS_TYPES {[entry.type for entry in result['progress']]}" ,
        flush=True,
    )
    print(f"R1_FINAL_STATUS {result['control_status']}", flush=True)
    print(f"R1_FINAL_ARTIFACT_ID {result['final_artifact_id']}", flush=True)
    assert result["control_status"] == RuntimeControlStatus.COMPLETED
    assert result["final_artifact_id"]
