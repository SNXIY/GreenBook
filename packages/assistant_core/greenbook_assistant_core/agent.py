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
import uuid
from datetime import datetime, timezone
from typing import Any

from greenbook_assistant_core.context import SessionContext

logger = logging.getLogger(__name__)

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
        now_str = datetime.now(timezone.utc).isoformat()
        context_lines = [
            self.system_prompt,
            PRODUCT_DEFAULTS,
            "",
            f"## 当前会话上下文",
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
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        tool_rounds = 0
        final_content = ""

        while tool_rounds < self.max_tool_rounds:
            resp = await self.llm.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tools_schema if self.tools_schema else None,
                temperature=0.3,
            )

            choice = resp.choices[0]
            msg = choice.message

            # Accumulate content delta
            if msg.content:
                final_content = msg.content
                messages.append({"role": "assistant", "content": msg.content})
                if on_assistant_delta:
                    await on_assistant_delta(msg.content)

            if msg.tool_calls:
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

                    try:
                        result = await tool_handler(tool_name, tool_args, session, rid, tc.id)
                    except Exception as exc:
                        logger.exception("Tool handler error: %s", tool_name)
                        result = {
                            "ok": False,
                            "code": "INTERNAL_ERROR",
                            "message": str(exc),
                            "user_message": "An error occurred processing your request.",
                            "retryable": True,
                            "request_sent": False,
                            "trace_id": tid,
                        }

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
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tc],
                    })
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
                                session.active_schedule_id = str(data["schedule_id"])
                                session.record_entity(
                                    ref=f"schedule:{data['schedule_id']}", kind="SCHEDULE",
                                    entity_id=str(data["schedule_id"]),
                                    label=f"Schedule", status=data.get("status", "SCHEDULED"),
                                    run_id=rid,
                                )
                            if data.get("post_id"):
                                session.active_post_id = str(data["post_id"])
                                session.record_entity(
                                    ref=f"post:{data['post_id']}", kind="POST",
                                    entity_id=str(data["post_id"]),
                                    label=data.get("title"), status="PUBLISHED", run_id=rid,
                                )
                continue

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
