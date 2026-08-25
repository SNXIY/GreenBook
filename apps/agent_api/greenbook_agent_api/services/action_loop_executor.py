"""Wiring layer that connects ActionLoop to the real Runtime collaborators.

ActionLoopExecutor owns the production decision-maker (one LLM structured call
per iteration), the Task persistence boundary, and the read/write handlers
that reuse the ConversationRuntimeAdapter's durable submission path.  It does
not own Queue/Worker/Lease/Retry/Checkpoint — those stay in the Runtime.
"""

from __future__ import annotations

import inspect
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from greenbook_agent_core.actionloop import (
    ActionDecision,
    ActionLoop,
)
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.llm_compat import structured_call
from greenbook_agent_core.task.models import TaskRevision, TaskRevisionType
from greenbook_agent_core.task.manager import TaskConfirmationConflictError
from greenbook_agent_core.task.objective_reducer import (
    mutation_conflicts,
    mutation_details,
    mutation_execution_state,
    mutation_objective_details,
    mutation_objective_is_superseded,
    supersede_mutation_objective,
)
from greenbook_agent_core.task.semantic_confirmation import confirmation_identity
from greenbook_agent_core.turn import ContextAssembler

from .conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
    _SEMANTIC_ACTION_CAPABILITIES,
    _bind_semantic_action_resource,
    _delta_resource_id,
)
from .retrieval_synthesis_projection import build_retrieval_interaction

_ACTION_LOOP_SYSTEM_PROMPT = """You are the GreenBook ActionLoop.

You drive ONE long-lived Task to verified completion by choosing the next
semantic action each turn.  You never invent user text, never run a tool, and
never persist state.  Return exactly one JSON object matching the
action_decision schema.

Decide only from the supplied Task observation: objective/objectives,
artifacts, resources, execution statuses, and (when present) an action plan.

Decision semantics:
- CALL_TOOL: execute one canonical semantic_action from the active capability
  catalog (SEARCH_POSTS, GET_POST, GET_DRAFT, LIST_DRAFTS, GET_SCHEDULE,
  LIST_OWN_POSTS, UPDATE_DRAFT, DELETE_DRAFT, UPDATE_SCHEDULE,
  CANCEL_SCHEDULE, PUBLISH_NOW, CREATE_SCHEDULE, CREATE_DRAFT).  Do not set
  tool_name; the runtime resolves it.
- GENERATE_CONTENT: create a draft (semantic_action CREATE_DRAFT).
- CLARIFY: the Task objective is ambiguous or missing required arguments.
- WAIT: an external write is in flight; do not reason over it.
- REPLAN: revise plan_steps after a failure or a pivot (dependency changes).
- FINISH: only when every objective is already satisfied by a real, verified
  resource in the observation (e.g. a SCHEDULE resource exists for a
  scheduled-publication objective, a DRAFT for a draft objective).

Never emit FINISH merely because a tool call returned or a queue accepted a
write.  Choose each step dynamically from the current observation — there is no
fixed Search->Summarize->Create->Schedule order.

Write objective, every description, and arguments in the same language as the
user's message (Chinese input -> Chinese).

For UPDATE_DRAFT, arguments MUST carry the concrete replacement: `content`
(the full rewritten body reflecting the requested change, written by you in
the user's language) and/or `title`.  Never emit an UPDATE_DRAFT with only
draft_id and no mutation field — an empty update would be rejected and the
user's edit would be lost.  Read the current draft from the observation when
available so you can rewrite it coherently.
"""

_UPDATE_CONTENT_PROMPT = """You rewrite a GreenBook draft body per a user edit.

Return exactly one JSON object {"content": "..."}. The content is the FULL
rewritten draft body in the same language as the user request. Apply the
requested change to the existing content and return the complete updated body,
not a diff or a partial excerpt. If no existing content is available, write a
coherent body that satisfies the user request.
"""


