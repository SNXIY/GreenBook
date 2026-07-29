import asyncio
from collections.abc import Awaitable, Callable

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel

from agents.moderation.tools.runtime import ToolRuntime
from community.tools import CommunityEvidenceReader, default_community_context_loader
from moderation.schemas import (
    ContactDetectionResult,
    ContentEvidenceItem,
    ContentReportsData,
    ContentReportsResult,
    ConversationContextData,
    ConversationContextResult,
    DetectContactInformationInput,
    ExplainObfuscatedExpressionInput,
    GetAuthorRecentContentsInput,
    GetAuthorViolationHistoryInput,
    GetContentReportsInput,
    GetConversationContextInput,
    GetParentCommentInput,
    ModerationToolName,
    ObfuscatedExpressionResult,
    ParentCommentData,
    ParentCommentResult,
    PolicySearchData,
    PolicySearchResult,
    RecentContentsData,
    RecentContentsResult,
    ReportSummaryItem,
    RiskType,
    SearchPlatformPoliciesInput,
    SearchSimilarReviewCasesInput,
    SimilarCasesData,
    SimilarCasesResult,
    ToolCallingConfig,
    ViolationHistoryData,
    ViolationHistoryItem,
    ViolationHistoryResult,
)
from moderation.schemas.context import (
    CommunityContentRecord,
    ReportEvidence,
    ViolationRecord,
)
from moderation.schemas.evidence import CaseEvidence, PolicyEvidence
from moderation.services.evidence_detection import EvidenceDetectionService
from rag.cases import CaseRetriever, default_case_retriever
from rag.policy import PolicyRetriever, default_policy_retriever


