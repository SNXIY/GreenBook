from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.domain import (
    AdaptiveExecutionDecision,
    AdaptiveRoutingDecision,
    AgentPlan,
    CommunityIntent,
)
from app.execution import (
    deterministic_verification,
    is_explicit_single_draft_request,
    is_immediate_publish_follow_up,
    is_new_scheduled_post_request,
    parse_explicit_schedule_time,
    normalize_execution_decision,
    render_continuation_publish_result,
    render_creator_result,
    requires_verification,
    workload_lane,
)
from app.tools import tool_registry
from app.llm import DeepSeekClient


def intent(*, domain: str = "general_answer") -> CommunityIntent:
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


def test_direct_path_is_upgraded_when_intent_requires_community_actions() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="DIRECT",
        classification_summary="错误地声明为直接回答",
        direct_response="我会直接完成。",
        intent=CommunityIntent(
            domain="community_operation",
            goal="分析活跃用户并定时发布相关内容",
            required_capabilities=[
                "user_insight",
                "generation",
                "schedule_publish",
            ],
            confidence=0.9,
        ),
    )

    path, compiled = normalize_execution_decision(decision, tool_registry)

    assert path == "ORCHESTRATED"
    assert compiled.steps == []
    assert compiled.intent_detail == decision.intent


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


def test_lean_creator_route_is_compiled_into_a_typed_plan() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    route = AdaptiveRoutingDecision(
        execution_path="CREATOR",
        classification_summary="Create one draft",
        intent=CommunityIntent(
            domain="content_publish",
            goal="Create a post about learning MySQL",
            required_capabilities=["generation"],
            confidence=0.98,
        ),
    )

    decision = client._compile_adaptive_route(
        route,
        prompt="Create a post about learning MySQL",
    )

    assert decision.execution_path == "CREATOR"
    assert decision.plan is not None
    assert len(decision.plan.steps) == 1
    assert decision.plan.steps[0].tool == "creator.create_draft"
    assert decision.plan.steps[0].arguments == {
        "instruction": "Create a post about learning MySQL",
        "references": [],
    }


def test_adaptive_route_canonicalizes_model_capability_synonyms() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    route = AdaptiveRoutingDecision(
        execution_path="ORCHESTRATED",
        classification_summary="Create and schedule a Redis draft",
        intent=CommunityIntent(
            domain="content_publish",
            goal="Create and schedule a Redis draft",
            required_capabilities=["generation", "scheduling"],
            confidence=0.98,
        ),
    )

    decision = client._compile_adaptive_route(route, prompt="create and schedule")

    assert decision.intent.required_capabilities == [
        "generation",
        "schedule_publish",
    ]


def test_parse_chinese_afternoon_time_for_schedule_mutation() -> None:
    parsed = parse_explicit_schedule_time(
        "发布时间五分钟之后改成下午两点半发布",
        client_timezone="Asia/Shanghai",
        now=datetime.fromisoformat("2026-08-03T08:00:00+08:00"),
    )

    assert parsed == "2026-08-03T14:30:00+08:00"


def test_parse_real_utf8_relative_schedule_mutation() -> None:
    parsed = parse_explicit_schedule_time(
        "\u53d1\u5e03\u65f6\u95f4\u4fee\u6539\u4e00\u4e0b\uff0c\u4fee\u6539\u6210\u4e94\u5206\u949f\u4e4b\u540e",
        client_timezone="Asia/Shanghai",
        now=datetime.fromisoformat("2026-08-03T18:00:00+08:00"),
    )

    assert parsed == "2026-08-03T18:05:00+08:00"


def test_schedule_update_uses_target_context_not_older_workspace_schedule() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    decision = client.deterministic_execution(
        prompt="\u53d1\u5e03\u65f6\u95f4\u4fee\u6539\u4e00\u4e0b\uff0c\u4fee\u6539\u6210\u4e94\u5206\u949f\u4e4b\u540e",
        client_timezone="Asia/Shanghai",
        conversation_workspace={
            "target_context": {
                "schedule_target": {
                    "target_type": "SCHEDULE",
                    "target_id": "current-action",
                    "schedule_id": "current-action",
                }
            },
            "entities": [
                {
                    "ref": "schedule:old-action",
                    "kind": "SCHEDULE",
                    "entity_id": "old-action",
                    "status": "SCHEDULED",
                    "actionable": True,
                },
                {
                    "ref": "schedule:current-action",
                    "kind": "SCHEDULE",
                    "entity_id": "current-action",
                    "status": "SCHEDULED",
                    "actionable": True,
                },
            ],
        },
    )

    # Schedule mutations now flow through the normal IntentDelta pipeline
    # (TurnIntentParser \u2192 TargetResolver \u2192 IntentDeltaPlanCompiler) instead
    # of deterministic_execution. The target_context already carries the
    # correct schedule_target.
    assert decision is None


