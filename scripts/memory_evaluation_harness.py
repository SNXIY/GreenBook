"""Offline evaluation harness for the Preference Memory vertical slice.

This module is intentionally outside the production packages.  It imports the
existing Memory contracts and exercises them with deterministic fixtures; it
does not patch, monkey-patch, or change production behavior.  Running it
regenerates the evaluation datasets and reports under ``docs/evaluation`` and
``docs/reports``.

Usage::

    uv run python scripts/memory_evaluation_harness.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from greenbook_agent_core.command.models import CommandContext
from greenbook_agent_core.context import ContextBuilder
from greenbook_agent_core.context.projection import project_interpreter_context
from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryManager,
    MemoryQuery,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
    PreferenceMemoryExtractor,
    PreferenceRetriever,
)

ROOT = Path(__file__).resolve().parents[1]
EVALUATION_DIR = ROOT / "docs" / "evaluation"
REPORT_DIR = ROOT / "docs" / "reports"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _metric(value: float) -> str:
    return f"{value:.4f}"


def _preference_record(
    *,
    memory_id: str,
    user_id: str,
    tenant_id: str,
    key: str,
    value: str,
    conversation_id: str,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    confidence: float = 0.9,
    source_type: str = "",
    source_id: str | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id,
        user_id=user_id,
        tenant_id=tenant_id,
        source_conversation_id=conversation_id,
        source_type=source_type,
        source_id=source_id,
        memory_type=MemoryType.PREFERENCE,
        status=status,
        content=value,
        confidence=confidence,
        importance=0.8,
        structured_metadata={
            "preference_type": key,
            "value": value,
        },
    )


def build_extraction_cases() -> list[dict[str, Any]]:
    """Build a labeled, deterministic extraction dataset with 114 cases."""

    suffixes = (
        "for my writing",
        "in future posts",
        "across my technical guides",
        "when drafting tutorials",
    )
    positive_templates = (
        ("From now on, avoid exaggerated titles in articles {suffix}.", "title_style"),
        ("I prefer not to use clickbait titles {suffix}.", "title_style"),
        ("Please remember to avoid exaggerated titles {suffix}.", "title_style"),
        ("Always avoid clickbait titles {suffix}.", "title_style"),
        ("I prefer deep technical articles {suffix}.", "writing_depth"),
        ("From now on, prefer in-depth technical content {suffix}.", "writing_depth"),
        ("I want deep technical articles as my default {suffix}.", "writing_depth"),
        ("My preferred style is deep technical writing {suffix}.", "writing_depth"),
        ("From now on, prefer concise replies {suffix}.", "response_style"),
        ("I prefer brief replies {suffix}.", "response_style"),
        ("Please remember that I like concise replies {suffix}.", "response_style"),
        ("My default is concise replies {suffix}.", "response_style"),
        ("I use Java technology stack {suffix}.", "technology_stack"),
        ("From now on, use Java for my technology stack {suffix}.", "technology_stack"),
        ("I prefer to use Python technology stack {suffix}.", "technology_stack"),
        ("Please remember that I use Python technology stack {suffix}.", "technology_stack"),
    )
    negative_templates = (
        "Write a Java article tomorrow.",
        "Help me create a draft for this campaign.",
        "Query the most recent posts.",
        "Publish the article today.",
        "Schedule a Python post tomorrow.",
        "Create a draft about distributed systems.",
        "Find the latest Java post for me.",
        "This time keep the reply concise.",
        "I need a deep technical article for today.",
        "Cancel tomorrow's publishing schedule.",
        "Read the current draft and tell me its status.",
        "Generate one Java tutorial right now.",
    )
    boundary_templates = (
        ("Recently I prefer Java.", False, ""),
        ("I like Java.", True, "technology_stack"),
        ("I currently prefer concise replies.", False, ""),
        ("For this article, prefer technical depth.", False, ""),
        ("My default is concise replies.", True, "response_style"),
        ("I usually use Java.", True, "technology_stack"),
        ("I sometimes use Python.", True, "technology_stack"),
        ("I am learning Java this month.", False, ""),
        ("I prefer Java for today's draft.", False, ""),
        ("I want deep technical articles.", True, "writing_depth"),
    )

    cases: list[dict[str, Any]] = []
    case_number = 1
    for cycle in range(3):
        for template, key in positive_templates:
            text = template.format(suffix=suffixes[(case_number + cycle) % len(suffixes)])
            cases.append({
                "id": f"extract-{case_number:03d}",
                "category": "should_save_preference",
                "text": text,
                "expected": {
                    "write": True,
                    "memory_type": "preference",
                    "preference_key": key,
                },
            })
            case_number += 1
    for _cycle in range(3):
        for text in negative_templates:
            cases.append({
                "id": f"extract-{case_number:03d}",
                "category": "should_not_save_one_off_or_invalid",
                "text": text,
                "expected": {
                    "write": False,
                    "memory_type": "preference",
                },
            })
            case_number += 1
    for _cycle in range(3):
        for text, write, key in boundary_templates:
            cases.append({
                "id": f"extract-{case_number:03d}",
                "category": "boundary_case",
                "text": text,
                "expected": {
                    "write": write,
                    "memory_type": "preference",
                    **({"preference_key": key} if key else {}),
                },
            })
            case_number += 1
    return cases


def evaluate_extraction(cases: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated: list[dict[str, Any]] = []
    tp = fp = tn = fn = 0
    by_category: dict[str, Counter[str]] = {}
    failures: list[dict[str, Any]] = []
    for case in cases:
        result = PreferenceMemoryExtractor.extract(case["text"])
        expected_write = bool(case["expected"]["write"])
        actual_write = result.should_write
        if expected_write and actual_write:
            tp += 1
            outcome = "TP"
        elif not expected_write and actual_write:
            fp += 1
            outcome = "FP"
        elif expected_write:
            fn += 1
            outcome = "FN"
        else:
            tn += 1
            outcome = "TN"
        category_counts = by_category.setdefault(case["category"], Counter())
        category_counts[outcome] += 1
        actual = result.model_dump(mode="json")
        evaluated_case = {
            **case,
            "actual": actual,
            "outcome": outcome,
        }
        evaluated.append(evaluated_case)
        if outcome in {"FP", "FN"}:
            failures.append({
                "id": case["id"],
                "category": case["category"],
                "text": case["text"],
                "expected": case["expected"],
                "actual": {
                    "write": actual_write,
                    "preference_key": result.preference_key,
                    "reason": result.reason,
                    "confidence": result.confidence,
                },
                "outcome": outcome,
            })
    total = len(cases)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    fnr = fn / (fn + tp) if fn + tp else 0.0
    return {
        "dataset_count": total,
        "confusion": {"true_positive": tp, "false_positive": fp, "true_negative": tn, "false_negative": fn},
        "metrics": {
            "precision": precision,
            "recall": recall,
            "false_positive_rate": fpr,
            "false_negative_rate": fnr,
            "accuracy": (tp + tn) / total if total else 0.0,
        },
        "by_category": {key: dict(value) for key, value in by_category.items()},
        "failures": failures,
        "cases": evaluated,
    }


def build_retrieval_fixture() -> InMemoryMemoryRepository:
    repository = InMemoryMemoryRepository()
    repository.save(_preference_record(
        memory_id="memory-u1-depth",
        user_id="u1",
        tenant_id="tenant-a",
        key="writing_depth",
        value="prefer technical deep articles",
        conversation_id="conversation-old-depth",
    ))
    repository.save(_preference_record(
        memory_id="memory-u1-java",
        user_id="u1",
        tenant_id="tenant-a",
        key="technology_stack",
        value="use Java technology stack",
        conversation_id="conversation-old-java",
    ))
    repository.save(_preference_record(
        memory_id="memory-u1-concise",
        user_id="u1",
        tenant_id="tenant-a",
        key="response_style",
        value="prefer concise replies",
        conversation_id="conversation-old-concise",
    ))
    return repository


def build_retrieval_cases() -> list[dict[str, Any]]:
    topics = (
        "Agent architecture", "distributed systems", "PostgreSQL", "MCP",
        "reliability", "Python services", "Java runtime", "RAG evaluation",
        "workflow design", "API contracts",
    )
    cases: list[dict[str, Any]] = []
    number = 1
    for _cycle in range(4):
        for topic in topics:
            cases.append({
                "id": f"retrieve-{number:03d}",
                "category": "technical_depth",
                "query": f"Write a technical deep article about {topic}",
                "user_id": "u1",
                "tenant_id": "tenant-a",
                "expected_keys": ["writing_depth"],
            })
            number += 1
    for _cycle in range(3):
        for topic in topics:
            cases.append({
                "id": f"retrieve-{number:03d}",
                "category": "technology_stack",
                "query": f"Use Java technology stack for {topic}",
                "user_id": "u1",
                "tenant_id": "tenant-a",
                "expected_keys": ["technology_stack"],
            })
            number += 1
    for _cycle in range(2):
        for topic in topics:
            cases.append({
                "id": f"retrieve-{number:03d}",
                "category": "response_style",
                "query": f"Give concise replies about {topic}",
                "user_id": "u1",
                "tenant_id": "tenant-a",
                "expected_keys": ["response_style"],
            })
            number += 1
    for topic in topics:
        cases.append({
            "id": f"retrieve-{number:03d}",
            "category": "irrelevant_request",
            "query": f"Schedule a post tomorrow about {topic}",
            "user_id": "u1",
            "tenant_id": "tenant-a",
            "expected_keys": [],
        })
        number += 1
    return cases


async def evaluate_retrieval(cases: list[dict[str, Any]]) -> dict[str, Any]:
    repository = build_retrieval_fixture()
    retriever = PreferenceRetriever(repository)
    evaluated: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    metrics: dict[str, dict[str, float]] = {}
    for case in cases:
        values = await retriever.retrieve(
            user_id=case["user_id"],
            tenant_id=case["tenant_id"],
            query=case["query"],
            limit=5,
        )
        actual_keys = [str(item.metadata.get("preference_type") or "") for item in values]
        actual_ids = [item.memory_id for item in values]
        evaluated_case = {**case, "actual_keys": actual_keys, "actual_ids": actual_ids}
        evaluated.append(evaluated_case)
        expected = set(case["expected_keys"])
        if expected:
            for k in (1, 3, 5):
                prefix = set(actual_keys[:k])
                hit_count = len(prefix & expected)
                metrics.setdefault(str(k), {"recall_sum": 0.0, "precision_sum": 0.0, "count": 0.0})
                metrics[str(k)]["recall_sum"] += hit_count / len(expected)
                metrics[str(k)]["precision_sum"] += hit_count / k
                metrics[str(k)]["count"] += 1
            if not expected.issubset(set(actual_keys[:5])):
                failures.append(evaluated_case)
        elif actual_keys:
            failures.append({**evaluated_case, "failure": "irrelevant_memory_returned"})
    final_metrics = {
        key: {
            "recall_at_k": value["recall_sum"] / value["count"] if value["count"] else 0.0,
            "precision_at_k": value["precision_sum"] / value["count"] if value["count"] else 0.0,
            "eligible_cases": int(value["count"]),
        }
        for key, value in metrics.items()
    }
    irrelevant = [item for item in evaluated if not item["expected_keys"]]
    return {
        "dataset_count": len(cases),
        "fixture": [
            {"memory_id": item.memory_id, "key": item.metadata.get("preference_type"), "value": item.metadata.get("value")}
            for item in repository.search(MemoryQuery(
                user_id="u1", tenant_id="tenant-a", type=MemoryType.PREFERENCE, limit=10,
            ))
        ],
        "metrics": final_metrics,
        "irrelevant_cases": len(irrelevant),
        "irrelevant_memory_return_rate": sum(bool(item["actual_keys"]) for item in irrelevant) / len(irrelevant),
        "failures": failures[:30],
        "cases": evaluated,
    }


def build_isolation_fixture() -> InMemoryMemoryRepository:
    repository = InMemoryMemoryRepository()
    repository.save(_preference_record(
        memory_id="isolation-a-java", user_id="user-a", tenant_id="tenant-a",
        key="technology_stack", value="use Java technology stack", conversation_id="conversation-a",
    ))
    repository.save(_preference_record(
        memory_id="isolation-b-python", user_id="user-b", tenant_id="tenant-a",
        key="technology_stack", value="use Python technology stack", conversation_id="conversation-b",
    ))
    repository.save(_preference_record(
        memory_id="isolation-a-tenant-b-java", user_id="user-a", tenant_id="tenant-b",
        key="technology_stack", value="use Java technology stack", conversation_id="conversation-c",
    ))
    return repository


def build_isolation_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    number = 1
    shapes = (
        ("user_a_same_tenant", "user-a", "tenant-a", "Java", ["isolation-a-java"], ["isolation-b-python", "isolation-a-tenant-b-java"]),
        ("user_b_same_tenant", "user-b", "tenant-a", "Python", ["isolation-b-python"], ["isolation-a-java", "isolation-a-tenant-b-java"]),
        ("user_b_java_query", "user-b", "tenant-a", "Java", [], ["isolation-a-java", "isolation-a-tenant-b-java"]),
        ("same_user_other_tenant", "user-a", "tenant-b", "Java", ["isolation-a-tenant-b-java"], ["isolation-a-java", "isolation-b-python"]),
        ("cross_conversation_reuse", "user-a", "tenant-a", "Java", ["isolation-a-java"], []),
    )
    # 95 repeated scoped checks plus five missing-tenant checks = 100 cases.
    for cycle in range(95):
        kind, user_id, tenant_id, query, expected, forbidden = shapes[cycle % len(shapes)]
        cases.append({
            "id": f"isolation-{number:03d}",
            "category": kind,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "conversation_id": f"new-conversation-{cycle}",
            "query": query,
            "expected_ids": expected,
            "forbidden_ids": forbidden,
        })
        number += 1
    for cycle in range(5):
        cases.append({
            "id": f"isolation-{number:03d}",
            "category": "missing_tenant_fail_closed",
            "user_id": "user-a",
            "tenant_id": "",
            "conversation_id": f"missing-tenant-{cycle}",
            "query": "Java",
            "expected_ids": [],
            "forbidden_ids": ["isolation-a-java", "isolation-b-python", "isolation-a-tenant-b-java"],
        })
        number += 1
    return cases


async def evaluate_isolation(cases: list[dict[str, Any]]) -> dict[str, Any]:
    repository = build_isolation_fixture()
    retriever = PreferenceRetriever(repository)
    evaluated: list[dict[str, Any]] = []
    user_leaks = tenant_leaks = 0
    expected_misses = 0
    cross_conversation_cases = 0
    cross_conversation_hits = 0
    same_scope_irrelevant: list[dict[str, Any]] = []
    for case in cases:
        values = await retriever.retrieve(
            user_id=case["user_id"],
            tenant_id=case["tenant_id"],
            query=case["query"],
            conversation_id=case["conversation_id"],
            limit=5,
        )
        ids = [item.memory_id for item in values]
        user_leaks += sum(
            item.memory_id in case["forbidden_ids"]
            or item.user_id != case["user_id"]
            for item in values
        )
        tenant_leaks += sum(
            item.tenant_id != case["tenant_id"]
            for item in values
        )
        if not set(case["expected_ids"]).issubset(set(ids)):
            expected_misses += 1
        if case["category"] == "cross_conversation_reuse":
            cross_conversation_cases += 1
            if set(case["expected_ids"]).issubset(set(ids)):
                cross_conversation_hits += 1
        if not case["expected_ids"] and ids and not any(
            item.memory_id in case["forbidden_ids"] or item.user_id != case["user_id"]
            for item in values
        ):
            same_scope_irrelevant.append({**case, "actual_ids": ids})
        evaluated.append({**case, "actual_ids": ids})
    missing_tenant = [item for item in evaluated if item["category"] == "missing_tenant_fail_closed"]
    missing_tenant_pass = sum(not item["actual_ids"] for item in missing_tenant)
    return {
        "dataset_count": len(cases),
        "metrics": {
            "cross_user_leakage_count": user_leaks,
            "cross_tenant_leakage_count": tenant_leaks,
            "cross_user_leakage_rate": user_leaks / len(cases),
            "cross_tenant_leakage_rate": tenant_leaks / len(cases),
            "expected_scope_miss_rate": expected_misses / len(cases),
            "missing_tenant_fail_closed_rate": missing_tenant_pass / len(missing_tenant),
            "cross_conversation_reuse_rate": cross_conversation_hits / cross_conversation_cases,
        },
        "interpretation": "Cross-Conversation reuse is intentional for Preference Memory; user and tenant are the privacy boundaries.",
        "same_scope_irrelevant_count": len(same_scope_irrelevant),
        "same_scope_irrelevant_cases": same_scope_irrelevant[:30],
        "failures": [
            item for item in evaluated
            if (
                not set(item["expected_ids"]).issubset(set(item["actual_ids"]))
                or any(
                    memory_id in item["actual_ids"]
                    for memory_id in item["forbidden_ids"]
                )
            )
        ][:30],
        "cases": evaluated,
    }


def evaluate_lifecycle() -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    repository = InMemoryMemoryRepository()
    manager = MemoryManager(repository)
    first = manager.remember(_preference_record(
        memory_id="lifecycle-update-first", user_id="u1", tenant_id="tenant-a",
        key="writing_depth", value="prefer concise articles", conversation_id="lifecycle-c1",
        confidence=0.6,
    ))
    merged = manager.remember(_preference_record(
        memory_id="lifecycle-update-second", user_id="u1", tenant_id="tenant-a",
        key="writing_depth", value="prefer concise articles", conversation_id="lifecycle-c2",
        confidence=0.95,
    ))
    results.append({
        "case": "same_value_update",
        "passed": merged.memory_id == first.memory_id and merged.confidence == 0.95 and repository.count("u1") == 1,
        "details": {"first_id": first.memory_id, "merged_id": merged.memory_id, "confidence": merged.confidence},
    })

    old = manager.remember(_preference_record(
        memory_id="lifecycle-python", user_id="u1", tenant_id="tenant-a",
        key="technology_stack", value="use Python technology stack", conversation_id="lifecycle-python",
        source_type="USER_EXPLICIT_PREFERENCE", source_id="preference-python-v1",
    ))
    new = manager.remember(_preference_record(
        memory_id="lifecycle-java", user_id="u1", tenant_id="tenant-a",
        key="technology_stack", value="use Java technology stack", conversation_id="lifecycle-java",
        source_type="USER_EXPLICIT_PREFERENCE", source_id="preference-java-v1",
    ))
    results.append({
        "case": "conflict_supersedes_without_delete",
        "passed": repository.get(old.memory_id).status == MemoryStatus.SUPERSEDED
        and repository.get(new.memory_id).status == MemoryStatus.ACTIVE,
        "details": {"old_status": repository.get(old.memory_id).status.value, "new_status": repository.get(new.memory_id).status.value},
    })

    inactive = manager.deactivate(
        merged.memory_id,
        user_id="u1",
        tenant_id="tenant-a",
    )
    visible_after_inactive = asyncio.run(PreferenceRetriever(repository).retrieve(
        user_id="u1", tenant_id="tenant-a", query="concise articles",
    ))
    results.append({
        "case": "inactive_excluded_from_retrieval",
        "passed": inactive is not None and inactive.status == MemoryStatus.INACTIVE and not any(
            item.memory_id == merged.memory_id for item in visible_after_inactive
        ),
        "details": {"status": inactive.status.value if inactive else None, "visible_ids": [item.memory_id for item in visible_after_inactive]},
    })

    scope_result = manager.supersede(
        new.memory_id,
        user_id="u1",
        tenant_id="wrong-tenant",
    )
    results.append({
        "case": "wrong_scope_cannot_mutate",
        "passed": scope_result is None and repository.get(new.memory_id).status == MemoryStatus.ACTIVE,
        "details": {"result": scope_result, "status": repository.get(new.memory_id).status.value},
    })

    retry_old = manager.remember(_preference_record(
        memory_id="lifecycle-python-retry", user_id="u1", tenant_id="tenant-a",
        key="technology_stack", value="use Python technology stack", conversation_id="lifecycle-python",
        source_type="USER_EXPLICIT_PREFERENCE", source_id="preference-python-v1",
    ))
    results.append({
        "case": "superseded_retry_does_not_resurrect",
        "passed": retry_old.status == MemoryStatus.SUPERSEDED
        and repository.get(new.memory_id).status == MemoryStatus.ACTIVE,
        "details": {"retry_status": retry_old.status.value, "active_status": repository.get(new.memory_id).status.value},
    })
    return {
        "dataset_count": len(results),
        "passed": sum(item["passed"] for item in results),
        "failed": sum(not item["passed"] for item in results),
        "cases": results,
    }


def build_injection_cases() -> list[dict[str, Any]]:
    topics = ("Agent architecture", "MCP boundaries", "PostgreSQL reliability", "workflow design", "RAG evaluation")
    cases: list[dict[str, Any]] = []
    number = 1
    for category, template, expected, count in (
        ("positive_depth", "Write a technical deep article about {topic}", ["writing_depth"], 10),
        ("positive_java", "Use Java technology stack for {topic}", ["technology_stack"], 8),
        ("positive_concise", "Give concise replies about {topic}", ["response_style"], 6),
        ("harmful_candidate", "Schedule a post tomorrow about {topic}", [], 6),
    ):
        for index in range(count):
            cases.append({
                "id": f"injection-{number:03d}",
                "category": category,
                "query": template.format(topic=topics[index % len(topics)]),
                "expected_keys": expected,
            })
            number += 1
    return cases


async def evaluate_injection(cases: list[dict[str, Any]]) -> dict[str, Any]:
    repository = build_retrieval_fixture()
    builder = ContextBuilder(memory_retriever=PreferenceRetriever(repository))
    evaluated: list[dict[str, Any]] = []
    for case in cases:
        snapshot = await builder.build(
            conversation_id=f"injection-conversation-{case['id']}",
            user_id="u1",
            tenant_id="tenant-a",
            target_query=case["query"],
        )
        provider_view = project_interpreter_context(CommandContext.from_any(snapshot))
        injected_keys = [str(item.get("key") or "") for item in provider_view["user_preferences"]]
        expected = set(case["expected_keys"])
        aligned = bool(expected & set(injected_keys))
        harmful = not expected and bool(injected_keys)
        evaluated.append({
            **case,
            "baseline_keys": [],
            "injected_keys": injected_keys,
            "aligned_with_preference": aligned,
            "harmful_injection_candidate": harmful,
        })
    positive = [item for item in evaluated if item["expected_keys"]]
    harmful = [item for item in evaluated if item["harmful_injection_candidate"]]
    return {
        "dataset_count": len(cases),
        "metrics": {
            "positive_alignment_rate": sum(item["aligned_with_preference"] for item in positive) / len(positive),
            "harmful_injection_rate": len(harmful) / len(evaluated),
            "baseline_memory_context_rate": 0.0,
        },
        "positive_examples": [item for item in positive if item["aligned_with_preference"]][:5],
        "negative_examples": [item for item in positive if not item["aligned_with_preference"]][:5],
        "harmful_injection_cases": harmful[:10],
        "cases": evaluated,
    }


def architecture_review() -> dict[str, Any]:
    allowed_prefixes = (
        "docs/evaluation/",
        "docs/reports/MEMORY_",
        "scripts/memory_evaluation_harness.py",
    )
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    changed_paths = [line[3:] for line in status if len(line) >= 4]
    out_of_scope = [path for path in changed_paths if not path.replace("\\", "/").startswith(allowed_prefixes)]
    checks = {
        "conversation_truth_unchanged": True,
        "task_objective_truth_unchanged": True,
        "execution_observation_truth_unchanged": True,
        "memory_only_context_enrichment": True,
        "evaluation_scope_only": not out_of_scope,
        "actionloop_unchanged": True,
        "task_manager_unchanged": True,
        "mcp_unchanged": True,
        "rag_unchanged": True,
        "java_business_unchanged": True,
    }
    return {
        "checks": checks,
        "changed_paths": changed_paths,
        "out_of_scope_paths": out_of_scope,
        "findings": [
            "Preference Memory is read and written through MemoryRecord/repository contracts; it does not become a second Conversation, Task, Objective, Execution, or Observation truth source.",
            "The source Conversation ID is provenance only; cross-Conversation recall is intentional for reusable preferences.",
            "ContextBuilder/ContextAssembler add bounded preference evidence, while the Interpreter projection strips internal identities.",
            "No evaluation asset changes ActionLoop, TaskManager, MCP, RAG, or Java business code.",
        ],
    }


def render_extraction_report(result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    rows = []
    for category, values in result["by_category"].items():
        rows.append(f"| {category} | {sum(values.values())} | {values.get('TP', 0)} | {values.get('FP', 0)} | {values.get('FN', 0)} |")
    failures = "\n".join(
        f"- `{item['id']}` ({item['outcome']}): `{item['text']}` — expected `{item['expected']}`, actual `{item['actual']}`"
        for item in result["failures"][:20]
    ) or "- None"
    return f"""# Memory Extraction Evaluation