def build_moderation_tools(
    *,
    community_reader: CommunityEvidenceReader = default_community_context_loader,
    policy_retriever: PolicyRetriever = default_policy_retriever,
    case_retriever: CaseRetriever = default_case_retriever,
    platform: str = "default",
    config: ToolCallingConfig | None = None,
    detection_service: EvidenceDetectionService | None = None,
) -> list[BaseTool]:
    runtime = ToolRuntime(config or ToolCallingConfig())
    detector = detection_service or EvidenceDetectionService()

    async def get_parent_comment(comment_id: str) -> str:
        result = await runtime.execute(
            tool_name="get_parent_comment",
            result_type=ParentCommentResult,
            operation=lambda: community_reader.get_parent_comment(comment_id),
            build_result=lambda comment: ParentCommentResult(
                success=True,
                tool_name="get_parent_comment",
                data=ParentCommentData(
                    found=comment is not None,
                    comment=_content_item(comment) if comment else None,
                ),
            ),
        )
        return runtime.serialize(result)

    async def get_conversation_context(content_id: str, limit: int = 10) -> str:
        result = await runtime.execute(
            tool_name="get_conversation_context",
            result_type=ConversationContextResult,
            operation=lambda: community_reader.get_conversation_context(content_id, limit),
            build_result=lambda items: ConversationContextResult(
                success=True,
                tool_name="get_conversation_context",
                data=ConversationContextData(items=[_content_item(item) for item in items[:limit]]),
            ),
        )
        return runtime.serialize(result)

    async def get_author_recent_contents(author_id: str, limit: int = 10) -> str:
        result = await runtime.execute(
            tool_name="get_author_recent_contents",
            result_type=RecentContentsResult,
            operation=lambda: community_reader.get_author_recent_contents(author_id, limit),
            build_result=lambda items: RecentContentsResult(
                success=True,
                tool_name="get_author_recent_contents",
                data=RecentContentsData(items=[_content_item(item) for item in items[:limit]]),
            ),
        )
        return runtime.serialize(result)

    async def get_author_violation_history(author_id: str) -> str:
        result = await runtime.execute(
            tool_name="get_author_violation_history",
            result_type=ViolationHistoryResult,
            operation=lambda: community_reader.get_author_violation_history(author_id),
            build_result=lambda items: ViolationHistoryResult(
                success=True,
                tool_name="get_author_violation_history",
                data=ViolationHistoryData(items=[_violation_item(item) for item in items[:50]]),
            ),
        )
        return runtime.serialize(result)

    async def get_content_reports(content_id: str) -> str:
        result = await runtime.execute(
            tool_name="get_content_reports",
            result_type=ContentReportsResult,
            operation=lambda: community_reader.get_content_reports(content_id),
            build_result=lambda items: ContentReportsResult(
                success=True,
                tool_name="get_content_reports",
                data=ContentReportsData(
                    report_count=len(items),
                    items=[_report_item(item) for item in items[:20]],
                ),
            ),
        )
        return runtime.serialize(result)

    async def search_platform_policies(
        query: str,
        risk_type: RiskType | None = None,
        limit: int = 5,
    ) -> str:
        risk_types = (risk_type,) if risk_type is not None else tuple(RiskType)
        result = await runtime.execute(
            tool_name="search_platform_policies",
            result_type=PolicySearchResult,
            operation=lambda: policy_retriever.search(
                query=query,
                platform=platform,
                risk_types=risk_types,
                limit=limit,
            ),
            build_result=lambda items: PolicySearchResult(
                success=True,
                tool_name="search_platform_policies",
                data=PolicySearchData(policies=[_policy_item(item) for item in items[:limit]]),
            ),
        )
        return runtime.serialize(result)

    async def search_similar_review_cases(
        content: str,
        risk_type: RiskType | None = None,
        limit: int = 3,
    ) -> str:
        risk_types = (risk_type,) if risk_type is not None else tuple(RiskType)
        result = await runtime.execute(
            tool_name="search_similar_review_cases",
            result_type=SimilarCasesResult,
            operation=lambda: case_retriever.search(
                query=content,
                platform=platform,
                risk_types=risk_types,
                limit=limit,
            ),
            build_result=lambda items: SimilarCasesResult(
                success=True,
                tool_name="search_similar_review_cases",
                data=SimilarCasesData(cases=[_case_item(item) for item in items[:limit]]),
            ),
        )
        return runtime.serialize(result)

    async def explain_obfuscated_expression(
        expression: str,
        context: str | None = None,
    ) -> str:
        async def detect():
            return await asyncio.to_thread(
                detector.explain_obfuscated_expression,
                expression,
                context,
            )

        result = await runtime.execute(
            tool_name="explain_obfuscated_expression",
            result_type=ObfuscatedExpressionResult,
            operation=detect,
            build_result=lambda data: ObfuscatedExpressionResult(
                success=True,
                tool_name="explain_obfuscated_expression",
                data=data,
            ),
        )
        return runtime.serialize(result)

    async def detect_contact_information(content: str) -> str:
        async def detect():
            return await asyncio.to_thread(detector.detect_contact_information, content)

        result = await runtime.execute(
            tool_name="detect_contact_information",
            result_type=ContactDetectionResult,
            operation=detect,
            build_result=lambda data: ContactDetectionResult(
                success=True,
                tool_name="detect_contact_information",
                data=data,
            ),
        )
        return runtime.serialize(result)

    return [
        _tool(
            runtime,
            "get_parent_comment",
            "Get the parent comment only when the current content is a reply and the target, "
            "reference, sarcasm, or insult cannot be understood without that parent. This tool "
            "returns evidence only and never decides a moderation action.",
            GetParentCommentInput,
            get_parent_comment,
        ),
        _tool(
            runtime,
            "get_conversation_context",
            "Get a bounded conversation window when pronouns, irony, indirect attacks, or missing "
            "dialogue context prevent reliable interpretation. Do not call for standalone content.",
            GetConversationContextInput,
            get_conversation_context,
        ),
        _tool(
            runtime,
            "get_author_recent_contents",
            "Get a bounded list of the author's recent content only to investigate repeated ads, "
            "repeated contact details, spam, or evasion. History is auxiliary evidence and cannot "
            "prove the current content is a violation.",
            GetAuthorRecentContentsInput,
            get_author_recent_contents,
        ),
        _tool(
            runtime,
            "get_author_violation_history",
            "Get prior confirmed violations only to assess repetition or human-review priority. "
            "Never reject the current content solely because of author history.",
            GetAuthorViolationHistoryInput,
            get_author_violation_history,
        ),
        _tool(
            runtime,
            "get_content_reports",
            "Get aggregate reports for the current content. Reports are unverified signals, not "
            "proof of a violation, and reporter identities are not returned.",
            GetContentReportsInput,
            get_content_reports,
        ),
        _tool(
            runtime,
            "search_platform_policies",
            "Search the existing platform Policy RAG index for rules relevant to the current "
            "content and risk hypothesis. Returned policy IDs must be used exactly as provided.",
            SearchPlatformPoliciesInput,
            search_platform_policies,
        ),
        _tool(
            runtime,
            "search_similar_review_cases",
            "Search a bounded number of corrected human-review cases. Cases are reference evidence "
            "only and never override an applicable current platform policy.",
            SearchSimilarReviewCasesInput,
            search_similar_review_cases,
        ),
        _tool(
            runtime,
            "explain_obfuscated_expression",
            "Deterministically explain suspected euphemisms, abbreviations, homophones, or character "
            "substitutions such as vx, v信, or 薇信. It does not call another model.",
            ExplainObfuscatedExpressionInput,
            explain_obfuscated_expression,
        ),
        _tool(
            runtime,
            "detect_contact_information",
            "Deterministically detect and mask phone numbers, emails, identity numbers, URLs, and "
            "common contact-channel hints in the supplied current content.",
            DetectContactInformationInput,
            detect_contact_information,
        ),
    ]