def test_existing_schedule_change_uses_deterministic_update_plan() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    decision = client.deterministic_execution(
        prompt="发布时间五分钟之后改成下午两点半发布",
        client_timezone="Asia/Shanghai",
        conversation_workspace={
            "entities": [
                {
                    "ref": "schedule:action-1",
                    "kind": "SCHEDULE",
                    "entity_id": "action-1",
                    "status": "SCHEDULED",
                    "actionable": True,
                }
            ]
        },
    )

    # Schedule mutations now flow through the normal IntentDelta pipeline.
    # deterministic_execution only guards terminal schedules, not active ones.
    assert decision is None


def test_new_scheduled_post_does_not_inherit_old_schedule_target() -> None:
    prompt = "\u660e\u5929\u4e0a\u5348\u516b\u70b9\u53d1\u5e03\u4e00\u7bc7\u5173\u4e8e\u5982\u4f55\u5b66\u597d Kafka \u7684\u5e16\u5b50"
    assert is_new_scheduled_post_request(prompt) is True
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    decision = client.deterministic_execution(
        prompt=prompt,
        client_timezone="Asia/Shanghai",
        conversation_workspace={
            "entities": [
                {
                    "ref": "schedule:old-action",
                    "kind": "SCHEDULE",
                    "entity_id": "old-action",
                    "status": "SCHEDULED",
                    "actionable": True,
                }
            ]
        },
    )
    assert decision is not None
    assert decision.turn_relation == "NEW_GOAL"
    assert decision.referenced_entities == []
    assert decision.intent.required_capabilities == ["generation", "schedule_publish"]


def test_schedule_change_scopes_to_active_goal_with_old_schedules_present() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    decision = client.deterministic_execution(
        prompt="发布时间五分钟之后改成下午两点半发布",
        client_timezone="Asia/Shanghai",
        conversation_workspace={
            "active_goal_ref": "goal:run-current",
            "entities": [
                {
                    "ref": "schedule:old-action",
                    "kind": "SCHEDULE",
                    "entity_id": "old-action",
                    "source_run_id": "run-old",
                    "status": "SCHEDULED",
                    "actionable": True,
                },
                {
                    "ref": "schedule:current-action",
                    "kind": "SCHEDULE",
                    "entity_id": "current-action",
                    "source_run_id": "run-current",
                    "status": "SCHEDULED",
                    "actionable": True,
                },
            ],
        },
    )

    # Schedule mutations now flow through the normal IntentDelta pipeline.
    # deterministic_execution only guards terminal schedules, not active ones.
    assert decision is None


def test_completed_schedule_change_returns_explanation_instead_of_retry_plan() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    decision = client.deterministic_execution(
        prompt="发布时间五分钟之后改成下午两点半发布",
        client_timezone="Asia/Shanghai",
        conversation_workspace={
            "active_goal_ref": "goal:run-current",
            "entities": [
                {
                    "ref": "schedule:completed-action",
                    "kind": "SCHEDULE",
                    "entity_id": "completed-action",
                    "source_run_id": "run-current",
                    "status": "COMPLETED",
                    "actionable": False,
                }
            ],
        },
    )

    assert decision is not None
    assert decision.execution_path == "DIRECT"
    assert decision.intent.domain == "general_answer"
    assert "不能再修改" in str(decision.direct_response)