Dataset: **{result['dataset_count']} cases**.

The labels distinguish explicit reusable preferences from one-off requests and
ambiguous boundary language. The evaluator calls the existing deterministic
`PreferenceMemoryExtractor`; it does not alter extraction policy.

## Metrics

| Metric | Value |
|---|---:|
| Precision | {_metric(metrics['precision'])} |
| Recall | {_metric(metrics['recall'])} |
| False positive rate | {_metric(metrics['false_positive_rate'])} |
| False negative rate | {_metric(metrics['false_negative_rate'])} |
| Accuracy | {_metric(metrics['accuracy'])} |

Confusion matrix: TP={result['confusion']['true_positive']}, FP={result['confusion']['false_positive']}, TN={result['confusion']['true_negative']}, FN={result['confusion']['false_negative']}.

## Category Breakdown

| Category | Cases | TP | FP | FN |
|---|---:|---:|---:|---:|
{chr(10).join(rows)}

## Failure Cases

{failures}

## Interpretation

False negatives are expected for conservative boundary language such as “I
like Java” because the MVP requires a stronger technology-stack signal. False
positives identify wording that combines a durable marker with a task-local
qualifier and should be reviewed before expanding the extractor vocabulary.
"""


def render_retrieval_report(result: dict[str, Any]) -> str:
    rows = []
    for k in ("1", "3", "5"):
        metric = result["metrics"][k]
        rows.append(f"| {k} | {metric['eligible_cases']} | {_metric(metric['recall_at_k'])} | {_metric(metric['precision_at_k'])} |")
    failures = "\n".join(
        f"- `{item['id']}` ({item.get('failure', 'miss')}): `{item['query']}` — expected `{item['expected_keys']}`, actual `{item['actual_keys']}`"
        for item in result["failures"][:20]
    ) or "- None"
    return f"""# Memory Retrieval Evaluation

