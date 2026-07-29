import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from moderation.schemas import (
    ModerationToolName,
    ToolCallingConfig,
    ToolErrorCode,
    ToolResult,
)
from moderation.security import redact_data, redact_text

DataT = TypeVar("DataT")
ResultT = TypeVar("ResultT", bound=ToolResult[Any])


class ModerationToolOperationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: ToolErrorCode = "INTERNAL_ERROR",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    config: ToolCallingConfig

    async def execute(
        self,
        *,
        tool_name: ModerationToolName,
        result_type: type[ResultT],
        operation: Callable[[], Awaitable[DataT]],
        build_result: Callable[[DataT], ResultT],
    ) -> ResultT:
        max_attempts = self.config.max_retries + 1
        for attempt in range(1, max_attempts + 1):
            try:
                data = await asyncio.wait_for(
                    operation(),
                    timeout=self.config.tool_timeout_seconds,
                )
                return build_result(data)
            except Exception as exc:
                code, retryable = classify_tool_error(exc)
                if retryable and attempt < max_attempts:
                    continue
                return result_type(
                    success=False,
                    tool_name=tool_name,
                    error_code=code,
                    error_message=safe_error_message(exc),
                    retryable=retryable,
                )
        raise AssertionError("unreachable tool execution state")

    def serialize(self, result: ToolResult[Any]) -> str:
        return serialize_tool_result(result, self.config.max_result_chars)

    def invalid_arguments(self, tool_name: ModerationToolName) -> str:
        return self.serialize(
            ToolResult(
                success=False,
                tool_name=tool_name,
                error_code="INVALID_ARGUMENT",
                error_message="Tool arguments failed validation.",
            )
        )


def classify_tool_error(exc: Exception) -> tuple[ToolErrorCode, bool]:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "TIMEOUT", True
    if isinstance(exc, ModerationToolOperationError):
        return exc.code, exc.retryable

    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 404:
        return "NOT_FOUND", False
    if status_code == 429 or isinstance(status_code, int) and status_code >= 500:
        return "RETRYABLE_ERROR", True
    if isinstance(exc, (ConnectionError, OSError)):
        return "UNAVAILABLE", True
    if isinstance(exc, (TypeError, ValueError)):
        return "INVALID_ARGUMENT", False
    return "INTERNAL_ERROR", False


def safe_error_message(exc: Exception) -> str:
    message = redact_text(str(exc)).strip()
    if not message:
        return type(exc).__name__
    return message[:500]


def serialize_tool_result(result: ToolResult[Any], max_chars: int) -> str:
    payload = redact_data(result.model_dump(mode="json"))
    serialized = _compact_json(payload)
    if len(serialized) <= max_chars:
        return serialized

    payload["is_partial"] = True
    if payload.get("error_code") is None:
        payload["error_code"] = "RESULT_TRUNCATED"
    if payload.get("error_message") is None:
        payload["error_message"] = "Tool result was truncated to the configured output limit."

    for text_limit in (1000, 500, 250, 120, 60):
        payload = _truncate_strings(payload, text_limit)
        serialized = _compact_json(payload)
        if len(serialized) <= max_chars:
            return serialized

        while _drop_one_list_item(payload):
            serialized = _compact_json(payload)
            if len(serialized) <= max_chars:
                return serialized

    payload["data"] = None
    serialized = _compact_json(payload)
    if len(serialized) <= max_chars:
        return serialized
    return _compact_json(
        {
            "success": payload["success"],
            "tool_name": payload["tool_name"],
            "data": None,
            "error_code": payload["error_code"],
            "error_message": "Tool result exceeded the configured output limit.",
            "is_partial": True,
            "retryable": payload["retryable"],
        }
    )


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _truncate_strings(value: Any, max_chars: int) -> Any:
    if isinstance(value, str):
        if len(value) <= max_chars:
            return value
        return f"{value[: max_chars - 3]}..."
    if isinstance(value, dict):
        return {key: _truncate_strings(item, max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_strings(item, max_chars) for item in value]
    return value


def _drop_one_list_item(value: Any) -> bool:
    lists: list[list[Any]] = []

    def collect(item: Any) -> None:
        if isinstance(item, list):
            if item:
                lists.append(item)
            for child in item:
                collect(child)
        elif isinstance(item, dict):
            for child in item.values():
                collect(child)

    collect(value.get("data") if isinstance(value, dict) else value)
    if not lists:
        return False
    max(lists, key=len).pop()
    return True
