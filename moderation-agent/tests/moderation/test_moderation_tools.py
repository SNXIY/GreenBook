import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from langchain_core.tools import BaseTool

from agents.moderation.tools import (
    ModerationToolOperationError,
    build_moderation_tools,
    moderation_tools_by_name,
)
from moderation.schemas import (
    CaseEvidence,
    CommunityContentRecord,
    ContactDetectionResult,
    ContentReportsResult,
    ConversationContextResult,
    ModerationAction,
    ModerationContentType,
    ObfuscatedExpressionResult,
    ParentCommentResult,
    PolicyEvidence,
    PolicySearchResult,
    RecentContentsResult,
    ReportEvidence,
    RiskType,
    SimilarCasesResult,
    ToolCallingConfig,
    ToolResult,
    ViolationHistoryResult,
    ViolationRecord,
)


class FakeCommunityReader:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.parent = CommunityContentRecord(
            content_id="comment-parent",
            content_type=ModerationContentType.COMMENT,
            author_id="author-parent",
            content="Call 13812345678 or email alice@example.com",
            audit_status="PUBLISHED",
            created_at=now,
        )
        self.items = [
            CommunityContentRecord(
                content_id=f"comment-{index}",
                content_type=ModerationContentType.COMMENT,
                author_id="author-1",
                content=f"context {index} " + "x" * 2000,
                audit_status="PUBLISHED",
                created_at=now,
            )
            for index in range(10)
        ]
        self.parent_delay = 0.0
        self.parent_failures = 0
        self.parent_calls = 0
        self.last_conversation_limit: int | None = None
        self.last_recent_limit: int | None = None

    async def get_parent_comment(
        self,
        content_id: str,
    ) -> CommunityContentRecord | None:
        self.parent_calls += 1
        if self.parent_delay:
            await asyncio.sleep(self.parent_delay)
        if self.parent_failures:
            self.parent_failures -= 1
            raise ModerationToolOperationError(
                "temporary phone backend failure for 13812345678",
                code="RETRYABLE_ERROR",
                retryable=True,
            )
        assert content_id == "comment-current"
        return self.parent

    async def get_conversation_context(
        self,
        content_id: str,
        limit: int = 10,
    ) -> list[CommunityContentRecord]:
        assert content_id == "comment-current"
        self.last_conversation_limit = limit
        return self.items[:limit]

    async def get_author_recent_contents(
        self,
        author_id: str,
        limit: int = 10,
    ) -> list[CommunityContentRecord]:
        assert author_id == "author-1"
        self.last_recent_limit = limit
        return self.items[:limit]

    async def get_author_violation_history(
        self,
        author_id: str,
    ) -> list[ViolationRecord]:
        assert author_id == "author-1"
        return [
            ViolationRecord(
                content_id="old-comment",
                risk_type=RiskType.ABUSE,
                action=ModerationAction.REJECT,
                reason="Confirmed targeted abuse",
                created_at=datetime.now(UTC),
            )
        ]

    async def get_content_reports(self, content_id: str) -> list[ReportEvidence]:
        assert content_id == "comment-current"
        return [
            ReportEvidence(
                report_type="ABUSE",
                reason="Possible personal attack",
                reporter_id="private-reporter-id",
            )
        ]


class FakePolicyRetriever:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types,
        limit: int = 5,
    ) -> list[PolicyEvidence]:
        self.calls.append(
            {
                "query": query,
                "platform": platform,
                "risk_types": tuple(risk_types),
                "limit": limit,
            }
        )
        return [
            PolicyEvidence(
                policy_id=uuid4(),
                code="ADV-001",
                title="Off-platform advertising",
                excerpt="Requests to move transactions off platform are prohibited.",
                score=0.91,
                risk_type=RiskType.ADVERTISING,
                default_action=ModerationAction.REJECT,
                version=2,
            )
        ]