class _ActionLoopTaskStore:
    """Minimal Task persistence boundary backed by the TaskManager."""

    def __init__(self, task_manager: Any, conversation_id: str, user_id: str, tenant_id: str) -> None:
        self._manager = task_manager
        self._conversation_id = conversation_id
        self._user_id = user_id
        self._tenant_id = tenant_id

    async def _record(self, task: Any, event: str, detail: Any) -> None:
        task_id = str(getattr(task, "task_id", "") or "")
        manager = self._manager
        if event == "suspend":
            set_status = getattr(manager, "set_status", None)
            if callable(set_status):
                value = set_status(task_id, "WAITING_EXTERNAL")
                if inspect.isawaitable(value):
                    await value
            return
        if event == "wait_human":
            wait = getattr(manager, "wait_for_human", None)
            if callable(wait):
                value = wait(task_id, reason=str(detail or ""))
                if inspect.isawaitable(value):
                    await value
            return
        if event == "finish":
            complete = getattr(manager, "complete_task", None) or getattr(manager, "complete", None)
            if callable(complete):
                value = complete(task_id)
                if inspect.isawaitable(value):
                    await value
            return
        # append_action / record history
        append = getattr(manager, "append_action", None)
        if callable(append):
            value = append(task_id, str(event))
            if inspect.isawaitable(value):
                await value

    async def persist_objectives(self, task: Any) -> None:
        """Persist the Task (including reduced Objective statuses) to storage."""
        manager = self._manager
        if manager is None:
            return
        repository = getattr(manager, "repository", None)
        repo = repository() if callable(repository) else getattr(manager, "_repository", None)
        update = getattr(repo, "update", None)
        if callable(update):
            try:
                await update(task, expected_version=getattr(task, "version", None))
            except Exception:  # noqa: BLE001 - objective projection is best-effort
                pass

    async def persist_mutation_plan(self, task: Any, changes: Any) -> None:
        """Persist an explicit cross-turn mutation as an auditable Task revision.

        The ActionLoop itself is intentionally in-memory, while a queued write
        may resume in another callback/process lifetime.  Task revisions are
        already the durable audit boundary for changes to a long-lived Task;
        reuse that boundary for the immutable resolved mutation request.  The
        current Execution/Operation stores remain the progress and side-effect
        authorities.
        """
        values = []
        for change in changes or ():
            desired = dict(getattr(change, "desired_changes", None) or {})
            action = str(desired.get("semantic_action") or "").upper()
            if not action:
                continue
            if hasattr(change, "model_dump"):
                values.append(change.model_dump(mode="json"))
            else:
                values.append({
                    "operation": str(getattr(change, "operation", "") or ""),
                    "target_reference": dict(getattr(change, "target_reference", None) or {}),
                    "desired_changes": desired,
                })
        if not values:
            return
        manager = self._manager
        get_task = getattr(manager, "get_task", None)
        repository = getattr(manager, "repository", None)
        repo = repository() if callable(repository) else getattr(manager, "_repository", None)
        update = getattr(repo, "update", None)
        if not callable(get_task) or not callable(update):
            return

        # Read the latest Task snapshot before appending.  Resource completion
        # and execution projection can update the same Task between turns.
        latest = get_task(str(getattr(task, "task_id", "") or ""))
        latest = await latest if inspect.isawaitable(latest) else latest
        if latest is None:
            return
        payload = {"kind": "ACTION_LOOP_MUTATION_PLAN", "task_changes": values}
        existing = next(
            (
                revision for revision in reversed(getattr(latest, "revisions", ()) or ())
                if dict(getattr(revision, "payload", None) or {}).get("kind")
                == "ACTION_LOOP_MUTATION_PLAN"
            ),
            None,
        )
        if existing is not None and dict(getattr(existing, "payload", None) or {}) == payload:
            return
        latest.revisions.append(
            TaskRevision(
                task_id=str(getattr(latest, "task_id", "") or ""),
                type=TaskRevisionType.MODIFY_GOAL,
                payload=payload,
                previous_version=int(getattr(latest, "version", 0) or 0),
            )
        )
        try:
            updated = update(
                latest,
                expected_version=getattr(latest, "version", None),
            )
            updated = await updated if inspect.isawaitable(updated) else updated
        except Exception:  # noqa: BLE001 - the write path still owns execution safety
            return
        if updated is not None:
            # Keep the current loop's version aligned with the successful
            # revision write without replacing its newer in-memory Objective
            # projection with the separately loaded snapshot.
            task.version = getattr(updated, "version", getattr(task, "version", 0))
            task.revisions = list(getattr(updated, "revisions", ()) or ())

    async def record_mutation_submission(
        self,
        task: Any,
        *,
        action: str,
        arguments: Mapping[str, Any],
        execution_id: str,
        objective_id: str = "",
    ) -> None:
        """Record target-to-execution correlation for durable continuation.

        The revision stores no execution status.  On resume the status is read
        from the existing TaskExecutionRef projection, so Execution remains the
        sole progress authority and this is only an auditable correlation.
        """
        resource_id = str(
            arguments.get("schedule_id")
            or arguments.get("draft_id")
            or arguments.get("post_id")
            or arguments.get("resource_id")
            or ""
        )
        if not resource_id or not execution_id:
            return
        manager = self._manager
        get_task = getattr(manager, "get_task", None)
        repository = getattr(manager, "repository", None)
        repo = repository() if callable(repository) else getattr(manager, "_repository", None)
        update = getattr(repo, "update", None)
        if not callable(get_task) or not callable(update):
            return
        latest = get_task(str(getattr(task, "task_id", "") or ""))
        latest = await latest if inspect.isawaitable(latest) else latest
        if latest is None:
            return
        payload = {
            "kind": "ACTION_LOOP_MUTATION_SUBMISSION",
            "action": str(action or "").upper(),
            "resource_id": resource_id,
            "execution_id": str(execution_id),
        }
        if objective_id:
            payload["objective_id"] = str(objective_id)
            submitted_objective = next(
                (
                    item for item in (getattr(latest, "objectives", ()) or ())
                    if str(getattr(item, "objective_id", "") or "")
                    == str(objective_id)
                ),
                None,
            )
            if submitted_objective is not None:
                details = mutation_objective_details(submitted_objective)
                payload["mutation_domain"] = details["domain"]
                payload["mutation_expected_state"] = dict(details["expected_state"])
                if details["mutation_identity"]:
                    payload["mutation_identity"] = details["mutation_identity"]
        if any(
            dict(getattr(revision, "payload", None) or {}) == payload
            for revision in (getattr(latest, "revisions", ()) or ())
        ):
            return
        latest.revisions.append(
            TaskRevision(
                task_id=str(getattr(latest, "task_id", "") or ""),
                type=TaskRevisionType.MODIFY_GOAL,
                payload=payload,
                previous_version=int(getattr(latest, "version", 0) or 0),
            )
        )
        try:
            updated = update(latest, expected_version=getattr(latest, "version", None))
            updated = await updated if inspect.isawaitable(updated) else updated
        except Exception:  # noqa: BLE001 - execution submission already owns safety
            return
        if updated is not None:
            task.version = getattr(updated, "version", getattr(task, "version", 0))
            task.revisions = list(getattr(updated, "revisions", ()) or ())

    async def _record_resource(
        self,
        task: Any,
        resource_id: str,
        resource_kind: str,
        title: str = "",
        content: str = "",
        objective_id: str = "",
    ) -> None:
        task_id = str(getattr(task, "task_id", "") or "")
        add = getattr(self._manager, "add_resource", None)
        # Surface the resource in the in-memory Task too, so the loop's
        # ResultComposer can read current-Task evidence (resource_index) for
        # readiness — otherwise the composer never sees the recorded facts and a
        # GROUNDED_SYNTHESIS loop burns its budget re-reading candidates.
        index = getattr(task, "resource_index", None)
        if isinstance(index, list):
            key = (str(resource_id), str(resource_kind).upper())
            if not any(
                (
                    str(item.get("resource_id") if isinstance(item, Mapping) else getattr(item, "resource_id", "") or ""),
                    str(item.get("resource_kind") if isinstance(item, Mapping) else getattr(item, "resource_kind", "") or "").upper(),
                ) == key
                for item in index
            ):
                index.append({
                    "resource_id": str(resource_id),
                    "resource_kind": str(resource_kind),
                    "objective_id": str(objective_id) if objective_id else None,
                    "title": str(title or ""),
                    "content": str(content or ""),
                })
        # Ownership production: bind this verified resource to the Objective that
        # initiated the action, so satisfaction is strictly Objective-scoped.
        if objective_id:
            for objective in getattr(task, "objectives", ()) or ():
                if str(getattr(objective, "objective_id", "")) != objective_id:
                    continue
                owned = list(getattr(objective, "related_resource_ids", ()) or ())
                if str(resource_id) not in owned:  # idempotent (replay-safe)
                    owned.append(str(resource_id))
                objective.related_resource_ids = owned
                break
        if not callable(add):
            return
        try:
            await add(
                task_id,
                resource_id=str(resource_id),
                resource_kind=str(resource_kind),
                title=str(title or ""),
                objective_id=objective_id or None,
            )
        except Exception:  # noqa: BLE001 - best-effort resource recording
            pass