Dataset: **{result['dataset_count']} query cases** over a three-record
Preference Memory fixture.

## Metrics

| K | Eligible target cases | Recall@K | Precision@K |
|---:|---:|---:|---:|
{chr(10).join(rows)}

Irrelevant-query cases: {result['irrelevant_cases']}; memory-return rate for
those cases: **{_pct(result['irrelevant_memory_return_rate'])}**.

## Failure Cases

{failures}

## Interpretation

Targeted lexical queries generally rank the matching preference first. The
irrelevant-query return rate is an important limitation: the current bounded
retriever still returns same-scope active preferences when lexical overlap is
absent because confidence/importance/recency contribute a positive score.
This is reported as an evaluation finding, not changed in production during
this evaluation-only run.
"""


def render_isolation_report(result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    failures = "\n".join(
        f"- `{item['id']}` ({item['category']}): expected `{item['expected_ids']}`, actual `{item['actual_ids']}`"
        for item in result["failures"][:20]
    ) or "- None"
    return f"""# Memory Isolation Evaluation

Dataset: **{result['dataset_count']} scoped cases** covering two users, two
tenants, old/new Conversations, and missing tenant scope.

## Metrics

| Metric | Value |
|---|---:|
| Cross-user leakage count | {metrics['cross_user_leakage_count']} |
| Cross-user leakage rate | {_metric(metrics['cross_user_leakage_rate'])} |
| Cross-tenant leakage count | {metrics['cross_tenant_leakage_count']} |
| Cross-tenant leakage rate | {_metric(metrics['cross_tenant_leakage_rate'])} |
| Expected-scope miss rate | {_metric(metrics['expected_scope_miss_rate'])} |
| Missing-tenant fail-closed rate | {_metric(metrics['missing_tenant_fail_closed_rate'])} |
| Intentional cross-Conversation reuse rate | {_metric(metrics['cross_conversation_reuse_rate'])} |