def moderation_tools_by_name(tools: list[BaseTool]) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in tools}


def _tool(
    runtime: ToolRuntime,
    name: ModerationToolName,
    description: str,
    args_schema: type[BaseModel],
    coroutine: Callable[..., Awaitable[str]],
) -> BaseTool:
    return StructuredTool.from_function(
        name=name,
        description=description,
        args_schema=args_schema,
        coroutine=coroutine,
        tags=["moderation", "moderation_tool_execution"],
        metadata={"trace_name": "moderation_tool_execution", "tool_name": name},
        handle_validation_error=lambda _: runtime.invalid_arguments(name),
    )


def _content_item(item: CommunityContentRecord) -> ContentEvidenceItem:
    return ContentEvidenceItem(
        content_id=item.content_id,
        content_type=item.content_type,
        author_id=item.author_id,
        content=item.content[:2000],
        title=item.title[:500] if item.title else None,
        audit_status=item.audit_status,
        created_at=item.created_at,
    )


def _violation_item(item: ViolationRecord) -> ViolationHistoryItem:
    return ViolationHistoryItem(
        content_id=item.content_id,
        risk_type=item.risk_type,
        action=item.action,
        reason=item.reason[:1000],
        created_at=item.created_at,
    )


def _report_item(item: ReportEvidence) -> ReportSummaryItem:
    return ReportSummaryItem(
        report_type=item.report_type,
        reason=item.reason[:1000],
        created_at=item.created_at,
    )


def _policy_item(item: PolicyEvidence) -> PolicyEvidence:
    return item.model_copy(
        update={
            "code": item.code[:128],
            "title": item.title[:500],
            "excerpt": item.excerpt[:2000],
        }
    )


def _case_item(item: CaseEvidence) -> CaseEvidence:
    return item.model_copy(
        update={
            "content_excerpt": item.content_excerpt[:1000],
            "reviewer_reason": item.reviewer_reason[:1000] if item.reviewer_reason else None,
        }
    )
