from __future__ import annotations

import unittest

from app.creator.agents.gateway import RoutedCreatorModelGateway
from app.creator.agents.schemas import DraftDocument
from app.creator.runtime.ports import CreatorModelRequest


class _RecordingGateway:
    def __init__(self) -> None:
        self.models: list[str | None] = []

    async def complete_structured(self, request, output_type):
        self.models.append(request.model)
        return (
            output_type.model_validate(
                {"title": "测试", "body_markdown": "用于验证模型路由。"}
            ),
            10,
            5,
        )


class CreatorModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_agent_operations_to_configured_models(self) -> None:
        delegate = _RecordingGateway()
        gateway = RoutedCreatorModelGateway(
            delegate,
            analysis_model="analysis-model",
            writer_model="writer-model",
            critic_model="critic-model",
            assist_model="assist-model",
        )

        for operation in (
            "memory.profile",
            "writer.draft",
            "critic.review",
            "evaluation.score",
            "editor.suggest",
        ):
            await gateway.complete_structured(
                CreatorModelRequest(
                    operation=operation,
                    system_prompt="system",
                    user_prompt="{}",
                ),
                DraftDocument,
            )

        self.assertEqual(
            delegate.models,
            [
                "analysis-model",
                "writer-model",
                "critic-model",
                "critic-model",
                "assist-model",
            ],
        )

    async def test_preserves_explicit_request_when_route_is_unconfigured(
        self,
    ) -> None:
        delegate = _RecordingGateway()
        gateway = RoutedCreatorModelGateway(delegate)

        await gateway.complete_structured(
            CreatorModelRequest(
                operation="writer.draft",
                system_prompt="system",
                user_prompt="{}",
                model="request-model",
            ),
            DraftDocument,
        )

        self.assertEqual(delegate.models, ["request-model"])
