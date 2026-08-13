"""Typed planning contracts and validation.

Dynamic planning is exported lazily so importing the legacy execution
validation models cannot pull GoalCompiler and the compatibility graph into a
partially initialized conversation module.
"""

from __future__ import annotations

from typing import Any

__all__ = ["DynamicPlanner", "PlanningDecision", "PlanningDecisionType"]


def __getattr__(name: str) -> Any:
    if name == "DynamicPlanner":
        from .dynamic import DynamicPlanner

        return DynamicPlanner
    if name in {"PlanningDecision", "PlanningDecisionType"}:
        from .contracts import PlanningDecision, PlanningDecisionType

        return {
            "PlanningDecision": PlanningDecision,
            "PlanningDecisionType": PlanningDecisionType,
        }[name]
    raise AttributeError(name)
