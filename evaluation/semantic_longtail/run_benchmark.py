"""Run the frozen GreenBook natural-language semantic long-tail benchmark.

This is an evaluation-only driver.  It calls the existing production
CommandInterpreter, TargetResolver, and TemporalResolver boundaries and writes
diagnostic artifacts; it does not create Tasks, invoke tools, or mutate
production state.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
for _path in (
    ROOT / "packages" / "agent_core",
    ROOT / "packages" / "contracts",
    ROOT / "packages" / "evaluation",
    ROOT / "packages" / "security",
    ROOT / "apps" / "agent_api",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


STAGES = (
    "raw",
    "schema_parse",
    "normalized",
    "segmentation",
    "semantic_derivation",
    "target_resolution",
    "temporal_resolution",
    "objective_projection",
)

METRIC_NAMES = (
    "exact_semantic_success",
    "interpreter_failure",
    "schema_parse_failure",
    "semantic_validation_failure",
    "missing_goal",
    "extra_goal",
    "wrong_action",
    "wrong_goal_split",
    "wrong_target",
    "target_ambiguity_missed",
    "wrong_time",
    "temporal_ownership_violation",
    "wrong_publication",
    "constraint_lost",
    "constraint_violation",
    "unnecessary_clarification",
    "missing_clarification",
    "premature_commitment",
    "UNKNOWN_should_have_been_used",
    "normalization_semantic_drift",
    "paraphrase_inconsistency",
)

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:")


def _load_dotenv() -> None:
    """Load only missing benchmark process variables; never print secrets."""

    dotenv = ROOT / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _json_read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class RecordingCompletions:
    def __init__(self, delegate: Any, records: list[dict[str, Any]]) -> None:
        self._delegate = delegate
        self.records = records

    async def create(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        response = await self._delegate.create(**kwargs)
        usage = getattr(response, "usage", None)
        record = {
            "model": str(kwargs.get("model") or ""),
            "temperature": kwargs.get("temperature"),
            "response_format": (kwargs.get("response_format") or {}).get("type")
            if isinstance(kwargs.get("response_format"), Mapping)
            else "",
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }
        self.records.append(record)
        return response


class RecordingClient:
    """Small OpenAI-compatible facade used only to measure provider calls."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.records: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(
            completions=RecordingCompletions(delegate.chat.completions, self.records)
        )
        self.base_url = getattr(delegate, "base_url", "")

    async def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result


def _provider_config() -> dict[str, str]:
    return {
        "provider": os.getenv("AI_PROVIDER", "deepseek"),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": (
            os.getenv("LLM_MODEL")
            or os.getenv("DEEPSEEK_MODEL")
            or os.getenv("DEFAULT_MODEL")
            or "deepseek-v4-flash"
        ),
        "api_key_present": str(
            os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        ).strip().lower()
        not in {"", "none", "null"},
    }


def _context_for(case: Mapping[str, Any], library: Mapping[str, Any]) -> dict[str, Any]:
    context = copy.deepcopy(library.get(case.get("context", "none"), {}))
    context["conversation_id"] = f"semantic-longtail-{case['id']}"
    context["timezone"] = str(library.get("timezone") or "Asia/Shanghai")
    context["history"] = copy.deepcopy(case.get("turns") or [])
    return context


