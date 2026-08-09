"""Built-in evaluation datasets for GreenBook Agent Runtime.

Phase 6.3: INTENT (20 cases) + DECOMPOSITION (10 cases).
"""

from __future__ import annotations

from .models import EvalCase

# ═══════════════════════════════════════════════════════════════════
# INTENT_DATASET — 20 cases
# ═══════════════════════════════════════════════════════════════════

INTENT_DATASET: list[EvalCase] = [
    # ── CREATE ──
    EvalCase(
        case_id="intent-create-01", category="INTENT",
        description="明确创建文章",
        user_message="帮我写一篇Java文章",
        expected_intent={"goal_category": "CREATE_CONTENT", "relation": "NEW_TASK"},
    ),
    EvalCase(
        case_id="intent-create-02", category="INTENT",
        description="创建并定时发布",
        user_message="帮我写一篇Java文章，明天上午8点发布",
        expected_intent={
            "goal_category": "CREATE_CONTENT", "relation": "NEW_TASK",
            "resource_requests": [
                {"operation": "CREATE", "resource_type": "CONTENT_DRAFT"},
                {"operation": "CREATE", "resource_type": "SCHEDULE"},
            ],
        },
    ),
    EvalCase(
        case_id="intent-create-03", category="INTENT",
        description="生成文章",
        user_message="生成一篇Spring Boot教程",
        expected_intent={"goal_category": "CREATE_CONTENT"},
    ),
    EvalCase(
        case_id="intent-create-04", category="INTENT",
        description="发一篇帖子",
        user_message="发一篇Java面试题总结",
        expected_intent={"goal_category": "CREATE_CONTENT"},
    ),

    # ── IMPROVE ──
    EvalCase(
        case_id="intent-improve-01", category="INTENT",
        description="明确修改",
        user_message="修改刚才那篇文章标题",
        expected_intent={"goal_category": "IMPROVE_CONTENT", "relation": "MODIFY_TASK"},
    ),
    EvalCase(
        case_id="intent-improve-02", category="INTENT",
        description="优化(同义表达)",
        user_message="参考热门Java帖子优化刚才文章",
        expected_intent={"goal_category": "IMPROVE_CONTENT"},
    ),
    EvalCase(
        case_id="intent-improve-03", category="INTENT",
        description="完善(同义表达)",
        user_message="完善一下这篇文章",
        expected_intent={"goal_category": "IMPROVE_CONTENT"},
    ),
    EvalCase(
        case_id="intent-improve-04", category="INTENT",
        description="打磨(同义表达)",
        user_message="重新打磨这篇文章",
        expected_intent={"goal_category": "IMPROVE_CONTENT"},
    ),
    EvalCase(
        case_id="intent-improve-05", category="INTENT",
        description="提升质量(同义表达)",
        user_message="提升文章质量",
        expected_intent={"goal_category": "IMPROVE_CONTENT"},
    ),
    EvalCase(
        case_id="intent-improve-06", category="INTENT",
        description="充实内容(同义表达)",
        user_message="充实文章内容",
        expected_intent={"goal_category": "IMPROVE_CONTENT"},
    ),

    # ── SEARCH ──
    EvalCase(
        case_id="intent-search-01", category="INTENT",
        description="搜索社区帖子",
        user_message="搜索社区Java帖子",
        expected_intent={"goal_category": "ANALYZE_COMMUNITY"},
    ),
    EvalCase(
        case_id="intent-search-02", category="INTENT",
        description="查找内容",
        user_message="查找社区中的Python教程",
        expected_intent={"goal_category": "ANALYZE_COMMUNITY"},
    ),
    EvalCase(
        case_id="intent-search-03", category="INTENT",
        description="找一下",
        user_message="找一下社区里的Spring教程",
        expected_intent={"goal_category": "ANALYZE_COMMUNITY"},
    ),

    # ── PUBLISH ──
    EvalCase(
        case_id="intent-publish-01", category="INTENT",
        description="定时发布现有草稿",
        user_message="把刚才那篇文章明天上午发布",
        expected_intent={"goal_category": "PUBLISH_CONTENT"},
    ),

    # ── CANCEL ──
    EvalCase(
        case_id="intent-cancel-01", category="INTENT",
        description="取消定时",
        user_message="取消定时发布",
        expected_intent={"goal_category": "MANAGE_SCHEDULE", "relation": "CANCEL_TASK"},
    ),
    EvalCase(
        case_id="intent-cancel-02", category="INTENT",
        description="撤销任务",
        user_message="撤销刚才的定时任务",
        expected_intent={"goal_category": "MANAGE_SCHEDULE"},
    ),

    # ── 模糊表达 ──
    EvalCase(
        case_id="intent-ambiguous-01", category="INTENT",
        description="刚才那个(纯时间)",
        user_message="修改刚才那个",
        expected_intent={"goal_category": "IMPROVE_CONTENT", "relation": "MODIFY_TASK"},
    ),
    EvalCase(
        case_id="intent-ambiguous-02", category="INTENT",
        description="那个文章(模糊指代)",
        user_message="把那个文章标题改一下",
        expected_intent={"goal_category": "IMPROVE_CONTENT", "relation": "MODIFY_TASK"},
    ),

    # ── DIRECT ──
    EvalCase(
        case_id="intent-direct-01", category="INTENT",
        description="简单问候",
        user_message="你好",
        expected_intent={"goal_category": "QUERY_INFO", "relation": "DIRECT"},
    ),
    EvalCase(
        case_id="intent-direct-02", category="INTENT",
        description="知识问答",
        user_message="Java是什么",
        expected_intent={"goal_category": "QUERY_INFO"},
    ),
]

