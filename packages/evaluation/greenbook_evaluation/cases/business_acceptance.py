"""Clear-language Business Acceptance cases for the GreenBook product.

This set is intentionally separate from ``semantic_baseline``.  The stress
set asks how robustly an LLM interprets varied language; this set asks whether
the product semantic boundary understands clear requests for the supported
community workflows.  The cases still exercise multi-objective, target, and
cross-turn context, but they do not use language tricks.
"""

from __future__ import annotations

from typing import Any

from ..models import EvalCase


def _expected(
    action_family: str,
    publication_mode: str,
    temporal_kind: str,
    temporal_resolved: bool,
    target_state: str,
    clarification_required: bool,
    objective_count: int,
    task_expectation: str = "READY",
) -> dict[str, Any]:
    return {
        "action_family": action_family,
        "publication_mode": publication_mode,
        "temporal_kind": temporal_kind,
        "temporal_resolved": temporal_resolved,
        "target_state": target_state,
        "clarification_required": clarification_required,
        "objective_count": objective_count,
        "task_expectation": task_expectation,
    }


def _resource_context(*records: dict[str, Any]) -> dict[str, Any]:
    """Build the same bounded typed target context used by production tests."""

    targets: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for record in records:
        title = str(record["title"])
        key = str(record["key"])
        task_id = f"business-task-{key}"
        objective_id = f"business-objective-{key}"
        resource_index: list[dict[str, Any]] = []
        targets.append({
            "kind": "TASK",
            "id": task_id,
            "resource_id": task_id,
            "task_id": task_id,
            "label": title,
            "status": "ACTIVE",
            "metadata": {"objective_id": objective_id},
        })
        for kind in record.get("resources", ()):
            resource_id = f"business-{key}-{str(kind).lower()}"
            status = {
                "DRAFT": "ACTIVE",
                "SCHEDULE": "SCHEDULED",
                "POST": "PUBLISHED",
            }.get(str(kind), "ACTIVE")
            metadata = {"objective_id": objective_id}
            if record.get("run_at") and kind == "SCHEDULE":
                metadata["run_at"] = record["run_at"]
            targets.append({
                "kind": kind,
                "id": resource_id,
                "resource_id": resource_id,
                "task_id": task_id,
                "label": title,
                "status": status,
                "created_at": "2026-08-20T09:00:00+08:00",
                "metadata": metadata,
            })
            resource_index.append({
                "resource_id": resource_id,
                "resource_kind": kind,
                "objective_id": objective_id,
            })
        tasks.append({
            "task_id": task_id,
            "goal": title,
            "objective_id": objective_id,
            "resource_index": resource_index,
        })
    return {"targets": targets, "active_tasks": tasks}


def _case(
    case_id: str,
    category: str,
    message: str,
    expected: dict[str, Any],
    *,
    setup_context: dict[str, Any] | None = None,
    conversation_turns: list[dict[str, str]] | None = None,
    initial_state: dict[str, Any] | None = None,
    **runtime_expectations: Any,
) -> EvalCase:
    return EvalCase(
        case_id=case_id,
        category=category,
        description="Clear-language Business Acceptance case",
        user_message=message,
        conversation_turns=list(conversation_turns or []),
        initial_state=dict(initial_state or {}),
        setup_context=dict(setup_context or {}),
        expected_semantic_state=expected,
        # The canonical semantic state is the single semantic oracle.  These
        # scalar fields remain unset to avoid a second expected contract.
        expected_objective_count=None,
        expected_temporal_resolution=None,
        expected_clarification=None,
        expected_task_state=None,
        **runtime_expectations,
    )


JAVA_DRAFT = {"key": "java", "title": "Java Backend Learning", "resources": ["DRAFT"]}
AGENT_DRAFT = {"key": "agent", "title": "Agent Development Learning", "resources": ["DRAFT"]}
REDIS_DRAFT = {"key": "redis", "title": "Redis Learning Route", "resources": ["DRAFT"]}
JAVA_SCHEDULE = {
    "key": "java-schedule",
    "title": "Java Backend Learning",
    "resources": ["DRAFT", "SCHEDULE"],
    "run_at": "2026-08-20T09:05:00+08:00",
}
AGENT_SCHEDULE = {
    "key": "agent-schedule",
    "title": "Agent Development Learning",
    "resources": ["DRAFT", "SCHEDULE"],
    "run_at": "2026-08-20T09:10:00+08:00",
}


