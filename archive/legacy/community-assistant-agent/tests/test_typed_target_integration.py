from __future__ import annotations

from app.artifact_contracts import ArtifactBinder
from app.domain import TargetBinding, TargetContext
from app.tool_runtime import ToolAdapterRuntime, ToolRuntimeContext
from app.tools import tool_registry


def _context() -> TargetContext:
    return TargetContext(
        content_target=TargetBinding(
            target_type="DRAFT",
            role="CONTENT",
            target_id="draft-342580615352291328",
            content_sha256="a" * 64,
        ),
        schedule_target=TargetBinding(
            target_type="SCHEDULE",
            role="SCHEDULE",
            target_id="schedule-892ae452",
        ),
    )


def _runtime(context: TargetContext) -> ToolAdapterRuntime:
    return ToolAdapterRuntime(ArtifactBinder())


def _tool_context(context: TargetContext, *roles: str) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        prompt="test",
        context_post_id=None,
        context_comment_id=None,
        resolved_targets={
            role: getattr(context, f"{role.lower()}_target")
            for role in roles
        },
    )


def test_create_post_materializes_content_and_schedule_targets() -> None:
    context = _context()

    assert context.content_target is not None
    assert context.content_target.role == "CONTENT"
    assert context.schedule_target is not None
    assert context.schedule_target.role == "SCHEDULE"


def test_update_schedule_runtime_consumes_only_schedule_target() -> None:
    context = _context()
    definition = tool_registry.get("publication.update_schedule")

    arguments = _runtime(context).prepare_arguments(
        definition=definition,
        planner_arguments={"run_at": "2026-08-04T10:05:00+08:00"},
        artifacts=[],
        context=_tool_context(context, "SCHEDULE"),
    )

    assert arguments["action_id"] == "schedule-892ae452"


def test_append_content_runtime_consumes_only_content_target() -> None:
    context = _context()
    definition = tool_registry.get("creator.revise_draft")

    arguments = _runtime(context).prepare_arguments(
        definition=definition,
        planner_arguments={"instruction": "append experience"},
        artifacts=[
            {
                "task_id": "read-draft",
                "artifact_type": "CONTENT_DRAFT",
                "result": {
                    "draft_id": "draft-342580615352291328",
                    "content_sha256": "a" * 64,
                },
            }
        ],
        context=_tool_context(context, "CONTENT"),
        binding_sources={"references": ["read-draft"]},
    )

    assert arguments["draft_id"] == "draft-342580615352291328"
    assert arguments["expected_content_sha256"] == "a" * 64


def test_publish_runtime_receives_content_and_schedule_targets() -> None:
    context = _context()
    definition = tool_registry.get("publication.publish_now")
    runtime_context = _tool_context(context, "CONTENT", "SCHEDULE")

    assert set(runtime_context.resolved_targets or {}) == {"CONTENT", "SCHEDULE"}
    arguments = _runtime(context).prepare_arguments(
        definition=definition,
        planner_arguments={},
        artifacts=[],
        context=runtime_context,
    )

    assert arguments["draft_id"] == "draft-342580615352291328"
    assert arguments["expected_content_sha256"] == "a" * 64