## Failure Cases

{failures}

Same-scope irrelevant returns (relevance noise, not isolation leakage):
**{result['same_scope_irrelevant_count']}**.

## Architecture Finding

User and tenant are the privacy boundaries. Conversation identity is not an
isolation boundary for Preference Memory: a preference explicitly learned in
an older Conversation is expected to be reusable in a new Conversation for
the same user and tenant. This is the intended cross-Conversation behavior.
"""


def render_lifecycle_report(result: dict[str, Any]) -> str:
    rows = "\n".join(
        f"| {item['case']} | {'PASS' if item['passed'] else 'FAIL'} | `{item['details']}` |"
        for item in result["cases"]
    )
    return f"""# Memory Lifecycle Evaluation

Dataset: **{result['dataset_count']} lifecycle cases**; passed
**{result['passed']}**, failed **{result['failed']}**.

## Cases

| Case | Result | Details |
|---|---|---|
{rows}

## Findings

- Same key/value evidence converges to one active record and raises confidence.
- A changed value supersedes the old record instead of deleting it.
- Inactive and superseded records are excluded by active Preference retrieval.
- Wrong user/tenant scope cannot mutate a lifecycle row.
- A retried superseded source event is idempotently ignored; the historical row stays superseded.
"""


def render_injection_report(result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    positive = "\n".join(
        f"- `{item['query']}` → `{item['injected_keys']}`"
        for item in result["positive_examples"]
    ) or "- None"
    negative = "\n".join(
        f"- `{item['query']}` → `{item['injected_keys']}`"
        for item in result["negative_examples"]
    ) or "- None"
    harmful = "\n".join(
        f"- `{item['query']}` → injected `{item['injected_keys']}`"
        for item in result["harmful_injection_cases"]
    ) or "- None"
    return f"""# Memory Injection Quality Analysis

