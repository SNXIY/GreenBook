from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.creator.runtime.models import (
    AgentDescriptor,
    AgentExecutionContext,
    AgentResult,
    CreatorArtifact,
)


class CreatorArtifactStore(Protocol):
    async def put(self, artifact: CreatorArtifact) -> None: ...

    async def get(self, artifact_id: str) -> CreatorArtifact | None: ...

    async def get_many(
        self, artifact_ids: tuple[str, ...]
    ) -> tuple[CreatorArtifact, ...]: ...

    async def list_for_run(self, run_id: str) -> tuple[CreatorArtifact, ...]: ...


class CreatorSpecialistAgent(Protocol):
    descriptor: AgentDescriptor

    async def execute(self, context: AgentExecutionContext) -> AgentResult: ...


class CreatorModelRequest(BaseModel):
    operation: str
    system_prompt: str
    user_prompt: str
    temperature: float = 0.2
    max_output_tokens: int = 4_000
    model: str | None = None


OutputModelT = TypeVar("OutputModelT", bound=BaseModel)


class CreatorModelGateway(Protocol):
    async def complete_structured(
        self,
        request: CreatorModelRequest,
        output_type: type[OutputModelT],
    ) -> tuple[OutputModelT, int, int]: ...
