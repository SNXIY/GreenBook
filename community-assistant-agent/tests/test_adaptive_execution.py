from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain import AdaptiveExecutionDecision, AgentPlan, CommunityIntent
from app.execution import (
    normalize_execution_decision,
    render_creator_result,
    requires_verification,
    workload_lane,
)
from app.tools import tool_registry


def intent(*, domain: str = "general_qa") -> CommunityIntent:
    return CommunityIntent(
        domain=domain,
        goal="完成当前请求",
        confidence=0.98,
    )


def plan(tool: str) -> AgentPlan:
    return AgentPlan.model_validate(
        {
            "intent": "EXECUTE",
            "summary": "执行任务",
            "steps": [
                {
                    "task_id": "task-1",
                    "agent": "AutoRouter",
                    "tool": tool,
                    "label": "执行",
                }
            ],
        }
    )


def test_direct_path_requires_a_complete_response() -> None:
    with pytest.raises(ValidationError):
        AdaptiveExecutionDecision(
            execution_path="DIRECT",
            classification_summary="普通问答",
            intent=intent(),
        )


def test_direct_path_compiles_without_tools_or_verifier() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="DIRECT",
        classification_summary="普通问答",
        intent=intent(),
        direct_response="今天是 7 月 29 日。",
    )
    path, compiled = normalize_execution_decision(decision, tool_registry)
    assert path == "DIRECT"
    assert compiled.steps == []
    assert requires_verification(path) is False
    assert (
        workload_lane(
            path=path,
            plan=compiled,
            registry=tool_registry,
            persists_comment_reply=False,
        )
        == "READ"
    )


def test_model_cannot_downgrade_a_write_tool_to_tool_fast_path() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="TOOL",
        classification_summary="错误地声明为单工具查询",
        intent=intent(domain="content_publish"),
        plan=plan("publication.publish_now"),
    )
    path, compiled = normalize_execution_decision(decision, tool_registry)
    assert path == "ORCHESTRATED"
    assert requires_verification(path) is True
    assert (
        workload_lane(
            path=path,
            plan=compiled,
            registry=tool_registry,
            persists_comment_reply=False,
        )
        == "WRITE"
    )


def test_creator_fast_path_only_accepts_one_creator_step() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="CREATOR",
        classification_summary="仅创建一篇草稿",
        intent=intent(domain="content_publish"),
        plan=plan("creator.create_draft"),
    )
    path, compiled = normalize_execution_decision(decision, tool_registry)
    assert path == "CREATOR"
    assert requires_verification(path) is False
    assert (
        workload_lane(
            path=path,
            plan=compiled,
            registry=tool_registry,
            persists_comment_reply=False,
        )
        == "WRITE"
    )


def test_comment_surface_promotes_even_direct_answer_to_write_lane() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="DIRECT",
        classification_summary="普通问答",
        intent=intent(),
        direct_response="这是回答。",
    )
    path, compiled = normalize_execution_decision(decision, tool_registry)
    assert (
        workload_lane(
            path=path,
            plan=compiled,
            registry=tool_registry,
            persists_comment_reply=True,
        )
        == "WRITE"
    )


def test_creator_result_is_rendered_without_an_extra_model_call() -> None:
    response = render_creator_result(
        [
            {
                "tool": "creator.create_draft",
                "result": {
                    "draft_id": "123",
                    "title": "MySQL 学习路线",
                    "content_sha256": "a" * 64,
                },
            }
        ]
    )
    assert "MySQL 学习路线" in response
    assert "草稿号：123" in response