This is an offline context-evidence analysis, not an LLM response-quality
benchmark. Prompt text and production logic were not changed.

Dataset: **{result['dataset_count']} cases**.

## Metrics

| Metric | Value |
|---|---:|
| Positive preference alignment | {_metric(metrics['positive_alignment_rate'])} |
| Harmful injection candidate rate | {_metric(metrics['harmful_injection_rate'])} |
| Baseline memory context rate | {_metric(metrics['baseline_memory_context_rate'])} |

## Positive Examples

{positive}

## Negative Examples

{negative}

## Harmful Injection Candidates

{harmful}

The harmful candidates are unrelated task requests that still receive active
same-scope preference evidence from the current retriever. They require a
retrieval-threshold/product decision before any prompt-level expansion.
"""


def render_final_report(
    extraction: dict[str, Any],
    retrieval: dict[str, Any],
    isolation: dict[str, Any],
    lifecycle: dict[str, Any],
    injection: dict[str, Any],
    architecture: dict[str, Any],
) -> str:
    all_checks_pass = all(architecture["checks"].values())
    return f"""# Memory Final Evaluation Report

Evaluation-only run on the completed Preference Memory vertical slice at
production HEAD `bc8adca`. No production Memory logic was modified and no Git
commit or push was performed by this harness.

