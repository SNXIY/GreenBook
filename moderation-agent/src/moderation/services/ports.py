from datetime import datetime
from typing import Protocol
from uuid import UUID

from moderation.models import ModerationPolicy, ModerationReviewCase


class ReviewQueueIndex(Protocol):
    async def enqueue(self, task_id: UUID, created_at: datetime) -> None: ...

    async def remove(self, task_id: UUID) -> None: ...


class KnowledgeIndex(Protocol):
    async def index_policy(self, policy: ModerationPolicy) -> None: ...

    async def index_case(self, review_case: ModerationReviewCase) -> None: ...


class NoopReviewQueueIndex:
    async def enqueue(self, task_id: UUID, created_at: datetime) -> None:
        del task_id, created_at

    async def remove(self, task_id: UUID) -> None:
        del task_id


class NoopKnowledgeIndex:
    async def index_policy(self, policy: ModerationPolicy) -> None:
        del policy

    async def index_case(self, review_case: ModerationReviewCase) -> None:
        del review_case