def test_mutating_route_defers_target_choice_to_operation_aware_resolver() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    route = AdaptiveRoutingDecision(
        execution_path="ORCHESTRATED",
        classification_summary="Publish the referenced draft",
        intent=CommunityIntent(
            domain="content_publish",
            goal="Publish it",
            required_capabilities=["publishing"],
            confidence=0.95,
        ),
        turn_relation="CONTINUE",
        referenced_entities=["draft:draft-java"],
    )
    workspace = {
        "entities": [
            {
                "ref": "draft:draft-java",
                "kind": "DRAFT",
                "entity_id": "draft-java",
                "label": "Java 学习路线",
                "actionable": True,
            },
            {
                "ref": "draft:draft-mysql",
                "kind": "DRAFT",
                "entity_id": "draft-mysql",
                "label": "MySQL 学习路线",
                "actionable": True,
            },
        ]
    }

    decision = client._compile_adaptive_route(
        route,
        prompt="发布它",
        conversation_workspace=workspace,
    )

    assert decision.execution_path == "ORCHESTRATED"
    assert decision.referenced_entities == []
    assert decision.direct_response is None


def test_numbered_clarification_reply_selects_the_requested_entity() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    route = AdaptiveRoutingDecision(
        execution_path="ORCHESTRATED",
        classification_summary="Revise the selected draft",
        intent=CommunityIntent(
            domain="content_edit",
            goal="Add code to the selected post",
            required_capabilities=["rewrite_content"],
            confidence=0.95,
        ),
        turn_relation="MODIFY",
        referenced_entities=[],
    )
    workspace = {
        "entities": [
            {
                "ref": "draft:draft-hot100",
                "kind": "DRAFT",
                "entity_id": "draft-hot100",
                "label": "力扣 Hot100 二叉树的层序遍历",
                "actionable": True,
            },
            {
                "ref": "draft:draft-redis",
                "kind": "DRAFT",
                "entity_id": "draft-redis",
                "label": "Redis 高并发学习路线",
                "actionable": True,
            },
        ]
    }

    decision = client._compile_adaptive_route(
        route,
        prompt="1. 力扣 Hot100 二叉树的层序遍历：从 BFS 到代码实现",
        conversation_workspace=workspace,
    )

    assert decision.execution_path == "ORCHESTRATED"
    assert decision.referenced_entities == ["draft:draft-hot100"]


def test_post_title_selection_also_carries_its_active_schedule() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    route = AdaptiveRoutingDecision(
        execution_path="ORCHESTRATED",
        classification_summary="Revise the selected draft",
        intent=CommunityIntent(
            domain="content_edit",
            goal="Add code to the selected post",
            required_capabilities=["rewrite_content"],
            confidence=0.95,
        ),
        turn_relation="MODIFY",
        referenced_entities=[],
    )
    workspace = {
        "entities": [
            {
                "ref": "draft:draft-island",
                "kind": "DRAFT",
                "entity_id": "draft-island",
                "label": "力扣 200 岛屿数量",
                "related_refs": ["schedule:action-island"],
                "status": "SCHEDULED",
                "actionable": True,
            },
            {
                "ref": "schedule:action-island",
                "kind": "SCHEDULE",
                "entity_id": "action-island",
                "status": "SCHEDULED",
                "actionable": True,
            },
        ]
    }

    decision = client._compile_adaptive_route(
        route,
        prompt="《力扣 200 岛屿数量：DFS 与 BFS 双解法详解》这个",
        conversation_workspace=workspace,
    )

    assert decision.referenced_entities == [
        "draft:draft-island",
        "schedule:action-island",
    ]


def test_complete_title_beats_shared_topic_word_between_drafts() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    route = AdaptiveRoutingDecision(
        execution_path="ORCHESTRATED",
        classification_summary="Revise the selected draft",
        intent=CommunityIntent(
            domain="content_edit",
            goal="Add code",
            required_capabilities=["rewrite_content"],
            confidence=0.95,
        ),
        turn_relation="MODIFY",
        referenced_entities=[],
    )
    workspace = {
        "entities": [
            {
                "ref": "draft:draft-cache-trinity",
                "kind": "DRAFT",
                "entity_id": "draft-cache-trinity",
                "label": "Redis 缓存三剑客：穿透、击穿、雪崩的应对之道",
                "status": "SCHEDULED",
                "actionable": True,
            },
            {
                "ref": "draft:draft-redis-concurrency",
                "kind": "DRAFT",
                "entity_id": "draft-redis-concurrency",
                "label": "Redis 高并发学习路线",
                "status": "SCHEDULED",
                "actionable": True,
            },
        ]
    }

    decision = client._compile_adaptive_route(
        route,
        prompt="给《Redis 缓存三剑客：穿透、击穿、雪崩的应对之道》增加代码",
        conversation_workspace=workspace,
    )

    assert decision.referenced_entities == ["draft:draft-cache-trinity"]


