"""Production semantic evaluation for the structured baseline cases.

This module is intentionally an adapter, not a second interpreter.  It calls
the same CommandInterpreter and TurnCoordinator semantic resolution methods
used by the Agent API and only projects their typed result into the existing
EvaluationRunner payload shape.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import canonical_semantic_result
from .models import EvalCase, EvaluationReport
from .runner import EvaluationRunner


class ProductionSemanticAdapter:
    """Drive the production semantic path for one ``EvalCase``."""

    def __init__(
        self,
        *,
        llm: Any | None = None,
        model: str = "",
        command_interpreter: Any | None = None,
        turn_coordinator: Any | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> None:
        if command_interpreter is None:
            from greenbook_agent_core.command import CommandInterpreter

            command_interpreter = CommandInterpreter(llm=llm, model=model)
        if turn_coordinator is None:
            from greenbook_agent_api.services.turn_coordinator import TurnCoordinator

            turn_coordinator = TurnCoordinator(command_runtime=command_interpreter)
        self._llm = llm
        self._model = model
        self._interpreter = command_interpreter
        self._coordinator = turn_coordinator
        self._timezone = timezone

    async def run_case(self, case: EvalCase) -> dict[str, Any]:
        from greenbook_agent_core.command.models import CommandContext
        from greenbook_agent_core.command.interpreter import CommandInterpretationError

        history = _previous_turns(case)
        context = CommandContext(
            conversation_id=f"evaluation-{case.case_id}",
            timezone=self._timezone,
            history=history,
            active_tasks=list(case.setup_context.get("active_tasks") or ()),
            unfinished_goals=list(case.setup_context.get("unfinished_goals") or ()),
            targets=list(case.setup_context.get("targets") or ()),
            metadata=dict(case.setup_context.get("metadata") or {}),
        )
        try:
            command = await self._interpreter.interpret(
                case.user_message,
                context,
                llm=self._llm,
                model=self._model,
                run_id=f"semantic-{case.case_id}",
            )
        except CommandInterpretationError as exc:
            # Invalid/incomplete input is a production interaction outcome,
            # not an EvaluationRunner execution error.  Preserve the
            # interpreter's error code while projecting the stable contract.
            if str(getattr(exc, "code", "")).startswith("COMMAND_"):
                return {
                    "semantic_state": {
                        "action_family": "INVALID",
                        "publication_mode": "NONE",
                        "temporal_kind": "NONE",
                        "temporal_resolved": False,
                        "target_state": "NONE",
                        "clarification_required": True,
                        "objective_count": None,
                        "task_expectation": "CLARIFY",
                    },
                    "raw_semantic_state": {},
                    "temporal_resolved": False,
                    "clarification": True,
                    "objective_count": None,
                    "task_state": "WAITING_USER",
                    "command": {},
                    "resolved_semantics": {},
                    "target": {},
                    "error_code": getattr(exc, "code", ""),
                    "error": str(exc),
                    "trace": {
                        "conversation_id": context.conversation_id,
                        "task_id": "",
                        "goal_id": "",
                        "events": [],
                    },
                }
            raise
        target_resolution = await self._coordinator._resolve_target(  # noqa: SLF001
            command,
            context,
        )
        semantic_state = self._coordinator._resolve_semantic_state(  # noqa: SLF001
            command,
            target_resolution=target_resolution,
            timezone=self._timezone,
        )
        state = semantic_state.model_dump(mode="json")
        canonical = canonical_semantic_result(state, command)
        clarification = bool(canonical["clarification_required"])
        return {
            # ``semantic_state`` is the stable product contract.  The raw
            # production model remains available for diagnosis but is never
            # used as the expected golden schema.
            "semantic_state": canonical,
            "raw_semantic_state": state,
            "temporal_resolved": bool(canonical["temporal_resolved"]),
            "clarification": clarification,
            "objective_count": canonical["objective_count"],
            # This is a semantic readiness projection only.  It is not a
            # claim about a persisted Task; durable Task status belongs to the
            # Runtime adapter below.
            "task_state": "WAITING_USER" if clarification else "COMPLETED",
            "command": command.model_dump(mode="json"),
            "resolved_semantics": state,
            "target": dict(state.get("resolved_target") or {}),
            "trace": {
                "conversation_id": context.conversation_id,
                "task_id": "",
                "goal_id": "",
                "events": [],
            },
        }


class SemanticEvaluator:
    """Evaluate semantic cases through the production adapter."""

    def __init__(
        self,
        adapter: ProductionSemanticAdapter | Any,
        *,
        badcase_store: Any | None = None,
    ) -> None:
        self.adapter = adapter
        self.badcase_store = badcase_store

    async def evaluate(
        self,
        cases: Sequence[EvalCase],
        *,
        run_id: str = "semantic-production",
    ) -> EvaluationReport:
        runner = EvaluationRunner(
            runtime=self.adapter,
            badcase_store=self.badcase_store,
        )
        return await runner.run_cases(cases, run_id=run_id)

    def evaluate_sync(
        self,
        cases: Sequence[EvalCase],
        *,
        run_id: str = "semantic-production",
    ) -> EvaluationReport:
        return asyncio.run(self.evaluate(cases, run_id=run_id))


def _previous_turns(case: EvalCase) -> list[dict[str, Any]]:
    turns = [dict(turn) for turn in (case.conversation_turns or ())]
    if not turns:
        return []
    last_content = str(turns[-1].get("content") or "")
    if last_content.strip() == str(case.user_message or "").strip():
        return turns[:-1]
    return turns


__all__ = ["ProductionSemanticAdapter", "SemanticEvaluator"]
