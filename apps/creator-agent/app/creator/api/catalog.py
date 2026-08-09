from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.creator.api.composition import CreatorApiRuntime
from app.creator.api.models import CreatorWorkspaceCatalogResponse
from app.creator.domain.models import CreatorTaskKind, CreatorTaskStatus
from app.creator.runtime.models import ArtifactKind


def creator_workspace_catalog(
    settings: Settings,
    runtime: CreatorApiRuntime,
) -> CreatorWorkspaceCatalogResponse:
    path = Path(settings.creator_workspace_catalog_path)
    if not path.is_absolute():
        path = settings.project_root / path
    raw = dict(_load_catalog(str(path.resolve())))
    _validate_domain_values(raw)
    model_provider = settings.ai_provider.strip().lower()
    community_provider = settings.creator_community_provider.strip().lower()
    raw.update(
        {
            "poll_interval_ms": settings.creator_workspace_poll_interval_ms,
            "backend": {
                "execution_mode": runtime.execution_mode,
                "model_provider": model_provider,
                "community_provider": community_provider,
            },
        }
    )
    return CreatorWorkspaceCatalogResponse.model_validate(raw)


@lru_cache(maxsize=8)
def _load_catalog(path: str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("Creator workspace catalog must contain a JSON object")
    return value


def _validate_domain_values(raw: dict[str, Any]) -> None:
    expected = {
        "task_kinds": {item.value for item in CreatorTaskKind},
        "task_statuses": {item.value for item in CreatorTaskStatus},
        "artifact_kinds": {item.value for item in ArtifactKind},
    }
    for key, allowed in expected.items():
        configured = {
            str(item.get("value"))
            for item in raw.get(key, ())
            if isinstance(item, dict)
        }
        unknown = configured - allowed
        missing = allowed - configured
        if unknown or missing:
            raise ValueError(
                f"Creator workspace catalog {key} mismatch; "
                f"unknown={sorted(unknown)}, missing={sorted(missing)}"
            )
