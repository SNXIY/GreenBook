"""ResourceResolver — resolve ResourceRequests to concrete ResourceTargets.

Reuses TaskResolver for Task-level matching, then queries Task.artifacts
for the specific resource (draft_id, schedule_id, post_id).
"""

from __future__ import annotations

import logging

from greenbook_assistant_core.task.models import Task, TaskIntent
from greenbook_assistant_core.task.resolver import TaskResolver

from .models import (
    ResourceOperation,
    ResourceRequest,
    ResourceResolutionResult,
    ResourceTarget,
    ResourceType,
)

logger = logging.getLogger(__name__)

# Artifact type → ResourceType mapping for lookup in Task.artifacts
_ARTIFACT_TO_RESOURCE: dict[ResourceType, str] = {
    ResourceType.CONTENT_DRAFT: "DRAFT",
    ResourceType.SCHEDULE: "SCHEDULE",
    ResourceType.POST: "POST",
}


class ResourceResolver:
    """Resolve ResourceRequests against conversation Tasks.

    CREATE → no target needed (resource_id stays None).
    UPDATE / DELETE → resolve Task first, then find artifact.
    """

    def __init__(self) -> None:
        self._task_resolver = TaskResolver()

    # ── main entry ───────────────────────────────────────────────

    def resolve(
        self,
        requests: list[ResourceRequest],
        tasks: list[Task],
    ) -> ResourceResolutionResult:
        """Resolve a list of ResourceRequests into ResourceTargets."""
        result = ResourceResolutionResult()
        sorted_tasks = sorted(tasks, key=lambda t: t.updated_at, reverse=True)

        for req in requests:
            target = self._resolve_one(req, sorted_tasks)
            result.targets.append(target)
            if target.is_ambiguous:
                result.needs_clarification = True

        return result

    # ── per-request resolution ───────────────────────────────────

    def _resolve_one(
        self,
        req: ResourceRequest,
        tasks: list[Task],
    ) -> ResourceTarget:
        if req.operation == ResourceOperation.CREATE:
            return ResourceTarget(
                operation=req.operation,
                resource_type=req.resource_type,
                resource_id=None,  # will be created
                task_id=req.task_id,
                confidence=1.0,
                match_reason="create_new",
            )

        # UPDATE / DELETE: need to find the target resource
        return self._resolve_update_target(req, tasks)

    def _resolve_update_target(
        self,
        req: ResourceRequest,
        tasks: list[Task],
    ) -> ResourceTarget:
        artifact_type = _ARTIFACT_TO_RESOURCE.get(req.resource_type, "")
        if not artifact_type:
            return ResourceTarget(
                operation=req.operation,
                resource_type=req.resource_type,
                match_reason="unsupported_type",
            )

        # 1. Explicit task_id → look up that task's artifacts
        if req.task_id:
            task = next((t for t in tasks if t.task_id == req.task_id), None)
            if task:
                return self._find_in_task(req, task, artifact_type)
            return ResourceTarget(
                operation=req.operation,
                resource_type=req.resource_type,
                task_id=req.task_id,
                match_reason="task_not_found",
            )

        # 2. Hint → TaskResolver → then artifacts
        if req.hint:
            intent = TaskIntent(
                relation="MODIFY_TASK",
                goal_category="",
                target_task_hint=req.hint,
                goal=req.hint,
            )
            resolved = self._task_resolver.resolve(intent, tasks)
            if resolved is None:
                return ResourceTarget(
                    operation=req.operation,
                    resource_type=req.resource_type,
                    hint=req.hint,
                    match_reason="no_task_match",
                )
            if resolved.is_ambiguous:
                # Task-level ambiguity → propagate
                return ResourceTarget(
                    operation=req.operation,
                    resource_type=req.resource_type,
                    hint=req.hint,
                    is_ambiguous=True,
                    candidates=resolved.candidates,
                    confidence=resolved.confidence,
                    match_reason="ambiguous_task",
                )
            task = next((t for t in tasks if t.task_id == resolved.task_id), None)
            if task:
                return self._find_in_task(req, task, artifact_type)

        # 3. No hint, no task_id — look across ALL tasks' artifacts
        return self._find_across_tasks(req, tasks, artifact_type)

    # ── artifact lookup ──────────────────────────────────────────

    @staticmethod
    def _find_in_task(
        req: ResourceRequest,
        task: Task,
        artifact_type: str,
    ) -> ResourceTarget:
        matches = [
            a for a in task.artifacts
            if a.artifact_type == artifact_type and a.resource_id
        ]
        if not matches:
            return ResourceTarget(
                operation=req.operation,
                resource_type=req.resource_type,
                task_id=task.task_id,
                match_reason="no_artifact_in_task",
            )
        if len(matches) == 1:
            return ResourceTarget(
                operation=req.operation,
                resource_type=req.resource_type,
                resource_id=matches[0].resource_id,
                task_id=task.task_id,
                confidence=0.90,
                match_reason="artifact_found",
            )
        # Multiple artifacts of same type in one task: pick newest
        newest = max(matches, key=lambda a: a.created_at)
        return ResourceTarget(
            operation=req.operation,
            resource_type=req.resource_type,
            resource_id=newest.resource_id,
            task_id=task.task_id,
            confidence=0.70,
            match_reason="artifact_found_multiple",
        )

    @staticmethod
    def _find_across_tasks(
        req: ResourceRequest,
        tasks: list[Task],
        artifact_type: str,
    ) -> ResourceTarget:
        """No hint given — search all tasks for the resource type."""
        matches: list[tuple[str, str]] = []  # (resource_id, task_id)
        for t in tasks:
            for a in t.artifacts:
                if a.artifact_type == artifact_type and a.resource_id:
                    matches.append((a.resource_id, t.task_id))

        if not matches:
            return ResourceTarget(
                operation=req.operation,
                resource_type=req.resource_type,
                match_reason="no_artifact_anywhere",
            )

        if len(matches) == 1:
            rid, tid = matches[0]
            return ResourceTarget(
                operation=req.operation,
                resource_type=req.resource_type,
                resource_id=rid,
                task_id=tid,
                confidence=0.60,
                match_reason="single_artifact_found",
            )

        # Multiple matches across tasks → ambiguous
        return ResourceTarget(
            operation=req.operation,
            resource_type=req.resource_type,
            is_ambiguous=True,
            candidates=[rid for rid, _ in matches],
            confidence=0.30,
            match_reason="ambiguous_multiple_artifacts",
        )
