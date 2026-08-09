from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


_TRUNCATION_MARKER = "\n[内容已按模型上下文预算截断]"


def bounded_conversation(
    history: list[dict[str, str]],
    *,
    current_prompt: str,
    max_chars: int,
    max_messages: int = 12,
) -> list[dict[str, str]]:
    """Keep a legal recent conversation suffix within a deterministic budget."""
    candidates = [
        {"role": str(item.get("role") or ""), "content": _clean_text(item.get("content"))}
        for item in history
        if str(item.get("role") or "") in {"user", "assistant"}
    ]
    if (
        candidates
        and candidates[-1]["role"] == "user"
        and candidates[-1]["content"] == _clean_text(current_prompt)
    ):
        candidates.pop()
    kept: list[dict[str, str]] = []
    used = 0
    for item in reversed(candidates[-max_messages:]):
        content = _truncate(item["content"], min(6_000, max_chars))
        cost = len(content) + len(item["role"]) + 16
        if kept and used + cost > max_chars:
            break
        if not kept and cost > max_chars:
            content = _truncate(content, max(256, max_chars - 32))
            cost = len(content) + len(item["role"]) + 16
        kept.append({"role": item["role"], "content": content})
        used += cost
    kept.reverse()
    while kept and kept[0]["role"] == "assistant":
        kept.pop(0)
    return kept


def bounded_tool_outputs(
    outputs: list[dict[str, Any]], *, max_chars: int
) -> list[dict[str, Any]]:
    """Build a model-facing evidence view without mutating durable tool results."""
    bounded = _bound_value(deepcopy(outputs), string_limit=6_000)
    if _json_size(bounded) <= max_chars:
        return bounded
    compact: list[dict[str, Any]] = []
    remaining = max(0, max_chars - 2)
    for item in reversed(bounded):
        candidate = None
        for string_limit in (2_000, 1_000, 400, 120):
            reduced = _bound_value(item, string_limit=string_limit)
            if _json_size(reduced) <= remaining:
                candidate = reduced
                break
        if candidate is None:
            reduced = {
                "ordinal": item.get("ordinal"),
                "tool": _truncate(str(item.get("tool") or ""), 80),
                "label": _truncate(str(item.get("label") or ""), 120),
                "result": {"truncated": True},
            }
            if _json_size(reduced) <= remaining:
                candidate = reduced
        if candidate is None:
            continue
        size = _json_size(candidate)
        compact.append(candidate)
        remaining -= size + 1
        if remaining <= 256:
            break
    compact.reverse()
    return compact


def bounded_post(post: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    bounded = _bound_value(
        deepcopy(post), string_limit=max(64, min(4_000, max_chars // 4))
    )
    body_key = "bodyMarkdown" if "bodyMarkdown" in bounded else "body_markdown"
    if body_key in bounded:
        reserved = _json_size({key: value for key, value in bounded.items() if key != body_key})
        bounded[body_key] = _truncate(
            str(bounded.get(body_key) or ""),
            max(64, max_chars - reserved - 256),
        )
    for string_limit in (2_000, 1_000, 400, 120):
        if _json_size(bounded) <= max_chars:
            break
        bounded = _bound_value(bounded, string_limit=string_limit)
    return bounded


def _bound_value(value: Any, *, string_limit: int) -> Any:
    if isinstance(value, str):
        return _truncate(_clean_text(value), string_limit)
    if isinstance(value, list):
        return [_bound_value(item, string_limit=string_limit) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key): _bound_value(item, string_limit=string_limit)
            for key, item in value.items()
        }
    return value


def _clean_text(value: Any) -> str:
    return str(value or "").replace("\x00", " ").strip()


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = _TRUNCATION_MARKER
    return value[: max(0, limit - len(marker))].rstrip() + marker


def _json_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    )
