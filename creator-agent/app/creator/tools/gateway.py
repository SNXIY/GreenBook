from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError

from app.creator.tools.errors import (
    CreatorToolAuditError,
    CreatorToolAuthorizationError,
    CreatorToolBudgetError,
    CreatorToolError,
    CreatorToolExecutionError,
    CreatorToolNotFoundError,
    CreatorToolResultTooLargeError,
    CreatorToolTimeoutError,
    CreatorToolValidationError,
)
from app.creator.tools.models import (
    CreatorToolCallAudit,
    CreatorToolCallContext,
    CreatorToolCallStatus,
    CreatorToolResult,
    CreatorToolRisk,
)
from app.creator.tools.ports import CreatorToolAuditStore, CreatorToolHandler

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CreatorToolDefinition:
    name: str
    description: str
    risk: CreatorToolRisk
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: CreatorToolHandler


_AGENT_ALLOWLISTS: dict[str, frozenset[str]] = {
    "MemoryAgent": frozenset({"get_creator_profile", "get_user_history"}),
    "ContentAnalyzerAgent": frozenset(
        {
            "get_user_history",
            "get_post_detail",
            "get_comments",
            "get_post_metrics",
            "get_engagement",
        }
    ),
    "ResearchAgent": frozenset({"search_posts", "get_post_detail", "get_comments"}),
    "StrategyAgent": frozenset(
        {
            "get_creator_profile",
            "get_user_history",
            "search_posts",
            "get_post_detail",
            "get_comments",
            "get_post_metrics",
            "get_engagement",
        }
    ),
    "WriterAgent": frozenset(
        {
            "get_creator_profile",
            "get_post_detail",
            "save_draft",
            "update_draft",
        }
    ),
    "CriticAgent": frozenset({"get_post_detail", "get_comments", "get_post_metrics"}),
    "EvaluationAgent": frozenset(),
    "CreatorSupervisorAgent": frozenset(),
}