def test_explicit_entity_name_overrides_model_guess_for_mutation() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    route = AdaptiveRoutingDecision(
        execution_path="ORCHESTRATED",
        classification_summary="Publish the named draft",
        intent=CommunityIntent(
            domain="content_publish",
            goal="Publish the MySQL draft",
            required_capabilities=["publishing"],
            confidence=0.95,
        ),
        turn_relation="CONTINUE",
        referenced_entities=["draft:draft-java"],
    )
    workspace = {
        "entities": [
            {
                "ref": "draft:draft-java",
                "kind": "DRAFT",
                "entity_id": "draft-java",
                "label": "Java 学习路线",
                "actionable": True,
            },
            {
                "ref": "draft:draft-mysql",
                "kind": "DRAFT",
                "entity_id": "draft-mysql",
                "label": "MySQL 学习路线",
                "actionable": True,
            },
        ]
    }

    decision = client._compile_adaptive_route(
        route,
        prompt="发布 MySQL 那篇",
        conversation_workspace=workspace,
    )

    assert decision.execution_path == "ORCHESTRATED"
    assert decision.referenced_entities == ["draft:draft-mysql"]


@pytest.mark.parametrize(
    "prompt",
    ["发布吧", "立即发布", "把刚才生成的帖子发布吧", "发布这篇草稿吧"],
)
def test_immediate_publish_follow_up_grammar(prompt: str) -> None:
    assert is_immediate_publish_follow_up(prompt) is True


@pytest.mark.parametrize(
    "prompt",
    ["五分钟后发布", "明天发布吧", "把这些全部发布", "发布哪一篇？"],
)
def test_ambiguous_or_scheduled_publish_is_not_a_deterministic_follow_up(
    prompt: str,
) -> None:
    assert is_immediate_publish_follow_up(prompt) is False


def test_previous_turn_draft_compiles_to_revalidate_then_publish() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry

    decision = client.deterministic_execution(
        prompt="发布吧",
        continuation_draft={
            "draft_id": "342506609282519040",
            "title": "MySQL 学习路线",
            "is_immediate": True,
        },
    )

    # Continuation draft publish now flows through the normal IntentDelta
    # pipeline (TurnIntentParser → PUBLISH_NOW → IntentDeltaPlanCompiler).
    assert decision is None


def test_publish_follow_up_without_trusted_draft_stays_under_llm_control() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry

    assert (
        client.deterministic_execution(
            prompt="发布吧",
            continuation_draft=None,
        )
        is None
    )


def test_fast_publish_does_not_use_active_goal_to_break_focused_draft_tie() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    workspace = {
        "active_goal_ref": "goal:run-java",
        "focus_refs": ["draft:draft-java", "draft:draft-mysql"],
        "entities": [
            {
                "ref": "draft:draft-java",
                "kind": "DRAFT",
                "entity_id": "draft-java",
                "label": "Java 学习路线",
                "status": "READY",
                "source_run_id": "run-java",
                "actionable": True,
            },
            {
                "ref": "draft:draft-mysql",
                "kind": "DRAFT",
                "entity_id": "draft-mysql",
                "label": "MySQL 学习路线",
                "status": "READY",
                "source_run_id": "run-mysql",
                "actionable": True,
            },
        ],
    }

    assert (
        client.deterministic_execution(
            prompt="发布它",
            conversation_workspace=workspace,
        )
        is None
    )


def test_continuation_publish_result_is_rendered_without_a_model_call() -> None:
    response = render_continuation_publish_result(
        [
            {
                "tool": "community.get_own_draft",
                "result": {"title": "MySQL 学习路线"},
            },
            {
                "tool": "publication.publish_now",
                "result": {"post_id": "342506609282519040"},
            },
        ]
    )

    assert response == "《MySQL 学习路线》已发布成功（帖子号：342506609282519040）。"


def test_lean_route_schema_does_not_require_nested_agent_plan() -> None:
    schema = AdaptiveRoutingDecision.model_json_schema()

    assert "plan" not in schema["properties"]
    assert {"execution_path", "classification_summary", "intent"}.issubset(
        schema["required"]
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "帮我创作一篇如何学习 MySQL 的帖子",
        "请帮我写一篇 Java 学习路线文章",
        "写一篇关于 Agent Harness 的帖子",
    ],
)
def test_explicit_single_draft_command_uses_high_confidence_fast_route(
    prompt: str,
) -> None:
    assert is_explicit_single_draft_request(prompt) is True