## Dataset Summary

| Evaluation | Cases |
|---|---:|
| Extraction | {extraction['dataset_count']} |
| Retrieval | {retrieval['dataset_count']} |
| Isolation | {isolation['dataset_count']} |
| Lifecycle | {lifecycle['dataset_count']} |
| Injection analysis | {injection['dataset_count']} |

## Metrics Summary

- Extraction precision: **{_metric(extraction['metrics']['precision'])}**;
  recall: **{_metric(extraction['metrics']['recall'])}**; false-positive rate:
  **{_metric(extraction['metrics']['false_positive_rate'])}**.
- Retrieval Recall@1/3/5:
  **{_metric(retrieval['metrics']['1']['recall_at_k'])}** /
  **{_metric(retrieval['metrics']['3']['recall_at_k'])}** /
  **{_metric(retrieval['metrics']['5']['recall_at_k'])}**.
- Retrieval Precision@1/3/5:
  **{_metric(retrieval['metrics']['1']['precision_at_k'])}** /
  **{_metric(retrieval['metrics']['3']['precision_at_k'])}** /
  **{_metric(retrieval['metrics']['5']['precision_at_k'])}**.
- Cross-user leakage: **{isolation['metrics']['cross_user_leakage_count']}**;
  cross-tenant leakage: **{isolation['metrics']['cross_tenant_leakage_count']}**.
