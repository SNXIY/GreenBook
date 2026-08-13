"""GroupScheduler — build execution batches from a TaskGroup DAG.

Phase 6.4: DAG-aware scheduling for parallel SubTask execution.
"""

from __future__ import annotations

from typing import Any


class ExecutionBatch:
    """A group of SubTasks that can execute in parallel."""

    def __init__(
        self, batch_id: int, sub_tasks: list[Any],
    ) -> None:
        self.batch_id = batch_id
        self.sub_tasks = sub_tasks  # list of SubTaskContext

    def __len__(self) -> int:
        return len(self.sub_tasks)

    def __repr__(self) -> str:
        indices = [s.sub_index for s in self.sub_tasks]
        return f"Batch({self.batch_id}: {indices})"


class GroupScheduler:
    """Build execution batches from a TaskGroup DAG.

    Tasks within the same batch have no dependencies on each other
    and can execute concurrently.  Batches are ordered such that
    all dependencies of a task are satisfied before its batch.
    """

    def __init__(self, max_parallel: int = 4) -> None:
        self._max_parallel = max_parallel

    # ── main entry ───────────────────────────────────────────────

    def schedule(self, group: Any) -> list[ExecutionBatch]:
        """Build ordered ExecutionBatches from a TaskGroup."""
        sub_tasks = group.sub_tasks

        # Build dependency map
        depends_on: dict[int, set[int]] = {}
        for i, st in enumerate(sub_tasks):
            deps: set[int] = set()
            if st.depends_on_task_index is not None:
                deps.add(st.depends_on_task_index)
            depends_on[i] = deps

        completed: set[int] = set()
        remaining = set(range(len(sub_tasks)))
        batches: list[ExecutionBatch] = []
        batch_id = 0

        while remaining:
            # Find ready tasks (all deps completed)
            ready = sorted([
                i for i in remaining
                if depends_on[i].issubset(completed)
            ])

            if not ready:
                # Should not happen in a valid DAG, but guard against cycles
                break

            # Cap batch size
            batch_indices = ready[:self._max_parallel]
            batch_tasks = [sub_tasks[i] for i in batch_indices]

            batches.append(ExecutionBatch(batch_id, batch_tasks))
            completed.update(batch_indices)
            remaining -= set(batch_indices)
            batch_id += 1

        return batches
