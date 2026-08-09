"""Creator content-angle ledger stored on long-term profile preferences."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.creator.memory.models import CreatorLongTermProfile


USED_CONTENT_ANGLES_KEY = "used_content_angles"
_MAX_ANGLES = 40


class UsedContentAngle(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    angle_key: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=512)
    angle: str = Field(default="", max_length=2_000)
    task_id: str = Field(default="", max_length=64)
    artifact_id: str = Field(default="", max_length=128)
    used_at: str = Field(default="", max_length=64)


def normalize_angle_key(*parts: str) -> str:
    joined = " ".join(part.strip().lower() for part in parts if part and part.strip())
    collapsed = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", joined)
    return collapsed.strip("-")[:160]


def extract_used_angles(profile: CreatorLongTermProfile | None) -> tuple[UsedContentAngle, ...]:
    if profile is None:
        return ()
    raw = profile.inferred_preferences.get(USED_CONTENT_ANGLES_KEY) or ()
    if not isinstance(raw, (list, tuple)):
        return ()
    angles: list[UsedContentAngle] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            angles.append(UsedContentAngle.model_validate(item))
        except Exception:
            continue
    return tuple(angles)


def angles_conflict(candidate_key: str, used: tuple[UsedContentAngle, ...]) -> bool:
    if not candidate_key:
        return False
    candidate_tokens = _tokens(candidate_key)
    for entry in used:
        if candidate_key == entry.angle_key:
            return True
        used_tokens = _tokens(entry.angle_key)
        if not candidate_tokens or not used_tokens:
            continue
        overlap = len(candidate_tokens & used_tokens)
        union = len(candidate_tokens | used_tokens)
        if union and overlap / union >= 0.72:
            return True
        if candidate_key in entry.angle_key or entry.angle_key in candidate_key:
            if min(len(candidate_key), len(entry.angle_key)) >= 18:
                return True
    return False


def append_used_angle(
    profile: CreatorLongTermProfile,
    entry: UsedContentAngle,
) -> CreatorLongTermProfile:
    existing = [
        item.model_dump(mode="json")
        for item in extract_used_angles(profile)
        if item.angle_key != entry.angle_key
    ]
    existing.insert(0, entry.model_dump(mode="json"))
    preferences = dict(profile.inferred_preferences)
    preferences[USED_CONTENT_ANGLES_KEY] = existing[:_MAX_ANGLES]
    return profile.model_copy(
        update={
            "inferred_preferences": preferences,
            "updated_at": datetime.now(timezone.utc),
        }
    )


def used_angles_as_dicts(used: tuple[UsedContentAngle, ...]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in used]


def _tokens(value: str) -> set[str]:
    return {token for token in value.split("-") if len(token) >= 2}
