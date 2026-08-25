from __future__ import annotations

from greenbook_evaluation.cases.business_semantic_seeds import (
    BUSINESS_SEMANTIC_SEED_CASES,
    BUSINESS_SEMANTIC_SEEDS,
)


def test_business_semantic_seeds_have_frozen_oracles_and_seven_expression_forms() -> None:
    assert len(BUSINESS_SEMANTIC_SEEDS) == 5
    assert len(BUSINESS_SEMANTIC_SEED_CASES) == 35
    for seed in BUSINESS_SEMANTIC_SEEDS:
        assert len(seed.expressions) == 7
        assert len({expression.variant for expression in seed.expressions}) == 7
        assert seed.expected_semantic_state["objective_count"] == len(seed.expected_work_items)
        assert any(expression.counterfactual for expression in seed.expressions)


def test_multi_objective_counterfactual_flips_item_binding_not_oracle_generation() -> None:
    seed = next(item for item in BUSINESS_SEMANTIC_SEEDS if item.seed_id == "multi-publication-binding")
    assert seed.expected_work_items[0]["desired_outcome"] == "PUBLISHED"
    assert seed.counterfactual_items[0]["desired_outcome"] == "SCHEDULED"
    assert seed.expected_work_items[1]["desired_outcome"] == "SCHEDULED"
    assert seed.counterfactual_items[1]["desired_outcome"] == "PUBLISHED"
    assert seed.expected_semantic_state["objective_count"] == 2


def test_counterfactuals_are_explicitly_grounded_in_seed_data() -> None:
    for seed in BUSINESS_SEMANTIC_SEEDS:
        counterfactuals = [item for item in seed.expressions if item.counterfactual]
        assert len(counterfactuals) == 1
        if seed.counterfactual_items:
            assert counterfactuals[0].user_message

