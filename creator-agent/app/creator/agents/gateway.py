from __future__ import annotations

import asyncio
import json
import logging
import time

from pydantic import ValidationError

from app.creator.model_client import CreatorMessage, CreatorModelClient
from app.creator.observability import creator_span, set_span_attributes
from app.creator.runtime.ports import (
    CreatorModelGateway,
    CreatorModelRequest,
    OutputModelT,
)


logger = logging.getLogger("uvicorn.error")


class CreatorModelGatewayError(RuntimeError):
    """Raised when the configured real model cannot produce a valid result."""


class AiClientCreatorModelGateway:
    """Structured-output adapter over a configured real model provider."""

    def __init__(self, client: CreatorModelClient):
        self._client = client

    async def complete_structured(
        self,
        request: CreatorModelRequest,
        output_type: type[OutputModelT],
    ) -> tuple[OutputModelT, int, int]:
        schema = json.dumps(
            output_type.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        messages = [
            CreatorMessage(
                role="system",
                content=(
                    f"{request.system_prompt}\n"
                    "Return exactly one JSON object. Do not use markdown fences.\n"
                    f"Required JSON Schema: {schema}"
                ),
            ),
            CreatorMessage(role="user", content=request.user_prompt),
        ]
        started = time.monotonic()
        try:
            logger.info(
                "Creator model request started operation=%s model=%s max_tokens=%s timeout=%s",
                request.operation,
                request.model or "provider-default",
                request.max_output_tokens,
                getattr(self._client._settings, "creator_model_timeout_seconds", None),
            )
            with creator_span(
                f"gen_ai.{request.operation}",
                attributes={
                    "gen_ai.operation.name": request.operation,
                    "gen_ai.request.model": request.model or "provider-default",
                    "gen_ai.request.max_tokens": request.max_output_tokens,
                    "gen_ai.request.temperature": request.temperature,
                },
            ) as span:
                raw = await asyncio.to_thread(
                    self._client.complete,
                    messages,
                    model=request.model,
                    temperature=request.temperature,
                    max_tokens=request.max_output_tokens,
                )
                parsed = _parse_json_object(raw)
                result = output_type.model_validate(parsed)
                logger.info(
                    "Creator model request finished operation=%s elapsed_seconds=%.2f",
                    request.operation,
                    time.monotonic() - started,
                )
                input_tokens = _estimate_tokens(request.user_prompt)
                output_tokens = _estimate_tokens(raw)
                set_span_attributes(
                    span,
                    {
                        "gen_ai.usage.input_tokens": input_tokens,
                        "gen_ai.usage.output_tokens": output_tokens,
                    },
                )
        except (ValueError, TypeError, ValidationError) as exc:
            raise CreatorModelGatewayError(
                f"Model returned invalid structured output for {request.operation}"
            ) from exc
        except Exception:
            logger.exception(
                "Creator model request failed operation=%s elapsed_seconds=%.2f",
                request.operation,
                time.monotonic() - started,
            )
            raise
        return result, input_tokens, output_tokens


class RoutedCreatorModelGateway:
    """Selects a provider-local model by operation without changing agent code."""

    def __init__(
        self,
        delegate: CreatorModelGateway,
        *,
        analysis_model: str = "",
        writer_model: str = "",
        critic_model: str = "",
        assist_model: str = "",
    ) -> None:
        self._delegate = delegate
        self._models = {
            "analysis": analysis_model.strip(),
            "writer": writer_model.strip(),
            "critic": critic_model.strip(),
            "assist": assist_model.strip(),
        }

    async def complete_structured(
        self,
        request: CreatorModelRequest,
        output_type: type[OutputModelT],
    ) -> tuple[OutputModelT, int, int]:
        model = self._models.get(_operation_route(request.operation), "")
        routed = request.model_copy(update={"model": model}) if model else request
        return await self._delegate.complete_structured(routed, output_type)


def _parse_json_object(raw: str) -> dict[str, object]:
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start < 0:
            raise
        parsed, _ = json.JSONDecoder().raw_decode(stripped[start:])
    if not isinstance(parsed, dict):
        raise TypeError("Structured model response must be a JSON object")
    return parsed


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _operation_route(operation: str) -> str:
    if operation.startswith("writer."):
        return "writer"
    if operation.startswith(("critic.", "evaluation.")):
        return "critic"
    if operation.startswith("editor."):
        return "assist"
    return "analysis"
