"""Phase 6.8.0: Baseline evaluation of current TaskUnderstanding.

Runs ALL_INTENT_CASES against current TU and reports accuracy.
DOES NOT fix anything — just measures the current state.
"""

from __future__ import annotations

import asyncio

import pytest
from greenbook_assistant_core.task.understanding import TaskUnderstanding
from greenbook_evaluation.intent_dataset import ALL_INTENT_CASES


def _eval_tu(intent: object, expected: dict) -> dict[str, bool]:
    """Check one intent against expectations."""
    results: dict[str, bool] = {}

    if "goal_category" in expected:
        results["goal_category"] = (
            getattr(intent, "goal_category", "") == expected["goal_category"]
        )
    if "relation" in expected:
        results["relation"] = (
            getattr(intent, "relation", "") == expected["relation"]
        )
    if "operation_mode" in expected:
        # Current TU doesn't have operation_mode — always "SIMPLE" equivalent
        results["operation_mode"] = True  # placeholder for now

    return results


async def _run_category(name: str, cases: list) -> dict:
    """Run one category and return stats."""
    tu = TaskUnderstanding()
    passed = 0
    total = len(cases)
    failures: list[str] = []

    for case in cases:
        intent = await tu.understand(case.user_message)
        checks = _eval_tu(intent, case.expected_intent or {})
        ok = all(checks.values())
        if ok:
            passed += 1
        else:
            failed_checks = [k for k, v in checks.items() if not v]
            actual_gc = getattr(intent, "goal_category", "")
            actual_rel = getattr(intent, "relation", "")
            failures.append(
                f"  {case.case_id}: {case.description}\n"
                f"    expected: {case.expected_intent}\n"
                f"    actual: gc={actual_gc}, rel={actual_rel}\n"
                f"    failed: {failed_checks}"
            )

    return {
        "category": name,
        "total": total,
        "passed": passed,
        "accuracy": passed / total if total > 0 else 0,
        "failures": failures,
    }


# ═══════════════════════════════════════════════════════════════════
# Test: print baseline accuracy for all categories
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_baseline_accuracy_report() -> None:
    """Run ALL categories and print accuracy. Always passes."""
    results = []
    total_pass = 0
    total_cases = 0

    for name, cases in ALL_INTENT_CASES.items():
        r = await _run_category(name, cases)
        results.append(r)
        total_pass += r["passed"]
        total_cases += r["total"]

    print("\n" + "=" * 60)
    print("TaskUnderstanding 2.0 — Baseline Accuracy")
    print("=" * 60)
    for r in results:
        bar = "#" * int(r["accuracy"] * 20)
        print(f"  {r['category']:15s}: {r['passed']:2d}/{r['total']:2d} "
              f"({r['accuracy']:.0%}) {bar}")
    print(f"  {'OVERALL':15s}: {total_pass:2d}/{total_cases:2d} "
          f"({total_pass/total_cases:.0%})")
    print()

    # Print failures
    for r in results:
        if r["failures"]:
            print(f"\n--- {r['category'].upper()} failures ---")
            for f in r["failures"]:
                print(f)

    # Always passes — this is a measurement, not a gate
    assert True


# ═══════════════════════════════════════════════════════════════════
# Individual category tests (informational only)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_simple_baseline() -> None:
    r = await _run_category("simple", ALL_INTENT_CASES["simple"])
    assert True  # measurement only


@pytest.mark.asyncio
async def test_modify_baseline() -> None:
    r = await _run_category("modify", ALL_INTENT_CASES["modify"])
    assert True


@pytest.mark.asyncio
async def test_composite_baseline() -> None:
    r = await _run_category("composite", ALL_INTENT_CASES["composite"])
    print(f"\nComposite accuracy: {r['accuracy']:.0%}")
    for f in r["failures"]:
        print(f)
    assert True


@pytest.mark.asyncio
async def test_conditional_baseline() -> None:
    r = await _run_category("conditional", ALL_INTENT_CASES["conditional"])
    print(f"\nConditional accuracy: {r['accuracy']:.0%}")
    for f in r["failures"]:
        print(f)
    assert True


@pytest.mark.asyncio
async def test_hitl_baseline() -> None:
    r = await _run_category("hitl", ALL_INTENT_CASES["hitl"])
    print(f"\nHITL accuracy: {r['accuracy']:.0%}")
    for f in r["failures"]:
        print(f)
    assert True
