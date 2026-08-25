"""Read evidence must be projected separately from Agent reflection."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from apps.agent_api.greenbook_agent_api.services.retrieval_synthesis_projection import (
    build_retrieval_interaction,
)
from apps.agent_api.greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from apps.agent_api.greenbook_agent_api.services.action_loop_executor import (
    ActionLoopExecutor,
)
from greenbook_agent_core.actionloop.models import ActionLoopResult, ActionObservation


class _LLM:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create),
        )

    async def create(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(self.payload, ensure_ascii=False),
                ),
            )],
        )


def _search(*items: dict[str, Any], total: int = 0) -> dict[str, Any]:
    return {
        "ok": True,
        "tool_name": "community.search_public_posts",
        "capability": "SEARCH_COMMUNITY",
        "data": {"total": total or len(items), "items": list(items)},
    }


def _detail(
    post_id: str,
    title: str,
    content: str,
    *,
    read_status: str | None = None,
) -> dict[str, Any]:
    result = {
        "ok": True,
        "tool_name": "community.get_post",
        "capability": "GET_POST_DETAIL",
        "data": {"post_id": post_id, "title": title, "content": content},
    }
    if read_status:
        result["read_status"] = read_status
    return result


@pytest.mark.asyncio
async def test_pure_search_stays_query_result() -> None:
    interaction, message = await build_retrieval_interaction(
        request="搜索几篇相关帖子",
        tool_results=[_search({"post_id": "post-1", "title": "结果一"}, total=8)],
        synthesis_requested=False,
    )

    assert interaction is not None
    assert interaction["kind"] == "QUERY_RESULT"
    assert interaction["result"]["search"]["count"] == 8
    assert "结果一" in json.dumps(interaction, ensure_ascii=False)
    assert "goal is satisfied" not in message.lower()


@pytest.mark.asyncio
async def test_fast_path_search_carries_cards_into_message_projection() -> None:
    class _Mcp:
        async def execute_tool(self, _tool_name: str, **_kwargs: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "data": {
                    "total": 9,
                    "items": [
                        {
                            "post_id": "post-java-1",
                            "title": "Java 集合详解",
                            "content": "从 List 到 Map 的学习路径。",
                        },
                        {
                            "post_id": "post-java-2",
                            "title": "JVM 入门",
                            "content": "理解字节码与运行时。",
                        },
                    ],
                },
            }

    result = await ConversationRuntimeAdapter().execute_fast_path_read(
        tool_name="community.search_public_posts",
        arguments={"query": "Java"},
        user_request="搜几篇 Java 学习相关帖子。",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-1",
        trace_id="trace-1",
        session=SimpleNamespace(),
        auth=SimpleNamespace(),
        mcp=_Mcp(),
    )

    interaction = result.partial_results["user_facing_interaction"]
    assert interaction["kind"] == "QUERY_RESULT"
    assert interaction["result"]["search"]["count"] == 9
    assert [item["id"] for item in interaction["result"]["search"]["items"]] == [
        "post-java-1",
        "post-java-2",
    ]
    assert interaction["result"]["search"]["items"][0]["href"] == "/post/post-java-1"


@pytest.mark.asyncio
async def test_fast_path_single_post_summary_uses_the_detail_projection() -> None:
    class _Mcp:
        async def execute_tool(self, _tool_name: str, **_kwargs: Any) -> dict[str, Any]:
            return _detail(
                "post-java",
                "Java 调度实践",
                "文章解释了 ScheduledExecutorService 与虚拟线程的边界。",
            )

    result = await ConversationRuntimeAdapter().execute_fast_path_read(
        tool_name="community.get_post",
        arguments={"post_id": "post-java"},
        user_request="总结这篇帖子",
        synthesis_requested=True,
        llm=_LLM({
            "intro": "已读取这篇帖子。",
            "common_patterns": [],
            "differences": [],
            "conclusion": "帖子重点是 ScheduledExecutorService 与虚拟线程的边界。",
        }),
        model="test",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-1",
        trace_id="trace-1",
        session=SimpleNamespace(),
        auth=SimpleNamespace(),
        mcp=_Mcp(),
    )

    interaction = result.partial_results["user_facing_interaction"]
    assert interaction["kind"] == "SYNTHESIS_RESULT"
    assert interaction["synthesis"]["sources"][0]["resource_id"] == "post-java"
    assert interaction["synthesis"]["conclusion"] == (
        "帖子重点是 ScheduledExecutorService 与虚拟线程的边界。"
    )
    assert "没有找到" not in result.content


@pytest.mark.asyncio
async def test_action_loop_search_carries_observation_detail_into_message_projection() -> None:
    class _ActionLoop:
        async def run(self, *_args: Any, **_kwargs: Any) -> ActionLoopResult:
            return ActionLoopResult(
                status="COMPLETED",
                success=True,
                run_id="run-1",
                task_id="task-1",
                content="internal completion text",
                observations=[ActionObservation(
                    action="SEARCH_POSTS",
                    tool_name="community.search_public_posts",
                    outcome="SUCCESS",
                    ok=True,
                    detail={
                        "ok": True,
                        "data": {
                            "total": 9,
                            "items": [
                                {"post_id": "post-java-1", "title": "Java 入门"},
                            ],
                        },
                    },
                )],
            )

    executor = ActionLoopExecutor(
        adapter=SimpleNamespace(),
        action_loop=_ActionLoop(),
    )
    result = await executor.run(
        task=SimpleNamespace(task_id="task-1"),
        command=SimpleNamespace(
            raw_input="搜几篇 Java 学习相关帖子。",
            requested_goal="",
            required_capabilities=[],
        ),
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        run_id="run-1",
        trace_id="trace-1",
        session=SimpleNamespace(),
        timezone="Asia/Shanghai",
        mcp=SimpleNamespace(),
        auth=SimpleNamespace(),
    )

    interaction = result.partial_results["user_facing_interaction"]
    assert interaction["kind"] == "QUERY_RESULT"
    assert interaction["result"]["search"]["count"] == 9
    assert interaction["result"]["search"]["items"][0]["href"] == "/post/post-java-1"
    assert result.content != "internal completion text"


@pytest.mark.asyncio
async def test_synthesis_uses_sources_and_drops_single_source_common_points() -> None:
    llm = _LLM({
        "intro": "综合阅读了两篇内容。",
        "common_patterns": [
            {
                "title": "先拆解目标",
                "explanation": "两篇内容都先拆分任务。",
                "source_refs": ["source-1", "source-2"],
            },
            {
                "title": "无共同证据",
                "explanation": "只有一篇提到。",
                "source_refs": ["source-1"],
            },
        ],
        "differences": [],
        "conclusion": "共同点集中在可执行的任务拆分。",
    })
    interaction, _ = await build_retrieval_interaction(
        request="找几篇内容并总结共同方法",
        tool_results=[
            _search(
                {"post_id": "post-1", "title": "内容一"},
                {"post_id": "post-2", "title": "内容二"},
                total=18,
            ),
            _detail("post-1", "内容一", "第一篇正文讨论任务拆分。"),
            _detail("post-2", "内容二", "第二篇正文也讨论任务拆分。"),
        ],
        synthesis_requested=True,
        llm=llm,
        model="test",
    )

    assert interaction is not None
    assert interaction["kind"] == "SYNTHESIS_RESULT"
    synthesis = interaction["synthesis"]
    assert synthesis["total_matched"] == 18
    assert synthesis["language"] == "zh"
    assert synthesis["read_count"] == 2
    assert len(synthesis["sources"]) == 2
    assert len(synthesis["common_patterns"]) == 1
    assert synthesis["common_patterns"][0]["source_refs"] == ["source-1", "source-2"]
    assert "goal is satisfied" not in json.dumps(interaction, ensure_ascii=False).lower()


@pytest.mark.asyncio
async def test_single_post_summary_projects_the_readable_detail() -> None:
    llm = _LLM({
        "intro": "已读取这篇帖子。",
        "common_patterns": [],
        "differences": [],
        "conclusion": "文章解释了 ScheduledExecutorService、固定延迟与虚拟线程的边界。",
    })

    interaction, message = await build_retrieval_interaction(
        request="总结这篇帖子，告诉我重点有哪些",
        tool_results=[
            _detail(
                "post-java",
                "Java 调度实践",
                "文章解释了 ScheduledExecutorService、固定延迟与虚拟线程的边界。",
            ),
        ],
        synthesis_requested=True,
        llm=llm,
        model="test",
    )

    assert interaction is not None
    assert interaction["kind"] == "SYNTHESIS_RESULT"
    synthesis = interaction["synthesis"]
    assert synthesis["total_matched"] == 1
    assert synthesis["read_count"] == 1
    assert synthesis["sources"][0]["resource_id"] == "post-java"
    assert synthesis["conclusion"] == "文章解释了 ScheduledExecutorService、固定延迟与虚拟线程的边界。"
    assert synthesis["evidence_note"] is None
    assert message == synthesis["conclusion"]


@pytest.mark.asyncio
async def test_insufficient_and_partial_evidence_are_explicit() -> None:
    interaction, _ = await build_retrieval_interaction(
        request="查找并总结这些内容",
        tool_results=[
            _search({"post_id": "post-1", "title": "内容一"}, total=10),
            _detail("post-1", "内容一", "只有一篇完整正文。"),
            {
                "ok": False,
                "tool_name": "community.get_post",
                "capability": "GET_POST_DETAIL",
                "error_code": "UNAVAILABLE",
            },
        ],
        synthesis_requested=True,
    )

    assert interaction is not None
    synthesis = interaction["synthesis"]
    assert interaction["status"] == "PARTIAL_SUCCESS"
    assert synthesis["read_count"] == 1
    assert synthesis["failed_count"] == 1
    assert synthesis["common_patterns"] == []
    assert "不足" in synthesis["evidence_note"]
    assert "goal" not in json.dumps(synthesis, ensure_ascii=False).lower()


@pytest.mark.asyncio
async def test_counts_and_read_statuses_are_projected_without_overclaiming() -> None:
    llm = _LLM({
        "common_patterns": [{
            "title": "共同方法",
            "explanation": "多篇内容都从基础概念进入实践。",
            "source_refs": ["source-1", "source-2"],
        }],
        "differences": [],
        "conclusion": "这些内容都把基础和实践连接起来。",
    })
    # Keep the fixture explicit: five detail attempts represent selected items;
    # one is partial and one fails, so only four have readable evidence.
    interaction, _ = await build_retrieval_interaction(
        request="找一些内容并总结共同方法",
        tool_results=[
            _search(
                *[
                    {"post_id": f"post-{index}", "title": f"内容 {index}"}
                    for index in range(1, 6)
                ],
                total=8,
            ),
            _detail("post-1", "内容 1", "## 基础\n- 进入实践"),
            _detail("post-2", "内容 2", "内容二也从基础进入实践。"),
            _detail("post-3", "内容 3", "内容三强调实践。"),
            _detail("post-4", "内容 4", "内容四的正文只读取了一部分。", read_status="PARTIAL"),
            {
                "ok": False,
                "tool_name": "community.get_post",
                "capability": "GET_POST_DETAIL",
                "error_code": "UNAVAILABLE",
            },
        ],
        synthesis_requested=True,
        llm=llm,
        model="test",
    )

    assert interaction is not None
    synthesis = interaction["synthesis"]
    assert synthesis["total_matched"] == 8
    assert synthesis["selected_count"] == 5
    assert synthesis["read_count"] == 4
    assert synthesis["failed_count"] == 1
    assert interaction["status"] == "PARTIAL_SUCCESS"
    assert "选择了 5 篇" in synthesis["intro"]
    assert "成功读取 4 篇" in synthesis["intro"]
    assert synthesis["sources"][0]["excerpt"] == "基础 进入实践"
    assert synthesis["sources"][3]["read_status"] == "PARTIAL"


@pytest.mark.asyncio
async def test_internal_refs_and_ungrounded_differences_never_become_user_copy() -> None:
    llm = _LLM({
        "common_patterns": [{
            "title": "共同点 source-1",
            "explanation": "source-1 和 source-2 都提到这一点。",
            "source_refs": ["source-1", "source-2"],
        }],
        "differences": [
            {
                "title": "阶段数量",
                "explanation": "另一个来源可能分为 3 个阶段。",
                "source_refs": ["source-1", "source-2"],
            },
            {
                "title": "单一来源差异",
                "explanation": "只有一篇内容提到这一点。",
                "source_refs": ["source-1"],
            },
        ],
        "conclusion": "综合来看，source-1 和 source-2 支持这个结论。",
    })
    interaction, _ = await build_retrieval_interaction(
        request="比较这些内容",
        tool_results=[
            _search(
                {"post_id": "post-1", "title": "内容一"},
                {"post_id": "post-2", "title": "内容二"},
                total=2,
            ),
            _detail("post-1", "内容一", "正文一"),
            _detail("post-2", "内容二", "正文二"),
        ],
        synthesis_requested=True,
        llm=llm,
        model="test",
    )

    assert interaction is not None
    synthesis = interaction["synthesis"]
    assert synthesis["common_patterns"] == []
    assert synthesis["differences"] == []
    assert synthesis["conclusion"] == ""
    assert "source-1" in json.dumps(synthesis, ensure_ascii=False)
    assert "source-1" not in synthesis["intro"]


@pytest.mark.asyncio
async def test_metadata_only_source_is_displayed_but_not_used_as_evidence() -> None:
    llm = _LLM({
        "common_patterns": [{
            "title": "共同点",
            "explanation": "两篇有正文的内容都强调实践。",
            "source_refs": ["source-2", "source-3"],
        }],
        "differences": [],
        "conclusion": "有正文的内容都强调实践。",
    })
    interaction, _ = await build_retrieval_interaction(
        request="找内容并总结",
        tool_results=[
            _search(
                {"post_id": "post-1", "title": "只有标题"},
                {"post_id": "post-2", "title": "正文二"},
                {"post_id": "post-3", "title": "正文三"},
                total=3,
            ),
            _detail("post-1", "只有标题", ""),
            _detail("post-2", "正文二", "正文二"),
            _detail("post-3", "正文三", "正文三"),
        ],
        synthesis_requested=True,
        llm=llm,
        model="test",
    )

    assert interaction is not None
    synthesis = interaction["synthesis"]
    assert synthesis["sources"][0]["read_status"] == "METADATA_ONLY"
    assert synthesis["read_count"] == 2
    assert synthesis["common_patterns"][0]["source_refs"] == ["source-2", "source-3"]


def test_synthesis_message_prefers_evidence_note_over_counts_when_no_conclusion() -> None:
    from greenbook_agent_api.services.retrieval_synthesis_projection import _interaction_message

    interaction = {
        "kind": "SYNTHESIS_RESULT",
        "synthesis": {
            "intro": "找到 31 篇相关内容，成功读取 1 篇。",
            "evidence_note": "目前取得的完整内容不足以可靠归纳共同点。",
            "conclusion": "",
        },
    }
    msg = _interaction_message(interaction)
    # Must surface the insufficiency, not the misleading count string.
    assert "不足以" in msg
    assert "找到 31" not in msg


def test_synthesis_message_prefers_conclusion_when_present() -> None:
    from greenbook_agent_api.services.retrieval_synthesis_projection import _interaction_message

    interaction = {
        "kind": "SYNTHESIS_RESULT",
        "synthesis": {
            "intro": "找到 5 篇，读取 4 篇。",
            "evidence_note": "部分正文未能读取。",
            "conclusion": "这些内容中反复出现的方法主要有：1. 分步拆解 2. 示例驱动 3. 持续练习",
        },
    }
    msg = _interaction_message(interaction)
    assert "分步拆解" in msg
    assert "找到 5" not in msg
