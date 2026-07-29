import json
from typing import Any, cast

from langchain_core.messages import AIMessage

from agents.moderation.state import ModerationState
from moderation.schemas import (
    CaseEvidence,
    CommunityContentRecord,
    ContentEvidenceItem,
    EvidenceCollectionResult,
    ModerationContentType,
    ModerationContextEvidence,
    ModerationSignalEvidence,
    ModerationSignalType,
    ModerationToolName,
    PolicyEvidence,
    ReportEvidence,
    RiskClassification,
    RiskType,
    SignalSource,
    ViolationRecord,
)


class EvidenceCollectionNodes:
    async def finalize(self, state: ModerationState) -> ModerationState:
        if state.get("use_dynamic_tool_agent", False):
            result = _dynamic_collection_result(state)
            if result is None:
                return {
                    "tool_agent_error": "evidence_collection_finalize:InvalidFinalResult",
                    "evidence_collection_complete": False,
                }
        else:
            result = _fixed_collection_result(state)

        policies = _collected_policies(state) or [
            PolicyEvidence.model_validate(item) for item in state.get("matched_policies", [])
        ]
        cases = _collected_cases(state) or [
            CaseEvidence.model_validate(item) for item in state.get("similar_cases", [])
        ]
        context = _collected_context(state)
        if context is None and state.get("context_evidence"):
            context = ModerationContextEvidence.model_validate(state["context_evidence"])
        signals = _merge_signals(state, context)
        missing = _critical_missing_evidence(state, result, policies, context)
        complete = result.complete and not result.missing_evidence and not missing
        result = result.model_copy(
            update={
                "complete": complete,
                "used_tools": _valid_tool_names(state.get("called_tools", [])),
                "failed_tools": _valid_tool_names(state.get("failed_tools", [])),
                "missing_evidence": list(dict.fromkeys([*result.missing_evidence, *missing]))[:20],
            }
        )
        requires_human = bool(
            result.recommended_path == "HUMAN_REVIEW"
            or (not complete and _missing_is_critical(state, missing))
        )
        classification = RiskClassification.model_validate(state["classification"])
        if state.get("tool_budget_exceeded") or state.get("failed_tools"):
            penalty = 0.15 if state.get("tool_budget_exceeded") else 0.05
            classification = classification.model_copy(
                update={"confidence": max(0.0, classification.confidence - penalty)}
            )

        context_data = context.model_dump(mode="json") if context is not None else None
        policy_data = [item.model_dump(mode="json") for item in policies]
        case_data = [item.model_dump(mode="json") for item in cases]
        signal_data = [item.model_dump(mode="json") for item in signals]
        summary = {
            "collection_result": result.model_dump(mode="json"),
            "tool_agent_fallback_used": state.get("tool_agent_fallback_used", False),
            "tool_agent_error": state.get("tool_agent_error"),
        }
        return {
            "classification": classification.model_dump(mode="json"),
            "risk_hypotheses": (
                [risk_type.value for risk_type in result.risk_hypotheses]
                or state.get("risk_hypotheses", [])
            ),
            "matched_policies": policy_data,
            "similar_cases": case_data,
            "context_evidence": context_data,
            "signals": signal_data,
            "evidence_collection_complete": complete,
            "evidence_summary": summary,
            "evidence_gaps": result.missing_evidence,
            "requires_human_review": requires_human,
            "tool_agent_error": (
                state.get("tool_agent_error") if state.get("tool_agent_fallback_used") else None
            ),
        }


def _dynamic_collection_result(state: ModerationState) -> EvidenceCollectionResult | None:
    last_message = state.get("messages", [])[-1] if state.get("messages") else None
    if isinstance(last_message, AIMessage) and not last_message.tool_calls:
        try:
            return EvidenceCollectionResult.model_validate_json(str(last_message.content))
        except (TypeError, ValueError):
            return None

    if not state.get("tool_budget_exceeded"):
        return None
    classification = RiskClassification.model_validate(state["classification"])
    return EvidenceCollectionResult(
        complete=False,
        risk_hypotheses=[classification.risk_type],
        collected_evidence=[],
        missing_evidence=["The Tool Agent reached its budget before producing a final summary."],
        used_tools=_valid_tool_names(state.get("called_tools", [])),
        failed_tools=_valid_tool_names(state.get("failed_tools", [])),
        recommended_path="HUMAN_REVIEW",
        reason="Evidence collection stopped at the configured tool budget.",
    )


