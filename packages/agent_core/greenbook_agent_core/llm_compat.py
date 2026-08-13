"""Provider compatibility helpers for structured LLM responses."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

# Reasoning-capable providers spend part of max_tokens on hidden reasoning
# before emitting the JSON object in content. A 2048- or 4096-token retry can
# therefore finish with empty content for a complex typed decision. Keep the
# retry bounded while leaving room for both reasoning and the validated
# response.
STRUCTURED_OUTPUT_RETRY_MAX_TOKENS = 8192


def structured_provider_options(client: Any, model: str = "") -> dict[str, Any]:
    """Return provider-specific options for typed routing calls.

    DeepSeek reasoning models can spend the whole structured-output budget in
    ``reasoning_content`` and leave ``content`` empty.  Routing boundaries
    need a short, schema-validated JSON envelope; Creator remains responsible
    for long-form reasoning and is not affected by this adapter.
    """

    base_url = str(getattr(client, "base_url", "")).lower()
    model_name = str(model or "").lower()
    if "deepseek" in base_url or model_name.startswith("deepseek"):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def add_json_schema_instruction(
    messages: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Include the contract when a provider only supports JSON mode.

    ``json_object`` validates that the response is JSON, but it does not
    enforce the Pydantic contract the ``json_schema`` response format would
    have enforced.  Supplying the exact schema in the system message keeps
    the provider fallback contract-aware; Python still performs final
    validation after the response arrives.
    """

    result = [dict(message) for message in messages]
    if not result:
        return result
    system_message = result[0]
    content = str(system_message.get("content") or "")
    schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    system_message["content"] = (
        f"{content}\n\n"
        "The provider does not enforce JSON Schema in this request. "
        "Return one JSON object that matches this schema exactly; do not add "
        "fields and do not change object fields into arrays:\n"
        f"{schema_json}"
    )
    return result


def has_structured_payload(response: Any) -> bool:
    """Return whether an OpenAI-compatible response contains output text."""

    try:
        message = response.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return False
    if getattr(message, "parsed", None) is not None:
        return True
    content = getattr(message, "content", None)
    if isinstance(content, Mapping):
        return bool(content)
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        return any(
            isinstance(item, Mapping) and str(item.get("text", "")).strip()
            for item in content
        )
    return False


async def retry_json_object(
    client: Any,
    request_kwargs: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> Any:
    """Retry one empty structured response using provider JSON mode."""

    retry_kwargs = dict(request_kwargs)
    retry_kwargs["response_format"] = {"type": "json_object"}
    messages = request_kwargs.get("messages", [])
    retry_kwargs["messages"] = add_json_schema_instruction(
        [dict(message) for message in messages],
        schema,
    )
    retry_kwargs["max_tokens"] = max(
        int(retry_kwargs.get("max_tokens") or 0),
        STRUCTURED_OUTPUT_RETRY_MAX_TOKENS,
    )
    retry_kwargs.update(
        structured_provider_options(
            client,
            str(retry_kwargs.get("model") or ""),
        )
    )
    return await client.chat.completions.create(**retry_kwargs)


__all__ = [
    "add_json_schema_instruction",
    "has_structured_payload",
    "retry_json_object",
    "STRUCTURED_OUTPUT_RETRY_MAX_TOKENS",
    "structured_provider_options",
]
