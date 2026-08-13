"""Runtime Dynamic Planner contracts.

The planner receives already-understood Goals and runtime evidence.  It may
emit a typed decision, but it does not execute tools, mutate persistence, or
interpret the original user message.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from greenbook_contracts.tool_contract import ToolMetadata
from pydantic import ValidationError

from greenbook_agent_core.goal.models import GoalTree
from greenbook_agent_core.llm_compat import (
    STRUCTURED_OUTPUT_RETRY_MAX_TOKENS,
    add_json_schema_instruction,
    has_structured_payload,
    retry_json_object,
    structured_provider_options,
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
            except ValueError as exc:
                # DeepSeek currently rejects ``json_schema`` and normally
                # succeeds with ``json_object``.  Under a long reasoning
                # prompt it can occasionally return an empty content field
                # without an HTTP error.  Give the provider one explicit
                # JSON-only retry before failing closed; do not invent a
                # planning decision locally.
                if "no structured response" not in str(exc).lower():
                    raise
                response = await _structured_call(
                    client,
                    model if model is not None else self._model,
                    payload,
                    retry=True,
                )
                try:
                    decision = PlanningDecision.model_validate(_response_payload(response))
                except ValidationError as retry_exc:
                    raise ValueError(
                        "Dynamic Planner output is not a PlanningDecision."
                    ) from retry_exc
            except ValidationError as exc:
                raise ValueError("Dynamic Planner output is not a PlanningDecision.") from exc
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


async def _structured_call(
    client: Any,
    model: str,
    payload: Mapping[str, Any],
    *,
    retry: bool = False,
) -> Any:
    kwargs = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
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
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "greenbook_planning_decision",
                "strict": True,
                "schema": PlanningDecision.model_json_schema(),
            },
        },
        "temperature": 0.0,
        **structured_provider_options(client, model),
    }
    if retry:
        kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"] = add_json_schema_instruction(
            kwargs["messages"],
            PlanningDecision.model_json_schema(),
        )
        kwargs["max_tokens"] = STRUCTURED_OUTPUT_RETRY_MAX_TOKENS
        response = await client.chat.completions.create(**kwargs)
        if not has_structured_payload(response):
            response = await retry_json_object(
                client,
                kwargs,
                PlanningDecision.model_json_schema(),
            )
        return response
    try:
        return await client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "response_format" not in str(exc).lower() and "json_schema" not in str(exc).lower():
            raise
        kwargs["response_format"] = {"type": "json_object"}
        kwargs["messages"] = add_json_schema_instruction(
            kwargs["messages"],
            PlanningDecision.model_json_schema(),
        )
        kwargs["max_tokens"] = STRUCTURED_OUTPUT_RETRY_MAX_TOKENS
        response = await client.chat.completions.create(**kwargs)
    if not has_structured_payload(response):
        response = await retry_json_object(
            client,
            kwargs,
            PlanningDecision.model_json_schema(),
        )
    return response


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
            return json.loads(text)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Dynamic Planner returned no structured response.")
    return json.loads(content)


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
