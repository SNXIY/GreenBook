"""Phase 4.4.1 deterministic reliability contracts."""

from __future__ import annotations

from greenbook_agent_core.agent.recovery import AgentRecoveryService
from greenbook_agent_core.execution.failure_decision import (
    FailureCategory,
    FailureClassifier,
)
from greenbook_agent_core.execution.models import StepExecution, StepStatus
from greenbook_agent_core.execution.runtime_agent_service import _business_resource_facts
from greenbook_agent_core.planning.contracts import PlanningDecisionType
from greenbook_agent_core.planning.dynamic import DynamicPlanner
from greenbook_contracts.external_agent_failure import ExternalAgentFailure, RecoveryAction


def test_field_too_long_is_permanent_input() -> None:
    assert FailureClassifier.category_for_code("FIELD_TOO_LONG") == FailureCategory.INVALID_ARGUMENT
    assert FailureClassifier.category_for_code("INTERNAL_ERROR") == FailureCategory.SERVER_FAILURE


def test_dynamic_planner_does_not_retry_same_permanent_input() -> None:
    decision = DynamicPlanner._evidence_fallback(
        {
            "observations": [
                {
                    "failure_kind": "FIELD_TOO_LONG",
                    "last_result": {
                        "ok": False,
                        "code": "FIELD_TOO_LONG",
                        "tool_name": "content.create_draft",
                        "request_sent": True,
                    },
                }
            ],
            "tool_metadata": [
                {
                    "name": "content.create_draft",
                    "side_effect": {"has_side_effect": True, "idempotent": True},
                }
            ],
        }
    )
    assert decision.decision == PlanningDecisionType.ASK_HUMAN
    assert decision.decision != PlanningDecisionType.RETRY_WITH_NEW_ARGS


def test_unknown_side_effect_is_not_retryable_without_reconciliation() -> None:
    failure = ExternalAgentFailure(
        error_code="RESULT_UNKNOWN",
        dependency="java",
        retryable=False,
        user_visible_message="unknown",
        recovery_action=RecoveryAction.RECONCILE,
        request_sent=None,
        side_effect_state="UNKNOWN",
    )
    classification = FailureClassifier().classify(failure)
    assert classification.category == FailureCategory.SIDE_EFFECT_UNKNOWN
    assert classification.requires_reconciliation is True
    assert classification.retryable is False


def test_checkpoint_resource_fact_survives_artifact_projection_failure() -> None:
    execution = type(
        "Execution",
        (),
        {
            "steps": [
                StepExecution(
                    step_id="create-1",
                    status=StepStatus.COMPLETED,
                    checkpoint_data={
                        "completed_tool_result": {"data": {"draft_id": "draft-42"}}
                    },
                )
            ]
        },
    )()
    facts = _business_resource_facts(execution)
    assert facts["DRAFT"] == {"resource_id": "draft-42", "step_id": "create-1"}


def test_resume_projection_keeps_checkpoint_draft_without_artifact_handle() -> None:
    execution = type(
        "Execution",
        (),
        {
            "execution_id": "e-1",
            "status": "COMPLETED",
            "steps": [
                {
                    "step_id": "create-1",
                    "status": "COMPLETED",
                    "output_artifact": None,
                    "checkpoint_data": {
                        "completed_tool_result": {"data": {"draft_id": "draft-42"}}
                    },
                }
            ],
        },
    )()
    context = AgentRecoveryService().build_resume_context(execution=execution)
    assert any(item.get("resource_id") == "draft-42" for item in context.artifacts)
