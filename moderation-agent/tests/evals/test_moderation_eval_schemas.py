import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.moderation.io import (
    DatasetValidationError,
    load_jsonl,
    validate_dataset,
    write_jsonl,
)
from evals.moderation.schemas import (
    EvalAnnotation,
    EvalAnnotationStatus,
    EvalCaseSource,
    EvalDatasetSplit,
    EvalEvidenceSpan,
    EvalInput,
    EvalLabel,
    EvalPolicyReference,
    EvalPolicySnapshot,
    ModerationEvalCase,
)
from moderation.schemas import ModerationAction, RiskType


def _case(
    *,
    case_id: str = "case-001",
    group_id: str = "group-001",
    split: EvalDatasetSplit = EvalDatasetSplit.UNASSIGNED,
    status: EvalAnnotationStatus = EvalAnnotationStatus.PROPOSED,
    reviewer_ids: list[str] | None = None,
) -> ModerationEvalCase:
    content = "请直接私信我下单"
    return ModerationEvalCase(
        case_id=case_id,
        scenario_group_id=group_id,
        split=split,
        input=EvalInput(content=content),
        label=EvalLabel(
            primary_risk_type=RiskType.ADVERTISING,
            risk_labels=[RiskType.ADVERTISING],
            expected_action=ModerationAction.REJECT,
            acceptable_actions=[ModerationAction.REJECT],
            policy_codes=["ADV-001"],
            evidence_spans=[
                EvalEvidenceSpan(start=1, end=8, text="直接私信我下单"),
            ],
            reason="内容直接推动私下交易。",
        ),
        annotation=EvalAnnotation(
            status=status,
            source=EvalCaseSource.CURATED_SEED,
            reviewer_ids=reviewer_ids or [],
        ),
        policy_snapshot=EvalPolicySnapshot(
            snapshot_id="test-policy-v1",
            policies=[EvalPolicyReference(code="ADV-001")],
        ),
    )


def test_case_rejects_evidence_offsets_that_do_not_match_source() -> None:
    payload = _case().model_dump(mode="json")
    payload["label"]["evidence_spans"][0]["start"] = 0

    with pytest.raises(ValidationError, match="evidence span does not match"):
        ModerationEvalCase.model_validate(payload)


def test_unlabeled_and_human_review_states_require_truthful_provenance() -> None:
    payload = _case().model_dump(mode="json")
    payload["annotation"]["status"] = "UNLABELED"
    with pytest.raises(ValidationError, match="UNLABELED cases cannot contain a label"):
        ModerationEvalCase.model_validate(payload)

    payload = _case().model_dump(mode="json")
    payload["annotation"]["status"] = "REVIEWED"
    with pytest.raises(ValidationError, match="at least one reviewer_id"):
        ModerationEvalCase.model_validate(payload)

    payload = _case().model_dump(mode="json")
    payload["annotation"].update(
        {
            "status": "ADJUDICATED",
            "reviewer_ids": ["reviewer-a", "reviewer-b"],
            "adjudicator_id": "reviewer-a",
        }
    )
    with pytest.raises(ValidationError, match="independent"):
        ModerationEvalCase.model_validate(payload)


def test_golden_validation_requires_human_review_and_assigned_split() -> None:
    with pytest.raises(DatasetValidationError, match="golden data requires"):
        validate_dataset([_case()], require_gold=True)

    reviewed = _case(
        status=EvalAnnotationStatus.REVIEWED,
        reviewer_ids=["reviewer-01"],
        split=EvalDatasetSplit.TEST,
    )
    validate_dataset([reviewed], require_gold=True)


def test_collection_validation_rejects_duplicate_ids_and_split_leakage() -> None:
    first = _case(case_id="case-001", group_id="paired-group")
    second_payload = _case(
        case_id="case-001",
        group_id="paired-group",
        split=EvalDatasetSplit.TEST,
    ).model_dump(mode="json")
    second = ModerationEvalCase.model_validate(second_payload)

    with pytest.raises(DatasetValidationError) as exc_info:
        validate_dataset([first, second])

    message = str(exc_info.value)
    assert "duplicate case_id" in message
    assert "spans splits" in message


def test_jsonl_round_trip_is_utf8_and_schema_validated(tmp_path: Path) -> None:
    path = tmp_path / "dataset.jsonl"
    cases = [_case()]
    write_jsonl(path, cases)

    assert load_jsonl(path) == cases
    assert "私信" in path.read_text(encoding="utf-8")

    path.write_text('{"case_id":\n', encoding="utf-8")
    with pytest.raises(DatasetValidationError, match="invalid JSON"):
        load_jsonl(path)


def test_jsonl_loader_reports_record_number(tmp_path: Path) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text(
        json.dumps(_case().model_dump(mode="json"), ensure_ascii=False)
        + "\n"
        + '{"schema_version":"1.0"}\n',
        encoding="utf-8",
    )
    with pytest.raises(DatasetValidationError, match=r":2: schema validation failed"):
        load_jsonl(path)
