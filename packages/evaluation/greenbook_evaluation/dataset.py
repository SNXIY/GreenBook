"""Golden multi-turn cases for the GreenBook Agent Runtime."""

from __future__ import annotations

from .models import EvalCase

GOLDEN_CASES: list[EvalCase] = [
    EvalCase(
        case_id="community-create-java",
        category="COMMAND_GOAL",
        conversation_turns=[{"role": "user", "content": "写一篇Java学习路线文章"}],
        user_message="写一篇Java学习路线文章",
        expected_command="CREATE",
        expected_goals=["create_article"],
        expected_task_state="COMPLETED",
        expected_tools=["content.create_draft"],
    ),
    EvalCase(
        case_id="community-research-create-schedule",
        category="MULTI_GOAL",
        conversation_turns=[{"role": "user", "content": "搜最近AI Agent热门内容，参考它们写文章，明天10点发布"}],
        user_message="搜最近AI Agent热门内容，参考它们写文章，明天10点发布",
        expected_command="CREATE",
        expected_goals=["research_topic", "generate_article", "schedule_publish"],
        expected_tools=["community.search_public_posts", "content.create_draft", "publication.schedule"],
        expected_task_state="COMPLETED",
        expected_side_effects=["publication.schedule"],
    ),
    EvalCase(
        case_id="community-revise-last",
        category="TARGET",
        conversation_turns=[
            {"role": "user", "content": "写一篇Java文章"},
            {"role": "user", "content": "把刚才那篇改得更短一点"},
        ],
        user_message="把刚才那篇改得更短一点",
        expected_command="MODIFY",
        expected_target={"reference": "刚才那篇", "kind": "DRAFT"},
        expected_tools=["content.create_draft"],
    ),
    EvalCase(
        case_id="community-target-java",
        category="TARGET",
        user_message="把Java那篇改一下",
        expected_command="MODIFY",
        expected_target={"topic": "Java", "kind": "DRAFT"},
        expected_tools=["content.create_draft"],
    ),
    EvalCase(
        case_id="community-target-ambiguous",
        category="TARGET",
        user_message="把那篇文章改一下",
        expected_command="MODIFY",
        expected_target={"ambiguous": True},
        expected_task_state="WAITING_HUMAN",
    ),
    EvalCase(
        case_id="community-preempt-task",
        category="TASK_LIFECYCLE",
        user_message="先帮我分析上一篇文章的数据",
        expected_command="CREATE",
        expected_task_state="RUNNING",
    ),
    EvalCase(
        case_id="community-search-replan",
        category="RECOVERY",
        user_message="搜索最近AI文章",
        expected_tools=["community.search_public_posts"],
        expected_task_state="COMPLETED",
    ),
    EvalCase(
        case_id="community-draft-recovery",
        category="RECOVERY",
        user_message="参考热门文章写一篇文章",
        expected_tools=["content.create_draft"],
        expected_task_state="COMPLETED",
    ),
    EvalCase(
        case_id="community-idempotent-publish",
        category="RECOVERY",
        user_message="继续发布任务",
        expected_tools=["publication.schedule"],
        expected_side_effects=["NO_DUPLICATE_PUBLICATION"],
    ),
    EvalCase(
        case_id="community-cancel-schedule",
        category="CONTROL",
        user_message="取消等待中的发布任务",
        expected_command="CANCEL",
        expected_tools=["publication.cancel_schedule"],
    ),
    EvalCase(
        case_id="community-preference-recall",
        category="MEMORY",
        conversation_turns=[
            {"role": "user", "content": "以后文章写简洁一点"},
            {"role": "user", "content": "还是按照我之前喜欢的简洁风格写"},
        ],
        user_message="还是按照我之前喜欢的简洁风格写",
        expected_command="CREATE",
        expected_goals=["generate_article"],
    ),
    EvalCase(
        case_id="community-long-context",
        category="CONTEXT",
        user_message="继续昨天那个任务",
        expected_command="MODIFY",
        expected_task_state="RUNNING",
    ),
]


def golden_cases() -> list[EvalCase]:
    return [case.model_copy(deep=True) for case in GOLDEN_CASES]


# Small, deterministic baseline used by both injected-runtime checks and the
# live E2E report.  The cases intentionally describe behavior, not a second
# execution platform.
BASELINE_CASES: list[EvalCase] = [
    EvalCase(case_id="baseline-single-step", category="SINGLE_STEP", user_message="search Java posts"),
    EvalCase(case_id="baseline-multi-step", category="MULTI_STEP", user_message="create and schedule a Java post"),
    EvalCase(case_id="baseline-multi-objective", category="MULTI_OBJECTIVE", user_message="create Java and Agent posts"),
    EvalCase(case_id="baseline-cross-turn", category="CROSS_TURN", user_message="change the Java schedule"),
    EvalCase(case_id="baseline-ambiguity", category="AMBIGUITY", user_message="change that post", expected_clarification=True),
    EvalCase(case_id="baseline-temporal", category="TEMPORAL", user_message="publish in five minutes"),
    EvalCase(case_id="baseline-idempotency", category="IDEMPOTENCY", user_message="retry the same publish request"),
    EvalCase(case_id="baseline-resume", category="RESUME", user_message="resume the interrupted run"),
]


def baseline_cases() -> list[EvalCase]:
    return [case.model_copy(deep=True) for case in BASELINE_CASES]


__all__ = ["BASELINE_CASES", "GOLDEN_CASES", "baseline_cases", "golden_cases"]