def _read_debug_trace(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            records.append(dict(value))
    return records


def _stage_map(records: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for record in records:
        stage = str(record.get("stage") or "")
        if stage:
            result[stage] = record.get("payload")
    return result


def _upper(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "_")


def _canonical_publication(value: Any) -> str:
    value = _upper(value)
    aliases = {
        "DRAFT": "DRAFT_ONLY",
        "SAVE_DRAFT": "DRAFT_ONLY",
        "DO_NOT_PUBLISH": "DRAFT_ONLY",
        "NO_PUBLISH": "DRAFT_ONLY",
        "PUBLISH": "IMMEDIATE_PUBLISH",
        "PUBLISH_NOW": "IMMEDIATE_PUBLISH",
        "IMMEDIATE": "IMMEDIATE_PUBLISH",
        "NOW": "IMMEDIATE_PUBLISH",
        "SCHEDULE": "SCHEDULED_PUBLISH",
        "SCHEDULED": "SCHEDULED_PUBLISH",
        "FUTURE": "SCHEDULED_PUBLISH",
        "PUBLISH_LATER": "SCHEDULED_PUBLISH",
    }
    return aliases.get(value, value)


def _publication_from(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    for key in ("publication_intent", "publication_mode", "content_state"):
        value_found = _canonical_publication(value.get(key))
        if value_found:
            return value_found
    if value.get("publish_now") is True:
        return "IMMEDIATE_PUBLISH"
    if value.get("schedule") is True or value.get("publish") is True:
        return "SCHEDULED_PUBLISH"
    if value.get("publish") is False or value.get("schedule") is False:
        return "DRAFT_ONLY"
    return ""


def _collect_strings(value: Any, *, key: str = "") -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for name, item in value.items():
            result.extend(_collect_strings(item, key=str(name)))
    elif isinstance(value, list):
        for item in value:
            result.extend(_collect_strings(item, key=key))
    elif isinstance(value, str):
        result.append((key, value))
    return result


def _pre_resolution_iso_values(stages: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for name in ("raw", "schema_parse", "normalized", "segmentation", "semantic_derivation"):
        for key, value in _collect_strings(stages.get(name)):
            if key in {"run_at", "publish_at", "scheduled_at"} and ISO_RE.match(value.strip()):
                values.append(value)
    return values


def _payload_evidence(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"goal_count": 0, "publication": "", "temporal": [], "target": {}}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if items:
        goal_count = len(items)
    elif payload.get("goal") or payload.get("objective"):
        goal_count = 1
    else:
        goal_count = 0
    publications: list[str] = []
    for container in (payload, payload.get("constraints"), payload.get("parameters"), payload.get("entities")):
        intent = _publication_from(container)
        if intent:
            publications.append(intent)
    for item in items:
        if isinstance(item, Mapping):
            intent = _publication_from(item.get("constraints"))
            if intent:
                publications.append(intent)
    temporal: list[str] = []
    for key, value in _collect_strings(payload):
        if key in {"temporal_text", "temporal_expression", "time", "run_at", "publish_at", "scheduled_at"}:
            if value.strip():
                temporal.append(value.strip())
    target = payload.get("target") if isinstance(payload.get("target"), Mapping) else {}
    delta_refs: list[Mapping[str, Any]] = []
    for delta in payload.get("task_changes") or []:
        if isinstance(delta, Mapping):
            reference = delta.get("target_reference")
            if isinstance(reference, Mapping):
                delta_refs.append(reference)
    return {
        "goal_count": goal_count,
        "publication": publications[-1] if publications else "",
        "publications": publications,
        "temporal": temporal,
        "target": dict(target),
        "delta_refs": [dict(item) for item in delta_refs],
    }


def _actual_goal_items(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = state.get("items")
    if isinstance(items, list) and items:
        return [item for item in items if isinstance(item, Mapping)]
    objectives = state.get("objectives")
    if isinstance(objectives, list):
        return [item for item in objectives if isinstance(item, Mapping)]
    return []


def _goal_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    wanted = str(expected.get("topic") or "").strip().casefold()
    if not wanted:
        return True
    haystack = " ".join(
        str(actual.get(key) or "")
        for key in ("title", "topic", "description", "intent", "requirements")
    ).casefold()
    return wanted in haystack or any(token in haystack for token in wanted.split() if len(token) > 1)


def _publication_mode_expected(value: Any) -> str:
    value = _upper(value)
    return {"UNSPECIFIED": "NONE", "DRAFT": "DRAFT_ONLY"}.get(value, value)


_CORE_LIFECYCLE_CONSTRAINTS = {
    "NEW_OBJECTIVE",
    "HISTORICAL_RESOURCE_CONTINUITY",
}


def _core_target_text(actual: Mapping[str, Any]) -> str:
    """Collect semantic labels, excluding canonical identity fields."""

    values: list[str] = []
    command = actual.get("command") or {}
    state = actual.get("state") or {}
    target = actual.get("target_resolution") or {}

    def collect(value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        for key in ("label", "reference", "value", "property", "description", "goal", "topic"):
            item = str(value.get(key) or "").strip()
            if item:
                values.append(item.casefold())

    collect(command.get("target"))
    collect(target.get("target"))
    collect(state.get("target_reference"))
    collect(state.get("resolved_target"))
    for delta in command.get("task_changes") or ():
        if isinstance(delta, Mapping):
            collect(delta.get("target_reference"))
            collect(delta.get("source_reference"))
    for item in _actual_goal_items(state):
        collect(item.get("target_reference"))
    return " ".join(values)


def _core_target_matches(case: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    truth = case.get("truth") or {}
    expected = _upper(truth.get("target_resolution"))
    command = actual.get("command") or {}
    target = actual.get("target_resolution") or {}
    state = actual.get("state") or {}
    status = _upper(target.get("status") or command.get("target_resolution")) or "NONE"
    clarification = bool((actual.get("canonical") or {}).get("clarification_required"))

    if expected == "NONE":
        return status in {"NONE", "NOT_FOUND"} or not command.get("target") and not command.get("task_changes")
    if expected == "RECONCILIATION_REQUIRED":
        reason = str(target.get("reason") or state.get("clarification_reason") or "")
        return "reconcil" in reason.casefold() or clarification
    if expected == "AMBIGUOUS":
        return status == "AMBIGUOUS" or clarification
    if expected == "NOT_FOUND":
        return status in {"NOT_FOUND", "NONE"} or clarification
    if expected != "RESOLVED":
        return True
    # User-triggered retry creates a new Objective.  The semantic stage may
    # retain only a FAILED_OBJECTIVE_RETRY source reference; requiring the new
    # Objective's canonical id here would make the evaluator depend on runtime
    # identity rather than the user's historical semantic reference.
    if status != "RESOLVED":
        retry_source = " ".join(
            json.dumps(delta.get("source_reference") or {}, ensure_ascii=False)
            for delta in command.get("task_changes") or ()
            if isinstance(delta, Mapping)
        ).casefold()
        if "failed_objective_retry" not in retry_source:
            return False
    expected_topics = [
        str(goal.get("topic") or "").strip().casefold()
        for goal in truth.get("goals") or []
        if isinstance(goal, Mapping) and str(goal.get("topic") or "").strip()
    ]
    if not expected_topics:
        return True
    text = _core_target_text(actual)
    return any(topic in text for topic in expected_topics)


def _core_action_matches(
    expected: str,
    actual: str,
    state: Mapping[str, Any],
    command: Mapping[str, Any],
) -> bool:
    items = _actual_goal_items(state)
    item_operations = {
        _upper(item.get("operation"))
        for item in items
        if isinstance(item, Mapping) and item.get("operation")
    }
    delta_operations = {
        _upper((delta.get("desired_changes") or {}).get("semantic_action"))
        for delta in command.get("task_changes") or ()
        if isinstance(delta, Mapping)
        and isinstance(delta.get("desired_changes") or {}, Mapping)
        and (delta.get("desired_changes") or {}).get("semantic_action")
    }
    lifecycle_operations = {
        _upper(delta.get("operation"))
        for delta in command.get("task_changes") or ()
        if isinstance(delta, Mapping) and delta.get("operation")
    }

    # Canonical projection is allowed to collapse a mutation into its
    # terminal publication family, but it must not erase the distinction
    # between UPDATE_GOAL on an existing resource and CREATE_TASK.  The
    # latter is precisely the unsafe duplicate-create shape in F06.
    existing_mutation = bool(
        item_operations.intersection({"MODIFY", "UPDATE", "UPDATE_DRAFT", "UPDATE_SCHEDULE", "REVISE", "CANCEL_SCHEDULE"})
        or delta_operations.intersection({"UPDATE_DRAFT", "UPDATE_SCHEDULE", "CANCEL_SCHEDULE", "PUBLISH_NOW"})
        and lifecycle_operations.intersection({"UPDATE_GOAL", "CANCEL_GOAL"})
    )
    if expected == "REVISE" and (
        "NO_CHANGE" in lifecycle_operations
        or _upper(command.get("semantic_operation")) in {"PRESERVE", "NO_CHANGE"}
    ):
        return True
    if expected == "REVISE" and "CREATE_TASK" in lifecycle_operations and not existing_mutation:
        return False
    if expected in item_operations or expected in delta_operations:
        return expected != "REVISE" or existing_mutation
    if expected == actual:
        if expected != "REVISE":
            return True
        return existing_mutation
    if expected == "REVISE" and actual == "PUBLISH_NOW":
        # "改标题再发" is one user outcome with a revise mutation and an
        # immediate publication requirement; the canonical projection may
        # expose the publication family as the terminal action.
        return any(
            _upper(item.get("operation")) in {"MODIFY", "UPDATE", "UPDATE_DRAFT", "REVISE"}
            and _canonical_publication(_publication_from(item)) == "IMMEDIATE_PUBLISH"
            for item in items
        )
    if expected == "PUBLISH_NOW" and actual == "REVISE":
        return any(
            _canonical_publication(_publication_from(item)) == "IMMEDIATE_PUBLISH"
            for item in items
        )
    return False


def _core_publication_matches(truth: Mapping[str, Any], state: Mapping[str, Any], canonical: Mapping[str, Any]) -> bool:
    expected = _publication_mode_expected(truth.get("publication_mode"))
    items = _actual_goal_items(state)
    item_publications = [_canonical_publication(_publication_from(item)) for item in items]
    item_publications = [value for value in item_publications if value]
    actual = _upper(canonical.get("publication_mode"))
    if expected == "NONE":
        # No publication intent is different from a product's later default
        # Draft behavior; this evaluator compares only semantic evidence.
        return not item_publications and _canonical_publication(state.get("publication_intent")) in {"", "NONE"}
    if expected == "DRAFT_ONLY":
        return "DRAFT_ONLY" in item_publications or actual == "DRAFT_ONLY"
    if expected == "IMMEDIATE":
        return actual == "IMMEDIATE" or "IMMEDIATE_PUBLISH" in item_publications
    if expected == "SCHEDULED":
        return actual == "SCHEDULED" or "SCHEDULED_PUBLISH" in item_publications
    if expected == "UNRESOLVED":
        return actual == "UNRESOLVED" or bool(canonical.get("clarification_required"))
    if expected == "MIXED":
        return actual == "MIXED" or len(set(item_publications)) > 1
    return True


def _semantic_publication_values(actual: Mapping[str, Any]) -> list[str]:
    """Read publication intent before deterministic product defaults.

    A later product policy may create a Draft for an unspecified request. That
    policy must not turn an UNKNOWN semantic field into DRAFT_ONLY truth.
    """

    raw = (actual.get("stages") or {}).get("raw")
    if isinstance(raw, Mapping):
        values: list[str] = []
        values.extend(
            _canonical_publication(_publication_from(raw.get("constraints") or {}))
            for _ in [0]
        )
        for item in raw.get("items") or ():
            if isinstance(item, Mapping):
                values.append(_canonical_publication(_publication_from(item.get("constraints") or item)))
        values = [value for value in values if value and value != "UNKNOWN"]
        if values or raw.get("constraints") is not None or raw.get("items") is not None:
            return values
    state = actual.get("state") or {}
    values = [_canonical_publication(_publication_from(item)) for item in _actual_goal_items(state)]
    top = _canonical_publication(_publication_from(state))
    if top:
        values.append(top)
    return [value for value in values if value and value != "UNKNOWN"]


def _core_temporal_matches(truth: Mapping[str, Any], state: Mapping[str, Any], canonical: Mapping[str, Any]) -> bool:
    expected = _upper(truth.get("temporal_kind"))
    actual = _upper(canonical.get("temporal_kind"))
    item_temporals = [
        _upper(item.get("temporal_kind"))
        for item in _actual_goal_items(state)
        if isinstance(item, Mapping)
    ]
    if expected == "NONE":
        # Direct publication is an action-time fact, not a user-supplied
        # temporal expression.  B03/B07 therefore remain temporal NONE even
        # when canonical projection reports NOW for the immediate item.
        return actual == "NONE" or (
            actual == "NOW"
            and not any(
                _upper(item.get("temporal_kind")) in {"FUTURE", "UNRESOLVED"}
                or item.get("temporal_text")
                for item in _actual_goal_items(state)
            )
        )
    if expected == "NOW":
        return (
            actual == "NOW" and bool(canonical.get("temporal_resolved"))
        ) or any(value == "NOW" for value in item_temporals)
    if expected == "FUTURE":
        return (
            actual == "FUTURE" and bool(canonical.get("temporal_resolved"))
        ) or any(value == "FUTURE" for value in item_temporals)
    if expected == "UNRESOLVED":
        return actual == "UNRESOLVED" or bool(canonical.get("clarification_required")) or "UNRESOLVED" in item_temporals
    if expected == "MIXED":
        return actual == "MIXED" or len({value for value in item_temporals if value not in {"", "NONE"}}) > 1
    return actual == expected


def _core_intent_evaluation(case: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate user-facing semantic intent independently of representation."""

    truth = case.get("truth") or {}
    canonical = actual.get("canonical") or {}
    state = actual.get("state") or {}
    if actual.get("error"):
        return {"exact": False, "unsafe": False, "reasons": ["interpreter_error"]}

    expected_clarify = bool(truth.get("clarification"))
    actual_clarify = bool(canonical.get("clarification_required"))
    if expected_clarify and actual_clarify:
        return {"exact": True, "unsafe": False, "reasons": [], "safe_clarification": True}
    if not expected_clarify and actual_clarify:
        return {"exact": False, "unsafe": False, "reasons": ["unnecessary_clarification"]}

    reasons: list[str] = []
    expected_count = truth.get("objective_count")
    actual_count = canonical.get("objective_count")
    if expected_count is not None and actual_count != expected_count:
        reasons.append("wrong_goal_split")
    expected_action = _upper(truth.get("action_family"))
    actual_action = _upper(canonical.get("action_family"))
    if not _core_action_matches(expected_action, actual_action, state, actual.get("command") or {}):
        reasons.append("wrong_action")
    if not _core_target_matches(case, actual):
        reasons.append("wrong_target")
    semantic_publications = _semantic_publication_values(actual)
    semantic_state = dict(state)
    raw_semantic = (actual.get("stages") or {}).get("raw")
    if isinstance(raw_semantic, Mapping):
        semantic_state["publication_intent"] = ""
        semantic_state["items"] = [
            {**item, "publication_intent": ""}
            for item in _actual_goal_items(state)
        ]
    if semantic_publications:
        semantic_state["publication_intent"] = "MIXED" if len(set(semantic_publications)) > 1 else semantic_publications[0]
        items = _actual_goal_items(state)
        if items:
            item_values = semantic_publications[-len(items):]
            semantic_state["items"] = [
                {**item, "publication_intent": value}
                for item, value in zip(items, item_values, strict=False)
            ]
    if not _core_publication_matches(truth, semantic_state, canonical):
        reasons.append("wrong_publication")
    if not _core_temporal_matches(truth, state, canonical):
        reasons.append("wrong_time")

    preserved, violated = _constraint_checks(case, actual.get("command") or {}, canonical, state)
    expected_constraints = {
        str(item).upper()
        for item in truth.get("constraints") or []
        if str(item).upper() not in _CORE_LIFECYCLE_CONSTRAINTS
    }
    if not expected_constraints.issubset(preserved):
        reasons.append("constraint_lost")
    if violated.intersection(expected_constraints):
        reasons.append("constraint_violation")

    unsafe_reasons = {
        "wrong_target",
        "wrong_action",
        "wrong_publication",
        "wrong_time",
        "constraint_lost",
        "constraint_violation",
    }
    unsafe = bool(set(reasons).intersection(unsafe_reasons))
    return {
        "exact": not reasons,
        "unsafe": unsafe,
        "reasons": sorted(set(reasons)),
        "safe_clarification": False,
    }


def _target_status(result: Any, command: Mapping[str, Any]) -> tuple[str, str, str]:
    if result is not None:
        status = _upper(result.get("status")) if isinstance(result, Mapping) else ""
        reason = str(result.get("reason") or "") if isinstance(result, Mapping) else ""
        target = result.get("target") if isinstance(result, Mapping) else None
        identity = ""
        if isinstance(target, Mapping):
            identity = str(target.get("resource_id") or target.get("id") or "")
            metadata = target.get("metadata")
            if isinstance(metadata, Mapping):
                identity = identity or str(metadata.get("objective_id") or "")
        return status or "NOT_FOUND", identity, reason
    if command.get("target") or command.get("task_changes"):
        return _upper(command.get("target_resolution")) or "NOT_FOUND", "", ""
    return "NONE", "", ""


def _serialize_command(command: Mapping[str, Any]) -> str:
    return json.dumps(command, ensure_ascii=False, default=str).casefold()


def _command_target_identities(command: Mapping[str, Any]) -> set[str]:
    """Collect identity evidence before coordinator projects to owning Task."""

    values: set[str] = set()

    def collect(value: Any) -> None:
        if not isinstance(value, Mapping):
            return
        for key in (
            "id",
            "resource_id",
            "objective_id",
            "target_objective_id",
            "goal_id",
            "task_id",
        ):
            item = str(value.get(key) or "").strip()
            if item:
                values.add(item)

    collect(command.get("target"))
    for delta in command.get("task_changes") or []:
        if isinstance(delta, Mapping):
            collect(delta.get("target_reference"))
            collect(delta.get("source_reference"))
    return values


def _constraint_checks(case: Mapping[str, Any], command: Mapping[str, Any], canonical: Mapping[str, Any], state: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    expected = [str(item).upper() for item in (case.get("truth", {}).get("constraints") or [])]
    serialized = _serialize_command(command)
    publication = _upper(canonical.get("publication_mode"))
    item_publications = {
        _upper(item.get("publication_intent"))
        for item in _actual_goal_items(state)
        if item.get("publication_intent")
    }
    preserved: set[str] = set()
    violated: set[str] = set()
    for marker in expected:
        if marker in {"DO_NOT_PUBLISH", "DRAFT_ONLY"}:
            if publication == "DRAFT_ONLY" or (
                marker in {"DO_NOT_PUBLISH", "REVIEW_BEFORE_PUBLISH"}
                and item_publications
                and item_publications <= {"DRAFT_ONLY"}
            ):
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker == "KEEP_DRAFT":
            if "cancel_schedule" in serialized or "keep_draft" in serialized:
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker in {"PRESERVE_SCHEDULE", "PRESERVE_OTHER_FIELDS"}:
            # Omission of an update field is the existing partial-update
            # contract.  A newly invented run_at is a direct violation.
            explicit_updates: list[Mapping[str, Any]] = []
            for delta in command.get("task_changes") or []:
                if isinstance(delta, Mapping) and isinstance(delta.get("desired_changes"), Mapping):
                    explicit_updates.append(delta["desired_changes"])
            for item in command.get("items") or []:
                if isinstance(item, Mapping):
                    explicit_updates.append(item)
            explicit_text = _serialize_command({"updates": explicit_updates})
            if not any(key in explicit_text for key in ("run_at", "publish_at", "scheduled_at", "publish_time")):
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker in {"AGENT_NO_CHANGE", "NO_CHANGE"}:
            if "no_change" in serialized or "no change" in serialized:
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker in {"FAILED_ONLY", "EXCLUDE_COMPLETED", "RETRY_FAILED_ONLY"}:
            if any(token in serialized for token in ("failed_objective_retry", "failed_objective", "retry", "failed")):
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker == "NO_BLIND_RETRY":
            if "reconcile" in serialized or bool(canonical.get("clarification_required")):
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker == "REVIEW_BEFORE_PUBLISH":
            expected_goals = list((case.get("truth") or {}).get("goals") or [])
            review_topics = {
                str(goal.get("topic") or "").strip()
                for goal in expected_goals
                if _canonical_publication(goal.get("publication")) == "DRAFT_ONLY"
            }
            actual_items = _actual_goal_items(state)
            review_preserved = (
                publication == "DRAFT_ONLY"
                if not review_topics
                else all(
                    any(
                        _goal_matches(item, {"topic": topic})
                        and _publication_from(item) == "DRAFT_ONLY"
                        for item in actual_items
                    )
                    for topic in review_topics
                )
            )
            if review_preserved:
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker.startswith("TITLE="):
            title = marker.split("=", 1)[1].casefold()
            if title in serialized:
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker == "NEW_OBJECTIVE":
            # The semantic contract exposes the retry marker on TaskDelta; a
            # missing marker is recorded as lost evidence, not guessed away.
            if "user_triggered_retry" in serialized or "failed_objective_retry" in serialized or "retry_of_objective" in serialized:
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker == "HISTORICAL_RESOURCE_CONTINUITY":
            if state.get("resolved_target") or state.get("target_reference"):
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker == "NO_RESOURCE_REUSE":
            if "create" in serialized:
                preserved.add(marker)
            else:
                violated.add(marker)
        elif marker == "PUBLICATION_UNDECIDED":
            if publication == "NONE":
                preserved.add(marker)
            else:
                violated.add(marker)
    return preserved, violated


def _semantic_signature(actual: Mapping[str, Any]) -> tuple[Any, ...]:
    canonical = actual.get("canonical") or {}
    state = actual.get("state") or {}
    target = actual.get("target_resolution") or {}
    goals = []
    for item in _actual_goal_items(state):
        goals.append((
            str(item.get("topic") or item.get("title") or "").strip().casefold(),
            _upper(item.get("publication_intent") or item.get("operation")),
            _upper(item.get("temporal_kind")),
        ))
    return (
        _upper(canonical.get("action_family")),
        _upper(canonical.get("publication_mode")),
        _upper(canonical.get("temporal_kind")),
        bool(canonical.get("temporal_resolved")),
        _upper(canonical.get("target_state")),
        bool(canonical.get("clarification_required")),
        canonical.get("objective_count"),
        _upper(target.get("status")),
        tuple(sorted(goals)),
    )


def _provenance_coverage(case: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, bool]:
    """Measure existing in-band evidence without inventing source spans."""

    truth = case.get("truth") or {}
    command = actual.get("command") or {}
    state = actual.get("state") or {}
    stages = actual.get("stages") or {}
    message = str(actual.get("message") or case.get("message") or "")
    expected_target = _upper(truth.get("target_resolution"))
    expected_expression = str(truth.get("temporal_expression") or "").strip()
    target_refs = _command_target_identities(command)
    target_payload = command.get("target") if isinstance(command.get("target"), Mapping) else {}
    target_ref_text = " ".join(
        str(target_payload.get(key) or "")
        for key in ("reference", "label", "value", "property")
    )
    for delta in command.get("task_changes") or []:
        if isinstance(delta, Mapping):
            ref = delta.get("target_reference") if isinstance(delta.get("target_reference"), Mapping) else {}
            target_ref_text += " " + " ".join(str(ref.get(key) or "") for key in ("reference", "label", "value", "property"))
    temporal_values = []
    for payload in (stages.get("raw"), stages.get("schema_parse"), command, state):
        evidence = _payload_evidence(payload)
        temporal_values.extend(str(value) for value in evidence.get("temporal", []))
    for item in _actual_goal_items(state):
        temporal_values.append(str(item.get("temporal_text") or ""))
    serialized = _serialize_command(command)
    expected_constraints = [str(item).upper() for item in truth.get("constraints") or []]
    preserved, _violated = _constraint_checks(case, command, actual.get("canonical") or {}, state)
    topic_values = [str(item.get("topic") or "") for item in _actual_goal_items(state)]
    goal_text = str(command.get("goal") or command.get("objective") or "")
    desired_topics = [str(item.get("topic") or "") for item in truth.get("goals") or [] if item.get("topic")]
    goal_covered = bool(goal_text) and (
        not desired_topics or all(topic.casefold() in goal_text.casefold() or any(token.casefold() in goal_text.casefold() for token in topic.split() if len(token) > 1) for topic in desired_topics)
    )
    target_covered = expected_target == "NONE" or bool(target_refs or target_ref_text.strip())
    temporal_covered = not expected_expression or any(
        expected_expression in value or value in expected_expression
        for value in temporal_values
        if value
    )
    hard_constraints_covered = not expected_constraints or set(expected_constraints) <= preserved
    return {
        "desired_outcome": goal_covered,
        "target_reference": target_covered,
        "temporal_expression": temporal_covered,
        "hard_constraints": hard_constraints_covered,
        "formal_source_span": False,
    }


def _state_publication_matches_expected(state: Mapping[str, Any], truth: Mapping[str, Any]) -> bool:
    expected = _publication_mode_expected(truth.get("publication_mode"))
    items = _actual_goal_items(state)
    expected_goals = list(truth.get("goals") or [])
    if expected_goals and items:
        for goal in expected_goals:
            wanted = _canonical_publication(goal.get("publication"))
            if wanted in {"", "UNSPECIFIED"}:
                continue
            if not any(
                _goal_matches(item, goal)
                and _canonical_publication(_publication_from(item)) == wanted
                for item in items
            ):
                return False
        return True
    actual = _canonical_publication(state.get("publication_intent"))
    if expected == "NONE":
        return actual in {"", "UNSPECIFIED"}
    if expected == "DRAFT_ONLY":
        return actual == "DRAFT_ONLY" or bool(items) and all(
            _canonical_publication(_publication_from(item)) == "DRAFT_ONLY"
            for item in items
        )
    if expected in {"IMMEDIATE", "IMMEDIATE_PUBLISH"}:
        return actual in {"IMMEDIATE", "IMMEDIATE_PUBLISH"}
    if expected in {"SCHEDULED", "SCHEDULED_PUBLISH"}:
        return actual in {"SCHEDULED", "SCHEDULED_PUBLISH"}
    if expected == "MIXED":
        publications = {
            _canonical_publication(_publication_from(item))
            for item in items
            if _publication_from(item)
        }
        return len(publications) > 1
    return False


def _state_temporal_matches_expected(state: Mapping[str, Any], truth: Mapping[str, Any]) -> bool:
    expected = _upper(truth.get("temporal_kind"))
    actual = _upper(state.get("temporal_kind"))
    items = _actual_goal_items(state)
    item_temporals = {_upper(item.get("temporal_kind")) for item in items if item.get("temporal_kind")}
    resolved = bool(state.get("temporal_resolved"))
    if expected == "NONE":
        return actual == "NONE" and item_temporals <= {"", "NONE"}
    if expected == "NOW":
        return actual == "NOW" and resolved
    if expected == "FUTURE":
        return actual == "FUTURE" and resolved
    if expected == "UNRESOLVED":
        return actual == "UNRESOLVED" or bool(state.get("clarification_required"))
    if expected == "MIXED":
        return actual == "MIXED"
    return actual == expected


def _first_bad_state(
    *,
    case: Mapping[str, Any],
    actual: Mapping[str, Any],
    metrics: set[str],
) -> str:
    if actual.get("error"):
        code = _upper(actual.get("error_code"))
        return "Schema/parse" if "SCHEMA" in code or "VALIDATION" in code else "Interpreter"
    if not metrics:
        return ""
    truth = case.get("truth") or {}
    stages = actual.get("stages") or {}
    expected_pub = _publication_mode_expected(truth.get("publication_mode"))
    expected_temporal = _upper(truth.get("temporal_kind"))
    expected_target = _upper(truth.get("target_resolution"))
    expected_expr = str(truth.get("temporal_expression") or "").strip()

    def evidence_bad(payload: Any, *, stage: str) -> bool:
        evidence = _payload_evidence(payload)
        pubs = {_upper(_publication_mode_expected(item)) for item in evidence.get("publications", [])}
        if expected_pub == "NONE" and pubs:
            return True
        if expected_pub == "DRAFT_ONLY" and "DRAFT_ONLY" not in pubs:
            return True
        if expected_pub == "IMMEDIATE" and not pubs.intersection({"IMMEDIATE_PUBLISH", "IMMEDIATE"}):
            return True
        if expected_pub in {"SCHEDULED", "UNRESOLVED"} and not pubs.intersection({"SCHEDULED_PUBLISH", "SCHEDULED", "UNRESOLVED"}):
            return True
        # semantic_derivation is a compact summary and intentionally omits
        # target/time evidence.  Its omission is not semantic loss.
        if stage != "semantic_derivation" and expected_expr and not any(expected_expr in value or value in expected_expr for value in evidence.get("temporal", [])):
            return True
        target = evidence.get("target") or {}
        refs = evidence.get("delta_refs") or []
        if stage != "semantic_derivation" and expected_target == "RESOLVED" and not (target or refs):
            return True
        if stage != "semantic_derivation" and expected_target in {"AMBIGUOUS", "NOT_FOUND", "RECONCILIATION_REQUIRED"} and not (target or refs):
            # A targetless incomplete sentence is allowed to fail later in
            # semantic projection; it is not evidence of an interpreter miss.
            return False
        return False

    if not stages.get("raw"):
        return "Interpreter"
    if evidence_bad(stages.get("raw"), stage="raw"):
        return "Interpreter"
    for stage in ("schema_parse", "normalized", "segmentation", "semantic_derivation"):
        if stages.get(stage) is not None and evidence_bad(stages.get(stage), stage=stage):
            return "Normalization" if stage in {"normalized", "segmentation", "semantic_derivation"} else "Schema/parse"
    if "wrong_target" in metrics or "target_ambiguity_missed" in metrics:
        return "TargetResolver"
    if "wrong_time" in metrics or "UNKNOWN_should_have_been_used" in metrics or "temporal_ownership_violation" in metrics:
        if _state_temporal_matches_expected(actual.get("state") or {}, truth):
            return "Objective projection"
        return "TemporalResolver" if expected_temporal != "NONE" else "Interpreter"
    if "wrong_publication" in metrics:
        if _state_publication_matches_expected(actual.get("state") or {}, truth):
            return "Objective projection"
        return "Normalization"
    if "normalization_semantic_drift" in metrics:
        return "Normalization"
    if metrics:
        return "Objective projection"
    return ""


def _evaluate_case(case: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    truth = case.get("truth") or {}
    canonical = actual.get("canonical") or {}
    state = actual.get("state") or {}
    command = actual.get("command") or {}
    target = actual.get("target_resolution") or {}
    if actual.get("error"):
        code = _upper(actual.get("error_code"))
        if "SCHEMA" in code or "VALIDATION" in code:
            failure_metric = "schema_parse_failure"
        elif "SEMANTIC_INVALID" in code:
            failure_metric = "semantic_validation_failure"
        else:
            failure_metric = "interpreter_failure"
        evaluation = {
            "metrics": [failure_metric],
            "exact": False,
            "strict_exact": False,
            # The current boundary fails closed; a rejected semantic
            # candidate does not itself imply an unsafe WRITE.
            "unsafe": False,
            "strict_unsafe": False,
            "core_intent_exact": False,
            "core_intent_unsafe": False,
            "core_intent_reasons": [failure_metric],
            "first_bad_state": _first_bad_state(case=case, actual=actual, metrics={failure_metric}),
            "constraint_preserved": [],
            "constraint_violated": [],
            "target_reason": "",
            "provenance": _provenance_coverage(case, actual),
        }
        return evaluation
    metrics: set[str] = set()
    expected_action = _upper(truth.get("action_family"))
    actual_action = _upper(canonical.get("action_family"))
    expected_count = truth.get("objective_count")
    actual_count = canonical.get("objective_count")
    actual_items = _actual_goal_items(state)
    expected_goals = list(truth.get("goals") or [])
    if expected_count is not None and actual_count != expected_count:
        if actual_count is None or int(actual_count or 0) < int(expected_count):
            metrics.add("missing_goal")
        if actual_count is not None and int(actual_count or 0) > int(expected_count):
            metrics.add("extra_goal")
        metrics.add("wrong_goal_split")
    # Existing-resource mutations intentionally project their business work
    # through TaskDelta/target evidence and may have no ResolvedSemanticItem.
    # Only CREATE/MULTI_OBJECTIVE item ownership is judged by item topics here;
    # otherwise the evaluator would call a valid UPDATE_DRAFT a missing goal.
    item_owned_semantics = bool(actual_items) or expected_action in {"CREATE", "MULTI_OBJECTIVE"} or actual_action in {"CREATE", "MULTI_OBJECTIVE"}
    if item_owned_semantics:
        if expected_count is not None and len(actual_items) < len(expected_goals) and not metrics.intersection({"missing_goal", "wrong_goal_split"}):
            metrics.add("missing_goal")
        unmatched = 0
        for expected in expected_goals:
            if expected.get("topic") and not any(_goal_matches(item, expected) for item in actual_items):
                unmatched += 1
        if unmatched:
            metrics.add("missing_goal")
            if expected_count == len(actual_items):
                metrics.add("wrong_goal_split")

    if expected_action != actual_action:
        metrics.add("wrong_action")
        if expected_action == "MULTI_OBJECTIVE" or actual_action == "MULTI_OBJECTIVE":
            metrics.add("wrong_goal_split")

    expected_pub = _publication_mode_expected(truth.get("publication_mode"))
    actual_pub = _upper(canonical.get("publication_mode"))
    if expected_pub != actual_pub:
        metrics.add("wrong_publication")

    expected_temporal = _upper(truth.get("temporal_kind"))
    actual_temporal = _upper(canonical.get("temporal_kind"))
    if expected_temporal != actual_temporal or bool(truth.get("temporal_resolved")) != bool(canonical.get("temporal_resolved")):
        metrics.add("wrong_time")
    if _pre_resolution_iso_values(actual.get("stages") or {}):
        metrics.add("temporal_ownership_violation")

    expected_target = _upper(truth.get("target_resolution"))
    target_status = _upper(target.get("status")) or "NONE"
    target_reason = str(target.get("reason") or "")
    if expected_target == "RECONCILIATION_REQUIRED":
        target_ok = "reconciliation" in target_reason.casefold()
    elif expected_target == "NONE":
        target_ok = target_status in {"NONE", "NOT_FOUND"} and not command.get("target") and not command.get("task_changes")
    else:
        target_ok = target_status == expected_target
    identity = str(truth.get("target_identity") or "")
    if target_ok and identity:
        identity_values = {
            str(target.get("identity") or ""),
            str((state.get("resolved_target") or {}).get("resource_id") or ""),
            str((state.get("resolved_target") or {}).get("objective_id") or ""),
            str((state.get("resolved_target") or {}).get("task_id") or ""),
        }
        identity_values.update(_command_target_identities(command))
        target_ok = identity in identity_values
    if not target_ok:
        metrics.add("wrong_target")
        if expected_target == "AMBIGUOUS":
            metrics.add("target_ambiguity_missed")

    expected_clarify = bool(truth.get("clarification"))
    actual_clarify = bool(canonical.get("clarification_required"))
    if not expected_clarify and actual_clarify:
        metrics.add("unnecessary_clarification")
    if expected_clarify and not actual_clarify:
        metrics.add("missing_clarification")
        if expected_target in {"AMBIGUOUS", "NOT_FOUND", "RECONCILIATION_REQUIRED"} or expected_temporal == "UNRESOLVED":
            metrics.add("premature_commitment")

    preserved, violated = _constraint_checks(case, command, canonical, state)
    if truth.get("constraints") and len(preserved) < len(set(str(item).upper() for item in truth.get("constraints") or [])):
        metrics.add("constraint_lost")
    if violated:
        metrics.add("constraint_violation")
    if expected_pub == "NONE" and actual_pub not in {"", "NONE"}:
        metrics.add("UNKNOWN_should_have_been_used")

    stages = actual.get("stages") or {}
    if stages.get("schema_parse") is not None and stages.get("normalized") is not None:
        before = _payload_evidence(stages.get("schema_parse"))
        after = _payload_evidence(stages.get("normalized"))
        if before.get("publication") != after.get("publication") and before.get("publication"):
            metrics.add("normalization_semantic_drift")

    exact = not metrics and not actual.get("error")
    if exact:
        metrics.add("exact_semantic_success")
    first_bad = _first_bad_state(case=case, actual=actual, metrics=metrics - {"exact_semantic_success"})
    # A clarification is fail-closed: field mismatches in a response that
    # cannot execute do not constitute a dangerous semantic WRITE.  Keep
    # those mismatches in exactness metrics, but reserve unsafe for a
    # non-clarifying path or a missing/invalid safety boundary.
    unsafe = bool(metrics.intersection({
        "temporal_ownership_violation",
        "premature_commitment",
        "missing_clarification",
        "UNKNOWN_should_have_been_used",
    }))
    if not actual_clarify:
        unsafe = unsafe or bool(metrics.intersection({
            "wrong_target",
            "target_ambiguity_missed",
            "wrong_time",
            "wrong_publication",
            "constraint_violation",
        }))
    core = _core_intent_evaluation(case, actual)
    return {
        "metrics": sorted(metrics),
        "exact": exact,
        "strict_exact": exact,
        "unsafe": unsafe,
        "strict_unsafe": unsafe,
        "core_intent_exact": bool(core.get("exact")),
        "core_intent_unsafe": bool(core.get("unsafe")),
        "core_intent_reasons": list(core.get("reasons") or []),
        "safe_clarification": bool(core.get("safe_clarification")),
        "first_bad_state": first_bad,
        "constraint_preserved": sorted(preserved),
        "constraint_violated": sorted(violated),
        "target_reason": target_reason,
        "provenance": _provenance_coverage(case, actual),
    }


async def _run_one(
    case: Mapping[str, Any],
    *,
    message: str,
    context_library: Mapping[str, Any],
    client: RecordingClient,
    model: str,
    timezone: str,
    fixed_now: datetime,
    snapshot_dir: Path,
    scoped_context: bool = False,
) -> dict[str, Any]:
    from greenbook_agent_core.command import CommandInterpreter
    from greenbook_agent_core.command.models import CommandContext
    from greenbook_agent_core.context.models import ContextSnapshot
    from greenbook_agent_core.context.projection import project_interpreter_context
    from greenbook_agent_core.execution.temporal_resolver import TemporalResolver
    from greenbook_agent_core.turn import ContextAssembler
    from greenbook_agent_api.services.turn_coordinator import TurnCoordinator
    from greenbook_evaluation.canonical import canonical_semantic_result

    class _FrozenSnapshotBuilder:
        def __init__(self, snapshot: ContextSnapshot) -> None:
            self._snapshot = snapshot

        async def build(self, **_kwargs: Any) -> ContextSnapshot:
            return self._snapshot

    case_key = f"{case['id']}-{abs(hash(message)) % 100000:05d}"
    trace_path = snapshot_dir / f"{case_key}.jsonl"
    if trace_path.exists():
        trace_path.unlink()
    payload = _context_for(case, context_library)
    payload["conversation_id"] = f"semantic-longtail-{case['id']}"
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
    context_projection = project_interpreter_context(context)
    context_projection_chars = len(
        json.dumps(context_projection, ensure_ascii=False, separators=(",", ":"))
    )
    interpreter = CommandInterpreter(llm=client, model=model)
    coordinator = TurnCoordinator(
        command_runtime=interpreter,
        temporal_resolver=TemporalResolver(now=fixed_now),
    )
    old_debug = os.environ.get("GREENBOOK_DEBUG_INTERPRETER")
    old_debug_file = os.environ.get("GREENBOOK_DEBUG_INTERPRETER_FILE")
    os.environ["GREENBOOK_DEBUG_INTERPRETER"] = "1"
    os.environ["GREENBOOK_DEBUG_INTERPRETER_FILE"] = str(trace_path)
    before_calls = len(client.records)
    started = time.perf_counter()
    result: dict[str, Any] = {
        "case_id": case["id"],
        "category": case["category"],
        "message": message,
        "turns": copy.deepcopy(case.get("turns") or []),
        "provider_calls_before": before_calls,
        "context_mode": "scoped" if scoped_context else "snapshot",
        "context_projection_chars": context_projection_chars,
    }
    try:
        command_obj = await interpreter.interpret(
            message,
            context,
            llm=client,
            model=model,
            run_id=f"semantic-longtail-{case['id']}",
            turn_id=f"semantic-longtail-{case['id']}",
        )
        resolution_obj = await coordinator._resolve_target(command_obj, context, assembled=assembled)  # noqa: SLF001
        state_obj = coordinator._resolve_semantic_state(  # noqa: SLF001
            command_obj,
            target_resolution=resolution_obj,
            timezone=timezone,
        )
        command = command_obj.model_dump(mode="json")
        state = state_obj.model_dump(mode="json")
        resolution = resolution_obj.model_dump(mode="json") if resolution_obj is not None else None
        result.update({
            "command": command,
            "state": state,
            "canonical": canonical_semantic_result(state, command_obj),
            "target_resolution": resolution or {
                "status": "NONE" if not command.get("target") and not command.get("task_changes") else command.get("target_resolution", "NOT_FOUND")
            },
            "objective_projection": list(state.get("objectives") or []),
            "stages": _stage_map(_read_debug_trace(trace_path)),
        })
    except Exception as exc:  # noqa: BLE001 - record provider/contract boundary precisely
        result.update({
            "error": str(exc),
            "error_code": str(getattr(exc, "code", "") or ""),
            "exception_type": type(exc).__name__,
            "traceback": traceback.format_exc(limit=8),
            "stages": _stage_map(_read_debug_trace(trace_path)),
        })
    finally:
        if old_debug is None:
            os.environ.pop("GREENBOOK_DEBUG_INTERPRETER", None)
        else:
            os.environ["GREENBOOK_DEBUG_INTERPRETER"] = old_debug
        if old_debug_file is None:
            os.environ.pop("GREENBOOK_DEBUG_INTERPRETER_FILE", None)
        else:
            os.environ["GREENBOOK_DEBUG_INTERPRETER_FILE"] = old_debug_file
    calls = client.records[before_calls:]
    result["provider_calls"] = len(calls)
    result["provider_usage"] = {
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in calls),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in calls),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in calls),
        "latency_ms": round(sum(float(item.get("latency_ms") or 0.0) for item in calls), 2),
        "records": calls,
    }
    result["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    if result.get("error"):
        result["evaluation"] = _evaluate_case(case, result)
    else:
        result["evaluation"] = _evaluate_case(case, result)
    return result


def _aggregate(
    cases: list[Mapping[str, Any]],
    results: list[Mapping[str, Any]],
) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = {}
    metric_counts: Counter[str] = Counter()
    first_bad: Counter[str] = Counter()
    structural: Counter[str] = Counter()
    exact = 0
    unsafe = 0
    core_exact = 0
    core_unsafe = 0
    primary_results = [result for result in results if int(result.get("variant_index") or 0) == 0]
    primary_exact = 0
    primary_unsafe = 0
    primary_core_exact = 0
    primary_core_unsafe = 0
    expected_clarifications = 0
    actual_clarifications = 0
    unnecessary_clarifications = 0
    missing_clarifications = 0
    provenance_counts: Counter[str] = Counter()
    provenance_total = 0
    for result in results:
        evaluation = result.get("evaluation") or {}
        metrics = set(evaluation.get("metrics") or [])
        metric_counts.update(metrics)
        if evaluation.get("exact"):
            exact += 1
        if evaluation.get("unsafe"):
            unsafe += 1
        if evaluation.get("core_intent_exact"):
            core_exact += 1
        if evaluation.get("core_intent_unsafe"):
            core_unsafe += 1
        if int(result.get("variant_index") or 0) == 0:
            primary_exact += int(bool(evaluation.get("exact")))
            primary_unsafe += int(bool(evaluation.get("unsafe")))
            primary_core_exact += int(bool(evaluation.get("core_intent_exact")))
            primary_core_unsafe += int(bool(evaluation.get("core_intent_unsafe")))
        truth = next(
            (case.get("truth") or {})
            for case in cases
            if str(case.get("id") or "") == str(result.get("case_id") or "")
        )
        expected_clarifications += int(bool(truth.get("clarification")))
        actual_clarifications += int(bool((result.get("canonical") or {}).get("clarification_required")))
        unnecessary_clarifications += int("unnecessary_clarification" in metrics)
        missing_clarifications += int("missing_clarification" in metrics)
        provenance = evaluation.get("provenance") or {}
        if provenance:
            provenance_total += 1
            for field, covered in provenance.items():
                provenance_counts[field] += int(bool(covered))
        if evaluation.get("first_bad_state"):
            first_bad[str(evaluation["first_bad_state"])] += 1
        category = str(result.get("category") or "")
        entry = by_category.setdefault(category, {
            "total": 0,
            "exact": 0,
            "unsafe": 0,
            "core_exact": 0,
            "core_unsafe": 0,
            "metrics": Counter(),
        })
        entry["total"] += 1
        entry["exact"] += int(bool(evaluation.get("exact")))
        entry["unsafe"] += int(bool(evaluation.get("unsafe")))
        entry["core_exact"] += int(bool(evaluation.get("core_intent_exact")))
        entry["core_unsafe"] += int(bool(evaluation.get("core_intent_unsafe")))
        entry["metrics"].update(metrics)
        for metric in metrics:
            if metric != "exact_semantic_success":
                structural[f"{category}:{metric}"] += 1

    category_output: dict[str, Any] = {}
    for category, entry in by_category.items():
        category_output[category] = {
            "total": entry["total"],
            "exact": entry["exact"],
            "exact_accuracy": entry["exact"] / entry["total"] if entry["total"] else 0.0,
            "unsafe": entry["unsafe"],
            "core_intent_exact": entry["core_exact"],
            "core_intent_accuracy": entry["core_exact"] / entry["total"] if entry["total"] else 0.0,
            "core_intent_unsafe": entry["core_unsafe"],
            "metrics": dict(sorted(entry["metrics"].items())),
        }
    primary_by_category: dict[str, dict[str, Any]] = {}
    for result in primary_results:
        category = str(result.get("category") or "")
        entry = primary_by_category.setdefault(category, {
            "total": 0,
            "exact": 0,
            "unsafe": 0,
            "core_intent_exact": 0,
            "core_intent_unsafe": 0,
        })
        entry["total"] += 1
        entry["exact"] += int(bool((result.get("evaluation") or {}).get("exact")))
        entry["unsafe"] += int(bool((result.get("evaluation") or {}).get("unsafe")))
        entry["core_intent_exact"] += int(bool((result.get("evaluation") or {}).get("core_intent_exact")))
        entry["core_intent_unsafe"] += int(bool((result.get("evaluation") or {}).get("core_intent_unsafe")))
    for entry in primary_by_category.values():
        entry["exact_accuracy"] = entry["exact"] / entry["total"] if entry["total"] else 0.0
        entry["unsafe_rate"] = entry["unsafe"] / entry["total"] if entry["total"] else 0.0
        entry["core_intent_accuracy"] = entry["core_intent_exact"] / entry["total"] if entry["total"] else 0.0
        entry["core_intent_unsafe_rate"] = entry["core_intent_unsafe"] / entry["total"] if entry["total"] else 0.0
    return {
        "primary_case_count": len(cases),
        "evaluated_utterance_count": len(results),
        "exact_semantic_success": exact,
        "exact_semantic_accuracy": exact / len(results) if results else 0.0,
        "primary_exact_semantic_success": primary_exact,
        "primary_exact_semantic_accuracy": primary_exact / len(primary_results) if primary_results else 0.0,
        "unsafe_semantic_error_count": unsafe,
        "unsafe_semantic_error_rate": unsafe / len(results) if results else 0.0,
        "primary_unsafe_semantic_error_count": primary_unsafe,
        "primary_unsafe_semantic_error_rate": primary_unsafe / len(primary_results) if primary_results else 0.0,
        "strict_exact": exact,
        "strict_exact_accuracy": exact / len(results) if results else 0.0,
        "primary_strict_exact": primary_exact,
        "primary_strict_exact_accuracy": primary_exact / len(primary_results) if primary_results else 0.0,
        "strict_unsafe": unsafe,
        "strict_unsafe_rate": unsafe / len(results) if results else 0.0,
        "primary_strict_unsafe": primary_unsafe,
        "primary_strict_unsafe_rate": primary_unsafe / len(primary_results) if primary_results else 0.0,
        "core_intent_exact": core_exact,
        "core_intent_exact_accuracy": core_exact / len(results) if results else 0.0,
        "primary_core_intent_exact": primary_core_exact,
        "primary_core_intent_exact_accuracy": primary_core_exact / len(primary_results) if primary_results else 0.0,
        "core_intent_unsafe": core_unsafe,
        "core_intent_unsafe_rate": core_unsafe / len(results) if results else 0.0,
        "primary_core_intent_unsafe": primary_core_unsafe,
        "primary_core_intent_unsafe_rate": primary_core_unsafe / len(primary_results) if primary_results else 0.0,
        "clarification": {
            "expected": expected_clarifications,
            "actual": actual_clarifications,
            "unnecessary": unnecessary_clarifications,
            "missing": missing_clarifications,
        },
        "metrics": dict(sorted(metric_counts.items())),
        "by_category": category_output,
        "primary_by_category": primary_by_category,
        "provenance_coverage": {
            field: {
                "covered": count,
                "total": provenance_total,
                "coverage": count / provenance_total if provenance_total else 0.0,
            }
            for field, count in sorted(provenance_counts.items())
        },
        "first_bad_state": dict(sorted(first_bad.items())),
        "top_failure_clusters": [
            {"cluster": name, "count": count}
            for name, count in structural.most_common(15)
        ],
    }


def _paraphrase_report(
    cases: list[Mapping[str, Any]],
    results: list[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[str(result.get("case_id") or "")].append(result)
    rows: list[dict[str, Any]] = []
    inconsistent = 0
    for case in cases:
        if case.get("category") != "H_PARAPHRASE":
            continue
        group = grouped.get(str(case["id"]), [])
        signatures = [_semantic_signature(item) for item in group if not item.get("error")]
        is_consistent = len(set(signatures)) <= 1 and len(signatures) == len(group)
        inconsistent += int(not is_consistent)
        rows.append({
            "case_id": case["id"],
            "variant_count": len(group),
            "consistent": is_consistent,
            "signatures": [list(signature) for signature in signatures],
        })
    total = len(rows)
    return {
        "groups": total,
        "consistent_groups": total - inconsistent,
        "inconsistent_groups": inconsistent,
        "consistency": (total - inconsistent) / total if total else 0.0,
        "rows": rows,
    }


async def _run(args: argparse.Namespace) -> int:
    _load_dotenv()
    dataset_path = Path(args.dataset).resolve()
    dataset = _json_read(dataset_path)
    cases = list(dataset.get("cases") or [])
    if args.case_ids:
        wanted_case_ids = {
            value.strip()
            for value in str(args.case_ids).split(",")
            if value.strip()
        }
        cases = [case for case in cases if str(case.get("id") or "") in wanted_case_ids]
    if args.limit:
        cases = cases[: args.limit]
    config = _provider_config()
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    output_dir = Path(args.output).resolve() if args.output else ROOT / "artifacts" / "semantic_longtail_20260822"
    snapshot_dir = output_dir / "snapshots"
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    _json_write(output_dir / "dataset_manifest.json", {
        "dataset_path": str(dataset_path),
        "benchmark": dataset.get("benchmark"),
        "version": dataset.get("version"),
        "primary_case_count": len(cases),
        "category_counts": dict(Counter(str(case.get("category") or "") for case in cases)),
        "fixed_now": dataset.get("fixed_now"),
        "timezone": dataset.get("timezone", "Asia/Shanghai"),
    })
    if not config["api_key_present"]:
        _json_write(output_dir / "provider_blocked.json", {
            "provider": config["provider"],
            "base_url": config["base_url"],
            "model": config["model"],
            "reason": "DEEPSEEK_API_KEY/OPENAI_API_KEY is not available to the benchmark process.",
        })
        print(json.dumps({"status": "PROVIDER_BLOCKED", "output_dir": str(output_dir)}, ensure_ascii=False))
        return 2

    from openai import AsyncOpenAI

    raw_client = AsyncOpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"),
        base_url=config["base_url"],
    )
    client = RecordingClient(raw_client)
    fixed_now = datetime.fromisoformat(str(dataset.get("fixed_now")))
    timezone = str(dataset.get("timezone") or "Asia/Shanghai")
    results: list[dict[str, Any]] = []
    provider_error_count = 0
    started = time.perf_counter()
    try:
        for index, case in enumerate(cases, start=1):
            messages = [str(case.get("message") or "")]
            if case.get("category") == "H_PARAPHRASE":
                messages.extend(str(value) for value in (case.get("variants") or []))
            for variant_index, message in enumerate(messages):
                result = await _run_one(
                    case,
                    message=message,
                    context_library=dataset.get("context_library") or {},
                    client=client,
                    model=config["model"],
                    timezone=timezone,
                    fixed_now=fixed_now,
                    snapshot_dir=snapshot_dir,
                    scoped_context=args.scoped_context,
                )
                result["variant_index"] = variant_index
                results.append(result)
                if result.get("error"):
                    provider_error_count += int(result.get("exception_type") in {"APIError", "APITimeoutError", "RateLimitError", "APIConnectionError"})
            if index % 5 == 0 or index == len(cases):
                print(f"completed {index}/{len(cases)} primary cases; utterances={len(results)}")
    finally:
        await client.close()
    summary = _aggregate(cases, results)
    paraphrase = _paraphrase_report(cases, results)
    all_calls = [call for result in results for call in (result.get("provider_usage", {}).get("records") or [])]
    context_chars = [
        int(result.get("context_projection_chars") or 0)
        for result in results
    ]
    cost = {
        "model": config["model"],
        "provider": config["provider"],
        "base_url": config["base_url"],
        "temperature": 0.0,
        "calls": len(all_calls),
        "input_tokens": sum(int(call.get("input_tokens") or 0) for call in all_calls),
        "output_tokens": sum(int(call.get("output_tokens") or 0) for call in all_calls),
        "total_tokens": sum(int(call.get("total_tokens") or 0) for call in all_calls),
        "latency_ms_sum": round(sum(float(call.get("latency_ms") or 0.0) for call in all_calls), 2),
        "latency_ms_avg": round(sum(float(call.get("latency_ms") or 0.0) for call in all_calls) / len(all_calls), 2) if all_calls else 0.0,
        "benchmark_wall_ms": round((time.perf_counter() - started) * 1000.0, 2),
        "provider_error_count": provider_error_count,
        "calls_per_primary_case": len(all_calls) / len(cases) if cases else 0.0,
        "context_projection_chars": {
            "min": min(context_chars) if context_chars else 0,
            "max": max(context_chars) if context_chars else 0,
            "avg": round(sum(context_chars) / len(context_chars), 2) if context_chars else 0.0,
            "sum": sum(context_chars),
        },
    }
    report = {
        "status": "COMPLETED" if not provider_error_count else "COMPLETED_WITH_PROVIDER_ERRORS",
        "context_mode": "scoped" if args.scoped_context else "snapshot",
        "run_id": stamp,
        "dataset": {
            "path": str(dataset_path),
            "version": dataset.get("version"),
            "primary_case_count": len(cases),
            "category_counts": dict(Counter(str(case.get("category") or "") for case in cases)),
        },
        "summary": summary,
        "paraphrase": paraphrase,
        "cost": cost,
        "provenance": {
            "formal_source_span_contract": False,
            "observed_fields": ["raw_input", "goal", "target.reference", "items.temporal_text", "constraints"],
            "method": "diagnostic in-band/source-substring checks; no new provenance model",
            "coverage": summary.get("provenance_coverage", {}),
        },
        "artifacts": {
            "dataset": str(dataset_path),
            "results": str(output_dir / "results.json"),
            "report": str(output_dir / "report.json"),
            "snapshots": str(snapshot_dir),
        },
    }
    _json_write(output_dir / "results.json", results)
    _json_write(output_dir / "report.json", report)
    print(json.dumps({
        "status": report["status"],
        "primary_cases": len(cases),
        "utterances": len(results),
        "exact_accuracy": summary["exact_semantic_accuracy"],
        "unsafe_rate": summary["unsafe_semantic_error_rate"],
        "calls": cost["calls"],
        "report": str(output_dir / "report.json"),
    }, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(ROOT / "evaluation" / "semantic_longtail" / "cases.json"))
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "semantic_longtail_20260822"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--case-ids",
        default="",
        help="Optional comma-separated primary case ids for a focused evaluation run.",
    )
    parser.add_argument(
        "--scoped-context",
        action="store_true",
        help="Run the frozen benchmark through ContextAssembler's scoped projection.",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