def _fixed_collection_result(state: ModerationState) -> EvidenceCollectionResult:
    classification = RiskClassification.model_validate(state["classification"])
    policies = state.get("matched_policies", [])
    complete = classification.risk_type == RiskType.NORMAL or bool(policies)
    if not complete:
        recommended_path = "HUMAN_REVIEW"
    elif classification.risk_type == RiskType.NORMAL:
        recommended_path = "FAST_REVIEW"
    else:
        recommended_path = "ADVERSARIAL_REVIEW"
    return EvidenceCollectionResult(
        complete=complete,
        risk_hypotheses=[classification.risk_type],
        collected_evidence=[],
        missing_evidence=([] if complete else ["No applicable platform policy was retrieved."]),
        used_tools=[],
        failed_tools=[],
        recommended_path=recommended_path,
        reason="The compatible fixed evidence-collection path was used.",
    )


def _successful_results(state: ModerationState, tool_name: str) -> list[dict[str, Any]]:
    results = []
    for record in state.get("tool_results", []):
        result = record.get("result", {})
        if record.get("tool_name") == tool_name and result.get("success"):
            data = result.get("data")
            if isinstance(data, dict):
                results.append(data)
    return results


def _collected_policies(state: ModerationState) -> list[PolicyEvidence]:
    policies: dict[str, PolicyEvidence] = {}
    for data in _successful_results(state, "search_platform_policies"):
        for value in data.get("policies", []):
            policy = PolicyEvidence.model_validate(value)
            policies[str(policy.policy_id)] = policy
    return list(policies.values())


def _collected_cases(state: ModerationState) -> list[CaseEvidence]:
    cases: dict[str, CaseEvidence] = {}
    for data in _successful_results(state, "search_similar_review_cases"):
        for value in data.get("cases", []):
            case = CaseEvidence.model_validate(value)
            cases[str(case.case_id)] = case
    return list(cases.values())


def _collected_context(state: ModerationState) -> ModerationContextEvidence | None:
    context_tool_names = {
        "get_parent_comment",
        "get_conversation_context",
        "get_author_recent_contents",
        "get_author_violation_history",
        "get_content_reports",
    }
    if not any(name in context_tool_names for name in state.get("called_tools", [])):
        return None

    parent = None
    parent_results = _successful_results(state, "get_parent_comment")
    if parent_results and parent_results[-1].get("comment"):
        parent = _content_record(parent_results[-1]["comment"])

    conversation = _content_records(state, "get_conversation_context")
    recent = _content_records(state, "get_author_recent_contents")
    violations: list[ViolationRecord] = []
    for data in _successful_results(state, "get_author_violation_history"):
        violations.extend(ViolationRecord.model_validate(item) for item in data.get("items", []))
    reports: list[ReportEvidence] = []
    for data in _successful_results(state, "get_content_reports"):
        reports.extend(
            ReportEvidence(
                report_type=item["report_type"],
                reason=item["reason"],
                reporter_id=f"redacted-reporter-{index}",
                created_at=item.get("created_at"),
            )
            for index, item in enumerate(data.get("items", []), start=1)
        )

    current = None
    if state.get("content_id") and state.get("creator_id"):
        current = CommunityContentRecord(
            content_id=state["content_id"],
            content_type=ModerationContentType(state.get("content_type", "TEXT")),
            author_id=state["creator_id"],
            content=state["normalized_content"],
        )
    parent_required = bool(state.get("metadata", {}).get("parent_comment_id"))
    failed_context = bool(
        context_tool_names.intersection(state.get("failed_tools", []))
        or (parent_required and parent is None)
    )
    return ModerationContextEvidence(
        current=current,
        parent_comment=parent,
        conversation_context=conversation[:10],
        author_recent_contents=recent[:10],
        author_violation_history=violations[:50],
        reports=reports[:20],
        parent_comment_required=parent_required,
        complete=not failed_context,
        errors=(
            ["One or more requested community context tools failed."] if failed_context else []
        ),
    )


