"""Runtime dispatch adapter shared by API-managed and standalone workers."""

from __future__ import annotations

import inspect
import os
from dataclasses import replace
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from greenbook_contracts.identity import AuthContext

from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.operation_tracking import (
    ExternalOperationStore,
    ExternalOperationTracker,
)
from greenbook_agent_core.execution.retry_scheduler import RetryScheduler
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.observability.metrics import MetricsCollector
from greenbook_agent_core.runtime.container import RuntimeContainer

from .operation_tracking import ExternalOperationRecord
from .evidence import ExecutionEvidence
from .execution_queue_worker import ExecutionHandlerDeferredError
from .runtime_agent_service import RuntimeAgentService

_TEST_RESULT_UNKNOWN_USED: set[str] = set()


def _inject_test_result_unknown(message: ExecutionQueueMessage, result: Any) -> Any:
    """Inject one post-write acknowledgement loss in test/development only."""
    if os.getenv("GREENBOOK_ENV", "").strip().lower() not in {"test", "development"}:
        return result
    if os.getenv("GREENBOOK_ALLOW_TEST_FAULTS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return result
    requested = os.getenv("GREENBOOK_TEST_RESULT_UNKNOWN_ONCE", "").strip()
    if not requested or requested not in {"*", message.execution_id}:
        return result
    if message.execution_id in _TEST_RESULT_UNKNOWN_USED:
        return result
    _TEST_RESULT_UNKNOWN_USED.add(message.execution_id)
    if hasattr(result, "status"):
        return replace(
            result,
            success=False,
            status="RESULT_UNKNOWN",
            error_code="RESULT_UNKNOWN",
            error_message="Test-only acknowledgement interruption; reconcile required",
            retryable=False,
        )
    return result


def _completion_evidence(
    result: Any,
    claimed: ExternalOperationRecord,
    execution_id: str,
) -> ExecutionEvidence | None:
    """Project the canonical tool result into the external ledger evidence.

    ``RuntimeResult.activity_records`` is the lossless boundary emitted by the
    Runtime for each tool attempt.  The queue handler owns the external
    operation claim, so it must carry the matching step's observed evidence
    into ``OperationLedger.complete`` instead of completing with an empty
    receipt.
    """
    records = getattr(result, "activity_records", None) or []
    matching: list[Mapping[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        step_id = str(record.get("step_id") or "")
        if step_id == str(claimed.step_id or ""):
            matching.append(record)
    # A direct single-step runtime result may omit the step id in its public
    # activity projection.  Only use that unambiguous case; never borrow a
    # resource from another step in a multi-step execution.
    if not matching and len(records) == 1 and isinstance(records[0], Mapping):
        matching = [records[0]]

    for record in matching:
        raw = record.get("result")
        if not isinstance(raw, Mapping):
            raw = record
        refs = raw.get("resource_refs") or record.get("resource_refs")
        if not isinstance(refs, list) or not refs:
            continue
        payload = dict(raw)
        payload["resource_refs"] = list(refs)
        payload["execution_id"] = execution_id
        payload["step_id"] = str(claimed.step_id or payload.get("step_id") or "")
        payload["operation_id"] = str(claimed.operation_id or "")
        return ExecutionEvidence.from_payload(payload, request_sent=True)
    return None


def _observe_operation_claim(claimed: ExternalOperationRecord) -> None:
    try:
        from greenbook_agent_core.observability.bus import observability

        ob = observability()
        ob.operation().inc(semantic_action=claimed.semantic_action or "WRITE", outcome="CLAIMED")
        ob.record_trace(
            "operation_claimed",
            trace_id=claimed.trace_id,
            conversation_id=claimed.conversation_id,
            operation_id=claimed.operation_id,
            semantic_action=claimed.semantic_action,
            status="RUNNING",
        )
    except Exception:  # noqa: BLE001 - observability must never break execution
        pass


def _observe_operation_complete(claimed: ExternalOperationRecord, outcome: str) -> None:
    try:
        from greenbook_agent_core.observability.bus import observability

        ob = observability()
        ob.operation().inc(semantic_action=claimed.semantic_action or "WRITE", outcome=outcome)
        if outcome == "RESULT_UNKNOWN":
            ob.result_unknown().inc()
        ob.record_trace(
            "operation_" + ("completed" if outcome == "SUCCEEDED" else "result_unknown" if outcome == "RESULT_UNKNOWN" else "failed"),
            trace_id=claimed.trace_id,
            conversation_id=claimed.conversation_id,
            operation_id=claimed.operation_id,
            semantic_action=claimed.semantic_action,
            status=outcome,
        )
    except Exception:  # noqa: BLE001
        pass

CredentialResolver = Callable[[ExecutionQueueMessage], AuthContext]
CompletionPublisher = Callable[
    [ExecutionQueueMessage, Any, AuthContext],
    Awaitable[None] | None,
]


class RuntimeExecutionQueueHandler:
    """Execute one durable queue message through ``RuntimeAgentService``.

    A standalone deployment may supply a service token.  The local combined
    API process instead supplies a credential resolver backed only by tokens
    previously validated by the API middleware.
    """

    def __init__(
        self,
        *,
        mcp: Any,
        service: RuntimeAgentService | None = None,
        repository: Any = None,
        event_store: Any = None,
        checkpoint_store: Any = None,
        external_operation_store: Any = None,
        worker_access_token: str = "",
        credential_resolver: CredentialResolver | None = None,
        completion_publisher: CompletionPublisher | None = None,
        llm: Any = None,
        model: str = "",
        metrics_collector: MetricsCollector | None = None,
        retry_scheduler: RetryScheduler | None = None,
        container: RuntimeContainer | None = None,
        memory_manager: Any | None = None,
        observation_writer: Any | None = None,
        user_activity_publisher: Any | None = None,
        operation_ledger: Any | None = None,
        worker_id: str = "",
        run_store: Any | Callable[[], Any] | None = None,
    ) -> None:
        if credential_resolver is None and not worker_access_token:
            raise RuntimeError(
                "Queued Runtime execution requires a credential resolver or "
                "GREENBOOK_AGENT_WORKER_ACCESS_TOKEN"
            )
        self._mcp = mcp
        self._worker_access_token = worker_access_token
        self._credential_resolver = credential_resolver
        self._completion_publisher = completion_publisher
        self._observation_writer = observation_writer
        self._user_activity_publisher = user_activity_publisher
        self._llm = llm
        self._model = model
        self._operation_ledger = operation_ledger
        self._worker_id = worker_id or "runtime-queue-worker"
        self._run_store = run_store
        self._service = service or RuntimeAgentService(
            container=container,
            repository=repository,
            event_store=event_store,
            checkpoint_store=checkpoint_store,
            artifact_store=(container.artifact_store if container is not None else None),
            # OperationLedger is the durable operation owner; the tracker is an
            # in-process audit/cache only and must not create a second durable
            # external_operation record.
            operation_tracker=ExternalOperationTracker(
                store=ExternalOperationStore(),
            ),
            metrics_collector=metrics_collector,
            retry_scheduler=retry_scheduler,
            memory_manager=memory_manager,
        )

    async def __call__(self, message: ExecutionQueueMessage) -> None:
        # A queue message can outlive the Agent Run that admitted it (for
        # example after a result-projection crash).  Do not keep redelivering
        # a message whose owning Run is already terminal: project the same
        # truth onto the orphaned Execution once, then let the queue ACK it.
        stale_result = self._terminal_run_result(message)
        if stale_result is not None:
            # This path must not depend on a process-local bearer-token
            # broker: the terminal Run is precisely the recovery evidence
            # needed after an API restart.
            self._fail_orphaned_operation(message)
            self._fail_orphaned_execution(message.execution_id)
            await self._publish_result(
                message,
                stale_result,
                self._service_auth(message),
            )
            return

        # Cancellation is an Execution-level terminal state too.  The Agent
        # Run may still be RUNNING while a user cancels the durable execution;
        # allowing that queue message into RuntimeAgentService then reopens a
        # retryable step and can create an unbounded claim/retry loop.  Replay
        # the terminal execution result and let the queue worker ACK the
        # message without invoking MCP or Java.
        terminal_execution = self._terminal_execution_result(message)
        if terminal_execution is not None:
            if terminal_execution.status in {"FAILED", "CANCELLED"}:
                self._fail_orphaned_operation(message)
            await self._publish_result(
                message,
                terminal_execution,
                self._service_auth(message),
            )
            return

        auth = (
            self._credential_resolver(message)
            if self._credential_resolver is not None
            else self._service_auth(message)
        )

        def emit_user_activity(event_type: str, payload: dict[str, Any]) -> None:
            if self._user_activity_publisher is None:
                return
            self._user_activity_publisher.publish_runtime_event(
                event_type,
                payload,
                conversation_id=str(message.payload.get("conversation_id") or ""),
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
                run_id=str(message.payload.get("run_id") or "") or None,
            )

        # Phase 4B.1: claim the durable operation before executing.  A stale or
        # duplicate worker cannot claim, so it cannot start a second side effect.
        ledger = self._operation_ledger
        claimed = None
        # Approval-gated writes must not consume their operation claim during
        # the pre-approval dispatch.  That pass intentionally returns
        # WAITING_APPROVAL; claiming it and marking the result FAILED would
        # prevent the approved SAME execution from claiming it later.
        approval_pending = _payload_requires_approval(message) and not bool(
            (message.payload or {}).get("approval_granted", False)
        )
        if ledger is not None and not approval_pending:
            operation_meta = _payload_operation_metadata(message)
            key = operation_meta["idempotency_key"]
            if key:
                trace_id = str(message.payload.get("trace_id") or "")
                op = ledger.begin_operation(
                    idempotency_key=key,
                    conversation_id=str(message.payload.get("conversation_id") or ""),
                    task_id=str(message.payload.get("task_id") or ""),
                    execution_id=message.execution_id,
                    step_id=operation_meta["step_id"],
                    tool_name=operation_meta["tool_name"],
                    semantic_action=operation_meta["semantic_action"],
                    trace_id=trace_id,
                )
                if op.status not in {"SUCCEEDED"}:
                    claimed = ledger.claim(op.operation_id, owner=self._worker_id)
                    if claimed is None:
                        # A process can die after the durable operation claim
                        # and before the queue handler reaches Java.  The
                        # normal PENDING claim then returns None on restart,
                        # but an expired RUNNING claim is safe to reclaim when
                        # no side effect was started.  Do not ACK that queue
                        # message while leaving its Execution RUNNING.
                        if str(getattr(op.status, "value", op.status)) == "RUNNING":
                            reclaim = getattr(
                                ledger,
                                "claim_after_lease_expiry",
                                None,
                            )
                            if callable(reclaim):
                                claimed = reclaim(
                                    op.operation_id,
                                    owner=self._worker_id,
                                )
                        if claimed is None:
                            raise ExecutionHandlerDeferredError(
                                "External operation claim is still owned or awaiting recovery"
                            )
                    _observe_operation_claim(claimed)
                else:
                    # A bare ledger-only embedder has no lagging projection to
                    # repair; preserve its duplicate-delivery short circuit.
                    # The production queue handler supplies completion or
                    # observation projection collaborators, so it continues
                    # through the terminal Execution replay below.
                    if (
                        self._completion_publisher is None
                        and self._observation_writer is None
                        and self._user_activity_publisher is None
                    ):
                        return
                    # The external mutation is already durably verified.  Do
                    # not short-circuit the queue handler: a process may have
                    # died after this Ledger commit but before completion or
                    # ActionObservation projection.  Replaying the existing
                    # terminal Execution lets those idempotent projections
                    # catch up; it must not create a new Operation claim or
                    # invoke a second physical write.
                    _observe_operation_complete(op, "SUCCEEDED_REPLAY")

        result = await self._service.execute_queued(
            message,
            mcp=self._mcp,
            llm=self._llm,
            model=self._model,
            auth=auth,
            activity_callback=emit_user_activity,
        )
        result = _inject_test_result_unknown(message, result)
        if ledger is not None and claimed is not None:
            from .operation_tracking import OperationStatus

            outcome_status = str(result.status or "")
            if outcome_status in {"COMPLETED", "SUCCESS"}:
                ledger.complete(
                    claimed,
                    status=OperationStatus.SUCCEEDED,
                    evidence=_completion_evidence(
                        result,
                        claimed,
                        message.execution_id,
                    ),
                )
                _observe_operation_complete(claimed, "SUCCEEDED")
            elif outcome_status in {
                "SUBMITTED", "RUNNING", "QUEUED", "RESULT_UNKNOWN", "PENDING", "UNKNOWN",
            }:
                ledger.mark_result_unknown(
                    claimed,
                    evidence=_completion_evidence(
                        result,
                        claimed,
                        message.execution_id,
                    ),
                )
                _observe_operation_complete(claimed, "RESULT_UNKNOWN")
            elif outcome_status in {"PAUSED", "WAITING_APPROVAL", "WAITING_HUMAN"}:
                # Control-flow suspension before a side effect is not a
                # business failure. Release the fenced claim so the same
                # execution can be resumed without duplicating or losing the
                # write.
                released = ledger.release_claim(claimed)
                _observe_operation_complete(
                    released or claimed,
                    "RELEASED" if released is not None else "RELEASE_FAILED",
                )
            else:
                ledger.complete(claimed, status=OperationStatus.FAILED)
                _observe_operation_complete(claimed, "FAILED")
        if result.error_code in {
            "EXECUTION_DISPATCH_INVALID",
            "EXECUTION_NOT_FOUND",
        }:
            raise RuntimeError(
                f"Queued execution {message.execution_id} could not be dispatched: "
                f"{result.error_code}"
            )
        await self._publish_result(message, result, auth)

    def _fail_orphaned_execution(self, execution_id: str) -> None:
        service_container = getattr(self._service, "container", None)
        state_manager = getattr(service_container, "execution_state_manager", None)
        fail_execution = getattr(state_manager, "fail_execution", None)
        if callable(fail_execution):
            try:
                fail_execution(
                    execution_id,
                    error_code="STALE_QUEUE_MESSAGE",
                    error_message="Queued work belongs to a terminal Run and was not submitted.",
                )
                return
            except Exception:
                # Compatibility embedders may expose a state manager backed by
                # a different repository; fall through to the repository
                # snapshot path below.
                pass
        repository = getattr(self._service, "_execution_repository", None)
        find = getattr(repository, "find_by_id", None)
        save = getattr(repository, "save", None)
        if not callable(find) or not callable(save):
            return
        execution = find(execution_id)
        if execution is None:
            return
        status = str(
            getattr(
                getattr(execution, "status", ""),
                "value",
                getattr(execution, "status", ""),
            )
        )
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return
        from greenbook_agent_core.execution.models import ExecutionStatus

        execution.status = ExecutionStatus.FAILED
        save(execution)

    def _fail_orphaned_operation(self, message: ExecutionQueueMessage) -> None:
        ledger = self._operation_ledger
        if ledger is None:
            return
        operation_meta = _payload_operation_metadata(message)
        key = operation_meta["idempotency_key"]
        if not key:
            return
        from .operation_ledger import stable_operation_id
        from .operation_tracking import OperationStatus

        operation = ledger.store.get(stable_operation_id(key))
        if operation is None or operation.status != OperationStatus.PENDING:
            return
        claimed = ledger.claim(operation.operation_id, owner=self._worker_id)
        if claimed is not None and not claimed.side_effect_started:
            ledger.complete(claimed, status=OperationStatus.FAILED)

    def _terminal_run_result(
        self,
        message: ExecutionQueueMessage,
    ) -> RuntimeResult | None:
        if self._run_store is None:
            return None
        store = self._run_store() if callable(self._run_store) else self._run_store
        if store is None:
            return None
        run_id = str((message.payload or {}).get("run_id") or "")
        if not run_id:
            return None
        getter = getattr(store, "get", None)
        run = getter(run_id) if callable(getter) else None
        status = str(getattr(run, "status", "") or "").upper()
        if status not in {"COMPLETED", "FAILED", "CANCELLED", "PARTIAL_SUCCESS"}:
            return None
        return RuntimeResult(
            success=False,
            status="FAILED",
            run_id=run_id,
            task_id=str((message.payload or {}).get("task_id") or ""),
            execution_id=message.execution_id,
            trace_id=str((message.payload or {}).get("trace_id") or message.trace_id),
            error_code="STALE_QUEUE_MESSAGE",
            error_message="Queued work belongs to a terminal Run and was not submitted.",
            retryable=False,
        )

    def _terminal_execution_result(
        self,
        message: ExecutionQueueMessage,
    ) -> RuntimeResult | None:
        """Return a replay result when the durable Execution is terminal.

        Agent Run and Runtime Execution state are persisted independently. A
        control request can therefore close the Execution while its parent
        Run is still RUNNING. Queue delivery must honor the more specific
        Execution terminal state before calling the Runtime again.
        """

        repository = getattr(self._service, "_execution_repository", None)
        finder = getattr(repository, "find_by_id", None)
        if not callable(finder):
            return None
        execution = finder(message.execution_id)
        if execution is None:
            return None
        status = str(
            getattr(
                getattr(execution, "status", ""),
                "value",
                getattr(execution, "status", ""),
            )
            or ""
        ).upper()
        if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            return None
        return RuntimeResult(
            success=status == "COMPLETED",
            status=status,
            run_id=str((message.payload or {}).get("run_id") or ""),
            task_id=str((message.payload or {}).get("task_id") or getattr(execution, "task_id", "") or ""),
            execution_id=message.execution_id,
            trace_id=str((message.payload or {}).get("trace_id") or message.trace_id),
            error_code=("EXECUTION_ALREADY_TERMINAL" if status != "COMPLETED" else ""),
            error_message=(
                "Execution was cancelled before queue delivery."
                if status == "CANCELLED"
                else "Execution is already terminal."
                if status == "FAILED"
                else ""
            ),
            retryable=False,
            started_execution=True,
        )

    async def _publish_result(
        self,
        message: ExecutionQueueMessage,
        result: Any,
        auth: AuthContext,
    ) -> None:
        if self._completion_publisher is not None:
            published = self._completion_publisher(message, result, auth)
            if inspect.isawaitable(published):
                await published
        if self._observation_writer is not None:
            # Persist the ActionObservation only after the durable completion
            # projection committed, so business resources are durable before
            # the continuation marker exists. The store is idempotent by
            # execution_id, so a repeated terminal hook cannot double-queue.
            observed = self._observation_writer(message, result, auth)
            if inspect.isawaitable(observed):
                await observed
        after_execution = getattr(self._completion_publisher, "after_execution", None)
        if after_execution is not None:
            settled = after_execution(message, result)
            if inspect.isawaitable(settled):
                await settled

    def _service_auth(self, message: ExecutionQueueMessage) -> AuthContext:
        identity = message.payload.get("auth_context") or {}
        user_id = str(identity.get("user_id") or message.payload.get("user_id") or "")
        tenant_id = str(
            identity.get("tenant_id") or message.payload.get("tenant_id") or ""
        )
        if not user_id or not tenant_id:
            raise RuntimeError(
                f"Queued execution {message.execution_id} has no authenticated scope"
            )
        return AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=[str(role) for role in (identity.get("roles") or [])],
            session_id=identity.get("session_id"),
            token_id=identity.get("token_id"),
            timezone=str(
                identity.get("timezone")
                or message.payload.get("timezone")
                or "Asia/Shanghai"
            ),
            raw_access_token=self._worker_access_token,
        )


def _payload_operation_metadata(message: ExecutionQueueMessage) -> dict[str, str]:
    payload = message.payload or {}
    execution_input = payload.get("execution_input")
    if isinstance(execution_input, dict):
        steps = execution_input.get("steps") or []
        step = steps[0] if isinstance(steps, list) and steps else {}
        task_id = str(execution_input.get("task_id") or payload.get("task_id") or "")
        capability = str(
            execution_input.get("capability")
            or step.get("capability")
            or ""
        )
        step_id = str(step.get("step_id") or "")
        conversation_id = str(
            execution_input.get("conversation_id")
            or payload.get("conversation_id")
            or ""
        )
        # Match RuntimeAgentService._step_key so queue delivery claims the
        # operation created at canonical submission instead of creating a
        # second record from ExecutionInput.idempotency_key.
        stable_key = ":".join((conversation_id, task_id, capability, step_id))
        return {
            "idempotency_key": stable_key,
            "step_id": step_id,
            "tool_name": str(step.get("tool_name") or ""),
            "semantic_action": str(
                step.get("semantic_action") or capability or ""
            ),
        }
    return {
        "idempotency_key": "",
        "step_id": "",
        "tool_name": "",
        "semantic_action": "",
    }


def _payload_requires_approval(message: ExecutionQueueMessage) -> bool:
    payload = message.payload or {}
    execution_input = payload.get("execution_input")
    if not isinstance(execution_input, dict):
        return False
    steps = execution_input.get("steps") or []
    for step in steps if isinstance(steps, list) else []:
        snapshot = step.get("policy_snapshot") or {}
        if bool(
            snapshot.get("requires_approval")
            or (snapshot.get("policy") or {}).get("requires_approval")
        ):
            return True
    return False


__all__ = [
    "CompletionPublisher",
    "CredentialResolver",
    "RuntimeExecutionQueueHandler",
]
