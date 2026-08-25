"""Runtime Dynamic Planner contracts.

The planner receives already-understood Goals and runtime evidence.  It may
emit a typed decision, but it does not execute tools, mutate persistence, or
interpret the original user message.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from greenbook_contracts.tool_contract import ToolMetadata
from pydantic import ValidationError

from greenbook_agent_core.goal.models import GoalTree
from greenbook_agent_core.llm_compat import (
    extract_top_level_json,
    structured_call,
)
from greenbook_agent_core.planning.contracts import PlanningDecision, PlanningDecisionType

DecisionMaker = Callable[..., PlanningDecision | Mapping[str, Any] | Awaitable[Any]]


class DynamicPlanner:
    """Choose the next plan mutation from structured runtime evidence."""

    def __init__(
        self,
        *,
        llm: Any | None = None,
        model: str = "",
        decision_maker: DecisionMaker | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._decision_maker = decision_maker

    async def decide(
        self,
        goal_tree: GoalTree,
        agent_state: Any,
        task: Any | None = None,
        tool_catalog: Sequence[ToolMetadata] = (),
        execution_history: Sequence[Mapping[str, Any]] = (),
        observations: Sequence[Mapping[str, Any]] = (),
        context_snapshot: Any | None = None,
        llm: Any | None = None,
        model: str | None = None,
    ) -> PlanningDecision:
        goal_tree.validate_tree()
        payload = self._payload(
            goal_tree=goal_tree,
            agent_state=agent_state,
            task=task,
            tool_catalog=tool_catalog,
            execution_history=execution_history,
            observations=observations,
            context_snapshot=context_snapshot,
        )
        if self._decision_maker is not None:
            raw = self._decision_maker(payload)
            raw = await raw if inspect.isawaitable(raw) else raw
            decision = await self._repair_empty_observation(
                payload,
                PlanningDecision.model_validate(raw),
                llm=llm,
                model=model if model is not None else self._model,
            )
            decision = _enforce_empty_observation_policy(payload, decision)
            return await self._repair_unavailable_retry(
                payload,
                decision,
                llm=llm,
                model=model if model is not None else self._model,
            )

        client = llm or self._llm
        if client is not None:
            response = await _structured_call(
                client,
                model if model is not None else self._model,
                payload,
            )
            try:
                decision = PlanningDecision.model_validate(_response_payload(response))
            except (ValueError, ValidationError) as exc:
                # Bounded structured-output recovery: give the model one explicit
                # schema repair before failing closed.  A planner JSON/schema
                # error must never leak a Pydantic trace to the user or kill the
                # Run (design 0813, §21).  The repair is schema-only, not a replan.
                summary = str(exc)
                if "no structured response" not in summary.lower():
                    repair_payload = dict(payload)
                    repair_payload["contract_repair"] = {
                        "validation_error": summary[:1000],
                        "instruction": (
                            "Return only valid JSON matching the PlanningDecision "
                            "schema. insert_nodes entries must be TaskNode objects "
                            "with exactly task_id and capability; do not include "
                            "Goal-only fields like description, goal_type, children, "
                            "required_capabilities, or target. Do not explain."
                        ),
                    }
                    try:
                        response = await _structured_call(
                            client,
                            model if model is not None else self._model,
                            repair_payload,
                        )
                        decision = PlanningDecision.model_validate(
                            _response_payload(response)
                        )
                    except (ValueError, ValidationError):
                        return PlanningDecision(
                            decision=PlanningDecisionType.ASK_HUMAN,
                            reason="The replan decision could not be validated; please confirm the next step.",
                        )
                else:
                    # DeepSeek under a long prompt occasionally returns an empty
                    # content field without an HTTP error.  Give the provider one
                    # explicit JSON-only retry before failing closed.
                    response = await _structured_call(
                        client,
                        model if model is not None else self._model,
                        payload,
                        retry=True,
                    )
                    try:
                        decision = PlanningDecision.model_validate(
                            _response_payload(response)
                        )
                    except (ValueError, ValidationError):
                        return PlanningDecision(
                            decision=PlanningDecisionType.ASK_HUMAN,
                            reason="The replan decision could not be validated; please confirm the next step.",
                        )
            decision = await self._repair_empty_observation(
                payload,
                decision,
                llm=client,
                model=model if model is not None else self._model,
            )
            decision = _enforce_empty_observation_policy(payload, decision)
            return await self._repair_unavailable_retry(
                payload,
                decision,
                llm=client,
                model=model if model is not None else self._model,
            )

        decision = self._evidence_fallback(payload)
        broadened = _broaden_empty_search(payload)
        if broadened is not None:
            decision = broadened
        return _enforce_empty_observation_policy(payload, decision)

    async def _repair_empty_observation(
        self,
        payload: Mapping[str, Any],
        decision: PlanningDecision,
        *,
        llm: Any | None,
        model: str,
    ) -> PlanningDecision:
        """Give an EMPTY read one typed opportunity to adapt its evidence query.

        The first planner response may incorrectly continue after an empty
        collection.  Ask the same planner boundary to choose a different
        read-only tool or explicitly changed arguments before the runtime
        fails closed.  No tool, query, or fallback is synthesized here.
        """

        if not _empty_observation_requires_adaptation(payload):
            return decision

        latest = _latest_observation(payload)
        last_result = latest.get("last_result") or {}
        if not isinstance(last_result, Mapping):
            last_result = {}
        failed_tool = str(
            last_result.get("tool_name") or latest.get("tool_name") or ""
        )
        failed_arguments = last_result.get("tool_arguments") or {}
        if not isinstance(failed_arguments, Mapping):
            failed_arguments = {}
        candidates = [
            str(item)
            for item in (latest.get("available_fallback_capabilities") or [])
            if item
        ]
        if not failed_tool and not failed_arguments and not candidates:
            return decision

        # ── Deterministic query broadening for empty searches ────────────
        # A community search that returns zero rows is usually a phrasing
        # problem ("Java 后端 面试" vs the community's "Java 学习路线图"),
        # not missing content.  Walk a bounded widening ladder — drop
        # trailing tokens, then fall back to the broadest single token —
        # before trusting the LLM (or giving up with EVIDENCE_INSUFFICIENT).
        # The next AgentLoop iteration replays the search through
        # ``preferred_tool`` without an extra LLM round trip.
        broadened = _broaden_empty_search(payload)
        if broadened is not None:
            return broadened

        if decision.decision == PlanningDecisionType.SELECT_ALTERNATIVE_TOOL:
            return decision if decision.tool_name in candidates else PlanningDecision(
                decision=PlanningDecisionType.ASK_HUMAN,
                reason="The proposed empty-result alternative is not a catalogued read-only tool.",
            )
        if decision.decision == PlanningDecisionType.RETRY_WITH_NEW_ARGS:
            if _arguments_change_scope(failed_arguments, decision.arguments):
                return decision.model_copy(
                    update={"tool_name": decision.tool_name or failed_tool}
                )
            return PlanningDecision(
                decision=PlanningDecisionType.ASK_HUMAN,
                reason="The empty-result retry did not change the evidence query.",
            )
        if decision.decision == PlanningDecisionType.ASK_HUMAN or llm is None:
            return decision

        repair_payload = dict(payload)
        repair_payload["planning_constraint"] = {
            "empty_result_adaptation_required": True,
            "failed_tool": failed_tool,
            "failed_arguments": dict(failed_arguments),
            "allowed_read_only_alternatives": candidates,
            "instruction": (
                "The read returned EMPTY. Do not continue or finish. Select a "
                "different listed read-only tool, or RETRY_WITH_NEW_ARGS using "
                "the same read tool only with explicitly broadened or narrowed "
                "arguments. If neither is evidence-bounded, choose ASK_HUMAN. "
                "Never invent a resource identifier or fabricate data."
            ),
        }
        try:
            response = await _structured_call(llm, model, repair_payload)
            repaired = PlanningDecision.model_validate(_response_payload(response))
        except (ValidationError, ValueError):
            return PlanningDecision(
                decision=PlanningDecisionType.ASK_HUMAN,
                reason="The empty result requires an evidence-bounded replan, but the replan could not be validated.",
            )
        if repaired.decision == PlanningDecisionType.SELECT_ALTERNATIVE_TOOL:
            if repaired.tool_name in candidates:
                return repaired
        elif repaired.decision == PlanningDecisionType.RETRY_WITH_NEW_ARGS:
            if _arguments_change_scope(failed_arguments, repaired.arguments):
                return repaired.model_copy(
                    update={"tool_name": repaired.tool_name or failed_tool}
                )
        elif repaired.decision == PlanningDecisionType.ASK_HUMAN:
            return repaired
        return PlanningDecision(
            decision=PlanningDecisionType.ASK_HUMAN,
            reason="The empty result did not produce a validated evidence-bounded replan.",
        )

    async def _repair_unavailable_retry(
        self,
        payload: Mapping[str, Any],
        decision: PlanningDecision,
        *,
        llm: Any | None,
        model: str,
    ) -> PlanningDecision:
        """Reject a same-tool retry when evidence exposes safe alternatives.

        This is a generic contract repair, not a capability-to-tool mapping:
        the model must choose one of the read-only candidates already present
        in ``available_fallback_capabilities`` or explicitly ask the user.
        """

        latest = _latest_observation(payload)
        failure_kind = str(latest.get("failure_kind") or "").upper()
        candidates = [
            str(item)
            for item in (latest.get("available_fallback_capabilities") or [])
            if item
        ]
        failed_result = latest.get("last_result") or {}
        failed_tool = str(
            failed_result.get("tool_name")
            if isinstance(failed_result, Mapping)
            else ""
        )
        failed_arguments = (
            failed_result.get("tool_arguments", {})
            if isinstance(failed_result, Mapping)
            else {}
        )
        if not isinstance(failed_arguments, Mapping):
            failed_arguments = {}
        if failure_kind in {
            "PERMANENT_INPUT",
            "FIELD_TOO_LONG",
            "INVALID_DRAFT_METADATA",
            "VALIDATION_ERROR",
            "INVALID_ARGUMENT",
        }:
            return PlanningDecision(
                decision=PlanningDecisionType.ASK_HUMAN,
                reason=(
                    "The action arguments are permanently invalid; the runtime "
                    "will not retry the same side effect or invent a correction."
                ),
            )
        if failure_kind not in {
            "DEPENDENCY_UNAVAILABLE",
            "TIMEOUT",
            "TRANSIENT_NETWORK",
            "RATE_LIMITED",
        }:
            return decision
        valid_alternative = (
            decision.decision == PlanningDecisionType.SELECT_ALTERNATIVE_TOOL
            and decision.tool_name in candidates
        )
        bounded_scope_retry = (
            decision.decision == PlanningDecisionType.RETRY_WITH_NEW_ARGS
            and _arguments_change_scope(
                failed_arguments,
                decision.arguments,
            )
        )
        if (
            valid_alternative
            or bounded_scope_retry
            or decision.decision == PlanningDecisionType.ASK_HUMAN
        ):
            if bounded_scope_retry and not decision.tool_name:
                return decision.model_copy(update={"tool_name": failed_tool})
            return decision
        if decision.decision not in {
            PlanningDecisionType.CONTINUE,
            PlanningDecisionType.RETRY_WITH_NEW_ARGS,
            PlanningDecisionType.SELECT_ALTERNATIVE_TOOL,
        }:
            return decision

        repair_payload = dict(payload)
        repair_payload["planning_constraint"] = {
            "failed_tool": failed_tool,
            "failed_arguments": dict(failed_arguments),
            "failure_kind": failure_kind,
            "blind_same_tool_retry_forbidden": True,
            "allowed_read_only_alternatives": candidates,
            "instruction": (
                "The failed read dependency is unavailable. Select one different "
                "read-only tool from allowed_read_only_alternatives if it can "
                "produce bounded evidence for the goal. If no equivalent tool is "
                "suitable, a RETRY_WITH_NEW_ARGS decision is allowed only when its "
                "arguments explicitly broaden or narrow the query/scope; otherwise "
                "choose ASK_HUMAN. Do not blindly repeat failed_tool and do not "
                "invent a tool name."
            ),
        }
        if llm is None:
            return PlanningDecision(
                decision=PlanningDecisionType.ASK_HUMAN,
                reason=(
                    "The read dependency failed and no evidence-bounded alternative "
                    "could be selected safely."
                ),
            )
        try:
            response = await _structured_call(llm, model, repair_payload)
            repaired = PlanningDecision.model_validate(_response_payload(response))
        except (ValidationError, ValueError):
            return PlanningDecision(
                decision=PlanningDecisionType.ASK_HUMAN,
                reason=(
                    "The read dependency failed and the alternative-tool decision "
                    "could not be validated safely."
                ),
            )
        if (
            repaired.decision == PlanningDecisionType.SELECT_ALTERNATIVE_TOOL
            and repaired.tool_name in candidates
            and repaired.tool_name != failed_tool
        ):
            return repaired
        if repaired.decision == PlanningDecisionType.RETRY_WITH_NEW_ARGS and _arguments_change_scope(
            failed_arguments,
            repaired.arguments,
        ):
            if not repaired.tool_name:
                return repaired.model_copy(update={"tool_name": failed_tool})
            return repaired
        if repaired.decision == PlanningDecisionType.ASK_HUMAN:
            return repaired
        return PlanningDecision(
            decision=PlanningDecisionType.ASK_HUMAN,
            reason=(
                "The read dependency failed and the proposed alternative was not "
                "a different catalogued read-only tool."
            ),
        )

    async def replan(self, *args: Any, **kwargs: Any) -> PlanningDecision:
        """Explicit spelling used by AgentLoop after Reflect."""

        return await self.decide(*args, **kwargs)

    async def plan(self, *args: Any, **kwargs: Any) -> PlanningDecision:
        """Planner-oriented alias for integrations that call ``plan``."""

        return await self.decide(*args, **kwargs)

    @staticmethod
    def apply(goal_tree: GoalTree, decision: PlanningDecision) -> GoalTree:
        """Apply a typed, non-executing plan mutation to a GoalTree copy."""

        _validate_insertion_publication_semantics(goal_tree, decision)
        candidate = goal_tree.model_copy(deep=True)
        if decision.decision == PlanningDecisionType.INSERT_STEP:
            existing = {node.task_id for node in candidate.task_nodes}
            candidate.task_nodes.extend(
                node for node in decision.insert_nodes if node.task_id not in existing
            )
        elif decision.decision == PlanningDecisionType.REMOVE:
            removed = set(decision.remove_task_ids)
            candidate.task_nodes = [
                node for node in candidate.task_nodes if node.task_id not in removed
            ]
        elif decision.decision == PlanningDecisionType.REORDER and decision.task_order:
            order = {task_id: index for index, task_id in enumerate(decision.task_order)}
            candidate.task_nodes.sort(key=lambda node: order.get(node.task_id, len(order)))
        elif decision.decision == PlanningDecisionType.RETRY_WITH_NEW_ARGS:
            for node in candidate.task_nodes:
                if decision.task_id and node.task_id != decision.task_id:
                    continue
                if decision.goal_id and node.goal_id != decision.goal_id:
                    continue
                node.inputs = {**node.inputs, **decision.arguments}
                node.status = "PENDING"
                break
        if decision.decision in {
            PlanningDecisionType.INSERT_STEP,
            PlanningDecisionType.REMOVE,
            PlanningDecisionType.REORDER,
            PlanningDecisionType.RETRY_WITH_NEW_ARGS,
        }:
            candidate.version = goal_tree.version + 1
        candidate.validate_tree()
        return candidate

    apply_decision = apply

    @staticmethod
    def _payload(
        *,
        goal_tree: GoalTree,
        agent_state: Any,
        task: Any | None,
        tool_catalog: Sequence[ToolMetadata],
        execution_history: Sequence[Mapping[str, Any]],
        observations: Sequence[Mapping[str, Any]],
        context_snapshot: Any | None,
    ) -> dict[str, Any]:
        def dump(value: Any) -> Any:
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json")
            if isinstance(value, Mapping):
                return dict(value)
            return value

        return {
            "goal_tree": dump(goal_tree),
            "agent_state": dump(agent_state),
            "task": dump(task) if task is not None else {},
            "tool_metadata": [dump(item) for item in tool_catalog],
            "execution_history": [dump(item) for item in execution_history],
            "observations": [dump(item) for item in observations],
            "context_snapshot": dump(context_snapshot) if context_snapshot is not None else {},
        }

    @staticmethod
    def _evidence_fallback(payload: Mapping[str, Any]) -> PlanningDecision:
        """Safe no-LLM behavior based only on typed runtime evidence."""

        observations = payload.get("observations") or []
        latest = observations[-1] if observations else {}
        if hasattr(latest, "model_dump"):
            latest = latest.model_dump(mode="json")
        if not isinstance(latest, Mapping):
            latest = {}
        last_result = latest.get("last_result") or {}
        if isinstance(last_result, Mapping) and last_result.get("status") == "WAITING_HUMAN":
            return PlanningDecision(
                decision=PlanningDecisionType.ASK_HUMAN,
                reason="Runtime evidence requires human input.",
            )
        if isinstance(last_result, Mapping) and (
            last_result.get("ok") is False or last_result.get("success") is False
        ):
            failure_kind = str(
                last_result.get("code") or latest.get("failure_kind") or ""
            ).upper()
            if failure_kind in {
                "PERMANENT_INPUT",
                "FIELD_TOO_LONG",
                "INVALID_DRAFT_METADATA",
                "VALIDATION_ERROR",
                "INVALID_ARGUMENT",
            }:
                return PlanningDecision(
                    decision=PlanningDecisionType.ASK_HUMAN,
                    reason=(
                        "The action arguments are invalid; same-argument retry is forbidden."
                    ),
                )
            tool_name = str(
                last_result.get("tool_name")
                or latest.get("tool_name")
                or ""
            )
            policy = _policy_for_tool(payload.get("tool_metadata") or [], tool_name)
            if policy is not None:
                side_effect = policy.get("side_effect") or {}
                if not isinstance(side_effect, Mapping):
                    side_effect = {}
                if bool(side_effect.get("destructive")) or not bool(
                    side_effect.get("idempotent", True)
                ):
                    return PlanningDecision(
                        decision=PlanningDecisionType.ASK_HUMAN,
                        reason=(
                            "The failed side-effecting action is not safe to repeat; "
                            "re-observe the external state or request human confirmation."
                        ),
                        tool_name=tool_name,
                    )
                if bool(side_effect.get("has_side_effect")) or last_result.get("request_sent") is not False:
                    return PlanningDecision(
                        decision=PlanningDecisionType.CONTINUE,
                        reason=(
                            "The action may have reached an external system; "
                            "re-observe before changing the plan or attempting it again."
                        ),
                        tool_name=tool_name,
                    )
            return PlanningDecision(
                decision=PlanningDecisionType.RETRY_WITH_NEW_ARGS,
                reason="The latest action failed; a new bounded attempt may be planned.",
                tool_name=tool_name,
            )
        return PlanningDecision(
            decision=PlanningDecisionType.CONTINUE,
            reason="No evidence requires a plan mutation.",
        )


def _validate_insertion_publication_semantics(
    goal_tree: GoalTree,
    decision: PlanningDecision,
) -> None:
    """Reject an INSERT_STEP that changes a Goal's publication semantics.

    Replan is a local execution-structure mutation. It must never introduce a
    publication capability the Goal did not declare: a DRAFT_ONLY Goal cannot
    gain SCHEDULE_PUBLISH or PUBLISH_NOW, a scheduled Goal cannot gain
    PUBLISH_NOW, and an immediate-publish Goal cannot gain SCHEDULE_PUBLISH.
    Mirrors GoalCompiler._validate_step_semantics so every path that changes
    the GoalTree preserves semantic monotonicity.
    """

    if decision.decision != PlanningDecisionType.INSERT_STEP:
        return
    goals_by_id = {goal.goal_id: goal for goal in goal_tree.all_goals()}
    for node in decision.insert_nodes:
        capability = str(getattr(node, "capability", "") or "").strip().upper()
        if capability not in {"SCHEDULE_PUBLISH", "PUBLISH_NOW"}:
            continue
        goal = goals_by_id.get(str(getattr(node, "goal_id", "") or ""))
        intent = _goal_publication_intent(goal) if goal is not None else ""
        if intent == "DRAFT_ONLY" and capability in {"SCHEDULE_PUBLISH", "PUBLISH_NOW"}:
            raise ValueError(
                f"INSERT_STEP '{node.task_id}' introduces {capability} into a DRAFT_ONLY goal."
            )
        if intent == "SCHEDULED_PUBLISH" and capability == "PUBLISH_NOW":
            raise ValueError(
                f"INSERT_STEP '{node.task_id}' introduces PUBLISH_NOW into a scheduled goal."
            )
        if intent == "IMMEDIATE_PUBLISH" and capability == "SCHEDULE_PUBLISH":
            raise ValueError(
                f"INSERT_STEP '{node.task_id}' introduces SCHEDULE_PUBLISH into an immediate-publish goal."
            )


def _goal_publication_intent(goal: Any) -> str:
    """Normalize a Goal's publication intent (field or constraints form)."""

    value = str(getattr(goal, "publication_intent", "") or "").strip()
    if not value:
        for item in getattr(goal, "constraints", ()) or ():
            if isinstance(item, Mapping) and item.get("publication_intent") not in (None, ""):
                value = str(item["publication_intent"]).strip()
                break
    normalized = value.upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "DRAFT": "DRAFT_ONLY",
        "SAVE_DRAFT": "DRAFT_ONLY",
        "DO_NOT_PUBLISH": "DRAFT_ONLY",
        "NO_PUBLISH": "DRAFT_ONLY",
        "SCHEDULE": "SCHEDULED_PUBLISH",
        "SCHEDULE_PUBLISH": "SCHEDULED_PUBLISH",
        "IMMEDIATE": "IMMEDIATE_PUBLISH",
        "PUBLISH_NOW": "IMMEDIATE_PUBLISH",
        "NOW": "IMMEDIATE_PUBLISH",
    }
    return aliases.get(normalized, normalized)