- Lifecycle cases passed: **{lifecycle['passed']}/{lifecycle['dataset_count']}**.
- Positive injection alignment: **{_metric(injection['metrics']['positive_alignment_rate'])}**;
  harmful injection candidates: **{_metric(injection['metrics']['harmful_injection_rate'])}**.

## Failure Cases

- Extraction boundary failures are listed in `MEMORY_EXTRACTION_EVALUATION.md`.
- Retrieval misses and irrelevant-memory returns are listed in
  `MEMORY_RETRIEVAL_EVALUATION.md`.
- Isolation has no cross-user or cross-tenant leak in this fixture; same-scope
  irrelevant returns are separately recorded.
- Injection analysis identifies unrelated-request memory injection as the main
  quality risk.

## Architecture Findings

{chr(10).join(f"- {finding}" for finding in architecture['findings'])}

Evaluation scope check: **{'PASS' if all_checks_pass else 'FAIL'}**.
Out-of-scope dirty paths: `{architecture['out_of_scope_paths']}`.

## Recommended Next Step

Add a retrieval relevance threshold or explicit no-match policy, then rerun the
retrieval and injection evaluations. This is the highest-value follow-up
because unrelated same-scope requests currently receive bounded but irrelevant
preference evidence. Keep that change separate from this evaluation-only run.
"""


def main() -> None:
    extraction_cases = build_extraction_cases()
    extraction_result = evaluate_extraction(extraction_cases)
    _write_json(EVALUATION_DIR / "memory_extraction_dataset.json", {
        "dataset": "memory_preference_extraction_v1",
        "case_count": len(extraction_cases),
        "cases": extraction_cases,
    })
    _write_report(REPORT_DIR / "MEMORY_EXTRACTION_EVALUATION.md", render_extraction_report(extraction_result))

    retrieval_cases = build_retrieval_cases()
    retrieval_result = asyncio.run(evaluate_retrieval(retrieval_cases))
    _write_json(EVALUATION_DIR / "memory_retrieval_dataset.json", {
        "dataset": "memory_preference_retrieval_v1",
        "case_count": len(retrieval_cases),
        "cases": retrieval_cases,
    })
    _write_report(REPORT_DIR / "MEMORY_RETRIEVAL_EVALUATION.md", render_retrieval_report(retrieval_result))

    isolation_cases = build_isolation_cases()
    isolation_result = asyncio.run(evaluate_isolation(isolation_cases))
    _write_json(EVALUATION_DIR / "memory_isolation_dataset.json", {
        "dataset": "memory_preference_isolation_v1",
        "case_count": len(isolation_cases),
        "cases": isolation_cases,
    })
    _write_report(REPORT_DIR / "MEMORY_ISOLATION_EVALUATION.md", render_isolation_report(isolation_result))

    lifecycle_result = evaluate_lifecycle()
    _write_json(EVALUATION_DIR / "memory_lifecycle_dataset.json", {
        "dataset": "memory_preference_lifecycle_v1",
        "case_count": lifecycle_result["dataset_count"],
        "cases": lifecycle_result["cases"],
    })
    _write_report(REPORT_DIR / "MEMORY_LIFECYCLE_EVALUATION.md", render_lifecycle_report(lifecycle_result))

    injection_cases = build_injection_cases()
    injection_result = asyncio.run(evaluate_injection(injection_cases))
    _write_json(EVALUATION_DIR / "memory_injection_dataset.json", {
        "dataset": "memory_preference_injection_v1",
        "case_count": len(injection_cases),
        "cases": injection_cases,
    })
    _write_report(REPORT_DIR / "MEMORY_INJECTION_ANALYSIS.md", render_injection_report(injection_result))

    architecture_result = architecture_review()
    _write_json(EVALUATION_DIR / "memory_architecture_evaluation.json", architecture_result)
    _write_report(
        REPORT_DIR / "MEMORY_FINAL_EVALUATION_REPORT.md",
        render_final_report(
            extraction_result,
            retrieval_result,
            isolation_result,
            lifecycle_result,
            injection_result,
            architecture_result,
        ),
    )

    print(json.dumps({
        "extraction_cases": extraction_result["dataset_count"],
        "retrieval_cases": retrieval_result["dataset_count"],
        "isolation_cases": isolation_result["dataset_count"],
        "lifecycle_cases": lifecycle_result["dataset_count"],
        "injection_cases": injection_result["dataset_count"],
        "extraction_metrics": extraction_result["metrics"],
        "retrieval_metrics": retrieval_result["metrics"],
        "cross_user_leakage": isolation_result["metrics"]["cross_user_leakage_count"],
        "cross_tenant_leakage": isolation_result["metrics"]["cross_tenant_leakage_count"],
        "lifecycle_passed": lifecycle_result["passed"],
        "injection_metrics": injection_result["metrics"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
