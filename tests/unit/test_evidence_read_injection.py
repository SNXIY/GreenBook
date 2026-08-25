"""Deterministic evidence-bounded analysis: a SEARCH+ANALYZE Goal must get a
GET_POST_DETAIL read step inserted between them (system guarantee, not a model
hint), ordered before the ANALYZE node."""

from __future__ import annotations

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.goal.compiler import GoalCompiler
from greenbook_agent_core.goal.models import Goal, GoalTree


def _compiler() -> GoalCompiler:
    return GoalCompiler(registry=CapabilityRegistry())


def _analysis_goal_tree() -> GoalTree:
    return GoalTree(
        root=Goal(
            goal_id="g1",
            description="搜索 Agent 帖子并总结共同方法再写文章",
            goal_type="TASK",
            required_capabilities=[
                "SEARCH_COMMUNITY",
                "ANALYZE_CONTENT_PATTERNS",
                "GENERATE_CONTENT",
                "SCHEDULE_PUBLISH",
            ],
        )
    )


def test_search_analyze_goal_gets_evidence_read_inserted() -> None:
    tree = _compiler().materialize_task_nodes(_analysis_goal_tree())
    caps = [node.capability for node in tree.task_nodes]
    assert "GET_POST_DETAIL" in caps
    # Ordering: SEARCH -> GET_POST_DETAIL -> ANALYZE -> GENERATE -> SCHEDULE
    search = caps.index("SEARCH_COMMUNITY")
    read = caps.index("GET_POST_DETAIL")
    analyze = caps.index("ANALYZE_CONTENT_PATTERNS")
    assert search < read < analyze, f"bad order: {caps}"
    # The read depends on the search; the analyze depends on the read.
    read_node = tree.task_nodes[read]
    analyze_node = tree.task_nodes[analyze]
    search_node = tree.task_nodes[search]
    assert search_node.task_id in read_node.dependencies
    assert read_node.task_id in analyze_node.dependencies


def test_goal_with_explicit_read_is_not_duplicated() -> None:
    tree = _compiler().materialize_task_nodes(_analysis_goal_tree())
    tree2 = _compiler().materialize_task_nodes(tree)  # idempotent
    read_count = sum(1 for n in tree2.task_nodes if n.capability == "GET_POST_DETAIL")
    assert read_count == 1


def test_plain_goal_without_analyze_gets_no_read() -> None:
    tree = _compiler().materialize_task_nodes(GoalTree(
        root=Goal(
            goal_id="g2",
            description="直接写一篇文章",
            goal_type="TASK",
            required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        )
    ))
    caps = [node.capability for node in tree.task_nodes]
    assert "GET_POST_DETAIL" not in caps