def _content_records(state: ModerationState, tool_name: str) -> list[CommunityContentRecord]:
    records: list[CommunityContentRecord] = []
    for data in _successful_results(state, tool_name):
        records.extend(_content_record(item) for item in data.get("items", []))
    return records


def _content_record(value: dict[str, Any]) -> CommunityContentRecord:
    item = ContentEvidenceItem.model_validate(value)
    return CommunityContentRecord.model_validate(item.model_dump(mode="python"))


def _merge_signals(
    state: ModerationState,
    context: ModerationContextEvidence | None,
) -> list[ModerationSignalEvidence]:
    signals = [ModerationSignalEvidence.model_validate(item) for item in state.get("signals", [])]
    contact_types: set[str] = set()
    contact_score = 0.0
    for data in _successful_results(state, "detect_contact_information"):
        for finding in data.get("findings", []):
            contact_types.add(str(finding.get("kind")))
            contact_score = max(contact_score, float(finding.get("confidence", 0.0)))
    if contact_types:
        signals.append(
            ModerationSignalEvidence(
                signal_type=ModerationSignalType.TEXT_PATTERN,
                source=SignalSource.CONTENT,
                score=contact_score,
                details={"contact_types": sorted(contact_types)},
            )
        )
    if context is not None and context.reports:
        signals.append(
            ModerationSignalEvidence(
                signal_type=ModerationSignalType.REPORT_COUNT,
                source=SignalSource.REPORT,
                score=min(1.0, len(context.reports) / 5),
                details={
                    "report_count": len(context.reports),
                    "report_types": sorted({item.report_type for item in context.reports}),
                },
            )
        )
    if context is not None and context.author_violation_history:
        signals.append(
            ModerationSignalEvidence(
                signal_type=ModerationSignalType.AUTHOR_VIOLATION_HISTORY,
                source=SignalSource.COMMUNITY,
                score=min(1.0, len(context.author_violation_history) / 5),
                details={"violation_count": len(context.author_violation_history)},
            )
        )
    if context is not None and not context.complete:
        signals.append(
            ModerationSignalEvidence(
                signal_type=ModerationSignalType.CONTEXT_INCOMPLETE,
                source=SignalSource.COMMUNITY,
                score=1.0,
                details={"errors": context.errors},
            )
        )
    unique: dict[str, ModerationSignalEvidence] = {}
    for signal in signals:
        key = json.dumps(signal.model_dump(mode="json"), sort_keys=True)
        unique[key] = signal
    return list(unique.values())


def _critical_missing_evidence(
    state: ModerationState,
    result: EvidenceCollectionResult,
    policies: list[PolicyEvidence],
    context: ModerationContextEvidence | None,
) -> list[str]:
    classification = RiskClassification.model_validate(state["classification"])
    missing = []
    if classification.risk_type != RiskType.NORMAL and not policies:
        missing.append("No valid platform policy supports the non-normal risk hypothesis.")
    if context is not None and not context.complete:
        missing.append("Required community context is incomplete.")
    if state.get("tool_budget_exceeded") and not result.complete:
        missing.append("The configured tool-call budget was exhausted.")
    return missing


def _missing_is_critical(state: ModerationState, missing: list[str]) -> bool:
    classification = RiskClassification.model_validate(state["classification"])
    return bool(missing and classification.risk_type != RiskType.NORMAL)


def _valid_tool_names(values: list[str]) -> list[ModerationToolName]:
    valid = {
        "get_parent_comment",
        "get_conversation_context",
        "get_author_recent_contents",
        "get_author_violation_history",
        "get_content_reports",
        "search_platform_policies",
        "search_similar_review_cases",
        "explain_obfuscated_expression",
        "detect_contact_information",
    }
    return [cast(ModerationToolName, value) for value in dict.fromkeys(values) if value in valid]
