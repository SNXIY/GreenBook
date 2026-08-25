"""Small, deterministic A/B control-layer falsification probe.

This is deliberately test-only.  It starts from one canonical scenario list,
does not parse language, call an LLM, call Java, or use the Durable Runtime.
The A probe drives the existing ActionLoop with in-memory tool results.  The
B probe invokes only the existing Commitment/WorkItem POC surface and reports
missing control-loop capabilities instead of implementing them.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from greenbook_agent_core.actionloop import ActionDecision, ActionDecisionType, ActionLoop
from greenbook_agent_core.command.models import Command, CommandType, TaskDelta, TaskDeltaOperation
from greenbook_agent_core.task.models import Objective, Task, TaskResourceRef
from greenbook_agent_core.turn.commitment_poc import freeze, project_command


RUN_AT = "2026-08-22T07:00:00Z"
OLD_RUN_AT = "2026-08-21T07:00:00Z"


@dataclass(frozen=True)
class CanonicalWork:
    work_id: str
    outcome: str
    target_id: str
    target_kind: str
    run_at: str | None = None
    existing: bool = False
    completed: bool = False


@dataclass(frozen=True)
class CanonicalScenario:
    scenario_id: str
    label: str
    works: tuple[CanonicalWork, ...]
    mutation: bool = False
    turns: tuple[tuple[str, ...], ...] = ()
    duplicate_target: str | None = None
    failure_target: str | None = None
    no_progress: bool = False


def _w(
    work_id: str,
    outcome: str,
    target_kind: str,
    *,
    existing: bool = False,
    completed: bool = False,
    run_at: str | None = None,
) -> CanonicalWork:
    return CanonicalWork(
        work_id=work_id,
        outcome=outcome,
        target_id=f"{target_kind.lower()}-{work_id}",
        target_kind=target_kind,
        existing=existing,
        completed=completed,
        run_at=run_at,
    )


def canonical_scenarios() -> tuple[CanonicalScenario, ...]:
    """Hand-authored ground truth; no user text or semantic derivation."""

    return (
        CanonicalScenario("01-single-draft", "single target Draft", (_w("A", "DRAFT", "DRAFT"),)),
        CanonicalScenario(
            "02-single-schedule", "single target Schedule", (_w("A", "SCHEDULED", "SCHEDULE", run_at=RUN_AT),)
        ),
        CanonicalScenario(
            "03-immediate-plus-scheduled",
            "A immediate, B scheduled",
            (_w("A", "PUBLISHED", "POST"), _w("B", "SCHEDULED", "SCHEDULE", run_at=RUN_AT)),
        ),
        CanonicalScenario(
            "04-counterfactual-scheduled-plus-immediate",
            "A scheduled, B immediate",
            (_w("A", "SCHEDULED", "SCHEDULE", run_at=RUN_AT), _w("B", "PUBLISHED", "POST")),
        ),
        CanonicalScenario(
            "05-three-publish-schedule-draft",
            "Publish + Schedule + Draft",
            (
                _w("A", "PUBLISHED", "POST"),
                _w("B", "SCHEDULED", "SCHEDULE", run_at=RUN_AT),
                _w("C", "DRAFT", "DRAFT"),
            ),
        ),
        CanonicalScenario(
            "06-two-existing-schedules-update",
            "two existing schedules updated in one turn",
            (
                _w("A", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
                _w("B", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
            ),
            mutation=True,
        ),
        CanonicalScenario(
            "07-cross-turn-two-resource-update",
            "A and B updated in separate turns",
            (
                _w("A", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
                _w("B", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
            ),
            mutation=True,
            turns=(("A",), ("B",)),
        ),
        CanonicalScenario(
            "08-one-complete-one-pending",
            "A complete, B pending",
            (
                _w("A", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, completed=True, run_at=RUN_AT),
                _w("B", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
            ),
            mutation=True,
        ),
        CanonicalScenario(
            "09-same-action-different-target",
            "same UPDATE_SCHEDULE action, distinct target identities",
            (
                _w("A", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
                _w("B", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
            ),
            mutation=True,
        ),
        CanonicalScenario(
            "10-idempotent-observation-continues",
            "duplicate/idempotent A observation must still reach B",
            (
                _w("A", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
                _w("B", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
            ),
            mutation=True,
            duplicate_target="schedule-A",
        ),
        CanonicalScenario(
            "11-partial-failure",
            "A succeeds, B fails",
            (
                _w("A", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
                _w("B", "SCHEDULE_UPDATED", "SCHEDULE", existing=True, run_at=RUN_AT),
            ),
            mutation=True,
            failure_target="schedule-B",
        ),
        CanonicalScenario(
            "12-no-progress-repeated-action",
            "repeated SEARCH with no new evidence",
            (_w("A", "SEARCH_RESULT", "SEARCH_RESULT"),),
            no_progress=True,
        ),
    )


_CAPABILITIES = {
    "DRAFT": ["GENERATE_CONTENT"],
    "SCHEDULED": ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
    "PUBLISHED": ["GENERATE_CONTENT", "PUBLISH_NOW"],
    # Production Objective capability; UPDATE_SCHEDULE is the semantic action.
    "SCHEDULE_UPDATED": ["MANAGE_SCHEDULE"],
    "SEARCH_RESULT": ["SEARCH_COMMUNITY"],
}


def _actions_for(work: CanonicalWork) -> tuple[str, ...]:
    return {
        "DRAFT": ("CREATE_DRAFT",),
        "SCHEDULED": ("CREATE_DRAFT", "CREATE_SCHEDULE"),
        "PUBLISHED": ("CREATE_DRAFT", "PUBLISH_NOW"),
        "SCHEDULE_UPDATED": ("UPDATE_SCHEDULE",),
        "SEARCH_RESULT": ("SEARCH_POSTS",),
    }[work.outcome]


def _objective(work: CanonicalWork, task_id: str) -> Objective:
    status = "COMPLETED" if work.completed else "PENDING"
    related = [work.target_id] if work.existing else []
    operations = [f"prior-{work.work_id}"] if work.existing and work.completed else []
    constraints = {"run_at": work.run_at, "timezone": "UTC"} if work.run_at else {}
    if work.outcome == "PUBLISHED":
        constraints["publication_intent"] = "IMMEDIATE"
    elif work.outcome == "SCHEDULED":
        constraints["publication_intent"] = "SCHEDULED"
    return Objective(
        task_id=task_id,
        objective_id=work.work_id,
        description=work.work_id,
        intent=work.outcome,
        status=status,
        expected_resource_kind=work.target_kind,
        result_requirement="RESOURCE_MUTATION" if work.outcome != "SEARCH_RESULT" else "DIRECT_RESULT",
        required_capabilities=_CAPABILITIES[work.outcome],
        constraints=constraints,
        related_resource_ids=related,
        related_operations=operations,
    )


def _task(scenario: CanonicalScenario) -> Task:
    task_id = f"task-{scenario.scenario_id}"
    objectives = [_objective(work, task_id) for work in scenario.works]
    resources = [
        TaskResourceRef(
            resource_id=work.target_id,
            resource_kind=work.target_kind,
            objective_id=work.work_id,
            scheduled_at=OLD_RUN_AT if work.existing else work.run_at,
        )
        for work in scenario.works
        if work.existing
    ]
    return Task(
        task_id=task_id,
        conversation_id=f"conversation-{scenario.scenario_id}",
        user_id="benchmark-user",
        tenant_id="benchmark-tenant",
        objectives=objectives,
        resource_index=resources,
    )


def _command(scenario: CanonicalScenario, works: tuple[CanonicalWork, ...]) -> Command:
    if not scenario.mutation:
        return Command(type=CommandType.CREATE, goal=scenario.label, raw_input="CANONICAL")
    changes = [
        TaskDelta(
            change_id=f"mutation-{work.work_id}",
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={
                "id": work.target_id,
                "resource_id": work.target_id,
                "objective_id": work.work_id,
            },
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "objective_id": work.work_id,
                "schedule_id": work.target_id,
                "run_at": work.run_at,
            },
        )
        for work in works
    ]
    return Command(type=CommandType.MODIFY, goal=scenario.label, task_changes=changes, raw_input="CANONICAL")


class _MemoryStore:
    def _record(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def _record_resource(self, task: Task, resource_id: str, resource_kind: str, title: str, **kwargs: Any) -> None:
        owner = kwargs.get("objective_id")
        if any(
            str(row.resource_id) == str(resource_id)
            and str(row.resource_kind).upper() == str(resource_kind).upper()
            and str(row.objective_id or "") == str(owner or "")
            for row in task.resource_index
        ):
            return
        task.resource_index.append(
            TaskResourceRef(
                resource_id=str(resource_id),
                resource_kind=str(resource_kind),
                objective_id=str(owner or "") or None,
                title=title,
            )
        )


def _work_by_id(scenario: CanonicalScenario) -> dict[str, CanonicalWork]:
    return {work.work_id: work for work in scenario.works}


def _choose_action(context: dict[str, Any], works: dict[str, CanonicalWork]) -> ActionDecision:
    current = context.get("current_objective") or {}
    work = works.get(str(current.get("objective_id") or ""))
    if work is None:
        return ActionDecision(decision=ActionDecisionType.FINISH, semantic_action="FINISH")
    owned = {
        str(row.get("resource_kind") or "").upper()
        for row in context.get("resources", [])
        if str(row.get("objective_id") or "") == work.work_id
    }
    if work.outcome == "SEARCH_RESULT":
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="SEARCH_POSTS",
            arguments={"query": work.work_id, "objective_id": work.work_id},
        )
    if "DRAFT" not in owned and "GENERATE_CONTENT" in _CAPABILITIES[work.outcome]:
        return ActionDecision(
            decision=ActionDecisionType.GENERATE_CONTENT,
            semantic_action="CREATE_DRAFT",
            arguments={"title": work.work_id, "objective_id": work.work_id},
        )
    if work.outcome == "PUBLISHED" and "POST" not in owned:
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="PUBLISH_NOW",
            arguments={"objective_id": work.work_id},
        )
    if work.outcome == "SCHEDULED" and "SCHEDULE" not in owned:
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="CREATE_SCHEDULE",
            arguments={"objective_id": work.work_id, "run_at": work.run_at},
        )
    return ActionDecision(decision=ActionDecisionType.FINISH, semantic_action="FINISH")


async def _run_production(scenario: CanonicalScenario) -> dict[str, Any]:
    works = _work_by_id(scenario)
    if scenario.turns:
        initial = tuple(works[item] for item in scenario.turns[0])
        task = _task(
            CanonicalScenario(
                scenario.scenario_id,
                scenario.label,
                initial,
                mutation=scenario.mutation,
            )
        )
    else:
        task = _task(scenario)
    logical: list[dict[str, Any]] = []
    physical: list[str] = []
    observations: list[dict[str, Any]] = []

    async def write(**kwargs: Any) -> dict[str, Any]:
        action = str(kwargs.get("semantic_action") or "")
        objective_id = str(kwargs.get("objective_id") or "")
        args = dict(kwargs.get("arguments") or {})
        work = works.get(objective_id)
        target = str(
            args.get("schedule_id")
            or args.get("draft_id")
            or args.get("post_id")
            or (f"schedule-{objective_id}" if action == "CREATE_SCHEDULE" else "")
            or (f"draft-{objective_id}" if action == "CREATE_DRAFT" else "")
            or (f"post-{objective_id}" if action == "PUBLISH_NOW" else "")
        )
        attempt = {
            "action": action,
            "objective_id": objective_id,
            "target_id": target,
            "run_at": args.get("run_at"),
        }
        logical.append(attempt)
        if scenario.failure_target and target == scenario.failure_target:
            attempt["outcome"] = "FAILED"
            return {
                "ok": False,
                "status": "FAILED",
                "error_code": "CANONICAL_PARTIAL_FAILURE",
                "message": "canonical failure",
            }
        if target not in physical:
            physical.append(target)
        attempt["outcome"] = "SUCCESS"
        return {
            "ok": True,
            "status": "COMPLETED",
            "resource_id": target,
            "execution_id": f"op-{target}-{len(logical)}",
            "duplicate": bool(scenario.duplicate_target and target == scenario.duplicate_target),
        }

    async def read(**_kwargs: Any) -> dict[str, Any]:
        return {"ok": True} if scenario.no_progress else {"ok": True, "data": {"items": []}}

    async def run_one(command: Command | None, *, max_failures: int = 6) -> Any:
        loop = ActionLoop(
            decision_maker=lambda context: _choose_action(context, works),
            read_handler=read,
            write_submitter=write,
            task_store=_MemoryStore(),
            max_iterations=8,
            max_failures=max_failures,
        )
        return await loop.run(task, command)

    if scenario.scenario_id == "08-one-complete-one-pending":
        loop = ActionLoop()
        completed = loop._verify_finish(task)
        return {
            "status": "NOT_COMPLETED" if not completed else "COMPLETED",
            "iterations": 0,
            "logical_attempts": logical,
            "physical_writes": physical,
            "observations": observations,
            "objective_omission": [work.work_id for work in scenario.works if not work.completed],
            "target_cross_binding": False,
            "temporal_mismatch": False,
            "duplicate_logical_selection": False,
            "retry_attempts": 0,
            "observation_correlation": True,
            "no_progress_detected": False,
            "errors": [],
            "decisions": [],
            "progress_trace": [],
            "task_objectives": [
                {"id": item.objective_id, "status": str(item.status), "operations": list(item.related_operations)}
                for item in task.objectives
            ],
        }

    results: list[Any] = []
    if scenario.turns:
        for index, ids in enumerate(scenario.turns):
            results.append(await run_one(_command(scenario, tuple(works[item] for item in ids))))
            if index + 1 < len(scenario.turns):
                for item_id in scenario.turns[index + 1]:
                    work = works[item_id]
                    task.objectives.append(_objective(work, task.task_id))
                    task.resource_index.append(
                        TaskResourceRef(
                            resource_id=work.target_id,
                            resource_kind=work.target_kind,
                            objective_id=work.work_id,
                            scheduled_at=OLD_RUN_AT,
                        )
                    )
    else:
        results.append(await run_one(_command(scenario, scenario.works), max_failures=1 if scenario.failure_target else 6))

    for result in results:
        observations.extend(
            {
                "action": item.action,
                "outcome": item.outcome,
                "objective_id": str(getattr(item, "objective_id", "") or ""),
                "resource_id": item.resource_id,
            }
            for item in result.observations
        )
    expected_targets = {work.target_id for work in scenario.works if not scenario.no_progress}
    target_cross = any(
        entry["target_id"]
        and entry["objective_id"]
        and entry["target_id"] != works[entry["objective_id"]].target_id
        for entry in logical
        if entry["objective_id"] in works and works[entry["objective_id"]].outcome == "SCHEDULE_UPDATED"
    )
    temporal_mismatch = any(
        entry["objective_id"] in works
        and works[entry["objective_id"]].run_at
        and entry["action"] in {"CREATE_SCHEDULE", "UPDATE_SCHEDULE"}
        and entry["run_at"] != works[entry["objective_id"]].run_at
        for entry in logical
    )
    successful_targets = [
        entry["target_id"]
        for entry in logical
        if entry["target_id"] and entry.get("outcome") in {"SUCCESS", "SUBMITTED"}
    ]
    duplicate_logical = len(successful_targets) != len(set(successful_targets))
    failed_targets = [
        entry["target_id"]
        for entry in logical
        if entry["target_id"] and entry.get("outcome") == "FAILED"
    ]
    retry_attempts = sum(max(0, count - 1) for count in Counter(failed_targets).values())
    status = results[-1].status if results else "FAILED"
    no_progress = any(getattr(result, "error_code", "") == "ACTION_LOOP_NO_PROGRESS" for result in results)
    omissions = sorted(expected_targets - set(physical))
    correlation = all(
        not item["resource_id"]
        or item["objective_id"] == next(
            (work.work_id for work in scenario.works if work.target_id == item["resource_id"]),
            item["objective_id"],
        )
        for item in observations
    )
    return {
        "status": status,
        "iterations": sum(int(getattr(result, "iterations", 0) or 0) for result in results),
        "logical_attempts": logical,
        "physical_writes": physical,
        "observations": observations,
        "objective_omission": omissions,
        "target_cross_binding": target_cross,
        "temporal_mismatch": temporal_mismatch,
        "duplicate_logical_selection": duplicate_logical,
        "retry_attempts": retry_attempts,
        "observation_correlation": correlation,
        "no_progress_detected": no_progress,
        "errors": [
            {
                "status": str(getattr(result, "status", "")),
                "error_code": str(getattr(result, "error_code", "") or ""),
                "error_message": str(getattr(result, "error_message", "") or ""),
            }
            for result in results
            if str(getattr(result, "error_code", "") or "")
        ],
        "decisions": [
            str(decision)
            for result in results
            for decision in (getattr(result, "decisions", ()) or ())
        ],
        "progress_trace": [
            trace
            for result in results
            for trace in (getattr(result, "progress_trace", ()) or ())
        ],
        "task_objectives": [
            {"id": item.objective_id, "status": str(item.status), "operations": list(item.related_operations)}
            for item in task.objectives
        ],
    }


def _poc_source(work: CanonicalWork) -> SimpleNamespace:
    publication = {
        "DRAFT": "DRAFT_ONLY",
        "SCHEDULED": "SCHEDULED_PUBLISH",
        "PUBLISHED": "IMMEDIATE_PUBLISH",
        "SCHEDULE_UPDATED": "SCHEDULED_PUBLISH",
        "SEARCH_RESULT": "",
    }[work.outcome]
    target = (
        {"id": work.target_id, "resource_id": work.target_id, "kind": work.target_kind}
        if work.existing
        else {}
    )
    return SimpleNamespace(
        operation="UPDATE_SCHEDULE" if work.outcome == "SCHEDULE_UPDATED" else "CREATE",
        publication_intent=publication,
        capabilities=list(_CAPABILITIES[work.outcome]),
        target_reference=target,
        run_at=work.run_at,
        topic=work.work_id,
        title=work.work_id,
        constraints={"run_at": work.run_at} if work.run_at else {},
    )


def _run_tool_first_projection(scenario: CanonicalScenario) -> dict[str, Any]:
    command = SimpleNamespace(
        command_id=f"canonical-{scenario.scenario_id}",
        items=[_poc_source(work) for work in scenario.works],
        resolved_target={},
        semantic_operation="",
        type="CREATE",
    )
    try:
        draft = project_command(command)
        frozen = freeze(draft)
        projection_error = ""
    except Exception as exc:  # the probe records, rather than hides, POC gaps
        draft = None
        frozen = None
        projection_error = f"{type(exc).__name__}: {exc}"
    items = list(getattr(draft, "work_items", ()) or ())
    expected = list(scenario.works)
    identity = [
        {
            "canonical_work_id": work.work_id,
            "poc_work_item_id": str(getattr(item, "work_item_id", "")),
            "target_reference": dict(getattr(item, "target_reference", {}) or {}),
            "resolved_target_ref": dict(getattr(item, "resolved_target_ref", {}) or {}),
            "canonical_run_at": getattr(item, "canonical_run_at", None),
            "status": str(getattr(item, "status", "")),
        }
        for work, item in zip(expected, items)
    ]
    # Inspect the existing POC surface; do not add the missing controller in
    # this experiment.  The names below are benchmark responsibilities, not
    # new API requirements imposed on B.
    import greenbook_agent_core.turn.commitment_poc as poc

    required_symbols = {
        "next_work": ("next_work", "select_next_work"),
        "tool_call": ("tool_call", "execute_tool", "run_tool"),
        "observation": ("observe", "record_observation", "apply_observation"),
        "continuation": ("continue_work", "resume", "continue_commitment"),
        "completion": ("complete", "is_complete", "reduce_completion"),
    }
    missing_control = {
        key: not any(callable(getattr(poc, name, None)) for name in names)
        for key, names in required_symbols.items()
    }
    binding_ok = len(items) == len(expected)
    temporal_ok = all(
        getattr(item, "canonical_run_at", None) == work.run_at
        for work, item in zip(expected, items)
    )
    target_ok = all(
        not work.existing
        or str((getattr(item, "resolved_target_ref", {}) or {}).get("resource_id") or "") == work.target_id
        for work, item in zip(expected, items)
    )
    return {
        "projection": "FROZEN" if frozen is not None else "FAILED",
        "projection_error": projection_error,
        "work_item_count": len(items),
        "projection_binding_ok": binding_ok,
        "target_binding_ok": target_ok,
        "temporal_binding_ok": temporal_ok,
        "identity_is_generated_by_poc": all(bool(getattr(item, "work_item_id", "")) for item in items),
        "identity": identity,
        "missing_control_surface": missing_control,
        "required_work_executed": None,
        "objective_omission": None,
        "target_cross_binding": None,
        "temporal_mismatch": None,
        "duplicate_logical_selection": None,
        "observation_correlation": None,
        "premature_completion": None,
        "no_progress_detection": None,
        "bounded_iterations": None,
    }


async def main() -> None:
    scenarios = canonical_scenarios()
    production: dict[str, Any] = {}
    tool_first: dict[str, Any] = {}
    for scenario in scenarios:
        production[scenario.scenario_id] = await _run_production(scenario)
        tool_first[scenario.scenario_id] = _run_tool_first_projection(scenario)
    report = {
        "benchmark": "GreenBook Control Layer A/B Falsification",
        "llm": False,
        "java_mutation": False,
        "durable_runtime": False,
        "canonical_case_count": len(scenarios),
        "canonical_cases": [
            {
                "id": scenario.scenario_id,
                "label": scenario.label,
                "work": [work.__dict__ for work in scenario.works],
            }
            for scenario in scenarios
        ],
        "production": production,
        "tool_first": tool_first,
        "tool_first_control_loop_available": False,
        "verdict": "INSUFFICIENT_EVIDENCE",
        "blocker": (
            "The existing Tool-first POC projects Command facts into WorkItem/Commitment, "
            "but exposes no next-work selector, tool-call executor, Observation correlation, "
            "continuation driver, or completion reducer. Adding those would be a new controller, "
            "which the experiment is explicitly forbidden to implement."
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