def business_acceptance_cases() -> list[EvalCase]:
    """Return the independent 50-case product acceptance set."""

    cases = [
        # CREATE / DRAFT (1-4)
        _case(
            "business-create-draft-1", "CREATE_DRAFT",
            "Create a post titled 'Java Backend Learning' and save it as a draft.",
            _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1),
            expected_resource_types=["DRAFT"],
        ),
        _case(
            "business-create-draft-2", "CREATE_DRAFT",
            "Create a post titled 'Agent Development Learning' and save it as a draft.",
            _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1),
            expected_resource_types=["DRAFT"],
        ),
        _case(
            "business-create-draft-3", "CREATE_DRAFT",
            "Create a post titled 'Redis Learning Route' and save it as a draft.",
            _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1),
            expected_resource_types=["DRAFT"],
        ),
        _case(
            "business-create-draft-4", "CREATE_DRAFT",
            "Create a post titled 'Java Concurrency Learning Route'. The content must explain thread pools and locks. Save it as a draft.",
            _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1),
            expected_resource_types=["DRAFT"],
        ),
        # PUBLISH NOW (5-7)
        _case(
            "business-publish-now-1", "PUBLISH_NOW",
            "Create a post titled 'Java Backend Learning' and publish it immediately.",
            _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "NONE", False, 1),
            expected_resource_types=["POST"],
        ),
        _case(
            "business-publish-now-2", "PUBLISH_NOW",
            "Create a post titled 'Agent Development Learning' and publish it immediately.",
            _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "NONE", False, 1),
            expected_resource_types=["POST"],
        ),
        _case(
            "business-publish-now-3", "PUBLISH_NOW",
            "Publish the existing draft titled 'Java Backend Learning' immediately.",
            _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_DRAFT),
            expected_target={"resource_id": "business-java-draft"},
            expected_approval="NOT_REQUIRED",
        ),
        # SCHEDULE (8-12)
        _case(
            "business-schedule-1", "SCHEDULE",
            "Create a post titled 'Java Backend Learning' and publish it in five minutes.",
            _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NONE", False, 1),
            expected_resource_types=["DRAFT", "SCHEDULE"],
        ),
        _case(
            "business-schedule-2", "SCHEDULE",
            "Create a post titled 'Agent Development Learning' and publish it in ten minutes.",
            _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NONE", False, 1),
            expected_resource_types=["DRAFT", "SCHEDULE"],
        ),
        _case(
            "business-schedule-3", "SCHEDULE",
            "Create a post titled 'Redis Learning Route' and publish it tomorrow at 09:00.",
            _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NONE", False, 1),
            expected_resource_types=["DRAFT", "SCHEDULE"],
        ),
        _case(
            "business-schedule-4", "SCHEDULE",
            "Create a post titled 'Spring Boot Learning Route' and publish it tomorrow at 14:00.",
            _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NONE", False, 1),
            expected_resource_types=["DRAFT", "SCHEDULE"],
        ),
        _case(
            "business-schedule-5", "SCHEDULE",
            "Publish the existing draft titled 'Java Backend Learning' in five minutes.",
            _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_DRAFT),
            expected_target={"resource_id": "business-java-draft"},
        ),
        # MULTI-OBJECTIVE (13-17)
        _case(
            "business-multi-objective-1", "MULTI_OBJECTIVE",
            "Create two posts: 'Java Backend Learning' saved as a draft, and 'Agent Development Learning' saved as a draft.",
            _expected("MULTI_OBJECTIVE", "MIXED", "MIXED", False, "NONE", False, 2),
        ),
        _case(
            "business-multi-objective-2", "MULTI_OBJECTIVE",
            "Create two posts: 'Java Backend Learning' and publish it immediately; 'Agent Development Learning' and publish it in five minutes.",
            _expected("MULTI_OBJECTIVE", "MIXED", "MIXED", True, "NONE", False, 2),
        ),
        _case(
            "business-multi-objective-3", "MULTI_OBJECTIVE",
            "Create three posts: save 'Java Backend Learning' as a draft, publish 'Agent Development Learning' in five minutes, and publish 'Redis Learning Route' immediately.",
            _expected("MULTI_OBJECTIVE", "MIXED", "MIXED", True, "NONE", False, 3),
        ),
        _case(
            "business-multi-objective-4", "MULTI_OBJECTIVE",
            "Create two posts: publish 'Java Backend Learning' in ten minutes and publish 'Agent Development Learning' tomorrow at 14:00.",
            _expected("MULTI_OBJECTIVE", "SCHEDULED", "FUTURE", True, "NONE", False, 2),
        ),
        _case(
            "business-multi-objective-5", "MULTI_OBJECTIVE",
            "Create two posts: publish 'Java Backend Learning' immediately and save 'Agent Development Learning' as a draft.",
            _expected("MULTI_OBJECTIVE", "MIXED", "MIXED", True, "NONE", False, 2),
        ),
        # SEARCH (18-20)
        _case(
            "business-search-1", "SEARCH",
            "Search recent posts about Java backend learning.",
            _expected("SEARCH", "NONE", "NONE", False, "NONE", False, 1),
            expected_side_effects=[],
        ),
        _case(
            "business-search-2", "SEARCH",
            "Search recent posts about Agent development learning.",
            _expected("SEARCH", "NONE", "NONE", False, "NONE", False, 1),
            expected_side_effects=[],
        ),
        _case(
            "business-search-3", "SEARCH",
            "Search posts related to Java concurrency.",
            _expected("SEARCH", "NONE", "NONE", False, "NONE", False, 1),
            expected_side_effects=[],
        ),
        # SEARCH -> CREATE (21-23)
        _case(
            "business-search-create-1", "SEARCH_CREATE",
            "Search recent posts about Java backend learning, then use the results to write a post titled 'Java Backend Learning Route' and save it as a draft.",
            _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1),
            expected_resource_types=["DRAFT"],
        ),
        _case(
            "business-search-create-2", "SEARCH_CREATE",
            "Search recent posts about Agent development, then use the results to write a post titled 'Agent Development Learning Route' and save it as a draft.",
            _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1),
            expected_resource_types=["DRAFT"],
        ),
        _case(
            "business-search-create-3", "SEARCH_CREATE",
            "Search recent posts about Redis learning, then use the results to write 'Redis Learning Route' and publish it in five minutes.",
            _expected("CREATE", "SCHEDULED", "FUTURE", True, "NONE", False, 1),
            expected_resource_types=["DRAFT", "SCHEDULE"],
        ),
        # REVISE (24-26)
        _case(
            "business-revise-1", "REVISE",
            "Change the title of the existing post 'Java Backend Learning' to 'Java Backend Learning Route: From Beginner to Practice'.",
            _expected("REVISE", "NONE", "NONE", False, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_DRAFT),
            expected_target={"resource_id": "business-java-draft"},
        ),
        _case(
            "business-revise-2", "REVISE",
            "Shorten the body of the existing draft 'Agent Development Learning' while keeping the core learning route.",
            _expected("REVISE", "NONE", "NONE", False, "RESOLVED", False, 1),
            setup_context=_resource_context(AGENT_DRAFT),
            expected_target={"resource_id": "business-agent-draft"},
        ),
        _case(
            "business-revise-3", "REVISE",
            "Add a section about Spring Boot project practice to the body of the existing draft 'Java Backend Learning'.",
            _expected("REVISE", "NONE", "NONE", False, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_DRAFT),
            expected_target={"resource_id": "business-java-draft"},
        ),
        # CROSS-TURN (27-30)
        _case(
            "business-cross-turn-1", "CROSS_TURN",
            "Change the Java post's publication time to ten minutes from now.",
            _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_SCHEDULE),
            conversation_turns=[{"role": "user", "content": "Create Java Backend Learning and publish it in five minutes."}],
            expected_target={"resource_id": "business-java-schedule"},
        ),
        _case(
            "business-cross-turn-2", "CROSS_TURN",
            "Change the title of the Agent post to '2026 Agent Development Learning Route'.",
            _expected("REVISE", "NONE", "NONE", False, "RESOLVED", False, 1),
            setup_context=_resource_context(AGENT_DRAFT),
            conversation_turns=[{"role": "user", "content": "Create Agent Development Learning and save it as a draft."}],
            expected_target={"resource_id": "business-agent-draft"},
        ),
        _case(
            "business-cross-turn-3", "CROSS_TURN",
            "Publish the Java post in five minutes.",
            _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_DRAFT, AGENT_DRAFT),
            conversation_turns=[{"role": "user", "content": "Create Java Backend Learning and Agent Development Learning as two drafts."}],
            expected_target={"resource_id": "business-java-draft"},
        ),
        _case(
            "business-cross-turn-4", "CROSS_TURN",
            "Change the Agent post's publication time to fifteen minutes from now.",
            _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_SCHEDULE, AGENT_SCHEDULE),
            conversation_turns=[{"role": "user", "content": "Create Java Backend Learning for five minutes and Agent Development Learning for ten minutes."}],
            expected_target={"resource_id": "business-agent-schedule"},
        ),
        # CANCEL (31-33)
        _case(
            "business-cancel-1", "CANCEL",
            "Cancel the scheduled publication for the Java post, but keep the draft.",
            _expected("CANCEL", "NONE", "NONE", False, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_SCHEDULE),
            expected_target={"resource_id": "business-java-schedule"},
            expected_approval="NOT_REQUIRED",
        ),
        _case(
            "business-cancel-2", "CANCEL",
            "Cancel the scheduled publication for the Agent post.",
            _expected("CANCEL", "NONE", "NONE", False, "RESOLVED", False, 1),
            setup_context=_resource_context(AGENT_SCHEDULE),
            expected_target={"resource_id": "business-agent-schedule"},
        ),
        _case(
            "business-cancel-3", "CANCEL",
            "Cancel the scheduled publication for the Java post.",
            _expected("CANCEL", "NONE", "NONE", False, "AMBIGUOUS", True, 1, "CLARIFY"),
            setup_context=_resource_context(JAVA_SCHEDULE, {
                "key": "java-schedule-2",
                "title": "Java Concurrency Learning",
                "resources": ["SCHEDULE"],
            }),
            expected_target=None,
        ),
        # DELETE + HITL (34-36)
        _case(
            "business-delete-1", "DELETE_HITL",
            "Delete the published post titled 'Java Backend Learning'.",
            _expected("DELETE", "NONE", "NONE", False, "RESOLVED", False, 1),
            setup_context=_resource_context({
                "key": "java-post", "title": "Java Backend Learning", "resources": ["POST"],
            }),
            expected_target={"resource_id": "business-java-post-post"},
            expected_approval="PENDING",
        ),
        _case(
            "business-delete-2", "DELETE_HITL",
            "Delete the published post titled 'Java Backend Learning'.",
            _expected("DELETE", "NONE", "NONE", False, "RESOLVED", False, 1),
            setup_context=_resource_context({
                "key": "java-post-reject", "title": "Java Backend Learning", "resources": ["POST"],
            }),
            expected_target={"resource_id": "business-java-post-reject-post"},
            expected_approval="PENDING",
        ),
        _case(
            "business-delete-3", "DELETE_HITL",
            "Delete the post titled 'Java Backend Learning Route'.",
            _expected("DELETE", "NONE", "NONE", False, "RESOLVED", False, 1),
            setup_context=_resource_context(
                {"key": "java-route", "title": "Java Backend Learning Route", "resources": ["POST"]},
                {"key": "agent-route", "title": "Agent Development Learning Route", "resources": ["POST"]},
            ),
            expected_target={"resource_id": "business-java-route-post"},
            expected_approval="PENDING",
        ),
        # Necessary clarification (37-40)
        _case(
            "business-clarify-1", "CLARIFICATION",
            "Delete the Java post.",
            _expected("DELETE", "NONE", "NONE", False, "AMBIGUOUS", True, 1, "CLARIFY"),
            setup_context=_resource_context(
                {"key": "java-one", "title": "Java Backend Learning", "resources": ["POST"]},
                {"key": "java-two", "title": "Java Concurrency Learning", "resources": ["POST"]},
            ),
        ),
        _case(
            "business-clarify-2", "CLARIFICATION",
            "Cancel the scheduled publication for the Java post.",
            _expected("CANCEL", "NONE", "NONE", False, "AMBIGUOUS", True, 1, "CLARIFY"),
            setup_context=_resource_context(
                {"key": "java-schedule-one", "title": "Java Backend Learning", "resources": ["SCHEDULE"]},
                {"key": "java-schedule-two", "title": "Java Concurrency Learning", "resources": ["SCHEDULE"]},
            ),
        ),
        _case(
            "business-clarify-3", "CLARIFICATION",
            "Schedule the existing draft titled 'Java Backend Learning' for publication.",
            _expected("SCHEDULE", "UNRESOLVED", "UNRESOLVED", False, "RESOLVED", True, 1, "CLARIFY"),
            setup_context=_resource_context(JAVA_DRAFT),
        ),
        _case(
            "business-clarify-4", "CLARIFICATION",
            "Move the schedule for the Java draft to five minutes from now.",
            _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "NOT_FOUND", True, 1, "CLARIFY"),
        ),
        # Durable execution fixtures (41-45).  These keep clear semantic
        # requests; the fault/continuation behavior is exercised by Runtime.
        _case(
            "business-durable-1", "DURABLE",
            "Create a post titled 'Redis Learning Route' and publish it in five minutes.",
            _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NONE", False, 1),
            initial_state={"fault_injection": "TRANSIENT_CREATE_SCHEDULE"},
        ),
        _case(
            "business-durable-2", "DURABLE",
            "Move the schedule for the Java post to tomorrow at 09:00.",
            _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_SCHEDULE),
            initial_state={"fault_injection": "INVALID_UPDATE_SCHEDULE_ARGS"},
        ),
        _case(
            "business-durable-3", "DURABLE",
            "Create a post titled 'Redis Learning Route' and publish it in five minutes.",
            _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NONE", False, 1),
            initial_state={"fault_injection": "RESULT_UNKNOWN_CREATE_SCHEDULE"},
        ),
        _case(
            "business-durable-4", "DURABLE",
            "Create a post titled 'Agent Development Learning' and publish it immediately.",
            _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "NONE", False, 1),
            initial_state={"fault_injection": "RESULT_UNKNOWN_PUBLISH_NOW"},
        ),
        _case(
            "business-durable-5", "DURABLE",
            "Create a post titled 'Java Backend Learning' and save it as a draft.",
            _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1),
            initial_state={"fault_injection": "DUPLICATE_CONTINUATION"},
        ),
        # Resource ownership safety (46-50)
        _case(
            "business-ownership-1", "OWNERSHIP",
            "Publish the existing draft titled 'Agent Development Learning' in five minutes.",
            _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NOT_FOUND", True, 1, "CLARIFY"),
            setup_context=_resource_context(JAVA_DRAFT),
        ),
        _case(
            "business-ownership-2", "OWNERSHIP",
            "Move the schedule for the Agent post to tomorrow at 16:00.",
            _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "NOT_FOUND", True, 1, "CLARIFY"),
            setup_context=_resource_context(JAVA_SCHEDULE),
        ),
        _case(
            "business-ownership-3", "OWNERSHIP",
            "Create two posts: publish 'Java Backend Learning' in five minutes, and save 'Agent Development Learning' as a draft.",
            _expected("MULTI_OBJECTIVE", "MIXED", "MIXED", True, "NONE", False, 2),
        ),
        _case(
            "business-ownership-4", "OWNERSHIP",
            "Publish the draft titled 'Java Backend Learning' immediately.",
            _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "RESOLVED", False, 1),
            setup_context=_resource_context(JAVA_DRAFT, AGENT_DRAFT),
            expected_target={"resource_id": "business-java-draft"},
        ),
        _case(
            "business-ownership-5", "OWNERSHIP",
            "Create two posts: publish 'Java Backend Learning' in five minutes, and publish 'Agent Development Learning' in ten minutes.",
            _expected("MULTI_OBJECTIVE", "SCHEDULED", "FUTURE", True, "NONE", False, 2),
        ),
    ]
    if len(cases) != 50:
        raise AssertionError(f"Business Acceptance Set must contain 50 cases, got {len(cases)}")
    return cases


BUSINESS_ACCEPTANCE_CASES = business_acceptance_cases()

__all__ = ["BUSINESS_ACCEPTANCE_CASES", "business_acceptance_cases"]
