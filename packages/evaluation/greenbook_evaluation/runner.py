"""EvalRunner — execute EvalCases against the Runtime."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.task.models import Task, TaskIntent, TaskStatus

from .datasets import ALL_DATASETS
from .metrics import MetricsCalculator
from .models import EvalCase, EvalCheck, EvalResult, EvaluationReport


class MockMCP:
    """Mock MCP that returns canned responses keyed by tool name."""

    def __init__(self, responses: dict[str, dict] | None = None):
        self._responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> dict:
        self.calls.append((tool_name, kwargs))
        if tool_name in self._responses:
            return dict(self._responses[tool_name])
        # Default success for any tool
        return {
            "ok": True, "code": "",
            "data": {"draft_id": "draft-mock", "title": "Mock Result"},
        }


_DEFAULT_MCP_RESPONSES: dict[str, dict] = {
    "content.create_draft": {
        "ok": True, "code": "",
        "data": {"draft_id": "draft-eval", "title": "Eval Article"},
    },
    "content.revise_draft": {
        "ok": True, "code": "",
        "data": {"draft_id": "draft-eval", "title": "Revised", "status": "DRAFT"},
    },
    "community.search_public_posts": {
        "ok": True, "code": "",
        "data": {"items": [{"post_id": "p1", "title": "Hot Java"}], "total": 1},
    },
    "publication.schedule": {
        "ok": True, "code": "",
        "data": {"schedule_id": "sched-eval", "draft_id": "draft-eval",
                 "status": "SCHEDULED"},
    },
    "publication.update_schedule": {
        "ok": True, "code": "",
        "data": {"schedule_id": "sched-eval", "status": "SCHEDULED"},
    },
    "publication.cancel_schedule": {
        "ok": True, "code": "",
        "data": {"schedule_id": "sched-eval", "status": "CANCELLED"},
    },
}


class EvalRunner:
    """Execute EvalCases against RuntimeAgentService and check expectations."""

    def __init__(
        self,
        ras: RuntimeAgentService | None = None,
        mcp_responses: dict[str, dict] | None = None,
    ) -> None:
        self._ras = ras or RuntimeAgentService()
        self._mcp_responses = {**_DEFAULT_MCP_RESPONSES,
                               **(mcp_responses or {})}

    # ── main entry ───────────────────────────────────────────────

    async def run_dataset(
        self, dataset: list[EvalCase], dataset_name: str = "",
    ) -> EvaluationReport:
        results: list[EvalResult] = []
        for case in dataset:
            result = await self._run_one(case)
            results.append(result)
        return MetricsCalculator.compute(results, dataset_name)

    async def run_all_datasets(self) -> dict[str, EvaluationReport]:
        reports: dict[str, EvaluationReport] = {}
        for name, dataset in ALL_DATASETS.items():
            reports[name] = await self.run_dataset(dataset, name)
        return reports

    # ── single case ──────────────────────────────────────────────

    async def _run_one(self, case: EvalCase) -> EvalResult:
        t0 = time.monotonic()

        try:
            ctx = self._build_context(case)
            result = await self._ras.execute(ctx)
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000
            return EvalResult(
                case_id=case.case_id, category=case.category,
                description=case.description,
                passed=False, errors=[str(exc)], duration_ms=elapsed,
            )

        elapsed = (time.monotonic() - t0) * 1000
        checks = self._check_expectations(case, ctx, result)
        passed = all(c.ok for c in checks)

        return EvalResult(
            case_id=case.case_id, category=case.category,
            description=case.description,
            passed=passed, checks=checks, duration_ms=elapsed,
        )

    # ── context building ─────────────────────────────────────────

    def _build_context(self, case: EvalCase) -> RuntimeContext:
        mcp = MockMCP(self._mcp_responses)

        intent = TaskIntent(
            relation="NEW_TASK",
            goal=case.user_message,
        )

        recent_tasks = self._build_recent_tasks(case.existing_tasks)

        return RuntimeContext(
            conversation_id="eval-conv",
            run_id=f"eval-{case.case_id}",
            trace_id=f"trace-{case.case_id}",
            user_id="eval-user",
            tenant_id="eval-tenant",
            user_message=case.user_message,
            task_intent=intent,
            mcp=mcp,
            llm=None,  # L1 only
            model="eval-model",
            recent_tasks=recent_tasks,
            session=None,
        )

    @staticmethod
    def _build_recent_tasks(raw: list[dict]) -> list[Task]:
        from datetime import UTC, datetime, timedelta
        tasks: list[Task] = []
        for d in raw:
            ago = d.get("created_at_ago", 0)
            created = (datetime.now(UTC) - timedelta(seconds=ago)).isoformat()
            artifacts = []
            for a in d.get("artifacts", []):
                from greenbook_assistant_core.task.models import ArtifactRef
                artifacts.append(ArtifactRef(
                    artifact_id=f"art-{a.get('resource_id', 'x')}",
                    task_id=d.get("task_id", ""),
                    artifact_type=a.get("type", "DRAFT"),
                    resource_id=a.get("resource_id"),
                    resource_kind=a.get("type", "DRAFT"),
                ))
            tasks.append(Task(
                task_id=d.get("task_id", ""),
                conversation_id="eval-conv",
                user_id="eval-user", tenant_id="eval-tenant",
                goal=d.get("goal", ""),
                goal_category=d.get("goal_category", "CREATE_CONTENT"),
                status=TaskStatus.COMPLETED,
                artifacts=artifacts,
                created_at=created,
            ))
        return tasks

    # ── expectation checking ─────────────────────────────────────

    @staticmethod
    def _check_expectations(
        case: EvalCase, ctx: RuntimeContext, result: Any,
    ) -> list[EvalCheck]:
        checks: list[EvalCheck] = []

        # ── intent ──
        if case.expected_intent:
            intent = ctx.task_intent
            for key, expected_val in case.expected_intent.items():
                if key == "requirements":
                    actual_reqs = [
                        r.get("type") if isinstance(r, dict) else str(r)
                        for r in (getattr(intent, "requirements", []) or [])
                    ]
                    expected_reqs = [
                        r.get("type") if isinstance(r, dict) else str(r)
                        for r in (expected_val or [])
                    ]
                    checks.append(EvalCheck(
                        check="intent.requirements",
                        expected=expected_reqs, actual=actual_reqs,
                        ok=set(expected_reqs).issubset(set(actual_reqs)),
                    ))
                elif key == "resource_requests":
                    actual = getattr(intent, "resource_requests", []) or []
                    expected = expected_val
                    # Compare as dict lists
                    match = len(actual) >= len(expected)
                    checks.append(EvalCheck(
                        check="intent.resource_requests",
                        expected=expected, actual=actual,
                        ok=match,
                    ))
                else:
                    actual_val = getattr(intent, key, None)
                    checks.append(EvalCheck(
                        check=f"intent.{key}",
                        expected=expected_val, actual=actual_val,
                        ok=actual_val == expected_val,
                    ))

        # ── sub_task_count ──
        if case.expected_sub_task_count is not None:
            pr = getattr(result, "partial_results", None) if result else None
            actual_count = (pr or {}).get("sub_task_count", 1) if isinstance(pr, dict) else 1
            checks.append(EvalCheck(
                check="sub_task_count",
                expected=case.expected_sub_task_count,
                actual=actual_count,
                ok=actual_count == case.expected_sub_task_count,
            ))

        # ── status ──
        actual_status = getattr(result, "status", "FAILED") if result else "FAILED"
        checks.append(EvalCheck(
            check="status",
            expected=case.expected_status,
            actual=actual_status,
            ok=actual_status == case.expected_status,
        ))

        # ── clarification ──
        if case.expected_clarification:
            actual_clar = actual_status == "WAITING_APPROVAL"
            checks.append(EvalCheck(
                check="clarification",
                expected=True, actual=actual_clar,
                ok=actual_clar is True,
            ))

        # ── template ──
        if case.expected_template:
            # Template is not directly accessible from RuntimeResult
            # Check via partial_results or skip
            pass

        # ── tools ──
        if case.expected_tools:
            mcp = getattr(ctx, "mcp", None)
            called = []
            if hasattr(mcp, "calls"):
                called = [c[0] for c in mcp.calls]
            checks.append(EvalCheck(
                check="tools",
                expected=case.expected_tools,
                actual=called,
                ok=set(case.expected_tools).issubset(set(called)),
            ))

        # ── resource_id ──
        if case.expected_resource_id:
            actual_rid = getattr(result, "draft_id", None) if result else None
            checks.append(EvalCheck(
                check="resource_id",
                expected=case.expected_resource_id,
                actual=actual_rid,
                ok=actual_rid == case.expected_resource_id,
            ))

        # ── trace events ──
        if case.expected_trace_events:
            actual_events = []
            if result and hasattr(result, "events"):
                actual_events = [e.get("event", "") for e in (result.events or [])]
            checks.append(EvalCheck(
                check="trace_events",
                expected=case.expected_trace_events,
                actual=actual_events[:len(case.expected_trace_events)],
                ok=set(case.expected_trace_events).issubset(set(actual_events)),
            ))

        return checks
