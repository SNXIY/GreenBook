import json
from collections import Counter
from pathlib import Path

from evals.moderation.dedup import validate_duplicates
from evals.moderation.io import load_jsonl, validate_dataset
from evals.moderation.privacy import validate_privacy
from evals.moderation.schemas import (
    EvalAnnotationStatus,
    EvalCaseSource,
    EvalDatasetSplit,
    ModerationEvalCase,
)
from evals.moderation.seed_data import build_seed_cases
from moderation.schemas import RiskType

ROOT = Path(__file__).resolve().parents[2]
MATERIALIZED_SEED = ROOT / "evals" / "candidates" / "seed-v1.proposed.jsonl"
MATERIALIZED_SCHEMA = ROOT / "evals" / "schema" / "moderation-eval-case-v1.schema.json"


def test_seed_catalog_has_100_grouped_proposed_cases() -> None:
    cases = build_seed_cases()
    groups = Counter(case.scenario_group_id for case in cases)

    assert len(cases) == 100
    assert len({case.case_id for case in cases}) == 100
    assert len(groups) == 25
    assert set(groups.values()) == {4}
    assert all(case.annotation.status == EvalAnnotationStatus.PROPOSED for case in cases)
    assert all(case.annotation.source == EvalCaseSource.CURATED_SEED for case in cases)
    assert all(case.split == EvalDatasetSplit.UNASSIGNED for case in cases)


def test_seed_risk_distribution_is_balanced_and_valid() -> None:
    cases = build_seed_cases()
    distribution = Counter(
        case.label.primary_risk_type
        for case in cases
        if case.label is not None
    )

    assert distribution == {
        RiskType.NORMAL: 25,
        RiskType.ADVERTISING: 27,
        RiskType.ABUSE: 24,
        RiskType.PRIVACY: 24,
    }
    validate_dataset(cases)
    assert validate_privacy(cases).warnings == ()
    assert validate_duplicates(cases).blocking == ()


def test_materialized_seed_matches_deterministic_builder() -> None:
    assert MATERIALIZED_SEED.exists()
    assert load_jsonl(MATERIALIZED_SEED) == build_seed_cases()


def test_materialized_json_schema_matches_pydantic_contract() -> None:
    actual = json.loads(MATERIALIZED_SCHEMA.read_text(encoding="utf-8"))
    actual.pop("$id")
    actual.pop("$schema")

    assert actual == ModerationEvalCase.model_json_schema(mode="validation")
