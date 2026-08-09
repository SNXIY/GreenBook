"""GroupExecutor — execute a TaskGroup with DAG-aware parallel scheduling.

Phase 6.4: supports concurrent SubTask execution for independent tasks.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from greenbook_assistant_core.execution.group_scheduler import GroupScheduler

from ..models.runtime_context import RuntimeContext
from ..models.runtime_result import RuntimeResult

logger = logging.getLogger(__name__)


class GroupExecutor:
    """Execute a TaskGroup with DAG-aware parallelism.

    Independent SubTasks within a batch run concurrently via
    asyncio.gather.  A semaphore caps concurrency.
    """

    def __init__(
        self, ras: Any,  # RuntimeAgentService
        trace: Any = None,  # AgentTrace | None
        max_parallel: int = 4,
    ) -> None:
        self._ras = ras
        self._trace = trace
        self._group: Any = None
        self._scheduler = GroupScheduler(max_parallel)
        self._semaphore = asyncio.Semaphore(max_parallel)

    # ── main entry ───────────────────────────────────────────────

    async def execute(
        self, group: Any, shared_ctx: RuntimeContext,
    ) -> RuntimeResult:
        self._group = group
        results: dict[int, RuntimeResult] = {}
        tr = self._trace

        if tr is not None:
            tr.group_created(sub_count=len(group.sub_tasks))

        batches = self._scheduler.schedule(group)

        if tr is not None and len(batches) > 1:
            tr.group_parallel_started(batch_count=len(batches))

        for batch in batches:
            if tr is not None:
                tr.sub_task_batch_started(
                    batch.batch_id,
                    [s.sub_index for s in batch.sub_tasks],
                )
            tasks = [
                self._execute_one(sub, shared_ctx, results, tr)
                for sub in batch.sub_tasks
            ]
            await asyncio.gather(*tasks)

        if tr is not None and len(batches) > 1:
            tr.group_parallel_completed()

        ordered = [results[i] for i in sorted(results)]
        completed = sum(1 for r in ordered if r.success)
        if tr is not None:
            tr.group_completed(
                status="COMPLETED" if completed == len(ordered) else "PARTIAL",
                count=completed,
            )
        return self._aggregate(ordered)

    # ── single sub-task ──────────────────────────────────────────

    async def _execute_one(
        self, sub: Any, shared_ctx: RuntimeContext,
        results: dict[int, RuntimeResult], tr: Any,
    ) -> None:
        async with self._semaphore:
            if self._should_skip(sub, results):
                if tr is not None:
                    tr.sub_task_skipped(sub.sub_index, "upstream failed")
                results[sub.sub_index] = self._skip_result(sub)
                return

            dep_resources = self._resolve_dep_resources(sub)
            sub.dependency_resources = dep_resources
            sub_ctx = self._build_sub_context(sub, shared_ctx)

            if tr is not None:
                tr.sub_task_started(sub.sub_index, sub.user_message)

            result = await self._ras._execute_single(sub_ctx)
            sub.task_id = result.task_id
            sub.result = result
            results[sub.sub_index] = result

            if tr is not None:
                if result.success:
                    tr.sub_task_completed(sub.sub_index, result.task_id)
                else:
                    tr.sub_task_failed(sub.sub_index,
                                       result.error_message or "failed")

    def _resolve_dep_resources(self, sub: Any) -> dict[str, str]:
        if sub.depends_on_task_index is None:
            return {}
        source = self._group.sub_tasks[sub.depends_on_task_index]
        if not source.result:
            return {}
        resources: dict[str, str] = {}
        if getattr(source.result, "draft_id", None):
            resources["draft_id"] = source.result.draft_id
        if getattr(source.result, "schedule_id", None):
            resources["schedule_id"] = source.result.schedule_id
        return resources

    def _should_skip(
        self, sub: Any, results: dict[int, RuntimeResult],
    ) -> bool:
        if sub.depends_on_task_index is None:
            return False
        source = results.get(sub.depends_on_task_index)
        return source is not None and not source.success

    def _skip_result(self, sub: Any) -> RuntimeResult:
        dep_idx = sub.depends_on_task_index or 0
        return RuntimeResult(
            success=False, status="SKIPPED", run_id="",
            content=(
                f"任务{sub.sub_index + 1}已跳过："
                f"依赖的任务{dep_idx + 1}执行失败"
            ),
            execution_path="runtime",
        )

    def _build_sub_context(
        self, sub: Any, shared: RuntimeContext,
    ) -> RuntimeContext:
        task_id = ""
        if sub.depends_on_task_index is not None:
            source = self._group.sub_tasks[sub.depends_on_task_index]
            if source.task_id:
                task_id = source.task_id
        return RuntimeContext(
            llm=shared.llm, mcp=shared.mcp, auth=shared.auth,
            timezone=shared.timezone,
            user_id=shared.user_id, tenant_id=shared.tenant_id,
            conversation_id=shared.conversation_id,
            session=shared.session,
            recent_tasks=shared.recent_tasks,
            conversation_history=shared.conversation_history,
            run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
            task_id=task_id, task_intent=sub.task_intent,
            user_message=sub.user_message,
            active_draft_id=None, active_schedule_id=None,
            resolved_resources=None,
        )

    @staticmethod
    def _aggregate(results: list[RuntimeResult]) -> RuntimeResult:
        all_ok = all(r.success for r in results)
        completed = sum(1 for r in results if r.success)
        parts = [
            f"[{'OK' if r.success else 'SKIP'}] 任务{i + 1}: {r.content}"
            for i, r in enumerate(results)
        ]
        return RuntimeResult(
            success=all_ok,
            status="COMPLETED" if all_ok else "PARTIAL",
            run_id="", content="\n\n".join(parts),
            summary=f"{completed}/{len(results)} tasks completed",
            execution_path="runtime", events=[],
            tool_rounds=sum(r.tool_rounds for r in results),
            artifact_ids=[aid for r in results for aid in (r.artifact_ids or [])],
            artifacts=[artifact for r in results for artifact in (r.artifacts or [])],
            draft_id=results[-1].draft_id if results else None,
            partial_results={
                "group": True,
                "sub_task_count": len(results),
                "completed_count": completed,
                "failed_count": len(results) - completed,
            },
        )
