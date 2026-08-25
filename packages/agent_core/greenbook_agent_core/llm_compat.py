"""Provider compatibility helpers for structured LLM responses."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

# Reasoning-capable providers spend part of max_tokens on hidden reasoning
# before emitting the JSON object in content. A 2048- or 4096-token retry can
# therefore finish with empty content for a complex typed decision. Keep the
# retry bounded while leaving room for both reasoning and the validated
# response.
STRUCTURED_OUTPUT_RETRY_MAX_TOKENS = 8192


def provider_accepts_json_schema(client: Any, model: str = "") -> bool:
    """Return whether the provider supports ``response_format=json_schema``.

    DeepSeek rejects the strict JSON-Schema response format with a 400 on every
    request; callers should use ``json_object`` + an explicit schema instruction
    and let Python validate the contract instead.
    """

    base_url = str(getattr(client, "base_url", "")).lower()
    model_name = str(model or "").lower()
    return not ("deepseek" in base_url or model_name.startswith("deepseek"))


def structured_provider_options(client: Any, model: str = "") -> dict[str, Any]:
    """Return provider-specific options for typed routing calls.

    DeepSeek reasoning models can spend the whole structured-output budget in
    ``reasoning_content`` and leave ``content`` empty.  Routing boundaries
    need a short, schema-validated JSON envelope; the host LLM writes the
    long-form draft body separately and is not affected by this adapter.
    """

    base_url = str(getattr(client, "base_url", "")).lower()
    model_name = str(model or "").lower()
    if "deepseek" in base_url or model_name.startswith("deepseek"):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}


def extract_top_level_json(content: str) -> str:
    """Return the first top-level JSON object substring from model text.

    Safe normalization only: strips a markdown code fence and locates the first
    balanced ``{...}`` object, respecting strings and nesting.  It never infers
    a business action; if no balanced object is found the original text is
    returned so the caller's JSON decoder reports the real error.
    """

    text = str(content or "").strip()
    if not text:
        return text
    if text.startswith("```"):
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    start = text.find("{")
    if start == -1:
        return str(content or "")
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return str(content or "")


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


async def structured_call(
    client: Any,
    model: str,
    prompt: str,
    schema_name: str,
    schema: Mapping[str, Any],
    request: Mapping[str, Any] | str | None = None,
    *,
    retry: bool = False,
) -> Any:
    """One canonical typed LLM call with provider-compatibility fallbacks.

    Every intelligence boundary (AgentLoop, DynamicPlanner, ToolSelector,
    CommandInterpreter, GoalDecomposer) routes structured output through this
    single path so the provider adapters are not duplicated per caller:

    * DeepSeek rejects ``response_format=json_schema`` with a 400 on every
      call, so unsupported providers go straight to ``json_object`` with the
      exact schema embedded in the system message (Python still validates the
      contract after the response).
    * A provider that accepts the request but rejects the response-format
      variant falls back once to ``json_object``.
    * A reasoning-heavy provider can return empty ``content`` after a long
      trace; the call is retried once with a bounded output budget.

    ``retry=True`` forces the ``json_object`` path (used when a previous
    attempt already produced no structured payload).  The caller remains
    responsible for validating the returned payload against the typed schema.
    """

    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": (
                request
                if isinstance(request, str)
                else json.dumps(request, ensure_ascii=False, default=str)
            ),
        },
    ]
    provider_options = structured_provider_options(client, model)
    json_schema_unsupported = not provider_accepts_json_schema(client, model)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        **provider_options,
    }
    if retry or json_schema_unsupported:
        kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"] = add_json_schema_instruction(kwargs["messages"], schema)
        kwargs["max_tokens"] = STRUCTURED_OUTPUT_RETRY_MAX_TOKENS
    else:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": dict(schema)},
        }
    started_at = time.perf_counter()
    try:
        response = await client.chat.completions.create(**kwargs)
    except Exception as exc:
        if json_schema_unsupported or (
            "response_format" not in str(exc).lower()
            and "json_schema" not in str(exc).lower()
        ):
            raise
        kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"] = add_json_schema_instruction(kwargs["messages"], schema)
        kwargs["max_tokens"] = STRUCTURED_OUTPUT_RETRY_MAX_TOKENS
        response = await client.chat.completions.create(**kwargs)
    if not has_structured_payload(response):
        response = await retry_json_object(client, kwargs, schema)
    try:
        from .observability.run_metrics import record_llm
        record_llm(response, round((time.perf_counter() - started_at) * 1000))
    except Exception:
        pass
    return response


__all__ = [
    "add_json_schema_instruction",
    "extract_top_level_json",
    "has_structured_payload",
    "provider_accepts_json_schema",
    "retry_json_object",
    "STRUCTURED_OUTPUT_RETRY_MAX_TOKENS",
    "structured_call",
    "structured_provider_options",
]
