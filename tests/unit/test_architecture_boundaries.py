"""Small import-boundary checks for the consolidated monorepo."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> list[str]:
    values: list[str] = []
    for source in path.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                values.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                values.append(node.module)
    return values


def test_consolidated_package_names_have_no_physical_compatibility_copy() -> None:
    assert not (ROOT / "packages" / "assistant_core").exists()
    assert not (ROOT / "apps" / "assistant_api").exists()
    assert not (ROOT / "apps" / "assistant_worker").exists()
    assert not (ROOT / "packages" / "agent_core" / "greenbook_agent_core" / "context" / "manager.py").exists()
    assert not (ROOT / "packages" / "agent_core" / "greenbook_agent_core" / "task" / "graph_models.py").exists()


def test_execution_and_toolruntime_do_not_depend_on_api_or_intelligence_entrypoints() -> None:
    forbidden = (
        "greenbook_agent_core.command",
        "greenbook_agent_core.goal.decomposer",
        "greenbook_agent_core.agent.loop",
        "greenbook_agent_api.api",
        "greenbook_agent_api.services",
    )
    for package in (
        ROOT / "packages" / "agent_core" / "greenbook_agent_core" / "execution",
        ROOT / "packages" / "agent_core" / "greenbook_agent_core" / "toolruntime",
    ):
        assert not any(
            imported == prefix or imported.startswith(prefix + ".")
            for imported in _imports(package)
            for prefix in forbidden
        ), package


def test_goal_and_external_clients_do_not_cross_runtime_implementation_boundaries() -> None:
    goal_imports = _imports(ROOT / "packages" / "agent_core" / "greenbook_agent_core" / "goal")
    assert not any("ExecutionWorker" in imported for imported in goal_imports)

    java_imports = _imports(ROOT / "packages" / "java_client" / "greenbook_java_client")
    assert not any(imported.startswith("greenbook_agent_core") for imported in java_imports)


def test_worker_and_core_do_not_depend_on_api_implementation() -> None:
    forbidden = ("greenbook_agent_api", "apps.agent_api")
    worker_imports = _imports(ROOT / "apps" / "agent_worker")
    core_imports = _imports(ROOT / "packages" / "agent_core" / "greenbook_agent_core")
    assert not any(
        imported == prefix or imported.startswith(prefix + ".")
        for imported in worker_imports + core_imports
        for prefix in forbidden
    )
