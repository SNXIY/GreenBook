"""TaskUnderstanding 2.0 evaluation dataset.

5 categories: SIMPLE, MODIFY, COMPOSITE, CONDITIONAL, HITL.
Each case defines expected operation_mode + operations.
"""

from __future__ import annotations

from .models import EvalCase

# ═══════════════════════════════════════════════════════════════════
# 1. SIMPLE — single-goal, single-step
# ═══════════════════════════════════════════════════════════════════

SIMPLE_CASES: list[EvalCase] = [
    EvalCase(
        case_id="tu-simple-01", category="INTENT",
        description="写Java文章",
        user_message="帮我写一篇Java文章",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "CREATE_CONTENT",
            "relation": "NEW_TASK",
        },
    ),
    EvalCase(
        case_id="tu-simple-02", category="INTENT",
        description="创建Python教程",
        user_message="创建一个Python教程",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "CREATE_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-simple-03", category="INTENT",
        description="明天发布Spring文章",
        user_message="明天发布一篇Spring文章",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "CREATE_CONTENT",
            "relation": "NEW_TASK",
        },
    ),
    EvalCase(
        case_id="tu-simple-04", category="INTENT",
        description="搜索社区Java帖子",
        user_message="搜索社区Java帖子",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "ANALYZE_COMMUNITY",
        },
    ),
    EvalCase(
        case_id="tu-simple-05", category="INTENT",
        description="取消定时发布",
        user_message="取消定时发布",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "MANAGE_SCHEDULE",
        },
    ),
    EvalCase(
        case_id="tu-simple-06", category="INTENT",
        description="查看我的草稿",
        user_message="查看我的草稿",
        expected_intent={
            "operation_mode": "SIMPLE" if False else "SIMPLE",  # placeholder
            "goal_category": "QUERY_INFO",
        },
    ),
]

# ═══════════════════════════════════════════════════════════════════
# 2. MODIFY — modify existing resource
# ═══════════════════════════════════════════════════════════════════

MODIFY_CASES: list[EvalCase] = [
    EvalCase(
        case_id="tu-modify-01", category="INTENT",
        description="修改刚才那篇文章",
        user_message="修改刚才那篇文章",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "IMPROVE_CONTENT",
            "relation": "MODIFY_TASK",
        },
    ),
    EvalCase(
        case_id="tu-modify-02", category="INTENT",
        description="优化昨天Java帖子",
        user_message="优化昨天的Java帖子",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "IMPROVE_CONTENT",
            "relation": "MODIFY_TASK",
        },
    ),
    EvalCase(
        case_id="tu-modify-03", category="INTENT",
        description="把标题改一下",
        user_message="把标题改一下",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "IMPROVE_CONTENT",
            "relation": "MODIFY_TASK",
        },
    ),
    EvalCase(
        case_id="tu-modify-04", category="INTENT",
        description="把发布时间改成晚上9点",
        user_message="把发布时间改成晚上9点",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "MANAGE_SCHEDULE",
            "relation": "MODIFY_TASK",
        },
    ),
    EvalCase(
        case_id="tu-modify-05", category="INTENT",
        description="完善文章内容",
        user_message="完善一下这篇文章的内容",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "IMPROVE_CONTENT",
        },
    ),
]

# ═══════════════════════════════════════════════════════════════════
# 3. COMPOSITE — multi-step, single goal
# ═══════════════════════════════════════════════════════════════════

