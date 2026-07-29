from moderation.schemas import (
    EvidenceCollectionResult,
    EvidenceItem,
    RiskType,
    ToolAgentMetrics,
    evidence_collection_audit_from_state,
)


def test_fixed_path_without_dynamic_attempt_has_no_tool_audit() -> None:
    assert (
        evidence_collection_audit_from_state(
            {
                "use_dynamic_tool_agent": False,
                "tool_agent_fallback_used": False,
                "evidence_summary": {},
            }
        )
        is None
    )


def test_tool_audit_is_bounded_and_omits_full_tool_results() -> None:
    collection = EvidenceCollectionResult(
        complete=True,
        risk_hypotheses=[RiskType.PRIVACY],
        collected_evidence=[
            EvidenceItem(
                source="detect_contact_information",
                category="PHONE",
                summary="A masked phone-number pattern was detected.",
                quote="138****5678",
                confidence=1.0,
            )
        ],
        used_tools=["detect_contact_information"],
        failed_tools=[],
        recommended_path="ADVERSARIAL_REVIEW",
        reason="A deterministic contact detector supplied bounded evidence.",
    )
    audit = evidence_collection_audit_from_state(
        {
            "use_dynamic_tool_agent": True,
            "evidence_collection_complete": True,
            "evidence_summary": {"collection_result": collection.model_dump(mode="json")},
            "tool_results": [
                {
                    "tool_name": "detect_contact_information",
                    "success": True,
                    "cache_hit": False,
                    "round": 1,
                    "error_code": None,
                    "is_partial": False,
                    "result": {"unpersisted_raw_value": "13812345678"},
                }
            ],
            "tool_call_count": 1,
            "tool_call_round": 2,
            "tool_cache_hits": 0,
            "tool_budget_exceeded": False,
            "tool_agent_metrics": ToolAgentMetrics(
                model_name="fake-tool-model",
                latency_ms=12.5,
                total_tokens=42,
            ).model_dump(mode="json"),
        }
    )

    assert audit is not None
    assert audit.called_tools == ["detect_contact_information"]
    assert audit.tool_executions[0].success is True
    assert audit.tool_call_round == 2
    assert audit.tool_agent_metrics is not None
    assert "13812345678" not in audit.model_dump_json()
    assert "unpersisted_raw_value" not in audit.model_dump_json()


def test_fixed_fallback_is_visible_in_dynamic_audit() -> None:
    collection = EvidenceCollectionResult(
        complete=True,
        risk_hypotheses=[RiskType.NORMAL],
        collected_evidence=[],
        used_tools=[],
        failed_tools=[],
        recommended_path="FAST_REVIEW",
        reason="The compatible fixed evidence-collection path was used.",
    )
    audit = evidence_collection_audit_from_state(
        {
            "use_dynamic_tool_agent": False,
            "tool_agent_fallback_used": True,
            "tool_agent_error": "moderation_tool_agent:TimeoutError",
            "tool_call_round": 1,
            "evidence_collection_complete": True,
            "evidence_summary": {"collection_result": collection.model_dump(mode="json")},
        }
    )

    assert audit is not None
    assert audit.dynamic_attempted is True
    assert audit.fallback_used is True
    assert audit.tool_agent_error == "moderation_tool_agent:TimeoutError"
