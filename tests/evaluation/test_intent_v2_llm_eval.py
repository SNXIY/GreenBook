"""Phase 6.8.1 Stage C.1 — Audited Intent Understanding Evaluation.

Clean separation:
  A. IntentSpec Metrics (from IntentSpec fields directly)
  B. Legacy Compatibility Metrics (from to_task_intent() output)

Metrics:
  1. Mode Accuracy
  2. Required Action Recall (missing required → penalty, extra → not)
  3. Optional Action Validity
  4. Forbidden Action Violation Rate
  5. Resource Accuracy
  6. Condition Accuracy
  7. Constraint Accuracy
  8. Empty Action Rate
  9. Complex IntentSpec Success
  10. Repair attempts/successes/failures
  11. Legacy TaskIntent Accuracy (goal_category, relation)
  12. IntentSpec → TaskIntent information loss rate
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import time
from dataclasses import dataclass, field

import pytest
from greenbook_assistant_core.task.intent_models import IntentSpec
from greenbook_assistant_core.task.understanding import TaskUnderstanding
from greenbook_evaluation.intent_dataset_v2 import (
    ALL_V2_CASES,
    IntentEvalCase,
    flatten_cases,
)


# ═══════════════════════════════════════════════════════════════════════
# Result models
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class CaseResult:
    case_id: str
    category: str
    description: str
    user_message: str
    route: str = ""
    routing_reason: str = ""
    # IntentSpec fields
    actual_mode: str = ""
    actual_actions: set[str] = field(default_factory=set)
    actual_resources: set[str] = field(default_factory=set)
    actual_conditions: list[dict] = field(default_factory=list)
    actual_constraints: set[str] = field(default_factory=set)
    # Legacy fields
    actual_goal_category: str = ""
    actual_relation: str = ""
    # Debug
    raw_llm_output: str = ""
    validator_issues: list[str] = field(default_factory=list)
    repair_raw_output: str = ""
    repair_parsed: bool = False
    was_repaired: bool = False
    was_fallback: bool = False
    llm_parse_failed: bool = False
    duration_ms: float = 0.0
    # Checks
    intent_spec_passed: bool = False
    legacy_passed: bool = False
    checks: dict[str, bool] = field(default_factory=dict)
    failure_types: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class EvalStats:
    total: int = 0
    l1_count: int = 0
    l2_v2_count: int = 0
    l2_legacy_count: int = 0
    # IntentSpec metrics
    mode_correct: int = 0
    mode_denom: int = 0
    required_action_recall_numer: int = 0
    required_action_recall_denom: int = 0
    optional_action_valid: int = 0
    optional_action_denom: int = 0
    forbidden_violations: int = 0
    forbidden_denom: int = 0
    resource_correct: int = 0
    resource_denom: int = 0
    condition_correct: int = 0
    condition_denom: int = 0
    constraint_correct: int = 0
    constraint_denom: int = 0
    empty_actions: int = 0
    l2_v2_total: int = 0  # denominator for IntentSpec metrics
    # Complex
    complex_total: int = 0
    complex_intent_spec_pass: int = 0
    # Repair
    repair_attempts: int = 0
    repair_successes: int = 0
    repair_failures: int = 0
    fallbacks: int = 0
    llm_parse_failures: int = 0
    # Legacy
    legacy_category_correct: int = 0
    legacy_category_denom: int = 0
    legacy_relation_correct: int = 0
    legacy_relation_denom: int = 0
    # Per-category
    by_category: dict[str, dict] = field(default_factory=dict)
    duration_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════════════
# Evaluation — clean separation of IntentSpec vs Legacy
# ═══════════════════════════════════════════════════════════════════════

def _eval_intent_spec(case: IntentEvalCase, spec: IntentSpec | None) -> tuple[dict, list[str]]:
    """Evaluate IntentSpec fields directly. Returns (checks, failures)."""
    checks: dict[str, bool] = {}
    failures: list[str] = []

    if spec is None:
        return checks, failures

    # Mode
    if case.expected_mode:
        actual = spec.mode.value if spec.mode else ""
        checks["mode"] = (actual == case.expected_mode)
        if not checks["mode"]:
            failures.append("WRONG_MODE")

    # Required actions: use explicit required_actions, or fall back to expected_actions
    effective_required = case.required_actions or case.expected_actions
    if effective_required is not None:
        required = effective_required
        optional = case.optional_actions or set()
        forbidden = case.forbidden_actions or set()
        actual = {a.action.value for a in spec.actions}

        missing = required - actual
        checks["required_actions"] = len(missing) == 0
        if missing:
            failures.append("MISSING_ACTION")

        present_forbidden = forbidden & actual
        checks["forbidden_actions"] = len(present_forbidden) == 0
        if present_forbidden:
            failures.append("FORBIDDEN_ACTION")

        extra = actual - (required | optional)
        checks["optional_actions"] = True
        if extra and case.optional_actions is not None:
            # Only flag EXTRA_ACTION when optional was explicitly defined
            failures.append("EXTRA_ACTION")

    # Resources
    if case.expected_resources:
        expected = case.expected_resources
        actual = {f"{a.action.value}:{a.resource.value}"
                  for a in spec.actions if a.resource}
        checks["resources"] = expected.issubset(actual)
        if not checks["resources"]:
            failures.append("WRONG_RESOURCE")

    # Conditions
    if case.expected_conditions:
        has_cond = len(spec.conditions) > 0
        checks["conditions"] = has_cond
        if not has_cond:
            failures.append("MISSING_CONDITION")
        if case.expected_condition_types and has_cond:
            actual_types = {c.type.value for c in spec.conditions}
            checks["condition_types"] = (actual_types == case.expected_condition_types)
            if not checks["condition_types"]:
                failures.append("WRONG_CONDITION")

    # Constraints
    if case.expected_constraints:
        actual = {c.type.value for c in spec.constraints}
        checks["constraints"] = case.expected_constraints.issubset(actual)
        if not checks["constraints"]:
            failures.append("MISSING_CONSTRAINT")

    return checks, failures


def _eval_legacy(case: IntentEvalCase, task_intent) -> tuple[dict, list[str]]:
    """Evaluate legacy TaskIntent fields."""
    checks: dict[str, bool] = {}
    failures: list[str] = []

    if case.expected_goal_category:
        actual = getattr(task_intent, "goal_category", "")
        checks["goal_category"] = (actual == case.expected_goal_category)
        if not checks["goal_category"]:
            failures.append("WRONG_LEGACY_MAPPING")

    if case.expected_relation:
        actual = getattr(task_intent, "relation", "")
        checks["relation"] = (actual == case.expected_relation)
        if not checks["relation"]:
            failures.append("WRONG_LEGACY_MAPPING")

    return checks, failures


# ═══════════════════════════════════════════════════════════════════════
# Main evaluation runner
# ═══════════════════════════════════════════════════════════════════════

async def run_llm_eval() -> tuple[list[CaseResult], EvalStats]:
    from openai import AsyncOpenAI

    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.getenv("LLM_MODEL", "deepseek-v4-pro")

    print(f"\n{'='*70}")
    print(f"Phase 6.8.1 Stage C.1 — Audited Intent Evaluation")
    print(f"{'='*70}")
    print(f"  Model: {model}")
    print(f"  API Key: {'***' + api_key[-4:] if api_key else 'NOT SET'}")

    if not api_key:
        print("\n  ERROR: DEEPSEEK_API_KEY not set.")
        return [], EvalStats()

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    tu = TaskUnderstanding(llm=client, model=model)

    all_cases = flatten_cases()
    results: list[CaseResult] = []
    stats = EvalStats(total=len(all_cases))
    t0 = time.monotonic()

    for i, case in enumerate(all_cases):
        print(f"\n  [{i+1}/{len(all_cases)}] {case.case_id}: {case.description}", end=" ", flush=True)
        t_case = time.monotonic()

        route = "L1"
        intent_spec: IntentSpec | None = None
        raw_llm = ""
        repair_raw = ""
        repair_parsed_flag = False
        was_repaired = False
        was_fallback = False
        llm_parse_failed = False

        l2_triggered = tu._needs_l2_v2(case.user_message)
        routing_reason = getattr(tu, '_last_routing_reason', "")

        try:
            task_intent = await tu.understand(case.user_message)
            raw_spec = getattr(task_intent, "intent_spec", None)
            if raw_spec and isinstance(raw_spec, dict):
                raw_llm = _json.dumps(raw_spec, ensure_ascii=False, indent=2)[:500]

            if raw_spec and isinstance(raw_spec, dict):
                try:
                    intent_spec = IntentSpec.model_validate(raw_spec)
                    route = "L2-v2"
                except Exception:
                    llm_parse_failed = True
                    route = "L2-v2" if l2_triggered else "L1"
            elif getattr(task_intent, "source", "") == "L2":
                if l2_triggered:
                    route = "L2-v2"
                    was_fallback = True
                else:
                    route = "L2-legacy"
            else:
                route = "L1"
                if l2_triggered:
                    was_fallback = True

        except Exception as e:
            from greenbook_assistant_core.task.models import TaskIntent as TI
            task_intent = TI(relation="DIRECT", goal_category="QUERY_INFO",
                            goal=case.user_message[:200], source="L1", confidence=0.5)
            route = "L1"
            was_fallback = True
            llm_parse_failed = True

        duration = (time.monotonic() - t_case) * 1000

        # Read repair stats
        repair_attempts = tu._repair_stats.get("attempts", 0)
        repair_successes = tu._repair_stats.get("successes", 0)
        repair_failures = tu._repair_stats.get("failures", 0)
        fallbacks = tu._repair_stats.get("fallbacks", 0)

        # Evaluate IntentSpec (only when produced by v2)
        ispec_checks, ispec_failures = _eval_intent_spec(case, intent_spec)
        ispec_passed = len(ispec_checks) > 0 and all(ispec_checks.values())

        # Evaluate Legacy
        legacy_checks, legacy_failures = _eval_legacy(case, task_intent)
        legacy_passed = len(legacy_checks) > 0 and all(legacy_checks.values())

        # Build result
        r = CaseResult(
            case_id=case.case_id,
            category=case.category,
            description=case.description,
            user_message=case.user_message,
            route=route,
            routing_reason=routing_reason,
            actual_mode=intent_spec.mode.value if intent_spec and intent_spec.mode else "",
            actual_actions={a.action.value for a in intent_spec.actions} if intent_spec else set(),
            actual_resources={f"{a.action.value}:{a.resource.value}"
                              for a in intent_spec.actions if a.resource} if intent_spec else set(),
            actual_conditions=[{"type": c.type.value, "then": c.then_action.value if c.then_action else None,
                                "else": c.else_action.value if c.else_action else None}
                               for c in intent_spec.conditions] if intent_spec else [],
            actual_constraints={c.type.value for c in intent_spec.constraints} if intent_spec else set(),
            actual_goal_category=getattr(task_intent, "goal_category", ""),
            actual_relation=getattr(task_intent, "relation", ""),
            raw_llm_output=raw_llm,
            was_repaired=was_repaired,
            was_fallback=was_fallback,
            llm_parse_failed=llm_parse_failed,
            duration_ms=duration,
            intent_spec_passed=ispec_passed,
            legacy_passed=legacy_passed,
            checks={**ispec_checks, **legacy_checks},
            failure_types=ispec_failures + legacy_failures,
        )

        # Add routing-specific failure
        if case.should_trigger_l2 and route == "L1":
            r.failure_types.append("WRONG_ROUTING")

        results.append(r)

        status_parts = []
        if intent_spec is not None:
            status_parts.append(f"ISpec={'PASS' if ispec_passed else 'FAIL'}")
        status_parts.append(f"Legacy={'PASS' if legacy_passed else 'FAIL'}")
        print(f"→ {' '.join(status_parts)} ({route}, {duration:.0f}ms)")

    stats.duration_ms = (time.monotonic() - t0) * 1000

    # ── Compute stats ──
    stats.l1_count = sum(1 for r in results if r.route == "L1")
    stats.l2_v2_count = sum(1 for r in results if r.route == "L2-v2")
    stats.l2_legacy_count = sum(1 for r in results if r.route == "L2-legacy")
    stats.l2_v2_total = stats.l2_v2_count
    stats.repair_attempts = repair_attempts
    stats.repair_successes = repair_successes
    stats.repair_failures = repair_failures
    stats.fallbacks = fallbacks
    stats.llm_parse_failures = sum(1 for r in results if r.llm_parse_failed)
    stats.empty_actions = sum(1 for r in results if r.route == "L2-v2" and not r.actual_actions)

    # IntentSpec metrics (only L2-v2 cases where spec was produced)
    for r in results:
        if r.route != "L2-v2":
            continue
        case = next((c for c in all_cases if c.case_id == r.case_id), None)
        if case is None:
            continue

        if case.expected_mode:
            stats.mode_denom += 1
            if r.checks.get("mode", False):
                stats.mode_correct += 1

        # If case has required_actions or expected_actions, evaluate
        effective_required = case.required_actions or case.expected_actions
        if effective_required is not None:
            stats.required_action_recall_denom += 1
            required_set = effective_required
            if required_set.issubset(r.actual_actions):
                stats.required_action_recall_numer += 1

        if case.optional_actions:
            stats.optional_action_denom += 1
            if r.checks.get("optional_actions", True):
                stats.optional_action_valid += 1

        if case.forbidden_actions is not None:
            stats.forbidden_denom += 1
            if not r.checks.get("forbidden_actions", True):
                stats.forbidden_violations += 1

        if case.expected_resources:
            stats.resource_denom += 1
            if r.checks.get("resources", False):
                stats.resource_correct += 1

        if case.expected_conditions:
            stats.condition_denom += 1
            if r.checks.get("conditions", False):
                stats.condition_correct += 1

        if case.expected_constraints:
            stats.constraint_denom += 1
            if r.checks.get("constraints", False):
                stats.constraint_correct += 1

        if case.category == "COMPLEX":
            stats.complex_total += 1
            if r.intent_spec_passed:
                stats.complex_intent_spec_pass += 1

    # Legacy metrics (all cases)
    for r in results:
        case = next((c for c in all_cases if c.case_id == r.case_id), None)
        if case is None:
            continue
        if case.expected_goal_category:
            stats.legacy_category_denom += 1
            if r.checks.get("goal_category", False):
                stats.legacy_category_correct += 1
        if case.expected_relation:
            stats.legacy_relation_denom += 1
            if r.checks.get("relation", False):
                stats.legacy_relation_correct += 1

    # Per-category
    for r in results:
        cat = r.category
        if cat not in stats.by_category:
            stats.by_category[cat] = {"total": 0, "ispec_pass": 0, "legacy_pass": 0}
        stats.by_category[cat]["total"] += 1
        if r.intent_spec_passed:
            stats.by_category[cat]["ispec_pass"] += 1
        if r.legacy_passed:
            stats.by_category[cat]["legacy_pass"] += 1

    return results, stats


def _pct(numer: int, denom: int) -> str:
    if denom == 0:
        return "N/A"
    return f"{numer}/{denom} ({numer/denom:.0%})"


def _print_report(results: list[CaseResult], stats: EvalStats) -> None:
    print(f"\n{'='*70}")
    print(f"EVALUATION REPORT")
    print(f"{'='*70}")

    print(f"\n── Routing ──")
    print(f"  Total:       {stats.total}")
    print(f"  L1:          {stats.l1_count}")
    print(f"  L2-v2:       {stats.l2_v2_count}")
    print(f"  L2-legacy:   {stats.l2_legacy_count}")

    print(f"\n── IntentSpec Metrics (L2-v2 only, n={stats.l2_v2_total}) ──")
    print(f"  Mode Accuracy:          {_pct(stats.mode_correct, stats.mode_denom)}")
    print(f"  Required Action Recall: {_pct(stats.required_action_recall_numer, stats.required_action_recall_denom)}")
    print(f"  Optional Action Valid:  {_pct(stats.optional_action_valid, stats.optional_action_denom)}")
    print(f"  Forbidden Violations:   {stats.forbidden_violations}/{stats.forbidden_denom}")
    print(f"  Resource Accuracy:      {_pct(stats.resource_correct, stats.resource_denom)}")
    print(f"  Condition Accuracy:     {_pct(stats.condition_correct, stats.condition_denom)}")
    print(f"  Constraint Accuracy:    {_pct(stats.constraint_correct, stats.constraint_denom)}")
    print(f"  Empty Actions:          {stats.empty_actions}/{stats.l2_v2_total}")

    if stats.complex_total > 0:
        print(f"  Complex ISpec Success:  {_pct(stats.complex_intent_spec_pass, stats.complex_total)}")

    print(f"\n── Repair ──")
    print(f"  Attempts:  {stats.repair_attempts}")
    print(f"  Successes: {stats.repair_successes}")
    print(f"  Failures:  {stats.repair_failures}")
    print(f"  Fallbacks: {stats.fallbacks}")
    print(f"  LLM Parse Failures: {stats.llm_parse_failures}")

    print(f"\n── Legacy Compatibility Metrics ──")
    print(f"  goal_category: {_pct(stats.legacy_category_correct, stats.legacy_category_denom)}")
    print(f"  relation:      {_pct(stats.legacy_relation_correct, stats.legacy_relation_denom)}")

    # Information loss: cases where IntentSpec passed but Legacy failed
    ispec_ok_legacy_fail = sum(
        1 for r in results if r.intent_spec_passed and not r.legacy_passed
    )
    ispec_total = sum(1 for r in results if r.intent_spec_passed)
    info_loss = ispec_ok_legacy_fail / ispec_total if ispec_total > 0 else 0
    print(f"  Info Loss Rate:  {ispec_ok_legacy_fail}/{ispec_total} ({info_loss:.0%})")

    print(f"\n── Per-Category ──")
    for cat_name in ["SIMPLE", "MODIFY", "COMPOSITE", "CONDITIONAL", "HITL", "PARAPHRASE", "COMPLEX"]:
        cat = stats.by_category.get(cat_name, {})
        if not cat:
            continue
        t = cat["total"]
        ip = cat["ispec_pass"]
        lp = cat["legacy_pass"]
        print(f"  {cat_name:15s}: ISpec={_pct(ip, t)}  Legacy={_pct(lp, t)}")

    total_ispec = sum(1 for r in results if r.intent_spec_passed)
    total_legacy = sum(1 for r in results if r.legacy_passed)
    print(f"  {'OVERALL':15s}: ISpec={_pct(total_ispec, stats.total)}  Legacy={_pct(total_legacy, stats.total)}")

    print(f"\n── Timing ──")
    print(f"  Total: {stats.duration_ms/1000:.1f}s")
    avg_ms = sum(r.duration_ms for r in results) / len(results) if results else 0
    print(f"  Avg:   {avg_ms:.0f}ms")

    # Badcases
    failed_ispec = [r for r in results if not r.intent_spec_passed and r.route == "L2-v2"]
    failed_legacy = [r for r in results if not r.legacy_passed]

    if failed_ispec:
        print(f"\n── IntentSpec Failures ({len(failed_ispec)}) ──")
        for r in failed_ispec:
            print(f"\n  [{r.case_id}] {r.description}")
            print(f"  Route: {r.route} | Empty actions: {not r.actual_actions}")
            print(f"  Mode: {r.actual_mode} | Actions: {r.actual_actions}")
            print(f"  Resources: {r.actual_resources} | Constraints: {r.actual_constraints}")
            print(f"  Conditions: {r.actual_conditions}")
            print(f"  Failures: {r.failure_types}")

    if failed_legacy:
        print(f"\n── Legacy Mapping Failures ({len(failed_legacy)}) ──")
        for r in failed_legacy[:10]:
            ispec_ok = "ISpec=OK" if r.intent_spec_passed else "ISpec=FAIL"
            print(f"  [{r.case_id}] {r.description} ({ispec_ok})")
            print(f"    Actions: {r.actual_actions} → goal_category={r.actual_goal_category}, relation={r.actual_relation}")
            print(f"    Failures: {r.failure_types}")


@pytest.mark.asyncio
async def test_llm_intent_evaluation() -> None:
    results, stats = await run_llm_eval()
    if not results:
        pytest.skip("No LLM API key configured")
    _print_report(results, stats)
    assert True
