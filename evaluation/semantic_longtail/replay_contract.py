"""Replay frozen semantic outputs through deterministic GreenBook stages.

This evaluation-only driver never calls an LLM and never creates a Task or
executes a tool.  It is used to separate schema/Resolver/projection changes
from provider understanding changes in the frozen long-tail benchmark.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
for _path in (
    ROOT,
    ROOT / "packages" / "agent_core",
    ROOT / "packages" / "contracts",
    ROOT / "packages" / "evaluation",
    ROOT / "packages" / "security",
    ROOT / "apps" / "agent_api",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _build_command(raw: Mapping[str, Any], message: str) -> tuple[Any, Any, dict[str, Any]]:
    from greenbook_agent_core.command.interpreter import (
        _ensure_create_item,
        _materialize_request_publication_constraints,
        _normalize_delete_post,
        _normalize_draft_only,
        _normalize_multi_objective_items,
        apply_semantic_derivation,
        normalize_task_deltas,
        _strip_unknown_command_fields,
        validate_semantic_candidate,
    )
    from greenbook_agent_core.command.models import Command, StructuredCommandOutput

    payload = _strip_unknown_command_fields(raw)
    structured = StructuredCommandOutput.model_validate(payload)
    structured = _normalize_multi_objective_items(structured)
    structured = _ensure_create_item(structured)
    structured = _materialize_request_publication_constraints(structured)
    command = Command(
        type=structured.command,
        goal=structured.goal or structured.objective or message,
        objective=structured.goal or structured.objective or message,
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
        raw_input=message,
        items=list(structured.items or []),
    )
    _normalize_delete_post(command)
    _normalize_draft_only(command, message)
    command.task_changes = normalize_task_deltas(command.task_changes)
    command = apply_semantic_derivation(command)
    validation = validate_semantic_candidate(command)
    return command, validation, {
        "schema_parse": structured.model_dump(mode="json"),
        "normalized": structured.model_dump(mode="json"),
        "segmentation": structured.model_dump(mode="json"),
        "semantic_derivation": {
            "semantic_operation": command.semantic_operation,
            "required_capabilities": list(command.required_capabilities),
            "publication_intent": command.constraints.get("publication_intent", ""),
            "item_count": len(command.items or ()),
        },
    }


async def _replay_one(
    case: Mapping[str, Any],
    result_before: Mapping[str, Any],
    *,
    context_library: Mapping[str, Any],
    timezone: str,
    fixed_now: datetime,
    scoped_context: bool = False,
) -> dict[str, Any]:
    from greenbook_agent_core.command.models import CommandContext
    from greenbook_agent_core.context.models import ContextSnapshot
    from greenbook_agent_core.execution.temporal_resolver import TemporalResolver
    from greenbook_agent_core.turn import ContextAssembler
    from greenbook_agent_api.services.turn_coordinator import TurnCoordinator
    from greenbook_evaluation.canonical import canonical_semantic_result
    from evaluation.semantic_longtail.run_benchmark import _context_for, _evaluate_case

    class _FrozenSnapshotBuilder:
        def __init__(self, snapshot: ContextSnapshot) -> None:
            self._snapshot = snapshot

        async def build(self, **_kwargs: Any) -> ContextSnapshot:
            return self._snapshot

    message = str(result_before.get("message") or case.get("message") or "")
    raw = (result_before.get("stages") or {}).get("raw")
    replay: dict[str, Any] = {
        "case_id": case["id"],
        "category": case.get("category", ""),
        "message": message,
        "variant_index": int(result_before.get("variant_index") or 0),
        "provider_calls": [],
        "provider_usage": {"records": []},
        "latency_ms": 0.0,
        "stages": {"raw": raw},
    }
    from greenbook_agent_core.command.interpreter import _strip_unknown_command_fields
    from greenbook_agent_core.command.models import StructuredCommandOutput
    try:
        StructuredCommandOutput.model_validate(_strip_unknown_command_fields(raw))
        replay["raw_schema_valid"] = True
    except Exception:
        replay["raw_schema_valid"] = False
    if not isinstance(raw, Mapping):
        replay.update({
            "error": "Frozen result has no raw structured output.",
            "error_code": "COMMAND_SCHEMA_INVALID",
            "exception_type": "ReplayError",
        })
        replay["evaluation"] = _evaluate_case(case, replay)
        return replay
    try:
        command, validation, stages = _build_command(raw, message)
        replay["stages"].update(stages)
        if not validation.valid:
            codes = ", ".join(error.code for error in validation.errors)
            replay.update({
                "error": f"Structured semantic candidate is internally inconsistent: {codes}",
                "error_code": "COMMAND_SEMANTIC_INVALID",
                "exception_type": "CommandInterpretationError",
            })
            replay["evaluation"] = _evaluate_case(case, replay)
            return replay
    except Exception as exc:  # noqa: BLE001 - replay records deterministic failure
        replay.update({
            "error": str(exc),
            "error_code": "COMMAND_SCHEMA_INVALID",
            "exception_type": type(exc).__name__,
        })
        replay["evaluation"] = _evaluate_case(case, replay)
        return replay

    payload = _context_for(case, context_library)
    if scoped_context:
        snapshot = ContextSnapshot.model_validate(payload)
        assembled = await ContextAssembler(
            _FrozenSnapshotBuilder(snapshot)
        ).assemble(
            conversation_id=str(snapshot.conversation_id),
            user_id=str(snapshot.user_id),
            tenant_id=str(snapshot.tenant_id),
            user_input=message,
        )
        context = assembled.to_command_context()
    else:
        context = CommandContext.model_validate(payload)
        assembled = SimpleNamespace(
            snapshot=SimpleNamespace(
                active_tasks=list(payload.get("active_tasks") or [])
            )
        )
    coordinator = TurnCoordinator(
        temporal_resolver=TemporalResolver(now=fixed_now),
    )
    resolution = await coordinator._resolve_target(  # noqa: SLF001
        command,
        context,
        assembled=assembled,
    )
    state = coordinator._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=resolution,
        timezone=timezone,
    )
    command_json = command.model_dump(mode="json")
    state_json = state.model_dump(mode="json")
    resolution_json = (
        resolution.model_dump(mode="json")
        if resolution is not None
        else {
            "status": (
                "NONE"
                if not command_json.get("target") and not command_json.get("task_changes")
                else command_json.get("target_resolution", "NOT_FOUND")
            )
        }
    )
    replay.update({
        "command": command_json,
        "state": state_json,
        "canonical": canonical_semantic_result(state_json, command),
        "target_resolution": resolution_json,
        "objective_projection": list(state_json.get("objectives") or []),
    })
    replay["evaluation"] = _evaluate_case(case, replay)
    return replay


def _load_cases(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _affected_case(case_id: str) -> bool:
    return case_id[:1] in {"B", "F", "G", "H"}


def _metric_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    before_metrics = before.get("metrics") or {}
    after_metrics = after.get("metrics") or {}
    names = sorted(set(before_metrics) | set(after_metrics))
    return {
        name: {
            "before": int(before_metrics.get(name) or 0),
            "after": int(after_metrics.get(name) or 0),
            "delta": int(after_metrics.get(name) or 0) - int(before_metrics.get(name) or 0),
        }
        for name in names
    }


def write_report(
    *,
    output_dir: Path,
    dataset: Mapping[str, Any],
    cases: list[Mapping[str, Any]],
    before: list[Mapping[str, Any]],
    after: list[Mapping[str, Any]],
    before_path: Path,
    scoped_context: bool,
) -> None:
    from evaluation.semantic_longtail.run_benchmark import _aggregate, _paraphrase_report

    affected_cases = [case for case in cases if _affected_case(str(case.get("id") or ""))]
    affected_ids = {str(case.get("id") or "") for case in affected_cases}
    before_affected = [item for item in before if str(item.get("case_id") or "") in affected_ids]
    after_affected = [item for item in after if str(item.get("case_id") or "") in affected_ids]
    before_all_report = _aggregate(cases, before)
    after_all_report = _aggregate(cases, after)
    before_affected_report = _aggregate(affected_cases, before_affected)
    after_affected_report = _aggregate(affected_cases, after_affected)
    raw_schema_valid = sum(int(bool(item.get("raw_schema_valid"))) for item in after)
    baseline_schema_failures = sum(
        int("schema_parse_failure" in set((item.get("evaluation") or {}).get("metrics") or ()))
        for item in before
    )
    report = {
        "status": "COMPLETED",
        "mode": "deterministic_replay_no_llm",
        "context_mode": "scoped" if scoped_context else "snapshot",
        "dataset": str(ROOT / "evaluation" / "semantic_longtail" / "cases.json"),
        "baseline_results": str(before_path),
        "scope": {
            "all_cases": len(cases),
            "all_utterances": len(after),
            "affected_case_prefixes": ["B", "F", "G", "H"],
            "affected_cases": len(affected_cases),
            "affected_utterances": len(after_affected),
        },
        "schema_replay": {
            "baseline_schema_parse_failures": baseline_schema_failures,
            "after_raw_schema_valid": raw_schema_valid,
            "after_raw_schema_invalid": len(after) - raw_schema_valid,
        },
        "all": {
            "before": before_all_report,
            "after": after_all_report,
            "metric_delta": _metric_delta(before_all_report, after_all_report),
        },
        "affected": {
            "before": before_affected_report,
            "after": after_affected_report,
            "metric_delta": _metric_delta(before_affected_report, after_affected_report),
            "paraphrase_before": _paraphrase_report(affected_cases, before_affected),
            "paraphrase_after": _paraphrase_report(affected_cases, after_affected),
        },
        "artifacts": {
            "results": str(output_dir / "results.json"),
            "report": str(output_dir / "report.json"),
        },
    }
    _write_json(output_dir / "results.json", after)
    _write_json(output_dir / "report.json", report)


async def _run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=ROOT / "artifacts" / "semantic_longtail_20260822_final" / "results.json",
        help="Frozen benchmark results containing the raw provider outputs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "semantic_contract_replay_20260822",
        help="Directory for deterministic replay artifacts.",
    )
    parser.add_argument(
        "--scoped-context",
        action="store_true",
        help="Replay through ContextAssembler's scoped projection.",
    )
    args = parser.parse_args()
    dataset_path = ROOT / "evaluation" / "semantic_longtail" / "cases.json"
    before_path = args.baseline.resolve()
    output_dir = args.output.resolve()
    dataset = _load_cases(dataset_path)
    before = json.loads(before_path.read_text(encoding="utf-8"))
    cases = list(dataset.get("cases") or [])
    cases_by_id = {str(case["id"]): case for case in cases}
    before_by_key = {
        (str(item.get("case_id") or ""), int(item.get("variant_index") or 0)): item
        for item in before
    }
    after: list[dict[str, Any]] = []
    for key, item in before_by_key.items():
        case = cases_by_id.get(key[0])
        if case is None:
            continue
        after.append(await _replay_one(
            case,
            item,
            context_library=dataset.get("context_library") or {},
            timezone=str(dataset.get("timezone") or "Asia/Shanghai"),
            fixed_now=datetime.fromisoformat(str(dataset["fixed_now"])),
            scoped_context=args.scoped_context,
        ))
    write_report(
        output_dir=output_dir,
        dataset=dataset,
        cases=cases,
        before=before,
        after=after,
        before_path=before_path,
        scoped_context=args.scoped_context,
    )
    print(json.dumps({
        "status": "COMPLETED",
        "output_dir": str(output_dir),
        "results": str(output_dir / "results.json"),
        "report": str(output_dir / "report.json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
