"""Objective-driven CREATE_DRAFT argument-repair (G1-G4).

A new Business Objective needing GENERATE_CONTENT must yield a legal
CREATE_DRAFT(title, instruction) from its OWN description/topic/constraints,
never from a sibling Objective's draft/title/content.  If the model returns a
content action with empty arguments, the repair fills the required args from
the current Objective; if the Objective still cannot supply them, the write
must fail closed (controlled validation failure) without a side effect.
"""

from __future__ import annotations

from typing import Any

from greenbook_agent_core.actionloop.loop import _normalize_arguments
from greenbook_agent_core.task.models import Objective


def _objective(objective_id: str, description: str) -> Objective:
    return Objective(
        task_id="t1",
        objective_id=objective_id,
        description=description,
        intent=description,
    )


def test_g1_model_provides_title_content_passes_through() -> None:
    """Objective A returns valid title/content -> CREATE_DRAFT unchanged."""
    obj_a = _objective("A", "Java 集合")
    args = _normalize_arguments(
        "CREATE_DRAFT",
        {"title": "Java 集合实战", "instruction": "写一篇 Java 集合调试帖"},
        None,
        objective=obj_a,
    )
    assert args["title"] == "Java 集合实战"
    assert args["instruction"] == "写一篇 Java 集合调试帖"


def test_g2_empty_args_repaired_from_objective() -> None:
    """Model returns CREATE_DRAFT arguments={} -> repaired to B's own topic."""
    obj_b = _objective("B", "JVM")
    args = _normalize_arguments("CREATE_DRAFT", {}, None, objective=obj_b)
    assert args["title"] == "JVM"
    assert args["instruction"] == "JVM"


def test_g2b_objective_repair_precedes_aggregate_command_goal() -> None:
    """A sibling-free objective must win over a multi-objective command goal."""
    obj_b = _objective("B", "Agent 运维")
    command = type(
        "_C",
        (),
        {"requested_goal": "Java、Agent、Redis 三篇文章的混合安排"},
    )()

    args = _normalize_arguments("CREATE_DRAFT", {}, command, objective=obj_b)

    assert args["title"] == "Agent 运维"
    assert args["instruction"] == "Agent 运维"


def test_g3_repair_uses_only_current_objective() -> None:
    """B's repair must come from B's description, never A's draft/content."""
    obj_a = _objective("A", "Java 集合")
    obj_b = _objective("B", "JVM")
    # Simulate A already owning a draft (its content must never leak into B).
    args_a = _normalize_arguments(
        "CREATE_DRAFT",
        {"title": "Java 集合实战", "instruction": "A 的内容"},
        None,
        objective=obj_a,
    )
    assert args_a["title"] == "Java 集合实战"
    # B repaired independently from B's own description.
    args_b = _normalize_arguments("CREATE_DRAFT", {}, None, objective=obj_b)
    assert args_b["title"] == "JVM"
    assert args_b["instruction"] == "JVM"
    assert "Java 集合" not in args_b["title"]
    assert "A 的内容" not in args_b["instruction"]


def test_g4_repair_cannot_fill_returns_empty() -> None:
    """Objective with no usable topic -> repair leaves required args missing."""
    obj_empty = _objective("B", "")
    args = _normalize_arguments("CREATE_DRAFT", {}, None, objective=obj_empty)
    # Required args are still missing: the CREATE_DRAFT tool must reject this
    # (controlled validation failure) and never create a draft.
    assert args.get("title") in (None, "")
    assert args.get("instruction") in (None, "")


def test_g4_command_fallback_still_applies_when_no_objective() -> None:
    """Legacy path: command requested_goal fills title when no objective."""
    command = type(
        "_C",
        (),
        {"requested_goal": "写一篇调试短帖"},
    )()
    args = _normalize_arguments("CREATE_DRAFT", {}, command, objective=None)
    assert args["title"] == "写一篇调试短帖"
