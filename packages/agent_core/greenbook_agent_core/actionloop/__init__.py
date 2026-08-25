"""ActionLoop — the Phase 3B reasoning loop for complex Tasks.

One loop drives a Task from its objectives to verified completion by deciding
the next *semantic action* each iteration, executing it through the durable
Runtime (never owning Queue/Lease/Retry/Checkpoint), observing the verified
result, and continuing.  No fixed Search->Summarize->Create->Schedule workflow
is encoded: the next step is chosen from the Task objective, current artifacts,
resources, tool results, and unfinished objectives.
"""

from __future__ import annotations

from .loop import ActionLoop, ActionLoopError
from .qualification import ActionGuardResult, guard_action
from .models import (
    ActionDecision,
    ActionDecisionType,
    ActionLoopResult,
    ActionObservation,
    ActionStepPlan,
)

__all__ = [
    "ActionDecision",
    "ActionDecisionType",
    "ActionLoop",
    "ActionLoopError",
    "ActionLoopResult",
    "ActionObservation",
    "ActionStepPlan",
    "ActionGuardResult",
    "guard_action",
]