class ActionLoopExecutor:
    """Run the ActionLoop for one Task against the production Runtime."""

    def __init__(
        self,
        *,
        adapter: ConversationRuntimeAdapter | Any,
        context_assembler: ContextAssembler | Any | None = None,
        task_manager: Any | None = None,
        llm: Any | None = None,
        model: str = "",
        max_iterations: int = 8,
        action_loop: ActionLoop | Any | None = None,
        decision_event_store: Any | None = None,
    ) -> None:
        self._adapter = adapter
        self._context_assembler = context_assembler
        self._task_manager = task_manager
        self._llm = llm
        self._model = model
        self._max_iterations = max(1, max_iterations)
        self._decision_event_store = decision_event_store
        from greenbook_agent_core.actionloop.result import ResultComposer

        self._action_loop = action_loop or ActionLoop(
            decision_maker=self._decision_maker,
            read_handler=self._read_handler,
            write_submitter=self._write_submitter,
            context_assembler=self._context_assembler,
            decision_observer=self._record_decision,
            result_composer=ResultComposer(),
            llm=self._llm,
            model=self._model,
            max_iterations=self._max_iterations,
        )

    @staticmethod
    def _observe_actionloop(
        stage: str,
        *,
        task_id: str,
        trace_id: str,
        conversation_id: str,
        status: str = "",
        iterations: int = 0,
    ) -> None:
        try:
            from greenbook_agent_core.observability.bus import observability

            ob = observability()
            if iterations:
                ob.actionloop_iterations().observe(float(iterations))
            ob.record_trace(
                "actionloop_" + stage,
                trace_id=trace_id,
                conversation_id=conversation_id,
                task_id=task_id,
                status=status,
            )
        except Exception:  # noqa: BLE001
            pass

    def _record_decision(
        self,
        *,
        run_id: str,
        task_id: str,
        objective_id: str,
        iteration: int,
        decision: Any,
        decision_source: str = "LLM",
        llm_called: bool = True,
    ) -> None:
        """Persist one ActionLoop decision durably (best-effort, never raises).

        Reuses the existing durable execution-event store: the row is keyed by
        run_id so all decisions for a run are recoverable after a process
        restart.  Pure observability — no execution state machine reads it.
        """
        from greenbook_agent_core.observability.run_metrics import record_stage

        record_stage("actionloop_decision_ready", run_id=run_id)
        store = self._decision_event_store
        if store is None:
            return
        try:
            from datetime import UTC, datetime

            from greenbook_agent_core.execution.events import (
                EventType,
                ExecutionEvent,
            )

            source = str(decision_source or "LLM").upper()
            called = bool(llm_called and source == "LLM")
            raw_usage = dict(getattr(self, "_latest_actionloop_llm", {}) or {}) if called else {}
            llm_usage = {
                "category": "ACTIONLOOP" if called else "",
                "latency_ms": int(raw_usage.get("latency_ms", 0) or 0) if called else 0,
                "input_tokens": int(raw_usage.get("input_tokens", 0) or 0) if called else 0,
                "output_tokens": int(raw_usage.get("output_tokens", 0) or 0) if called else 0,
            }
            store.append(ExecutionEvent(
                execution_id=run_id,
                event_type=EventType.ACTION_LOOP_DECISION,
                payload={
                    "kind": "actionloop_decision",
                    "run_id": run_id,
                    "task_id": task_id,
                    "objective_id": objective_id,
                    "iteration": int(iteration),
                    "decision_type": str(getattr(decision, "decision", "")),
                    "semantic_action": str(
                        getattr(decision, "semantic_action", "") or ""
                    ),
                    "arguments": dict(getattr(decision, "arguments", None) or {}),
                    "reason": str(getattr(decision, "reason", "") or ""),
                    "decision_source": source,
                    "llm_called": called,
                    "llm_latency_ms": llm_usage["latency_ms"],
                    "llm_input_tokens": llm_usage["input_tokens"],
                    "llm_output_tokens": llm_usage["output_tokens"],
                    "llm": llm_usage,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            ))
        except Exception:  # noqa: BLE001 - observability is best-effort
            return

    async def run(
        self,
        *,
        task: Any,
        command: Any,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
        session: Any,
        timezone: str,
        mcp: Any,
        auth: Any,
        activity_callback: Any = None,
        completion_callback: Any = None,
        boundary: Any = None,
    ) -> Any:
        from greenbook_agent_core.observability.run_metrics import record_stage_once

        if _semantic_confirmation_blocks_task(task):
            return _semantic_confirmation_waiting_result(
                task=task,
                run_id=run_id,
                trace_id=trace_id,
            )

        record_stage_once("actionloop_entry", run_id=run_id)
        store = _ActionLoopTaskStore(
            self._task_manager, conversation_id, user_id, tenant_id
        )
        request = _LoopRequest(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            run_id=run_id,
            trace_id=trace_id,
            session=session,
            timezone=timezone,
            mcp=mcp,
            auth=auth,
            llm=self._llm,
            model=self._model,
            activity_callback=activity_callback,
            completion_callback=completion_callback,
        )
        self._observe_actionloop(
            "started", task_id=str(getattr(task, "task_id", "") or ""),
            trace_id=trace_id, conversation_id=conversation_id,
        )
        from greenbook_agent_core.observability.run_metrics import (
            record_actionloop,
            record_tool,
            run_scope,
        )
        with run_scope(run_id):
            result = await self._action_loop.run(
                task,
                command,
                request=request,
                task_store=store,
                boundary=boundary,
            )
        record_actionloop(int(getattr(result, "iterations", 0) or 0), run_id=run_id)
        self._observe_actionloop(
            "finished", task_id=str(getattr(task, "task_id", "") or ""),
            trace_id=trace_id, conversation_id=conversation_id,
            status=str(getattr(result, "status", "") or ""),
            iterations=int(getattr(result, "iterations", 0) or 0),
        )
        runtime_result = _to_runtime_result(result)
        tool_results = (runtime_result.partial_results or {}).get("tool_results") or []
        capabilities = {
            str(item).upper()
            for item in (getattr(command, "required_capabilities", ()) or ())
        }
        semantic_operation = str(
            getattr(command, "semantic_operation", "")
            or getattr(getattr(command, "resolved_semantics", None), "semantic_operation", "")
            or ""
        ).upper()
        synthesis_requested = (
            "ANALYZE_CONTENT_PATTERNS" in capabilities
            or semantic_operation in {"SUMMARIZE", "SUMMARIZE_POST", "SUMMARIZE_CONTENT"}
        )
        interaction, safe_message = await build_retrieval_interaction(
            request=(
                str(getattr(command, "raw_input", "") or "")
                or str(getattr(command, "requested_goal", "") or "")
            ),
            tool_results=tool_results,
            synthesis_requested=synthesis_requested,
            llm=self._llm,
            model=self._model,
        )
        if interaction is not None:
            partial_results = dict(runtime_result.partial_results or {})
            partial_results["user_facing_interaction"] = interaction
            runtime_result.partial_results = partial_results
            runtime_result.content = safe_message
            runtime_result.summary = safe_message
        if boundary is not None:
            runtime_result.partial_results = dict(runtime_result.partial_results or {})
            runtime_result.partial_results["execution_boundary"] = boundary.as_dict()
        return runtime_result

    async def run_for_command(
        self,
        *,
        command: Any,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
        session: Any,
        timezone: str,
        mcp: Any,
        auth: Any,
        activity_callback: Any = None,
        completion_callback: Any = None,
        boundary: Any = None,
    ) -> Any:
        """Load a durable Task for the command (create one when needed) and run."""
        from greenbook_agent_core.observability.run_metrics import record_stage

        record_stage("actionloop_task_prepare_start", run_id=run_id)
        task = await self._load_or_create_task(
            command,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            session=session,
            run_id=run_id,
            turn_id=trace_id,
        )
        task = await self._ensure_mutation_objectives(task, command)
        task = await self._admit_task_if_needed(task)
        if _semantic_confirmation_blocks_task(task):
            return _semantic_confirmation_waiting_result(
                task=task,
                run_id=run_id,
                trace_id=trace_id,
            )
        record_stage("actionloop_task_ready", run_id=run_id)
        return await self.run(
            task=task,
            command=command,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            run_id=run_id,
            trace_id=trace_id,
            session=session,
            timezone=timezone,
            mcp=mcp,
            auth=auth,
            activity_callback=activity_callback,
            completion_callback=completion_callback,
            boundary=boundary,
        )

    async def prepare_for_confirmation(
        self,
        *,
        command: Any,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        session: Any,
        run_id: str = "",
        turn_id: str = "",
    ) -> Any:
        """Materialize canonical Task/Objectives without entering ActionLoop.

        Semantic Confirmation needs the same durable Task projection that a
        later ActionLoop resume will consume.  This method deliberately stops
        before ``run``: no observation, decision, read, or write is allowed
        while the Task is pending confirmation.
        """

        if self._task_manager is None:
            raise RuntimeError("Semantic confirmation requires a durable Task manager.")
        task = await self._load_or_create_task(
            command,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            session=session,
            run_id=run_id,
            turn_id=turn_id,
        )
        task = await self._ensure_mutation_objectives(task, command)
        if getattr(command, "task_changes", None):
            store = _ActionLoopTaskStore(
                self._task_manager,
                conversation_id,
                user_id,
                tenant_id,
            )
            await store.persist_mutation_plan(task, command.task_changes)
        # Objective and mutation-plan persistence can each advance Task.version;
        # reload the canonical projection before the confirmation CAS.
        get_task = getattr(self._task_manager, "get_task", None)
        if callable(get_task):
            latest = get_task(
                str(getattr(task, "task_id", "") or ""),
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
            latest = await latest if inspect.isawaitable(latest) else latest
            if latest is not None:
                task = latest
        return task

    async def _ensure_mutation_objectives(self, task: Any, command: Any) -> Any:
        """Create one new Objective for each new cross-turn mutation.

        Target resolution first identifies the historical Objective/resource.
        The mutation is a new user outcome, so it receives a new Objective id
        while retaining the exact ResourceBinding.  This keeps old terminal
        Objectives immutable and lets the existing ActionLoop/Durable Runtime
        carry the new owner through submission and observation.
        """

        manager = self._task_manager
        if manager is None or task is None:
            return task
        changes = list(getattr(command, "task_changes", None) or ())
        pending: list[Any] = []
        for index, change in enumerate(changes):
            operation = str(getattr(getattr(change, "operation", None), "value", getattr(change, "operation", "")) or "").upper()
            desired = dict(getattr(change, "desired_changes", None) or {})
            semantic_action = str(
                desired.get("semantic_action") or desired.get("semantic_operation") or ""
            ).strip().upper()
            # ``SCHEDULE_PUBLISH`` is the capability spelling emitted by the
            # semantic contract; ActionLoop executes the same outcome through
            # its canonical CREATE_SCHEDULE action.  Normalize before the
            # mutation admission filter so the new Objective is not skipped.
            if semantic_action == "SCHEDULE_PUBLISH":
                semantic_action = "CREATE_SCHEDULE"
                desired["semantic_action"] = semantic_action
                change = change.model_copy(update={"desired_changes": desired})
            if operation == "CREATE_TASK" or semantic_action not in _SEMANTIC_ACTION_CAPABILITIES:
                continue
            pending.append((index, change, semantic_action))
        if not pending:
            return task

        # The command has already crossed the canonical resolver boundary in
        # TurnCoordinator.  Bind each delta against its resolved historical
        # Objective before allocating the new action Objective.
        updated_changes = list(changes)
        changed = False
        resolved_items = list(
            getattr(getattr(command, "resolved_semantics", None), "items", None)
            or ()
        )

        def resolved_item_for(index: int, bound: Any, action: str) -> Any | None:
            """Find the matching resolved item without changing target identity."""

            target = dict(getattr(bound, "target_reference", None) or {})
            resource_id = str(
                target.get("resource_id")
                or target.get("draft_id")
                or target.get("schedule_id")
                or target.get("post_id")
                or ""
            )
            target_objective_id = str(
                target.get("target_objective_id")
                or target.get("objective_id")
                or ""
            )
            matches: list[Any] = []
            for item in resolved_items:
                item_action = str(getattr(item, "operation", "") or "").upper()
                if item_action != action:
                    continue
                item_target = dict(getattr(item, "target_reference", None) or {})
                item_resource_id = str(
                    item_target.get("resource_id")
                    or item_target.get("draft_id")
                    or item_target.get("schedule_id")
                    or item_target.get("post_id")
                    or ""
                )
                item_objective_id = str(
                    item_target.get("target_objective_id")
                    or item_target.get("objective_id")
                    or ""
                )
                if resource_id and item_resource_id == resource_id:
                    matches.append(item)
                elif target_objective_id and item_objective_id == target_objective_id:
                    matches.append(item)
            if matches:
                return matches[0]
            if len(resolved_items) == len(changes) and index < len(resolved_items):
                return resolved_items[index]
            if len(pending) == 1:
                return resolved_items[0] if resolved_items else getattr(
                    command, "resolved_semantics", None
                )
            return None

        for index, change, semantic_action in pending:
            bound = _bind_semantic_action_resource(change, task)
            desired = dict(bound.desired_changes or {})
            resolved_item = resolved_item_for(index, bound, semantic_action)
            resolved_constraints = dict(
                getattr(resolved_item, "constraints", {}) or {}
            ) if resolved_item is not None else {}
            resolved_publication_intent = str(
                getattr(resolved_item, "publication_intent", None)
                or resolved_constraints.get("publication_intent")
                or ""
            ).strip()
            if resolved_publication_intent:
                # The resolved semantic item is the canonical owner of the
                # publication mode for this mutation.  Preserve it on the
                # durable Objective so ActionLoop qualification cannot turn an
                # explicit immediate publish into an unqualified PUBLISH_NOW.
                desired["publication_intent"] = resolved_publication_intent
            resolved_temporal_kind = str(
                getattr(resolved_item, "temporal_kind", None)
                or resolved_constraints.get("temporal_kind")
                or ""
            ).strip()
            if resolved_temporal_kind:
                desired["temporal_kind"] = resolved_temporal_kind
            if resolved_item is not None and (
                hasattr(resolved_item, "temporal_resolved")
                or "temporal_resolved" in resolved_constraints
            ):
                desired["temporal_resolved"] = bool(
                    getattr(
                        resolved_item,
                        "temporal_resolved",
                        resolved_constraints.get("temporal_resolved", False),
                    )
                )
            canonical_run_at = str(
                getattr(resolved_item, "run_at", None) or ""
            ).strip()
            if canonical_run_at:
                # TemporalResolver is the single time authority. Preserve the
                # original temporal expression for audit, while the durable
                # Objective receives the canonical instant used by
                # Qualification and the Java write.
                desired["run_at"] = canonical_run_at
                constraints = dict(getattr(resolved_item, "constraints", {}) or {})
                desired["timezone"] = str(
                    constraints.get("timezone") or "Asia/Shanghai"
                )
                desired["temporal_kind"] = str(
                    getattr(resolved_item, "temporal_kind", "") or ""
                )
                desired["temporal_resolved"] = bool(
                    getattr(resolved_item, "temporal_resolved", True)
                )
                bound = bound.model_copy(update={"desired_changes": desired})
            identity = f"{getattr(command, 'command_id', '')}:{getattr(bound, 'change_id', '') or index}"
            resource_id = _delta_resource_id(bound)
            metadata = mutation_details(semantic_action, desired, resource_id)
            existing = next(
                (
                    item for item in (getattr(task, "objectives", ()) or ())
                    if str(
                        (getattr(item, "constraints", {}) or {}).get(
                            "mutation_identity", ""
                        )
                    ) == identity
                ),
                None,
            )
            if existing is not None and mutation_objective_is_superseded(existing):
                # Replaying an older command must not resurrect its historical
                # mutation after a newer intent won the conflict.
                desired["objective_id"] = str(getattr(existing, "objective_id", "") or "")
                desired["mutation_objective_id"] = desired["objective_id"]
                updated_changes[index] = bound.model_copy(update={"desired_changes": desired})
                changed = True
                continue

            # An identical desired state already pending/in-flight/verified is
            # the same logical mutation for idempotency purposes.  Reuse its
            # Objective instead of allocating a new OperationLedger key.
            same_state = next(
                (
                    item for item in (getattr(task, "objectives", ()) or ())
                    if not mutation_objective_is_superseded(item)
                    and mutation_objective_details(item)["resource_id"] == resource_id
                    and mutation_objective_details(item)["domain"]
                    == str(metadata["mutation_domain"] or "").upper()
                    and mutation_objective_details(item)["expected_state"]
                    == metadata["mutation_expected_state"]
                    and mutation_execution_state(task, item)
                    in {"PENDING", "INFLIGHT", "UNKNOWN", "COMPLETED"}
                ),
                None,
            )
            if same_state is not None:
                existing = same_state

            supersede_candidates = []
            if existing is None:
                for item in (getattr(task, "objectives", ()) or ()):
                    if mutation_objective_is_superseded(item):
                        continue
                    if not mutation_conflicts(
                        item,
                        {
                            "resource_id": resource_id,
                            "domain": metadata["mutation_domain"],
                            "expected_state": metadata["mutation_expected_state"],
                            "target_objective_id": metadata.get("target_objective_id", ""),
                        },
                    ):
                        continue
                    phase = mutation_execution_state(task, item)
                    if phase == "PENDING":
                        supersede_candidates.append(item)
            if existing is None:
                from greenbook_agent_core.task.models import Objective, TaskRevision, TaskRevisionType
                from greenbook_agent_core.task.objective_compat import objectives_for_capabilities

                capability = _SEMANTIC_ACTION_CAPABILITIES[semantic_action]
                templates = objectives_for_capabilities(
                    [capability],
                    str(getattr(task, "task_id", "") or ""),
                    fallback_intent=semantic_action,
                )
                objective = templates[0] if templates else Objective(
                    task_id=str(getattr(task, "task_id", "") or "")
                )
                objective.objective_id = f"mutation-{uuid.uuid4().hex[:12]}"
                objective.description = str(
                    desired.get("description")
                    or desired.get("instruction")
                    or semantic_action.replace("_", " ").title()
                ).strip()
                objective.intent = semantic_action
                objective.required_capabilities = [capability]
                objective.result_requirement = "RESOURCE_MUTATION"
                objective.constraints = dict(desired)
                objective.constraints["mutation_identity"] = identity
                objective.constraints.update(metadata)
                objective.constraints["mutation_status"] = "ACTIVE"
                if resource_id:
                    objective.related_resource_ids = [resource_id]
                    target = dict(desired.get("resource_target") or {})
                    if target:
                        objective.constraints["target"] = target
                task.objectives.append(objective)
                for old in supersede_candidates:
                    supersede_mutation_objective(
                        task,
                        old,
                        new_objective_id=objective.objective_id,
                        resource_id=resource_id,
                        new_details=metadata,
                    )
                task.revisions.append(
                    TaskRevision(
                        task_id=str(getattr(task, "task_id", "") or ""),
                        type=TaskRevisionType.ADD_GOAL,
                        payload={
                            "kind": "CROSS_TURN_OBJECTIVE_MUTATION",
                            "objective_id": objective.objective_id,
                            "target_objective_id": str(
                                desired.get("target_objective_id")
                                or desired.get("objective_id")
                                or ""
                            ),
                            "semantic_action": semantic_action,
                            "resource_id": resource_id,
                            "mutation_identity": identity,
                            **metadata,
                        },
                        previous_version=int(getattr(task, "version", 0) or 0),
                    )
                )
                existing = objective
            desired["objective_id"] = str(getattr(existing, "objective_id", "") or "")
            desired["mutation_objective_id"] = desired["objective_id"]
            updated_changes[index] = bound.model_copy(update={"desired_changes": desired})
            changed = True

        if not changed:
            return task
        command.task_changes = updated_changes
        repository = getattr(manager, "repository", None)
        repository = repository() if callable(repository) else getattr(manager, "_repository", None)
        update = getattr(repository, "update", None)
        if not callable(update):
            return task
        persisted = update(task, expected_version=getattr(task, "version", None))
        return await persisted if inspect.isawaitable(persisted) else persisted

    async def resume_task(
        self,
        *,
        task_id: str,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str,
        trace_id: str,
        session: Any,
        timezone: str,
        mcp: Any,
        auth: Any,
        activity_callback: Any = None,
        completion_callback: Any = None,
        command: Any = None,
        expected_confirmation_id: str | None = None,
        expected_confirmation_version: int | None = None,
        expected_task_version: int | None = None,
    ) -> Any:
        """Re-drive a Task's ActionLoop from its persisted state after a queued
        write reached a terminal execution.

        Idempotent per terminal completion: if the Task still has a nonterminal
        execution (in-flight or RESULT_UNKNOWN) it is NOT resumed — it waits for
        verification/reconciliation.  Completed objectives/resources are skipped
        by the loop, so a done CREATE_DRAFT is never re-run.
        """
        manager = self._task_manager
        if manager is None:
            return None
        def _debug_resume(stage: str, **payload: Any) -> None:
            try:
                from greenbook_agent_core.command.interpreter import _debug_structured_stage

                _debug_structured_stage(
                    "continuation_" + stage,
                    {
                        "task_id": task_id,
                        "run_id": run_id,
                        "execution_trace_id": trace_id,
                        **payload,
                    },
                )
            except Exception:  # noqa: BLE001 - diagnostics must never affect resume
                pass

        _debug_resume("start", conversation_id=conversation_id)
        get_task = getattr(manager, "get_task", None)
        if not callable(get_task):
            _debug_resume("no_task_manager_get")
            return None
        # A completion callback can wake this continuation while the same
        # process is still committing target->execution correlation.  Wait
        # before loading the Task so this resume cannot carry a stale snapshot
        # past the commit boundary.
        wait_for_submission = getattr(self._action_loop, "wait_for_mutation_submission", None)
        if callable(wait_for_submission):
            await wait_for_submission(task_id)
        task = get_task(task_id, conversation_id=conversation_id, user_id=user_id, tenant_id=tenant_id)
        task = await task if inspect.isawaitable(task) else task
        _debug_resume(
            "task_loaded",
            found=task is not None,
            task_status=str(getattr(getattr(task, "status", None), "value", getattr(task, "status", "")) or ""),
            objective_count=len(getattr(task, "objectives", ()) or ()) if task is not None else 0,
            execution_ref_statuses=[
                str(getattr(ref, "status", "") or "")
                for ref in (getattr(task, "execution_refs", ()) or ())
            ] if task is not None else [],
        )
        if task is None:
            return None
        if not _semantic_confirmation_matches(
            task,
            expected_confirmation_id=expected_confirmation_id,
            expected_confirmation_version=expected_confirmation_version,
            expected_task_version=expected_task_version,
        ):
            _debug_resume("stale_confirmation")
            return _semantic_confirmation_stale_result(
                task=task,
                run_id=run_id,
                trace_id=trace_id,
            )
        task_status = str(
            getattr(getattr(task, "status", None), "value", getattr(task, "status", ""))
            or ""
        ).upper()
        # A completion callback may be delivered again after the Task has
        # already converged.  A later MODIFY turn may, however, append a new
        # Objective to the same Task before Semantic Confirmation.  In that
        # shape the aggregate status can still be the predecessor's terminal
        # value, but the confirmed resume has real continuation work and must
        # reopen through TaskManager before entering ActionLoop.
        if command is None and task_status in {"COMPLETED", "FAILED", "CANCELLED"}:
            from greenbook_agent_core.task.objective_reducer import unsatisfied_objectives

            has_continuation_work = bool(unsatisfied_objectives(task))
            if task_status == "CANCELLED" or not has_continuation_work:
                _debug_resume(
                    "blocked_terminal_task",
                    task_status=task_status,
                    has_continuation_work=has_continuation_work,
                )
                return None
        from greenbook_agent_core.task.objective_reducer import has_nonterminal_execution

        if has_nonterminal_execution(task):
            _debug_resume("blocked_nonterminal")
            return None  # a write is still in flight / RESULT_UNKNOWN: wait.
        if task_status == "RUNNING" and not getattr(task, "active_execution_id", None):
            objectives = list(getattr(task, "objectives", ()) or ())
            if objectives:
                from greenbook_agent_core.task.objective_reducer import unsatisfied_objectives

                has_terminal_predecessor = any(
                    str(getattr(ref, "status", "") or "").upper()
                    in {"COMPLETED", "FAILED", "CANCELLED"}
                    for ref in (getattr(task, "execution_refs", ()) or ())
                )
                if not unsatisfied_objectives(task) or not has_terminal_predecessor:
                    _debug_resume(
                        "blocked_no_continuation_work",
                        has_terminal_predecessor=has_terminal_predecessor,
                    )
                    return None
        if _semantic_confirmation_blocks_task(task):
            _debug_resume("blocked_confirmation")
            return _semantic_confirmation_waiting_result(
                task=task,
                run_id=run_id,
                trace_id=trace_id,
            )
        if _semantic_confirmation_confirmed(task):
            resume = getattr(manager, "resume_task", None)
            if callable(resume):
                try:
                    resume_kwargs = {
                        key: value
                        for key, value in {
                            "expected_confirmation_id": expected_confirmation_id,
                            "expected_confirmation_version": expected_confirmation_version,
                            "expected_task_version": expected_task_version,
                        }.items()
                        if value is not None
                    }
                    resumed = resume(task.task_id, **resume_kwargs)
                    task = await resumed if inspect.isawaitable(resumed) else resumed
                except TaskConfirmationConflictError:
                    _debug_resume("stale_confirmation_during_resume")
                    return _semantic_confirmation_stale_result(
                        task=task,
                        run_id=run_id,
                        trace_id=trace_id,
                    )
                if task is None:
                    return None
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("continuation_start", run_id=run_id)
        except Exception:
            pass
        _debug_resume("before_loop")
        result = await self.run(
            task=task,
            command=None,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            run_id=run_id,
            trace_id=trace_id,
            session=session,
            timezone=timezone,
            mcp=mcp,
            auth=auth,
            activity_callback=activity_callback,
            completion_callback=completion_callback,
        )
        _debug_resume(
            "after_loop",
            status=str(getattr(result, "status", "") or ""),
            error_code=str(getattr(result, "error_code", "") or ""),
            execution_id=str(getattr(result, "execution_id", "") or ""),
        )
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("continuation_finished", run_id=run_id)
        except Exception:
            pass
        return result

    async def _admit_task_if_needed(self, task: Any) -> Any:
        """Apply the policy-false Task admission before ActionLoop starts."""

        manager = self._task_manager
        if manager is None:
            return task
        state = _confirmation_state(task)
        if state == "RESOLVED":
            admit = getattr(manager, "auto_admit_task", None)
            if callable(admit):
                admitted = admit(str(getattr(task, "task_id", "") or ""))
                admitted = await admitted if inspect.isawaitable(admitted) else admitted
                if admitted is not None:
                    return admitted
        return task

    async def _load_or_create_task(
        self,
        command: Any,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        session: Any,
        run_id: str = "",
        turn_id: str = "",
    ) -> Any:
        task_id = _command_task_id(command, session)
        manager = self._task_manager
        if task_id and manager is not None:
            get_task = getattr(manager, "get_task", None)
            if callable(get_task):
                task = get_task(task_id)
                task = await task if inspect.isawaitable(task) else task
                if task is not None:
                    return task
        create = getattr(manager, "create_task", None)
        if callable(create):
            description = str(getattr(command, "requested_goal", "") or "")
            task = create(
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                goal=description,
                goal_category=str(getattr(command, "goal_category", "") or "GOAL_DRIVEN"),
            )
            task = await task if inspect.isawaitable(task) else task
            if task is not None:
                await self._attach_objectives(
                    task,
                    command,
                    run_id=run_id,
                    turn_id=turn_id,
                )
                return task
        raise RuntimeError("ActionLoopExecutor cannot resolve or create a durable Task.")

    async def _attach_objectives(
        self,
        task: Any,
        command: Any,
        *,
        run_id: str = "",
        turn_id: str = "",
    ) -> None:
        """Seed Objectives for a new Task from Command.items (preferred) or its
        required capabilities (compatibility)."""
        if getattr(task, "objectives", None):
            return
        from greenbook_agent_core.actionloop.result import classify_result_requirement
        from greenbook_agent_core.task.objective_compat import objectives_from_items, objectives_for_capabilities

        items = list(getattr(command, "items", None) or ())
        if not items:
            items = self._items_from_task_changes(command)
        capabilities = getattr(command, "required_capabilities", None) or ()
        if items:
            # Per-business-item path: one Objective per CommandItem, each with its
            # own canonical run_at.  Do NOT also create capability Objectives.
            objectives = objectives_from_items(
                items,
                str(getattr(task, "task_id", "") or ""),
                timezone="Asia/Shanghai",
                resolved_state=getattr(command, "resolved_semantics", None),
            )
        else:
            objectives = objectives_for_capabilities(
                capabilities,
                str(getattr(task, "task_id", "") or ""),
                fallback_intent=str(getattr(command, "requested_goal", "") or ""),
            )
        if not objectives:
            return
        try:
            from greenbook_agent_core.command.interpreter import _debug_structured_stage
            _debug_structured_stage(
                "objective_attach",
                {"objective_count": len(objectives),
                 "objectives": [
                     {"objective_id": str(getattr(item, "objective_id", "")),
                      "description": str(getattr(item, "description", "")),
                      "constraints": dict(getattr(item, "constraints", {}) or {})}
                     for item in objectives
                  ]},
                run_id=run_id,
                turn_id=turn_id,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never affect execution
            pass
        task.objectives = objectives
        # Persist immediately so Objectives survive a process restart.
        manager = self._task_manager
        if manager is not None:
            repository = getattr(manager, "repository", None)
            repo = repository() if callable(repository) else getattr(manager, "_repository", None)
            update = getattr(repo, "update", None)
            if callable(update):
                try:
                    await update(task, expected_version=getattr(task, "version", None))
                except Exception:  # noqa: BLE001 - objective persistence is best-effort
                    pass

    @staticmethod
    def _items_from_task_changes(command: Any) -> list[Any]:
        """Project structured CREATE_TASK deltas into thin CommandItems.

        The command contract permits multi-target creation through task_changes
        (the interpreter's canonical form).  Objective construction must not
        silently discard those business items when the optional ``items``
        projection is absent.
        """
        from greenbook_agent_core.command.models import CommandItem, TaskDeltaOperation

        result: list[CommandItem] = []
        for delta in getattr(command, "task_changes", None) or ():
            operation = getattr(delta, "operation", None)
            if operation != TaskDeltaOperation.CREATE_TASK:
                continue
            desired = dict(getattr(delta, "desired_changes", None) or {})
            constraints = dict(desired.get("constraints") or {})
            capabilities = list(
                desired.get("required_capabilities")
                or desired.get("capabilities")
                or ()
            )
            title = str(
                desired.get("title")
                or desired.get("topic")
                or desired.get("description")
                or ""
            )
            temporal_text = str(
                desired.get("temporal_text")
                or constraints.get("temporal_text")
                or desired.get("publish_at")
                or desired.get("run_at")
                or constraints.get("publish_at")
                or constraints.get("run_at")
                or ""
            )
            result.append(CommandItem(
                title=title,
                topic=title,
                operation="CREATE",
                capabilities=[str(item).upper() for item in capabilities],
                temporal_text=temporal_text,
                constraints=constraints,
            ))
        return result

    # ── collaborators ────────────────────────────────────────────────

    async def _decision_maker(self, context: Any) -> ActionDecision:
        if self._llm is None:
            raise RuntimeError("ActionLoop requires an LLM decision maker.")
        from greenbook_agent_core.observability.run_metrics import (
            llm_category_scope,
            record_stage,
            record_stage_once,
        )

        # ActionLoop invokes this callback inside the executor's run_scope.
        record_stage_once("actionloop_first_decision_start")
        record_stage_once("actionloop_first_llm_start")
        record_stage("actionloop_last_llm_start")
        started_at = time.perf_counter()
        response: Any = None
        try:
            with llm_category_scope("ACTIONLOOP"):
                response = await structured_call(
                    self._llm,
                    self._model,
                    _ACTION_LOOP_SYSTEM_PROMPT,
                    "action_decision",
                    ActionDecision.model_json_schema(),
                    context,
                )
        finally:
            latency_ms = round((time.perf_counter() - started_at) * 1000)
            record_stage_once("actionloop_first_llm_end")
            record_stage("actionloop_last_llm_end")
        usage = getattr(response, "usage", None)
        self._latest_actionloop_llm = {
            "category": "ACTIONLOOP",
            "latency_ms": latency_ms,
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        }
        payload = _response_payload(response)
        # The decision schema is strict (extra=forbid) to keep the loop
        # deterministic, but the model occasionally adds envelope fields
        # (e.g. "language").  Strip unknown top-level keys before validating,
        # matching the CommandInterpreter's tolerance.
        if isinstance(payload, dict):
            allowed = set(ActionDecision.model_fields)
            payload = {key: value for key, value in payload.items() if key in allowed}
        decision = ActionDecision.model_validate(payload)
        record_stage("actionloop_last_decision_validated")
        return decision

    async def _read_handler(self, *, tool_name: str, arguments: dict, task: Any,
                            command: Any, request: Any) -> dict[str, Any]:
        import time
        started_at = time.perf_counter()
        adapter = self._adapter
        result = adapter.execute_fast_path_read(
            tool_name=tool_name,
            arguments=arguments,
            user_request=str(getattr(request, "message", "") or ""),
            synthesis_requested=(
                str(
                    getattr(command, "semantic_operation", "")
                    or getattr(getattr(command, "resolved_semantics", None), "semantic_operation", "")
                    or ""
                ).strip().upper()
                in {"SUMMARIZE", "SUMMARIZE_POST", "SUMMARIZE_CONTENT"}
            )
            or "ANALYZE_CONTENT_PATTERNS" in {
                str(item).upper()
                for item in (getattr(command, "required_capabilities", ()) or ())
            },
            llm=getattr(request, "llm", None),
            model=str(getattr(request, "model", "") or ""),
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            trace_id=request.trace_id,
            session=request.session,
            auth=request.auth,
            mcp=request.mcp,
        )
        result = await result if inspect.isawaitable(result) else result
        from greenbook_agent_core.observability.run_metrics import record_tool
        record_tool(round((time.perf_counter() - started_at) * 1000), run_id=getattr(request, "run_id", ""))
        # Forward the structured ToolResult payload so the ActionLoop can record
        # real resource_refs/evidence instead of guessing business state from a
        # stringified content.  The Fast Path read keeps the full tool result in
        # artifacts[0].
        structured: Any = None
        tool_payload: dict[str, Any] = {}
        artifacts = getattr(result, "artifacts", None) or ()
        if artifacts and isinstance(artifacts[0], Mapping):
            tool_payload = dict(artifacts[0])
            structured = (
                tool_payload["data"]
                if "data" in tool_payload
                else tool_payload
            )
        code = str(
            tool_payload.get("code")
            or tool_payload.get("error_code")
            or getattr(result, "error_code", "")
            or ""
        )
        message = str(
            tool_payload.get("message")
            or tool_payload.get("user_message")
            or getattr(result, "error_message", "")
            or getattr(result, "content", "")
            or ""
        )
        return {
            "ok": bool(getattr(result, "success", False)),
            "status": str(getattr(result, "status", "")),
            "content": str(getattr(result, "content", "")),
            "resource_id": getattr(result, "draft_id", None),
            "message": message,
            "user_message": str(tool_payload.get("user_message") or ""),
            "code": code,
            "error_code": code,
            "retryable": bool(tool_payload.get("retryable", False)),
            "request_sent": tool_payload.get("request_sent"),
            "state": dict(
                tool_payload.get("state")
                or getattr(result, "failure_state", None)
                or {}
            ),
            "provenance": list(tool_payload.get("provenance") or []),
            "resource_refs": list(tool_payload.get("resource_refs") or []),
            "data": structured,
        }

    async def _write_submitter(self, *, tool_name: str, arguments: dict, capability: str,
                               semantic_action: str, task: Any, command: Any,
                               request: Any, objective_id: str = "") -> dict[str, Any]:
        # Phase 4B.1: the Execution Runtime (submit_plan) owns durable operation
        # dedupe/claim.  This Agent-layer handler has no separate ledger — both
        # Fast Path and Complex ActionLoop write through the same submission API.
        from greenbook_agent_core.observability.run_metrics import record_stage

        record_stage("actionloop_write_dispatch_ready", run_id=getattr(request, "run_id", ""))
        started_at = time.perf_counter()
        adapter = self._adapter
        result = adapter.submit_fast_path_write(
            tool_name=tool_name,
            arguments=arguments,
            capability=capability,
            semantic_action=semantic_action,
            command=command,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            trace_id=request.trace_id,
            session=request.session,
            timezone=request.timezone,
            mcp=request.mcp,
            llm=request.llm,
            model=request.model,
            auth=request.auth,
            completion_callback=request.completion_callback,
            activity_callback=request.activity_callback,
            task_id=str(getattr(task, "task_id", "") or ""),
            objective_id=objective_id,
            plan_mode="INCREMENTAL",
        )
        result = await result if inspect.isawaitable(result) else result
        from greenbook_agent_core.observability.run_metrics import record_tool
        record_tool(round((time.perf_counter() - started_at) * 1000), run_id=getattr(request, "run_id", ""))
        status = str(getattr(result, "status", ""))
        # A QUEUED/SUBMITTED write is durably accepted (not complete, not failed).
        ok = bool(getattr(result, "success", False)) or status in {
            "QUEUED", "SUBMITTED", "RUNNING", "PENDING",
        }
        # An approval-gated write is a real durable pause, not a failure: carry
        # the execution + approval identity so the ActionLoop surfaces a real
        # WAITING_APPROVAL instead of a fake WAITING_USER.
        if status in {"WAITING_APPROVAL", "WAITING_HUMAN"} and getattr(result, "execution_id", None):
            ok = True
        return {
            "ok": ok,
            "status": status,
            "execution_id": getattr(result, "execution_id", None),
            "approval_id": getattr(result, "approval_id", None),
            "resource_id": getattr(result, "draft_id", None)
            or getattr(result, "schedule_id", None),
            "message": str(getattr(result, "content", "") or ""),
        }


class _LoopRequest:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _command_task_id(command: Any, session: Any) -> str:
    # A fresh CREATE request is a new business Task even when the conversation
    # session still points at the last completed Task.  Reusing that pointer
    # would make the ActionLoop observe an already-satisfied Objective and
    # finish without creating the requested Draft/Schedule.
    command_type = str(getattr(command, "type", "") or getattr(command, "command", "")).upper()
    if command_type in {"CREATE", "QUERY"}:
        return ""
    target = getattr(command, "resolved_target", None)
    if isinstance(target, dict) and target.get("task_id"):
        return str(target["task_id"])
    for field in ("active_task_id", "active_draft_id", "active_schedule_id"):
        value = str(getattr(session, field, "") or "").strip()
        if value:
            return value
    return ""


def _response_payload(response: Any) -> dict[str, Any]:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else dict(parsed)
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    import json as _json

    from greenbook_agent_core.llm_compat import extract_top_level_json

    return _json.loads(extract_top_level_json(content))


def _to_runtime_result(result: Any) -> Any:
    from greenbook_agent_core.execution.runtime_result import RuntimeResult

    execution_id = getattr(result, "execution_id", None)
    observations = [
        observation.model_dump(mode="json")
        for observation in getattr(result, "observations", [])
    ]
    partial_results = dict(getattr(result, "partial_results", {}) or {})
    partial_results.update({
        "iterations": int(getattr(result, "iterations", 0)),
        "decisions": list(getattr(result, "decisions", [])),
        "observations": observations,
        "tool_calls": len(observations),
        "progress_trace": list(getattr(result, "progress_trace", [])),
        "task_ids": [str(getattr(result, "task_id", ""))],
    })
    tool_results = _tool_results_from_observations(observations)
    if tool_results:
        partial_results["tool_results"] = tool_results
    task_plan = getattr(result, "task_plan", None)
    if task_plan is not None:
        partial_results["task_plan"] = task_plan.model_dump(mode="json")
    # A WAITING_EXTERNAL result carries a durable write execution; without
    # execution_ids the Agent runner sees no accepted work and flips the Run to
    # COMPLETED while the side effect is still in flight.
    if execution_id:
        partial_results["execution_ids"] = [str(execution_id)]

    return RuntimeResult(
        success=bool(getattr(result, "success", False)),
        status=str(getattr(result, "status", "")),
        run_id=str(getattr(result, "run_id", "")),
        task_id=str(getattr(result, "task_id", "")),
        trace_id=str(getattr(result, "trace_id", "")),
        execution_id=execution_id,
        approval_id=getattr(result, "approval_id", None),
        execution_path="action_loop",
        content=str(getattr(result, "content", "")),
        summary=str(getattr(result, "content", "")),
        error_code=str(getattr(result, "error_code", "")),
        error_message=str(getattr(result, "error_message", "")),
        tool_rounds=len(observations),
        partial_results=partial_results,
    )


def _tool_results_from_observations(
    observations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve structured read evidence for the user-facing projection.

    ActionLoop keeps the authoritative ToolResult in each observation's
    ``detail`` field.  The compatibility RuntimeResult must carry that same
    evidence forward; otherwise a successful read is reduced to the loop's
    internal completion sentence and the frontend cannot render result cards.
    """

    results: list[dict[str, Any]] = []
    for observation in observations:
        if not isinstance(observation, Mapping):
            continue
        detail = observation.get("detail")
        tool_name = str(observation.get("tool_name") or "").strip()
        if not tool_name or not isinstance(detail, Mapping):
            continue
        result = dict(detail)
        result["tool_name"] = tool_name
        result["ok"] = bool(observation.get("ok", result.get("ok", False)))
        error_code = str(observation.get("error_code") or "").strip()
        if error_code:
            result["error_code"] = error_code
        message = str(observation.get("message") or "").strip()
        if message and not result.get("message"):
            result["message"] = message
        results.append(result)
    return results


def _confirmation_state(task: Any) -> str:
    return str(
        getattr(
            getattr(task, "confirmation_state", ""),
            "value",
            getattr(task, "confirmation_state", ""),
        )
        or "RESOLVED"
    ).upper()


def _semantic_confirmation_confirmed(task: Any) -> bool:
    return bool(
        getattr(task, "requires_confirmation", False)
        and _confirmation_state(task) == "CONFIRMED"
        and getattr(task, "confirmed_version", None)
        == getattr(task, "confirmation_version", None)
    )


def _semantic_confirmation_matches(
    task: Any,
    *,
    expected_confirmation_id: str | None,
    expected_confirmation_version: int | None,
    expected_task_version: int | None,
) -> bool:
    """Pin a queued resume to the confirmation snapshot that won CAS."""

    if expected_confirmation_version is not None:
        if (
            _confirmation_state(task) != "CONFIRMED"
            or getattr(task, "confirmed_version", None)
            != expected_confirmation_version
            or getattr(task, "confirmation_version", None)
            != expected_confirmation_version
        ):
            return False
    if expected_task_version is not None and getattr(task, "version", None) != expected_task_version:
        return False
    if expected_confirmation_id and confirmation_identity(task) != expected_confirmation_id:
        return False
    return True


def _semantic_confirmation_blocks_task(task: Any) -> bool:
    return bool(
        getattr(task, "requires_confirmation", False)
        and not _semantic_confirmation_confirmed(task)
    )


def _semantic_confirmation_waiting_result(
    *,
    task: Any,
    run_id: str,
    trace_id: str,
) -> RuntimeResult:
    return RuntimeResult(
        success=False,
        status="WAITING_HUMAN",
        run_id=run_id,
        task_id=str(getattr(task, "task_id", "") or ""),
        trace_id=trace_id,
        execution_path="semantic_confirmation",
        error_code="SEMANTIC_CONFIRMATION_REQUIRED",
        error_message="Please confirm the resolved task before execution.",
        content="Please confirm the resolved task before execution.",
        partial_results={
            "semantic_confirmation": {
                "task_id": str(getattr(task, "task_id", "") or ""),
                "confirmation_version": int(
                    getattr(task, "confirmation_version", 0) or 0
                ),
            }
        },
    )


def _semantic_confirmation_stale_result(
    *,
    task: Any,
    run_id: str,
    trace_id: str,
) -> RuntimeResult:
    return RuntimeResult(
        success=False,
        status="FAILED",
        run_id=run_id,
        task_id=str(getattr(task, "task_id", "") or ""),
        trace_id=trace_id,
        execution_path="semantic_confirmation",
        error_code="SEMANTIC_CONFIRMATION_STALE",
        error_message="The confirmed Task version is no longer current.",
    )


__all__ = ["ActionLoopExecutor"]
