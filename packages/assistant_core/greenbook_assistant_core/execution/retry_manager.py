"""Explicit step retry and evidence-aware recovery orchestration."""

from __future__ import annotations

from .events import EventType, ExecutionEvent
from greenbook_assistant_core.observability.metrics import MetricsCollector
from .models import StepExecution
from .recovery import RecoveryPolicy
from .retry_decision import (
    FailureEvidenceSnapshot,
    RetryDecision,
    RetryDecisionEngine,
    RetryEvidenceResolver,
)
from .runtime_manager import RuntimeManager
from .state_manager import ExecutionStateManager


class RetryManager:
    """Prepare failed steps for a later Worker.run() pass.

    ``RetryDecisionEngine`` is the only authorization gate.  This class still
    performs the legacy state transition and checkpoint bookkeeping, but it
    does not execute a tool itself.
    """

    def __init__(
        self,
        state_manager: ExecutionStateManager | None = None,
        policy: RecoveryPolicy | None = None,
        runtime_manager: RuntimeManager | None = None,
        decision_engine: RetryDecisionEngine | None = None,
        metrics_collector: MetricsCollector | None = None,
    ) -> None:
        self._state = state_manager or ExecutionStateManager()
        # Retained for source compatibility. Retry authorization is now owned
        # by RetryDecisionEngine, not the legacy error-code policy.
        self._policy = policy or RecoveryPolicy()
        self._runtime = runtime_manager or RuntimeManager(self._state)
        self._decision_engine = decision_engine or RetryDecisionEngine()
        self._evidence = RetryEvidenceResolver(self._state.event_store)
        self._metrics = metrics_collector

    def retry_step(
        self,
        execution_id: str,
        step_id: str,
        *,
        source: str = "retry_api",
        user_requested_retry: bool = True,
    ) -> StepExecution:
        """Authorize and reset one failed Step for a later Worker pass."""

        step = self._find_step(execution_id, step_id)
        snapshot = self._evidence.resolve(execution_id, step)
        decision = self._decision_for_snapshot(
            snapshot,
            step,
            source=source,
            user_requested_retry=user_requested_retry,
        )
        self._emit(
            execution_id,
            EventType.STEP_RETRY_REQUESTED,
            step,
            {
                "retry_count": step.retry_count,
                "reason": step.error_code,
                "retry_decision": decision.model_dump(mode="json"),
            },
        )

        if not decision.allowed:
            self._emit(
                execution_id,
                EventType.STEP_RETRY_DENIED,
                step,
                {"retry_decision": decision.model_dump(mode="json")},
            )
            return step

        result = self._state.retry_step(execution_id, step.step_execution_id)
        self._runtime.save_checkpoint(execution_id)
        self._emit(
            execution_id,
            EventType.STEP_RETRY_STARTED,
            result,
            {
                "retry_count": result.retry_count,
                "reason": step.error_code,
                "retry_decision": decision.model_dump(mode="json"),
            },
        )
        if self._metrics is not None:
            context = self._state.trace_context(execution_id)
            if context is not None:
                context = context.for_step(step.step_id)
            self._metrics.record_retry(context=context)
        return result

    def decision_for_step(
        self,
        execution_id: str,
        step_id: str,
        *,
        source: str = "retry",
        user_requested_retry: bool = False,
    ) -> RetryDecision:
        """Return the decision without changing Step state."""

        step = self._find_step(execution_id, step_id)
        snapshot = self._evidence.resolve(execution_id, step)
        return self._decision_for_snapshot(
            snapshot,
            step,
            source=source,
            user_requested_retry=user_requested_retry,
        )

    def resume_execution(self, execution_id: str):
        """Resume only steps approved by the common retry decision gate."""

        execution = self._state.get_execution(execution_id)
        approved_retry_ids: set[str] = set()
        approved_running_ids: set[str] = set()
        for step in execution.steps:
            if step.status.value == "FAILED_RETRYABLE":
                decision = self.decision_for_step(
                    execution_id,
                    step.step_id,
                    source="resume",
                )
                if decision.allowed:
                    approved_retry_ids.add(step.step_execution_id)
            elif step.status.value == "RUNNING":
                decision = self.decision_for_step(
                    execution_id,
                    step.step_id,
                    source="resume_crash_recovery",
                )
                if decision.allowed:
                    approved_running_ids.add(step.step_execution_id)
        return self._state.resume_execution(
            execution_id,
            retryable_step_ids=approved_retry_ids,
            running_step_ids=approved_running_ids,
        )

    def _decision_for_snapshot(
        self,
        snapshot: FailureEvidenceSnapshot,
        step: StepExecution,
        *,
        source: str,
        user_requested_retry: bool,
    ) -> RetryDecision:
        return self._decision_engine.decide_for_step(
            snapshot.failure,
            step,
            evidence=snapshot.evidence,
            source=source,
            user_requested_retry=user_requested_retry,
        )

    def get_checkpoint(self, execution_id: str):
        return self._runtime.restore_checkpoint(execution_id)

    def _find_step(self, execution_id: str, step_id: str) -> StepExecution:
        for step in self._state.list_steps(execution_id):
            if step.step_id == step_id or step.step_execution_id == step_id:
                return step
        raise ValueError(f"Step {step_id} not found in execution {execution_id}")

    def _emit(
        self,
        execution_id: str,
        event_type: EventType,
        step: StepExecution,
        payload: dict[str, object],
    ) -> None:
        self._state.event_store.append(
            ExecutionEvent(
                execution_id=execution_id,
                event_type=event_type,
                step_id=step.step_id,
                payload={"step_execution_id": step.step_execution_id, **payload},
            )
        )


__all__ = ["RetryManager"]
