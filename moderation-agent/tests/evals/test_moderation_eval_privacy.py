import pytest

from evals.moderation.privacy import PrivacyValidationError, inspect_privacy, validate_privacy
from evals.moderation.schemas import (
    EvalAnnotation,
    EvalAnnotationStatus,
    EvalCaseSource,
    EvalInput,
    EvalLabel,
    EvalPolicyReference,
    EvalPolicySnapshot,
    EvalPrivacyDeclaration,
    EvalPrivacyMode,
    ModerationEvalCase,
)
from moderation.schemas import ModerationAction, RiskType


def _privacy_case(
    content: str,
    *,
    source: EvalCaseSource = EvalCaseSource.CURATED_SEED,
    mode: EvalPrivacyMode = EvalPrivacyMode.NO_SENSITIVE_DATA,
    declared: list[str] | None = None,
    reason: str = "隐私测试样本。",
) -> ModerationEvalCase:
    return ModerationEvalCase(
        case_id="privacy-case",
        scenario_group_id="privacy-group",
        input=EvalInput(content=content),
        label=EvalLabel(
            primary_risk_type=RiskType.PRIVACY,
            risk_labels=[RiskType.PRIVACY],
            expected_action=ModerationAction.REJECT,
            acceptable_actions=[ModerationAction.REJECT],
            policy_codes=["PRIVACY-001"],
            reason=reason,
        ),
        annotation=EvalAnnotation(
            status=EvalAnnotationStatus.PROPOSED,
            source=source,
        ),
        privacy=EvalPrivacyDeclaration(
            mode=mode,
            synthetic_sensitive_values=declared or [],
        ),
        policy_snapshot=EvalPolicySnapshot(
            snapshot_id="privacy-policy-v1",
            policies=[EvalPolicyReference(code="PRIVACY-001")],
        ),
    )


def test_declared_synthetic_sensitive_value_is_allowed() -> None:
    case = _privacy_case(
        "示例手机号 13800000000",
        mode=EvalPrivacyMode.SYNTHETIC_ONLY,
        declared=["13800000000"],
    )

    report = validate_privacy([case])

    assert report.passed
    assert report.warnings == ()


def test_undeclared_sensitive_value_fails_without_echoing_raw_value() -> None:
    case = _privacy_case("联系 13800000000")

    with pytest.raises(PrivacyValidationError) as exc_info:
        validate_privacy([case])

    message = str(exc_info.value)
    assert "not declared" in message or "without a synthetic declaration" in message
    assert "13800000000" not in message
    assert "138****0000" in message


@pytest.mark.parametrize(
    ("content", "declared"),
    [
        ("号码写成 138-0000-0000", "138-0000-0000"),
        ("邮箱写成 victim[at]example[dot]com", "victim[at]example[dot]com"),
        ("password:DemoOnly-7788", "DemoOnly-7788"),
    ],
)
def test_obfuscated_and_credential_values_are_detected(
    content: str,
    declared: str,
) -> None:
    undeclared_case = _privacy_case(content)
    with pytest.raises(PrivacyValidationError):
        validate_privacy([undeclared_case])

    declared_case = _privacy_case(
        content,
        mode=EvalPrivacyMode.SYNTHETIC_ONLY,
        declared=[declared],
    )
    assert validate_privacy([declared_case]).passed


def test_production_derived_source_cannot_claim_raw_value_is_synthetic() -> None:
    case = _privacy_case(
        "用户邮箱 victim@example.com",
        source=EvalCaseSource.HUMAN_REVIEW,
        mode=EvalPrivacyMode.SYNTHETIC_ONLY,
        declared=["victim@example.com"],
    )

    with pytest.raises(PrivacyValidationError, match="forbidden for production-derived"):
        validate_privacy([case])


def test_scanner_checks_labels_and_warns_on_unused_declarations() -> None:
    case = _privacy_case(
        "正文没有联系方式",
        mode=EvalPrivacyMode.SYNTHETIC_ONLY,
        declared=["unused@example.com"],
        reason="审核理由误写了 reviewer@example.com。",
    )

    report = inspect_privacy([case])

    assert len(report.errors) == 1
    assert report.errors[0].path.endswith(".label.reason")
    assert len(report.warnings) == 1
    assert "unused@example.com" not in report.warnings[0].redacted_value
