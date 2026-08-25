"""Read-only forensic snapshots for the frozen semantic long-tail benchmark.

This module is an evaluation artifact.  It reconstructs the exact DeepSeek
JSON-mode request produced by the current ``structured_call`` compatibility
path and records the already-frozen provider/stage snapshots.  It never calls
the provider and never enters the execution runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
import sys

for _path in (
    ROOT / "packages" / "agent_core",
    ROOT / "packages" / "contracts",
    ROOT / "packages" / "evaluation",
    ROOT / "apps" / "agent_api",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from greenbook_agent_core.command.interpreter import _COMMAND_SYSTEM_PROMPT
from greenbook_agent_core.command.models import CommandContext, StructuredCommandOutput
from greenbook_agent_core.llm_compat import add_json_schema_instruction
from run_benchmark import _context_for


INPUT_RESULTS = ROOT / "artifacts" / "residual_semantic_gate_20260822_full_after2" / "results.json"
DATASET = ROOT / "evaluation" / "semantic_longtail" / "cases.json"
OUTPUT = ROOT / "artifacts" / "semantic_pipeline_whitebox_20260822"


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ids(value: Any) -> list[str]:
    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if key in {"id", "task_id", "resource_id", "objective_id", "schedule_id", "draft_id", "post_id"}:
                    if child not in (None, ""):
                        found.add(str(child))
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return sorted(found)


def _semantic_diff(before: Any, after: Any) -> dict[str, Any]:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return {"changed": before != after, "before_type": type(before).__name__, "after_type": type(after).__name__}
    keys = {
        "command", "semantic_operation", "goal", "objective", "target", "task_changes",
        "items", "constraints", "required_capabilities", "needs_clarification",
        "temporal_text", "run_at", "temporal_kind", "temporal_resolved",
    }
    changed: dict[str, Any] = {}
    for key in sorted(keys):
        if before.get(key) != after.get(key):
            changed[key] = {"before": before.get(key), "after": after.get(key)}
    return {"changed": bool(changed), "fields": changed}


def _build_request(case: Mapping[str, Any], dataset: Mapping[str, Any]) -> dict[str, Any]:
    context_payload = _context_for(case, dataset["context_library"])
    context_payload["conversation_id"] = f"semantic-longtail-{case['id']}"
    context = CommandContext.model_validate(context_payload)
    request = {
        "user_input": case.get("message", ""),
        "context": context.model_dump(mode="json"),
        "available_capabilities": [],
    }
    base_messages = [
        {"role": "system", "content": _COMMAND_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False, default=str)},
    ]
    schema = StructuredCommandOutput.model_json_schema()
    messages = add_json_schema_instruction(base_messages, schema)
    return {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 8192,
        "extra_body": {"thinking": {"type": "disabled"}},
        "messages": messages,
        "prompt_source": {
            "system_constant": "packages/agent_core/greenbook_agent_core/command/interpreter.py:_COMMAND_SYSTEM_PROMPT",
            "schema_source": "packages/agent_core/greenbook_agent_core/command/models.py:StructuredCommandOutput",
            "compatibility_path": "packages/agent_core/greenbook_agent_core/llm_compat.py:structured_call",
            "base_system_sha256": _hash(_COMMAND_SYSTEM_PROMPT),
            "final_system_sha256": _hash(str(messages[0]["content"])),
            "base_system_chars": len(_COMMAND_SYSTEM_PROMPT),
            "final_system_chars": len(str(messages[0]["content"])),
            "schema_appended": True,
            "context_in_user_message": True,
        },
        "context_ids_exposed": _ids(context.model_dump(mode="json")),
    }


def main() -> None:
    dataset = _read(DATASET)
    results = _read(INPUT_RESULTS)
    cases = {str(case["id"]): case for case in dataset["cases"]}
    primary_non_exact = [
        item for item in results
        if int(item.get("variant_index") or 0) == 0
        and not bool((item.get("evaluation") or {}).get("exact"))
    ]
    request_rows: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []
    for item in primary_non_exact:
        case = cases[str(item["case_id"])]
        request = _build_request(case, dataset)
        request_rows.append({"case_id": item["case_id"], "message": item["message"], **request})
        stages = item.get("stages") or {}
        case_rows.append({
            "case_id": item["case_id"],
            "message": item["message"],
            "turns": item.get("turns") or [],
            "context": case.get("context", "none"),
            "truth": case.get("truth") or {},
            "provider_usage": item.get("provider_usage") or {},
            "raw_provider_output": stages.get("raw"),
            "parsed_structured": stages.get("schema_parse"),
            "normalized": stages.get("normalized"),
            "segmentation": stages.get("segmentation"),
            "semantic_derivation": stages.get("semantic_derivation"),
            "command": item.get("command"),
            "target_resolution": item.get("target_resolution"),
            "state": item.get("state"),
            "objective_projection": item.get("objective_projection"),
            "evaluation": item.get("evaluation"),
            "stage_diffs": {
                "raw_to_schema": _semantic_diff(stages.get("raw"), stages.get("schema_parse")),
                "schema_to_normalized": _semantic_diff(stages.get("schema_parse"), stages.get("normalized")),
                "normalized_to_segmentation": _semantic_diff(stages.get("normalized"), stages.get("segmentation")),
            },
        })
    _dump(OUTPUT / "provider_requests.json", request_rows)
    _dump(OUTPUT / "primary_non_exact_forensic.json", case_rows)
    _dump(OUTPUT / "request_construction.json", {
        "source": "frozen reconstruction of structured_call; no provider call",
        "system_prompt_source": "packages/agent_core/greenbook_agent_core/command/interpreter.py:_COMMAND_SYSTEM_PROMPT",
        "final_request_source": "packages/agent_core/greenbook_agent_core/llm_compat.py:structured_call",
        "deepseek_branch": "json_object + schema appended to system + max_tokens=8192 + thinking disabled",
        "case_count": len(request_rows),
        "cases": [
            {
                "case_id": row["case_id"],
                "message": row["message"],
                "prompt_source": row["prompt_source"],
                "context_ids_exposed": row["context_ids_exposed"],
                "system_message": row["messages"][0],
                "user_message": row["messages"][1],
            }
            for row in request_rows
        ],
    })
    _dump(OUTPUT / "manifest.json", {
        "dataset": str(DATASET),
        "input_results": str(INPUT_RESULTS),
        "benchmark_version": dataset.get("version"),
        "primary_non_exact_count": len(request_rows),
        "provider_calls_replayed": 0,
        "production_code_changed": False,
    })


if __name__ == "__main__":
    main()