class CreatorToolGateway:
    def __init__(
        self,
        definitions: tuple[CreatorToolDefinition, ...],
        *,
        audit_store: CreatorToolAuditStore,
        timeout_seconds: float = 10.0,
        max_result_bytes: int = 262_144,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Tool timeout must be positive")
        if max_result_bytes < 1_024:
            raise ValueError("Tool result limit must be at least 1024 bytes")
        self._definitions: dict[str, CreatorToolDefinition] = {}
        for definition in definitions:
            if definition.name in self._definitions:
                raise ValueError(f"Duplicate Creator tool {definition.name}")
            self._definitions[definition.name] = definition
        self._audit = audit_store
        self._timeout_seconds = timeout_seconds
        self._max_result_bytes = max_result_bytes
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    @property
    def definitions(self) -> tuple[CreatorToolDefinition, ...]:
        return tuple(self._definitions[name] for name in self.tool_names)

    async def call(
        self,
        name: str,
        arguments: dict,
        context: CreatorToolCallContext,
    ) -> CreatorToolResult:
        definition = self._definitions.get(name)
        if definition is None:
            raise CreatorToolNotFoundError(f"Unknown Creator tool {name}")
        self._authorize(definition, context)
        if context.remaining_call_budget <= 0:
            raise CreatorToolBudgetError("Tool call budget is exhausted")
        try:
            request = definition.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise CreatorToolValidationError(
                "Tool arguments did not match the registered schema",
                details={"validation_errors": exc.errors(include_input=False)},
            ) from exc

        call_id = self._id_factory()
        started_at = self._clock()
        started_ns = time.monotonic_ns()
        arguments_sha256 = _hash_json(request.model_dump(mode="json"))
        audit = CreatorToolCallAudit(
            call_id=call_id,
            trace_id=context.trace_id,
            task_id=context.task_id,
            run_id=context.run_id,
            tenant_id=context.principal.tenant_id,
            creator_id=context.principal.creator_id,
            actor_id=context.principal.actor_id,
            caller=context.principal.caller,
            tool_name=name,
            risk=definition.risk,
            arguments_sha256=arguments_sha256,
            status=CreatorToolCallStatus.RUNNING,
            started_at=started_at,
        )
        try:
            await self._audit.start(audit)
        except Exception as exc:
            raise CreatorToolAuditError(
                "Tool execution was blocked because audit storage is unavailable",
                call_id=call_id,
            ) from exc

        try:
            handler_result = await asyncio.wait_for(
                definition.handler(request, context),
                timeout=self._timeout_seconds,
            )
            validated_data = definition.output_model.model_validate(handler_result.data)
            result_payload = {
                "data": validated_data.model_dump(mode="json"),
                "provenance": [
                    item.model_dump(mode="json") for item in handler_result.provenance
                ],
                "degraded_services": list(handler_result.degraded_services),
            }
            encoded = _encode_json(result_payload)
            if len(encoded) > self._max_result_bytes:
                raise CreatorToolResultTooLargeError(
                    "Tool result exceeded the configured size limit",
                    call_id=call_id,
                    details={
                        "result_size_bytes": len(encoded),
                        "max_result_bytes": self._max_result_bytes,
                    },
                )
            result = CreatorToolResult(
                call_id=call_id,
                data=validated_data,
                provenance=handler_result.provenance,
                degraded_services=handler_result.degraded_services,
                trace_id=context.trace_id,
            )
            await self._finish_audit(
                call_id=call_id,
                status=CreatorToolCallStatus.SUCCESS,
                started_ns=started_ns,
                result_sha256=hashlib.sha256(encoded).hexdigest(),
                result_size_bytes=len(encoded),
                error_code=None,
            )
            return result
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish_audit(
                    call_id=call_id,
                    status=CreatorToolCallStatus.CANCELLED,
                    started_ns=started_ns,
                    result_sha256=None,
                    result_size_bytes=None,
                    error_code="TOOL_CANCELLED",
                )
            )
            raise
        except TimeoutError as exc:
            timeout_error = CreatorToolTimeoutError(
                "Tool execution timed out",
                call_id=call_id,
            )
            await self._finish_error(call_id, timeout_error, started_ns)
            raise timeout_error from exc
        except CreatorToolError as exc:
            tool_error = _with_call_id(exc, call_id)
            await self._finish_error(call_id, tool_error, started_ns)
            raise tool_error from exc
        except Exception as exc:
            code = str(getattr(exc, "code", "TOOL_EXECUTION_FAILED"))
            retryable = bool(getattr(exc, "retryable", False))
            safe_message = (
                str(exc)
                if hasattr(exc, "code")
                else "Tool execution failed unexpectedly"
            )
            execution_error = CreatorToolExecutionError(
                safe_message,
                call_id=call_id,
                details={"cause_code": code, "retryable": retryable},
            )
            execution_error.code = code
            execution_error.retryable = retryable
            if hasattr(exc, "code"):
                logger.warning(
                    "Creator tool rejected call_id=%s trace_id=%s tool=%s code=%s",
                    call_id,
                    context.trace_id,
                    name,
                    code,
                )
            else:
                logger.exception(
                    "Creator tool failed call_id=%s trace_id=%s tool=%s code=%s",
                    call_id,
                    context.trace_id,
                    name,
                    code,
                )
            await self._finish_error(call_id, execution_error, started_ns)
            raise execution_error from exc

    def _authorize(
        self,
        definition: CreatorToolDefinition,
        context: CreatorToolCallContext,
    ) -> None:
        principal = context.principal
        explicit = principal.allowed_tools
        if explicit and definition.name not in explicit:
            raise CreatorToolAuthorizationError(
                f"Caller is not allowed to use {definition.name}"
            )
        agent_allowlist = _AGENT_ALLOWLISTS.get(principal.caller)
        if agent_allowlist is not None and definition.name not in agent_allowlist:
            raise CreatorToolAuthorizationError(
                f"{principal.caller} is not allowed to use {definition.name}"
            )
        if principal.caller in {"MCP", "API", "SERVICE"} and not principal.has_role(
            "CREATOR",
            "CREATOR_SERVICE",
            "ADMIN",
        ):
            raise CreatorToolAuthorizationError(
                "Creator role is required for this tool"
            )
        if (
            definition.risk == CreatorToolRisk.DRAFT_WRITE
            and principal.caller not in {"WriterAgent", "MCP", "API", "SERVICE"}
            and not principal.has_role("ADMIN")
        ):
            raise CreatorToolAuthorizationError(
                "Draft writes are restricted to WriterAgent or creator services"
            )
        if definition.risk == CreatorToolRisk.EXTERNAL_WRITE:
            raise CreatorToolAuthorizationError(
                "External publication tools are disabled"
            )

    async def _finish_error(
        self,
        call_id: str,
        error: CreatorToolError,
        started_ns: int,
    ) -> None:
        status = (
            CreatorToolCallStatus.TIMEOUT
            if isinstance(error, CreatorToolTimeoutError)
            else CreatorToolCallStatus.FAILED
        )
        await self._finish_audit(
            call_id=call_id,
            status=status,
            started_ns=started_ns,
            result_sha256=None,
            result_size_bytes=None,
            error_code=error.code,
        )

    async def _finish_audit(
        self,
        *,
        call_id: str,
        status: CreatorToolCallStatus,
        started_ns: int,
        result_sha256: str | None,
        result_size_bytes: int | None,
        error_code: str | None,
    ) -> None:
        try:
            await self._audit.finish(
                call_id=call_id,
                status=status,
                finished_at=self._clock(),
                latency_ms=max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
                result_sha256=result_sha256,
                result_size_bytes=result_size_bytes,
                error_code=error_code,
            )
        except CreatorToolAuditError:
            raise
        except Exception as exc:
            raise CreatorToolAuditError(
                "Tool audit finalization failed",
                call_id=call_id,
            ) from exc


def _with_call_id(error: CreatorToolError, call_id: str) -> CreatorToolError:
    if error.call_id is not None:
        return error
    error.call_id = call_id
    return error


def _encode_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _hash_json(value) -> str:
    return hashlib.sha256(_encode_json(value)).hexdigest()
