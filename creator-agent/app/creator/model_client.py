from __future__ import annotations

import json
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict


class CreatorMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    content: str


class CreatorModelClientSettings(Protocol):
    ai_provider: str
    ai_temperature: float
    ai_max_tokens: int
    ollama_base_url: str
    ollama_model: str
    openai_base_url: str
    openai_api_key: str
    openai_model: str
    deepseek_base_url: str
    deepseek_api_key: str
    deepseek_model: str
    deepseek_thinking_enabled: bool
    creator_model_timeout_seconds: float


class CreatorModelClient:
    """Text completion client used only by the Creator structured gateway."""

    def __init__(self, settings: CreatorModelClientSettings):
        self._settings = settings

    def complete(
        self,
        messages: list[CreatorMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        provider = self._settings.ai_provider.strip().lower()
        if provider == "ollama":
            return self._ollama(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        if provider == "openai":
            return self._openai(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        if provider == "deepseek":
            return self._deepseek(
                messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        raise ValueError(f"Unsupported Creator AI provider: {provider}")

    def _timeout(self) -> float:
        return max(1.0, float(self._settings.creator_model_timeout_seconds))

    def _ollama(
        self,
        messages: list[CreatorMessage],
        *,
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> str:
        payload = {
            "model": model or self._settings.ollama_model,
            "messages": [message.model_dump() for message in messages],
            "stream": False,
            "options": {
                "temperature": (
                    self._settings.ai_temperature
                    if temperature is None
                    else temperature
                ),
                "num_predict": max_tokens or self._settings.ai_max_tokens,
            },
        }
        response = httpx.post(
            f"{self._settings.ollama_base_url.rstrip('/')}/api/chat",
            json=payload,
            timeout=self._timeout(),
        )
        response.raise_for_status()
        return str(response.json()["message"]["content"])

    def _openai(
        self,
        messages: list[CreatorMessage],
        *,
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> str:
        return self._openai_compatible(
            messages,
            base_url=self._settings.openai_base_url,
            api_key=self._settings.openai_api_key,
            model=model or self._settings.openai_model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _deepseek(
        self,
        messages: list[CreatorMessage],
        *,
        model: str | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> str:
        return self._openai_compatible(
            messages,
            base_url=self._settings.deepseek_base_url,
            api_key=self._settings.deepseek_api_key,
            model=model or self._settings.deepseek_model,
            json_output=True,
            thinking_enabled=self._settings.deepseek_thinking_enabled,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def _openai_compatible(
        self,
        messages: list[CreatorMessage],
        *,
        base_url: str,
        api_key: str,
        model: str,
        json_output: bool = False,
        thinking_enabled: bool | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload = {
            "model": model,
            "messages": [message.model_dump() for message in messages],
            "max_tokens": max_tokens or self._settings.ai_max_tokens,
            "stream": False,
        }
        if thinking_enabled is None or not thinking_enabled:
            payload["temperature"] = (
                self._settings.ai_temperature
                if temperature is None
                else temperature
            )
        if json_output:
            payload["response_format"] = {"type": "json_object"}
        if thinking_enabled is not None:
            payload["thinking"] = {
                "type": "enabled" if thinking_enabled else "disabled"
            }
        response = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self._timeout(),
        )
        response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"])
