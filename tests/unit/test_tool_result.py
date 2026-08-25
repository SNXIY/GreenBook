"""Unit tests for ToolResult and error classification."""

from __future__ import annotations

from greenbook_contracts.tool_result import OperationReceipt, ResourceRef, ToolResult


def test_success():
    r = ToolResult.success({"key": "value"}, trace_id="t1")
    assert r.ok is True
    assert r.code == "OK"
    assert r.data == {"key": "value"}
    assert r.trace_id == "t1"
    assert r.request_sent is True


def test_dependency_unavailable():
    r = ToolResult.dependency_unavailable("test", trace_id="t1")
    assert r.ok is False
    assert r.code == "DEPENDENCY_UNAVAILABLE"
    assert r.retryable is True
    assert r.request_sent is False


def test_validation_error():
    r = ToolResult.validation_error("bad input", user_message="Invalid params")
    assert r.ok is False
    assert r.code == "VALIDATION_ERROR"
    assert r.retryable is False
    assert r.request_sent is False
    assert r.user_message == "Invalid params"


def test_permission_denied():
    r = ToolResult.permission_denied("no access")
    assert r.code == "PERMISSION_DENIED"
    assert r.retryable is False


def test_not_found():
    r = ToolResult.not_found("gone")
    assert r.code == "NOT_FOUND"
    assert r.request_sent is True


def test_conflict():
    r = ToolResult.conflict("stale")
    assert r.code == "CONFLICT"


def test_draft_version_conflict():
    r = ToolResult.draft_version_conflict("version mismatch")
    assert r.code == "DRAFT_VERSION_CONFLICT"


def test_idempotency_conflict():
    r = ToolResult.idempotency_conflict("dup key")
    assert r.code == "IDEMPOTENCY_CONFLICT"


def test_timeout():
    r = ToolResult.timeout("slow")
    assert r.code == "TIMEOUT"
    assert r.retryable is True
    assert r.request_sent is True


def test_result_unknown():
    r = ToolResult.result_unknown("uncertain")
    assert r.code == "RESULT_UNKNOWN"
    assert r.retryable is False
    assert r.request_sent is None
    assert r.state["side_effect_started"] is True
    assert r.state["side_effect_state"] == "POSSIBLE"
    assert r.state["result_known"] is False

    receipt = OperationReceipt(
        operation_id="op-1",
        semantic_action="UPDATE_DRAFT",
        result_known=True,
        status="COMPLETED",
    )
    uncertain = ToolResult.result_unknown("uncertain", operation_receipt=receipt)
    assert uncertain.operation_receipt is not None
    assert uncertain.operation_receipt.result_known is False
    assert uncertain.operation_receipt.status == "RESULT_UNKNOWN"


def test_request_not_sent():
    r = ToolResult.request_not_sent("not sent")
    assert r.code == "REQUEST_NOT_SENT"
    assert r.retryable is True
    assert r.request_sent is False


def test_internal_error():
    r = ToolResult.internal_error("boom", trace_id="t1")
    assert r.code == "INTERNAL_ERROR"
    assert r.retryable is False
    assert r.request_sent is False
    assert r.trace_id == "t1"


def test_business_rejected():
    r = ToolResult.business_rejected("not allowed", user_message="Cannot do that")
    assert r.code == "BUSINESS_REJECTED"
    assert r.user_message == "Cannot do that"


def test_resource_refs():
    r = ToolResult.success(
        {"id": "123"},
        trace_id="t1",
        receipt_id="r1",
        resource_refs=[
            ResourceRef(ref="draft:123", kind="DRAFT", resource_id="123", version=1),
        ],
    )
    assert r.receipt_id == "r1"
    assert len(r.resource_refs) == 1
    assert r.resource_refs[0].kind == "DRAFT"


def test_authentication_required():
    r = ToolResult.authentication_required("login needed")
    assert r.code == "AUTHENTICATION_REQUIRED"
