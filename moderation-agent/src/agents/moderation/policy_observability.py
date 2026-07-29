import logging
from enum import Enum
from typing import Any
from uuid import UUID

from langsmith.run_helpers import get_current_run_tree

from moderation.security import redact_data

logger = logging.getLogger(__name__)

_ALLOWED_METADATA_KEYS = {
    "trace_name",
    "moderation_task_id",
    "initial_risk_type",
    "risk_hypotheses",
    "query_count",
    "query_history",
    "retrieval_mode",
    "retrieval_round",
    "vector_result_count",
    "keyword_result_count",
    "retrieved_policy_count",
    "applicable_policy_count",
    "partial_policy_count",
    "rejected_policy_count",
    "rewrite_count",
    "cache_hits",
    "fallback_used",
    "budget_exceeded",
    "sufficient",
    "final_policy_ids",
    "requires_human_review",
    "model_name",
    "error_count",
}


def safe_policy_rag_metadata(values: dict[str, Any]) -> dict[str, Any]:
    filtered = {
        key: _bounded_value(value)
        for key, value in values.items()
        if key in _ALLOWED_METADATA_KEYS and value is not None
    }
    return redact_data(filtered)


def record_policy_rag_trace_metadata(**values: Any) -> dict[str, Any]:
    metadata = safe_policy_rag_metadata(values)
    try:
        run_tree = get_current_run_tree()
        if run_tree is not None:
            run_tree.add_metadata(metadata)
    except Exception:
        logger.debug("Unable to attach Agentic Policy RAG trace metadata", exc_info=True)
    return metadata


def _bounded_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, dict):
        return {str(key)[:128]: _bounded_value(item) for key, item in list(value.items())[:30]}
    if isinstance(value, (list, tuple, set)):
        return [_bounded_value(item) for item in list(value)[:20]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:512]
