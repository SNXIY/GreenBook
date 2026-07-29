from typing import Any, Literal

from langchain_core.runnables import RunnableConfig

from agents.moderation.policy_observability import record_policy_rag_trace_metadata
from agents.moderation.state import ModerationState
from moderation.schemas import (
    ModerationSignalEvidence,
    PolicyEvidence,
    PolicyEvidenceSummary,
    PolicyGradeNextAction,
    PolicyGradeResult,
    PolicyQueryHistoryEntry,
    PolicyQueryPlan,
    RejectedPolicy,
    RetrievedPolicy,
    RiskClassification,
    RiskType,
)
from rag.policy.agentic import (
    PolicyFactsUnavailableError,
    PolicyRetrievalBatch,
    retrieved_policy_to_evidence,
)

from .dependencies import ModerationDependencies
from .policy_rewriter import policy_query_signature


class PolicyRAGNodes:
    def __init__(self, dependencies: ModerationDependencies) -> None:
        self.dependencies = dependencies

    async def select_policy_rag_strategy(self, state: ModerationState) -> ModerationState:
        del state
        return {}

    def policy_rag_strategy_route(
        self,
        state: ModerationState,
    ) -> Literal["agentic", "legacy"]:
        if state.get("low_risk_fast_path_used", False) or (
            state.get("adaptive_cascade_enabled", False)
            and state.get("reasoning_tier") != "DEEP"
        ):
            return "legacy"
        if (
            self.dependencies.policy_rag_config.enabled
            and self.dependencies.agentic_policy_retriever is not None
        ):
            return "agentic"
        return "legacy"

    async def policy_query_planner(
        self,
        state: ModerationState,
        config: RunnableConfig,
    ) -> ModerationState:
        classification = RiskClassification.model_validate(state["classification"])
        planner = self.dependencies.policy_query_planner
        if planner is None:
            raise RuntimeError("Real-model Policy Query Planner is not configured")
        call = await planner.plan(
            content=state["normalized_content"],
            classification=classification,
            signals=[
                ModerationSignalEvidence.model_validate(value) for value in state.get("signals", [])
            ],
            risk_hypotheses=[
                RiskType(value)
                for value in state.get("risk_hypotheses", [classification.risk_type.value])
            ],
            evidence_summary=state.get("evidence_summary"),
            preliminary_policies=[
                PolicyEvidence.model_validate(value) for value in state.get("matched_policies", [])
            ],
            config=config,
        )
        errors = [call.error] if call.error else []
        policy_version = str(state.get("metadata", {}).get("policy_version", "current"))
        record_policy_rag_trace_metadata(
            trace_name="policy_query_planner",
            moderation_task_id=state.get("task_id"),
            initial_risk_type=classification.risk_type.value,
            risk_hypotheses=[item.value for item in call.plan.risk_hypotheses],
            query_count=len(call.plan.queries),
            query_history=call.plan.queries,
            retrieval_mode=call.plan.retrieval_mode.value,
            fallback_used=call.fallback_used,
            model_name=str(config.get("configurable", {}).get("model", "unconfigured")),
            error_count=len(errors),
        )
        return {
            "policy_query_plan": call.plan.model_dump(mode="json"),
            "policy_queries": call.plan.queries,
            "policy_query_history": [],
            "policy_query_cache": {},
            "policy_query_cache_version": policy_version,
            "policy_retrieval_mode": call.plan.retrieval_mode.value,
            "policy_retrieval_round": 0,
            "retrieved_policies": [],
            "applicable_policies": [],
            "partial_policies": [],
            "rejected_policies": [],
            "policy_grade_result": None,
            "policy_rewrite_count": 0,
            "policy_rewrite_no_change": False,
            "policy_no_new_result_rounds": 0,
            "policy_rag_complete": False,
            "policy_rag_sufficient": False,
            "policy_rag_budget_exceeded": False,
            "policy_rag_fallback_used": call.fallback_used,
            "policy_rag_requires_human_review": False,
            "policy_rag_errors": errors,
        }

    async def policy_retriever(self, state: ModerationState) -> ModerationState:
        retriever = self.dependencies.agentic_policy_retriever
        if retriever is None:
            record_policy_rag_trace_metadata(
                trace_name="policy_retriever",
                moderation_task_id=state.get("task_id"),
                error_count=1,
                requires_human_review=True,
            )
            return {
                "policy_rag_complete": False,
                "policy_rag_sufficient": False,
                "policy_rag_requires_human_review": True,
                "policy_rag_errors": ["policy_retriever:NotConfigured"],
            }

        plan = PolicyQueryPlan.model_validate(state["policy_query_plan"])
        retrieval_round = int(state.get("policy_retrieval_round", 0)) + 1
        policy_version = state.get("policy_query_cache_version", "current")
        signature = policy_query_signature(plan, policy_version=policy_version)
        cache = dict(state.get("policy_query_cache", {}))
        cached = cache.get(signature)
        if cached is not None:
            batch = _batch_from_cache(
                cached,
                plan=plan,
                retrieval_round=retrieval_round,
            )
        else:
            try:
                batch = await retriever.retrieve(
                    plan=plan,
                    platform=state.get("platform", "default"),
                    retrieval_round=retrieval_round,
                )
            except PolicyFactsUnavailableError:
                record_policy_rag_trace_metadata(
                    trace_name="policy_retriever",
                    moderation_task_id=state.get("task_id"),
                    query_count=len(plan.queries),
                    query_history=plan.queries,
                    retrieval_mode=plan.retrieval_mode.value,
                    retrieval_round=retrieval_round,
                    error_count=1,
                    requires_human_review=True,
                )
                return self._retrieval_failure(
                    state,
                    retrieval_round=retrieval_round,
                    error="policy_retriever:PolicyFactsUnavailable",
                )
            except Exception as exc:
                record_policy_rag_trace_metadata(
                    trace_name="policy_retriever",
                    moderation_task_id=state.get("task_id"),
                    query_count=len(plan.queries),
                    query_history=plan.queries,
                    retrieval_mode=plan.retrieval_mode.value,
                    retrieval_round=retrieval_round,
                    error_count=1,
                    requires_human_review=True,
                )
                return self._retrieval_failure(
                    state,
                    retrieval_round=retrieval_round,
                    error=f"policy_retriever:{type(exc).__name__}",
                )
            cache[signature] = _batch_to_cache(batch)

        previous = {
            str(item.policy_id): item
            for value in state.get("retrieved_policies", [])
            for item in [RetrievedPolicy.model_validate(value)]
        }
        previous_ids = set(previous)
        for item in batch.policies:
            key = str(item.policy_id)
            existing = previous.get(key)
            if existing is None or item.combined_score > existing.combined_score:
                previous[key] = item
        policies = sorted(
            previous.values(),
            key=lambda item: (item.combined_score, item.version),
            reverse=True,
        )[: self.dependencies.policy_rag_config.max_total_retrieved_policies]
        new_policy_ids = [
            item.policy_id for item in policies if str(item.policy_id) not in previous_ids
        ]
        history = batch.history.model_copy(
            update={
                "new_policy_ids": new_policy_ids,
                "rewritten": retrieval_round > 1,
            }
        )
        histories = [
            PolicyQueryHistoryEntry.model_validate(value)
            for value in state.get("policy_query_history", [])
        ]
        no_new_rounds = (
            0 if new_policy_ids else int(state.get("policy_no_new_result_rounds", 0)) + 1
        )
        record_policy_rag_trace_metadata(
            trace_name="policy_retriever",
            moderation_task_id=state.get("task_id"),
            query_count=len(plan.queries),
            query_history=plan.queries,
            retrieval_mode=plan.retrieval_mode.value,
            retrieval_round=retrieval_round,
            vector_result_count=history.vector_result_count,
            keyword_result_count=history.keyword_result_count,
            retrieved_policy_count=len(policies),
            cache_hits=history.cache_hits,
            fallback_used=history.fallback_used,
            error_count=len(batch.errors),
        )
        return {
            "policy_retrieval_round": retrieval_round,
            "retrieved_policies": [item.model_dump(mode="json") for item in policies],
            "policy_query_history": [
                item.model_dump(mode="json") for item in [*histories, history]
            ],
            "policy_query_cache": cache,
            "policy_no_new_result_rounds": no_new_rounds,
            "policy_rag_fallback_used": bool(
                state.get("policy_rag_fallback_used") or history.fallback_used
            ),
            "policy_rag_errors": list(batch.errors),
        }

    async def policy_grader(
        self,
        state: ModerationState,
        config: RunnableConfig,
    ) -> ModerationState:
        grader = self.dependencies.policy_grader
        if grader is None:
            raise RuntimeError("Real-model Policy Grader is not configured")
        plan = PolicyQueryPlan.model_validate(state["policy_query_plan"])
        policies = [
            RetrievedPolicy.model_validate(value) for value in state.get("retrieved_policies", [])
        ]
        call = await grader.grade(
            content=state["normalized_content"],
            classification=RiskClassification.model_validate(state["classification"]),
            signals=[
                ModerationSignalEvidence.model_validate(value) for value in state.get("signals", [])
            ],
            evidence_summary=state.get("evidence_summary"),
            plan=plan,
            policies=policies,
            config=config,
        )
        result = PolicyGradeResult.model_validate(call.result)
        considered = {policy.policy_id: policy for policy in call.considered_policies}
        applicable = [
            considered[policy_id]
            for policy_id in result.applicable_policy_ids
            if policy_id in considered
        ]
        partial = [
            considered[policy_id]
            for policy_id in result.partial_policy_ids
            if policy_id in considered
        ]
        rejected = {
            item.policy_id: RejectedPolicy.model_validate(item) for item in call.rejected_policies
        }
        record_policy_rag_trace_metadata(
            trace_name="policy_grader",
            moderation_task_id=state.get("task_id"),
            initial_risk_type=RiskClassification.model_validate(
                state["classification"]
            ).risk_type.value,
            query_count=len(plan.queries),
            retrieval_mode=plan.retrieval_mode.value,
            retrieval_round=int(state.get("policy_retrieval_round", 0)),
            retrieved_policy_count=len(policies),
            applicable_policy_count=len(applicable),
            partial_policy_count=len(partial),
            rejected_policy_count=len(rejected),
            sufficient=result.sufficient,
            final_policy_ids=[
                *(str(item.policy_id) for item in applicable),
                *(str(item.policy_id) for item in partial),
            ],
            fallback_used=call.fallback_used,
            model_name=str(config.get("configurable", {}).get("model", "unconfigured")),
            error_count=len(call.errors),
        )
        return {
            "policy_grade_result": result.model_dump(mode="json"),
            "applicable_policies": [item.model_dump(mode="json") for item in applicable],
            "partial_policies": [item.model_dump(mode="json") for item in partial],
            "rejected_policies": [item.model_dump(mode="json") for item in rejected.values()],
            "policy_rag_complete": result.sufficient,
            "policy_rag_sufficient": result.sufficient,
            "policy_rag_fallback_used": bool(
                state.get("policy_rag_fallback_used") or call.fallback_used
            ),
            "policy_rag_errors": list(call.errors),
        }

    async def policy_query_rewriter(
        self,
        state: ModerationState,
        config: RunnableConfig,
    ) -> ModerationState:
        plan = PolicyQueryPlan.model_validate(state["policy_query_plan"])
        grade_result = PolicyGradeResult.model_validate(state["policy_grade_result"])
        policies = [
            RetrievedPolicy.model_validate(value) for value in state.get("retrieved_policies", [])
        ]
        rewriter = self.dependencies.policy_query_rewriter
        if rewriter is None:
            raise RuntimeError("Real-model Policy Query Rewriter is not configured")
        call = await rewriter.rewrite(
            plan=plan,
            grade_result=grade_result,
            retrieved_policies=policies,
            retrieval_round=int(state.get("policy_retrieval_round", 0)),
            config=config,
        )

        rewritten = call.rewritten
        next_plan = plan.model_copy(
            update={
                "queries": rewritten.queries,
                "risk_type_filters": rewritten.risk_type_filters,
                "severity_filters": rewritten.severity_filters,
                "retrieval_mode": rewritten.retrieval_mode,
                "reason": rewritten.reason,
            }
        )
        policy_version = state.get("policy_query_cache_version", "current")
        old_signature = policy_query_signature(plan, policy_version=policy_version)
        next_signature = policy_query_signature(next_plan, policy_version=policy_version)
        no_change = old_signature == next_signature or next_signature in state.get(
            "policy_query_cache", {}
        )
        rewrite_count = int(state.get("policy_rewrite_count", 0)) + 1
        record_policy_rag_trace_metadata(
            trace_name="policy_query_rewriter",
            moderation_task_id=state.get("task_id"),
            query_count=len(next_plan.queries),
            query_history=next_plan.queries,
            retrieval_mode=next_plan.retrieval_mode.value,
            retrieval_round=int(state.get("policy_retrieval_round", 0)),
            rewrite_count=rewrite_count,
            fallback_used=call.fallback_used,
            model_name=str(config.get("configurable", {}).get("model", "unconfigured")),
            error_count=int(call.error is not None),
        )
        return {
            "policy_query_plan": next_plan.model_dump(mode="json"),
            "policy_queries": next_plan.queries,
            "policy_retrieval_mode": next_plan.retrieval_mode.value,
            "policy_rewrite_count": rewrite_count,
            "policy_rewrite_no_change": no_change,
            "policy_rag_fallback_used": bool(
                state.get("policy_rag_fallback_used") or call.fallback_used
            ),
            "policy_rag_errors": [call.error] if call.error else [],
        }

    async def policy_evidence_finalize(self, state: ModerationState) -> ModerationState:
        grade_value = state.get("policy_grade_result")
        grade = PolicyGradeResult.model_validate(grade_value) if grade_value else None
        applicable = [
            RetrievedPolicy.model_validate(value) for value in state.get("applicable_policies", [])
        ]
        applicable_ids = {item.policy_id for item in applicable}
        partial = [
            item
            for value in state.get("partial_policies", [])
            for item in [RetrievedPolicy.model_validate(value)]
            if item.policy_id not in applicable_ids
        ]
        applicable_evidence = [retrieved_policy_to_evidence(item) for item in applicable]
        partial_evidence = [retrieved_policy_to_evidence(item) for item in partial]
        sufficient = bool(state.get("policy_rag_sufficient") and applicable_evidence)
        histories = [
            PolicyQueryHistoryEntry.model_validate(value)
            for value in state.get("policy_query_history", [])
        ]
        queries_used = list(
            dict.fromkeys(query for history in histories for query in history.queries)
        )
        if not queries_used and state.get("policy_query_plan"):
            queries_used = PolicyQueryPlan.model_validate(state["policy_query_plan"]).queries
        missing_topics = (
            list(grade.missing_policy_topics)
            if grade
            else ["A verified applicable Policy could not be established."]
        )
        missing_evidence = (
            list(grade.missing_evidence)
            if grade
            else ["Policy retrieval or grading did not complete reliably."]
        )
        reason = (
            grade.reason
            if grade
            else "Formal Policy evidence is unavailable, so automated enforcement is unsafe."
        )
        summary = PolicyEvidenceSummary(
            complete=True,
            sufficient=sufficient,
            applicable_policies=applicable_evidence,
            partial_policies=partial_evidence,
            missing_policy_topics=missing_topics,
            missing_evidence=missing_evidence,
            retrieval_rounds=int(state.get("policy_retrieval_round", 0)),
            queries_used=queries_used,
            fallback_used=bool(state.get("policy_rag_fallback_used")),
            reason=reason,
        )
        summary_data = summary.model_dump(mode="json")
        evidence_summary = dict(state.get("evidence_summary") or {})
        evidence_summary["policy_evidence"] = summary_data
        classification = RiskClassification.model_validate(state["classification"])
        if not sufficient:
            classification = classification.model_copy(
                update={"confidence": max(0.0, classification.confidence - 0.15)}
            )
        policy_evidence = [*applicable_evidence, *partial_evidence]
        requires_human = _requires_human_after_policy_finalize(
            state,
            has_policy_evidence=bool(policy_evidence),
        )
        record_policy_rag_trace_metadata(
            trace_name="policy_evidence_finalize",
            moderation_task_id=state.get("task_id"),
            query_count=len(queries_used),
            query_history=queries_used,
            retrieval_round=int(state.get("policy_retrieval_round", 0)),
            applicable_policy_count=len(applicable_evidence),
            partial_policy_count=len(partial_evidence),
            rejected_policy_count=len(state.get("rejected_policies", [])),
            rewrite_count=int(state.get("policy_rewrite_count", 0)),
            fallback_used=bool(state.get("policy_rag_fallback_used")),
            budget_exceeded=bool(state.get("policy_rag_budget_exceeded")),
            sufficient=sufficient,
            final_policy_ids=[str(item.policy_id) for item in policy_evidence],
            requires_human_review=requires_human,
            error_count=len(state.get("policy_rag_errors", [])),
        )
        evidence_gaps = [
            gap for gap in state.get("evidence_gaps", []) if not _is_policy_only_gap(gap)
        ]
        evidence_gaps.extend([*missing_topics, *missing_evidence])
        return {
            "classification": classification.model_dump(mode="json"),
            "matched_policies": [item.model_dump(mode="json") for item in policy_evidence],
            "policy_evidence_summary": summary_data,
            "evidence_summary": evidence_summary,
            "evidence_gaps": list(dict.fromkeys(evidence_gaps))[:20],
            "policy_rag_complete": True,
            "policy_rag_sufficient": sufficient,
            "requires_human_review": requires_human,
        }

    def route_after_policy_grade(
        self,
        state: ModerationState,
    ) -> Literal["accept", "rewrite", "partial_stop", "human_stop"]:
        result = PolicyGradeResult.model_validate(state["policy_grade_result"])
        if result.sufficient:
            return "accept"
        if result.suggested_next_action == PolicyGradeNextAction.HUMAN_REVIEW:
            return "human_stop"

        rag_config = self.dependencies.policy_rag_config
        exhausted = (
            int(state.get("policy_retrieval_round", 0)) >= rag_config.max_retrieval_rounds
            or int(state.get("policy_no_new_result_rounds", 0)) >= 2
        )
        if not exhausted:
            return "rewrite"
        return self._incomplete_stop_route(state)

    def route_after_policy_rewrite(
        self,
        state: ModerationState,
    ) -> Literal["retrieve", "partial_stop", "human_stop"]:
        rag_config = self.dependencies.policy_rag_config
        exhausted = (
            bool(state.get("policy_rewrite_no_change"))
            or int(state.get("policy_retrieval_round", 0)) >= rag_config.max_retrieval_rounds
            or int(state.get("policy_rewrite_count", 0)) >= rag_config.max_retrieval_rounds
        )
        if not exhausted:
            return "retrieve"
        return self._incomplete_stop_route(state)

    def mark_partial_stop(self, state: ModerationState) -> ModerationState:
        return {
            "policy_rag_complete": True,
            "policy_rag_sufficient": False,
            "policy_rag_budget_exceeded": self._budget_exhausted(state),
            "policy_rag_requires_human_review": False,
        }

    def mark_human_stop(self, state: ModerationState) -> ModerationState:
        return {
            "policy_rag_complete": True,
            "policy_rag_sufficient": False,
            "policy_rag_budget_exceeded": self._budget_exhausted(state),
            "policy_rag_requires_human_review": True,
        }

    def _incomplete_stop_route(
        self,
        state: ModerationState,
    ) -> Literal["partial_stop", "human_stop"]:
        has_partial_evidence = bool(
            state.get("applicable_policies") or state.get("partial_policies")
        )
        if (
            has_partial_evidence
            and self.dependencies.policy_rag_config.allow_partial_policy_continue
        ):
            return "partial_stop"
        return "human_stop"

    def _budget_exhausted(self, state: ModerationState) -> bool:
        rag_config = self.dependencies.policy_rag_config
        return bool(
            int(state.get("policy_retrieval_round", 0)) >= rag_config.max_retrieval_rounds
            or int(state.get("policy_no_new_result_rounds", 0)) >= 2
            or int(state.get("policy_rewrite_count", 0)) >= rag_config.max_retrieval_rounds
        )

    def _retrieval_failure(
        self,
        state: ModerationState,
        *,
        retrieval_round: int,
        error: str,
    ) -> ModerationState:
        return {
            "policy_retrieval_round": retrieval_round,
            "retrieved_policies": list(state.get("retrieved_policies", [])),
            "policy_no_new_result_rounds": int(state.get("policy_no_new_result_rounds", 0)) + 1,
            "policy_rag_complete": False,
            "policy_rag_sufficient": False,
            "policy_rag_requires_human_review": True,
            "policy_rag_errors": [error],
        }