@pytest.mark.asyncio
async def test_explicit_creator_command_does_not_spend_a_router_model_call() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry

    async def unexpected_model_call(*args, **kwargs):
        del args, kwargs
        raise AssertionError("explicit creator command should not call router model")

    client._structured_chat = unexpected_model_call
    decision = await client.decide_execution(
        prompt="帮我创作一篇如何学习 MySQL 的帖子",
        context_post_id=None,
        context_comment_id=None,
        client_timezone="Asia/Shanghai",
        history=[],
        memories=[],
        recalled_memories=[],
    )

    assert decision.execution_path == "CREATOR"
    assert decision.plan is not None
    assert decision.plan.steps[0].tool == "creator.create_draft"


@pytest.mark.parametrize(
    "prompt",
    [
        "如何写一篇 MySQL 学习帖子？",
        "帮我创作一篇 MySQL 帖子，然后立即发布",
        "先搜索社区帖子，再参考它们创作一篇文章",
        "分析热门主题后写一篇帖子",
    ],
)
def test_ambiguous_or_multistep_creation_stays_under_llm_planning(
    prompt: str,
) -> None:
    assert is_explicit_single_draft_request(prompt) is False


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


def test_completed_write_workflow_uses_deterministic_verification() -> None:
    compiled = AgentPlan.model_validate(
        {
            "intent": "CREATE_AND_SCHEDULE",
            "summary": "Create and schedule",
            "steps": [
                {
                    "task_id": "create",
                    "primary_capability": "generation",
                    "tool": "creator.create_draft",
                    "label": "Create draft",
                    "expected_artifact_type": "content_draft",
                },
                {
                    "task_id": "schedule",
                    "primary_capability": "schedule_publish",
                    "tool": "publication.schedule",
                    "label": "Schedule draft",
                    "expected_artifact_type": "schedule_receipt",
                    "depends_on": ["create"],
                },
            ],
        }
    )
    outputs = [
        {
            "task_id": "create",
            "tool": "creator.create_draft",
            "artifact_type": "CONTENT_DRAFT",
            "result": {"draft_id": "1", "content_sha256": "a" * 64},
        },
        {
            "task_id": "schedule",
            "tool": "publication.schedule",
            "artifact_type": "SCHEDULE_RECEIPT",
            "result": {"action_id": "2", "status": "SCHEDULED"},
        },
    ]

    verification = deterministic_verification(
        plan=compiled,
        outputs=outputs,
        registry=tool_registry,
    )

    assert verification is not None
    assert verification.decision == "COMPLETE"


def test_deterministic_verification_falls_back_for_read_or_incomplete_work() -> None:
    read_plan = plan("community.search_posts")
    write_plan = plan("publication.publish_now")

    assert deterministic_verification(
        plan=read_plan,
        outputs=[],
        registry=tool_registry,
    ) is None
    assert deterministic_verification(
        plan=write_plan,
        outputs=[],
        registry=tool_registry,
    ) is None


def test_deterministic_verification_rejects_empty_upstream_evidence() -> None:
    compiled = AgentPlan.model_validate(
        {
            "intent": "SEARCH_AND_CREATE",
            "summary": "Search and create",
            "steps": [
                {
                    "task_id": "search",
                    "tool": "community.search_posts",
                    "label": "Search",
                    "expected_artifact_type": "post_search_results",
                },
                {
                    "task_id": "create",
                    "tool": "creator.create_draft",
                    "label": "Create",
                    "expected_artifact_type": "content_draft",
                    "depends_on": ["search"],
                },
            ],
        }
    )
    outputs = [
        {
            "task_id": "search",
            "artifact_type": "POST_SEARCH_RESULTS",
            "result": {"results": []},
        },
        {
            "task_id": "create",
            "artifact_type": "CONTENT_DRAFT",
            "result": {"draft_id": "1", "content_sha256": "a" * 64},
        },
    ]

    assert deterministic_verification(
        plan=compiled,
        outputs=outputs,
        registry=tool_registry,
    ) is None
