"""CommunityOperationsAssistant — single unified agent with tool-calling loop.

Does NOT use:
  - Router → TaskManager → GoalResolver → TargetResolver
  - Planner → PlanCompiler → AgentRegistry
  - Multiple business agents re-judging user intent

The LLM understands and plans once. Deterministic code handles schema,
auth, idempotency, approval, timeout, retry, and result validation.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from greenbook_assistant_core.context import SessionContext

logger = logging.getLogger(__name__)

_CREATE_MARKERS = (
    "草稿", "保存", "存为", "写一篇", "写个", "创作一篇", "创建一篇",
    "发一篇", "发布一篇",
)
_SCHEDULE_MARKERS = ("定时", "安排", "后发布", "发布任务")
_NUMBER_RE_FOR_TIME = r"[零〇一二两三四五六七八九十百\d]+"


def _has_future_time_expression(text: str) -> bool:
    if re.search(
        rf"{_NUMBER_RE_FOR_TIME}\s*"
        rf"(?:\u5206\u949f|\u5c0f\u65f6|\u5929)\s*"
        rf"(?:\u4e4b\u540e|\u540e)",
        text,
    ):
        return True
    return bool(
        re.search(
            rf"明天|后天|今天(?:上午|早上|下午|晚上|今晚)|下周|"
            rf"{_NUMBER_RE_FOR_TIME}\s*(?:分钟|分|小时|个小时|天)\s*后|"
            rf"20\d{{2}}[-年/]\d{{1,2}}[-月/]\d{{1,2}}日?",
            text,
        )
    )


def _turn_intents(user_message: str) -> tuple[bool, bool, bool, bool, bool]:
    text = user_message.strip().lower()
    asks_create = any(word in text for word in _CREATE_MARKERS)
    asks_revise = any(word in text for word in ("修改", "改成", "改得", "润色", "重写"))
    asks_schedule = any(word in text for word in _SCHEDULE_MARKERS) or (
        "发布" in text and _has_future_time_expression(text)
    )
    asks_cancel = any(word in text for word in ("取消", "撤销"))
    asks_search = any(word in text for word in ("搜索", "查找", "检索")) and any(
        word in text for word in ("社区", "帖子", "文章")
    )
    return asks_create, asks_revise, asks_schedule, asks_cancel, asks_search

# Product default context injected into every system prompt
PRODUCT_DEFAULTS = """## GreenBook产品默认语义

