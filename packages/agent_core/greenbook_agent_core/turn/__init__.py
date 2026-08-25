"""Phase 3A unified Turn orchestration.

The Turn boundary owns: context assembly, command understanding, target and
temporal resolution, fast-path gating, and routing between a lightweight
Fast Path (single reads and explicit writes) and the existing Complex Path
(GoalDecomposer / AgentLoop, owned by ConversationRuntimeAdapter).

The coordinator is intentionally free of Queue/Worker/Lease/Retry/JavaClient
and tool-execution details; Fast Path writes still reach the same durable
execution and verification pipeline as the Complex Path.
"""

from __future__ import annotations

from .commitment_poc import (
    CommitmentDraft,
    CommitmentStatus,
    DesiredOutcome,
    FrozenCommitment,
    HITLType,
    WorkItem,
    WorkItemStatus,
)
from .context_assembler import ContextAssembler
from .fast_path_executor import FastPathExecutor
from .fast_path_gate import FastPathGate, TurnRoute
from .models import (
    AssembledTurnContext,
    FastPathDecision,
    TurnBudget,
    TurnRequest,
)

__all__ = [
    "AssembledTurnContext",
    "CommitmentDraft",
    "CommitmentStatus",
    "ContextAssembler",
    "DesiredOutcome",
    "FastPathDecision",
    "FastPathExecutor",
    "FastPathGate",
    "FrozenCommitment",
    "HITLType",
    "TurnBudget",
    "TurnRequest",
    "TurnRoute",
    "WorkItem",
    "WorkItemStatus",
]
