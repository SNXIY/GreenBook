from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.creator.runtime.composition import validate_creator_model_settings


class CreatorModelSettingsTests(unittest.TestCase):
    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "real provider"):
            validate_creator_model_settings(SimpleNamespace(ai_provider="unsupported"))

    def test_openai_provider_fails_closed_when_model_configuration_is_missing(
        self,
    ) -> None:
        settings = SimpleNamespace(
            ai_provider="openai",
            openai_base_url="https://model.example.test/v1",
            openai_api_key="",
            openai_model="",
        )

        with self.assertRaisesRegex(
            ValueError,
            "OPENAI_API_KEY, OPENAI_MODEL",
        ):
            validate_creator_model_settings(settings)

    def test_openai_provider_accepts_explicit_configuration(self) -> None:
        validate_creator_model_settings(
            SimpleNamespace(
                ai_provider="openai",
                openai_base_url="https://model.example.test/v1",
                openai_api_key="test-only-key",
                openai_model="creator-model",
            )
        )

    def test_deepseek_provider_fails_closed_without_api_key(self) -> None:
        settings = SimpleNamespace(
            ai_provider="deepseek",
            deepseek_base_url="https://api.deepseek.com",
            deepseek_api_key="",
            deepseek_model="deepseek-v4-flash",
        )

        with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
            validate_creator_model_settings(settings)

    def test_deepseek_provider_accepts_explicit_configuration(self) -> None:
        validate_creator_model_settings(
            SimpleNamespace(
                ai_provider="deepseek",
                deepseek_base_url="https://api.deepseek.com",
                deepseek_api_key="test-only-key",
                deepseek_model="deepseek-v4-flash",
            )
        )


if __name__ == "__main__":
    unittest.main()
