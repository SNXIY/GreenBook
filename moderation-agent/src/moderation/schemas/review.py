from pydantic import BaseModel, Field

from moderation.schemas.decision import HumanDecision
from moderation.schemas.task import ModerationTaskDetail


class HumanReviewSubmit(HumanDecision):
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class HumanReviewResult(BaseModel):
    task: ModerationTaskDetail
    case_created: bool
