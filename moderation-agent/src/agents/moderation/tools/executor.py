import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolNode

from agents.moderation.nodes.dependencies import ModerationDependencies
from agents.moderation.state import ModerationState
from agents.moderation.tools.factory import build_moderation_tools, moderation_tools_by_name
from community.tools import CommunityEvidenceReader
from moderation.schemas import ModerationToolName, RiskType, ToolErrorCode
from moderation.security import redact_data

logger = logging.getLogger(__name__)

_ALL_TOOL_NAMES = {
    "get_parent_comment",
    "get_conversation_context",
    "get_author_recent_contents",
    "get_author_violation_history",
    "get_content_reports",
    "search_platform_policies",
    "search_similar_review_cases",
    "explain_obfuscated_expression",
    "detect_contact_information",
}
_ALLOWED_TOOLS = {
    RiskType.NORMAL: {
        "get_parent_comment",
        "get_conversation_context",
        "search_platform_policies",
        "explain_obfuscated_expression",
        "detect_contact_information",
    },
    RiskType.ADVERTISING: _ALL_TOOL_NAMES,
    RiskType.ABUSE: {
        "get_parent_comment",
        "get_conversation_context",
        "get_author_violation_history",
        "get_content_reports",
        "search_platform_policies",
        "search_similar_review_cases",
        "explain_obfuscated_expression",
    },
    RiskType.PRIVACY: {
        "get_parent_comment",
        "get_conversation_context",
        "get_content_reports",
        "search_platform_policies",
        "search_similar_review_cases",
        "explain_obfuscated_expression",
        "detect_contact_information",
    },
}
_LIMITS = {
    "get_conversation_context": 10,
    "get_author_recent_contents": 10,
    "search_platform_policies": 5,
    "search_similar_review_cases": 3,
}


def allowed_tool_names_for_risk(risk_type: RiskType) -> frozenset[str]:
    """Return the model-visible tool allowlist for one classified risk."""
    return frozenset(_ALLOWED_TOOLS[risk_type])


@dataclass(frozen=True, slots=True)
class _PreparedCall:
    name: ModerationToolName
    args: dict[str, Any]
    call_id: str
    signature: str

    def as_tool_call(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "args": self.args,
            "id": self.call_id,
            "type": "tool_call",
        }


