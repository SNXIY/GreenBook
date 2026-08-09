"""Stage 6.9.1 consumer loss assertions and report smoke test."""

from __future__ import annotations

from intent_spec_consumer_loss import analyze_consumer_loss, build_loss_report, consumer_cases


def test_consumer_loss_focus_cases() -> None:
    findings = {case.case_id: analyze_consumer_loss(case) for case in consumer_cases()}

    assert not findings["search-create"].action_loss
    assert findings["search-create"].resource_loss
    assert findings["search-analyze-update"].action_loss
    assert findings["search-analyze-update"].resource_loss
    assert findings["conditional-update-or-create"].action_loss
    assert findings["conditional-update-or-create"].condition_loss
    assert not findings["conditional-update-or-create"].constraint_loss
    assert not findings["hitl-publish"].action_loss
    assert findings["hitl-publish"].resource_loss
    assert not findings["hitl-publish"].constraint_loss


def test_loss_report_contains_planner_guidance() -> None:
    report = build_loss_report()

    assert report["summary"] == {
        "action_loss_cases": 2,
        "resource_loss_cases": 4,
        "condition_loss_cases": 1,
        "constraint_loss_cases": 0,
        "case_count": 4,
    }
    assert "conditional branches: condition type, then_action, else_action" in report[
        "planner_requirements"
    ]["must_preserve"]
