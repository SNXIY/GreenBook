"""Unified Context assembly for a user turn.

ContextAssembler reuses the canonical ContextBuilder (which joins
Conversation / Task / Execution / Artifact / Memory) and then applies a
task-scoped Fast Path trim.  It never depends on a single "active" task:
completed Tasks remain referenceable, and one Task's artifacts/resources are
never exposed inside another Task's scoped view.  Full chat history, execution
logs, lease and checkpoint state are deliberately excluded.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from ..context.builder import ContextBuilder
from ..context.models import ContextSnapshot, DerivedConversationContext
from ..context.projection import derive_conversation_context
from .models import AssembledTurnContext, TurnBudget

# Execution statuses worth surfacing to a Fast Path turn.  Internal diagnostic
# states (RUNNING step log, checkpoint/lease) are never forwarded.
_PENDING_STATUSES = {"PENDING", "QUEUED", "SUBMITTED", "RUNNING", "RESULT_UNKNOWN"}


class ContextAssembler:
    """Build and trim a bounded per-turn working set."""

    def __init__(
        self,
        context_builder: ContextBuilder | Any | None = None,
        *,
        budget: TurnBudget | None = None,
    ) -> None:
        self._builder = context_builder or ContextBuilder()
        self._budget = budget or TurnBudget()

    async def assemble(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        timezone: str = "Asia/Shanghai",
        session: Any | None = None,
        history: Sequence[Mapping[str, Any]] | None = None,
        current_command: Any | None = None,
        current_goal: Any | None = None,
        focus_task_ids: Sequence[str] | None = None,
        run_id: str = "",
        memory_recall: bool = False,
        user_input: str = "",
    ) -> AssembledTurnContext:
        """Return the canonical snapshot plus task-scoped Fast Path views."""

        snapshot = await self._build_snapshot(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=timezone,
            session=session,
            history=history,
            current_command=current_command,
            current_goal=current_goal,
            run_id=run_id,
            memory_recall=memory_recall,
        )
        focus_ids = self._resolve_focus(
            snapshot,
            session=session,
            explicit=focus_task_ids,
        )
        derived_context = derive_conversation_context(
            snapshot,
            user_input=user_input,
            focus_task_ids=focus_ids,
            resource_limit=self._budget.max_scoped_resources,
            objective_limit=self._budget.max_objectives,
            outcome_limit=self._budget.max_verified_outcomes,
        )
        (
            selected_tasks,
            selected_objectives,
            selected_artifacts,
            selected_resources,
            selected_executions,
        ) = self._scope(snapshot, focus_ids, derived_context)
        return AssembledTurnContext(
            conversation_id=snapshot.conversation_id or conversation_id,
            user_id=snapshot.user_id or user_id,
            tenant_id=snapshot.tenant_id or tenant_id,
            timezone=snapshot.timezone or timezone,
            snapshot=snapshot,
            derived_context=derived_context,
            focus_task_ids=focus_ids,
            selected_tasks=selected_tasks,
            selected_objectives=selected_objectives,
            selected_artifacts=selected_artifacts,
            selected_resources=selected_resources,
            selected_executions=selected_executions,
            budget=self._budget,
        )

    async def _build_snapshot(self, **kwargs: Any) -> ContextSnapshot:
        build = getattr(self._builder, "build", None)
        if not callable(build):
            return ContextSnapshot()
        try:
            parameters = inspect.signature(build).parameters.values()
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            parameter_names = {parameter.name for parameter in parameters}
            accepts_run_id = any(
                parameter.name == "run_id"
                or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            if not accepts_run_id:
                kwargs.pop("run_id", None)
            if not accepts_kwargs and "memory_recall" not in parameter_names:
                kwargs.pop("memory_recall", None)
        except (TypeError, ValueError):
            pass
        value = build(**kwargs)
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _resolve_focus(
        snapshot: ContextSnapshot,
        *,
        session: Any,
        explicit: Sequence[str] | None,
    ) -> list[str]:
        """Derive conversation focus without trusting DB updated_at recency.

        Priority: explicit caller focus (from resolved target) -> session
        bindings -> the conversation's own tasks (so completed Tasks stay
        referenceable).  A list is returned because a turn may legitimately
        span more than one Task; there is never a single implicit choice.
        """
        seen: list[str] = []
        for value in (explicit or ()):
            identifier = str(value or "").strip()
            if identifier and identifier not in seen:
                seen.append(identifier)
        if not seen and session is not None:
            for field in ("active_task_id",):
                identifier = str(getattr(session, field, "") or "").strip()
                if identifier and identifier not in seen:
                    seen.append(identifier)
        if not seen:
            for task in snapshot.active_tasks:
                identifier = str(task.get("task_id") or "").strip()
                if identifier and identifier not in seen:
                    seen.append(identifier)
        return seen[:6]

    def _scope(
        self,
        snapshot: ContextSnapshot,
        focus_ids: list[str],
        derived_context: DerivedConversationContext | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Trim tasks/objectives/artifacts/resources/executions to the focused set.

        Objectives are prioritized: pending/WAITING first, then a small summary
        of completed history.  Artifacts/resources/executions are scoped to the
        focused Tasks and their pending Objectives; irrelevant completed history
        is excluded (budgeted).
        """
        derived = derived_context or DerivedConversationContext()
        has_reference = bool(derived.reference_evidence)
        derived_task_ids = {
            str(item.get("task_id") or "")
            for item in (
                list(derived.relevant_resources)
                + list(derived.relevant_objectives)
            )
            if isinstance(item, Mapping) and item.get("task_id")
        }
        # An explicit reference narrows the whole shared package.  A missing
        # match intentionally produces an empty package so the resolver can
        # return NOT_FOUND instead of silently falling back to active/latest.
        scope_ids = derived_task_ids if has_reference else set(focus_ids)
        focus = set(scope_ids)
        tasks = [
            dict(item)
            for item in snapshot.active_tasks
            if (not has_reference and not focus)
            or str(item.get("task_id") or "") in focus
        ]
        terminal_resources = {
            (
                str(item.get("resource_kind") or item.get("kind") or "").upper(),
                str(item.get("resource_id") or item.get("id") or ""),
            )
            for item in derived.relevant_resources
            if str(item.get("lifecycle") or "").upper() != "CURRENT"
        }
        # Keep historical cards available to the derived projection, but do
        # not leave terminal resource refs inside the canonical resolver
        # candidate Task.  This prevents an explicitly named old Schedule
        # from becoming a current mutation target.
        for task in tasks:
            refs = []
            for ref in task.get("resource_index") or ():
                if not isinstance(ref, Mapping):
                    continue
                key = (
                    str(ref.get("resource_kind") or ref.get("kind") or "").upper(),
                    str(ref.get("resource_id") or ref.get("id") or ""),
                )
                if key not in terminal_resources:
                    refs.append(ref)
            task["resource_index"] = refs
        tasks = tasks[: self._budget.max_focus_tasks]

        # ── Objectives: pending first, then a capped completed summary ──
        pending_objectives: list[dict[str, Any]] = []
        completed_summary: list[dict[str, Any]] = []
        objective_task_ids: set[str] = set()
        for task in tasks:
            task_id = str(task.get("task_id") or "")
            for objective in task.get("objectives") or ():
                status = str(objective.get("status") or "").upper()
                constraints = dict(objective.get("constraints") or {})
                is_superseded = str(constraints.get("mutation_status") or "").upper() == "SUPERSEDED"
                entry = dict(objective)
                entry["task_id"] = task_id
                objective_task_ids.add(task_id)
                if is_superseded or status in {"COMPLETED", "CANCELLED", "SUPERSEDED"}:
                    completed_summary.append(entry)
                else:
                    pending_objectives.append(entry)
        selected_objectives = (
            pending_objectives
            + completed_summary[: self._budget.max_completed_objective_summary]
        )[: self._budget.max_objectives]

        scoped_artifacts: list[dict[str, Any]] = []
        scoped_resources: list[dict[str, Any]] = []
        for item in snapshot.artifacts:
            if (has_reference or focus) and str(item.get("task_id") or "") not in focus:
                continue
            entry = dict(item)
            body = str(entry.get("body") or entry.get("content") or "")
            if len(body) > self._budget.artifact_max_chars:
                entry["body"] = body[: self._budget.artifact_max_chars]
            scoped_artifacts.append(entry)
            if item.get("resource_id") and item.get("resource_kind"):
                scoped_resources.append({
                    "kind": str(item.get("resource_kind")).upper(),
                    "id": str(item.get("resource_id")),
                    "resource_id": str(item.get("resource_id")),
                    "resource_kind": str(item.get("resource_kind")).upper(),
                    "artifact_id": item.get("artifact_id"),
                    "task_id": item.get("task_id"),
                    "label": item.get("title") or item.get("summary"),
                })
        if derived.relevant_resources:
            for item in derived.relevant_resources:
                if (has_reference or focus) and str(item.get("task_id") or "") not in focus:
                    continue
                entry = dict(item)
                entry.setdefault("id", entry.get("resource_id"))
                entry.setdefault("kind", entry.get("resource_kind"))
                scoped_resources.append(entry)
        elif not has_reference:
            for item in snapshot.available_resources:
                if focus and str(item.get("task_id") or "") not in focus:
                    continue
                scoped_resources.append(dict(item))

        scoped_executions = [
            dict(item)
            for item in snapshot.execution_states
            if ((not has_reference and not focus) or str(item.get("task_id") or "") in focus)
            and str(item.get("status") or "").upper() in _PENDING_STATUSES
        ]

        return (
            tasks,
            selected_objectives,
            scoped_artifacts[: self._budget.max_scoped_artifacts],
            scoped_resources[: self._budget.max_scoped_resources],
            scoped_executions[: self._budget.max_scoped_executions],
        )


__all__ = ["ContextAssembler"]