class ModerationToolExecutionNode:
    def __init__(self, dependencies: ModerationDependencies) -> None:
        self.dependencies = dependencies
    # __call__ 负责编排（校验 + 构建 + 准备 + 汇总），
    # _execute 只负责逐个执行工具函数。拆开是为了让每个方法职责单一。
    async def __call__(
        self,
        state: ModerationState,
        config: RunnableConfig,
    ) -> ModerationState:
        last_message = state.get("messages", [])[-1]
        if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
            return {
                "tool_agent_error": "moderation_tools:MissingToolCalls",
                "evidence_collection_complete": False,
            }

        reader = self.dependencies.context_loader
        if not isinstance(reader, CommunityEvidenceReader):
            return {
                "tool_agent_error": "moderation_tools:CommunityEvidenceReaderUnavailable",
                "evidence_collection_complete": False,
            }

        tools = build_moderation_tools(
            community_reader=reader,
            policy_retriever=self.dependencies.policy_retriever,
            case_retriever=self.dependencies.case_retriever,
            platform=state.get("platform", "default"),
            config=self.dependencies.tool_calling_config,
        )
        tool_map = moderation_tools_by_name(tools)
        cache = dict(state.get("tool_call_cache", {}))
        records = list(state.get("tool_results", []))
        called_tools = list(state.get("called_tools", []))
        failed_tools = list(state.get("failed_tools", []))
        outputs: dict[str, ToolMessage] = {}
        prepared: list[_PreparedCall] = []
        duplicates: dict[str, list[_PreparedCall]] = {}
        seen_signatures: set[str] = set()
        cache_hits = state.get("tool_cache_hits", 0)
        budget_exceeded = state.get("tool_budget_exceeded", False)
        remaining = max(
            0,
            self.dependencies.tool_calling_config.max_total_calls - state.get("tool_call_count", 0),
        )

        for raw_call in last_message.tool_calls:
            name = str(raw_call["name"])
            call_id = str(raw_call["id"])
            args = _normalize_arguments(name, dict(raw_call["args"]))
            error = _validate_call(name, args, state)
            if error is not None:
                outputs[call_id] = _error_message(name, call_id, *error)
                records.append(_record(name, outputs[call_id], state, cache_hit=False))
                _append_unique(failed_tools, name)
                continue

            typed_name = cast(ModerationToolName, name)
            signature = _signature(typed_name, args)
            call = _PreparedCall(typed_name, args, call_id, signature)
            _append_unique(called_tools, name)
            cached = cache.get(signature)
            if cached is not None:
                outputs[call_id] = _cached_message(call, cached)
                records.append(_record(name, outputs[call_id], state, cache_hit=True))
                cache_hits += 1
                continue
            if signature in seen_signatures:
                duplicates.setdefault(signature, []).append(call)
                continue
            if len(prepared) >= self.dependencies.tool_calling_config.max_parallel_calls:
                outputs[call_id] = _error_message(
                    name,
                    call_id,
                    "PARALLEL_LIMIT",
                    "The per-round parallel tool limit was reached.",
                    True,
                )
                records.append(_record(name, outputs[call_id], state, cache_hit=False))
                _append_unique(failed_tools, name)
                continue
            if len(prepared) >= remaining:
                outputs[call_id] = _error_message(
                    name,
                    call_id,
                    "BUDGET_EXCEEDED",
                    "The total tool-call budget was reached.",
                    False,
                )
                records.append(_record(name, outputs[call_id], state, cache_hit=False))
                _append_unique(failed_tools, name)
                budget_exceeded = True
                continue
            prepared.append(call)
            seen_signatures.add(signature)

        if prepared:
            executed = await self._execute(prepared, tool_map, state, config)
            for call in prepared:
                message = executed[call.call_id]
                outputs[call.call_id] = message
                record = _record(call.name, message, state, cache_hit=False)
                records.append(record)
                result = record["result"]
                if result.get("success"):
                    cache[call.signature] = {
                        "tool_name": call.name,
                        "content": message.content,
                        "success": True,
                    }
                else:
                    _append_unique(failed_tools, call.name)
                for duplicate in duplicates.get(call.signature, []):
                    duplicate_message = ToolMessage(
                        content=message.content,
                        tool_call_id=duplicate.call_id,
                        name=duplicate.name,
                        status=message.status,
                    )
                    outputs[duplicate.call_id] = duplicate_message
                    records.append(
                        _record(duplicate.name, duplicate_message, state, cache_hit=True)
                    )
                    cache_hits += 1

        ordered_messages: list[AnyMessage] = [
            outputs[str(call["id"])]
            for call in last_message.tool_calls
            if str(call["id"]) in outputs
        ]
        return {
            "messages": ordered_messages,
            "tool_results": records,
            "called_tools": called_tools,
            "failed_tools": failed_tools,
            "tool_call_cache": cache,
            "tool_call_count": state.get("tool_call_count", 0) + len(prepared),
            "tool_cache_hits": cache_hits,
            "tool_budget_exceeded": budget_exceeded,
        }

    async def _execute(
        self,
        prepared: list[_PreparedCall],
        tool_map: dict[str, Any],
        state: ModerationState,
        config: RunnableConfig,
    ) -> dict[str, ToolMessage]:
        message = AIMessage(
            content="",
            tool_calls=[call.as_tool_call() for call in prepared],
        )
        tool_node = ToolNode(
            list(tool_map.values()),
            handle_tool_errors=True,
            messages_key="messages",
        )
        try:
            result = await tool_node.ainvoke(
                {"messages": [message]},
                _execution_config(config, state, prepared),
            )
            return {item.tool_call_id: item for item in result["messages"]}
        except Exception:
            logger.exception("Moderation ToolNode execution failed")
            return {
                call.call_id: _error_message(
                    call.name,
                    call.call_id,
                    "INTERNAL_ERROR",
                    "Tool execution failed.",
                    False,
                )
                for call in prepared
            }


