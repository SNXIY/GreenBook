import asyncio
import hashlib
import re
import unicodedata
from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from agents.moderation.routes import (
    decision_route,
    detect_evidence_conflict,
    final_action_for_route,
    policy_risk_types,
    should_use_adversarial_review,
)
from agents.moderation.state import ModerationState
from moderation.schemas import (
    AgentDecision,
    CaseEvidence,
    HumanDecision,
    ModerationAction,
    ModerationContentType,
    ModerationContextEvidence,
    ModerationSignalEvidence,
    ModerationSignalType,
    ModerationTaskStatus,
    PolicyEvidence,
    RiskClassification,
    RiskType,
    SignalSource,
    reasoning_cascade_audit_from_state,
)
from moderation.schemas.cascade import ReasoningTier
from moderation.services.preflight import ModerationPreflightService
from moderation.services.reason_messages import public_preflight_reason

from .dependencies import ModerationDependencies

_IGNORED_CHARACTERS = re.compile(r"[\u200b-\u200d\ufeff]")
_WHITESPACE = re.compile(r"\s+")


class ModerationNodes:
    def __init__(self, dependencies: ModerationDependencies) -> None:
        self.dependencies = dependencies
#去重——相同内容哈希一样，可以跳过重复审核 审计追溯——记录审核了什么内容，但不存原文（脱敏/隐私保护）
    async def preprocess(self, state: ModerationState) -> ModerationState:
        normalized = unicodedata.normalize("NFKC", state["content"])
        normalized = _IGNORED_CHARACTERS.sub("", normalized)
        normalized = _WHITESPACE.sub(" ", normalized).strip()
        return { # 写入state dict
            "normalized_content": normalized,
            "content_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "status": ModerationTaskStatus.RUNNING.value,
        }

    async def classify(self, state: ModerationState, config: RunnableConfig) -> ModerationState:
        classification = await self.dependencies.classifier.classify(
            content=state["normalized_content"],
            content_type=ModerationContentType(state.get("content_type", "TEXT")),
            context=self._context(state),
            signals=self._signals(state),
            config=config,
        )
        return {"classification": classification.model_dump(mode="json")}

    async def gather_context(self, state: ModerationState) -> ModerationState:
        content_type = ModerationContentType(state.get("content_type", "TEXT"))
        content_id = state.get("content_id")
        if content_type == ModerationContentType.TEXT or not content_id:
            return {
                "context_evidence": None,
                "cascade_context_prefetched": bool(
                    state.get("adaptive_cascade_enabled", False)
                ),
            }
        context = await self.dependencies.context_loader.load_context(
            content_id=content_id,
            content_type=content_type,
            author_id=state.get("creator_id"),
        )
        if context is None:
            context = ModerationContextEvidence(
                complete=False,
                errors=["community context unavailable"],
            )
        return {
            "context_evidence": context.model_dump(mode="json"),
            "cascade_context_prefetched": bool(state.get("adaptive_cascade_enabled", False)),
        }

    async def scan_signals(self, state: ModerationState) -> ModerationState:
        context = self._context(state)
        signals: list[ModerationSignalEvidence] = []
        if context is not None:
            if context.reports:
                reporters = {report.reporter_id for report in context.reports}
                signals.append(
                    ModerationSignalEvidence(
                        signal_type=ModerationSignalType.REPORT_COUNT,
                        source=SignalSource.REPORT,
                        score=min(1.0, len(context.reports) / 5),
                        details={
                            "report_count": len(context.reports),
                            "reporter_count": len(reporters),
                            "report_types": sorted(
                                {report.report_type for report in context.reports}
                            ),
                        },
                    )
                )
            if context.author_violation_history:
                signals.append(
                    ModerationSignalEvidence(
                        signal_type=ModerationSignalType.AUTHOR_VIOLATION_HISTORY,
                        source=SignalSource.COMMUNITY,
                        score=min(1.0, len(context.author_violation_history) / 5),
                        details={"violation_count": len(context.author_violation_history)},
                    )
                )
            if not context.complete:
                signals.append(
                    ModerationSignalEvidence(
                        signal_type=ModerationSignalType.CONTEXT_INCOMPLETE,
                        source=SignalSource.COMMUNITY,
                        score=1.0,
                        details={"errors": context.errors},
                    )
                )
        return {"signals": [signal.model_dump(mode="json") for signal in signals]}

    async def run_preflight(self, state: ModerationState) -> ModerationState:
        service = self.dependencies.preflight
        if service is None:
            config = self.dependencies.preflight_config
            if not config.l0_enabled and not config.l1_enabled:
                return {
                    "preflight_direct_decision": False,
                    "preflight_layer": None,
                    "preflight_reasons": [],
                }
            service = ModerationPreflightService(config)

        existing = self._signals(state)
        result = await service.evaluate(
            content=state["normalized_content"],
            content_type=ModerationContentType(state.get("content_type", "TEXT")),
            metadata=state.get("metadata") or {},
            existing_signals=existing,
        )
        updates: ModerationState = {
            "signals": [signal.model_dump(mode="json") for signal in result.signals],
            "preflight_layer": result.layer,
            "preflight_reasons": list(result.reasons),
            "preflight_direct_decision": result.disposition == "enforce",
            "preflight_action": (
                result.recommended_action.value if result.recommended_action else None
            ),
        }
        if result.disposition == "enforce" and result.classification is not None:
            updates["classification"] = result.classification.model_dump(mode="json")
            updates["policy_risk_types"] = [result.classification.risk_type.value]
        return updates

    def preflight_route(self, state: ModerationState) -> str:
        if state.get("preflight_direct_decision"):
            return "enforce"
        return "continue"

    def route_after_evidence_check(self, state: ModerationState) -> str:
        if state.get("preflight_direct_decision"):
            return "finalize"
        if self.dependencies.policy_engine_enabled:
            return "policy_engine"
        return "review"

    async def prepare_preflight_decision(
        self,
        state: ModerationState,
    ) -> ModerationState:
        classification = RiskClassification.model_validate(state["classification"])
        action = ModerationAction(
            state.get("preflight_action")
            or (
                ModerationAction.PASS
                if classification.risk_type == RiskType.NORMAL
                else ModerationAction.REJECT
            )
        )
        reasons = list(state.get("preflight_reasons") or [])
        layer = state.get("preflight_layer") or "L0"
        cascade_reasons = [
            f"Preflight {layer} enforced a direct decision before the Agent path.",
            *reasons,
        ][:20]
        cascade = reasoning_cascade_audit_from_state(
            {
                **state,
                "adaptive_cascade_enabled": True,
                "reasoning_tier": ReasoningTier.FAST.value,
                "cascade_direct_decision": True,
                "cascade_reasons": cascade_reasons,
            }
        )
        reason = public_preflight_reason(
            reasons=reasons,
            risk_type=classification.risk_type,
            action=action,
        )
        decision = AgentDecision(
            risk_type=classification.risk_type,
            risk_score=classification.risk_score,
            confidence=classification.confidence,
            recommended_action=action,
            reason=reason[:2000],
            signals=self._signals(state),
            context_evidence=self._context(state),
            source_evidence=[f"preflight:{layer}"],
            needs_context_review=False,
            evidence_complete=True,
            reasoning_cascade=cascade,
        )
        return {
            "agent_decision": decision.model_dump(mode="json"),
            "agent_decision_version": int(state.get("agent_decision_version", 0)) + 1,
            "reasoning_tier": ReasoningTier.FAST.value,
            "adaptive_cascade_enabled": True,
            "cascade_direct_decision": True,
            "cascade_reasons": cascade_reasons,
            "evidence_collection_complete": True,
            "evidence_complete": True,
            "evidence_summary": {
                "preflight": {
                    "layer": layer,
                    "reasons": reasons,
                    "action": action.value,
                },
                "cascade": cascade.model_dump(mode="json") if cascade else None,
            },
            "low_risk_fast_path_used": action == ModerationAction.PASS,
        }

    async def route_risk(self, state: ModerationState) -> ModerationState:
        classification = RiskClassification.model_validate(state["classification"])
        signals = self._signals(state)
        if classification.indicators:
            signals.append(
                ModerationSignalEvidence(
                    signal_type=ModerationSignalType.TEXT_PATTERN,
                    source=SignalSource.CONTENT,
                    score=classification.risk_score,
                    details={"indicators": classification.indicators},
                )
            )
        return {
            "policy_risk_types": [risk.value for risk in policy_risk_types(state)],
            "signals": [signal.model_dump(mode="json") for signal in signals],
        }

    async def retrieve_evidence(self, state: ModerationState) -> ModerationState:
        query = state["normalized_content"]
        platform = state.get("platform", "default")
        risk_types = [RiskType(risk_type) for risk_type in state["policy_risk_types"]]
        policies, cases = await asyncio.gather(
            self.dependencies.policy_retriever.search(
                query=query,
                platform=platform,
                risk_types=risk_types,
                limit=5,
            ),
            self.dependencies.case_retriever.search(
                query=query,
                platform=platform,
                risk_types=risk_types,
                limit=3,
            ),
        )
        return {
            "matched_policies": [policy.model_dump(mode="json") for policy in policies],
            "similar_cases": [case.model_dump(mode="json") for case in cases],
        }

    async def select_review_mode(self, state: ModerationState) -> ModerationState:
        evidence_conflict = bool(state.get("evidence_conflict")) or detect_evidence_conflict(state)
        adversarial_review_available = all(
            (
                self.dependencies.risk_investigator,
                self.dependencies.safe_advocate,
                self.dependencies.adversarial_judge,
            )
        )
        return {
            "evidence_conflict": evidence_conflict,
            "use_adversarial_review": (
                adversarial_review_available
                and should_use_adversarial_review(state, self.dependencies.thresholds)
            ),
            "adversarial_review_count": state.get("adversarial_review_count", 0),
            "adversarial_errors": [],
        }

    async def judge(self, state: ModerationState, config: RunnableConfig) -> ModerationState:
        decision = await self.dependencies.judge.decide(
            content=state["normalized_content"],
            content_type=ModerationContentType(state.get("content_type", "TEXT")),
            classification=RiskClassification.model_validate(state["classification"]),
            policies=[
                PolicyEvidence.model_validate(policy)
                for policy in state.get("matched_policies", [])
            ],
            cases=[CaseEvidence.model_validate(case) for case in state.get("similar_cases", [])],
            context=self._context(state),
            signals=self._signals(state),
            evidence_summary=state.get("evidence_summary"),
            config=config,
        )
        return {
            "agent_decision": decision.model_dump(mode="json"),
            "agent_decision_version": int(state.get("agent_decision_version", 0)) + 1,
        }

    async def prepare_cascade_fast_decision(
        self,
        state: ModerationState,
    ) -> ModerationState:
        classification = RiskClassification.model_validate(state["classification"])
        if classification.risk_type != RiskType.NORMAL:
            raise ValueError("The direct cascade path only supports NORMAL classifications")
        cascade = reasoning_cascade_audit_from_state(
            {**state, "cascade_direct_decision": True}
        )
        decision = AgentDecision(
            risk_type=RiskType.NORMAL,
            risk_score=classification.risk_score,
            confidence=classification.confidence,
            recommended_action=ModerationAction.PASS,
            reason=(
                "The item met the conservative low-risk cascade gates and no contextual "
                "or deterministic risk signal required deeper review."
            ),
            signals=self._signals(state),
            context_evidence=self._context(state),
            source_evidence=[],
            needs_context_review=False,
            evidence_complete=True,
            reasoning_cascade=cascade,
        )
        return {
            "agent_decision": decision.model_dump(mode="json"),
            "agent_decision_version": int(state.get("agent_decision_version", 0)) + 1,
            "cascade_direct_decision": True,
            "evidence_collection_complete": True,
            "evidence_summary": {
                "cascade": cascade.model_dump(mode="json") if cascade else None,
            },
        }

    async def check_evidence(self, state: ModerationState) -> ModerationState:
        decision = AgentDecision.model_validate(state["agent_decision"])
        classification = RiskClassification.model_validate(state["classification"])
        policies = _validated_decision_policies(state, decision)
        claimed_policy_ids = {str(policy.policy_id) for policy in decision.matched_policies}
        validated_policy_ids = {str(policy.policy_id) for policy in policies}
        issues: list[dict[str, object]] = []
        if classification.fallback_used:
            issues.append(
                {
                    "code": "CLASSIFIER_FALLBACK_USED",
                    "severity": "HIGH",
                    "affected_fields": ["classification"],
                    "message": (
                        "The primary classifier failed, so automatic action is disabled."
                    ),
                }
            )
        if decision.model_fallback_used:
            issues.append(
                {
                    "code": "JUDGE_FALLBACK_USED",
                    "severity": "HIGH",
                    "affected_fields": ["agent_decision"],
                    "message": "The primary Judge failed, so automatic action is disabled.",
                }
            )
        invalid_policy_ids = sorted(claimed_policy_ids - validated_policy_ids)
        if invalid_policy_ids:
            issues.append(
                {
                    "code": "INVALID_POLICY_REFERENCE",
                    "severity": "HIGH",
                    "affected_fields": ["matched_policies"],
                    "message": "The decision referenced Policy IDs that are unavailable, invalid, or inapplicable.",
                    "policy_ids": invalid_policy_ids,
                }
            )
        action_supported = True
        preflight = bool(state.get("preflight_direct_decision"))
        if (
            not preflight
            and state.get("policy_evidence_summary")
            and decision.recommended_action
            in {
                ModerationAction.REJECT,
                ModerationAction.LIMIT,
            }
        ):
            action_supported = any(
                decision.recommended_action in {policy.default_action, *policy.suggested_actions}
                for policy in policies
            )
            if not action_supported:
                issues.append(
                    {
                        "code": "ACTION_NOT_SUPPORTED_BY_POLICY",
                        "severity": "HIGH",
                        "affected_fields": ["recommended_action", "matched_policies"],
                        "message": "The verified Policy evidence does not support the recommended action.",
                    }
                )
        if not preflight and decision.risk_type != RiskType.NORMAL and not policies:
            issues.append(
                {
                    "code": "MISSING_VALID_POLICY",
                    "severity": "HIGH",
                    "affected_fields": ["matched_policies"],
                    "message": "A non-normal decision has no current, applicable Policy evidence.",
                }
            )
        if not preflight and decision.needs_context_review:
            issues.append(
                {
                    "code": "CONTEXT_REVIEW_REQUIRED",
                    "severity": "HIGH",
                    "affected_fields": ["context_evidence"],
                    "message": "The decision identifies context that must be reviewed before automation.",
                }
            )
        context = state.get("context_evidence")
        if (
            not preflight
            and isinstance(context, dict)
            and context.get("complete") is False
        ):
            issues.append(
                {
                    "code": "CONTEXT_INCOMPLETE",
                    "severity": "HIGH",
                    "affected_fields": ["context_evidence"],
                    "message": "Required moderation context is incomplete.",
                }
            )
        if state.get("policy_rag_requires_human_review", False):
            issues.append(
                {
                    "code": "POLICY_RAG_REQUIRES_HUMAN_REVIEW",
                    "severity": "HIGH",
                    "affected_fields": ["policy_evidence_summary"],
                    "message": "Formal Policy evidence could not be established safely.",
                }
            )
        if preflight:
            evidence_complete = bool(decision.reason.strip())
        else:
            evidence_complete = bool(decision.reason.strip()) and (
                decision.risk_type == RiskType.NORMAL or (bool(policies) and action_supported)
            )
        confidence = decision.confidence
        if state.get("policy_evidence_summary") and not state.get(
            "policy_rag_sufficient",
            False,
        ):
            confidence = min(
                confidence,
                max(0.0, self.dependencies.policy_rag_config.grader_min_confidence - 0.01),
            )
        checked = decision.model_copy(
            update={
                "matched_policies": policies,
                "confidence": confidence,
                "evidence_complete": evidence_complete,
                "reasoning_cascade": (
                    reasoning_cascade_audit_from_state(state)
                    or decision.reasoning_cascade
                ),
            }
        )
        checked_data = checked.model_dump(mode="json")
        forced_human_review = bool(
            state.get("requires_human_review", False)
            or state.get("policy_rag_requires_human_review", False)
            or classification.fallback_used
            or decision.model_fallback_used
        )
        route = decision_route(
            {
                **state,
                "agent_decision": checked_data,
                "requires_human_review": forced_human_review,
            },
            self.dependencies.thresholds,
        )
        return {
            "agent_decision": checked_data,
            "evidence_complete": evidence_complete,
            "evidence_check_passed": evidence_complete and not issues,
            "evidence_check_issues": issues,
            "requires_human_review": route == "human_review",
        }

    async def auto_pass(self, state: ModerationState) -> ModerationState:
        decision = AgentDecision.model_validate(state["agent_decision"])
        return {
            "final_action": final_action_for_route("auto_pass").value,
            "final_risk_type": decision.risk_type.value,
            "requires_human_review": False,
        }

    async def auto_reject(self, state: ModerationState) -> ModerationState:
        decision = AgentDecision.model_validate(state["agent_decision"])
        return {
            "final_action": final_action_for_route("auto_reject").value,
            "final_risk_type": decision.risk_type.value,
            "requires_human_review": False,
        }

    async def auto_limit(self, state: ModerationState) -> ModerationState:
        decision = AgentDecision.model_validate(state["agent_decision"])
        return {
            "final_action": final_action_for_route("auto_limit").value,
            "final_risk_type": decision.risk_type.value,
            "requires_human_review": False,
        }

    async def human_review(self, state: ModerationState) -> ModerationState:
        decision = AgentDecision.model_validate(state["agent_decision"])
        review_payload = interrupt(
            {
                "kind": "moderation_human_review",
                "task_id": state["task_id"],
                "thread_id": state["thread_id"],
                "content": state["content"],
                "agent_decision": decision.model_dump(mode="json"),
                "context_evidence": state.get("context_evidence"),
                "signals": state.get("signals", []),
            }
        )
        human_decision = HumanDecision.model_validate(review_payload)
        return {
            "human_decision": human_decision.model_dump(mode="json"),
            "final_action": human_decision.action.value,
            "final_risk_type": (
                human_decision.risk_type.value
                if human_decision.risk_type
                else decision.risk_type.value
            ),
            "requires_human_review": True,
        }

    async def save_final_result(self, state: ModerationState) -> ModerationState:
        return {"status": ModerationTaskStatus.COMPLETED.value}

    @staticmethod
    def _context(state: ModerationState) -> ModerationContextEvidence | None:
        value = state.get("context_evidence")
        return ModerationContextEvidence.model_validate(value) if value else None

    @staticmethod
    def _signals(state: ModerationState) -> list[ModerationSignalEvidence]:
        return [
            ModerationSignalEvidence.model_validate(signal) for signal in state.get("signals", [])
        ]


