from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.creator.model_client import CreatorMessage, CreatorModelClient


def _settings(*, thinking_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        ai_provider="deepseek",
        ai_temperature=0.35,
        ai_max_tokens=4096,
        creator_model_timeout_seconds=60.0,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_api_key="test-only-key",
        deepseek_model="deepseek-v4-flash",
        deepseek_thinking_enabled=thinking_enabled,
    )


class CreatorModelClientTests(unittest.TestCase):
    def test_deepseek_uses_json_output_and_non_thinking_mode_by_default(
        self,
    ) -> None:
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": '{"title":"测试"}'}}]
        }

        with patch(
            "app.creator.model_client.httpx.post",
            return_value=response,
        ) as post:
            content = CreatorModelClient(_settings(thinking_enabled=False)).complete(
                [
                    CreatorMessage(
                        role="system",
                        content="Return one JSON object.",
                    )
                ]
            )

        self.assertEqual(content, '{"title":"测试"}')
        response.raise_for_status.assert_called_once_with()
        call = post.call_args
        self.assertEqual(
            call.args[0],
            "https://api.deepseek.com/chat/completions",
        )
        self.assertEqual(
            call.kwargs["headers"]["Authorization"],
            "Bearer test-only-key",
        )
        payload = json.loads(call.kwargs["content"])
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["temperature"], 0.35)

    def test_deepseek_omits_temperature_in_thinking_mode(self) -> None:
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": '{"status":"ok"}'}}]
        }

        with patch(
            "app.creator.model_client.httpx.post",
            return_value=response,
        ) as post:
            CreatorModelClient(_settings(thinking_enabled=True)).complete(
                [CreatorMessage(role="user", content="Return JSON.")]
            )

        payload = json.loads(post.call_args.kwargs["content"])
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertNotIn("temperature", payload)

    def test_request_can_override_model_budget_and_temperature(self) -> None:
        response = Mock()
        response.json.return_value = {
            "choices": [{"message": {"content": '{"status":"ok"}'}}]
        }

        with patch(
            "app.creator.model_client.httpx.post",
            return_value=response,
        ) as post:
            CreatorModelClient(_settings(thinking_enabled=False)).complete(
                [CreatorMessage(role="user", content="Return JSON.")],
                model="deepseek-writer",
                temperature=0.15,
                max_tokens=1200,
            )

        payload = json.loads(post.call_args.kwargs["content"])
        self.assertEqual(payload["model"], "deepseek-writer")
        self.assertEqual(payload["temperature"], 0.15)
        self.assertEqual(payload["max_tokens"], 1200)


if __name__ == "__main__":
    unittest.main()
