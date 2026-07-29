import pytest

from evals.moderation.dedup import (
    DuplicateKind,
    DuplicateValidationError,
    exact_fingerprint,
    inspect_duplicates,
    normalize_text,
    validate_duplicates,
)
from evals.moderation.schemas import (
    EvalAnnotation,
    EvalAnnotationStatus,
    EvalCaseSource,
    EvalInput,
    EvalLabel,
    EvalPolicyReference,
    EvalPolicySnapshot,
    ModerationEvalCase,
)
from moderation.schemas import ModerationAction, RiskType


def _case(case_id: str, group_id: str, content: str) -> ModerationEvalCase:
    return ModerationEvalCase(
        case_id=case_id,
        scenario_group_id=group_id,
        input=EvalInput(content=content),
        label=EvalLabel(
            primary_risk_type=RiskType.ADVERTISING,
            risk_labels=[RiskType.ADVERTISING],
            expected_action=ModerationAction.REJECT,
            acceptable_actions=[ModerationAction.REJECT],
            policy_codes=["ADV-001"],
            reason="测试标签。",
        ),
        annotation=EvalAnnotation(
            status=EvalAnnotationStatus.PROPOSED,
            source=EvalCaseSource.CURATED_SEED,
        ),
        policy_snapshot=EvalPolicySnapshot(
            snapshot_id="policy-v1",
            policies=[EvalPolicyReference(code="ADV-001")],
        ),
    )


def test_exact_fingerprint_normalizes_width_case_whitespace_and_zero_width() -> None:
    left = _case("case-left", "group-left", "ＡＢＣ  Offer")
    right = _case("case-right", "group-right", "abc\u200b\toffer")

    assert normalize_text(left.input.content) == "abc offer"
    assert exact_fingerprint(left) == exact_fingerprint(right)

    report = inspect_duplicates([left, right])
    assert report.exact[0].kind == DuplicateKind.EXACT
    with pytest.raises(DuplicateValidationError, match="EXACT"):
        validate_duplicates([left, right])


def test_near_duplicates_inside_scenario_group_are_intentional() -> None:
    left = _case(
        "case-left",
        "minimal-pair",
        "今天课程五折，想购买的请直接私信我下单。",
    )
    right = _case(
        "case-right",
        "minimal-pair",
        "今天课程五折，想了解内容的请直接在评论区提问。",
    )

    report = inspect_duplicates([left, right], near_threshold=0.45)

    assert report.near == ()
    assert len(report.intentional_variants) == 1
    validate_duplicates([left, right], near_threshold=0.45)


def test_cross_scenario_near_duplicates_are_blocking() -> None:
    left = _case(
        "case-left",
        "group-left",
        "限时课程今天五折，想购买的请直接私信我下单领取。",
    )
    right = _case(
        "case-right",
        "group-right",
        "限时课程今天六折，想购买的请直接私信我下单领取。",
    )

    report = inspect_duplicates([left, right], near_threshold=0.70)

    assert len(report.near) == 1
    assert report.near[0].similarity >= 0.70
    with pytest.raises(DuplicateValidationError, match="NEAR"):
        validate_duplicates([left, right], near_threshold=0.70)