_PLANNER_PROMPT = (
    "You are GreenBook Dynamic Planner. Return only a typed "
    "PlanningDecision. Use GoalTree and runtime evidence; "
    "never execute tools or reinterpret the user message. "
    "Treat an EMPTY read result as evidence, not as success: inspect "
    "result_status, resource_count, missing_required_reference, "
    "failure_kind, and available_fallback_capabilities. For a safe "
    "read failure or empty result, choose SELECT_ALTERNATIVE_TOOL, "
    "RETRY_WITH_NEW_ARGS, a bounded lower-scope step, or ASK_HUMAN "
    "according to the supplied ToolMetadata. Do not choose a tool "
    "merely because it is first in the catalog, and never invent a "
    "missing resource identifier. "
    "When a read returned a non-empty result whose topic only partially "
    "matches the Goal, prefer to proceed with the available evidence "
    "(extract the related themes and continue) or a materially changed "
    "query scope, rather than ASK_HUMAN. ASK_HUMAN is for Goals with no "
    "usable evidence at all or a genuine approval need — not for imperfect "
    "topic matches."
    "For DEPENDENCY_UNAVAILABLE, TIMEOUT, TRANSIENT_NETWORK, or "
    "RATE_LIMITED failures, prefer a different listed read-only "
    "candidate; if none is semantically suitable, a retry is allowed "
    "only with explicit changed arguments that adjust scope. Never "
    "blindly repeat the failed tool. "
    "When an external side effect may already have been sent, "
    "re-observe or reconcile before retrying. For destructive "
    "or non-idempotent failures, ask for human input instead "
    "of blindly repeating the tool. Preserve independent "
    "parallel work. Represent conditional outcomes as typed "
    "INSERT_STEP, REMOVE, REORDER, or SELECT_ALTERNATIVE_TOOL "
    "decisions based on runtime evidence; never emit an "
    "executable condition string or a fixed workflow template."
)


