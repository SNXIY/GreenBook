"""Phase15-F evaluator and dataset contract tests (no external-service mock E2E)."""

from __future__ import annotations

from pathlib import Path

from evaluation.artifact_eval import compare_artifacts
from evaluation.runtime_eval import evaluate_cases, load_jsonl
from evaluation.task_graph_eval import compare_task_graph
from evaluation.tool_eval import recovery_metrics, tool_metrics


ROOT = Path(__file__).resolve().parents[2]


def test_all_phase15f_datasets_are_jsonl_and_have_required_fields() -> None:
    files = sorted((ROOT / "evaluation" / "datasets").glob("*.jsonl"))
    assert {path.stem for path in files} >= {
        "simple_query", "single_task", "multi_task", "cross_turn_reference",
        "compound_action", "failure_recovery",
    }
    for path in files:
        for case in load_jsonl(path):
            assert case["input"]
            for field in (
                "expected_tasks", "expected_goals", "expected_dependencies",
                "expected_agents", "expected_side_effects",
            ):
                assert field in case, f"{path.name}:{case.get('case_id')} missing {field}"


def test_task_graph_and_artifact_metrics_accept_matching_observation() -> None:
    case = {
        "case_id": "graph-1",
        "expected_tasks": 2,
        "expected_goals": 2,
        "expected_dependencies": [["a", "b"]],
        "expected_agents": ["SearchAgent", "CreatorAgent"],
        "expected_artifacts": [{"artifact_type": "POST_COLLECTION", "lifecycle": "AVAILABLE"}],
        "expected_side_effects": [],
    }
    actual = {
        "tasks": ["a", "b"],
        "goals": [{}, {}],
        "dependencies": [["a", "b"]],
        "agents": ["SearchAgent", "CreatorAgent"],
        "artifacts": [{"artifact_type": "POST_COLLECTION", "lifecycle": "AVAILABLE"}],
        "side_effects": [],
    }
    assert all(check["ok"] for check in compare_task_graph(case, actual))
    assert all(check["ok"] for check in compare_artifacts(case["expected_artifacts"], actual["artifacts"]))


def test_runtime_and_recovery_metrics_are_computed_without_fake_calls() -> None:
    observations = [{
        "execution_status": "COMPLETED",
        "tool_calls": [{"success": True}, {"success": False, "status": "TIMEOUT"}],
        "steps": [{"status": "COMPLETED"}],
        "retry_attempts": 1,
        "retry_succeeded": True,
        "worker_restart": True,
        "worker_restart_recovered": True,
    }]
    assert tool_metrics(observations)["tool_success_rate"] == 0.5
    assert tool_metrics(observations)["runtime_success_rate"] == 1.0
    assert recovery_metrics(observations)["worker_restart_recovery_rate"] == 1.0


def test_missing_live_observation_is_blocked_not_passed() -> None:
    report = evaluate_cases(
        [{
            "case_id": "live-1",
            "input": "真实服务测试",
            "expected_tasks": 1,
            "expected_goals": 1,
            "expected_dependencies": [],
            "expected_agents": ["CreatorAgent"],
            "expected_side_effects": ["DRAFT_CREATED"],
        }],
        blocked_reason="BLOCKED_BY_ENV: credentials missing",
    )
    assert report["status"] == "BLOCKED_BY_ENV"
    assert report["badcases"][0]["badcase"].startswith("BLOCKED_BY_ENV")