class FakeCaseRetriever:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types,
        limit: int = 3,
    ) -> list[CaseEvidence]:
        self.calls.append(
            {
                "query": query,
                "platform": platform,
                "risk_types": tuple(risk_types),
                "limit": limit,
            }
        )
        return [
            CaseEvidence(
                case_id=uuid4(),
                content_excerpt="Add my account for course material",
                risk_type=RiskType.ADVERTISING,
                final_action=ModerationAction.REJECT,
                reviewer_reason="Clear off-platform diversion",
                score=0.83,
            )
        ]


def _tools(
    reader: FakeCommunityReader | None = None,
    *,
    config: ToolCallingConfig | None = None,
) -> tuple[dict[str, BaseTool], FakePolicyRetriever, FakeCaseRetriever]:
    policies = FakePolicyRetriever()
    cases = FakeCaseRetriever()
    tools = build_moderation_tools(
        community_reader=reader or FakeCommunityReader(),
        policy_retriever=policies,
        case_retriever=cases,
        platform="community",
        config=config,
    )
    return moderation_tools_by_name(tools), policies, cases


def test_factory_registers_the_nine_bounded_read_only_tools() -> None:
    tools, _, _ = _tools()

    assert list(tools) == [
        "get_parent_comment",
        "get_conversation_context",
        "get_author_recent_contents",
        "get_author_violation_history",
        "get_content_reports",
        "search_platform_policies",
        "search_similar_review_cases",
        "explain_obfuscated_expression",
        "detect_contact_information",
    ]
    assert all(tool.tags == ["moderation", "moderation_tool_execution"] for tool in tools.values())
    assert all(
        tool.metadata["trace_name"] == "moderation_tool_execution" for tool in tools.values()
    )


@pytest.mark.asyncio
async def test_parent_comment_result_is_structured_and_redacted() -> None:
    tools, _, _ = _tools()

    raw = await tools["get_parent_comment"].ainvoke({"comment_id": "comment-current"})
    result = ParentCommentResult.model_validate_json(raw)

    assert result.success is True
    assert result.data is not None
    assert result.data.found is True
    assert result.data.comment is not None
    assert result.data.comment.content == "Call 138****5678 or email a***@example.com"
    assert "13812345678" not in raw
    assert "alice@example.com" not in raw


@pytest.mark.asyncio
async def test_context_and_recent_content_limits_are_forwarded() -> None:
    reader = FakeCommunityReader()
    tools, _, _ = _tools(reader)

    context_raw = await tools["get_conversation_context"].ainvoke(
        {"content_id": "comment-current", "limit": 2}
    )
    recent_raw = await tools["get_author_recent_contents"].ainvoke(
        {"author_id": "author-1", "limit": 3}
    )

    context = ConversationContextResult.model_validate_json(context_raw)
    recent = RecentContentsResult.model_validate_json(recent_raw)
    assert reader.last_conversation_limit == 2
    assert reader.last_recent_limit == 3
    assert context.data is not None and len(context.data.items) == 2
    assert recent.data is not None and len(recent.data.items) == 3


@pytest.mark.asyncio
async def test_history_and_reports_return_auxiliary_data_without_reporter_identity() -> None:
    tools, _, _ = _tools()

    history_raw = await tools["get_author_violation_history"].ainvoke({"author_id": "author-1"})
    reports_raw = await tools["get_content_reports"].ainvoke({"content_id": "comment-current"})

    history = ViolationHistoryResult.model_validate_json(history_raw)
    reports = ContentReportsResult.model_validate_json(reports_raw)
    assert history.data is not None
    assert history.data.items[0].risk_type == RiskType.ABUSE
    assert reports.data is not None and reports.data.report_count == 1
    assert "reporter_id" not in reports_raw
    assert "private-reporter-id" not in reports_raw


