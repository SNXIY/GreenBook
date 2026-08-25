"""LLM-backed Command Runtime interpreter.

This module owns the only user-message-to-command conversion used by the new
boundary.  Python validates model output; it does not classify messages with
language keywords or route tools from text.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from greenbook_agent_core.context.projection import project_interpreter_context
from greenbook_agent_core.llm_compat import extract_top_level_json, structured_call

from .models import (
    Command,
    CommandContext,
    CommandTarget,
    CommandType,
    DeliverableSegmentation,
    InputSpan,
    SpanGrouping,
    StructuredCommandOutput,
    TargetKind,
    TargetReferenceType,
    TaskDelta,
)
from .normalization import normalize_task_deltas
from .reference_extractor import ReferenceExtractor
from .semantic_derivation import apply_semantic_derivation
from .semantic_validator import validate_semantic_candidate
from .target import TargetResolutionStatus, TargetResolver

logger = logging.getLogger(__name__)


class CommandInterpretationError(ValueError):
    """Raised when the model does not return a valid Command shape."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CommandInterpreter:
    """Convert one user message into one validated Command object."""

    def __init__(
        self,
        *,
        llm: Any | None = None,
        model: str = "",
        target_resolver: TargetResolver | None = None,
        capability_registry: Any | None = None,
        reference_extractor: ReferenceExtractor | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._target_resolver = target_resolver or TargetResolver()
        self._capability_registry = capability_registry
        self._reference_extractor = reference_extractor or ReferenceExtractor()

    async def interpret(
        self,
        user_input: str,
        context: CommandContext | Any | None = None,
        *,
        llm: Any | None = None,
        model: str | None = None,
        run_id: str = "",
        turn_id: str = "",
    ) -> Command:
        """Ask the model for structured output and validate it."""

        text = user_input.strip()
        if not text:
            raise CommandInterpretationError("COMMAND_INPUT_EMPTY", "Command input is empty.")

        client = llm or self._llm
        if client is None:
            raise CommandInterpretationError(
                "COMMAND_LLM_UNAVAILABLE",
                "Command Runtime requires an LLM structured-output client.",
            )
        command_context = CommandContext.from_any(context)
        capability_catalog = self._capability_catalog()
        response = await self._create_response(
            client,
            text,
            command_context,
            capability_catalog,
            model if model is not None else self._model,
            run_id=run_id,
            turn_id=turn_id,
        )
        payload = _response_payload(response)
        _debug_structured_stage("raw", payload, run_id=run_id, turn_id=turn_id)
        structured = await self._validate_or_repair(
            payload,
            client=client,
            model=model if model is not None else self._model,
            user_input=text,
            context=command_context,
            capability_catalog=capability_catalog,
        )
        _debug_structured_stage(
            "schema_parse",
            structured.model_dump(mode="json"),
            run_id=run_id,
            turn_id=turn_id,
        )
        repair_triggered = _looks_like_merged_multi_objective(text, structured)
        if repair_triggered:
            structured = await self._repair_multi_objective_items(
                structured,
                client=client,
                model=model if model is not None else self._model,
                user_input=text,
                context=command_context,
                capability_catalog=capability_catalog,
                run_id=run_id,
                turn_id=turn_id,
            )
        _debug_structured_stage(
            "repair",
            {"triggered": repair_triggered, "items": [item.model_dump(mode="json") for item in structured.items]},
            run_id=run_id,
            turn_id=turn_id,
        )
        structured = _normalize_multi_objective_items(structured)
        _debug_structured_stage(
            "normalized",
            structured.model_dump(mode="json"),
            run_id=run_id,
            turn_id=turn_id,
        )
        # A complex CREATE has an ambiguous deliverable cardinality.  Let the
        # existing WHAT-only segmentation boundary resolve that shape before
        # the final Command is handed to semantic projection.  Simple CREATE
        # responses, including legacy provider fallbacks that omit ``items``,
        # keep the existing one-item schema projection and do not incur an
        # extra model call.
        should_segment_deliverables = (
            structured.command == CommandType.CREATE
            and str(structured.request_complexity).upper() == "COMPLEX"
        )
        structured = _ensure_create_item(structured)
        structured = await self._group_spans_into_items(
            structured,
            client=client,
            model=model if model is not None else self._model,
            user_input=text,
            context=command_context,
            capability_catalog=capability_catalog,
            run_id=run_id,
            turn_id=turn_id,
        )
        if should_segment_deliverables:
            structured = await self._segment_and_extract_items(
                structured,
                client=client,
                model=model if model is not None else self._model,
                user_input=text,
                context=command_context,
                capability_catalog=capability_catalog,
                run_id=run_id,
                turn_id=turn_id,
            )
        structured = _materialize_request_publication_constraints(structured)
        _debug_structured_stage(
            "segmentation",
            structured.model_dump(mode="json"),
            run_id=run_id,
            turn_id=turn_id,
        )

        command = Command(
            type=structured.command,
            goal=structured.goal or structured.objective or text,
            objective=structured.goal or structured.objective or text,
            first_action=structured.first_action,
            request_complexity=structured.request_complexity,
            task_changes=list(structured.task_changes or []),
            target=structured.target,
            parameters=structured.parameters,
            entities=structured.entities,
            constraints=structured.constraints,
            semantic_operation=structured.semantic_operation,
            scope=structured.scope,
            risk=structured.risk,
            references=structured.references,
            ambiguity=structured.ambiguity,
            needs_clarification=structured.needs_clarification,
            required_capabilities=list(dict.fromkeys(structured.required_capabilities)),
            confidence=structured.confidence,
            raw_input=text,
            items=list(structured.items or []),
        )
        _normalize_delete_post(command)
        _normalize_draft_only(command, text)
        # Command normalization boundary: a single TaskDelta may bundle
        # desired_changes of several business resources (e.g. a draft's title
        # AND its schedule's run_at).  Decompose it into one delta per resource
        # so each business action (UPDATE_DRAFT / UPDATE_SCHEDULE) is scheduled
        # independently instead of the bundled fields being silently dropped.
        command.task_changes = normalize_task_deltas(command.task_changes)
        # Keep the single structured candidate authoritative.  The compatibility
        # projection below only canonicalizes values that are already present;
        # it must not reconstruct an omitted operation, capability, or
        # publication requirement from another field or from raw text.
        command = apply_semantic_derivation(command)
        _debug_structured_stage(
            "semantic_derivation",
            {
                "semantic_operation": command.semantic_operation,
                "required_capabilities": list(command.required_capabilities),
                "publication_intent": command.constraints.get("publication_intent", ""),
                "item_count": len(command.items or ()),
            },
            run_id=run_id,
            turn_id=turn_id,
        )
        validation = validate_semantic_candidate(command)
        if not validation.valid:
            _debug_structured_stage(
                "semantic_validation",
                validation.model_dump(mode="json"),
                run_id=run_id,
                turn_id=turn_id,
            )
            codes = ", ".join(error.code for error in validation.errors)
            raise CommandInterpretationError(
                "COMMAND_SEMANTIC_INVALID",
                f"Structured semantic candidate is internally inconsistent: {codes}",
            )
        if command.is_broad_destructive:
            command.target_resolution = "NOT_APPLICABLE"
        elif command.requires_target and not (
            command.semantic_operation == "DELETE_POST"
            and command.target_resolution == TargetResolutionStatus.RESOLVED.value
        ):
            self._enrich_with_extracted_reference(command)
            self._resolve_target(command, command_context)
        self._validate_capabilities(command, capability_catalog)
        return command

    async def _group_spans_into_items(
        self,
        structured: StructuredCommandOutput,
        *,
        client: Any,
        model: str,
        user_input: str,
        context: CommandContext,
        capability_catalog: list[dict[str, str]],
        run_id: str = "",
        turn_id: str = "",
    ) -> StructuredCommandOutput:
        """Group deterministic input spans without overwriting explicit items.

        Span grouping is a bounded fallback for the one-item shape.  Once the
        semantic extraction boundary has already returned multiple explicit
        deliverables, regrouping raw spans can erase their per-item outcome
        ownership (for example, immediate versus scheduled publication).
        Preserve that structured cardinality and its constraints.
        """
        if structured.command != CommandType.CREATE or len(structured.items) != 1:
            return structured
        spans = _input_spans(user_input)
        if len(spans) <= 1:
            return structured
        request = {
            "spans": [span.model_dump(mode="json") for span in spans],
            "rule": "Assign every span exactly once. Group spans serving one final deliverable.",
        }
        try:
            response = await structured_call(
                client, model, _SPAN_GROUPING_PROMPT, "greenbook_span_grouping",
                SpanGrouping.model_json_schema(), request,
            )
            grouping = SpanGrouping.model_validate(_response_payload(response))
            assignments = _validate_span_grouping(grouping, spans)
            if assignments is None:
                repair = await structured_call(
                    client, model, _SPAN_GROUPING_REPAIR_PROMPT,
                    "greenbook_span_grouping_repair",
                    SpanGrouping.model_json_schema(),
                    {"spans": request["spans"], "current": _response_payload(response)},
                )
                grouping = SpanGrouping.model_validate(_response_payload(repair))
                assignments = _validate_span_grouping(grouping, spans)
            if assignments is None:
                return structured
            grouped: dict[str, list[InputSpan]] = {}
            for span in spans:
                group_id = assignments[span.span_id]
                grouped.setdefault(group_id, []).append(span)
            base_items = list(structured.items)
            items: list[dict[str, Any]] = []
            for index, group_spans in enumerate(grouped.values()):
                merged_text = " ".join(span.text for span in group_spans).strip()
                base = (
                    base_items[index].model_dump(mode="python")
                    if index < len(base_items)
                    else (base_items[0].model_dump(mode="python") if base_items else {})
                )
                title = str(base.get("title") or "").strip()
                if not title or len(grouped) > 1 and index >= len(base_items):
                    title = merged_text[:240]
                base_constraints = dict(base.get("constraints") or {})
                item_intent = _publication_intent_from_constraints(base_constraints)
                if not item_intent:
                    item_intent = _publication_intent_from_constraints(
                        structured.constraints
                    )
                temporal_text = str(base.get("temporal_text") or "").strip()
                # The span is evidence for deliverable grouping, not a time
                # expression.  Only use it as a temporal fallback when the
                # structured request already says this item is scheduled.
                # Otherwise incidental numbers in titles, test markers, or
                # ordinary prose must never become a publication time.
                if not temporal_text and item_intent in {
                    "SCHEDULED_PUBLISH",
                    "SCHEDULE",
                    "SCHEDULED",
                    "FUTURE_PUBLISH",
                    "FUTURE",
                }:
                    temporal_text = merged_text
                item = {
                    **base,
                    "title": title,
                    "topic": str(base.get("topic") or title),
                    "requirements": [merged_text],
                    "operation": "CREATE",
                    "capabilities": list(base.get("capabilities") or structured.required_capabilities or ()),
                    # Keep explicit per-item temporal evidence.  The span is
                    # only a fallback for an already scheduled item; it is
                    # never a generic temporal input.
                    "temporal_text": temporal_text,
                }
                items.append(item)
            payload = structured.model_dump(mode="python")
            payload["items"] = items
            return StructuredCommandOutput.model_validate(payload)
        except Exception:
            logger.warning("span_grouping_failed", exc_info=True)
            return structured

    async def _segment_and_extract_items(
        self,
        structured: StructuredCommandOutput,
        *,
        client: Any,
        model: str,
        user_input: str,
        context: CommandContext,
        capability_catalog: list[dict[str, str]],
        run_id: str = "",
        turn_id: str = "",
    ) -> StructuredCommandOutput:
        """Stabilize CREATE cardinality without introducing a planner.

        A single initial item is the only ambiguous shape: it can represent
        one connected pipeline or several merged deliverables.  The first
        constrained call answers only how many final entities exist.  When it
        returns more than one segment, the existing CommandItem schema is
        applied independently to each segment and the results are merged
        deterministically.
        """
        if structured.command != CommandType.CREATE or len(structured.items) != 1:
            return structured
        try:
            response = await structured_call(
                client,
                model,
                _DELIVERABLE_SEGMENTATION_PROMPT,
                "greenbook_deliverable_segmentation",
                DeliverableSegmentation.model_json_schema(),
                {
                    "user_input": user_input,
                    "context": project_interpreter_context(context),
                },
            )
            payload = _response_payload(response)
            _debug_structured_stage(
                "segmentation_raw",
                payload,
                run_id=run_id,
                turn_id=turn_id,
            )
            segmentation = DeliverableSegmentation.model_validate(payload)
            # A validated list with multiple entries is explicit cardinality
            # evidence even when the user left each entity's details open.
            # Preserve those placeholder entries; filtering them would merge
            # independent outcomes back into the fallback item.
            segments = list(segmentation.deliverables)
            if len(segments) <= 1:
                segments = [
                    segment for segment in segments
                    if any(
                        (
                            str(segment.text or "").strip(),
                            str(segment.entity_type or "").strip(),
                            str(segment.topic or "").strip(),
                            str(segment.title or "").strip(),
                            bool(segment.requirements),
                            str(segment.temporal_text or "").strip(),
                            bool(segment.constraints),
                            bool(segment.target_reference),
                        )
                    )
                ]
            if len(segments) <= 1 and str(structured.request_complexity).upper() == "COMPLEX":
                repair_response = await structured_call(
                    client,
                    model,
                    _DELIVERABLE_SEGMENTATION_REPAIR_PROMPT,
                    "greenbook_deliverable_segmentation_repair",
                    DeliverableSegmentation.model_json_schema(),
                    {
                        "user_input": user_input,
                        "current_segmentation": payload,
                    },
                )
                repaired = DeliverableSegmentation.model_validate(
                    _response_payload(repair_response)
                )
                repaired_segments = list(repaired.deliverables)
                if len(repaired_segments) <= 1:
                    repaired_segments = [
                        segment for segment in repaired_segments
                        if any(
                            (
                                str(segment.text or "").strip(),
                                str(segment.entity_type or "").strip(),
                                str(segment.topic or "").strip(),
                                str(segment.title or "").strip(),
                                bool(segment.requirements),
                                str(segment.temporal_text or "").strip(),
                                bool(segment.constraints),
                                bool(segment.target_reference),
                            )
                        )
                    ]
                if repaired_segments:
                    segments = repaired_segments
            if len(segments) <= 1:
                return structured
            # Stage 1 already carries the complete WHAT contract.  Mapping is
            # deterministic; no per-deliverable LLM calls or execution plan
            # synthesis are needed here.
            extracted: list[dict[str, Any]] = []
            for _index, segment in enumerate(segments):
                fallback = structured.items[0].model_dump(mode="python")
                item = {
                    **fallback,
                    "operation": segment.operation_hint or fallback.get("operation", "CREATE"),
                    "topic": segment.topic or fallback.get("topic", ""),
                    "title": segment.title or segment.topic or fallback.get("title", ""),
                    "requirements": list(segment.requirements or fallback.get("requirements") or ()),
                    "temporal_text": segment.temporal_text or fallback.get("temporal_text", ""),
                    "constraints": {
                        **dict(fallback.get("constraints") or {}),
                        **dict(segment.constraints or {}),
                    },
                }
                segment_intent = _publication_intent_from_constraints(item["constraints"])
                if segment_intent == "DRAFT_ONLY":
                    item["capabilities"] = [
                        capability
                        for capability in (item.get("capabilities") or ())
                        if str(capability).upper().replace("-", "_")
                        not in _SCHEDULE_PUBLICATION_CAPABILITIES
                    ]
                elif segment_intent == "SCHEDULED_PUBLISH":
                    capabilities = [str(value).upper() for value in (item.get("capabilities") or ())]
                    if "SCHEDULE_PUBLISH" not in capabilities:
                        capabilities.append("SCHEDULE_PUBLISH")
                    item["capabilities"] = list(dict.fromkeys(capabilities))
                if segment.target_reference:
                    item["constraints"]["target_reference"] = dict(segment.target_reference)
                if not item["title"] and segment.text:
                    item["title"] = segment.text[:240]
                if not item["topic"]:
                    item["topic"] = item["title"]
                extracted.append(item)
            merged = structured.model_dump(mode="python")
            merged["items"] = extracted
            return StructuredCommandOutput.model_validate(merged)
        except Exception:  # noqa: BLE001 - bounded shape repair, preserve original on failure
            logger.warning("deliverable_segmentation_failed", exc_info=True)
            return structured

    def _enrich_with_extracted_reference(self, command: Command) -> None:
        """Deterministically fill a missing/weak target when the LLM omitted one.

        An explicit id read from the text is authoritative and keeps the LLM's
        resource kind.  A semantic feature (topic token, ordinal, proximal, or
        coarse time window) overrides a weak model target — a bare label,
        ACTIVE, or a non-TASK resource reference that would otherwise resolve
        without an owning task_id — so the write binds to the real Task.
        TargetResolver still owns candidate selection and the bind/clarify
        boundary; the extractor never decides the resource.
        """
        feature = self._reference_extractor.extract(command.raw_input)
        if feature is None:
            return
        llm_target = command.target
        if feature.id:
            # The user spelled out an id; use it but preserve the LLM's kind.
            if llm_target is not None:
                command.target = llm_target.model_copy(
                    update={
                        "id": feature.id,
                        "resource_id": feature.id,
                        "reference_type": TargetReferenceType.IDENTIFIER,
                    }
                )
                return
            command.target = feature.to_command_target()
            return
        extracted = feature.to_command_target()
        if llm_target is None:
            command.target = extracted
            return
        if self._llm_target_is_specific(llm_target):
            return
        command.target = extracted

    @staticmethod
    def _llm_target_is_specific(target: CommandTarget) -> bool:
        """A model target is concrete only when it pins an owned Task identity.

        A DRAFT/SCHEDULE resource reference without an owning task_id is not
        enough to defeat a stronger deterministic topic/ordinal extract: a
        cross-turn reference ("Java 那篇") must resolve to the owning Task so a
        schedule/draft write binds a real task_id instead of failing with an
        unbound Execution.
        """
        if target.task_id:
            return True
        return target.kind == TargetKind.TASK and bool(target.explicit_id)

    async def _validate_or_repair(
        self,
        payload: Any,
        *,
        client: Any,
        model: str,
        user_input: str,
        context: CommandContext,
        capability_catalog: list[dict[str, str]],
    ) -> StructuredCommandOutput:
        """Validate the model's Command payload with bounded repair.

        Recovery ladder:
        1. strip unknown top-level keys (envelope echo tolerance);
        2. deterministically normalize known schema violations (command
           enum variants, container types, per-delta extra fields);
        3. one LLM repair pass carrying the concrete validation error;
        4. fail closed with a log that records the offending payload shape.
        """

        def attempt(candidate: Any) -> StructuredCommandOutput | None:
            if not isinstance(candidate, Mapping):
                return None
            try:
                return StructuredCommandOutput.model_validate(candidate)
            except ValidationError:
                return None

        structured = attempt(payload)
        if structured is not None:
            return structured

        stripped = _strip_unknown_command_fields(payload)
        structured = attempt(stripped)
        if structured is not None:
            return structured

        repaired = _repair_command_output(stripped if stripped != payload else payload)
        structured = attempt(repaired)
        if structured is not None:
            return structured

        # One bounded LLM repair pass: give the model the concrete error and
        # ask it to return only the schema fields.  This mirrors the planner's
        # contract-repair behaviour and must not loop.
        try:
            repaired_payload = dict(repaired if isinstance(repaired, Mapping) else {})
            repaired_payload["contract_repair"] = {
                "validation_error": _validation_summary(payload, repaired),
                "instruction": (
                    "Return only valid JSON matching the greenbook_command "
                    "schema. command must be one of CREATE, MODIFY, CANCEL, "
                    "QUERY, CONTROL. Do not add envelope fields. Do not explain."
                ),
            }
            response = await structured_call(
                client,
                model,
                _COMMAND_SYSTEM_PROMPT,
                "greenbook_command",
                StructuredCommandOutput.model_json_schema(),
                {
                    "user_input": user_input,
                    "context": project_interpreter_context(context),
                    "available_capabilities": capability_catalog,
                    "contract_repair": repaired_payload["contract_repair"],
                },
            )
            structured = attempt(_response_payload(response))
            if structured is not None:
                return structured
        except (ValidationError, ValueError):
            pass

        logger.warning(
            "command_schema_invalid user_input=%r payload=%r repaired=%r",
            user_input[:200],
            json.dumps(payload, ensure_ascii=False, default=str)[:2000],
            json.dumps(repaired, ensure_ascii=False, default=str)[:2000],
        )
        raise CommandInterpretationError(
            "COMMAND_SCHEMA_INVALID",
            "我没能完全理解这句话的意思，请换个说法再试一次。",
        )

    async def _create_response(
        self,
        client: Any,
        user_input: str,
        context: CommandContext,
        capability_catalog: list[dict[str, str]],
        model: str,
        run_id: str = "",
        turn_id: str = "",
    ) -> Any:
        from greenbook_agent_core.observability.run_metrics import llm_category_scope, run_scope

        request_payload = {
            "user_input": user_input,
            "context": project_interpreter_context(context),
            "available_capabilities": capability_catalog,
        }
        provider_request = {
            "model": model,
            "schema_name": "greenbook_command",
            "request": request_payload,
        }
        # Optional, structured-only evidence for Golden E2E.  It is disabled
        # by default and never becomes runtime/business state.
        _debug_structured_stage(
            "provider_request",
            provider_request,
            run_id=run_id,
            turn_id=turn_id,
        )
        with run_scope(run_id), llm_category_scope("SEMANTIC"):
            return await structured_call(
                client,
                model,
                _COMMAND_SYSTEM_PROMPT,
                "greenbook_command",
                StructuredCommandOutput.model_json_schema(),
                request_payload,
            )

    def _capability_catalog(self) -> list[dict[str, str]]:
        registry = self._capability_registry
        if registry is None:
            return []
        list_all = getattr(registry, "list_all", None)
        values = list_all() if callable(list_all) else registry
        if values is None:
            return []
        catalog: list[dict[str, str]] = []
        for value in values:
            if isinstance(value, Mapping):
                name = str(value.get("name", "")).strip()
                description = str(value.get("description", "")).strip()
            else:
                name = str(getattr(value, "name", "")).strip()
                description = str(getattr(value, "description", "")).strip()
            if name:
                catalog.append({"name": name, "description": description})
        return catalog

    @staticmethod
    def _validate_capabilities(
        command: Command,
        capability_catalog: Sequence[Mapping[str, str]],
    ) -> None:
        if not capability_catalog or command.is_broad_destructive:
            return
        allowed = {item["name"] for item in capability_catalog}
        unknown = set(command.required_capabilities) - allowed
        if unknown:
            logger.warning(
                "command_capability_unavailable unknown=%s",
                ",".join(sorted(unknown)),
            )
            raise CommandInterpretationError(
                "COMMAND_CAPABILITY_UNAVAILABLE",
                "这个请求涉及的能力暂时不可用，请换个说法再试一次。",
            )
    def _resolve_target(self, command: Command, context: CommandContext) -> None:
        if command.target is None:
            command.target_resolution = TargetResolutionStatus.NOT_FOUND.value
            _observe_target_resolution(command, TargetResolutionStatus.NOT_FOUND.value)
            return
        resolution = self._target_resolver.resolve(command, context)
        command.target_resolution = resolution.status.value
        command.target_candidates = [
            candidate.model_dump(mode="json")
            for candidate in (resolution.candidates or [])
        ]
        _observe_target_resolution(command, resolution.status.value)
        if resolution.is_resolved and resolution.target is not None:
            candidate = resolution.target
            command.resolved_target = candidate.model_dump(mode="json")
            command.target = command.target.model_copy(
                update={
                    "id": candidate.identity,
                    "task_id": candidate.task_id,
                    "resource_id": candidate.resource_id,
                }
            )


def _observe_target_resolution(command: Command, status: str) -> None:
    try:
        from greenbook_agent_core.observability.bus import observability

        observability().target_resolution().inc(status=status)
    except Exception:  # noqa: BLE001 - observability must never break interpretation
        pass


def _debug_structured_stage(
    stage: str,
    payload: Any,
    *,
    run_id: str = "",
    turn_id: str = "",
) -> None:
    """Write structured-only Interpreter diagnostics when explicitly enabled."""
    if os.getenv("GREENBOOK_DEBUG_INTERPRETER", "").strip().lower() not in {"1", "true", "yes"}:
        return
    try:
        path = os.getenv("GREENBOOK_DEBUG_INTERPRETER_FILE", ".tmp-interpreter-structured.jsonl")
        record = {
            "stage": stage,
            "run_id": str(run_id or "") or None,
            "turn_id": str(turn_id or run_id or "") or None,
            "payload": payload,
        }
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - diagnostics must never affect interpretation
        logger.debug("interpreter_debug_trace_failed", exc_info=True)


# These are semantic cue groups, not a phrase classifier.  The structured
# model remains authoritative for normal commands; this boundary only repairs
# the high-risk case where a model emits a read envelope for an imperative
# mutation while the conversation already has an owned publication target.
_PUBLICATION_WORDS = (
    "\u53d1\u5e03",  # 发布
    "\u53d1\u51fa\u53bb",  # 发出去
    "\u53d1\u51fa",  # 发出
    "\u53d1\u5e16",  # 发帖
    "\u53d1\u6587",  # 发文
    "\u53d1",  # 发
)
_IMMEDIATE_PUBLICATION_CUES = (
    "\u73b0\u5728",  # 现在
    "\u7acb\u5373",  # 立即
    "\u9a6c\u4e0a",  # 马上
    "\u7acb\u523b",  # 立刻
    "\u76f4\u63a5",  # 直接
    "\u4e0d\u7528\u7b49",  # 不用等
    "\u4e0d\u7b49",  # 不等
    "\u63d0\u524d",  # 提前
    "\u5373\u65f6",  # 即时
)
_PUBLICATION_STATUS_CUES = (
    "\u5417",  # 吗
    "\u662f\u5426",  # 是否
    "\u6709\u6ca1\u6709",  # 有没有
    "\u4ec0\u4e48\u72b6\u6001",  # 什么状态
    "\u5f53\u524d\u72b6\u6001",  # 当前状态
    "\u53d1\u5e03\u72b6\u6001",  # 发布状态
    "\u4ec0\u4e48\u65f6\u5019",  # 什么时候
    "\u4f55\u65f6",  # 何时
    "\u51e0\u70b9",  # 几点
    "\u67e5\u8be2",  # 查询
    "\u67e5\u770b",  # 查看
    "\u770b\u4e00\u4e0b",  # 看一下
)


def _normalize_delete_post(command: Command) -> None:
    """Normalize a structured destructive POST mutation to DELETE_POST.

    This consumes only structured command fields; it does not inspect user
    text.  A POST target plus an explicit destructive command must never be
    downgraded to a read/detail action.
    """
    target = command.target
    if target is None or target.kind != TargetKind.POST:
        return
    semantic = str(command.semantic_operation or "").upper()
    risk = str(command.risk or "").upper()
    if command.type != CommandType.CANCEL and semantic not in {"DELETE", "REMOVE", "PURGE", "DESTROY"} and "DESTRUCTIVE" not in risk:
        return
    command.type = CommandType.CANCEL
    command.semantic_operation = "DELETE_POST"
    command.first_action = "DELETE_POST"
    command.request_complexity = "SIMPLE"
    command.required_capabilities = ["DELETE_POST"]
    post_id = target.explicit_id or target.reference
    if post_id:
        command.target_resolution = TargetResolutionStatus.RESOLVED.value
        command.resolved_target = {"kind": "POST", "id": post_id, "resource_id": post_id}
    command.task_changes = [
        TaskDelta(
            operation="UPDATE_GOAL",
            target_reference={"reference_type": "IDENTIFIER", "id": post_id, "kind": "POST"},
            desired_changes={"semantic_action": "DELETE_POST", "post_id": post_id},
        )
    ]

    async def _repair_multi_objective_items(
        self,
        structured: StructuredCommandOutput,
        *,
        client: Any,
        model: str,
        user_input: str,
        context: CommandContext,
        capability_catalog: list[dict[str, str]],
        run_id: str = "",
        turn_id: str = "",
    ) -> StructuredCommandOutput:
        """Bounded semantic shape repair for an obviously merged output."""
        try:
            response = await structured_call(
                client,
                model,
                _COMMAND_SYSTEM_PROMPT
                + "\nThe previous output is malformed: the user supplied multiple independent publish times but you returned one item. Return the same schema with one atomic CommandItem per independent final deliverable. Keep each item's title, topic, capabilities, and temporal_text together. Do not split dependent steps such as SEARCH/ANALYZE/GENERATE within one deliverable.",
                "greenbook_command",
                StructuredCommandOutput.model_json_schema(),
                {
                    "user_input": user_input,
                    "context": project_interpreter_context(context),
                    "available_capabilities": capability_catalog,
                    "previous_output": structured.model_dump(mode="json"),
                    "semantic_repair": "atomic_multi_objective_items",
                },
            )
            candidate = _response_payload(response)
            if isinstance(candidate, Mapping):
                return StructuredCommandOutput.model_validate(candidate)
        except (ValidationError, ValueError, TypeError):
            logger.warning("multi_objective_shape_repair_failed", exc_info=True)
        return structured
def _normalize_draft_only(command: Command, user_input: str) -> None:
    """Remove hallucinated publication work from an explicit draft-only turn.

    The structured model output is allowed to be repaired at this boundary,
    but publication must never be inferred when the user explicitly asks to
    retain a draft.  This keeps the request on the single CREATE_DRAFT path
    and prevents an unrelated approval execution from being spawned.
    """
    if command.type != CommandType.CREATE:
        return
    constraints = command.constraints if isinstance(command.constraints, Mapping) else {}
    intents = [constraints.get("publication_intent"), constraints.get("publication_mode"), constraints.get("content_state")]
    for item in command.items or ():
        item_constraints = getattr(item, "constraints", {})
        if isinstance(item_constraints, Mapping):
            intents.extend([item_constraints.get("publication_intent"), item_constraints.get("publication_mode"), item_constraints.get("content_state")])
    item_intents = [
        _publication_intent_from_constraints(getattr(item, "constraints", {}))
        for item in command.items or ()
    ]
    top_intent = _publication_intent_from_constraints(constraints)
    # Draft-only normalization is request-wide only when no item carries a
    # different publication outcome.  A mixed draft/scheduled request must
    # retain its per-item publication ownership.
    if any(intent not in {"", "DRAFT_ONLY"} for intent in item_intents):
        return
    if top_intent in {"SCHEDULED_PUBLISH", "IMMEDIATE_PUBLISH"} and "DRAFT_ONLY" in item_intents:
        return
    draft_only = any(
        str(value or "").strip().upper().replace("-", "_") in {"DRAFT_ONLY", "SAVE_AS_DRAFT", "DRAFT"}
        for value in intents
    ) or any(value is False for value in (constraints.get("publish"), constraints.get("schedule"), constraints.get("publish_now")))
    if not draft_only:
        return
    # Draft-only describes the final publication outcome.  It must not erase
    # explicit read/evidence prerequisites from the same structured request
    # (for example SEARCH_COMMUNITY -> GENERATE_CONTENT -> CREATE_DRAFT).
    # Remove only publication mutations, then keep the remaining semantic
    # capabilities in their existing order.
    publication_capabilities = {
        "PUBLISH_NOW",
        "SCHEDULE_PUBLISH",
        "MANAGE_SCHEDULE",
        "UPDATE_SCHEDULE",
        "CANCEL_SCHEDULE",
        "DELETE_POST",
    }
    preserved = [
        str(capability).upper()
        for capability in (command.required_capabilities or ())
        if str(capability).upper() not in publication_capabilities
        and str(capability).upper() not in {"CREATE_DRAFT", "GENERATE_CONTENT"}
    ]
    command.required_capabilities = [*dict.fromkeys([*preserved, "GENERATE_CONTENT"])]
    command.first_action = (
        "SEARCH_COMMUNITY"
        if "SEARCH_COMMUNITY" in command.required_capabilities
        else "GENERATE_CONTENT"
    )
    command.semantic_operation = "CREATE"
    command.request_complexity = "SIMPLE"
    command.constraints = {
        key: value
        for key, value in (command.constraints or {}).items()
        if key not in {
            "run_at", "scheduled_at", "publish_at", "publish_time",
            "publication_intent", "publication_mode", "content_state",
        }
    }
    for item in command.items or []:
        item_capabilities = [
            str(capability).upper()
            for capability in (getattr(item, "capabilities", ()) or ())
            if str(capability).upper() not in publication_capabilities
            and str(capability).upper() not in {"CREATE_DRAFT", "GENERATE_CONTENT"}
        ]
        item_capabilities = list(dict.fromkeys([*item_capabilities, "GENERATE_CONTENT"]))
        if hasattr(item, "capabilities"):
            item.capabilities = item_capabilities
        elif isinstance(item, Mapping):
            item["capabilities"] = item_capabilities


LLMCommandInterpreter = CommandInterpreter


def _response_payload(response: Any) -> Any:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else parsed
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        raise CommandInterpretationError(
            "COMMAND_RESPONSE_EMPTY",
            "我没能理解这句话的意思，请换个说法再试一次。",
        )
    try:
        return json.loads(extract_top_level_json(content))
    except json.JSONDecodeError as exc:
        raise CommandInterpretationError(
            "COMMAND_RESPONSE_INVALID_JSON",
            "我没能理解这句话的意思，请换个说法再试一次。",
        ) from exc


_DELIVERABLE_SEGMENTATION_PROMPT = """You are the GreenBook deliverable segmentation boundary.
Answer only WHAT, never HOW.

Identify the independent final business entities the user wants created,
revised, or published.  One independently createable Draft/Post/article is
one deliverable.  A connected search -> summarize -> write -> schedule
pipeline is one deliverable.  Two independent articles with separate titles
or publication times are two deliverables.  For each deliverable fill its own
operation_hint, entity_type, topic, title, requirements, temporal_text,
constraints, and target_reference.  Preserve per-deliverable
publication_intent such as SCHEDULED_PUBLISH or DRAFT_ONLY when publication
outcomes differ.  A future publication requirement without a concrete time
keeps publication_intent=SCHEDULED_PUBLISH with an empty temporal_text; do not
invent a timestamp or downgrade it to NONE.  Do not put execution steps in
separate deliverables.
Preserve the cardinality of explicitly independent final entities even when
the user has not supplied each entity's topic or title; an entity_type and an
empty detail field are valid.  Do not collapse such entities into one vague
aggregate.  Count independently satisfiable outcomes, not verbs, tools, or
prerequisite steps.
Do not emit tools, capabilities, plans, dependencies, or execution steps.
Return deliverables=[] only for a pure query or conversational message.
"""

_DELIVERABLE_SEGMENTATION_REPAIR_PROMPT = """Repair only the deliverable cardinality.
Re-read the original user request and the current segmentation.  One final
Draft/Post/article that can be independently created or published must be one
deliverable.  Never merge independent articles just because they share one
request.  A search, summary, or generation instruction remains a requirement
inside its deliverable.  Preserve each deliverable's own title, topic,
requirements, constraints, and publication time.  Preserve explicitly requested
independent-entity cardinality even when some details are unknown; use the
entity_type field with empty detail fields rather than collapsing entities.
When a deliverable must be published in the future but its time is unknown,
keep constraints.publication_intent=SCHEDULED_PUBLISH and temporal_text empty;
do not invent a timestamp.
Do not add tools, capabilities, plans, or execution steps.  Return the same
deliverables schema and nothing else.
"""

_SPAN_GROUPING_PROMPT = """You are a strict span grouping boundary.
The input contains numbered spans cut deterministically from one user message.
Return only assignments. Every span_id must appear exactly once. A group means
the spans belong to one final independently createable/publishable entity.
Search, summarize, write, and schedule spans for the same entity share a group;
independent final articles use different groups. Never invent or omit spans.
Do not output deliverables, titles, tools, capabilities, plans, or prose.
"""

_SPAN_GROUPING_REPAIR_PROMPT = """Repair only assignment coverage. Return one
assignment for every supplied span_id, exactly once. Group spans by the final
business entity they serve; do not create or remove spans and do not emit any
other fields.
"""


def _input_spans(text: str) -> list[InputSpan]:
    """Split only on structural punctuation/paragraph/list boundaries."""
    chunks = re.split(r"(?:\r?\n+|(?<=[。！？!?；;.]))", text)
    spans: list[InputSpan] = []
    for chunk in chunks:
        value = re.sub(r"^\s*(?:[-*]|\d+[.)]|[一二三四五六七八九十]+[、.)])\s*", "", chunk).strip()
        if value:
            spans.append(InputSpan(span_id=len(spans) + 1, text=value))
    return spans


def _validate_span_grouping(
    grouping: SpanGrouping,
    spans: Sequence[InputSpan],
) -> dict[int, str] | None:
    expected = {span.span_id for span in spans}
    result: dict[int, str] = {}
    for assignment in grouping.assignments:
        group = str(assignment.group_id or "").strip()
        if assignment.span_id not in expected or not group or assignment.span_id in result:
            return None
        result[assignment.span_id] = group
    return result if set(result) == expected else None


_COMMAND_SYSTEM_PROMPT = """You are the GreenBook Command Runtime.

Return exactly one JSON object matching the supplied greenbook_command schema.
The command field is only the coarse operation boundary CREATE, MODIFY, CANCEL,
QUERY, or CONTROL; it is not a traditional intent taxonomy and must not be
chosen by keyword rules.  Extract the user's open semantic facts into goal,
entities, constraints, references, ambiguity, publication requirements,
temporal expressions, and target references.  ``semantic_operation``,
``required_capabilities``, ``first_action``, ``request_complexity``,
``needs_clarification``, ``temporal_kind``, ``temporal_resolved``, and ``run_at``
are compatibility fields or evidence only: the runtime deterministically
derives their canonical values after item normalization.
Write goal, objective, and every task/change description in the same language
as the user's message (Chinese user input → Chinese goal and descriptions).

Describe the open semantic evidence needed by the runtime: action family,
publication requirement, target/reference, temporal expression, deliverable
ownership, content constraints, and dependencies.  The runtime derives the
canonical capabilities and first action from those facts.  It also derives
SIMPLE/COMPLEX after deliverable segmentation; the complexity value here is
only a bootstrap hint for that existing boundary.

When the user is modifying or steering existing work in this conversation
("Redis 那个不用了", "总结完再写一篇", "改成明天下午3点", "顺便查一下 RAG"),
populate task_changes with one TaskDelta per change, each carrying an
operation (CREATE_TASK / ADD_GOAL / UPDATE_GOAL / CANCEL_GOAL / CANCEL_TASK /
CONTINUE_TASK / NO_CHANGE / ASK_USER), a target_reference identifying the
existing Task/Goal (label or id), desired_changes with the new desired state
(for UPDATE_GOAL: run_at / description / publication state), and optional
dependency_reference / source_reference. target_reference is a reference, not
an execution plan; Tool capabilities never appear as an operation. A change
that cannot be safely grounded sets needs_target_resolution true. task_changes
is a state mutation; the Command type, goal, and required_capabilities still
describe any genuinely new work in the same message. Emit NO_CHANGE when the
message adds no task-level change.

Business operations against an existing resource are NOT Task/Goal lifecycle
operations. For update/delete a draft, delete an owned published post, create/update/cancel a schedule, or
publish now, use UPDATE_GOAL and put exactly one canonical semantic_action in
desired_changes: UPDATE_DRAFT, DELETE_DRAFT, DELETE_POST, CREATE_SCHEDULE,
UPDATE_SCHEDULE, CANCEL_SCHEDULE, or PUBLISH_NOW. Include only fields the
user asked to mutate (for example title without body, or run_at without draft
content); preserve unspecified fields. For "取消发布，草稿保留", emit
UPDATE_GOAL + semantic_action=CANCEL_SCHEDULE — never CANCEL_TASK and never
CANCEL_GOAL. Use CANCEL_GOAL only for abandoning an unexecuted logical
objective with no external business resource; use CANCEL_TASK only when the
user explicitly wants the Agent to stop pursuing the whole Task. Neither
operation itself proves an external write occurred.

For time changes, preserve the user expression in run_at and declare its
temporal_base: CURRENT_TIME for "十分钟后", EXISTING_SCHEDULE_TIME for
"比原计划晚十分钟" / "提前两小时", and EXPLICIT_DATETIME for a stated date
and time. The runtime, not the model, turns that meaning into the final Java
timestamp using the authenticated timezone and (when applicable) a fresh
authoritative schedule read.

Use the conversation history, summary, active tasks, unfinished goals, and
structured target candidates to understand follow-up turns.  When the user
refers to an existing task or artifact, emit a structured target/reference and
prefer reference_type ACTIVE only when the conversation context establishes an
active object.  A follow-up such as changing an existing task should modify the
existing target rather than silently creating a new task.  Set
  ambiguity or missing-evidence details when present, but do not treat the
boolean needs_clarification value as the final routing decision.  Target
cardinality, temporal resolution, and semantic validation determine the final
clarification state; do not guess an identity.

Canonical objective/task/resource IDs are resolver-owned. Even when IDs are
visible in context, do not copy one into a target unless the user supplied it
or it is the explicit identity of the semantic reference. A named label such
as "Java 那篇" or "Redis 那篇" is label/reference evidence, not ACTIVE and
not permission to select the active/latest object; use the label field with
reference_type NONE, not PROPERTY, unless the user explicitly asks for an
attribute/property match. Use ACTIVE/RECENT/LATEST
only when the user explicitly uses a recentness expression (for example
"刚刚那篇" or "最新那篇"); never synthesize those types as a fallback, and
never turn a new CREATE into MODIFY merely because context has active tasks.

Follow-up references take several conversational forms; express them in
target_reference so the runtime can resolve them deterministically:
- a content label ("Java 那篇") -> target_reference.label set to the quoted
  label (a distinctive substring of the task goal or draft title);
- an ordinal ("第三篇", "第一篇") -> target_reference.ordinal (1-based
  creation order) and reference_type ORDINAL;
- the most recent object ("刚刚那篇", "最新那篇") -> reference_type ACTIVE
  (the runtime prefers persisted conversation focus; multiple candidates
  without focus require clarification);
- a publication-time window ("下午那篇") -> target_reference.label may carry
  the time word; the runtime matches the task's run_at window.
Prefer the most specific available identity: a unique goal_id when visible,
otherwise a distinctive label. Never invent a goal_id.

For a user-triggered retry after PARTIAL/FAILED work (for example "失败的那个
再试一下", "只重试没成功的", or "失败的那个标题改成 X 再发"), emit a fresh
TaskDelta with source_reference.kind="FAILED_OBJECTIVE_RETRY",
source_reference.user_triggered_retry=true, and target_reference.reference_type
="FAILED". Include a concrete objective_id/label only when the user supplied
one or the context makes one identity explicit. The runtime accepts exactly
one FAILED Objective; multiple failed Objectives must remain ambiguous and be
clarified. Put only user-requested edits such as title/content/run_at in
desired_changes. For a plain retry, still include the existing business
semantic_action in desired_changes: PUBLISH_NOW for a failed publication, or
the create action for a failed pre-resource creation. For a retry with edits,
use the existing-resource mutation action plus the requested edits when a
resource already exists; do not leave desired_changes empty or represent that
mutation as an unrelated CREATE. The old Objective is history/resource provenance, never a
state to reopen, and COMPLETED/CANCELLED/SUPERSEDED Objectives are not retry
targets. A retry with an existing Draft reuses that Draft and continues the
unmet outcome. RESULT_UNKNOWN/VERIFYING_RESULT is not FAILED and requires
reconciliation before any new user retry; set needs_clarification and retain
the FAILED/unknown semantic reference without emitting a physical retry.

This natural-language retry is different from the typed CONTROL command
RETRY_EXECUTION: CONTROL retries one existing Runtime execution with the same
Objective, while FAILED_OBJECTIVE_RETRY creates a new Task and Objective.

An active_task, active_target, unfinished Goal, similar history, or previous
successful Run is only grounding context; none of these alone authorizes a
mutation.  A complete independent request that restates a full business
outcome (for example search + analysis + article + future publication) is
NEW_WORK/CREATE even when an earlier Task remains active.  Emit UPDATE_GOAL or
CANCEL_GOAL only when the user explicitly steers existing work and the
task_changes reference identifies the intended Goal by a non-blank goal_id,
strong label, or explicit task-relative reference (for example
target_reference.reference_type=ACTIVE for an explicitly task-relative
command).  If the mutation meaning is clear but that reference is absent or ambiguous, set
needs_target_resolution to true; never emit an empty goal_id and never silently
convert the unresolved mutation into CREATE_TASK.

Use reference_type IDENTIFIER for a specific ID, ORDINAL with ordinal for an
ordered target, PROPERTY with property and value for an attribute match, and
TEMPORAL with ISO after/before bounds for a time-window match.  Do not emit MCP
tool names, execution plans, queue operations, or prose outside the JSON.  When
available_capabilities are supplied in the user payload, required_capabilities
must contain only those exact canonical names; never invent synonyms or
lowercase aliases. CONTROL is reserved for explicit runtime controls such as

approve, reject, pause, resume, retry, or cancel an existing execution. A
business request such as "立即发布这篇文章" is a CREATE or MODIFY operation
with the PUBLISH_NOW capability, not a CONTROL command.

Required capabilities must be semantic and sufficient for the requested
outcome, but must not include capabilities whose required target or evidence
is absent. In particular, do not request GET_POST_DETAIL or
ANALYZE_PERFORMANCE for a general community trend, interest, column-planning,
or promotion request unless the user explicitly asks for a concrete post,
engagement metrics, account performance, or supplies an eligible target. Do
not treat understanding community interests as a request for the user's own
performance metrics, and do not add capabilities merely because they are
present in the catalog.

When the user asks to write, generate, create, or save an article as a draft,
use GENERATE_CONTENT; SAVE_DRAFT is not a canonical capability. Use
SCHEDULE_PUBLISH for a future publication and PUBLISH_NOW only for an explicit
immediate publication. When the user explicitly says publication must wait for
their confirmation, record the structured constraint {"requires_approval":
true}; do not infer that constraint for an ordinary future schedule.

Publication intent is independent from whether a concrete time is known. If
the requested outcome includes future publication but the user did not supply
enough temporal detail, preserve the request-level structured evidence with
constraints.publication_intent="SCHEDULED_PUBLISH", include SCHEDULE_PUBLISH
in required_capabilities, leave temporal_text/run_at empty when no time was
given, and set needs_clarification=true. This is unresolved future publication,
not publication intent NONE and never PUBLISH_NOW. When several deliverables
are present, put the same intent on each affected item's constraints when the
request applies to all of them; put DRAFT_ONLY on draft-only items and keep
item-specific publication evidence separate.

Distinguish an immediate publication mutation from a status question by the
requested outcome, not by the word "now" alone. Existing content plus an
imperative immediate-publication cue (publish/send it directly, do not wait,
publish early, immediately publish) is MODIFY with semantic_action=PUBLISH_NOW
and required_capabilities=["PUBLISH_NOW"]. Questions asking whether, when, or
what status a publication has remain QUERY/read actions.

When the requested outcome is draft-only, set constraints.publication_intent to
"DRAFT_ONLY" (and omit SCHEDULE_PUBLISH/PUBLISH_NOW from that item's
capabilities). Do not encode this as free-form text for downstream code.

Publication intent is evidence, not a product default. If the user only asks
to write/create/make something and does not say draft/save/not publish,
publish-now, or future publication, leave publication_intent absent (NONE or
UNKNOWN); when the field is present, use exactly UNKNOWN. Do not infer DRAFT_ONLY from CREATE, GENERATE_CONTENT, or a retry
that recreates a failed pre-resource operation. "Whether to publish is not
decided" also leaves publication intent absent while preserving that
undecided constraint. A later product policy may save content as a draft, but
that policy is not semantic evidence. A plain retry of a failed publication
may carry forward that failed business outcome as the new requested outcome;
it must not carry forward the old terminal lifecycle or invent DRAFT_ONLY.

Negative and preservation constraints have the same priority as positive
intent. Attach "do not change", "keep the draft", "preserve the schedule",
and "only change this field" evidence to the owning item or TaskDelta, and
omit unrequested mutations. A QUERY such as viewing/showing a draft is not a
new CREATE; keep its semantic target label, leave publication intent absent,
and require clarification when no real candidate is grounded.

When one request contains several independent content targets,
required_capabilities is the aggregate capability set only. Do not use a
request-wide concrete run_at, target, or action value to represent every
target; preserve those values for the Goal Runtime to attach to the
corresponding Goal. A shared unresolved publication requirement is different:
constraints.publication_intent="SCHEDULED_PUBLISH" may be request-level when
the structured request applies it to every deliverable, while each item's
missing time remains unresolved. A future time is never PUBLISH_NOW, and
saving as a draft or explicitly not publishing is never an immediate
publication request.

Each independent user-mentioned outcome owns exactly one item or one
TaskDelta. Keep that item's target, action, publication intent, temporal
expression, and constraints together; do not copy a sibling's value, and do
not emit mutations for other active tasks that the user did not mention.
For a user-triggered retry, preserve the FAILED semantic reference and create
new-work evidence; if the old outcome is RESULT_UNKNOWN, emit reconciliation
required/clarification evidence and no new CREATE, PUBLISH, or other physical
retry. RESULT_UNKNOWN is never FAILED.
"""

_COMMAND_SYSTEM_PROMPT += """

For a broad destructive request such as deleting all owned posts or articles,
represent the meaning without inventing a target ID: set semantic_operation to
DELETE, scope to ALL_OWNED_POSTS (or an equivalent explicit unbounded scope),
and risk to BROAD_DESTRUCTIVE. Do not turn it into a normal target-not-found
error and do not select a delete-all capability that is not in the catalog.

Deliverable-first contract for new work:
items is the authoritative business projection. One independent final entity
that can be created, revised, or published (one Draft/Post/article) is exactly
one item. For every new CREATE request, populate items with one object per
independent deliverable; do not merge items merely because they occur in one
user message. Each item preserves its own topic, title, requirements (plain
language requirements for the deliverable), and temporal_text/run_at.
SEARCH, ANALYZE, summarization, and GENERATE are requirements within that item,
not separate items. A connected pipeline is one item; two independent articles
with two titles or publish times are two items. The top-level command describes
the request; items answer only WHAT.

Do not turn items into an execution plan. Do not emit TaskNode/GoalTree data.
Capabilities may be included as canonical high-level metadata for compatibility,
but do not split an item by capability and do not require a separate
CREATE_TASK entry for each item. task_changes is reserved for explicit updates
to existing tasks/resources and is not the source of new-item truth. For a
simple new draft, still return items with one object. For QUERY or an explicit
update/cancel of an existing resource, items may be empty.

A single connected pipeline (search → summarize → write → schedule a future
publication) is ONE outcome, not several: keep it as one item; do not emit an execution plan here.
"""


_SCHEDULED_PUBLICATION_INTENTS = frozenset({
    "FUTURE",
    "FUTURE_PUBLISH",
    "PUBLISH_LATER",
    "SCHEDULE",
    "SCHEDULED",
    "SCHEDULED_PUBLISH",
    "UNRESOLVED",
})
_DRAFT_PUBLICATION_INTENTS = frozenset({"DRAFT", "DRAFT_ONLY", "SAVE_AS_DRAFT"})
_IMMEDIATE_PUBLICATION_INTENTS = frozenset({"IMMEDIATE", "IMMEDIATE_PUBLISH", "NOW", "PUBLISH_NOW"})
_SCHEDULE_PUBLICATION_CAPABILITIES = frozenset({
    "CREATE_SCHEDULE",
    "FUTURE_PUBLISH",
    "MANAGE_SCHEDULE",
    "SCHEDULE",
    "SCHEDULE_PUBLISH",
})


def _canonical_publication_intent(value: Any) -> str:
    normalized = str(value or "").strip().upper().replace("-", "_")
    if normalized in _SCHEDULED_PUBLICATION_INTENTS:
        return "SCHEDULED_PUBLISH"
    if normalized in _DRAFT_PUBLICATION_INTENTS:
        return "DRAFT_ONLY"
    if normalized in _IMMEDIATE_PUBLICATION_INTENTS:
        return "IMMEDIATE_PUBLISH"
    return ""


def _publication_intent_from_constraints(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in ("publication_intent", "publication_mode", "content_state"):
        intent = _canonical_publication_intent(value.get(key))
        if intent:
            return intent
    if value.get("publish_now") is True:
        return "IMMEDIATE_PUBLISH"
    if value.get("schedule") is True or value.get("publish") is True:
        return "SCHEDULED_PUBLISH"
    if value.get("publish") is False or value.get("schedule") is False:
        return "DRAFT_ONLY"
    return ""


def _request_publication_intent(structured: StructuredCommandOutput) -> str:
    """Read only explicit request-level publication evidence.

    Capabilities, operation names, and temporal text are not publication
    intent.  If the Interpreter omitted the requirement, this boundary keeps
    it omitted instead of inventing one for segmentation.
    """

    for container in (
        structured.constraints,
        structured.parameters,
        structured.entities,
    ):
        intent = _publication_intent_from_constraints(container)
        if intent:
            return intent
    for delta in structured.task_changes or ():
        desired = getattr(delta, "desired_changes", None) or {}
        if not isinstance(desired, Mapping):
            continue
        intent = _publication_intent_from_constraints(desired)
        if intent:
            return intent
    return ""


def _item_publication_evidence(item: Mapping[str, Any]) -> str:
    return _publication_intent_from_constraints(item.get("constraints"))


def _materialize_request_publication_constraints(
    structured: StructuredCommandOutput,
) -> StructuredCommandOutput:
    """Preserve a structured future-publication requirement on each item."""

    if structured.command != CommandType.CREATE:
        return structured
    request_intent = _request_publication_intent(structured)
    payload = structured.model_dump(mode="python")
    raw_items = list(payload.get("items") or ())
    if not raw_items:
        return structured
    items = [dict(item) for item in raw_items]
    evidence = [_item_publication_evidence(item) for item in items]
    has_scheduled_item = "SCHEDULED_PUBLISH" in evidence
    if request_intent not in {"", "SCHEDULED_PUBLISH"}:
        return structured
    if request_intent != "SCHEDULED_PUBLISH" and not has_scheduled_item:
        return structured
    has_item_ownership = any(evidence)
    if has_item_ownership:
        target_indexes = [
            index for index, intent in enumerate(evidence)
            if intent == "SCHEDULED_PUBLISH"
        ]
    else:
        target_indexes = list(range(len(items)))

    request_constraints = dict(payload.get("constraints") or {})
    request_constraints.setdefault("publication_intent", "SCHEDULED_PUBLISH")
    payload["constraints"] = request_constraints
    capabilities = [str(value).upper() for value in (payload.get("required_capabilities") or ())]
    if "SCHEDULE_PUBLISH" not in capabilities:
        capabilities.append("SCHEDULE_PUBLISH")
    payload["required_capabilities"] = list(dict.fromkeys(capabilities))

    unresolved = False
    for index, item in enumerate(items):
        if index not in target_indexes:
            continue
        item_constraints = dict(item.get("constraints") or {})
        item_intent = _publication_intent_from_constraints(item_constraints)
        if item_intent in {"DRAFT_ONLY", "IMMEDIATE_PUBLISH"}:
            continue
        item_constraints.setdefault("publication_intent", "SCHEDULED_PUBLISH")
        item["constraints"] = item_constraints
        item_capabilities = [str(value).upper() for value in (item.get("capabilities") or ())]
        if "SCHEDULE_PUBLISH" not in item_capabilities:
            item_capabilities.append("SCHEDULE_PUBLISH")
        item["capabilities"] = list(dict.fromkeys(item_capabilities))
        if not any(
            str(item.get(key) or "").strip()
            for key in ("temporal_text", "run_at", "publish_at", "scheduled_at")
        ) and not any(
            str(item_constraints.get(key) or "").strip()
            for key in ("temporal_text", "run_at", "publish_at", "scheduled_at")
        ):
            unresolved = True
    if unresolved:
        payload["needs_clarification"] = True
    payload["items"] = items
    return StructuredCommandOutput.model_validate(payload)


def _normalize_multi_objective_items(
    structured: StructuredCommandOutput,
) -> StructuredCommandOutput:
    """Keep one atomic business deliverable per CommandItem.

    This is a shape repair only: it never infers targets from user-language
    keywords and never changes capability steps inside an item.  The common
    malformed shape is a single item paired with multiple structured
    ``CREATE_TASK`` deltas, or a single item carrying parallel title/time
    arrays.  Both can be split without another planning pass.
    """
    payload = structured.model_dump(mode="python")
    items = list(payload.get("items") or ())
    deltas = [
        delta for delta in (payload.get("task_changes") or ())
        if str(delta.get("operation", "")).upper() == "CREATE_TASK"
    ]
    single_delta_desired = dict(deltas[0].get("desired_changes") or {}) if len(deltas) == 1 else {}
    has_parallel_delta = any(
        key in {"titles", "topics", "temporal_texts", "publish_times", "deliverables", "items"}
        and isinstance(value, list) and len(value) > 1
        for key, value in single_delta_desired.items()
    )
    if len(deltas) > len(items) and not has_parallel_delta:
        repaired: list[dict[str, Any]] = []
        for delta in deltas:
            desired = dict(delta.get("desired_changes") or {})
            constraints = dict(desired.get("constraints") or {})
            repaired.append({
                "title": str(desired.get("title") or desired.get("topic") or desired.get("description") or ""),
                "topic": str(desired.get("topic") or desired.get("title") or ""),
                "requirements": list(desired.get("requirements") or ()),
                "operation": "CREATE",
                "capabilities": list(desired.get("required_capabilities") or desired.get("capabilities") or ()),
                "temporal_text": str(
                    desired.get("temporal_text") or constraints.get("temporal_text")
                    or desired.get("publish_at") or desired.get("run_at")
                    or constraints.get("publish_at") or constraints.get("run_at") or ""
                ),
                "constraints": constraints,
            })
        items = repaired
    elif len(items) == 1 or (not items and has_parallel_delta):
        item = dict(items[0]) if items else {}
        constraints = dict(item.get("constraints") or {})
        list_fields = {
            key: value for key, value in constraints.items()
            if isinstance(value, list) and len(value) > 1
        }
        # Some providers keep the parallel deliverables inside the single
        # CREATE_TASK delta instead of the CommandItem constraints.  This is
        # still a structural shape repair: only explicitly parallel business
        # fields qualify; capability arrays remain one item's step list.
        if len(deltas) == 1:
            desired = dict(deltas[0].get("desired_changes") or {})
            for key, value in desired.items():
                if key in {"titles", "topics", "temporal_texts", "publish_times", "deliverables", "items"} and isinstance(value, list) and len(value) > 1:
                    list_fields.setdefault(key, value)
        if list_fields:
            count = max(len(value) for value in list_fields.values())
            split_items: list[dict[str, Any]] = []
            for index in range(count):
                child = dict(item)
                child_constraints = {
                    key: (value[index] if index < len(value) else value[-1])
                    if key in list_fields else value
                    for key, value in constraints.items()
                }
                child["constraints"] = child_constraints
                for source, target in (("titles", "title"), ("topics", "topic"), ("temporal_texts", "temporal_text"), ("publish_times", "temporal_text")):
                    values = list_fields.get(source)
                    if values:
                        child[target] = str(values[index] if index < len(values) else values[-1])
                deliverables = list_fields.get("deliverables") or list_fields.get("items")
                if deliverables:
                    value = deliverables[index] if index < len(deliverables) else deliverables[-1]
                    if isinstance(value, Mapping):
                        child.update({
                            key: value[key]
                            for key in ("title", "topic", "requirements", "temporal_text", "constraints", "capabilities")
                            if key in value
                        })
                split_items.append(child)
            items = split_items
    if items != payload.get("items"):
        payload["items"] = items
        return StructuredCommandOutput.model_validate(payload)
    return structured


def _ensure_create_item(structured: StructuredCommandOutput) -> StructuredCommandOutput:
    """Keep the CREATE contract atomic when a provider omits ``items``.

    This is a schema projection, not language classification: an explicit
    CREATE with no item still denotes one final business entity.  All business
    fields come from the already validated structured envelope.
    """
    if structured.command != CommandType.CREATE or structured.items:
        return structured
    payload = structured.model_dump(mode="python")
    parameters = dict(payload.get("parameters") or {})
    entities = dict(payload.get("entities") or {})
    title = str(
        parameters.get("title")
        or parameters.get("topic")
        or entities.get("title")
        or entities.get("topic")
        or payload.get("goal")
        or payload.get("objective")
        or ""
    ).strip()
    temporal_text = str(
        parameters.get("temporal_text")
        or parameters.get("run_at")
        or parameters.get("publish_at")
        or ""
    ).strip()
    payload["items"] = [{
        "title": title,
        "topic": str(parameters.get("topic") or entities.get("topic") or title),
        "requirements": [],
        "operation": "CREATE",
        "capabilities": list(payload.get("required_capabilities") or ()),
        "temporal_text": temporal_text,
        "constraints": dict(payload.get("constraints") or {}),
    }]
    return StructuredCommandOutput.model_validate(payload)


def _looks_like_merged_multi_objective(
    user_input: str,
    structured: StructuredCommandOutput,
) -> bool:
    """Detect only explicit parallel fields in one returned item."""
    if len(structured.items) != 1:
        return False
    if structured.command not in {CommandType.CREATE, CommandType.MODIFY}:
        return False
    item = structured.items[0].model_dump(mode="python")
    constraints = item.get("constraints") or {}
    return any(
        isinstance(value, list) and len(value) > 1
        for key, value in constraints.items()
        if key in {"titles", "topics", "temporal_texts", "publish_times", "deliverables", "items"}
    )


def _strip_unknown_command_fields(payload: Any) -> Any:
    """Drop top-level keys the Command schema does not own.

    A reasoning model occasionally echoes envelope fields while expressing
    several independent tasks; stripping them lets the owned fields (including
    ``task_changes``) validate instead of failing the whole request.
    """

    if not isinstance(payload, Mapping):
        return payload
    allowed = set(StructuredCommandOutput.model_fields)
    stripped = {key: value for key, value in payload.items() if key in allowed}
    return stripped if stripped != dict(payload) else payload


# Common model output variants for the coarse operation enum.  The schema only
# owns CREATE/MODIFY/CANCEL/QUERY/CONTROL; models that translate the user
# phrasing ("发布" → PUBLISH, "安排" → SCHEDULE) need a deterministic mapping.
_COMMAND_ENUM_ALIASES: dict[str, str] = {
    "PUBLISH": "CREATE",
    "PUBLISH_NOW": "CREATE",
    "SCHEDULE": "CREATE",
    "SCHEDULED_PUBLISH": "CREATE",
    "CREATE_POST": "CREATE",
    "CREATE_DRAFT": "CREATE",
    "WRITE": "CREATE",
    "POST": "CREATE",
    "GENERATE": "CREATE",
    "SEARCH": "QUERY",
    "FIND": "QUERY",
    "LOOKUP": "QUERY",
    "LIST": "QUERY",
    "ANALYZE": "QUERY",
    "UPDATE": "MODIFY",
    "EDIT": "MODIFY",
    "CHANGE": "MODIFY",
    "REVISE": "MODIFY",
    "DELETE": "CANCEL",
    "REMOVE": "CANCEL",
    "STOP": "CANCEL",
    "PAUSE": "CONTROL",
    "RESUME": "CONTROL",
}

_DELTA_OPERATION_ALIASES: dict[str, str] = {
    "CREATE": "CREATE_TASK",
    "CREATE_TASKS": "CREATE_TASK",
    "ADD": "ADD_GOAL",
    "ADD_GOALS": "ADD_GOAL",
    "UPDATE": "UPDATE_GOAL",
    "EDIT": "UPDATE_GOAL",
    "CANCEL": "CANCEL_GOAL",
    "DELETE": "CANCEL_GOAL",
    "REMOVE": "CANCEL_GOAL",
    "CONTINUE": "CONTINUE_TASK",
}


def _repair_command_output(payload: Any) -> Any:
    """Deterministically normalize known schema violations in model output.

    Never invents business facts: it only coerces the shape of fields the
    model already emitted (enum variants, container types, per-delta extra
    keys), so a harmless phrasing difference cannot fail the whole request.
    """

    if not isinstance(payload, Mapping):
        return payload
    repaired = dict(payload)

    command = repaired.get("command")
    if isinstance(command, str):
        normalized = command.strip().upper().replace("-", "_").replace(" ", "_")
        repaired["command"] = _COMMAND_ENUM_ALIASES.get(normalized, normalized)

    for key in ("constraints", "parameters", "entities"):
        value = repaired.get(key)
        if value is None or isinstance(value, str) or not isinstance(value, Mapping):
            repaired[key] = {}

    capabilities = repaired.get("required_capabilities")
    if isinstance(capabilities, (list, tuple)):
        repaired["required_capabilities"] = [
            str(item).strip() for item in capabilities
            if isinstance(item, (str, int, float)) and str(item).strip()
        ]
    elif capabilities is None:
        repaired["required_capabilities"] = []

    changes = repaired.get("task_changes")
    if isinstance(changes, list):
        cleaned: list[Any] = []
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            delta = dict(change)
            operation = delta.get("operation")
            if isinstance(operation, str):
                normalized = operation.strip().upper().replace("-", "_").replace(" ", "_")
                delta["operation"] = _DELTA_OPERATION_ALIASES.get(normalized, normalized)
            allowed_delta = set(TaskDelta.model_fields)
            cleaned.append(
                {key: value for key, value in delta.items() if key in allowed_delta}
            )
        repaired["task_changes"] = cleaned
    elif changes is None:
        repaired["task_changes"] = []

    target = repaired.get("target")
    if isinstance(target, Mapping):
        kind = target.get("kind")
        if isinstance(kind, str):
            target = dict(target)
            target["kind"] = str(kind).strip().upper().replace("-", "_")
            repaired["target"] = target

    return repaired if repaired != dict(payload) else payload


def _validation_summary(original: Any, repaired: Any) -> str:
    """Bounded human-readable summary of what still fails validation."""

    try:
        StructuredCommandOutput.model_validate(original)
        return "output still invalid after normalization"
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        return details[:1200] or "unknown validation error"


__all__ = [
    "CommandInterpretationError",
    "CommandInterpreter",
    "LLMCommandInterpreter",
]
