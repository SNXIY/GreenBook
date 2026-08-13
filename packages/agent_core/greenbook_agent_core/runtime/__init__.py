"""Composition and dependency boundaries for the Agent Runtime."""

# Keep this package initializer import-free. RuntimeContainer depends on
# ArtifactRegistry, while artifact/execution modules import runtime.container.
# Eager re-exporting here would create a package-initialization cycle.

__all__: list[str] = []