async def _structured_call(
    client: Any,
    model: str,
    payload: Mapping[str, Any],
    *,
    retry: bool = False,
) -> Any:
    """Route the planner call through the canonical llm_compat adapter.

    The typed request/response contract and the repair semantics live here;
    the provider-compatibility fallbacks (json_schema → json_object,
    response-format 400 retry, empty-content retry) are shared with
    AgentLoop / ToolSelector / CommandInterpreter / GoalDecomposer.
    """
    return await structured_call(
        client,
        model,
        _PLANNER_PROMPT,
        "greenbook_planning_decision",
        PlanningDecision.model_json_schema(),
        payload,
        retry=retry,
    )


def _response_payload(response: Any) -> Any:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else parsed
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        text = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, Mapping)
        ).strip()
        if text:
            try:
                return json.loads(extract_top_level_json(text))
            except json.JSONDecodeError as exc:
                raise ValueError("Dynamic Planner returned invalid JSON.") from exc
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Dynamic Planner returned no structured response.")
    try:
        return json.loads(extract_top_level_json(content))
    except json.JSONDecodeError as exc:
        raise ValueError("Dynamic Planner returned invalid JSON.") from exc


def _policy_for_tool(values: Sequence[Any], tool_name: str) -> Mapping[str, Any] | None:
    """Find policy in the already supplied ToolMetadata projection."""

    if not tool_name:
        return None
    for item in values:
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="json")
        if not isinstance(item, Mapping) or str(item.get("name", "")) != tool_name:
            continue
        policy = item.get("policy")
        if isinstance(policy, Mapping):
            return policy
    return None