def _validated_decision_policies(
    state: ModerationState,
    decision: AgentDecision,
) -> list[PolicyEvidence]:
    allowed = {
        str(policy.policy_id): policy
        for value in state.get("matched_policies", [])
        for policy in [PolicyEvidence.model_validate(value)]
        if _policy_is_current(policy)
    }
    selected: list[PolicyEvidence] = []
    for claimed in decision.matched_policies:
        policy = allowed.get(str(claimed.policy_id))
        if policy is None:
            continue
        if policy.risk_type is not None and policy.risk_type != decision.risk_type:
            continue
        selected.append(policy)
    return list({policy.policy_id: policy for policy in selected}.values())


def _policy_is_current(policy: PolicyEvidence, *, as_of: datetime | None = None) -> bool:
    if policy.enabled is False:
        return False
    now = as_of or datetime.now(UTC)
    effective_at = policy.effective_at
    expires_at = policy.expires_at
    if effective_at is not None:
        effective_at = (
            effective_at.replace(tzinfo=UTC)
            if effective_at.tzinfo is None
            else effective_at.astimezone(UTC)
        )
        if effective_at > now:
            return False
    if expires_at is not None:
        expires_at = (
            expires_at.replace(tzinfo=UTC)
            if expires_at.tzinfo is None
            else expires_at.astimezone(UTC)
        )
        if expires_at <= now:
            return False
    return True
