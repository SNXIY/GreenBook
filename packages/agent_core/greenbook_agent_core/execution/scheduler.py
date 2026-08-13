"""StepScheduler — determines which steps are ready to execute.

Phase 4.1: schedule only — no execution.
"""

from __future__ import annotations

from .models import PlanExecution, StepExecution, StepStatus


class StepScheduler:
    """Determine execution order from a DAG of StepExecutions."""

    # ── readiness ────────────────────────────────────────────────

    def get_ready_steps(self, execution: PlanExecution) -> list[StepExecution]:
        """Return steps whose dependencies are all satisfied and status is PENDING.

        A step is *ready* when:
        - status == PENDING, AND
        - every step_id in depends_on has status == COMPLETED.
        """
        completed_ids = {
            s.step_id
            for s in execution.steps
            if s.status == StepStatus.COMPLETED
        }
        ready: list[StepExecution] = []
        for step in execution.steps:
            if step.status != StepStatus.PENDING:
                continue
            if self._dependencies_satisfied(step, completed_ids):
                ready.append(step)
        return ready

    @staticmethod
    def _dependencies_satisfied(
        step: StepExecution,
        completed_ids: set[str],
    ) -> bool:
        if not step.depends_on:
            return True
        return all(dep in completed_ids for dep in step.depends_on)

    # ── progress check ───────────────────────────────────────────

    def is_stalled(self, execution: PlanExecution) -> bool:
        """True when no step can make progress.

        Stalled = no PENDING steps can become READY and no step is
        currently RUNNING or WAITING_APPROVAL.
        """
        has_running = any(
            s.status in (StepStatus.RUNNING, StepStatus.WAITING_APPROVAL)
            for s in execution.steps
        )
        if has_running:
            return False
        return len(self.get_ready_steps(execution)) == 0

    def has_pending_or_retryable(self, execution: PlanExecution) -> bool:
        """True when there are steps that could still execute."""
        return any(
            s.status in (StepStatus.PENDING, StepStatus.FAILED_RETRYABLE)
            for s in execution.steps
        )

    # ── dependency helpers ───────────────────────────────────────

    def get_blocked_steps(self, execution: PlanExecution) -> list[StepExecution]:
        """Return PENDING steps whose dependencies are NOT satisfied."""
        completed_ids = {
            s.step_id
            for s in execution.steps
            if s.status == StepStatus.COMPLETED
        }
        blocked: list[StepExecution] = []
        for step in execution.steps:
            if step.status != StepStatus.PENDING:
                continue
            if step.depends_on and not self._dependencies_satisfied(step, completed_ids):
                blocked.append(step)
        return blocked

    def mark_skipped_downstream(
        self, execution: PlanExecution, failed_step_id: str,
    ) -> list[StepExecution]:
        """Mark all downstream steps of *failed_step_id* as SKIPPED.

        Returns the list of steps that were skipped.
        """
        skipped: list[StepExecution] = []
        # Find all transitive dependents
        dependents = self._transitive_dependents(execution, failed_step_id)
        for step in execution.steps:
            if step.step_id in dependents and step.status == StepStatus.PENDING:
                step.status = StepStatus.SKIPPED
                skipped.append(step)
        return skipped

    @staticmethod
    def _transitive_dependents(
        execution: PlanExecution,
        root_id: str,
    ) -> set[str]:
        """Find all step_ids that transitively depend on *root_id*."""
        result: set[str] = set()
        queue = [root_id]
        while queue:
            current = queue.pop(0)
            for step in execution.steps:
                if current in step.depends_on and step.step_id not in result:
                    result.add(step.step_id)
                    queue.append(step.step_id)
        return result