@pytest.mark.asyncio
async def test_policy_and_case_tools_reuse_existing_retrievers() -> None:
    tools, policies, cases = _tools()

    policy_raw = await tools["search_platform_policies"].ainvoke(
        {"query": "加我 vx", "risk_type": "ADVERTISING", "limit": 2}
    )
    case_raw = await tools["search_similar_review_cases"].ainvoke(
        {"content": "加我 vx", "risk_type": "ADVERTISING", "limit": 1}
    )

    policy_result = PolicySearchResult.model_validate_json(policy_raw)
    case_result = SimilarCasesResult.model_validate_json(case_raw)
    assert policy_result.data is not None and policy_result.data.policies[0].code == "ADV-001"
    assert case_result.data is not None
    assert case_result.data.cases[0].final_action == ModerationAction.REJECT
    assert policies.calls == [
        {
            "query": "加我 vx",
            "platform": "community",
            "risk_types": (RiskType.ADVERTISING,),
            "limit": 2,
        }
    ]
    assert cases.calls[0]["platform"] == "community"


@pytest.mark.asyncio
async def test_deterministic_tools_detect_obfuscation_and_mask_contacts() -> None:
    tools, _, _ = _tools()

    obfuscated_raw = await tools["explain_obfuscated_expression"].ainvoke(
        {"expression": "内部资料，私聊加我 vx"}
    )
    contacts_raw = await tools["detect_contact_information"].ainvoke(
        {"content": "加我 vx，手机号 13812345678，邮箱 alice@example.com"}
    )

    obfuscated = ObfuscatedExpressionResult.model_validate_json(obfuscated_raw)
    contacts = ContactDetectionResult.model_validate_json(contacts_raw)
    assert obfuscated.data is not None
    assert {item.normalized_form for item in obfuscated.data.matches} >= {
        "微信/WeChat",
        "转入私聊",
    }
    assert contacts.data is not None and contacts.data.has_contact_information is True
    assert {item.kind for item in contacts.data.findings} >= {
        "PHONE",
        "EMAIL",
        "WECHAT_HINT",
    }
    assert "13812345678" not in contacts_raw
    assert "alice@example.com" not in contacts_raw


@pytest.mark.asyncio
async def test_invalid_arguments_are_returned_as_a_structured_error() -> None:
    tools, _, _ = _tools()

    raw = await tools["get_conversation_context"].ainvoke(
        {"content_id": "comment-current", "limit": 100}
    )
    result = ToolResult.model_validate_json(raw)

    assert result.success is False
    assert result.error_code == "INVALID_ARGUMENT"
    assert "100" not in result.error_message


@pytest.mark.asyncio
async def test_retryable_failure_is_retried_once_and_error_text_is_redacted() -> None:
    reader = FakeCommunityReader()
    reader.parent_failures = 1
    tools, _, _ = _tools(reader, config=ToolCallingConfig(max_retries=1))

    raw = await tools["get_parent_comment"].ainvoke({"comment_id": "comment-current"})
    result = ParentCommentResult.model_validate_json(raw)

    assert result.success is True
    assert reader.parent_calls == 2
    assert "13812345678" not in raw


@pytest.mark.asyncio
async def test_tool_timeout_returns_structured_failure_instead_of_raising() -> None:
    reader = FakeCommunityReader()
    reader.parent_delay = 0.05
    tools, _, _ = _tools(
        reader,
        config=ToolCallingConfig(tool_timeout_seconds=0.01, max_retries=0),
    )

    raw = await tools["get_parent_comment"].ainvoke({"comment_id": "comment-current"})
    result = ParentCommentResult.model_validate_json(raw)

    assert result.success is False
    assert result.error_code == "TIMEOUT"
    assert result.retryable is True


@pytest.mark.asyncio
async def test_oversized_result_is_valid_json_and_respects_output_budget() -> None:
    tools, _, _ = _tools(config=ToolCallingConfig(max_result_chars=600))

    raw = await tools["get_conversation_context"].ainvoke(
        {"content_id": "comment-current", "limit": 10}
    )
    result = ConversationContextResult.model_validate_json(raw)

    assert len(raw) <= 600
    assert result.success is True
    assert result.is_partial is True
    assert result.error_code == "RESULT_TRUNCATED"
