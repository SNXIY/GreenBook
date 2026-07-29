from operator import add
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ModerationState(TypedDict, total=False):
    task_id: str
    thread_id: str
    content: str
    content_type: str
    content_id: str | None
    platform: str
    creator_id: str | None
    metadata: dict[str, Any]
    normalized_content: str
    content_hash: str
    context_evidence: dict[str, Any] | None
    signals: list[dict[str, Any]]
    classification: dict[str, Any]
    policy_risk_types: list[str]
    matched_policies: list[dict[str, Any]]
    similar_cases: list[dict[str, Any]]
    messages: Annotated[list[AnyMessage], add_messages]
    use_dynamic_tool_agent: bool
    adaptive_cascade_enabled: bool
    reasoning_tier: str
    cascade_reasons: list[str]
    cascade_direct_decision: bool
    cascade_context_prefetched: bool
    cascade_tool_agent_available: bool
    low_risk_fast_path_used: bool
    preflight_layer: str | None
    preflight_direct_decision: bool
    preflight_reasons: list[str]
    preflight_action: str | None
    risk_hypotheses: list[str]
    evidence_gaps: list[str]
    tool_results: list[dict[str, Any]]
    called_tools: list[str]
    failed_tools: list[str]
    tool_call_cache: dict[str, dict[str, Any]]
    tool_call_count: int
    tool_call_round: int
    tool_cache_hits: int
    tool_budget_exceeded: bool
    evidence_collection_complete: bool
    evidence_summary: dict[str, Any] | None
    tool_agent_error: str | None
    tool_agent_fallback_used: bool
    tool_agent_metrics: dict[str, Any]
    policy_query_plan: dict[str, Any] | None
    policy_queries: list[str]
    policy_query_history: list[dict[str, Any]]
    policy_query_cache: dict[str, dict[str, Any]]
    policy_query_cache_version: str
    policy_retrieval_mode: str
    policy_retrieval_round: int
    retrieved_policies: list[dict[str, Any]]
    applicable_policies: list[dict[str, Any]]
    partial_policies: list[dict[str, Any]]
    rejected_policies: list[dict[str, Any]]
    policy_grade_result: dict[str, Any] | None
    policy_rewrite_count: int
    policy_rewrite_no_change: bool
    policy_no_new_result_rounds: int
    policy_rag_complete: bool
    policy_rag_sufficient: bool
    policy_rag_budget_exceeded: bool
    policy_rag_fallback_used: bool
    policy_rag_requires_human_review: bool
    policy_rag_errors: Annotated[list[str], add]
    policy_evidence_summary: dict[str, Any] | None
    use_adversarial_review: bool
    evidence_conflict: bool
    risk_agent_result: dict[str, Any] | None
    safe_agent_result: dict[str, Any] | None
    judge_agent_result: dict[str, Any] | None
    agent_conflict: bool
    adversarial_review_count: int
    adversarial_errors: Annotated[list[str], add]
    risk_agent_metrics: dict[str, Any]
    safe_agent_metrics: dict[str, Any]
    judge_agent_metrics: dict[str, Any]
    agent_decision: dict[str, Any]
    agent_decision_version: int
    evidence_complete: bool
    evidence_check_passed: bool
    evidence_check_issues: list[dict[str, Any]]
    evidence_ledger: dict[str, Any]
    policy_engine_result: dict[str, Any]
    requires_human_review: bool
    reviewer_decision: dict[str, Any] | None
    reviewer_history: Annotated[list[dict[str, Any]], add]
    reviewer_iteration: int
    reviewer_revision_count: int
    reviewer_tool_revision_count: int
    reviewer_policy_revision_count: int
    reviewer_judgment_revision_count: int
    reviewer_feedback_for_tools: list[str]
    reviewer_feedback_for_policy: list[str]
    reviewer_feedback_for_judge: list[str]
    reviewer_route: str | None
    reviewer_budget_exceeded: bool
    reviewer_errors: Annotated[list[str], add]
    reviewer_revision_signatures: Annotated[list[str], add]
    reviewer_progress_snapshot: dict[str, Any]
    reviewer_no_progress: bool
    reviewer_judge_scope: str | None
    reviewer_model_metrics: dict[str, Any]
    revision_source: str | None
    human_decision: dict[str, Any]
    final_action: str
    final_risk_type: str
    status: str