def _normalize_arguments(name: str, args: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(args)
    limit_max = _LIMITS.get(name)
    if limit_max is not None and "limit" in normalized:
        try:
            normalized["limit"] = max(1, min(int(normalized["limit"]), limit_max))
        except (TypeError, ValueError):
            pass
    return normalized


def _validate_call(
    name: str,
    args: dict[str, Any],
    state: ModerationState,
) -> tuple[ToolErrorCode, str, bool] | None:
    if name not in _ALL_TOOL_NAMES:
        return "TOOL_NOT_ALLOWED", "Unknown moderation tool.", False
    risk_type = RiskType(state["classification"]["risk_type"])
    if name not in allowed_tool_names_for_risk(risk_type):
        return "TOOL_NOT_ALLOWED", "The tool is not allowed for this risk type.", False

    content_id = state.get("content_id")
    author_id = state.get("creator_id")
    if name == "get_parent_comment" and args.get("comment_id") != content_id:
        return "TOOL_NOT_ALLOWED", "Only the current comment may be queried.", False
    if name in {"get_conversation_context", "get_content_reports"}:
        if args.get("content_id") != content_id:
            return "TOOL_NOT_ALLOWED", "Only the current content may be queried.", False
    if name in {"get_author_recent_contents", "get_author_violation_history"}:
        if not author_id or args.get("author_id") != author_id:
            return "TOOL_NOT_ALLOWED", "Only the current author may be queried.", False
    if name in {"get_parent_comment", "get_conversation_context"}:
        if state.get("content_type") != "COMMENT" or not content_id:
            return "TOOL_NOT_ALLOWED", "Comment context is unavailable for this item.", False

    current_content = state.get("normalized_content", "")
    if name in {"detect_contact_information", "search_similar_review_cases"}:
        argument_name = "content"
        if args.get(argument_name) != current_content:
            return "TOOL_NOT_ALLOWED", "Only the current content may be inspected.", False
    if name == "explain_obfuscated_expression":
        expression = args.get("expression")
        if not isinstance(expression, str) or not _known_text(expression, state):
            return "TOOL_NOT_ALLOWED", "The expression must come from collected evidence.", False
        context = args.get("context")
        if context is not None and (
            not isinstance(context, str) or not _known_text(context, state)
        ):
            return "TOOL_NOT_ALLOWED", "The context must come from collected evidence.", False
    return None


def _known_text(value: str, state: ModerationState) -> bool:
    if value in state.get("normalized_content", ""):
        return True
    evidence = json.dumps(state.get("tool_results", []), ensure_ascii=False)
    return bool(value and value in evidence)


def _signature(name: ModerationToolName, args: dict[str, Any]) -> str:
    normalized = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{name}:{normalized}".encode()).hexdigest()


def _error_message(
    name: str,
    call_id: str,
    code: ToolErrorCode,
    message: str,
    retryable: bool,
) -> ToolMessage:
    content = json.dumps(
        {
            "success": False,
            "tool_name": name,
            "data": None,
            "error_code": code,
            "error_message": message,
            "is_partial": False,
            "retryable": retryable,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return ToolMessage(
        content=content,
        tool_call_id=call_id,
        name=name,
        status="error",
    )


def _cached_message(call: _PreparedCall, cached: dict[str, Any]) -> ToolMessage:
    return ToolMessage(
        content=cached["content"],
        tool_call_id=call.call_id,
        name=call.name,
        status="success" if cached.get("success") else "error",
    )


def _record(
    name: str,
    message: ToolMessage,
    state: ModerationState,
    *,
    cache_hit: bool,
) -> dict[str, Any]:
    try:
        result = json.loads(str(message.content))
    except (TypeError, ValueError, json.JSONDecodeError):
        result = {
            "success": False,
            "tool_name": name,
            "data": None,
            "error_code": "INTERNAL_ERROR",
            "error_message": "Tool returned an invalid structured result.",
            "is_partial": False,
            "retryable": False,
        }
    return {
        "tool_name": name,
        "success": bool(result.get("success")),
        "cache_hit": cache_hit,
        "round": state.get("tool_call_round", 1),
        "error_code": result.get("error_code"),
        "is_partial": bool(result.get("is_partial")),
        "result": redact_data(result),
    }


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _execution_config(
    config: RunnableConfig,
    state: ModerationState,
    prepared: list[_PreparedCall],
) -> RunnableConfig:
    call_config = config.copy()
    call_config.pop("run_id", None)
    call_config["run_name"] = "moderation_tool_execution"
    call_config["tags"] = list(
        dict.fromkeys(
            [
                *config.get("tags", []),
                "moderation",
                "tool_calling",
                "moderation_tool_execution",
            ]
        )
    )
    metadata = dict(config.get("metadata", {}))
    metadata.update(
        {
            "moderation_task_id": state.get("task_id"),
            "initial_risk_type": state.get("classification", {}).get("risk_type"),
            "selected_tools": list(dict.fromkeys(call.name for call in prepared)),
            "tool_call_round": state.get("tool_call_round", 0),
            "total_tool_calls": state.get("tool_call_count", 0) + len(prepared),
            "cache_hits": state.get("tool_cache_hits", 0),
        }
    )
    call_config["metadata"] = metadata
    return call_config