# ═══════════════════════════════════════════════════════════════════
# DECOMPOSITION_DATASET — 10 cases
# ═══════════════════════════════════════════════════════════════════

DECOMPOSITION_DATASET: list[EvalCase] = [
    # ── 应拆分 ──
    EvalCase(
        case_id="decomp-split-01", category="DECOMPOSITION",
        description="两个独立创建(然后)",
        user_message="写Java文章。然后写Python文章。",
        expected_sub_task_count=2,
    ),
    EvalCase(
        case_id="decomp-split-02", category="DECOMPOSITION",
        description="两个独立创建(再)",
        user_message="写一篇Java文章，再写一篇Python文章",
        expected_sub_task_count=2,
    ),
    EvalCase(
        case_id="decomp-split-03", category="DECOMPOSITION",
        description="两个独立创建(另外)",
        user_message="创建Spring文章，另外创建Java文章",
        expected_sub_task_count=2,
    ),
    EvalCase(
        case_id="decomp-split-04", category="DECOMPOSITION",
        description="三个任务带序数引用",
        user_message=(
            "写一篇Spring Boot文章明天10点发布。"
            "然后再写一篇Java集合文章晚上8点发布。"
            "最后把第一篇文章发布时间改成晚上9点。"
        ),
        expected_sub_task_count=3,
    ),
    EvalCase(
        case_id="decomp-split-05", category="DECOMPOSITION",
        description="两个取消",
        user_message="取消Java文章发布，再取消Python文章发布",
        expected_sub_task_count=2,
    ),

    # ── 不应拆分 ──
    EvalCase(
        case_id="decomp-merge-01", category="DECOMPOSITION",
        description="单任务多步骤(搜索→分析→生成)",
        user_message="搜索Java帖子然后分析原因然后生成文章",
        expected_sub_task_count=1,
    ),
    EvalCase(
        case_id="decomp-merge-02", category="DECOMPOSITION",
        description="单任务创建(无分隔符)",
        user_message="帮我写一篇Java并发文章标题新颖包含代码示例明天上午发布",
        expected_sub_task_count=1,
    ),
    EvalCase(
        case_id="decomp-merge-03", category="DECOMPOSITION",
        description="单任务(简单创建)",
        user_message="写一篇Spring教程",
        expected_sub_task_count=1,
    ),
    EvalCase(
        case_id="decomp-merge-04", category="DECOMPOSITION",
        description="单任务(搜索+总结)",
        user_message="搜索社区Java帖子并总结热门内容",
        expected_sub_task_count=1,
    ),
    EvalCase(
        case_id="decomp-merge-05", category="DECOMPOSITION",
        description="单任务(优化文章)",
        user_message="参考社区热门Java帖子优化刚才那篇文章",
        expected_sub_task_count=1,
    ),
]

# ── master catalog ───────────────────────────────────────────────────

ALL_DATASETS: dict[str, list[EvalCase]] = {
    "intent": INTENT_DATASET,
    "decomposition": DECOMPOSITION_DATASET,
}
