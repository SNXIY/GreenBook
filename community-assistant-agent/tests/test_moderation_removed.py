"""Static constraints: Moderation Agent is out of community-assistant scope."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.tools import tool_registry

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


def test_tool_registry_has_no_moderation_check_draft() -> None:
    assert "moderation.check_draft" not in tool_registry.names()
    with pytest.raises(ValueError, match="moderation.check_draft"):
        tool_registry.get("moderation.check_draft")


def test_production_code_does_not_reference_moderation_check_draft() -> None:
    hits: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "moderation.check_draft" in text:
            hits.append(str(path.relative_to(APP_ROOT.parent)))
    assert hits == [], f"unexpected moderation.check_draft refs: {hits}"


def test_production_code_does_not_define_or_construct_moderation_client() -> None:
    hits: list[str] = []
    for path in APP_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "ModerationClient" in text or "moderation_base_url" in text:
            hits.append(str(path.relative_to(APP_ROOT.parent)))
    assert hits == [], f"unexpected ModerationClient wiring: {hits}"


def test_runtime_init_does_not_construct_moderation_client() -> None:
    source = (APP_ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_def = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Runtime"
    )
    init = next(
        node
        for node in class_def.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    for node in ast.walk(init):
        if isinstance(node, ast.Attribute) and node.attr == "moderation":
            raise AssertionError("Runtime still wires self.moderation")
        if isinstance(node, ast.Name) and node.id == "ModerationClient":
            raise AssertionError("Runtime still constructs ModerationClient")


def test_settings_has_no_moderation_fields() -> None:
    from app.config import Settings

    fields = set(Settings.model_fields)
    assert "moderation_base_url" not in fields
    assert "moderation_auth_secret" not in fields
