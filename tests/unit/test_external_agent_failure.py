from __future__ import annotations

import pytest
from greenbook_contracts import (
    ExternalAgentFailure,
    FailureNormalizer,
    RecoveryAction,
    SideEffectState,
    ToolResult,
    normalize_external_failure,
)


def _failure(
    code: str,
    *,
    request_sent: bool | None,
    side_effect_state: str | None = None,
    retryable: bool = True,
    dependency: str | None = None,
) -> ToolResult[dict[str, str]]:
    state: dict[str, str] = {}
    if side_effect_state is not None:
        state["side_effect_state"] = side_effect_state
    if dependency is not None:
        state["dependency"] = dependency
    return ToolResult(
        ok=False,
        code=code,
        message="downstream detail",
        user_message="依赖暂时不可用",
        retryable=retryable,
        request_sent=request_sent,
        state=state or None,
        trace_id="trace-1",
    )


@pytest.mark.parametrize(
    ("code", "dependency", "action"),
    [
        ("JAVA_BACKEND_UNAVAILABLE", "java", RecoveryAction.RETRY),
        ("MCP_TIMEOUT", "mcp", RecoveryAction.RETRY),
        ("MODEL_TIMEOUT", "model", RecoveryAction.RETRY),
        ("RATE_LIMIT", "external", RecoveryAction.WAIT_DEPENDENCY),
        ("AUTH_FAILURE", "identity", RecoveryAction.REAUTH),
    ],
)
def test_supported_external_failure_codes_are_normalized(
    code: str,
    dependency: str,
    action: RecoveryAction,
) -> None:
    result = _failure(
        code,
        request_sent=False,
        side_effect_state=SideEffectState.NOT_STARTED,
    )

    normalized = normalize_external_failure(result)

    assert isinstance(normalized, ExternalAgentFailure)
    assert normalized.error_code == code
    assert normalized.dependency == dependency
    assert normalized.recovery_action is action
    assert normalized.user_visible_message == result.user_message


@pytest.mark.parametrize(
    ("request_sent", "expected_state", "expected_action"),
    [
        (False, SideEffectState.NOT_STARTED, RecoveryAction.RETRY),
        (True, SideEffectState.POSSIBLE, RecoveryAction.RECONCILE),
        (None, SideEffectState.UNKNOWN, RecoveryAction.RECONCILE),
    ],
)
def test_request_sent_is_three_state_and_drives_safe_recovery(
    request_sent: bool | None,
    expected_state: SideEffectState,
    expected_action: RecoveryAction,
) -> None:
    result = _failure(
        "JAVA_BACKEND_UNAVAILABLE",
        request_sent=request_sent,
    )

    normalized = FailureNormalizer.normalize(result)

    assert normalized.request_sent is request_sent
    assert normalized.side_effect_state is expected_state
    assert normalized.recovery_action is expected_action


@pytest.mark.parametrize(
    "side_effect_state",
    [
        SideEffectState.NONE,
        SideEffectState.NOT_STARTED,
        SideEffectState.POSSIBLE,
        SideEffectState.UNKNOWN,
    ],
)
def test_explicit_side_effect_states_are_preserved(
    side_effect_state: SideEffectState,
) -> None:
    result = _failure(
        "MCP_TIMEOUT",
        request_sent=None,
        side_effect_state=side_effect_state,
    )

    normalized = normalize_external_failure(result)

    assert normalized.side_effect_state is side_effect_state


def test_normalizer_preserves_original_code_and_metadata_without_mutation() -> None:
    result = _failure(
        "java_backend_unavailable",
        request_sent=None,
        side_effect_state=SideEffectState.UNKNOWN,
        dependency="java",
    )
    before = result.model_dump(mode="json")

    normalized = normalize_external_failure(result)

    assert normalized.error_code == "java_backend_unavailable"
    assert normalized.dependency == "java"
    assert normalized.trace_id == "trace-1"
    assert result.model_dump(mode="json") == before


def test_auth_failure_is_not_retryable_even_if_downstream_hint_is_unsafe() -> None:
    normalized = normalize_external_failure(
        _failure(
            "AUTH_FAILURE",
            request_sent=False,
            side_effect_state=SideEffectState.NONE,
            retryable=True,
        )
    )

    assert normalized.retryable is False
    assert normalized.recovery_action is RecoveryAction.REAUTH


def test_validation_hint_cannot_become_transport_retry() -> None:
    normalized = normalize_external_failure(
        _failure(
            "TOOL_ARGUMENT_VALIDATION_FAILED",
            request_sent=False,
            retryable=True,
        )
    )

    assert normalized.retryable is False
    assert normalized.side_effect_state is SideEffectState.NOT_STARTED
    assert normalized.recovery_action is RecoveryAction.FAIL


def test_known_business_rejection_is_not_result_unknown() -> None:
    normalized = normalize_external_failure(
        _failure(
            "BUSINESS_REJECTED",
            request_sent=True,
            retryable=False,
        )
    )

    assert normalized.side_effect_state is SideEffectState.NOT_STARTED
    assert normalized.recovery_action is RecoveryAction.FAIL


def test_internal_error_is_not_retryable_or_reconcilable_without_evidence() -> None:
    normalized = normalize_external_failure(
        ToolResult.internal_error("KeyError in local runtime")
    )

    assert normalized.retryable is False
    assert normalized.side_effect_state is SideEffectState.NOT_STARTED
    assert normalized.recovery_action is RecoveryAction.FAIL


def test_successful_tool_result_is_not_a_failure() -> None:
    with pytest.raises(ValueError, match="successful ToolResult"):
        normalize_external_failure(ToolResult.success({"ok": True}))
