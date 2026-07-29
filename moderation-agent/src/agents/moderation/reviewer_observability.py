import logging
from enum import Enum
from typing import Any

from langsmith.run_helpers import get_current_run_tree

from moderation.security import redact_data

logger = logging.getLogger(__name__)

_ALLOWED_METADATA_KEYS = {
    "trace_name",
    "moderation_task_id",
    "judge_type",
    "reviewer_iteration",
    "problem_types",
    "next_action",
    "reviewer_confidence",
    "tool_revision_count",
    "policy_revision_count",
    "judgment_revision_count",
    "revision_count",
    "budget_exceeded",
    "no_progress",
    "final_route",
    "model_name",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "latency_ms",
    "error_count",
}


def safe_reviewer_metadata(values: dict[str, Any]) -> dict[str, Any]:
    filtered = {
        key: _bounded_value(value)
        for key, value in values.items()
        if key in _ALLOWED_METADATA_KEYS and value is not None
    }
    return redact_data(filtered)


def record_reviewer_trace_metadata(**values: Any) -> dict[str, Any]:
    metadata = safe_reviewer_metadata(values)
    try:
        run_tree = get_current_run_tree()
        if run_tree is not None:
            run_tree.add_metadata(metadata)
    except Exception:
        logger.debug("Unable to attach Evidence Reviewer trace metadata", exc_info=True)
    return metadata


def _bounded_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        return value[:256]
    if isinstance(value, (list, tuple, set)):
        return [_bounded_value(item) for item in list(value)[:20]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:256]
