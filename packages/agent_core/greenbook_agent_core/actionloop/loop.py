"""ActionLoop: one reasoning loop that drives a Task to verified completion.

Decision flow per iteration:
    Observe  -> Build Context (reuse ContextAssembler) -> Decide (one LLM call)
    -> Act (read direct / write via durable Runtime) -> Observe result -> Continue.

Guards that keep the loop honest and cheap:
  * A write already submitted (SUBMITTED/RUNNING) or a RESULT_UNKNOWN execution
    switches the loop to WAIT / WAITING_EXTERNAL instead of calling the model
    again (no busy-loop, no unbounded reasoning).
  * FINISH is honored only when every pending objective is satisfied by a real,
    verified resource — never because the model said so or a queue accepted it.
  * No fixed workflow is encoded; the next semantic action is chosen from
    objective + artifacts + resources + tool results.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC
from typing import Any

from greenbook_contracts.tool_result import ResourceRef
from pydantic import BaseModel

from ..command.models import Command
from ..execution.temporal_resolver import TemporalResolver
from ..planning.contracts import PlanStep, TaskPlan
from ..task.objective_compat import resolve_objectives
from ..task.models import ArtifactRef
from ..task.objective_reducer import (
    ObjectiveStateReducer,
    all_objectives_satisfied,
    bind_related,
    is_objective_satisfied,
    mutation_conflicts,
    mutation_is_superseded,
    mutation_objective_details,
    mutation_objective_is_superseded,
    mutation_execution_state,
    objective_for_resource,
)
from .models import (
    ActionDecision,
    ActionDecisionType,
    ActionLoopResult,
    ActionObservation,
    ActionStepPlan,
)
from .qualification import guard_action

logger = logging.getLogger(__name__)

# ── deterministic SemanticAction -> capability/tool resolution ──────────────
# This mapping replaces a second LLM tool-selection pass: a canonical semantic
# action resolves to exactly one capability + tool, or to a resolver callback
# only when genuinely ambiguous (requirement 06).

_SEMANTIC_CAPABILITY: dict[str, str] = {
    "SEARCH_POSTS": "SEARCH_COMMUNITY",
    "ANSWER_FROM_KNOWLEDGE": "ANSWER_FROM_KNOWLEDGE",
    "GET_POST": "GET_POST_DETAIL",
    "LIST_OWN_POSTS": "LIST_OWN_POSTS",
    "CREATE_DRAFT": "GENERATE_CONTENT",
    "GET_DRAFT": "GET_DRAFT",
    "LIST_DRAFTS": "LIST_DRAFTS",
    "UPDATE_DRAFT": "MANAGE_DRAFT",
    "DELETE_DRAFT": "DELETE_DRAFT",
    "DELETE_POST": "DELETE_POST",
    "CREATE_SCHEDULE": "SCHEDULE_PUBLISH",
    "GET_SCHEDULE": "GET_SCHEDULE_STATUS",
    "UPDATE_SCHEDULE": "MANAGE_SCHEDULE",
    "CANCEL_SCHEDULE": "CANCEL_SCHEDULE",
    "PUBLISH_NOW": "PUBLISH_NOW",
}

_SEMANTIC_TOOL: dict[str, str] = {
    "SEARCH_POSTS": "community.search_public_posts",
    "ANSWER_FROM_KNOWLEDGE": "community.answer_from_knowledge",
    "GET_POST": "community.get_post",
    "LIST_OWN_POSTS": "community.list_own_posts",
    "CREATE_DRAFT": "content.create_draft",
    "GET_DRAFT": "content.get_draft",
    "LIST_DRAFTS": "content.list_drafts",
    "UPDATE_DRAFT": "content.update_draft",
    "DELETE_DRAFT": "content.delete_draft",
    "DELETE_POST": "community.delete_post",
    "CREATE_SCHEDULE": "publication.schedule",
    "GET_SCHEDULE": "publication.get_status",
    "UPDATE_SCHEDULE": "publication.update_schedule",
    "CANCEL_SCHEDULE": "publication.cancel_schedule",
    "PUBLISH_NOW": "publication.publish_now",
}

# Actions with an external side effect; they must go through the durable
# Runtime and are never treated as complete until verified.
_WRITE_ACTIONS = frozenset({
    "CREATE_DRAFT",
    "UPDATE_DRAFT",
    "DELETE_DRAFT",
    "DELETE_POST",
    "CREATE_SCHEDULE",
    "UPDATE_SCHEDULE",
    "CANCEL_SCHEDULE",
    "PUBLISH_NOW",
})
_MUTATION_ACTIONS = frozenset({
    "UPDATE_DRAFT", "DELETE_DRAFT", "DELETE_POST", "UPDATE_SCHEDULE", "CANCEL_SCHEDULE",
    "PUBLISH_NOW",
})

# Action -> the business resource it is expected to produce (drives FINISH).
_ACTION_RESOURCE_KIND: dict[str, str] = {
    "CREATE_DRAFT": "DRAFT",
    "UPDATE_DRAFT": "DRAFT",
    "CREATE_SCHEDULE": "SCHEDULE",
    "UPDATE_SCHEDULE": "SCHEDULE",
    "CANCEL_SCHEDULE": "SCHEDULE",
    "PUBLISH_NOW": "POST",
    "DELETE_POST": "POST",
    "SEARCH_POSTS": "SEARCH_RESULT",
    "LIST_OWN_POSTS": "SEARCH_RESULT",
    "GET_DRAFT": "DRAFT",
    "LIST_DRAFTS": "DRAFT",
    "GET_SCHEDULE": "SCHEDULE",
    # A successful GET_POST produces a POST resource = strong evidence for a
    # GROUNDED_SYNTHESIS objective.  Distinct from the SEARCH_RESULT candidate set.
    "GET_POST": "POST",
}

# Discovery actions produce candidate sets; detail actions produce strong evidence.
_DISCOVERY_ACTIONS = {"SEARCH_POSTS", "LIST_OWN_POSTS"}
_DETAIL_ACTIONS = {"GET_POST"}
_READ_NO_PROGRESS_THRESHOLD = 2

_PLAN_CAPABILITY_ACTION = {
    "SEARCH_COMMUNITY": "SEARCH_POSTS",
    "ANSWER_FROM_KNOWLEDGE": "ANSWER_FROM_KNOWLEDGE",
    "GET_POST_DETAIL": "GET_POST",
    "GENERATE_CONTENT": "CREATE_DRAFT",
    "SCHEDULE_PUBLISH": "CREATE_SCHEDULE",
    "MANAGE_DRAFT": "UPDATE_DRAFT",
    "MANAGE_SCHEDULE": "UPDATE_SCHEDULE",
    "CANCEL_SCHEDULE": "CANCEL_SCHEDULE",
    "PUBLISH_NOW": "PUBLISH_NOW",
    "DELETE_POST": "DELETE_POST",
}

# Executions that are not terminal: a loop must never keep reasoning over them.
_NONTERMINAL_STATUSES = {
    "SUBMITTED",
    "RUNNING",
    "PENDING",
    "QUEUED",
    "WAITING_EXTERNAL",
    "RESULT_UNKNOWN",
    "PROCESSING",
    "UNKNOWN",
    "IN_PROGRESS",
    "WAITING",
    "WAITING_APPROVAL",
    "WAITING_HUMAN",
}
_OBJECTIVE_WAITING_STATUSES = _NONTERMINAL_STATUSES | {
    "FAILED_RETRYABLE",
    "RETRYABLE",
    "RETRYING",
}


class ActionLoopError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Injected collaborator type aliases.
DecisionMaker = Callable[[Mapping[str, Any]], Awaitable[ActionDecision] | ActionDecision]
SemanticResolver = Callable[[str], tuple[str, str] | None]  # action -> (capability, tool)


class ActionLoop:
    """Run one Task to verified completion through semantic-action decisions."""

    def __init__(
        self,
        *,
        decision_maker: DecisionMaker | None = None,
        semantic_resolver: SemanticResolver | None = None,
        read_handler: Callable[..., Any] | None = None,
        write_submitter: Callable[..., Any] | None = None,
        task_store: Any | None = None,
        context_assembler: Any | None = None,
        activity_callback: Any = None,
        decision_observer: Callable[..., Any] | None = None,
        llm: Any | None = None,
        model: str = "",
        result_composer: Any | None = None,
        max_iterations: int = 8,
        max_llm_calls: int = 8,
        max_tool_calls: int = 12,
        max_replans: int = 4,
        max_failures: int = 6,
        max_compose_attempts: int = 3,
        max_parallel_objectives: int = 2,
    ) -> None:
        self._decision_maker = decision_maker
        self._resolver = semantic_resolver or _default_resolver
        self._read_handler = read_handler
        self._write_submitter = write_submitter
        self._task_store = task_store
        self._context_assembler = context_assembler
        self._activity_callback = activity_callback
        self._decision_observer = decision_observer
        self._llm = llm
        self._model = model
        if result_composer is None:
            try:
                from .result import ResultComposer
                result_composer = ResultComposer()
            except Exception:  # noqa: BLE001 - fall back to no-op
                result_composer = _NullComposer()
        self._result_composer = result_composer
        self._max_compose_attempts = max(1, max_compose_attempts)
        self._max_iterations = max(1, max_iterations)
        # Phase 7 runtime protection budgets (per-run; no token metering).
        self._max_llm_calls = max(1, max_llm_calls)
        self._max_tool_calls = max(1, max_tool_calls)
        self._max_replans = max(0, max_replans)
        self._max_failures = max(1, max_failures)
        # Objective-level concurrency is deliberately bounded.  The scheduler
        # below only admits a proven-safe subset; all other shapes retain the
        # serial ActionLoop path.
        self._max_parallel_objectives = max(1, max_parallel_objectives)
        # Deterministic mutation plan tracking: (task_id, resource_id) already
        # submitted by a mutation command, so each desired mutation (UPDATE_DRAFT,
        # UPDATE_SCHEDULE, ...) runs exactly once even across continuations.
        self._mutation_done: set[tuple[str, str]] = set()
        # The desired mutation plan per task, so a resume (which passes
        # command=None) can continue the remaining mutations.
        self._task_mutations: dict[str, list[Any]] = {}
        # A Worker continuation can be delivered while the submitting ActionLoop
        # is still persisting the target->Execution correlation.  This lock is
        # process-local scheduling protection only; durable recovery still uses
        # TaskRevision + TaskExecutionRef.
        self._mutation_commit_locks: dict[str, asyncio.Lock] = {}

    async def wait_for_mutation_submission(self, task_id: str) -> None:
        """Wait for a same-process submitter to persist its correlation row."""
        lock = self._mutation_commit_locks.get(str(task_id or ""))
        if lock is None or not lock.locked():
            return
        await lock.acquire()
        lock.release()

    # ── public run ────────────────────────────────────────────────────

    async def run(
        self,
        task: Any,
        command: Command | None = None,
        *,
        request: Any = None,
        assembled_context: Any | None = None,
        max_iterations: int | None = None,
        task_store: Any | None = None,
        boundary: Any | None = None,
    ) -> ActionLoopResult:
        task_id = str(getattr(task, "task_id", "") or "")
        run_id = str(getattr(request, "run_id", "") or "")
        trace_id = str(getattr(request, "trace_id", "") or "")
        result = ActionLoopResult(
            task_id=task_id,
            run_id=run_id,
            trace_id=trace_id,
            status="FAILED",
        )
        result.task_plan = self._build_objective_plan(task)
        plan = result.task_plan
        self._refresh_plan_status(task, plan)
        iterations = max(1, max_iterations or self._max_iterations)
        # A per-run task store keeps a shared ActionLoop instance concurrency-safe:
        # concurrent turns never clobber each other's persistence boundary.
        store = task_store or self._task_store or _NullTaskStore()
        # A fresh turn's command carries the desired mutation plan; seed it so a
        # resume (command=None) can continue the remaining mutations.  The
        # immutable request is also recorded through the existing Task revision
        # audit boundary, because ActionLoop memory is not a recovery boundary.
        if command is not None and getattr(command, "task_changes", None):
            self._task_mutations[task_id] = list(command.task_changes)
            persist_plan = getattr(store, "persist_mutation_plan", None)
            if callable(persist_plan):
                await _maybe_await(persist_plan(task, command.task_changes))
        elif not self._task_mutations.get(task_id):
            self._task_mutations[task_id] = self._mutation_changes_from_revisions(task)
        # Per-run execution boundary (also concurrency-safe for a shared loop).
        boundary = boundary or _NullBoundary()
        # Phase 7 runtime protection counters (per-run; reset each resume so
        # already-completed objectives never recount).
        llm_calls = 0
        tool_calls = 0
        replans = 0
        # Successful reads are Objective-owned evidence.  The same read may be
        # useful to two Objectives, but it must bind separately to each owner.
        # A read that already SUCCEEDED with the same signature must not re-run:
        # the model choosing it again is a controlled rejection, not a real tool
        # call.  This is the Runtime invariant that stops a SEARCH busy-loop.
        last_read_observation_key: tuple[Any, ...] | None = None
        read_equivalent_streak = 0
        # Deterministic evidence acquisition state is Objective-local.  A
        # candidate discovered while serving Objective A must never be selected
        # as evidence for Objective B.
        candidate_state: dict[str, dict[str, str]] = {}
        self._hydrate_candidate_state(task, candidate_state)
        failures = 0
        previous_progress_key: tuple[Any, ...] | None = None
        no_progress_streak = 0

        # Guard: an in-flight write or a RESULT_UNKNOWN execution means we must
        # not keep reasoning over it — suspend and let Execution/UserActivity
        # drive the resume (requirement 11/13).  The WAITING_EXTERNAL result must
        # carry the real in-flight execution id so the Run stays non-terminal
        # (invariant: WAITING_EXTERNAL <=> a real non-terminal execution) instead
        # of being converged to COMPLETED with no execution behind it.
        initial_objective = self._current_objective(task)
        pending_exec_ids = self._has_nonterminal_execution(
            task,
            objective_id=(
                str(getattr(initial_objective, "objective_id", "") or "")
                if initial_objective is not None
                else ""
            ),
        )
        if not pending_exec_ids and initial_objective is not None:
            pending_exec_ids = self._mutation_blocking_execution_ids(
                task,
                initial_objective,
            )
        if pending_exec_ids:
            boundary.record_result_unknown()
            await _maybe_await(self._suspend(task, store))
            result.status = "WAITING_EXTERNAL"
            result.success = True
            result.execution_id = next((e for e in pending_exec_ids if e), "")
            result.content = "该任务有正在执行或结果未知的操作，等待其完成。"
            return result

        if await self._try_parallel_independent_creates(
            task, command, request, boundary, store, result,
        ):
            return result

        for i in range(1, iterations + 1):
            context = await self._observe(task, command, assembled_context)
            self._refresh_plan_status(task, plan)
            current_objective = self._current_objective(task)
            iteration_before = self._progress_snapshot(task, plan)
            try:
                from greenbook_agent_core.command.interpreter import _debug_structured_stage
                _debug_structured_stage(
                    "actionloop_state",
                    {"iteration": i,
                     "objectives": [{"id": str(getattr(o, "objective_id", "")), "status": str(getattr(o, "status", "")), "resources": list(getattr(o, "related_resource_ids", ()) or ())} for o in (getattr(task, "objectives", ()) or ())],
                     "steps": [{"id": s.step_id, "goal_id": s.goal_id, "capability": s.capability, "status": s.status, "depends_on": list(s.depends_on)} for s in (plan.steps if plan else [])],
                     "current": str(getattr(self._current_objective(task), "objective_id", "") or "")},
                )
            except Exception:  # noqa: BLE001 - diagnostics must never affect execution
                pass
            # Completion is a reducer decision, never an LLM decision.  A
            # synthesis Objective must first materialize its result artifact:
            # it may own evidence resources before its user-facing conclusion
            # exists.  Compose that deterministic projection before terminal
            # reduction, then finish a fully satisfied Task without another
            # model turn.
            top_composed = await self._compose_ready_synthesis(task, store)
            if top_composed is not None:
                result.final_result = top_composed
                result.content = str(getattr(top_composed, "content", "") or result.content)
            # Once
            # all Objective-owned postconditions are present, finish before
            # asking the model for another action.  Without this boundary a
            # model can re-select an old read after the last plan step and
            # burn the iteration budget despite a fully satisfied Task.
            if (
                getattr(task, "objectives", None)
                and self._pending_synthesis_objective(task) is None
                and self._verify_finish(task)
                and not self._next_pending_mutation(task, command)
            ):
                result.iterations = i
                self._append_progress_trace(
                    result, iteration_before, self._progress_snapshot(task, plan),
                    iteration=i, semantic_action="FINISH", replan=False,
                    execution_submitted=False, waiting=False,
                )
                return await self._finished(result, task, store)
            if current_objective is None:
                pending_exec_ids = self._has_nonterminal_execution(task)
                if pending_exec_ids:
                    boundary.record_result_unknown()
                    await _maybe_await(self._suspend(task, store))
                    result.status = "WAITING_EXTERNAL"
                    result.success = True
                    result.execution_id = next((e for e in pending_exec_ids if e), "")
                    result.content = "A required operation is still running or its result is unknown."
                    return result
                blocked = self._blocked_dependency_info(task)
                if blocked is not None:
                    return await self._dependency_blocked_result(
                        result,
                        task,
                        store,
                        objective=blocked[0],
                        dependency=blocked[1],
                        iteration=i,
                    )
            # Deterministic Evidence Acquisition: for a GROUNDED_SYNTHESIS
            # Objective with pending candidates and insufficient strong evidence,
            # GET_POST the next candidate WITHOUT asking the LLM (state machine,
            # not prompt guidance).  The model never has to choose which post to
            # read next.
            self._hydrate_candidate_state(task, candidate_state)
            evd_decision = await self._evidence_acquisition_decision(task, candidate_state)
            canonical_answer = self._structured_answer_decision(task, command)
            decision_source = "DETERMINISTIC"
            pending_mutation = self._next_pending_mutation(task, command)
            if evd_decision is not None:
                decision = evd_decision
            elif canonical_answer is not None:
                # ANSWER_FROM_KNOWLEDGE is already a resolved semantic fact.
                # Keep it on the existing ActionLoop path, but do not ask the
                # per-iteration model to turn that fact into a different read
                # action (or to invent its required question argument).
                decision = canonical_answer
            else:
                # Deterministic completion: once a synthesis Objective's evidence
                # is ready, compose + finish NOW instead of waiting for the model
                # to emit FINISH (the model is not the readiness authority).
                comp = await self._compose_ready_synthesis(task, store)
                if comp is not None:
                    result.final_result = comp
                    result.content = str(getattr(comp, "content", "") or result.content)
                    if self._verify_finish(task) and not self._next_pending_mutation(task, command):
                        return await self._finished(result, task, store)
                det_next = self._next_required_write_action(task)
                if det_next:
                    # Deterministic next capability (e.g. CREATE_SCHEDULE once
                    # the Objective owns its Draft).  Bypasses the LLM so the
                    # loop cannot spin on an already-satisfied write.
                    current = self._current_objective(task)
                    deterministic_arguments = {
                        "objective_id": str(
                            getattr(current, "objective_id", "") or ""
                        )
                    } if current is not None else {}
                    if current is not None:
                        objective_constraints = dict(
                            getattr(current, "constraints", None) or {}
                        )
                        for key in (
                            "draft_id",
                            "schedule_id",
                            "post_id",
                            "title",
                            "content",
                            "body",
                            "instruction",
                            "summary",
                            "run_at",
                            "timezone",
                        ):
                            if objective_constraints.get(key) not in (None, ""):
                                deterministic_arguments[key] = objective_constraints[key]
                        if det_next in {"CREATE_SCHEDULE", "PUBLISH_NOW"}:
                            # A dependent Objective may consume one verified
                            # Draft owned by its explicit predecessor.  Carry
                            # that typed artifact identity into the durable
                            # boundary; do not copy the resource into the
                            # dependent Objective's ownership list.
                            dependency_drafts = _dependency_draft_ids(task, current)
                            if len(dependency_drafts) == 1:
                                deterministic_arguments["draft_id"] = dependency_drafts[0]
                    decision = ActionDecision(
                        decision=ActionDecisionType.CALL_TOOL,
                        semantic_action=det_next,
                        arguments=deterministic_arguments,
                    )
                elif pending_mutation and self._mutation_matches_current_objective(
                    current_objective,
                    pending_mutation,
                ):
                    # Deterministic mutation plan: an explicit command mutation
                    # (UPDATE_DRAFT / UPDATE_SCHEDULE ...) must run even when the
                    # Objective is already satisfied.  Each mutation runs once.
                    # It must not preempt an independent read/synthesis Objective
                    # that is currently ready in the same Task.
                    decision = self._mutation_decision(task, command)
                else:
                    ready = self._next_ready_plan_step(
                        plan,
                        objective_id=str(
                            getattr(current_objective, "objective_id", "") or ""
                        ),
                    )
                    planned_action = _PLAN_CAPABILITY_ACTION.get(
                        str(getattr(ready, "capability", "") or "").upper(), ""
                    ) if ready is not None else ""
                    decision_source = (
                        "LLM" if not planned_action or planned_action == "SEARCH_POSTS"
                        else "DETERMINISTIC"
                    )
                    decision = await self._decide_for_plan(context, task, plan)
                    if decision_source == "LLM":
                        llm_calls += 1
                    if decision_source == "LLM" and llm_calls > self._max_llm_calls:
                        return _budget_failure(result, "ACTION_LOOP_LLM_BUDGET", "LLM 调用次数超限。")
            mutation_action = self._next_pending_mutation(task, command)
            mutation_is_allowed = bool(
                mutation_action
                and str(decision.semantic_action or "").upper() == mutation_action
            )
            if not mutation_is_allowed and command is not None:
                mutation_is_allowed = any(
                    str(
                        (getattr(change, "desired_changes", None) or {}).get(
                            "semantic_action", ""
                        )
                    ).upper()
                    == str(decision.semantic_action or "").upper()
                    for change in (getattr(command, "task_changes", None) or ())
                )
            result.decisions.append(f"{i}:{decision.decision.value}")
            result.iterations = i
            try:
                from greenbook_agent_core.command.interpreter import _debug_structured_stage
                _debug_structured_stage(
                    "actionloop_decision",
                    {"iteration": i, "decision": str(decision.decision), "semantic_action": str(decision.semantic_action or ""), "arguments": dict(decision.arguments or {})},
                )
            except Exception:  # noqa: BLE001 - diagnostics must never affect execution
                pass
            # Durable decision observability: fire-and-forget, never alters
            # decision/action flow.  Any observer failure is swallowed.
            observer = self._decision_observer
            if observer is not None:
                try:
                    observer_args = dict(
                        run_id=run_id,
                        task_id=task_id,
                        objective_id=str(
                            getattr(self._current_objective(task), "objective_id", "") or ""
                        ),
                        iteration=i,
                        decision=decision,
                        decision_source=decision_source,
                        llm_called=decision_source == "LLM",
                    )
                    try:
                        value = observer(**observer_args)
                    except TypeError:
                        # Preserve compatibility for injected observers using
                        # the pre-source signature; production observers accept
                        # the richer decision metadata.
                        value = observer(**{
                            key: observer_args[key]
                            for key in ("run_id", "task_id", "objective_id", "iteration", "decision")
                        })
                    await _maybe_await(value)
                except Exception:  # noqa: BLE001 - observability is best-effort
                    pass

            if decision.decision not in {
                ActionDecisionType.CALL_TOOL,
                ActionDecisionType.GENERATE_CONTENT,
            }:
                last_read_observation_key = None
                read_equivalent_streak = 0

            if decision.decision == ActionDecisionType.FINISH:
                # Deterministic result composition: whether to generate the
                # user-facing answer is a state decision, not an LLM guess.  A
                # GROUNDED_SYNTHESIS Objective with ready evidence is composed
                # here automatically; FINISH only confirms Objectives + FinalResult.
                composed = await self._compose_ready_synthesis(task, store)
                if composed is not None:
                    result.final_result = composed
                    result.content = str(getattr(composed, "content", "") or result.content)
                if self._verify_finish(task):
                    return await _maybe_await(self._finished(result, task, store))
                # False positive: the model claims completion but objectives are
                # not yet backed by real verified resources.  Do not finish;
                # keep deciding (requirement 10).
                await _maybe_await(store._record(task, "finish_blocked", decision.reason))
                continue

            if decision.decision == ActionDecisionType.CLARIFY:
                await _maybe_await(self._wait_human(task, decision, store))
                result.status = "WAITING_HUMAN"
                result.success = False
                result.error_code = "ACTION_LOOP_CLARIFY"
                result.error_message = decision.reason or "需要澄清下一步。"
                result.content = result.error_message
                return result

            if decision.decision == ActionDecisionType.WAIT:
                self._suspend(task, store)
                result.status = "WAITING_EXTERNAL"
                result.success = True
                result.content = decision.reason or "等待外部结果。"
                return result

            if decision.decision == ActionDecisionType.REPLAN:
                replans += 1
                if replans > self._max_replans:
                    return _budget_failure(result, "ACTION_LOOP_REPLAN_BUDGET", "重规划次数超限，需要澄清。")
                plan = await _maybe_await(self._apply_plan(task, decision, store))
                result.plan = plan
                result.task_plan = self._task_plan_from_steps(task, plan)
                plan = result.task_plan
                await _maybe_await(store._record(task, "replan", decision.reason))
                continue

            if decision.decision == ActionDecisionType.COMPOSE_RESULT:
                # Build the user-facing FinalResult from ready current-Task
                # evidence and bind it to the synthesis Objective.  Execution of
                # tools is a prerequisite, not completion: a GROUNDED_SYNTHESIS
                # Objective only completes once its result is composed.
                composed = await self._compose_result(task, store, decision.reason)
                if composed is None:
                    # Not enough evidence yet -> NOT_READY.  Do not finish; keep
                    # choosing an evidence-producing action.  Bounded: fail fast
                    # instead of burning the whole iteration budget.
                    result.compose_attempts += 1
                    if result.compose_attempts > self._max_compose_attempts:
                        return _budget_failure(result, "ACTION_LOOP_EVIDENCE_BUDGET",
                                               "合成所需证据不足，无法基于空证据生成结论。")
                    result.observations.append(ActionObservation(
                        iteration=i, action="COMPOSE_RESULT", outcome="NOT_READY", ok=False,
                        message="当前 Task 证据不足以合成结果，需要先获取更多真实证据。",
                    ))
                    await _maybe_await(store._record(task, "compose_not_ready", ""))
                    continue
                result.content = composed.content
                result.final_result = composed
                await _maybe_await(store._record(task, "composed_result", composed.source_refs))
                continue

            # CALL_TOOL / GENERATE_CONTENT
            action = str(decision.semantic_action or "").upper()
            if not action:
                await _maybe_await(self._wait_human(task, ActionDecision(decision=ActionDecisionType.CLARIFY,
                                                      reason="模型未给出明确动作，请澄清。"), store))
                result.status = "WAITING_HUMAN"
                result.success = False
                result.error_code = "ACTION_LOOP_NO_ACTION"
                result.error_message = "需要澄清下一步动作。"
                return result

            selected_objective = self._objective_for_action(task, command, decision)
            dependency = self._dependency_block_for_objective(
                task,
                selected_objective,
            )
            if dependency is not None:
                return await self._dependency_blocked_result(
                    result,
                    task,
                    store,
                    objective=selected_objective,
                    dependency=dependency,
                    iteration=i,
                )
            mutation_blockers = self._mutation_blocking_execution_ids(
                task,
                selected_objective,
            ) if selected_objective is not None else []
            if mutation_blockers:
                boundary.record_result_unknown()
                await _maybe_await(self._suspend(task, store))
                result.status = "WAITING_EXTERNAL"
                result.success = True
                result.execution_id = next((e for e in mutation_blockers if e), "")
                result.content = "A conflicting mutation is still unresolved; wait for reconciliation."
                return result

            # Resume guard: never re-submit a write whose expected resource is
            # already present from a verified/terminal execution.  A duplicated
            # write would be a second side effect even without a fallback.
            if self._already_satisfied(
                task,
                action,
                objective=selected_objective,
            ) and action not in _MUTATION_ACTIONS:
                result.observations.append(ActionObservation(
                    iteration=i, action=action, outcome="ALREADY_SATISFIED", ok=True,
                    message="该动作已由既有结果满足，无需重复执行。",
                ))
                await _maybe_await(store._record(task, "already_satisfied", action))
                continue

            observation = await self._act(
                action,
                decision,
                task,
                command,
                request,
                boundary,
                task_store=store,
                mutation_plan_selected=mutation_is_allowed,
            )
            result.observations.append(observation)
            if action == "ANSWER_FROM_KNOWLEDGE" and observation.outcome == "SUCCESS" and observation.ok:
                self._record_direct_result_artifact(task, observation)
                answer_text = _direct_answer_text(observation)
                if answer_text:
                    # The canonical tool has already performed grounded
                    # generation. Preserve that exact result for the final
                    # Runtime envelope; a generic completion sentence would
                    # discard the user's answer.
                    result.content = answer_text
            # Progress is recorded only after the write boundary accepted or
            # verified this exact mutation.  Marking it while constructing the
            # decision would make the final mutation look non-mutation to the
            # allow-list check, and would also lose a mutation after a failed
            # submission.
            if mutation_is_allowed and observation.outcome in {
                "SUCCESS",
                "SUBMITTED",
                "RESULT_UNKNOWN",
            }:
                resource_id = str(
                    (decision.arguments or {}).get("schedule_id")
                    or (decision.arguments or {}).get("draft_id")
                    or (decision.arguments or {}).get("post_id")
                    or (decision.arguments or {}).get("resource_id")
                    or ""
                )
                objective_id = str(
                    (decision.arguments or {}).get("objective_id")
                    or getattr(observation, "objective_id", "")
                    or ""
                )
                if resource_id or objective_id:
                    self._mutation_done.add(
                        self._mutation_key(task_id, action, resource_id, objective_id)
                    )
            try:
                from greenbook_agent_core.command.interpreter import _debug_structured_stage
                _debug_structured_stage(
                    "actionloop_observation",
                    {
                        "iteration": i,
                        "action": action,
                        "outcome": observation.outcome,
                        "ok": observation.ok,
                        "resource_id": observation.resource_id,
                        "resource_kind": observation.resource_kind,
                        "objective_id": str(getattr(observation, "objective_id", "") or ""),
                        "message": observation.message,
                        "detail": dict(observation.detail or {}),
                    },
                )
            except Exception:  # noqa: BLE001 - diagnostics must never affect execution
                pass
            tool_calls += 1
            # Track candidates + evidence from discovery/detail reads.
            self._track_read_state(
                action, observation, candidate_state,
                attempted_pid=(dict(decision.arguments or {}).get("post_id") if action in _DETAIL_ACTIONS else None),
            )
            if tool_calls > self._max_tool_calls:
                return _budget_failure(result, "ACTION_LOOP_TOOL_BUDGET", "工具调用次数超限。")
            if observation.outcome == "FAILED":
                failures += 1
                # ToolRuntime already classifies whether a failed read is
                # safe to retry. A concrete non-retryable provider failure
                # must terminate this Objective after the first attempt;
                # replaying the same request only adds latency and can turn a
                # clear dependency outage into an opaque failure budget.
                if (observation.detail or {}).get("retryable") is False:
                    result.status = "FAILED"
                    result.success = False
                    result.iterations = i
                    result.error_code = observation.error_code or "READ_FAILED"
                    result.error_message = observation.message or "The read operation failed and is not retryable."
                    result.content = observation.message or result.error_message
                    return result
                if failures > self._max_failures:
                    return _budget_failure(result, "ACTION_LOOP_FAILURE_BUDGET", "失败次数超限，需要澄清。")

            # An authoritative empty discovery response is a valid terminal
            # read result. It must not be retried as if the tool made no
            # progress: the tool proved that the requested search/list has
            # zero rows, without inventing a resource identity.
            if self._is_empty_discovery_result(action, observation):
                empty_objective = selected_objective
                if empty_objective is None:
                    empty_objective = next(
                        (
                            item for item in (getattr(task, "objectives", ()) or ())
                            if str(getattr(item, "objective_id", "") or "")
                            == str(getattr(observation, "objective_id", "") or "")
                        ),
                        None,
                    )
                if empty_objective is not None:
                    constraints = dict(getattr(empty_objective, "constraints", {}) or {})
                    constraints["discovery_result"] = {
                        "status": "EMPTY",
                        "count": 0,
                        "action": action,
                        "tool": observation.tool_name,
                    }
                    empty_objective.constraints = constraints
                    self._mark_plan_step(plan, action, observation)
                    ObjectiveStateReducer().reduce(task)
                    persist = getattr(store, "persist_objectives", None)
                    if callable(persist):
                        await _maybe_await(persist(task))
                    await _maybe_await(store._record(task, "empty_discovery", action))
                    result.content = observation.message or "No results found."
                    if self._verify_finish(task) and not self._next_pending_mutation(task, command):
                        return await self._finished(result, task, store)
                    continue

            if observation.outcome == "WAITING_APPROVAL":
                # Write paused for human approval: the durable PlanExecution and
                # its approval request already exist.  Surface a real
                # WAITING_APPROVAL (with execution + approval identity) instead
                # of a fake WAITING_USER with nothing to approve.
                await _maybe_await(self._suspend(task, store))
                result.status = "WAITING_APPROVAL"
                result.success = True
                result.execution_id = observation.execution_id
                result.approval_id = getattr(observation, "approval_id", None)
                result.content = observation.message or "该操作需要用户确认后才能继续。"
                return result

            if observation.outcome == "SUBMITTED" or observation.outcome == "RESULT_UNKNOWN":
                # Write is durable-submitted / verification pending: WAIT, do
                # not call the model again on this Task.
                await _maybe_await(self._suspend(task, store))
                result.status = "WAITING_EXTERNAL"
                result.success = True
                result.execution_id = observation.execution_id
                result.content = observation.message or "操作已提交，等待执行结果。"
                return result

            # Read succeeded and produced one or more real resources.  Persist
            # every identity (not only the first result) so a resumed turn can
            # target the exact historical ResourceRef without an active/latest
            # fallback.  Full detail content is retained only for the exact
            # resource read; discovery rows remain identity/title-only.
            if observation.resource_id and observation.resource_kind:
                ev = _read_evidence_fields(observation)
                owner_id = str(getattr(observation, "objective_id", "") or "")
                if not owner_id:
                    # For new Business Objectives (required_capabilities set), a
                    # missing correlation id must NOT fall back to the current
                    # Objective (that could bind A's result to B after resume).
                    # Legacy compatibility Objectives keep the fallback.
                    is_new_business = any(
                        getattr(o, "required_capabilities", None)
                        for o in (getattr(task, "objectives", ()) or ())
                    )
                    if not is_new_business:
                        owner = self._current_objective(task)
                        owner_id = str(getattr(owner, "objective_id", "")) if owner is not None else ""
                refs = list(observation.resource_refs)
                if not refs:
                    refs = [ResourceRef(
                        ref=f"{str(observation.resource_kind).lower()}:{observation.resource_id}",
                        kind=str(observation.resource_kind),
                        resource_id=str(observation.resource_id),
                        tool=observation.tool_name or None,
                    )]
                seen_refs: set[tuple[str, str]] = set()
                for ref in refs:
                    resource_id = str(ref.resource_id or "")
                    if not resource_id:
                        continue
                    resource_kind = (
                        "SEARCH_RESULT"
                        if action in _DISCOVERY_ACTIONS
                        else str(ref.kind or observation.resource_kind)
                    )
                    resource_key = (resource_id, resource_kind.upper())
                    if resource_key in seen_refs:
                        continue
                    seen_refs.add(resource_key)
                    detail_observation = observation.model_copy(update={
                        "resource_id": resource_id,
                        "resource_kind": resource_kind,
                    })
                    content = (
                        ev["content"]
                        if resource_id == str(observation.resource_id)
                        and action in _DETAIL_ACTIONS
                        else ""
                    )
                    title = str(ref.title or ref.label or "") or (
                        ev["title"] if resource_id == str(observation.resource_id) else ""
                    )
                    await _maybe_await(
                        store._record_resource(
                            task, resource_id, resource_kind,
                            title, content=content,
                            objective_id=owner_id,
                        )
                    )
                    await _maybe_await(self._bind_observation(task, detail_observation, store))
            if observation.artifact_id:
                await _maybe_await(self._bind_observation(task, observation, store))
            self._mark_plan_step(plan, action, observation)
            result.task_plan = plan
            await _maybe_await(store._record(task, "act", observation.action))
            if action in _WRITE_ACTIONS:
                last_read_observation_key = None
                read_equivalent_streak = 0
                no_progress, previous_progress_key, no_progress_streak = self._record_progress_and_check_no_progress(
                    result, iteration_before, task, plan, i, action,
                    previous_progress_key, no_progress_streak,
                    execution_submitted=observation.outcome in {"SUBMITTED", "RESULT_UNKNOWN"},
                    waiting=observation.outcome in {"SUBMITTED", "RESULT_UNKNOWN", "WAITING_APPROVAL"},
                )
                if no_progress:
                    return _no_progress_failure(result, task, plan, i, action)
            else:
                self._append_progress_trace(
                    result, iteration_before, self._progress_snapshot(task, plan),
                    iteration=i, semantic_action=action, replan=False,
                    execution_submitted=False, waiting=False,
                )
                read_key = _read_observation_signature(
                    action, observation, dict(decision.arguments or {})
                )
                if observation.outcome == "SUCCESS" and observation.ok:
                    if read_key == last_read_observation_key:
                        read_equivalent_streak += 1
                    else:
                        read_equivalent_streak = 1
                    last_read_observation_key = read_key
                    if read_equivalent_streak >= _READ_NO_PROGRESS_THRESHOLD:
                        return _no_progress_failure(result, task, plan, i, action)
                else:
                    last_read_observation_key = None
                    read_equivalent_streak = 0

        # Exhausted the iteration budget without a terminal state.
        result.status = "FAILED"
        result.error_code = "ACTION_LOOP_ITERATION_BUDGET"
        result.error_message = "任务未能在有限步数内完成，请重试或澄清。"
        result.success = False
        return result

    # ── Observe ───────────────────────────────────────────────────────

    async def _observe(self, task: Any, command: Command | None, assembled_context: Any) -> dict[str, Any]:
        """Gather a bounded observation of the Task's current state.

        Reuses ContextAssembler when available; otherwise projects the Task
        directly.  No full chat history / execution logs / lease/checkpoint are
        included.
        """
        if self._context_assembler is not None:
            assemble = getattr(self._context_assembler, "assemble", None)
            if callable(assemble):
                try:
                    value = assemble(task=task, command=command)
                    return await value if inspect.isawaitable(value) else value
                except Exception:  # noqa: BLE001 - fall through to projection
                    pass
        return self._project_task(task, command)

    def _project_task(self, task: Any, command: Command | None) -> dict[str, Any]:
        objectives = [
            {
                "objective_id": str(getattr(o, "objective_id", "")),
                "description": str(getattr(o, "description", "")),
                "intent": str(getattr(o, "intent", "")),
                "expected_resource_kind": str(getattr(o, "expected_resource_kind", "") or "").upper(),
                "status": _status_str(getattr(o, "status", "")),
            }
            for o in resolve_objectives(task)
        ]
        current = self._current_objective(task)
        command_projection = {}
        if isinstance(command, BaseModel):
            # ActionLoop consumes resolved semantic facts, never the original
            # user language.  The interpreter remains the only NLP boundary.
            command_projection = {
                "type": str(getattr(command, "type", "") or ""),
                "semantic_operation": str(getattr(command, "semantic_operation", "") or ""),
                "required_capabilities": list(getattr(command, "required_capabilities", ()) or ()),
                "target": dict(getattr(command, "resolved_target", None) or {}),
                "resolved_semantics": (
                    command.resolved_semantics.model_dump(mode="json")
                    if getattr(command, "resolved_semantics", None) is not None
                    else {}
                ),
            }
        return {
            "task_id": str(getattr(task, "task_id", "")),
            "objective": str(getattr(task, "goal", "") or ""),
            "objectives": objectives,
            # Explicitly identify the current Objective so the decision model
            # knows exactly which Objective's content to generate next.  In a
            # multi-Objective Task the loop advances to the next unsatisfied
            # Objective; without this the model cannot tell which topic to
            # write and can emit a content action with no arguments.
            "current_objective": (
                {
                    "objective_id": str(getattr(current, "objective_id", "")),
                    "description": str(getattr(current, "description", "")),
                    "intent": str(getattr(current, "intent", "")),
                    "required_capabilities": list(
                        getattr(current, "required_capabilities", None) or ()
                    ),
                    "expected_resource_kind": str(
                        getattr(current, "expected_resource_kind", "") or ""
                    ).upper(),
                }
                if current is not None
                else None
            ),
            "artifacts": [dict(a) for a in getattr(task, "artifacts", ()) or ()],
            "resources": [dict(r) for r in getattr(task, "resource_index", ()) or ()],
            "execution_statuses": [
                str(getattr(e, "status", "")) for e in getattr(task, "execution_refs", ()) or ()
            ],
            "status": str(getattr(task, "status", "")),
            "command": command_projection,
        }

    # ── Decide ────────────────────────────────────────────────────────

    async def _decide(self, context: Mapping[str, Any], task: Any) -> ActionDecision:
        maker = self._decision_maker
        if maker is None:
            maker = self._default_decision_maker
        value = maker(context)
        decision = await value if inspect.isawaitable(value) else value
        if isinstance(decision, Mapping):
            decision = ActionDecision.model_validate(decision)
        if not isinstance(decision, ActionDecision):
            raise ActionLoopError("ACTION_DECISION_INVALID", "Decision maker returned an invalid decision.")
        # Deterministic tool/capability resolution overrides any model guess,
        # so there is no second tool-selection LLM call (requirement 06).
        resolved = self._resolver(str(decision.semantic_action or "").upper())
        if resolved is not None and decision.decision in {
            ActionDecisionType.CALL_TOOL,
            ActionDecisionType.GENERATE_CONTENT,
        }:
            capability, tool_name = resolved
            if not decision.capability:
                decision = decision.model_copy(update={"capability": capability})
            if not decision.tool_name:
                decision = decision.model_copy(update={"tool_name": tool_name})
        return decision

    async def _decide_for_plan(
        self,
        context: Mapping[str, Any],
        task: Any,
        plan: TaskPlan | None,
    ) -> ActionDecision:
        """Choose the current READY plan step before asking for free routing."""
        ready = self._next_ready_plan_step(
            plan,
            objective_id=str(
                getattr(self._current_objective(task), "objective_id", "") or ""
            ),
        )
        planned_action = _PLAN_CAPABILITY_ACTION.get(
            str(getattr(ready, "capability", "") or "").upper(), ""
        ) if ready is not None else ""
        if not planned_action:
            return await self._decide(context, task)
        # SEARCH needs a topic/query from the model; its semantic action is
        # still constrained to the READY step so it cannot regress to SEARCH
        # after that step has completed.
        if planned_action == "SEARCH_POSTS":
            decision = await self._decide(context, task)
            arguments = dict(decision.arguments or {})
            if getattr(ready, "goal_id", None):
                arguments["objective_id"] = str(ready.goal_id)
            return decision.model_copy(update={
                "semantic_action": planned_action,
                "capability": "SEARCH_COMMUNITY",
                "tool_name": "community.search_public_posts",
                "arguments": arguments,
                "reason": "__PLAN__" + str(decision.reason or ""),
            })
        arguments = dict(getattr(ready, "constraints", {}) or {})
        if getattr(ready, "goal_id", None):
            arguments["objective_id"] = str(ready.goal_id)
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action=planned_action,
            arguments=arguments,
            reason="__PLAN__",
        )

    def _structured_answer_decision(
        self,
        task: Any,
        command: Command | None,
    ) -> ActionDecision | None:
        """Admit the resolved grounded-answer capability deterministically.

        ANSWER_FROM_KNOWLEDGE is a final read result, while SEARCH_COMMUNITY
        is only a supporting capability of that result. Once the semantic
        boundary has supplied the capability, the ActionLoop must preserve it
        instead of handing read selection back to its LLM.

        Arguments are copied only from structured command/objective facts. If
        no question fact survived interpretation, fail closed with
        clarification rather than forwarding an empty MCP request or using raw
        user text.
        """
        objective = self._current_objective(task)
        if objective is None or not _objective_requires_answer(objective, command):
            return None
        arguments = _structured_answer_arguments(command, objective)
        objective_id = str(getattr(objective, "objective_id", "") or "")
        if not arguments.get("question"):
            return ActionDecision(
                decision=ActionDecisionType.CLARIFY,
                reason="The community knowledge question is incomplete.",
            )
        arguments["objective_id"] = objective_id
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="ANSWER_FROM_KNOWLEDGE",
            capability="ANSWER_FROM_KNOWLEDGE",
            tool_name="community.answer_from_knowledge",
            arguments=arguments,
            reason="__STRUCTURED_ANSWER__",
        )

    @staticmethod
    def _record_direct_result_artifact(task: Any, observation: ActionObservation) -> None:
        """Bind a successful direct answer without inventing a business resource.

        The answer payload is a real read artifact, not a Draft/Post/Schedule.
        Its id is derived from the observed structured payload so a replay of
        the same verified result remains idempotent and objective ownership is
        still explicit.
        """
        detail = dict(getattr(observation, "detail", None) or {})
        structured = detail.get("structured_data")
        answer = _direct_answer_text(observation)
        facts = dict(getattr(observation, "verified_facts", None) or {})
        fingerprint = str(
            facts.get("data_fingerprint")
            or _payload_fingerprint(structured)
            or ""
        )
        if not answer or not fingerprint:
            return
        artifact_id = f"knowledge-answer:{fingerprint}"
        try:
            observation.artifact_id = artifact_id
        except Exception:  # pragma: no cover - defensive for injected observations
            return
        artifacts = getattr(task, "artifacts", None)
        if not isinstance(artifacts, list):
            return
        if any(
            str(getattr(item, "artifact_id", "") or "") == artifact_id
            for item in artifacts
        ):
            return
        artifacts.append(ArtifactRef(
            artifact_id=artifact_id,
            task_id=str(getattr(task, "task_id", "") or ""),
            artifact_type="KNOWLEDGE_ANSWER",
            summary=answer[:2000],
        ))

    async def _default_decision_maker(self, context: Mapping[str, Any]) -> ActionDecision:
        raise ActionLoopError(
            "ACTION_DECISION_MAKER_UNAVAILABLE",
            "No decision maker or LLM is configured for the ActionLoop.",
        )

    # ── Act ───────────────────────────────────────────────────────────

    async def _act(
        self,
        action: str,
        decision: ActionDecision,
        task: Any,
        command: Command | None,
        request: Any,
        boundary: Any,
        *,
        task_store: Any = None,
        mutation_plan_selected: bool = False,
    ) -> ActionObservation:
        resolved = self._resolver(action)
        tool_name = decision.tool_name or (resolved[1] if resolved else "")
        capability = decision.capability or (resolved[0] if resolved else "")
        current_objective = self._objective_for_action(task, command, decision)
        args = _normalize_arguments(
            action, dict(decision.arguments or {}), command,
            objective=current_objective,
        )

        # Defense in depth only: normal admission stops a pending Task before
        # ActionLoop starts.  If a stale/manual continuation reaches a WRITE,
        # the Task-level confirmation gate still blocks the durable boundary.
        if action in _WRITE_ACTIONS and _semantic_confirmation_blocks_write(task):
            return ActionObservation(
                iteration=0,
                action=action,
                outcome="FAILED",
                ok=False,
                message="semantic confirmation is required before a write",
                detail={"code": "SEMANTIC_CONFIRMATION_REQUIRED"},
            )

        # ActionLoop may select the next semantic action, but final write
        # admission belongs immediately before the durable boundary.  The
        # guard only checks current action qualification and business
        # preconditions; it does not parse language or plan work.
        if action in _WRITE_ACTIONS and current_objective is not None:
            guard = guard_action(
                action,
                current_objective,
                task,
                command=command,
                arguments=args,
                mutation_plan_selected=mutation_plan_selected,
            )
            if not guard.allowed:
                return ActionObservation(
                    iteration=0,
                    action=action,
                    outcome="FAILED",
                    ok=False,
                    message=guard.reason or guard.code or "write action rejected",
                    detail={
                        "code": guard.code,
                        "action": action,
                        "objective_id": str(getattr(current_objective, "objective_id", "") or ""),
                        "retryable": False,
                    },
                )

        # Capture the action-initiation objective_id ONCE here (the Objective
        # driving this action), so a verified Resource later binds to the SAME
        # Objective even across WAITING_EXTERNAL/resume, never re-inferred from
        # the result-time current Objective.
        action_objective_id = str(getattr(current_objective, "objective_id", "")) if current_objective is not None else ""
        # Hard invariant for new Business Objectives (required_capabilities set):
        # a WRITE with no objective_id must fail BEFORE any side effect — no
        # Operation, no queue, no Java call — because the resource could never
        # be owned.  Legacy compatibility Objectives (no required_capabilities)
        # still allow objective_id=None.
        is_new_business = bool(getattr(current_objective, "required_capabilities", None)) if current_objective is not None else False
        if action in _WRITE_ACTIONS and is_new_business and not action_objective_id:
            return ActionObservation(
                iteration=0, action=action, outcome="FAILED", ok=False,
                message="新业务目标缺少 objective_id，无法为写入结果建立归属，已拒绝提交。",
            )
        if action in _WRITE_ACTIONS:
            lock = None
            if mutation_plan_selected:
                task_id = str(getattr(task, "task_id", "") or "")
                lock = self._mutation_commit_locks.setdefault(
                    task_id,
                    asyncio.Lock(),
                )
                await lock.acquire()
            try:
                obs = await self._submit_write(
                    task, command, request, action, capability, tool_name, args, boundary,
                    objective_id=action_objective_id,
                )
                if (
                    mutation_plan_selected
                    and obs.outcome in {"SUBMITTED", "RESULT_UNKNOWN"}
                    and task_store is not None
                ):
                    record_submission = getattr(task_store, "record_mutation_submission", None)
                    if callable(record_submission):
                        submission_kwargs = {
                            "action": action,
                            "arguments": dict(args),
                            "execution_id": str(obs.execution_id or ""),
                        }
                        # Existing injectable stores may implement the older
                        # correlation contract.  Persist Objective ownership
                        # whenever the store supports it, while retaining
                        # compatibility with those readers without creating a
                        # second ledger or queue.
                        try:
                            parameters = inspect.signature(record_submission).parameters
                            if (
                                "objective_id" in parameters
                                or any(
                                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                                    for parameter in parameters.values()
                                )
                            ):
                                submission_kwargs["objective_id"] = action_objective_id
                        except (TypeError, ValueError):
                            pass
                        await _maybe_await(record_submission(task, **submission_kwargs))
            finally:
                if lock is not None:
                    lock.release()
        else:
            boundary.record_read()
            obs = await self._do_read(task, command, request, action, capability, tool_name, args)
        with suppress(Exception):
            obs.objective_id = action_objective_id
        return obs

    async def _submit_write(
        self, task: Any, command: Command | None, request: Any,
        action: str, capability: str, tool_name: str, args: dict[str, Any], boundary: Any,
        objective_id: str = "",
    ) -> ActionObservation:
        submitter = self._write_submitter
        if submitter is None:
            return ActionObservation(iteration=0, action=action, outcome="FAILED",
                                     message="写入执行边界不可用。")
        boundary.record_operation_submitted(tool_name=tool_name)
        value = submitter(
            tool_name=tool_name,
            arguments=args,
            capability=capability,
            semantic_action=action,
            task=task,
            command=command,
            request=request,
            objective_id=objective_id,
        )
        value = await value if inspect.isawaitable(value) else value
        status = _result_status(value)
        execution_id = _result_execution_id(value)
        ok = _result_ok(value)
        resource_id = _result_resource_id(value)
        approval_id = (
            value.get("approval_id")
            if isinstance(value, Mapping)
            else getattr(value, "approval_id", None)
        )
        if status in {"COMPLETED", "SUCCESS"} and resource_id:
            return ActionObservation(
                iteration=0, action=action, outcome="SUCCESS", ok=True,
                resource_id=resource_id,
                resource_kind=_ACTION_RESOURCE_KIND.get(action),
                execution_id=execution_id,
                message=str(value.get("message") or _dict_get(value, "content") or "完成。"),
                detail=dict(value) if isinstance(value, Mapping) else {},
            )
        # A write paused for human approval is a real durable state, never a
        # failure: the PlanExecution exists (execution_id) and the approval
        # request is durable (approval_id), so the loop surfaces a real
        # WAITING_APPROVAL instead of a fake WAITING_USER.
        if status in {"WAITING_APPROVAL", "WAITING_HUMAN"} and execution_id:
            return ActionObservation(
                iteration=0, action=action, outcome="WAITING_APPROVAL", ok=True,
                execution_id=execution_id,
                approval_id=approval_id,
                message=_result_message(value) or "该操作需要用户确认后才能继续。",
                detail=dict(value) if isinstance(value, Mapping) else {},
            )
        # Submitted but not yet verified -> the loop must WAIT, but only when a
        # durable execution was actually materialized.  A submit that reports ok
        # with no execution_id (or returns None) means no PlanExecution exists to
        # wait on; reporting SUBMITTED here would fabricate WAITING_EXTERNAL and a
        # falsely COMPLETED run with no real execution behind it (invariant:
        # WAITING_EXTERNAL <=> a real non-terminal execution).
        if ok and execution_id:
            return ActionObservation(
                iteration=0, action=action, outcome="SUBMITTED", ok=True,
                execution_id=execution_id,
                message=_result_message(value),
                detail=dict(value) if isinstance(value, Mapping) else {},
            )
        return ActionObservation(
            iteration=0, action=action, outcome="FAILED", ok=False,
            execution_id=execution_id,
            message=_result_message(value)
            or "写入提交失败：未产生可等待的持久化执行。",
            detail=dict(value) if isinstance(value, Mapping) else {},
        )

    async def _do_read(
        self, task: Any, command: Command | None, request: Any,
        action: str, capability: str, tool_name: str, args: dict[str, Any],
    ) -> ActionObservation:
        handler = self._read_handler
        if handler is None:
            return ActionObservation(
                iteration=0,
                action=action,
                tool_name=tool_name,
                task_id=str(getattr(task, "task_id", "") or ""),
                query=str(args.get("query") or args.get("question") or ""),
                input_fingerprint=_input_fingerprint(args),
                outcome="FAILED",
                error_code="INTERNAL_ERROR",
                message="读取执行边界不可用。",
            )
        try:
            try:
                from greenbook_agent_core.command.interpreter import _debug_structured_stage
                _debug_structured_stage("tool_request", {"action": action, "capability": capability, "tool_name": tool_name, "arguments": dict(args)})
            except Exception:  # noqa: BLE001
                pass
            value = handler(tool_name=tool_name, arguments=args, task=task,
                            command=command, request=request)
            value = await value if inspect.isawaitable(value) else value
        except Exception as exc:  # noqa: BLE001 - failure is an observation
            try:
                from greenbook_agent_core.command.interpreter import _debug_structured_stage
                _debug_structured_stage("tool_exception", {"action": action, "tool_name": tool_name, "error_type": type(exc).__name__, "error": str(exc)[:1000]})
            except Exception:  # noqa: BLE001
                pass
            return ActionObservation(
                iteration=0,
                action=action,
                tool_name=tool_name,
                task_id=str(getattr(task, "task_id", "") or ""),
                query=str(args.get("query") or args.get("question") or ""),
                input_fingerprint=_input_fingerprint(args),
                outcome="FAILED",
                error_code="INTERNAL_ERROR",
                message=str(exc) or "读取失败。",
            )
        payload = _as_mapping(value)
        ok = _result_ok(payload)
        try:
            from greenbook_agent_core.command.interpreter import _debug_structured_stage
            safe = {
                key: payload.get(key)
                for key in ("ok", "success", "status", "code", "error_code", "error", "message", "user_message")
                if key in payload
            }
            safe["response_keys"] = sorted(str(key) for key in payload)[:40]
            _debug_structured_stage("tool_response", {"action": action, "tool_name": tool_name, "ok": ok, "value": safe})
        except Exception:  # noqa: BLE001
            pass
        detail = self._project_read_observation(
            payload,
            action=action,
            tool_name=tool_name,
        )
        # Prefer a real source id (first post_id) over the stringified content
        # so the recorded resource is a traceable fact, not a blob of text.
        resource_refs = _resource_ref_models(detail.get("resource_refs") or [])
        resource_id = _result_resource_id(payload) or (
            resource_refs[0].resource_id if resource_refs else None
        )
        if action == "GET_POST":
            requested_id = str(args.get("post_id") or "")
            # Detail evidence is valid only when it has the canonical source
            # identity requested by this Objective.  An anonymous successful
            # envelope cannot move any state and would otherwise repeat until
            # the iteration/no-progress guard fires.
            if not requested_id or not resource_id or str(resource_id) != requested_id:
                return ActionObservation(
                    iteration=0, action=action, tool_name=tool_name,
                    task_id=str(getattr(task, "task_id", "") or ""),
                    query=str(args.get("query") or args.get("question") or ""),
                    input_fingerprint=_input_fingerprint(args),
                    outcome="FAILED", ok=False,
                    error_code="VALIDATION_ERROR",
                    message="GET_POST returned no verifiable source identity.",
                    detail=detail,
                )
        return ActionObservation(
            iteration=0,
            action=action,
            tool_name=tool_name,
            task_id=str(getattr(task, "task_id", "") or ""),
            query=str(args.get("query") or args.get("question") or ""),
            input_fingerprint=_input_fingerprint(args),
            outcome="SUCCESS" if ok else "FAILED",
            ok=ok,
            resource_id=resource_id,
            resource_kind=_ACTION_RESOURCE_KIND.get(action),
            resource_refs=resource_refs if ok else [],
            provenance=list(detail.get("provenance") or []) if ok else [],
            verified_facts=dict(detail.get("verified_facts") or {}) if ok else {},
            error_code="" if ok else _result_error_code(payload),
            message=_result_message(payload),
            detail=detail,
        )

    @staticmethod
    def _project_read_observation(
        value: Any,
        *,
        action: str = "",
        tool_name: str = "",
    ) -> dict[str, Any]:
        """Project a read result into a structured Observation.

        ``content`` stays model-facing text; ``structured_data`` keeps the real
        ToolResult truth (total/items/metrics) so the runtime never has to guess
        business state back from a string.  resource_refs/source_refs are
        extracted from the payload, not invented by the model.
        """
        detail = _as_mapping(value)
        data = detail.get("data") if isinstance(detail.get("data"), (Mapping, list)) else detail.get("structured_data")
        structured_data = data if data is not None else detail.get("payload")
        ok = _result_ok(detail)
        resource_refs = _extract_resource_ref_records(
            detail,
            action=action,
            tool_name=tool_name,
        ) if ok else []
        provenance = _normalize_provenance(detail.get("provenance") or []) if ok else []
        facts = _verified_read_facts(structured_data, resource_refs)
        detail["structured_data"] = structured_data
        detail["resource_refs"] = resource_refs
        detail["source_refs"] = [str(item.get("resource_id") or "") for item in resource_refs]
        detail["provenance"] = provenance
        detail["tool_name"] = tool_name
        detail["verified_facts"] = facts
        return detail

    def _llm_generator(self):
        """Return a generator that composes the answer from evidence via the
        loop's LLM (facts -> user-facing result).  None when no LLM is wired
        (stub/deterministic tests use their own composer generator)."""
        llm = self._llm
        if llm is None:
            return None
        model = self._model

        async def generate(intent: str, evidence: Sequence[Mapping[str, Any]]) -> str:
            from greenbook_agent_core.llm_compat import structured_call
            prompt = (
                "你是 GreenBook。基于以下真实证据，用用户意图的语言组织一段面向用户的自然语言结论。"
                "不要编造证据中不存在的事实；综合多个来源的共同点。只输出结论文本，不要引用内部 id。"
                "\n\n意图：{intent}\n\n证据：\n{evidence}\n\n"
                "只输出结论文本。"
            ).format(
                intent=intent,
                evidence="\n".join(
                    f"- [{e.get('kind')}] {e.get('title') or e.get('source_ref')}:\n    {(e.get('content') or '')[:600]}"
                    for e in evidence
                ),
            )
            response = await structured_call(llm, model, prompt, "synthesis", {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            }, {"intent": intent, "evidence_count": len(evidence)})
            try:
                payload = _extract_structured_payload(response)
                c = str((payload or {}).get("content") or "") if isinstance(payload, dict) else ""
                return c
            except Exception:  # noqa: BLE001 - fall back to empty on malformed response
                return ""
        return generate

    async def _evidence_acquisition_decision(
        self, task: Any, candidate_state: dict[str, dict[str, str]],
    ) -> ActionDecision | None:
        """Deterministic next-detail decision for a GROUNDED_SYNTHESIS Objective.

        When candidates exist and strong evidence is insufficient, return a
        GET_POST for the next PENDING candidate (in original order) so the loop
        does NOT need the LLM to choose which post to read.  Returns None when
        no acquisition should run (no candidates yet -> let LLM discover, or
        evidence already ready -> let compose run).
        """
        objective = self._pending_synthesis_objective(task)
        objective_id = str(getattr(objective, "objective_id", "") or "") if objective else ""
        candidates = candidate_state.get(objective_id, {})
        if objective is None or not candidates:
            return None
        if await self._synthesis_evidence_ready(task, objective):
            return None
        pending = next((ref for ref, st in candidates.items() if st == "PENDING"), None)
        if pending is None:
            return None  # all candidates attempted; model/handling takes over
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action="GET_POST",
            arguments={"post_id": pending, "objective_id": objective_id},
            reason="deterministic evidence acquisition",
        )

    async def _try_parallel_independent_creates(
        self,
        task: Any,
        command: Command | None,
        request: Any,
        boundary: Any,
        store: Any,
        result: ActionLoopResult,
    ) -> bool:
        """Submit a bounded batch of provably independent draft objectives.

        This is a deterministic scheduler decision, not an LLM choice.  The
        initial safe slice is intentionally narrow: one CREATE_DRAFT action per
        objective, no dependencies, no existing artifacts/resources, and no
        pending mutation plan.  Every write still crosses ``_act`` and the
        injected durable submitter; this method never invokes a tool directly.
        """
        if self._max_parallel_objectives < 2:
            return False
        if self._has_nonterminal_execution(task):
            return False
        if command is not None and getattr(command, "task_changes", None):
            return False
        if self._next_pending_mutation(task, command):
            return False

        candidates: list[Any] = []
        for objective in getattr(task, "objectives", ()) or ():
            status = str(
                getattr(getattr(objective, "status", None), "value", None)
                or getattr(objective, "status", "")
                or ""
            ).upper()
            if status in {"COMPLETED", "FAILED", "CANCELLED", "SUPERSEDED"}:
                continue
            if getattr(objective, "dependencies", None):
                continue
            if getattr(objective, "related_resource_ids", None):
                continue
            if getattr(objective, "related_artifact_ids", None):
                continue
            if getattr(objective, "related_operations", None):
                continue
            capabilities = {
                str(value).upper()
                for value in (getattr(objective, "required_capabilities", ()) or ())
            }
            if capabilities not in ({"GENERATE_CONTENT"}, {"CREATE_DRAFT"}):
                continue
            if not str(getattr(objective, "objective_id", "") or ""):
                continue
            candidates.append(objective)
            if len(candidates) >= self._max_parallel_objectives:
                break
        if len(candidates) < 2:
            return False

        async def submit(objective: Any) -> ActionObservation:
            objective_id = str(getattr(objective, "objective_id", "") or "")
            decision = ActionDecision(
                decision=ActionDecisionType.CALL_TOOL,
                semantic_action="CREATE_DRAFT",
                arguments={"objective_id": objective_id},
                reason="deterministic independent-objective scheduler",
            )
            try:
                observation = await self._act(
                    "CREATE_DRAFT",
                    decision,
                    task,
                    command,
                    request,
                    boundary,
                    task_store=store,
                )
            except Exception as exc:  # noqa: BLE001 - isolate sibling failure
                observation = ActionObservation(
                    iteration=1,
                    action="CREATE_DRAFT",
                    objective_id=objective_id,
                    outcome="FAILED",
                    ok=False,
                    error_code="PARALLEL_OBJECTIVE_EXCEPTION",
                    message="parallel objective submission failed",
                    detail={"error_type": type(exc).__name__},
                )
            observation.iteration = 1
            observation.objective_id = objective_id
            return observation

        observations = list(await asyncio.gather(*(submit(item) for item in candidates)))
        result.iterations = 1
        result.decisions.extend(
            f"1:PARALLEL:{getattr(observation, 'objective_id', '')}:CREATE_DRAFT"
            for observation in observations
        )
        result.observations.extend(observations)

        execution_ids: list[str] = []
        parallel_results: list[dict[str, Any]] = []
        for observation in observations:
            execution_id = str(getattr(observation, "execution_id", "") or "")
            if execution_id and execution_id not in execution_ids:
                execution_ids.append(execution_id)
            parallel_results.append({
                "objective_id": str(getattr(observation, "objective_id", "") or ""),
                "action": "CREATE_DRAFT",
                "outcome": str(getattr(observation, "outcome", "") or ""),
                "status": (
                    "SUBMITTED"
                    if str(getattr(observation, "outcome", "") or "").upper()
                    in {"SUBMITTED", "RESULT_UNKNOWN"}
                    else str(getattr(observation, "outcome", "") or "")
                ),
                "execution_id": execution_id,
                "resource_id": str(getattr(observation, "resource_id", "") or ""),
                "error_code": str(getattr(observation, "error_code", "") or ""),
            })
            if observation.outcome == "SUCCESS" and observation.resource_id:
                await _maybe_await(store._record_resource(
                    task,
                    str(observation.resource_id),
                    str(observation.resource_kind or "DRAFT"),
                    objective_id=str(observation.objective_id or ""),
                ))
                await _maybe_await(self._bind_observation(task, observation, store))
            self._mark_plan_step(result.task_plan, "CREATE_DRAFT", observation)

        result.partial_results["parallel_results"] = parallel_results
        result.partial_results["parallel_objectives"] = {
            "mode": "BOUNDED_OBJECTIVE_EXECUTOR",
            "max_parallel_objectives": self._max_parallel_objectives,
            "eligible_objective_ids": [
                str(getattr(item, "objective_id", "") or "") for item in candidates
            ],
        }
        result.partial_results["execution_ids"] = execution_ids
        result.task_ids = [str(getattr(task, "task_id", "") or "")]

        if execution_ids:
            boundary.record_result_unknown()
            await _maybe_await(self._suspend(task, store))
            result.status = "WAITING_EXTERNAL"
            result.success = True
            result.execution_id = execution_ids[0]
            result.content = "Independent objectives were submitted and are awaiting verification."
            return True
        if any(str(getattr(item, "outcome", "") or "").upper() == "WAITING_APPROVAL"
               for item in observations):
            await _maybe_await(self._suspend(task, store))
            result.status = "WAITING_APPROVAL"
            result.success = True
            result.execution_id = next(
                (str(getattr(item, "execution_id", "") or "") for item in observations),
                "",
            )
            result.approval_id = next(
                (str(getattr(item, "approval_id", "") or "") for item in observations),
                "",
            ) or None
            return True
        if any(str(getattr(item, "outcome", "") or "").upper() == "FAILED"
               for item in observations):
            result.status = "FAILED"
            result.success = False
            result.error_code = "PARALLEL_OBJECTIVE_FAILED"
            result.error_message = "One or more independent objectives failed."
            result.content = result.error_message
            return True
        ObjectiveStateReducer().reduce(task)
        if self._verify_finish(task):
            await self._finished(result, task, store)
        return True

    async def _synthesis_evidence_ready(self, task: Any, objective: Any) -> bool:
        intent = str(getattr(objective, "intent", "") or getattr(objective, "description", "") or "")
        composed = await _maybe_await(
            self._result_composer.compose(objective=objective, intent=intent, task=task)
        )
        return bool(composed and getattr(composed, "ready", False))

    def _track_read_state(
        self, action: str, observation: ActionObservation,
        candidate_state: dict[str, dict[str, str]],
        *,
        attempted_pid: str | None = None,
    ) -> None:
        """Track candidate/evidence state from a read observation."""
        ok = bool(observation is not None and observation.ok and observation.outcome == "SUCCESS")
        objective_id = str(getattr(observation, "objective_id", "") or "")
        if not objective_id:
            return
        candidates = candidate_state.setdefault(objective_id, {})
        refs = [
            str(ref.resource_id)
            for ref in _resource_ref_models(
                list((observation.detail or {}).get("resource_refs") or [])
                if observation is not None else []
            )
        ]
        if action in _DISCOVERY_ACTIONS:
            if ok:
                for ref in refs:
                    candidates.setdefault(str(ref), "PENDING")
        elif action in _DETAIL_ACTIONS:
            pid = attempted_pid or (observation.resource_id if observation is not None else None) or (refs[0] if refs else None)
            if pid:
                # A failed GET_POST marks that candidate FAILED so the loop moves
                # to the next PENDING candidate instead of retrying it forever.
                candidates[str(pid)] = "SUCCESS" if ok else "FAILED"

    @staticmethod
    def _hydrate_candidate_state(
        task: Any,
        candidate_state: dict[str, dict[str, str]],
    ) -> None:
        """Restore minimal durable candidate evidence for a resumed loop.

        A SEARCH_RESULT ResourceRef is an Objective-owned candidate identity.
        TaskResourceRef intentionally stores only durable identity, so a resume
        can safely re-read that exact post without consulting task-global
        latest/first-resource state.
        """
        kind_by_id = _resource_kind_by_id(task)
        for objective in getattr(task, "objectives", ()) or ():
            objective_id = str(getattr(objective, "objective_id", "") or "")
            if not objective_id:
                continue
            candidates = candidate_state.setdefault(objective_id, {})
            for resource_id in getattr(objective, "related_resource_ids", ()) or ():
                resource_id = str(resource_id)
                if "SEARCH_RESULT" in kind_by_id.get(resource_id, set()):
                    candidates.setdefault(resource_id, "PENDING")

    async def _compose_ready_synthesis(self, task: Any, store: Any) -> Any | None:
        """Deterministically compose every evidence-ready GROUNDED_SYNTHESIS
        Objective that lacks a result artifact.

        This is a pure state transition: it runs when (a) the Objective's
        result_requirement is GROUNDED_SYNTHESIS, (b) evidence is ready, and
        (c) no result artifact has been bound yet.  The LLM decides HOW to phrase
        the answer, never WHETHER to produce one.  Returns the last composed
        FinalResult (for the run's content), else None.
        """
        composed = None
        while True:
            objective = self._pending_synthesis_objective(task)
            if objective is None:
                break
            intent = str(getattr(objective, "intent", "") or getattr(objective, "description", "") or "")
            result = await _maybe_await(
                self._result_composer.compose(
                    objective=objective, intent=intent, task=task,
                    generator=self._llm_generator(),
                )
            )
            if result is None or not getattr(result, "ready", False):
                break  # not evidence-ready yet; stay on evidence acquisition
            result_artifact_id = getattr(result, "result_artifact_id", "") or f"result-{getattr(objective, 'objective_id', '')}"
            _bind_composed_result(task, objective, result_artifact_id)
            await _maybe_await(store._record(task, "composed_result", getattr(result, "source_refs", [])))
            composed = result
        return composed

    async def _compose_result(self, task: Any, store: Any, reason: str = "") -> Any | None:
        """Compose the user-facing FinalResult for the pending synthesis Objective.

        Returns the composed FinalResult when evidence is ready, else None
        (NOT_READY).  Binds the composed result artifact to the Objective so a
        GROUNDED_SYNTHESIS Objective is only COMPLETED once its result exists.
        """
        objective = self._pending_synthesis_objective(task)
        if objective is None:
            return None
        intent = str(getattr(objective, "intent", "") or getattr(objective, "description", "") or "")
        composer = self._result_composer
        composed = await _maybe_await(
            composer.compose(
                objective=objective, intent=intent, task=task,
                generator=self._llm_generator(),
            )
        )
        if composed is None or not getattr(composed, "ready", False):
            return None
        result_artifact_id = composed.result_artifact_id or f"result-{objective.objective_id}"
        with suppress(Exception):
            _bind_composed_result(task, objective, result_artifact_id)
        return composed

    @staticmethod
    def _current_objective(task: Any):
        """Return the active (non-completed) Objective for parameter binding."""
        objectives = list(getattr(task, "objectives", ()) or ())
        if not objectives:
            return None
        for objective in objectives:
            status = str(getattr(objective, "status", "") or "").upper()
            if status in {"COMPLETED", "FAILED", "CANCELLED", "SUPERSEDED"}:
                continue
            # A multi-objective Task must move to the next Objective once the
            # current one already owns all its required resources, even while
            # its persisted status is still PENDING (status is only reduced to
            # COMPLETED at finish).  Skip satisfied Objectives so the loop
            # progresses instead of re-targeting an already-done one.
            if is_objective_satisfied(task, objective):
                continue
            # Objective.dependencies are compiled into the disposable Work
            # plan.  Keep the same prerequisite gate here because this loop
            # has a deterministic WRITE shortcut that runs before plan-step
            # selection.  A blocked predecessor must never be crossed by that
            # shortcut or by an explicit model-selected objective.
            if ActionLoop._objective_has_waiting_execution(task, objective):
                continue
            if ActionLoop._dependency_block_for_objective(task, objective) is not None:
                continue
            return objective
        # There is no legal next action once every Objective is terminal.  The
        # caller's finish/recovery boundary owns Task terminal projection; a
        # fallback to objectives[0] would resurrect completed work.
        return None

    @staticmethod
    def _mutation_matches_current_objective(
        objective: Any,
        mutation_action: str,
    ) -> bool:
        """Allow a pending explicit mutation only for its active Objective.

        A Task may carry an independent read Objective next to a destructive
        mutation.  The mutation list is retained for deterministic durable
        execution, but it cannot bypass the current read Objective and trigger
        approval/side effects first.  When no active Objective exists, keep the
        legacy recovery behavior and let the pending mutation be selected.
        """

        if objective is None:
            return True
        requested = str(mutation_action or "").upper()
        if not requested:
            return False
        objective_actions = {
            _PLAN_CAPABILITY_ACTION.get(
                str(capability or "").upper(),
            )
            or str(capability or "").upper()
            for capability in (getattr(objective, "required_capabilities", ()) or ())
        }
        return requested in objective_actions

    @staticmethod
    def _objective_execution_refs(task: Any, objective: Any) -> list[Any]:
        """Return execution refs owned by an Objective, failing closed for legacy refs."""
        objective_id = str(getattr(objective, "objective_id", "") or "")
        refs = list(getattr(task, "execution_refs", ()) or ())
        owned = [
            ref
            for ref in refs
            if str(getattr(ref, "goal_id", "") or "") == objective_id
        ]
        if owned:
            return owned
        # A legacy unowned ref is safe to associate only when this is the
        # Task's single Objective.  For multi-objective Tasks, refs owned by a
        # different Objective must not freeze an independent sibling.
        if len(list(getattr(task, "objectives", ()) or ())) == 1:
            return refs
        if any(not str(getattr(ref, "goal_id", "") or "") for ref in refs):
            return refs
        return []

    @classmethod
    def _objective_has_waiting_execution(cls, task: Any, objective: Any) -> bool:
        return any(
            str(getattr(ref, "status", "") or "").upper()
            in _OBJECTIVE_WAITING_STATUSES
            for ref in cls._objective_execution_refs(task, objective)
        )

    @classmethod
    def _mutation_blocking_execution_ids(
        cls,
        task: Any,
        objective: Any,
    ) -> list[str]:
        """Find an unresolved conflicting mutation on the same resource line."""
        right = mutation_objective_details(objective)
        if not right.get("resource_id") or not right.get("domain"):
            return []
        blocking: list[str] = []
        current_id = str(getattr(objective, "objective_id", "") or "")
        for other in (getattr(task, "objectives", ()) or ()):
            if str(getattr(other, "objective_id", "") or "") == current_id:
                continue
            if mutation_objective_is_superseded(other):
                continue
            if not mutation_conflicts(other, right):
                continue
            phase = mutation_execution_state(task, other)
            if phase not in {"INFLIGHT", "UNKNOWN"}:
                continue
            refs = cls._objective_execution_refs(task, other)
            ids = [
                str(getattr(ref, "execution_id", "") or "")
                for ref in refs
                if str(getattr(ref, "status", "") or "").upper()
                in _OBJECTIVE_WAITING_STATUSES
            ]
            blocking.extend(value for value in ids if value)
            if not ids:
                # A durable submission revision without its projected ref is
                # itself an unsafe correlation gap; keep the new mutation
                # waiting even though no execution id can be displayed.
                blocking.append("")
        return list(dict.fromkeys(blocking))

    @staticmethod
    def _dependency_block_for_objective(
        task: Any,
        objective: Any | None,
    ) -> dict[str, Any] | None:
        """Return the existing prerequisite state for one Objective.

        This is only a readiness check over the dependencies already compiled
        into the disposable Work plan.  It does not create an Objective graph,
        change lifecycle, or mark downstream work as FAILED.
        """
        if objective is None:
            return None
        dependency_resolution = dict(
            (getattr(objective, "constraints", {}) or {}).get(
                "dependency_resolution", {}
            ) or {}
        )
        if str(dependency_resolution.get("status") or "").upper() == "UNRESOLVED":
            # The structured boundary reported a prerequisite relation but it
            # could not bind that reference to one materialized Objective.
            # Fail closed before any dependent tool side effect; independent
            # sibling Objectives remain selectable by _current_objective.
            return {
                "kind": "FAILED",
                "dependency_ids": [],
                "details": {
                    "status": "UNRESOLVED",
                    "references": [
                        str(value)
                        for value in (dependency_resolution.get("references") or [])
                    ],
                },
            }
        dependency_ids = [
            str(value)
            for value in (getattr(objective, "dependencies", ()) or ())
            if str(value)
        ]
        if not dependency_ids:
            return None
        objectives = {
            str(getattr(item, "objective_id", "")): item
            for item in (getattr(task, "objectives", ()) or ())
        }
        failed: list[str] = []
        waiting: list[str] = []
        details: dict[str, str] = {}
        for dependency_id in dependency_ids:
            predecessor = objectives.get(dependency_id)
            if predecessor is None:
                failed.append(dependency_id)
                details[dependency_id] = "MISSING"
                continue
            predecessor_refs = [
                ref
                for ref in (getattr(task, "execution_refs", ()) or ())
                if str(getattr(ref, "goal_id", "") or "") == dependency_id
            ]
            predecessor_status = str(
                getattr(getattr(predecessor, "status", None), "value", None)
                or getattr(predecessor, "status", "")
                or ""
            ).upper()
            ref_statuses = {
                str(getattr(ref, "status", "") or "").upper()
                for ref in predecessor_refs
            }
            if ref_statuses & {
                "RESULT_UNKNOWN",
                "VERIFYING_RESULT",
                "RECONCILING",
                "SUBMITTED",
                "RUNNING",
                "PENDING",
                "QUEUED",
                "WAITING_EXTERNAL",
                "PROCESSING",
                "UNKNOWN",
                "IN_PROGRESS",
                "WAITING",
                "FAILED_RETRYABLE",
                "RETRYABLE",
                "RETRYING",
            }:
                waiting.append(dependency_id)
                details[dependency_id] = "WAITING"
                continue
            if predecessor_status in {"FAILED", "ERROR", "CANCELLED", "SUPERSEDED"}:
                failed.append(dependency_id)
                details[dependency_id] = predecessor_status
                continue
            if predecessor_status == "COMPLETED" or is_objective_satisfied(task, predecessor):
                continue
            if ref_statuses & {"FAILED", "ERROR"}:
                failed.append(dependency_id)
                details[dependency_id] = "FAILED"
                continue
            waiting.append(dependency_id)
            details[dependency_id] = predecessor_status or "PENDING"
        if failed:
            return {
                "kind": "FAILED",
                "dependency_ids": failed,
                "details": details,
            }
        if waiting:
            return {
                "kind": "WAITING",
                "dependency_ids": waiting,
                "details": details,
            }
        return None

    @classmethod
    def _blocked_dependency_info(cls, task: Any) -> tuple[Any, dict[str, Any]] | None:
        """Find a pending Objective whose existing prerequisite is blocked."""
        for objective in (getattr(task, "objectives", ()) or ()):
            status = str(getattr(objective, "status", "") or "").upper()
            if status in {"COMPLETED", "FAILED", "CANCELLED", "SUPERSEDED"}:
                continue
            if is_objective_satisfied(task, objective):
                continue
            dependency = cls._dependency_block_for_objective(task, objective)
            if dependency is not None:
                return objective, dependency
        return None

    async def _dependency_blocked_result(
        self,
        result: ActionLoopResult,
        task: Any,
        store: Any,
        *,
        objective: Any,
        dependency: Mapping[str, Any],
        iteration: int,
    ) -> ActionLoopResult:
        """Stop before ToolRuntime when an existing prerequisite is not ready."""
        kind = str(dependency.get("kind") or "WAITING").upper()
        blocked_objective_id = str(getattr(objective, "objective_id", "") or "")
        dependency_ids = [str(value) for value in dependency.get("dependency_ids", ())]
        result.iterations = iteration
        result.status = "WAITING_HUMAN" if kind == "FAILED" else "WAITING_EXTERNAL"
        result.success = kind != "FAILED"
        result.error_code = "DEPENDENCY_BLOCKED"
        result.error_message = (
            "A required predecessor failed; dependent work was not run."
            if kind == "FAILED"
            else "A required predecessor is still unresolved; dependent work is waiting."
        )
        result.content = result.error_message
        result.partial_results["dependency_blocked"] = {
            "objective_id": blocked_objective_id,
            "dependency_ids": dependency_ids,
            "kind": kind,
            "details": dict(dependency.get("details") or {}),
        }
        result.observations.append(ActionObservation(
            iteration=iteration,
            action="DEPENDENCY_GATE",
            objective_id=blocked_objective_id,
            outcome="BLOCKED_BY_DEPENDENCY",
            ok=False,
            error_code="DEPENDENCY_BLOCKED",
            message=result.error_message,
            detail=dict(result.partial_results["dependency_blocked"]),
        ))
        if kind == "FAILED":
            await self._wait_human(
                task,
                ActionDecision(
                    decision=ActionDecisionType.CLARIFY,
                    reason=result.error_message,
                ),
                store,
            )
        else:
            await self._suspend(task, store)
        return result

    @classmethod
    def _objective_for_action(cls, task: Any, command: Any, decision: Any):
        """Use an explicitly resolved mutation Objective when available."""
        arguments = getattr(decision, "arguments", None) or {}
        objective_id = str(arguments.get("objective_id") or "") if isinstance(arguments, Mapping) else ""
        if not objective_id and command is not None:
            target = getattr(command, "resolved_target", None) or {}
            if isinstance(target, Mapping):
                objective_id = str(target.get("objective_id") or "")
        if objective_id:
            for objective in getattr(task, "objectives", ()) or ():
                if str(getattr(objective, "objective_id", "")) == objective_id:
                    return objective
        return cls._current_objective(task)

    def _next_required_write_action(self, task: Any) -> str:
        """Return a deterministic WRITE action for the current Objective's next
        unsatisfied required capability, or "" when the LLM must decide.

        A multi-capability Objective (GENERATE_CONTENT + SCHEDULE_PUBLISH) needs
        its Draft before its Schedule.  Once the Draft exists, scheduling is a
        deterministic step: re-prompting the LLM would let it keep re-picking
        the already-satisfied CREATE_DRAFT and never progress.  The Schedule's
        run_at comes from the Objective's canonical constraints and its
        draft_id from Objective ownership, both resolved later at binding.
        """
        objective = self._current_objective(task)
        if objective is None:
            return ""
        required_caps = list(getattr(objective, "required_capabilities", None) or ())
        if not required_caps:
            return ""
        # A user retry may reuse an existing Draft while applying a new title
        # or body before the remaining publication step.  Keep the existing
        # deterministic capability ordering so the Draft update cannot be
        # bypassed by the schedule shortcut below.
        if (
            "MANAGE_DRAFT" in required_caps
            and not (getattr(objective, "related_operations", None) or [])
        ):
            return "UPDATE_DRAFT"
        if "SCHEDULE_PUBLISH" not in required_caps:
            return ""
        kind_by_id = _resource_kind_by_id(task)
        owned = set(getattr(objective, "related_resource_ids", ()) or ())
        owned_drafts = tuple(
            str(rid)
            for rid in owned
            if "DRAFT" in kind_by_id.get(str(rid), set())
        )
        dependency_drafts = _dependency_draft_ids(task, objective)
        # A CREATE_SCHEDULE normally consumes the current Objective's Draft.
        # An explicit artifact dependency is the only exception: a dependent
        # Objective may consume exactly one verified predecessor Draft without
        # claiming it as its own resource.
        has_draft = len(owned_drafts) == 1 or (
            not owned_drafts and len(dependency_drafts) == 1
        )
        has_schedule = any("SCHEDULE" in kind_by_id.get(str(rid), set()) for rid in owned)
        if has_draft and not has_schedule:
            return "CREATE_SCHEDULE"
        return ""

    def _next_pending_mutation_change(self, task: Any, command: Any) -> Any | None:
        """Return the next un-executed WRITE mutation from a command's task_changes.

        An explicit mutation command (e.g. "改标题 / 改发布时间") carries desired
        changes that must be applied even though the Objective is already
        satisfied.  Each (task_id, target_resource) runs exactly once so the loop
        does not finish after the first mutation.
        """
        task_id = str(getattr(task, "task_id", "") or "")
        changes = self._mutation_changes(task, command)
        for change in changes:
            desired = dict(getattr(change, "desired_changes", None) or {})
            action = str(desired.get("semantic_action") or "").upper()
            if action not in _WRITE_ACTIONS:
                continue
            # A newer canonical mutation may have superseded this logical
            # change.  The old TaskDelta remains in audit/history, but it is
            # never selected again by a continuation or duplicate resume.
            if mutation_is_superseded(task, change):
                continue
            ref = dict(getattr(change, "target_reference", None) or {})
            resource_id = str(ref.get("id") or ref.get("resource_id") or "")
            objective_id = str(desired.get("objective_id") or ref.get("objective_id") or "")
            if not objective_id and resource_id:
                owner = self._objective_for_resource_id(task, resource_id)
                objective_id = str(getattr(owner, "objective_id", "") or "") if owner else ""
            mutation_key = self._mutation_key(task_id, action, resource_id, objective_id)
            if mutation_key in self._mutation_done or self._mutation_is_verified(task, change):
                self._mutation_done.add(mutation_key)
                continue
            return change
        return None

    def _next_pending_mutation(self, task: Any, command: Any) -> str:
        """Return the semantic action for the exact next pending mutation."""
        change = self._next_pending_mutation_change(task, command)
        if change is None:
            return ""
        desired = dict(getattr(change, "desired_changes", None) or {})
        return str(desired.get("semantic_action") or "").upper()

    @staticmethod
    def _mutation_key(task_id: str, action: str, resource_id: str, objective_id: str) -> tuple[str, str]:
        # Labels often carry no resource id. Include the resolved Objective and
        # action so two explicit mutations in one Task remain independent.
        return task_id, ":".join((objective_id, action, resource_id))

    def _mutation_decision(self, task: Any, command: Any) -> ActionDecision:
        """Build the deterministic decision for the next pending mutation."""
        selected_change = self._next_pending_mutation_change(task, command)
        action = ""
        if selected_change is not None:
            action = str(
                (getattr(selected_change, "desired_changes", None) or {}).get(
                    "semantic_action", ""
                )
            ).upper()
        # Keep the exact identity selected by _next_pending_mutation_change.
        # Re-scanning the plan by action would select the first UPDATE_SCHEDULE
        # again when A and B share the same semantic action.
        changes = [selected_change] if selected_change is not None else []
        for change in changes:
            desired = dict(getattr(change, "desired_changes", None) or {})
            if str(desired.get("semantic_action") or "").upper() != action:
                continue
            ref = dict(getattr(change, "target_reference", None) or {})
            resource_id = str(ref.get("id") or ref.get("resource_id") or "")
            objective_id = str(desired.get("objective_id") or ref.get("objective_id") or "")
            if not objective_id and resource_id:
                owner = self._objective_for_resource_id(task, resource_id)
                objective_id = str(getattr(owner, "objective_id", "") or "") if owner else ""
            arguments = {
                key: value
                for key, value in desired.items()
                if key != "semantic_action"
            }
            # TaskDelta.target_reference is already resolved and typed.  Carry
            # its concrete resource into the canonical ToolCall; falling back
            # to the active/current resource would cross Objective ownership
            # when a single turn mutates more than one target.
            target_argument = {
                "UPDATE_DRAFT": "draft_id",
                "UPDATE_SCHEDULE": "schedule_id",
                "CANCEL_SCHEDULE": "schedule_id",
                "DELETE_POST": "post_id",
                "DELETE_DRAFT": "draft_id",
                "PUBLISH_NOW": "draft_id",
            }.get(action)
            if target_argument and resource_id:
                arguments[target_argument] = resource_id
            if objective_id:
                arguments["objective_id"] = objective_id
            if action == "UPDATE_SCHEDULE" and arguments.get("run_at"):
                # Mutation time authority: set the Objective's canonical run_at
                # to the user's desired instant so UPDATE_SCHEDULE (which always
                # honours the Objective canonical) schedules the new time.
                objective = next(
                    (
                        item for item in (getattr(task, "objectives", ()) or ())
                        if str(getattr(item, "objective_id", "")) == objective_id
                    ),
                    None,
                ) or self._current_objective(task)
                if objective is not None:
                    tz = str(
                        (getattr(objective, "constraints", None) or {}).get(
                            "timezone", "Asia/Shanghai"
                        )
                    )
                    resolved = TemporalResolver().resolve(
                        str(arguments["run_at"]),
                        timezone=tz,
                    )
                    if resolved:
                        constraints = dict(getattr(objective, "constraints", None) or {})
                        constraints["run_at"] = resolved
                        objective.constraints = constraints
                        arguments["run_at"] = resolved
            return ActionDecision(
                decision=ActionDecisionType.CALL_TOOL,
                semantic_action=action,
                arguments=arguments,
            )
        return ActionDecision(
            decision=ActionDecisionType.CALL_TOOL,
            semantic_action=action,
            arguments={},
        )

    @staticmethod
    def _mutation_changes(task: Any, command: Any) -> list[Any]:
        changes = list(getattr(command, "task_changes", None) or ()) if command is not None else []
        if changes:
            return changes
        task_id = str(getattr(task, "task_id", "") or "")
        return list(ActionLoop._task_mutations_for_task(task, task_id))

    @staticmethod
    def _task_mutations_for_task(task: Any, task_id: str) -> list[Any]:
        """Rehydrate the latest explicit mutation plan from Task revisions."""
        from ..command.models import TaskDelta

        for revision in reversed(getattr(task, "revisions", ()) or ()):
            payload = dict(getattr(revision, "payload", None) or {})
            if payload.get("kind") != "ACTION_LOOP_MUTATION_PLAN":
                continue
            values = []
            for item in payload.get("task_changes") or ():
                if not isinstance(item, Mapping):
                    continue
                try:
                    values.append(TaskDelta.model_validate(item))
                except Exception:  # noqa: BLE001 - old revisions are ignored safely
                    continue
            return values
        return []

    def _mutation_changes_from_revisions(self, task: Any) -> list[Any]:
        return self._task_mutations_for_task(
            task,
            str(getattr(task, "task_id", "") or ""),
        )

    @staticmethod
    def _mutation_is_verified(task: Any, change: Any) -> bool:
        """Use existing verified ResourceBinding projections to skip a replay."""
        desired = dict(getattr(change, "desired_changes", None) or {})
        action = str(desired.get("semantic_action") or "").upper()
        ref = dict(getattr(change, "target_reference", None) or {})
        resource_id = str(ref.get("resource_id") or ref.get("id") or "")
        objective_id = str(
            desired.get("objective_id")
            or ref.get("objective_id")
            or ""
        )
        if not resource_id:
            return False
        # A completed predecessor Execution is authoritative even when its
        # ResourceBinding projection is still catching up.  The revision only
        # correlates target -> execution; it does not duplicate the status.
        for revision in reversed(getattr(task, "revisions", ()) or ()):
            payload = dict(getattr(revision, "payload", None) or {})
            if payload.get("kind") != "ACTION_LOOP_MUTATION_SUBMISSION":
                continue
            if (
                str(payload.get("action") or "").upper() == action
                and str(payload.get("resource_id") or "") == resource_id
                and (
                    not payload.get("objective_id")
                    or not objective_id
                    or str(payload.get("objective_id")) == objective_id
                )
            ):
                execution_id = str(payload.get("execution_id") or "")
                status = next(
                    (
                        str(getattr(item, "status", "") or "").upper()
                        for item in (getattr(task, "execution_refs", ()) or ())
                        if str(getattr(item, "execution_id", "") or "") == execution_id
                    ),
                    "",
                )
                if status == "COMPLETED":
                    return True
                # FAILED/CANCELLED may be retried by the normal bounded loop;
                # non-terminal status is handled by the outer WAIT boundary.
                break
        rows = [
            dict(row) if isinstance(row, Mapping) else {
                "resource_id": getattr(row, "resource_id", ""),
                "resource_kind": getattr(row, "resource_kind", ""),
                "status": getattr(row, "status", None),
                "scheduled_at": getattr(row, "scheduled_at", None),
                "title": getattr(row, "title", None),
            }
            for row in (getattr(task, "resource_index", ()) or ())
            if str((row.get("resource_id") if isinstance(row, Mapping) else getattr(row, "resource_id", "")) or "") == resource_id
        ]
        if not rows:
            return False
        row = rows[0]
        if action == "UPDATE_SCHEDULE" and desired.get("run_at"):
            expected = TemporalResolver().resolve(
                str(desired["run_at"]),
                timezone=str(
                    next(
                        (
                            getattr(item, "constraints", {}).get("timezone", "Asia/Shanghai")
                            for item in (getattr(task, "objectives", ()) or ())
                            if resource_id in set(getattr(item, "related_resource_ids", ()) or ())
                        ),
                        "Asia/Shanghai",
                    )
                ),
            )
            actual = str(row.get("scheduled_at") or "")
            if expected and actual:
                return _canonical_time_equal(actual, expected)
        if action == "CANCEL_SCHEDULE":
            return str(row.get("status") or "").upper() == "CANCELLED"
        if action == "UPDATE_DRAFT" and desired.get("title"):
            return str(row.get("title") or "") == str(desired["title"])
        return False

    @staticmethod
    def _objective_for_resource_id(task: Any, resource_id: str) -> Any | None:
        """Resolve an explicit business resource through its immutable owner."""
        rid = str(resource_id or "")
        if not rid:
            return None
        for objective in getattr(task, "objectives", ()) or ():
            if mutation_objective_is_superseded(objective):
                continue
            if rid in {str(value) for value in (getattr(objective, "related_resource_ids", ()) or ())}:
                return objective
        return None

    @staticmethod
    def _pending_synthesis_objective(task: Any):
        for objective in getattr(task, "objectives", ()) or ():
            requirement = str(getattr(objective, "result_requirement", "") or "").upper()
            status = str(getattr(objective, "status", "") or "").upper()
            if requirement != "GROUNDED_SYNTHESIS":
                continue
            if status in {"COMPLETED", "FAILED", "CANCELLED", "SUPERSEDED"}:
                continue
            # Already composed -> not pending; prevents a compose busy-loop before
            # the reducer marks the Objective COMPLETED.
            if getattr(objective, "related_artifact_ids", None):
                continue
            return objective
        return None

    async def _bind_observation(self, task: Any, observation: Any, store: Any) -> None:
        """Deterministically bind a verified result to its matching Objective."""
        if not observation.resource_kind and not getattr(observation, "artifact_id", None):
            return
        objective_id = str(getattr(observation, "objective_id", "") or "")
        objective = next(
            (
                item for item in (getattr(task, "objectives", ()) or ())
                if str(getattr(item, "objective_id", "")) == objective_id
            ),
            None,
        ) if objective_id else None
        # New business Objectives require immutable action correlation.  A
        # missing owner must never fall through to a task-global/first pending
        # Objective, because that silently cross-binds A's resource to B.
        if objective is None:
            is_new_business = any(
                getattr(item, "required_capabilities", None)
                for item in (getattr(task, "objectives", ()) or ())
            )
            if not is_new_business:
                if observation.resource_kind:
                    objective = objective_for_resource(task, observation.resource_kind)
        if objective is None:
            return
        bind_related(
            task,
            objective_id=objective.objective_id,
            resource_id=observation.resource_id,
            resource_kind=observation.resource_kind,
            artifact_id=getattr(observation, "artifact_id", None),
            operation_id=observation.execution_id,
        )
        await _maybe_await(store._record(task, "bound_objective", objective.objective_id))

    @staticmethod
    def _refresh_plan_status(task: Any, plan: TaskPlan | None) -> None:
        """Reconcile disposable step status from verified Objective resources."""
        if plan is None:
            return
        resources = {
            str(item.get("resource_id") if isinstance(item, Mapping) else getattr(item, "resource_id", "")): str(
                item.get("resource_kind", "") if isinstance(item, Mapping) else getattr(item, "resource_kind", "")
            ).upper()
            for item in (getattr(task, "resource_index", ()) or ())
        }
        expected_kind = {
            "SEARCH_COMMUNITY": "SEARCH_RESULT",
            "ANSWER_FROM_KNOWLEDGE": "KNOWLEDGE_ANSWER",
            "GET_POST_DETAIL": "POST",
            "GENERATE_CONTENT": "DRAFT",
            "SCHEDULE_PUBLISH": "SCHEDULE",
            "PUBLISH_NOW": "POST",
        }
        objectives = {
            str(getattr(item, "objective_id", "")): item
            for item in (getattr(task, "objectives", ()) or ())
        }
        for step in plan.steps:
            if str(step.status or "").upper() == "COMPLETED":
                continue
            objective = objectives.get(str(step.goal_id or ""))
            owned = set(getattr(objective, "related_resource_ids", ()) or ()) if objective else set()
            if (
                str(step.capability or "").upper() == "ANSWER_FROM_KNOWLEDGE"
                and objective is not None
                and getattr(objective, "related_artifact_ids", None)
            ):
                step.status = "COMPLETED"
                continue
            kind = expected_kind.get(str(step.capability or "").upper())
            if kind and any(resources.get(rid) == kind for rid in owned):
                step.status = "COMPLETED"

    @staticmethod
    def _next_ready_plan_step(
        plan: TaskPlan | None,
        *,
        objective_id: str = "",
    ) -> PlanStep | None:
        if plan is None:
            return None
        objective_id = str(objective_id or "")
        statuses = {step.step_id: str(step.status or "PENDING").upper() for step in plan.steps}
        for step in plan.steps:
            # A multi-objective plan is one disposable projection shared by
            # the loop.  Once Objective A is verified, the next decision must
            # select only Objective B's READY step; scanning the full list can
            # resurrect an earlier completed objective after a durable resume.
            if objective_id and str(getattr(step, "goal_id", "") or "") != objective_id:
                continue
            if statuses.get(step.step_id) in {"COMPLETED", "FAILED", "CANCELLED"}:
                continue
            if all(statuses.get(dep, "PENDING") == "COMPLETED" for dep in step.depends_on):
                # ANALYZE_CONTENT_PATTERNS is an in-loop synthesis phase, not
                # a standalone tool.  Once its read dependencies are real,
                # close the virtual step and expose the next executable action.
                if str(step.capability or "").upper() == "ANALYZE_CONTENT_PATTERNS":
                    step.status = "COMPLETED"
                    statuses[step.step_id] = "COMPLETED"
                    continue
                step.status = "READY"
                return step
        return None

    @staticmethod
    def _mark_plan_step(plan: TaskPlan | None, action: str, observation: ActionObservation) -> None:
        if plan is None or observation.outcome not in {"SUCCESS", "ALREADY_DONE"}:
            return
        capability = next(
            (name for name, semantic in _PLAN_CAPABILITY_ACTION.items() if semantic == action),
            "",
        )
        objective_id = str(getattr(observation, "objective_id", "") or "")
        for step in plan.steps:
            if (
                str(step.capability or "").upper() == capability
                and (not objective_id or str(step.goal_id or "") == objective_id)
                and str(step.status).upper() != "COMPLETED"
            ):
                step.status = "COMPLETED"
                return

    # ── finish / wait / replan ────────────────────────────────────────

    def _verify_finish(self, task: Any) -> bool:
        """FINISH only when every pending objective is backed by a real resource."""
        if self._has_nonterminal_execution(task):
            return False
        # Recompute states deterministically, then require all satisfied.
        ObjectiveStateReducer().reduce(task)
        return all_objectives_satisfied(task)

    @staticmethod
    def _is_empty_discovery_result(action: str, observation: ActionObservation) -> bool:
        """Return whether a successful discovery read authoritatively has zero rows."""

        if action not in _DISCOVERY_ACTIONS or not observation.ok or observation.outcome != "SUCCESS":
            return False
        if observation.resource_id or observation.resource_refs:
            return False
        detail = dict(observation.detail or {})
        data = detail.get("structured_data")
        if not isinstance(data, Mapping):
            data = detail.get("data")
        if not isinstance(data, Mapping):
            return False
        items = data.get("items")
        total = data.get("total")
        return (
            isinstance(items, list)
            and not items
            and total in (None, 0, "0")
        ) or (
            "items" not in data
            and total in (0, "0")
        )

    def _has_nonterminal_execution(
        self,
        task: Any,
        *,
        objective_id: str = "",
    ) -> list[str]:
        """Return the execution ids of in-flight/results-unknown executions.

        A non-empty list means the Task must not be reasoned over further.  The
        caller (the WAITING_EXTERNAL guard) uses the ids to keep the Run
        non-terminal, so a suspended Task is never converged to COMPLETED while
        a real execution is still in flight.
        """
        pending: list[str] = []
        seen: set[str] = set()
        refs = list(getattr(task, "execution_refs", ()) or ())
        if objective_id:
            owned = [
                ref
                for ref in refs
                if str(getattr(ref, "goal_id", "") or "") == objective_id
            ]
            if owned:
                refs = owned
            elif any(not str(getattr(ref, "goal_id", "") or "") for ref in refs):
                # Legacy unowned refs cannot be safely attributed to a sibling.
                refs = [
                    ref
                    for ref in refs
                    if not str(getattr(ref, "goal_id", "") or "")
                ]
            else:
                refs = []
        for ref in refs:
            status = str(getattr(ref, "status", "")).upper()
            if status in _NONTERMINAL_STATUSES:
                execution_id = str(getattr(ref, "execution_id", "") or "")
                if execution_id not in seen:
                    pending.append(execution_id)
                    seen.add(execution_id)
        # A submission revision can be durable before the TaskExecutionRef
        # projection commits.  Treat that narrow correlation gap as unknown,
        # never as permission to submit a conflicting mutation.
        execution_refs = {
            str(getattr(ref, "execution_id", "") or ""):
            str(getattr(ref, "status", "") or "").upper()
            for ref in (getattr(task, "execution_refs", ()) or ())
        }
        for revision in getattr(task, "revisions", ()) or ():
            payload = dict(getattr(revision, "payload", None) or {})
            if payload.get("kind") != "ACTION_LOOP_MUTATION_SUBMISSION":
                continue
            if objective_id and str(payload.get("objective_id") or "") != objective_id:
                continue
            execution_id = str(payload.get("execution_id") or "")
            if not execution_id or execution_id in seen:
                continue
            status = execution_refs.get(execution_id, "")
            # Older correlation revisions predate Objective/domain metadata and
            # cannot by themselves prove the current mutation's commit state;
            # keep their compatibility replay behavior.  New mutation
            # submissions carry objective/domain metadata, so a missing ref is
            # the real CAS commit gap and must wait for reconciliation.
            enriched = bool(payload.get("objective_id") or payload.get("mutation_domain"))
            if (enriched and not status) or status in _NONTERMINAL_STATUSES:
                pending.append(execution_id)
                seen.add(execution_id)
        return pending

    @staticmethod
    def _already_satisfied(
        task: Any,
        action: str,
        *,
        objective: Any | None = None,
    ) -> bool:
        """Return True when the current Objective already owns the expected
        resource for a create write.

        Objective-scoped: Objective B's CREATE_DRAFT must NOT be treated as
        "already satisfied" just because Objective A already owns a draft in the
        shared Task resource_index.  Only the current Objective's OWN owned
        resources guard a re-submit, so each Objective can create its own
        Draft and Schedule.  Update/cancel/reply remain re-runnable.
        """
        expected_kind = _ACTION_RESOURCE_KIND.get(action)
        if not expected_kind or action not in {"CREATE_DRAFT", "CREATE_SCHEDULE"}:
            return False
        kind_by_id = _resource_kind_by_id(task)
        if objective is not None:
            # Objective-scoped: each Objective creates its own Draft/Schedule,
            # so Objective B's CREATE_DRAFT is NOT blocked by Objective A's
            # already-owned draft in the shared Task resource_index.
            owned = set(getattr(objective, "related_resource_ids", ()) or ())
            return any(expected_kind in kind_by_id.get(str(rid), set()) for rid in owned)
        # Legacy / no-objective path: a task-global verified resource guards a
        # re-submit (single-task historical shape).
        return any(expected_kind in kinds for kinds in kind_by_id.values())

    async def _apply_plan(self, task: Any, decision: ActionDecision, store: Any) -> list[ActionStepPlan]:
        steps = [dict(s) for s in decision.plan_steps or ()]
        await _maybe_await(store._record(task, "plan", steps))
        return list(decision.plan_steps or ())

    @staticmethod
    def _progress_snapshot(task: Any, plan: TaskPlan | None) -> dict[str, Any]:
        """Capture only state that can prove an ActionLoop iteration moved."""
        objectives = tuple(
            (
                str(getattr(item, "objective_id", "")),
                str(getattr(item, "status", "")),
                tuple(str(value) for value in (getattr(item, "related_resource_ids", ()) or ())),
            )
            for item in (getattr(task, "objectives", ()) or ())
        )
        steps = tuple(
            (
                str(getattr(step, "step_id", "")),
                str(getattr(step, "status", "")),
            )
            for step in (getattr(plan, "steps", ()) or ())
        )
        # A resource identity is (owner, kind, id), not id alone.  SEARCH and
        # GET_POST legitimately refer to the same post id while producing two
        # different verified ResourceRefs; treating that as no progress would
        # reject a valid Observation transition.
        resources = tuple(sorted(
            "{}:{}:{}".format(
                str(getattr(item, "objective_id", "") or (item.get("objective_id", "") if isinstance(item, Mapping) else "")),
                str(getattr(item, "resource_kind", "") or (item.get("resource_kind", "") if isinstance(item, Mapping) else "")).upper(),
                str(getattr(item, "resource_id", "") or (item.get("resource_id", "") if isinstance(item, Mapping) else "")),
            )
            for item in (getattr(task, "resource_index", ()) or ())
        ))
        current = ActionLoop._current_objective(task)
        return {
            "objective_id": str(getattr(current, "objective_id", "") or ""),
            "plan_id": str(getattr(plan, "plan_id", "") or ""),
            "step_id": next((step_id for step_id, status in steps if status.upper() not in {"COMPLETED", "FAILED", "CANCELLED"}), ""),
            "semantic_action": "",
            "objectives": objectives,
            "steps": steps,
            "resources": resources,
        }

    @staticmethod
    def _append_progress_trace(
        result: ActionLoopResult,
        before: dict[str, Any],
        after: dict[str, Any],
        *,
        iteration: int,
        semantic_action: str,
        replan: bool,
        execution_submitted: bool,
        waiting: bool,
    ) -> tuple[bool, tuple[Any, ...]]:
        before_resources = set(before.get("resources", ()))
        after_resources = set(after.get("resources", ()))
        objective_changed = before.get("objectives") != after.get("objectives")
        step_changed = before.get("steps") != after.get("steps")
        resource_delta = sorted(after_resources - before_resources)
        progress = bool(objective_changed or step_changed or resource_delta or replan or execution_submitted or waiting)
        result.progress_trace.append({
            "iteration": iteration,
            "objective_id": before.get("objective_id", "") or after.get("objective_id", ""),
            "plan_id": after.get("plan_id", "") or before.get("plan_id", ""),
            "step_id": before.get("step_id", "") or after.get("step_id", ""),
            "semantic_action": semantic_action,
            "step_status_before": dict(before.get("steps", ())).get(before.get("step_id", ""), ""),
            "step_status_after": dict(after.get("steps", ())).get(before.get("step_id", ""), ""),
            "objective_status_before": {item[0]: item[1] for item in before.get("objectives", ())},
            "objective_status_after": {item[0]: item[1] for item in after.get("objectives", ())},
            "resource_delta": resource_delta,
            "replan": replan,
            "execution_submitted": execution_submitted,
            "waiting": waiting,
            "progress": progress,
        })
        key = (after.get("objective_id", ""), after.get("plan_id", ""), after.get("step_id", ""), after.get("objectives", ()), after.get("steps", ()), after.get("resources", ()))
        return progress, key

    def _record_progress_and_check_no_progress(
        self,
        result: ActionLoopResult,
        before: dict[str, Any],
        task: Any,
        plan: TaskPlan | None,
        iteration: int,
        semantic_action: str,
        previous_key: tuple[Any, ...] | None,
        streak: int,
        *,
        replan: bool = False,
        execution_submitted: bool = False,
        waiting: bool = False,
    ) -> tuple[bool, tuple[Any, ...], int]:
        after = self._progress_snapshot(task, plan)
        progress, key = self._append_progress_trace(
            result, before, after, iteration=iteration, semantic_action=semantic_action,
            replan=replan, execution_submitted=execution_submitted, waiting=waiting,
        )
        if progress or key != previous_key:
            streak = 0 if progress else 1
        else:
            streak += 1
        # Two consecutive identical no-progress states are enough to identify
        # a loop; the third observation trips the guard while preserving the
        # historical two-iteration budget contract.
        return streak >= 3, key, streak

    @staticmethod
    def _build_objective_plan(task: Any) -> TaskPlan | None:
        """Build the disposable plan projection directly from Objectives."""
        objectives = list(getattr(task, "objectives", ()) or ())
        if len(objectives) <= 1 and not any(getattr(item, "dependencies", ()) for item in objectives):
            return None
        steps: list[PlanStep] = []
        first_step_by_objective: dict[str, str] = {}
        for objective in objectives:
            oid = str(getattr(objective, "objective_id", "") or "")
            capabilities = [str(item).upper() for item in (getattr(objective, "required_capabilities", ()) or ())]
            for index, capability in enumerate(capabilities or [str(getattr(objective, "intent", "") or "TASK").upper()]):
                step_id = f"{oid}:{index}"
                first_step_by_objective.setdefault(oid, step_id)
                steps.append(PlanStep(
                    step_id=step_id,
                    ordinal=len(steps) + 1,
                    capability=capability,
                    description=str(getattr(objective, "description", "") or ""),
                    depends_on=([f"{oid}:{index - 1}"] if index else []),
                    constraints=dict(getattr(objective, "constraints", {}) or {}),
                    goal_id=oid or None,
                ))
        by_objective = {str(getattr(item, "objective_id", "") or ""): item for item in objectives}
        last_step_by_objective = {
            oid: next(step.step_id for step in reversed(steps) if step.goal_id == oid)
            for oid in first_step_by_objective
        }
        for step in steps:
            objective = by_objective.get(str(step.goal_id or ""))
            if objective is None:
                continue
            step.depends_on = [
                *list(step.depends_on),
                *[
                last_step_by_objective[dependency]
                for dependency in (getattr(objective, "dependencies", ()) or ())
                if dependency in first_step_by_objective
                ],
            ]
        return TaskPlan(
            # Stable identity across a durable write resume.  The plan is
            # rebuilt from the same Objective[] but remains the same ephemeral
            # work plan, while Execution remains the runtime truth.
            plan_id=f"objective-plan:{getattr(task, 'task_id', '')}",
            task_id=str(getattr(task, "task_id", "") or ""),
            steps=steps,
            plan_source="OBJECTIVE_ACTION_LOOP",
            plan_version=max(1, int(getattr(task, "plan_version", 0) or 0)),
        )

    @staticmethod
    def _task_plan_from_steps(task: Any, steps: list[ActionStepPlan]) -> TaskPlan | None:
        if not steps:
            return ActionLoop._build_objective_plan(task)
        return TaskPlan(
            task_id=str(getattr(task, "task_id", "") or ""),
            steps=[PlanStep(
                step_id=str(item.step_id),
                ordinal=index,
                capability=str(item.semantic_action or "").upper(),
                description=str(item.semantic_action or ""),
                depends_on=list(item.depends_on),
                constraints=dict(item.arguments or {}),
                goal_id=str(item.objective_id or "") or None,
            ) for index, item in enumerate(steps, start=1)],
            plan_source="OBJECTIVE_ACTION_LOOP_REPLAN",
            plan_version=max(1, int(getattr(task, "plan_version", 0) or 0)),
        )

    async def _wait_human(self, task: Any, decision: ActionDecision, store: Any) -> None:
        await _maybe_await(store._record(task, "wait_human", decision.reason or ""))

    async def _suspend(self, task: Any, store: Any) -> None:
        await _maybe_await(store._record(task, "suspend", ""))

    async def _finished(self, result: ActionLoopResult, task: Any, store: Any) -> ActionLoopResult:
        result.status = "COMPLETED"
        result.success = True
        # A GROUNDED_SYNTHESIS Task's answer is the composed FinalResult, never a
        # generic completion string.  DIRECT_RESULT/MUTATION tasks surface the
        # observed content (or a factual completion line).
        final_result = getattr(result, "final_result", None)
        if final_result is not None and getattr(final_result, "content", ""):
            result.content = str(final_result.content)
        elif result.content:
            result.content = result.content
        else:
            result.content = "任务目标已由真实结果满足。"
        # Persist the reduced Objective statuses (all COMPLETED) before marking
        # the Task terminal, so the durable Task carries verified objectives.
        persist = getattr(store, "persist_objectives", None)
        if callable(persist):
            await _maybe_await(persist(task))
        await _maybe_await(store._record(task, "finish", ""))
        return result


# ── task store helper (boundary kept minimal and injectable) ──────────────

class _NullComposer:
    async def compose(self, **kwargs: Any) -> None:
        return None


def _bind_composed_result(task: Any, objective: Any, result_artifact_id: str) -> None:
    """Bind the composed result artifact to its synthesis Objective (task-scoped)."""
    from .result import ResultComposer  # noqa: F401 - keeps the import surface explicit

    if not result_artifact_id:
        return
    for objective_item in getattr(task, "objectives", ()) or ():
        if str(getattr(objective_item, "objective_id", "")) != str(getattr(objective, "objective_id", "")):
            continue
        related = list(getattr(objective_item, "related_artifact_ids", ()) or ())
        if result_artifact_id not in related:
            related.append(result_artifact_id)
        objective_item.related_artifact_ids = related
        break


class _NullTaskStore:
    def _record(self, *args: Any, **kwargs: Any) -> None:
        return None

    def _record_resource(self, *args: Any, **kwargs: Any) -> None:
        return None


class _NullBoundary:
    """No-op boundary for direct ActionLoop use without a coordinator."""

    def record_operation_submitted(self, tool_name: str = "") -> None:
        return None

    def record_result_unknown(self) -> None:
        return None

    def record_read(self) -> None:
        return None


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dict(dump(mode="json"))
        except (TypeError, ValueError):
            return dict(dump())
    return {}


def _resource_ref_from_raw(
    raw: Any,
    *,
    action: str,
    tool_name: str,
) -> ResourceRef | None:
    data = _as_mapping(raw)
    if not data and raw not in (None, "") and not isinstance(raw, Mapping):
        data = {"resource_id": str(raw)}
    resource_id = str(
        data.get("resource_id")
        or data.get("resource_type_id")
        or data.get("post_id")
        or data.get("postId")
        or data.get("draft_id")
        or data.get("draftId")
        or data.get("schedule_id")
        or data.get("scheduleId")
        or data.get("comment_id")
        or data.get("commentId")
        or data.get("id")
        or ""
    )
    if not resource_id:
        return None
    default_kind = "POST" if action in {
        "SEARCH_POSTS", "LIST_OWN_POSTS", "GET_POST",
    } else str(_ACTION_RESOURCE_KIND.get(action) or action or "RESOURCE")
    kind = str(
        data.get("kind")
        or data.get("resource_type")
        or data.get("resource_kind")
        or default_kind
    ).upper()
    return ResourceRef(
        ref=str(data.get("ref") or f"{kind.lower()}:{resource_id}"),
        kind=kind,
        resource_id=resource_id,
        version=data.get("version"),
        title=str(data.get("title") or "") or None,
        label=str(data.get("label") or "") or None,
        source=str(data.get("source") or "") or None,
        tool=str(data.get("tool") or data.get("tool_name") or tool_name or "") or None,
    )


def _iter_read_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return list(data)
    if not isinstance(data, Mapping):
        return []
    for key in ("items", "posts", "results", "list"):
        if isinstance(data.get(key), list):
            return list(data[key])
    nested = data.get("data")
    if isinstance(nested, (Mapping, list)):
        return _iter_read_items(nested)
    return []


def _extract_resource_ref_records(
    value: Any,
    *,
    action: str,
    tool_name: str,
) -> list[dict[str, Any]]:
    """Extract typed real identities without inventing model-side refs."""
    payload = _as_mapping(value)
    explicit = payload.get("resource_refs") or payload.get("source_refs") or []
    raw_refs = list(explicit) if isinstance(explicit, (list, tuple)) else []
    refs: list[ResourceRef] = []
    for raw in raw_refs:
        ref = _resource_ref_from_raw(raw, action=action, tool_name=tool_name)
        if ref is not None:
            refs.append(ref)
    if not refs:
        data = payload.get("data")
        items = _iter_read_items(data)
        candidates = items or ([data] if isinstance(data, Mapping) else [])
        for item in candidates:
            ref = _resource_ref_from_raw(item, action=action, tool_name=tool_name)
            if ref is not None:
                refs.append(ref)
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (ref.kind.upper(), ref.resource_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref.model_dump(mode="json", exclude_none=True))
    return unique


def _extract_resource_refs(value: Any) -> list[str]:
    """Compatibility projection returning only real resource ids."""
    return [
        str(item.get("resource_id") or "")
        for item in _extract_resource_ref_records(value, action="", tool_name="")
        if item.get("resource_id")
    ]


def _resource_ref_models(records: list[Any]) -> list[ResourceRef]:
    result: list[ResourceRef] = []
    seen: set[tuple[str, str]] = set()
    for record in records:
        try:
            ref = record if isinstance(record, ResourceRef) else ResourceRef.model_validate(record)
        except (TypeError, ValueError):
            continue
        key = (ref.kind.upper(), ref.resource_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(ref)
    return result


def _normalize_provenance(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    for value in values:
        normalized = str(getattr(value, "value", value) or "").upper()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _payload_fingerprint(value: Any) -> str:
    try:
        import json as _json
        serialized = _json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        serialized = str(value)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16] if serialized else ""


def _verified_read_facts(
    structured_data: Any,
    resource_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "resource_count": len(resource_refs),
        "resource_ids": [str(item.get("resource_id") or "") for item in resource_refs],
        "data_fingerprint": _payload_fingerprint(structured_data) if structured_data is not None else "",
    }


def _read_evidence_fields(observation: Any) -> dict[str, str]:
    """Extract clean evidence fields (title/content) from a read observation,
    keeping structured_data instead of dumping raw JSON."""
    detail = (observation.detail if observation is not None else {}) or {}
    sd = detail.get("structured_data") if isinstance(detail.get("structured_data"), dict) else {}
    title = str(sd.get("title") or detail.get("title") or observation.message or "")[:180]
    content = str(
        sd.get("content") or sd.get("body") or sd.get("body_markdown") or sd.get("description") or ""
    )[:4000]
    return {"title": title, "content": content}


def _extract_structured_payload(response: Any) -> dict[str, Any] | None:
    """Extract a structured dict from an LLM provider response."""
    if response is None:
        return None
    try:
        message = response.choices[0].message
        parsed = getattr(message, "parsed", None)
        if parsed is not None:
            dump = getattr(parsed, "model_dump", None)
            if callable(dump):
                return dict(dump(mode="python"))
            return dict(parsed)
        content = getattr(message, "content", None)
        if isinstance(content, dict):
            return content
        if isinstance(content, str) and content.strip():
            import json as _json

            from greenbook_agent_core.llm_compat import extract_top_level_json
            return _json.loads(extract_top_level_json(content))
    except Exception:  # noqa: BLE001 - malformed provider response is a controlled no-result
        return None
    return None


def _semantic_confirmation_blocks_write(task: Any) -> bool:
    if not bool(getattr(task, "requires_confirmation", False)):
        return False
    state = str(
        getattr(
            getattr(task, "confirmation_state", ""),
            "value",
            getattr(task, "confirmation_state", ""),
        )
        or ""
    ).upper()
    return bool(
        state != "CONFIRMED"
        or getattr(task, "confirmed_version", None)
        != getattr(task, "confirmation_version", None)
    )


def _action_args_signature(arguments: dict[str, Any]) -> str:
    """Canonical, deterministic signature for a semantic action's arguments."""
    try:
        import json as _json
        return _json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(arguments)


def _input_fingerprint(arguments: dict[str, Any]) -> str:
    normalized = dict(arguments or {})
    normalized.pop("objective_id", None)
    return hashlib.sha256(
        _action_args_signature(normalized).encode("utf-8")
    ).hexdigest()[:16]


def _objective_requires_answer(objective: Any, command: Command | None) -> bool:
    capabilities = {
        str(value or "").strip().upper().replace("-", "_")
        for value in (getattr(objective, "required_capabilities", ()) or ())
    }
    if "ANSWER_FROM_KNOWLEDGE" in capabilities:
        return True
    if str(getattr(objective, "expected_resource_kind", "") or "").upper() == "KNOWLEDGE_ANSWER":
        return True
    return str(getattr(command, "semantic_operation", "") or "").upper() == "ANSWER_FROM_KNOWLEDGE"


def _structured_answer_arguments(
    command: Command | None,
    objective: Any | None,
) -> dict[str, Any]:
    """Read the answer question from the bounded semantic fact containers."""
    containers: list[Any] = [
        {"question": getattr(command, "question", "")} if command is not None else None,
        getattr(command, "parameters", None),
        getattr(command, "entities", None),
        getattr(command, "constraints", None),
        {"question": getattr(getattr(command, "resolved_semantics", None), "question", "")}
        if command is not None else None,
        getattr(getattr(command, "resolved_semantics", None), "constraints", None),
        getattr(objective, "constraints", None),
    ]
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for key in ("question", "query", "search_query", "topic", "subject"):
            value = container.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                return {"question": str(value).strip()}
    return {}


def _direct_answer_text(observation: Any) -> str:
    detail = dict(getattr(observation, "detail", None) or {})
    structured = detail.get("structured_data")
    if isinstance(structured, Mapping):
        value = structured.get("answer") or structured.get("content")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_observation_signature(
    action: str,
    observation: ActionObservation,
    arguments: dict[str, Any],
) -> tuple[Any, ...]:
    refs = tuple(sorted(
        (str(ref.kind or "").upper(), str(ref.resource_id or ""))
        for ref in (observation.resource_refs or ())
    ))
    facts = dict(observation.verified_facts or {})
    return (
        str(observation.objective_id or ""),
        str(action or "").upper(),
        str(observation.tool_name or ""),
        str(observation.input_fingerprint or _input_fingerprint(arguments)),
        refs,
        str(facts.get("data_fingerprint") or ""),
    )


def _default_resolver(action: str) -> tuple[str, str] | None:
    capability = _SEMANTIC_CAPABILITY.get(action)
    tool = _SEMANTIC_TOOL.get(action)
    if capability and tool:
        return capability, tool
    return None


def _resources(task: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in getattr(task, "resource_index", ()) or ()]


def _resource_kind_by_id(task: Any) -> dict[str, set[str]]:
    """Map each verified resource id to every typed fact recorded for it.

    A discovery result and its detail may legitimately have the same business
    id but different kinds (for example SEARCH_RESULT and POST).  The typed
    ResourceRef, rather than the raw id, is the durable identity used by
    Objective satisfaction and continuation.
    """
    result: dict[str, set[str]] = {}
    for resource in (dict(item) for item in getattr(task, "resource_index", ()) or ()):
        resource_id = str(resource.get("resource_id") or "")
        resource_kind = str(resource.get("resource_kind") or "").upper()
        if resource_id and resource_kind:
            result.setdefault(resource_id, set()).add(resource_kind)
    return result


def _dependency_draft_ids(task: Any, objective: Any) -> tuple[str, ...]:
    """Return verified Drafts from explicit Objective prerequisites only.

    A dependent schedule/publish action may consume an upstream Draft as an
    artifact, but it must never discover one from task recency or a sibling's
    active session binding.  The result is intentionally empty for missing,
    failed, ambiguous, or multi-artifact prerequisites so the durable boundary
    can reject the action closed.
    """
    dependency_ids = [
        str(value)
        for value in (getattr(objective, "dependencies", ()) or ())
        if str(value)
    ]
    if not dependency_ids:
        return ()
    objectives = {
        str(getattr(item, "objective_id", "") or ""): item
        for item in (getattr(task, "objectives", ()) or ())
    }
    kind_by_id = _resource_kind_by_id(task)
    result: list[str] = []
    for dependency_id in dependency_ids:
        predecessor = objectives.get(dependency_id)
        if predecessor is None:
            return ()
        predecessor_status = str(
            getattr(getattr(predecessor, "status", None), "value", None)
            or getattr(predecessor, "status", "")
            or ""
        ).upper()
        if predecessor_status in {"FAILED", "ERROR", "CANCELLED", "SUPERSEDED"}:
            return ()
        predecessor_drafts = [
            str(resource_id)
            for resource_id in (getattr(predecessor, "related_resource_ids", ()) or ())
            if "DRAFT" in kind_by_id.get(str(resource_id), set())
        ]
        if len(predecessor_drafts) != 1:
            return ()
        result.extend(predecessor_drafts)
    unique = tuple(dict.fromkeys(result))
    return unique if len(unique) == 1 else ()


def _normalize_arguments(
    action: str,
    args: dict[str, Any],
    command: Command | None,
    *,
    objective: Any | None = None,
) -> dict[str, Any]:
    """Map model-chosen argument keys to the canonical tool schema.

    The decision model sometimes emits a semantic intent in generic keys
    (``topic``/``description``) instead of the tool's schema names.  This
    deterministic normalization bridges that without hardcoding user text:
    it only renames/falls-back fields for a known capability's required inputs.
    """
    normalized = (action or "").upper()
    # ``objective_id`` is an internal ownership hint used to choose the
    # canonical Objective; it is never part of any MCP tool payload.
    args = dict(args)
    args.pop("objective_id", None)
    if normalized == "SEARCH_POSTS":
        result = dict(args)
        # The semantic decision contract historically used generic pagination
        # names.  The canonical MCP search schema exposes ``size``; normalize
        # the aliases before the authoritative tool-schema validation rather
        # than widening that schema with a second public contract.
        if not result.get("size"):
            alias_size = result.get("limit", result.get("page_size"))
            if alias_size is not None and alias_size != "":
                result["size"] = alias_size
        result.pop("limit", None)
        result.pop("page_size", None)
        # Keep the semantic alias boundary closed: the MCP search contract
        # intentionally exposes only these four fields.
        return {key: value for key, value in result.items() if key in {"query", "sort", "page", "size"}}
    if normalized == "ANSWER_FROM_KNOWLEDGE":
        result = dict(args)
        question = (
            result.get("question")
            or result.get("query")
            or result.get("search_query")
            or result.get("topic")
        )
        if question not in (None, ""):
            result["question"] = question
        return {
            key: value
            for key, value in result.items()
            if key in {"question", "top_posts", "top_chunks"}
            and value not in (None, "")
        }
    if normalized in {"CREATE_DRAFT", "GENERATE_CONTENT"}:
        result = dict(args)
        if not result.get("title") and result.get("topic"):
            result["title"] = result["topic"]
        if not result.get("instruction") and result.get("description"):
            result["instruction"] = result["description"]
        # General, Objective-driven argument-repair: when the model returned a
        # content action without its required args, fill title/instruction from
        # the CURRENT Objective's own description/intent (the content authority
        # for this Objective).  Never borrow the aggregate command goal for a
        # multi-objective task or another Objective's draft.
        if not result.get("title") and objective is not None:
            result["title"] = str(
                getattr(objective, "description", "")
                or getattr(objective, "intent", "")
                or ""
            )
        if not result.get("instruction") and objective is not None:
            result["instruction"] = str(
                getattr(objective, "description", "")
                or getattr(objective, "intent", "")
                or ""
            )
        # Legacy single-objective callers may not have an Objective projection;
        # retain the command fallback only after the objective-scoped repair.
        if not result.get("title") and command is not None:
            result["title"] = str(getattr(command, "requested_goal", "") or "")
        if not result.get("instruction") and command is not None:
            result["instruction"] = str(getattr(command, "requested_goal", "") or "")
        return result
    if normalized == "UPDATE_DRAFT":
        # The semantic mutation model may call the replacement body ``body``
        # (or ``body_markdown``), while the capability contract crossing the
        # durable/MCP boundary is ``content``.  Normalize aliases here and
        # remove them before schema binding so a multi-target update cannot
        # fail only for the item that edits the body.
        result = dict(args)
        if not result.get("content"):
            for alias in ("body", "body_markdown"):
                if result.get(alias):
                    result["content"] = result[alias]
                    break
        result.pop("body", None)
        result.pop("body_markdown", None)
        return result
    if normalized in {"CREATE_SCHEDULE", "SCHEDULE_PUBLISH", "UPDATE_SCHEDULE"}:
        result = dict(args)
        # The decision model may emit publish_at; the schedule tool uses run_at.
        if not result.get("run_at") and result.get("publish_at"):
            result["run_at"] = result["publish_at"]
        if not result.get("run_at") and result.get("scheduled_at"):
            result["run_at"] = result["scheduled_at"]
        # The schedule tool targets a draft; the loop carries it as resource_id.
        if not result.get("draft_id") and result.get("resource_id"):
            result["draft_id"] = result["resource_id"]
        # Single time authority: the current Objective's canonical run_at (from
        # TemporalResolver) OVERRIDES the model's guess.  The model never decides
        # the final instant.  Only apply when the Objective carries a canonical
        # run_at; otherwise the model is not trusted to invent a time.
        canonical = _objective_run_at(objective)
        if canonical:
            result["run_at"] = canonical
        return result
    return dict(args)


def _objective_run_at(objective: Any) -> str | None:
    """Read the Objective's canonical absolute run_at (the time authority)."""
    if objective is None:
        return None
    constraints = getattr(objective, "constraints", None)
    if isinstance(constraints, Mapping) or constraints:
        value = constraints.get("run_at")
        if value:
            return str(value)
    return None


def _canonical_time_equal(left: str, right: str) -> bool:
    """Compare two already-resolved instants without changing time ownership."""
    from datetime import datetime

    try:
        def parse(value: str) -> datetime:
            normalized = str(value or "").replace("Z", "+00:00")
            return datetime.fromisoformat(normalized)

        return parse(left).astimezone(UTC) == parse(right).astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return str(left or "") == str(right or "")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _budget_failure(result: ActionLoopResult, code: str, message: str) -> ActionLoopResult:
    result.status = "FAILED"
    result.error_code = code
    result.error_message = message
    result.success = False
    return result


def _no_progress_failure(
    result: ActionLoopResult,
    task: Any,
    plan: TaskPlan | None,
    iteration: int,
    action: str,
) -> ActionLoopResult:
    """Fail fast with the concrete repeated state, never a generic budget error."""
    current = ActionLoop._current_objective(task)
    step = next(
        (item for item in (getattr(plan, "steps", ()) or ())
         if str(getattr(item, "status", "")).upper() not in {"COMPLETED", "FAILED", "CANCELLED"}),
        None,
    )
    result.status = "FAILED"
    result.success = False
    result.error_code = "ACTION_LOOP_NO_PROGRESS"
    result.error_message = (
        f"NO_PROGRESS at iteration={iteration} objective="
        f"{getattr(current, 'objective_id', '')} step={getattr(step, 'step_id', '')} action={action}"
    )
    return result


def _status_str(value: Any) -> str:
    normalized = getattr(value, "value", value)
    return str(normalized or "").upper()


def _result_ok(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(value.get("ok", True))
    return bool(getattr(value, "success", True))


def _result_status(value: Any) -> str:
    return str(value.get("status") if isinstance(value, Mapping) else getattr(value, "status", "")) or ""

def _result_execution_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return value.get("execution_id")
    return getattr(value, "execution_id", None)

def _result_resource_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return value.get("resource_id") or value.get("draft_id") or value.get("schedule_id")
    return getattr(value, "resource_id", None) or getattr(value, "draft_id", None)

def _result_message(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("message") or value.get("user_message") or value.get("content") or "")
    return str(getattr(value, "message", "") or getattr(value, "content", "") or "")


def _result_error_code(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("error_code") or value.get("code") or "")
    return str(getattr(value, "error_code", "") or getattr(value, "code", "") or "")

def _dict_get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


__all__ = ["ActionLoop", "ActionLoopError"]
