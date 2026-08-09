"""Phase 6.8.1 Stage B — Enhanced Intent Evaluation Dataset.

Each case declares detailed expectations for IntentSpec fields:
  expected_mode, expected_actions, expected_resources,
  expected_conditions, expected_constraints,
  expected_goal_category, expected_relation

Plus paraphrase cases for semantic generalization testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IntentEvalCase:
    """One intent understanding evaluation case with detailed expectations."""

    case_id: str
    category: str  # SIMPLE | MODIFY | COMPOSITE | CONDITIONAL | HITL | PARAPHRASE | COMPLEX
    description: str
    user_message: str
    existing_tasks: list[dict] = field(default_factory=list)

    # ── IntentSpec-level expectations ──
    expected_mode: str | None = None       # SIMPLE | COMPOSITE | CONDITIONAL
    expected_actions: set[str] | None = None  # DEPRECATED — use required_actions
    expected_resources: set[str] | None = None  # {"CREATE:CONTENT", "UPDATE:SCHEDULE", ...}
    expected_conditions: bool = False       # True if conditions should be present
    expected_condition_types: set[str] | None = None  # {"IF_EXISTS", ...}
    expected_constraints: set[str] | None = None  # {"APPROVAL", "TIME", ...}

    # ── Phase 6.8.1 Stage C: relaxed action matching ──
    required_actions: set[str] | None = None   # Must all be present
    optional_actions: set[str] | None = None   # OK if present (e.g. ANALYZE)
    forbidden_actions: set[str] | None = None  # Must NOT be present (e.g. DELETE)

    # ── Legacy TaskIntent expectations ──
    expected_goal_category: str | None = None
    expected_relation: str | None = None

    # ── Routing expectation ──
    should_trigger_l2: bool = True  # Whether _needs_l2_v2 should return True


# ═══════════════════════════════════════════════════════════════════════
# 1. SIMPLE — single-goal, single-step (10 cases)
# ═══════════════════════════════════════════════════════════════════════

SIMPLE_CASES: list[IntentEvalCase] = [
    IntentEvalCase(
        case_id="v2-simple-01",
        category="SIMPLE",
        description="写Java文章",
        user_message="写一篇Java文章",
        expected_mode="SIMPLE",
        expected_actions={"CREATE"},
        expected_resources={"CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-simple-02",
        category="SIMPLE",
        description="创建Python教程",
        user_message="创建一个Python教程",
        expected_mode="SIMPLE",
        expected_actions={"CREATE"},
        expected_resources={"CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-simple-03",
        category="SIMPLE",
        description="搜索社区Java帖子",
        user_message="搜索社区Java帖子",
        expected_mode="SIMPLE",
        expected_actions={"SEARCH"},
        expected_resources={"SEARCH:POST"},
        expected_goal_category="ANALYZE_COMMUNITY",
        expected_relation="NEW_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-simple-04",
        category="SIMPLE",
        description="取消定时发布",
        user_message="取消定时发布",
        expected_mode="SIMPLE",
        expected_actions={"DELETE"},
        expected_resources={"DELETE:SCHEDULE"},
        expected_goal_category="MANAGE_SCHEDULE",
        expected_relation="CANCEL_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-simple-05",
        category="SIMPLE",
        description="查看我的草稿",
        user_message="查看我的草稿",
        expected_mode="SIMPLE",
        expected_actions={"QUERY"},
        expected_resources={"QUERY:DRAFT"},
        expected_goal_category="QUERY_INFO",
        expected_relation="DIRECT",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-simple-06",
        category="SIMPLE",
        description="你好(问候)",
        user_message="你好",
        expected_mode="SIMPLE",
        expected_actions={"QUERY"},
        expected_goal_category="QUERY_INFO",
        expected_relation="DIRECT",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-simple-07",
        category="SIMPLE",
        description="发布内容(无时间)",
        user_message="帮我发布这篇文章",
        expected_mode="SIMPLE",
        expected_actions={"PUBLISH"},
        expected_resources={"PUBLISH:CONTENT"},
        expected_goal_category="PUBLISH_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-simple-08",
        category="SIMPLE",
        description="明天发布Spring文章",
        user_message="明天发布一篇Spring文章",
        expected_mode="SIMPLE",
        expected_actions={"CREATE", "PUBLISH"},
        expected_resources={"CREATE:CONTENT", "PUBLISH:CONTENT"},
        expected_constraints={"TIME"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,  # "明天" is a time marker
    ),
    IntentEvalCase(
        case_id="v2-simple-09",
        category="SIMPLE",
        description="修改文章标题",
        user_message="修改刚才那篇文章的标题",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:CONTENT"},
        expected_goal_category="IMPROVE_CONTENT",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-simple-10",
        category="SIMPLE",
        description="优化文章",
        user_message="优化昨天那篇Java帖子",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:CONTENT"},
        expected_goal_category="IMPROVE_CONTENT",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,  # "优化" is ambiguous verb
    ),
]

# ═══════════════════════════════════════════════════════════════════════
# 2. MODIFY — modify existing resource (5 cases)
# ═══════════════════════════════════════════════════════════════════════

MODIFY_CASES: list[IntentEvalCase] = [
    IntentEvalCase(
        case_id="v2-modify-01",
        category="MODIFY",
        description="把发布时间改成晚上9点",
        user_message="把发布时间改成晚上9点",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:SCHEDULE"},
        expected_constraints={"TIME"},
        expected_goal_category="MANAGE_SCHEDULE",  # UPDATE SCHEDULE → MANAGE_SCHEDULE per compat mapping
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-modify-02",
        category="MODIFY",
        description="改发布时间(变体)",
        user_message="明天那篇改到十点发",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:SCHEDULE"},
        expected_constraints={"TIME"},
        expected_goal_category="MANAGE_SCHEDULE",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-modify-03",
        category="MODIFY",
        description="调整定时任务",
        user_message="调一下之前那个定时任务，晚上九点再发",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:SCHEDULE"},
        expected_constraints={"TIME"},
        expected_goal_category="MANAGE_SCHEDULE",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-modify-04",
        category="MODIFY",
        description="修改标题",
        user_message="把标题改一下",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:CONTENT"},
        expected_goal_category="IMPROVE_CONTENT",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-modify-05",
        category="MODIFY",
        description="完善文章内容",
        user_message="完善一下这篇文章的内容",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:CONTENT"},
        expected_goal_category="IMPROVE_CONTENT",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,
    ),
]

# ═══════════════════════════════════════════════════════════════════════
# 3. COMPOSITE — multi-step, single goal (5 cases)
# ═══════════════════════════════════════════════════════════════════════

COMPOSITE_CASES: list[IntentEvalCase] = [
    IntentEvalCase(
        case_id="v2-composite-01",
        category="COMPOSITE",
        description="搜索+分析+创建",
        user_message="搜索热门Java帖子，分析原因，然后写一篇新文章",
        expected_mode="COMPOSITE",
        required_actions={"SEARCH", "CREATE"},
        optional_actions={"ANALYZE"},
        forbidden_actions={"DELETE"},
        expected_resources={"SEARCH:POST", "CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-composite-02",
        category="COMPOSITE",
        description="搜索+总结学习路线",
        user_message="找Agent热门内容，整理一份学习路线",
        expected_mode="COMPOSITE",
        required_actions={"SEARCH", "CREATE"},
        optional_actions={"ANALYZE"},
        forbidden_actions={"DELETE"},
        expected_resources={"SEARCH:POST", "CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-composite-03",
        category="COMPOSITE",
        description="搜索+分析+创建+发布",
        user_message="搜索社区热门Java帖子，分析受欢迎原因，生成一篇原创文章，明天上午发布",
        expected_mode="COMPOSITE",
        required_actions={"SEARCH", "CREATE", "PUBLISH"},
        optional_actions={"ANALYZE"},
        forbidden_actions={"DELETE"},
        expected_resources={"SEARCH:POST", "CREATE:CONTENT", "PUBLISH:CONTENT"},
        expected_constraints={"TIME"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-composite-04",
        category="COMPOSITE",
        description="搜索+优化已有文章",
        user_message="搜索Spring Boot相关热门帖子，参考它们优化我昨天写的那篇文章",
        expected_mode="COMPOSITE",
        required_actions={"SEARCH", "UPDATE"},
        optional_actions={"ANALYZE"},
        forbidden_actions={"DELETE"},
        expected_resources={"SEARCH:POST", "UPDATE:CONTENT"},
        expected_goal_category="IMPROVE_CONTENT",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-composite-05",
        category="COMPOSITE",
        description="搜索+总结+写文章",
        user_message="搜索社区Go语言帖子，总结热门趋势，然后写一篇Go学习路线",
        expected_mode="COMPOSITE",
        required_actions={"SEARCH", "CREATE"},
        optional_actions={"ANALYZE"},
        forbidden_actions={"DELETE"},
        expected_resources={"SEARCH:POST", "CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
]

# ═══════════════════════════════════════════════════════════════════════
# 4. CONDITIONAL — conditional resource operations (5 cases)
# ═══════════════════════════════════════════════════════════════════════

CONDITIONAL_CASES: list[IntentEvalCase] = [
    IntentEvalCase(
        case_id="v2-cond-01",
        category="CONDITIONAL",
        description="有旧文章则修改，没有则创建",
        user_message="如果有旧文章就修改，没有就创建一篇新的",
        expected_mode="CONDITIONAL",
        required_actions={"UPDATE_OR_CREATE"},
        forbidden_actions={"DELETE"},
        expected_resources={"UPDATE_OR_CREATE:CONTENT"},
        expected_conditions=True,
        expected_condition_types={"IF_EXISTS"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-cond-02",
        category="CONDITIONAL",
        description="有草稿则优化，无草稿重新创建",
        user_message="有草稿则优化，没有草稿重新创建一篇Java文章",
        expected_mode="CONDITIONAL",
        required_actions={"UPDATE_OR_CREATE"},
        forbidden_actions={"DELETE"},
        expected_resources={"UPDATE_OR_CREATE:CONTENT"},
        expected_conditions=True,
        expected_condition_types={"IF_EXISTS"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-cond-03",
        category="CONDITIONAL",
        description="条件运营(搜索+条件创建)",
        user_message="帮我运营一个Java并发专题：搜索社区热门，分析原因，检查已有虚拟线程文章，有则修改无则创建",
        expected_mode="CONDITIONAL",
        required_actions={"SEARCH", "UPDATE_OR_CREATE"},
        optional_actions={"ANALYZE"},
        forbidden_actions={"DELETE"},
        expected_resources={"SEARCH:POST", "UPDATE_OR_CREATE:CONTENT"},
        expected_conditions=True,
        expected_condition_types={"IF_EXISTS"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-cond-04",
        category="CONDITIONAL",
        description="搜索后条件生成",
        user_message="搜索虚拟线程相关内容，如果已经有文章了就不要重复创建",
        expected_mode="CONDITIONAL",
        required_actions={"SEARCH", "UPDATE_OR_CREATE"},
        forbidden_actions={"DELETE"},
        expected_resources={"SEARCH:POST", "UPDATE_OR_CREATE:CONTENT"},
        expected_conditions=True,
        expected_condition_types={"IF_EXISTS"},
        expected_goal_category="ANALYZE_COMMUNITY",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-cond-05",
        category="CONDITIONAL",
        description="找到就改，没找到就新建(paraphrase)",
        user_message="找到以前的稿子就接着完善，找不到就重新写",
        expected_mode="CONDITIONAL",
        required_actions={"UPDATE_OR_CREATE"},
        forbidden_actions={"DELETE"},
        expected_resources={"UPDATE_OR_CREATE:CONTENT"},
        expected_conditions=True,
        expected_condition_types={"IF_EXISTS"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
]

# ═══════════════════════════════════════════════════════════════════════
# 5. HITL — human-in-the-loop (5 cases)
# ═══════════════════════════════════════════════════════════════════════

HITL_CASES: list[IntentEvalCase] = [
    IntentEvalCase(
        case_id="v2-hitl-01",
        category="HITL",
        description="发布之前让我确认",
        user_message="发布之前让我确认一下",
        expected_mode="SIMPLE",
        required_actions={"PUBLISH"},
        forbidden_actions={"DELETE"},
        expected_resources={"PUBLISH:CONTENT"},
        expected_constraints={"APPROVAL"},
        expected_goal_category="PUBLISH_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-hitl-02",
        category="HITL",
        description="等我审核后发布",
        user_message="等我审核后再发布",
        expected_mode="SIMPLE",
        required_actions={"PUBLISH"},
        forbidden_actions={"DELETE"},
        expected_resources={"PUBLISH:CONTENT"},
        expected_constraints={"APPROVAL"},
        expected_goal_category="PUBLISH_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-hitl-03",
        category="HITL",
        description="先别发，我看过以后再发布",
        user_message="先别直接发，我看过以后再发布",
        expected_mode="SIMPLE",
        required_actions={"PUBLISH"},
        forbidden_actions={"DELETE"},
        expected_resources={"PUBLISH:CONTENT"},
        expected_constraints={"APPROVAL"},
        expected_goal_category="PUBLISH_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-hitl-04",
        category="HITL",
        description="写好先给我看，确认后五分钟发",
        user_message="写好先给我看看，我同意以后五分钟发",
        expected_mode="SIMPLE",
        required_actions={"PUBLISH"},
        forbidden_actions={"DELETE"},
        expected_resources={"PUBLISH:CONTENT"},
        expected_constraints={"APPROVAL", "TIME"},
        expected_goal_category="PUBLISH_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-hitl-05",
        category="HITL",
        description="等我确认之后再定时发布",
        user_message="等我确认之后再定时发布",
        expected_mode="SIMPLE",
        required_actions={"PUBLISH"},
        forbidden_actions={"DELETE"},
        expected_resources={"PUBLISH:CONTENT"},
        expected_constraints={"APPROVAL"},
        expected_goal_category="PUBLISH_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
]

# ═══════════════════════════════════════════════════════════════════════
# 6. PARAPHRASE — semantic generalization (same intent, different wording)
# ═══════════════════════════════════════════════════════════════════════

PARAPHRASE_CASES: list[IntentEvalCase] = [
    # ── CREATE paraphrases ──
    IntentEvalCase(
        case_id="v2-para-create-01",
        category="PARAPHRASE",
        description="写Java教程(原版)",
        user_message="写一篇Java教程",
        expected_mode="SIMPLE",
        expected_actions={"CREATE"},
        expected_resources={"CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-para-create-02",
        category="PARAPHRASE",
        description="帮我搞个Java教程",
        user_message="帮我搞个Java教程",
        expected_mode="SIMPLE",
        expected_actions={"CREATE"},
        expected_resources={"CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-para-create-03",
        category="PARAPHRASE",
        description="来个Java入门帖子",
        user_message="来个Java入门帖子",
        expected_mode="SIMPLE",
        expected_actions={"CREATE"},
        expected_resources={"CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-para-create-04",
        category="PARAPHRASE",
        description="做一篇新手Java内容",
        user_message="做一篇适合新手看的Java内容",
        expected_mode="SIMPLE",
        expected_actions={"CREATE"},
        expected_resources={"CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=False,
    ),
    # ── QUERY DRAFT paraphrases ──
    IntentEvalCase(
        case_id="v2-para-query-01",
        category="PARAPHRASE",
        description="查看草稿(原版)",
        user_message="查看我的草稿",
        expected_mode="SIMPLE",
        expected_actions={"QUERY"},
        expected_resources={"QUERY:DRAFT"},
        expected_goal_category="QUERY_INFO",
        expected_relation="DIRECT",
        should_trigger_l2=False,
    ),
    IntentEvalCase(
        case_id="v2-para-query-02",
        category="PARAPHRASE",
        description="没发的文章",
        user_message="我之前还有哪些没发的文章",
        expected_mode="SIMPLE",
        expected_actions={"QUERY"},
        expected_resources={"QUERY:DRAFT"},
        expected_goal_category="QUERY_INFO",
        expected_relation="QUERY_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-para-query-03",
        category="PARAPHRASE",
        description="未发布内容",
        user_message="帮我看看现在有哪些内容还没发布",
        expected_mode="SIMPLE",
        expected_actions={"QUERY"},
        expected_resources={"QUERY:DRAFT"},
        expected_goal_category="QUERY_INFO",
        expected_relation="DIRECT",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-para-query-04",
        category="PARAPHRASE",
        description="找保存的稿子",
        user_message="找一下我之前保存的稿子",
        expected_mode="SIMPLE",
        expected_actions={"QUERY"},
        expected_resources={"QUERY:DRAFT"},
        expected_goal_category="QUERY_INFO",
        expected_relation="DIRECT",
        should_trigger_l2=False,
    ),
    # ── UPDATE SCHEDULE paraphrases ──
    IntentEvalCase(
        case_id="v2-para-sched-01",
        category="PARAPHRASE",
        description="改发布时间(原版)",
        user_message="把发布时间改成晚上九点",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:SCHEDULE"},
        expected_constraints={"TIME"},
        expected_goal_category="MANAGE_SCHEDULE",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-para-sched-02",
        category="PARAPHRASE",
        description="晚一点发",
        user_message="明天那篇晚一点发，改成十点",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:SCHEDULE"},
        expected_constraints={"TIME"},
        expected_goal_category="MANAGE_SCHEDULE",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-para-sched-03",
        category="PARAPHRASE",
        description="调定时任务",
        user_message="调一下之前那个定时任务，晚上九点再发",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:SCHEDULE"},
        expected_constraints={"TIME"},
        expected_goal_category="MANAGE_SCHEDULE",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-para-sched-04",
        category="PARAPHRASE",
        description="别八点发改九点",
        user_message="刚才那个别八点发了，改九点",
        expected_mode="SIMPLE",
        expected_actions={"UPDATE"},
        expected_resources={"UPDATE:SCHEDULE"},
        expected_constraints={"TIME"},
        expected_goal_category="MANAGE_SCHEDULE",
        expected_relation="MODIFY_TASK",
        should_trigger_l2=True,
    ),
    # ── COMPOSITE paraphrases ──
    IntentEvalCase(
        case_id="v2-para-comp-01",
        category="PARAPHRASE",
        description="搜索+分析+写路线(原版)",
        user_message="搜索热门Agent文章，分析以后写一篇学习路线",
        expected_mode="COMPOSITE",
        expected_actions={"SEARCH", "ANALYZE", "CREATE"},
        expected_resources={"SEARCH:POST", "CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-para-comp-02",
        category="PARAPHRASE",
        description="看热门+总结+写",
        user_message="看看社区最近Agent哪些内容比较火，总结规律后帮我写一篇",
        expected_mode="COMPOSITE",
        expected_actions={"SEARCH", "ANALYZE", "CREATE"},
        expected_resources={"SEARCH:POST", "CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-para-comp-03",
        category="PARAPHRASE",
        description="找+分析+生成",
        user_message="找相关帖子做分析，再根据结果生成新内容",
        expected_mode="COMPOSITE",
        expected_actions={"SEARCH", "ANALYZE", "CREATE"},
        expected_resources={"SEARCH:POST", "CREATE:CONTENT"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
]

# ═══════════════════════════════════════════════════════════════════════
# 7. COMPLEX — real-world compound cases (3 cases)
# ═══════════════════════════════════════════════════════════════════════

COMPLEX_CASES: list[IntentEvalCase] = [
    IntentEvalCase(
        case_id="v2-complex-01",
        category="COMPLEX",
        description="全流程运营专题",
        user_message=(
            "帮我运营一个Agent学习专题，先看看社区最近什么Agent内容比较火并分析原因。"
            "如果之前有我写过的Agent学习草稿，就在旧稿基础上完善；没有的话重新写一篇。"
            "标题参考热门内容调整，写好之后先让我确认，我确认以后五分钟发布。"
        ),
        expected_mode="CONDITIONAL",
        expected_actions={"SEARCH", "ANALYZE", "UPDATE_OR_CREATE", "PUBLISH"},
        expected_resources={"SEARCH:POST", "UPDATE_OR_CREATE:CONTENT", "PUBLISH:CONTENT"},
        expected_conditions=True,
        expected_condition_types={"IF_EXISTS"},
        expected_constraints={"APPROVAL", "TIME"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-complex-02",
        category="COMPLEX",
        description="搜索+条件+确认+发布",
        user_message=(
            "搜索Java并发相关帖子，如果之前写的并发文章还在就先改进它，"
            "没有就创建新的。发布前让我审一下。"
        ),
        expected_mode="CONDITIONAL",
        expected_actions={"SEARCH", "UPDATE_OR_CREATE", "PUBLISH"},
        expected_resources={"SEARCH:POST", "UPDATE_OR_CREATE:CONTENT", "PUBLISH:CONTENT"},
        expected_conditions=True,
        expected_condition_types={"IF_EXISTS"},
        expected_constraints={"APPROVAL"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
    IntentEvalCase(
        case_id="v2-complex-03",
        category="COMPLEX",
        description="运营专题(短版)",
        user_message="帮我运营一个Java专题：先搜索，再分析，有旧稿就改没有就新建",
        expected_mode="CONDITIONAL",
        expected_actions={"SEARCH", "ANALYZE", "UPDATE_OR_CREATE"},
        expected_resources={"SEARCH:POST", "UPDATE_OR_CREATE:CONTENT"},
        expected_conditions=True,
        expected_condition_types={"IF_EXISTS"},
        expected_goal_category="CREATE_CONTENT",
        expected_relation="NEW_TASK",
        should_trigger_l2=True,
    ),
]

# ── master catalog ───────────────────────────────────────────────────

ALL_V2_CASES: dict[str, list[IntentEvalCase]] = {
    "simple": SIMPLE_CASES,
    "modify": MODIFY_CASES,
    "composite": COMPOSITE_CASES,
    "conditional": CONDITIONAL_CASES,
    "hitl": HITL_CASES,
    "paraphrase": PARAPHRASE_CASES,
    "complex": COMPLEX_CASES,
}


def flatten_cases() -> list[IntentEvalCase]:
    """Return all cases as a flat list."""
    result: list[IntentEvalCase] = []
    for cases in ALL_V2_CASES.values():
        result.extend(cases)
    return result