def _empty_observation_requires_adaptation(payload: Mapping[str, Any]) -> bool:
    """Require an explicit adaptation before an empty read can be continued."""

    observations = payload.get("observations") or []
    if not observations:
        return False
    latest = observations[-1]
    if hasattr(latest, "model_dump"):
        latest = latest.model_dump(mode="json")
    if not isinstance(latest, Mapping):
        return False
    return str(latest.get("result_status") or "").upper() == "EMPTY"


def _latest_observation(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    observations = payload.get("observations") or []
    latest = observations[-1] if observations else {}
    if hasattr(latest, "model_dump"):
        latest = latest.model_dump(mode="json")
    return latest if isinstance(latest, Mapping) else {}


def _arguments_change_scope(
    previous: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> bool:
    """Require a retry to change the evidence query, not just repeat it."""

    if not candidate:
        return False
    if not previous:
        return True
    if "query" in previous:
        return str(candidate.get("query", "")).strip().casefold() != str(
            previous.get("query", "")
        ).strip().casefold()
    return dict(candidate) != dict(previous)


_SEARCH_TOOL_MARKERS = ("search", "find")


def _is_search_tool(tool_name: str) -> bool:
    """True for read-only search tools (community.search_public_posts, …)."""

    name = (tool_name or "").strip().casefold()
    return any(marker in name for marker in _SEARCH_TOOL_MARKERS)


def _broaden_empty_search(
    payload: Mapping[str, Any],
) -> PlanningDecision | None:
    """Return a bounded RETRY_WITH_NEW_ARGS for an EMPTY search, or None.

    The next AgentLoop iteration replays the search through the preferred
    tool without an extra LLM round trip.  When the widening ladder is
    exhausted (or the failed read is not a query search) this returns None
    so the normal planner/fallback decision path applies.
    """

    if not _empty_observation_requires_adaptation(payload):
        return None
    latest = _latest_observation(payload)
    last_result = latest.get("last_result") or {}
    if not isinstance(last_result, Mapping):
        last_result = {}
    failed_tool = str(
        last_result.get("tool_name") or latest.get("tool_name") or ""
    )
    failed_arguments = last_result.get("tool_arguments") or {}
    if not isinstance(failed_arguments, Mapping):
        failed_arguments = {}
    if not _is_search_tool(failed_tool) or "query" not in failed_arguments:
        return None
    broadened = _next_broadened_search_query(
        str(failed_arguments.get("query") or ""),
        tried=_tried_search_queries(payload),
    )
    if broadened is None:
        return None
    return PlanningDecision(
        decision=PlanningDecisionType.RETRY_WITH_NEW_ARGS,
        tool_name=failed_tool,
        arguments={**dict(failed_arguments), "query": broadened},
        reason=(
            f"Search returned no results for "
            f"{failed_arguments.get('query')!r}; retrying with "
            f"broader query {broadened!r}."
        ),
    )


def _tried_search_queries(payload: Mapping[str, Any]) -> set[str]:
    """Collect every search query already attempted across observations."""

    tried: set[str] = set()
    for obs in (payload.get("observations") or []):
        if hasattr(obs, "model_dump"):
            obs = obs.model_dump(mode="json")
        if not isinstance(obs, Mapping):
            continue
        result = obs.get("last_result") or {}
        if not isinstance(result, Mapping):
            result = {}
        args = result.get("tool_arguments") or result.get("arguments") or {}
        if isinstance(args, Mapping):
            query = args.get("query")
            if query:
                tried.add(str(query).strip().casefold())
    return tried


def _next_broadened_search_query(
    current: str,
    tried: set[str],
) -> str | None:
    """Walk a deterministic widening ladder for an empty search query.

    ``"Java 后端 面试"`` → ``"Java 后端"`` → ``"Java"`` (or a single
    remaining token).  The ladder only drops or keeps tokens that were
    already in the user's query — it never fabricates terms — and stops
    once the broadest single token has been tried.
    """

    raw = (current or "").strip()
    tokens = [t for t in re.split(r"[\s\u3000,，;；、]+", raw) if t]
    if len(tokens) <= 1:
        return None
    # 1) drop trailing tokens one at a time (specific → general)
    for width in range(len(tokens) - 1, 0, -1):
        candidate = " ".join(tokens[:width])
        if candidate.casefold() not in tried:
            return candidate
    # 2) fall back to the single broadest token
    for token in tokens:
        if token.casefold() not in tried:
            return token
    return None


def _enforce_empty_observation_policy(
    payload: Mapping[str, Any],
    decision: PlanningDecision,
) -> PlanningDecision:
    if not (
        _empty_observation_requires_adaptation(payload)
        and decision.decision == PlanningDecisionType.CONTINUE
    ):
        return decision
    return PlanningDecision(
        decision=PlanningDecisionType.ASK_HUMAN,
        reason=(
            "The read returned an empty result and the proposed plan did not "
            "include an evidence-bounded adaptation. Broaden the read scope or "
            "provide a bounded data source before continuing."
        ),
    )


__all__ = ["DynamicPlanner", "PlanningDecision", "PlanningDecisionType"]