def _batch_to_cache(batch: PolicyRetrievalBatch) -> dict[str, Any]:
    return {
        "policies": [item.model_dump(mode="json") for item in batch.policies],
        "history": batch.history.model_dump(mode="json"),
        "errors": list(batch.errors),
    }


def _batch_from_cache(
    value: dict[str, Any],
    *,
    plan: PolicyQueryPlan,
    retrieval_round: int,
) -> PolicyRetrievalBatch:
    policies = tuple(
        RetrievedPolicy.model_validate(item).model_copy(update={"retrieval_round": retrieval_round})
        for item in value.get("policies", [])
    )
    cached_history = PolicyQueryHistoryEntry.model_validate(value["history"])
    history = cached_history.model_copy(
        update={
            "retrieval_round": retrieval_round,
            "queries": plan.queries,
            "risk_type_filters": plan.risk_type_filters,
            "severity_filters": plan.severity_filters,
            "retrieval_mode": plan.retrieval_mode,
            "vector_result_count": 0,
            "keyword_result_count": 0,
            "cache_hits": cached_history.cache_hits + 1,
            "rewritten": retrieval_round > 1,
        }
    )
    return PolicyRetrievalBatch(
        policies=policies,
        history=history,
        errors=tuple(value.get("errors", [])),
    )


def _requires_human_after_policy_finalize(
    state: ModerationState,
    *,
    has_policy_evidence: bool,
) -> bool:
    if state.get("policy_rag_requires_human_review", False):
        return True
    if not state.get("requires_human_review", False):
        return False
    if not has_policy_evidence or state.get("tool_budget_exceeded"):
        return True
    if state.get("tool_agent_error"):
        return True
    context = state.get("context_evidence")
    if isinstance(context, dict) and context.get("complete") is False:
        return True
    collection = (state.get("evidence_summary") or {}).get("collection_result", {})
    missing = [
        *state.get("evidence_gaps", []),
        *collection.get("missing_evidence", []),
    ]
    return not missing or not all(_is_policy_only_gap(item) for item in missing)


def _is_policy_only_gap(value: str) -> bool:
    normalized = value.lower()
    return "policy" in normalized or "platform rule" in normalized