COMPOSITE_CASES: list[EvalCase] = [
    EvalCase(
        case_id="tu-composite-01", category="INTENT",
        description="搜索+分析+创建",
        user_message="搜索热门Java帖子，分析原因，然后写一篇新文章",
        expected_intent={
            "operation_mode": "COMPOSITE",
            "goal_category": "CREATE_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-composite-02", category="INTENT",
        description="搜索+总结学习路线",
        user_message="找Agent热门内容，整理一份学习路线",
        expected_intent={
            "operation_mode": "COMPOSITE",
            "goal_category": "CREATE_CONTENT" if False else "CREATE_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-composite-03", category="INTENT",
        description="搜索+分析+创建+发布",
        user_message="搜索社区热门Java帖子，分析受欢迎原因，生成一篇原创文章，明天上午发布",
        expected_intent={
            "operation_mode": "COMPOSITE",
            "goal_category": "CREATE_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-composite-04", category="INTENT",
        description="搜索+优化已有文章",
        user_message="搜索Spring Boot相关热门帖子，参考它们优化我昨天写的那篇文章",
        expected_intent={
            "operation_mode": "COMPOSITE",
            "goal_category": "IMPROVE_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-composite-05", category="INTENT",
        description="搜索+评估+选择生成",
        user_message="搜索社区Go语言帖子，如果热度高就生成一篇Go学习路线",
        expected_intent={
            "operation_mode": "COMPOSITE" if False else "COMPOSITE",
            "goal_category": "CREATE_CONTENT",
        },
    ),
]

# ═══════════════════════════════════════════════════════════════════
# 4. CONDITIONAL — conditional resource operations
# ═══════════════════════════════════════════════════════════════════

CONDITIONAL_CASES: list[EvalCase] = [
    EvalCase(
        case_id="tu-cond-01", category="INTENT",
        description="有旧文章则修改，没有则创建",
        user_message="如果有旧文章就修改，没有就创建一篇新的",
        expected_intent={
            "operation_mode": "CONDITIONAL",
            "goal_category": "CREATE_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-cond-02", category="INTENT",
        description="有搜索则总结，否则自己生成",
        user_message="搜索社区Java帖子，如果搜到相关内容就总结，否则自己生成一篇",
        expected_intent={
            "operation_mode": "CONDITIONAL",
            "goal_category": "CREATE_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-cond-03", category="INTENT",
        description="有草稿则优化，无草稿重新创建",
        user_message="有草稿则优化，没有草稿重新创建一篇Java文章",
        expected_intent={
            "operation_mode": "CONDITIONAL",
            "goal_category": "CREATE_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-cond-04", category="INTENT",
        description="有则修改无则创建(条件运营)",
        user_message="帮我运营一个Java并发专题：搜索社区热门，分析原因，检查已有虚拟线程文章，有则修改无则创建",
        expected_intent={
            "operation_mode": "CONDITIONAL",
            "goal_category": "CREATE_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-cond-05", category="INTENT",
        description="搜索+条件生成",
        user_message="搜索虚拟线程相关内容，如果已经有文章了就不要重复创建",
        expected_intent={
            "operation_mode": "CONDITIONAL",
            "goal_category": "ANALYZE_COMMUNITY",
        },
    ),
]

# ═══════════════════════════════════════════════════════════════════
# 5. HITL — human-in-the-loop
# ═══════════════════════════════════════════════════════════════════

HITL_CASES: list[EvalCase] = [
    EvalCase(
        case_id="tu-hitl-01", category="INTENT",
        description="发布之前让我确认",
        user_message="发布之前让我确认一下",
        expected_intent={
            "operation_mode": "SIMPLE" if False else "SIMPLE",
            "goal_category": "PUBLISH_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-hitl-02", category="INTENT",
        description="等我审核后发布",
        user_message="等我审核后再发布",
        expected_intent={
            "operation_mode": "SIMPLE" if False else "SIMPLE",
            "goal_category": "PUBLISH_CONTENT",
        },
    ),
    EvalCase(
        case_id="tu-hitl-03", category="INTENT",
        description="生成后让我审阅",
        user_message="帮我写一篇Java文章，生成后让我审阅再发布",
        expected_intent={
            "operation_mode": "SIMPLE",
            "goal_category": "CREATE_CONTENT",
        },
    ),
]

# ── master catalog ───────────────────────────────────────────────────

ALL_INTENT_CASES: dict[str, list[EvalCase]] = {
    "simple": SIMPLE_CASES,
    "modify": MODIFY_CASES,
    "composite": COMPOSITE_CASES,
    "conditional": CONDITIONAL_CASES,
    "hitl": HITL_CASES,
}