- "社区"默认指GreenBook站内公共社区
- "热门帖子"默认调用Java公共搜索
- "我的帖子"只查询当前用户
- "发布"默认发布到GreenBook当前账号
- "刚才那篇"优先绑定当前会话最近成功操作的Draft
- "刚才的任务"优先绑定active_schedule_id
- 相对时间使用用户timezone
- 未明确要求全网搜索时，不调用Web Search
- 未明确提及外部平台时，不询问发布平台
- 搜索结果是创作输入，不是搜索完成后直接结束
- 只有缺少真正必要的业务参数时才澄清"""


def _turn_routing_hint(
    user_message: str,
    session: SessionContext | None = None,
) -> str | None:
    """Give obvious business intents a short, internal tool-routing hint.

    Thinking models only support ``tool_choice=auto``.  For explicit product
    commands, this keeps auto tool selection deterministic without exposing a
    routing implementation detail to the user or disabling thinking mode.
    """
    asks_create, asks_revise, asks_schedule, asks_cancel, asks_search = _turn_intents(user_message)

    if asks_create and asks_schedule:
        return (
            "INTERNAL TURN ROUTING: This is a create-and-schedule request. "
            "Call content_create_draft first, then publication_schedule using its active draft."
        )
    if asks_cancel:
        return "INTERNAL TURN ROUTING: Call publication_cancel_schedule for this cancellation request."
    if asks_schedule and asks_revise:
        return (
            "INTERNAL TURN ROUTING: This is a two-action request. "
            "Call content_revise_draft first for the bound draft, then "
            "publication_update_schedule for the same bound schedule. "
            "Do not create a new draft or schedule."
        )
    if asks_create and asks_revise:
        return "INTERNAL TURN ROUTING: Call content_revise_draft for this draft operation."
    if asks_create:
        return "INTERNAL TURN ROUTING: Call content_create_draft directly for this draft operation. Do not search first unless the user explicitly asks for search references."
    if asks_revise:
        return "INTERNAL TURN ROUTING: Call content_revise_draft directly for this draft operation."
    if asks_schedule:
        return "INTERNAL TURN ROUTING: Call publication_schedule for this scheduling request."
    if asks_search:
        return "INTERNAL TURN ROUTING: Call community_search_public_posts before answering this community search request."
    return None


def _turn_tool_filter(
    user_message: str,
    session: SessionContext | None = None,
) -> set[str] | None:
    """Limit auto tool selection for explicit business commands.

    Thinking models support ``tool_choice=auto`` but can still choose a
    semantically adjacent tool when every tool is offered.  For an explicit
    command, expose only the operation that can satisfy the current turn.
    The next turn receives a fresh filter, so a create-and-schedule request
    can create first and schedule from the session's active draft afterward.
    """
    asks_create, asks_revise, asks_schedule, asks_cancel, asks_search = _turn_intents(user_message)

    if asks_cancel:
        return {"publication_cancel_schedule"}
    if asks_schedule and asks_revise:
        return {"content_revise_draft"}
    if asks_create:
        return {"content_create_draft"}
    if asks_revise:
        return {"content_revise_draft"}
    if asks_schedule:
        return {"publication_schedule"}
    if asks_search:
        return {"community_search_public_posts"}
    return None


class CommunityOperationsAssistant:
    """Lightweight tool-calling assistant for GreenBook community operations.

    Flow:
      User Message → AuthContext → SessionContext → Product Defaults
      → LLM (direct answer or tool call) → Pydantic validation
      → MCP Tool execution → Structured Observation → Continue or final response
    """

    MAX_TOOL_ROUNDS = 30

    def __init__(
        self,
        *,
        llm: Any,
        model: str,
        tools_schema: list[dict[str, Any]],
        system_prompt: str = "",
        max_tool_rounds: int = 30,
    ) -> None:
        self.llm = llm
        self.model = model
        self.tools_schema = tools_schema
        self.system_prompt = system_prompt
        self.max_tool_rounds = max_tool_rounds

    def _build_system_prompt(self, session: SessionContext) -> str:
        tz = session.timezone or "Asia/Shanghai"
        now_str = datetime.now(UTC).isoformat()
        context_lines = [
            self.system_prompt,
            "",
            "## 当前会话上下文",
            f"- 会话ID: {session.conversation_id}",
            f"- 当前时间: {now_str}",
            f"- 用户时区: {tz}",
        ]
        if session.active_draft_id:
            context_lines.append(f"- 当前活跃草稿: {session.active_draft_id}")
        if session.active_schedule_id:
            context_lines.append(f"- 当前活跃定时任务: {session.active_schedule_id}")
        if session.active_post_id:
            context_lines.append(f"- 当前上下文帖子: {session.active_post_id}")

        return "\n".join(context_lines)

    async def run(
        self,
        user_message: str,
        session: SessionContext,
        *,
        tool_handler: Any,
        conversation_history: list[dict[str, Any]] | None = None,
        trace_id: str | None = None,
        run_id: str | None = None,
        on_tool_start: Any = None,
        on_tool_complete: Any = None,
        on_assistant_delta: Any = None,
    ) -> dict[str, Any]:
        tid = trace_id or str(uuid.uuid4())
        rid = run_id or str(uuid.uuid4())

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._build_system_prompt(session)},
        ]
        routing_hint = _turn_routing_hint(user_message, session)
        if routing_hint:
            messages.append({"role": "system", "content": routing_hint})
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        allowed_tool_names = _turn_tool_filter(user_message, session)
        asks_create, asks_revise, asks_schedule, _, _ = _turn_intents(user_message)
        create_then_schedule = (
            allowed_tool_names == {"content_create_draft"}
            and asks_create
            and asks_schedule
        )
        revise_then_schedule = (
            allowed_tool_names == {"content_revise_draft"}
            and asks_revise
            and asks_schedule
        )
        turn_tools = self.tools_schema
        if allowed_tool_names is not None:
            turn_tools = [
                schema for schema in self.tools_schema
                if schema.get("function", {}).get("name") in allowed_tool_names
            ]

        tool_rounds = 0
        final_content = ""
        failed_tool_calls: set[str] = set()

        while tool_rounds < self.max_tool_rounds:
            resp = await self.llm.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=turn_tools if turn_tools else None,
                tool_choice="auto" if turn_tools else None,
                temperature=0.0,
            )

            choice = resp.choices[0]
            msg = choice.message
            logger.info(
                "llm_response trace_id=%s run_id=%s finish_reason=%s tool_call_count=%s reasoning_present=%s",
                tid,
                rid,
                getattr(choice, "finish_reason", None),
                len(msg.tool_calls or []),
                bool(getattr(msg, "reasoning_content", None)),
            )

            if msg.tool_calls:
                # DeepSeek thinking models require this complete assistant
                # message, including reasoning_content, on the next request.
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
                if hasattr(msg, "reasoning_content"):
                    reasoning_content = msg.reasoning_content
                    if reasoning_content is not None:
                        assistant_msg["reasoning_content"] = reasoning_content
                messages.append(assistant_msg)

                for tc in msg.tool_calls:
                    tool_rounds += 1
                    tool_name = tc.function.name
                    try:
                        tool_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}

                    logger.info("tool_call trace_id=%s run_id=%s tool=%s", tid, rid, tool_name)

                    if on_tool_start:
                        await on_tool_start(tool_name, tc.id, tool_args)

                    call_key = json.dumps(
                        [tool_name, tool_args], ensure_ascii=False, sort_keys=True, default=str
                    )
                    if call_key in failed_tool_calls:
                        result = {
                            "ok": False,
                            "code": "TOOL_EXECUTION_FAILED",
                            "message": "The same tool call already failed",
                            "user_message": "该工具调用已失败，不能重复执行。",
                            "retryable": False,
                            "request_sent": False,
                            "trace_id": tid,
                        }
                    else:
                        try:
                            result = await tool_handler(tool_name, tool_args, session, rid, tc.id)
                        except Exception:
                            logger.exception("Tool handler error tool=%s", tool_name)
                            result = {
                                "ok": False,
                                "code": "TOOL_EXECUTION_FAILED",
                                "message": "Tool handler raised an exception",
                                "user_message": "工具执行失败，请稍后重试。",
                                "retryable": False,
                                "request_sent": False,
                                "trace_id": tid,
                            }
                    if not result.get("ok"):
                        failed_tool_calls.add(call_key)
                        # A failed first action must not allow the model to
                        # continue into the next side-effect action.
                        turn_tools = []

                    if on_tool_complete:
                        await on_tool_complete(tool_name, tc.id, result)

                    # Build observation — strip sensitive data
                    observation = {
                        "tool_name": tool_name,
                        "ok": result.get("ok", False),
                        "code": result.get("code", "UNKNOWN"),
                        "user_message": result.get("user_message", ""),
                        "data": result.get("data"),
                        "receipt_id": result.get("receipt_id"),
                    }

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(observation, ensure_ascii=False, default=str),
                    })

                    # Record in session
                    session.record_tool_call(tool_name, tc.id, rid, "SUCCESS" if result.get("ok") else "FAILED")

                    # Update session state from successful tool results
                    if result.get("ok"):
                        data = result.get("data", {})
                        if isinstance(data, dict):
                            if data.get("draft_id"):
                                session.active_draft_id = str(data["draft_id"])
                                session.record_entity(
                                    ref=f"draft:{data['draft_id']}", kind="DRAFT",
                                    entity_id=str(data["draft_id"]),
                                    label=data.get("title"), status="READY", run_id=rid,
                                )
                            if data.get("schedule_id"):
                                schedule_status = str(data.get("status", "SCHEDULED"))
                                if schedule_status == "CANCELLED":
                                    session.active_schedule_id = None
                                else:
                                    session.active_schedule_id = str(data["schedule_id"])
                                session.record_entity(
                                    ref=f"schedule:{data['schedule_id']}", kind="SCHEDULE",
                                    entity_id=str(data["schedule_id"]),
                                    label="Schedule", status=schedule_status,
                                    run_id=rid,
                                )
                            if data.get("post_id"):
                                session.active_post_id = str(data["post_id"])
                                session.record_entity(
                                    ref=f"post:{data['post_id']}", kind="POST",
                                    entity_id=str(data["post_id"]),
                                    label=data.get("title"), status="PUBLISHED", run_id=rid,
                                )

                        # A create-and-schedule request is two ordered model
                        # turns. The schedule tool is exposed only after the
                        # draft side effect succeeds; after a successful
                        # write, no duplicate write tool is exposed.
                        if tool_name == "content_create_draft":
                            if create_then_schedule and session.active_draft_id:
                                turn_tools = [
                                    schema for schema in self.tools_schema
                                    if schema.get("function", {}).get("name")
                                    == "publication_schedule"
                                ]
                            else:
                                turn_tools = []
                        elif tool_name == "content_revise_draft":
                            if revise_then_schedule:
                                turn_tools = [
                                    schema for schema in self.tools_schema
                                    if schema.get("function", {}).get("name")
                                    == "publication_update_schedule"
                                ]
                            else:
                                turn_tools = []
                        elif tool_name in {
                            "publication_schedule",
                            "publication_update_schedule",
                        }:
                            turn_tools = []
                continue

            # A final assistant message is stored exactly once.  Reasoning is
            # intentionally not copied into the user-visible result/history.
            final_content = msg.content or ""
            if final_content and on_assistant_delta:
                await on_assistant_delta(final_content)
            break

        return {
            "run_id": rid,
            "trace_id": tid,
            "content": final_content,
            "tool_rounds": tool_rounds,
            "session_snapshot": {
                "conversation_id": session.conversation_id,
                "active_draft_id": session.active_draft_id,
                "active_post_id": session.active_post_id,
                "active_schedule_id": session.active_schedule_id,
            },
        }
