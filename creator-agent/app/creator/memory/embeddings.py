from __future__ import annotations

import hashlib
import math
import re
from typing import Any

import httpx

from app.creator.memory.errors import CreatorMemoryIntegrityError

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]", re.IGNORECASE)


class HashingCreatorEmbedder:
    """Deterministic local embedder for development and tests.

    It preserves lexical similarity without claiming model-level semantics.
    Production deployments should use the configured embedding provider.
    """

    name = "hashing-local"

    def __init__(self, *, dimensions: int = 256) -> None:
        if dimensions < 32:
            raise ValueError("Hashing embedder dimensions must be at least 32")
        self.dimensions = dimensions

    async def embed(
        self,
        texts: tuple[str, ...],
    ) -> tuple[tuple[float, ...], ...]:
        return tuple(self._embed_one(text) for text in texts)

    def _embed_one(self, text: str) -> tuple[float, ...]:
        vector = [0.0] * self.dimensions
        tokens = _tokens(text)
        if not tokens:
            vector[0] = 1.0
            return tuple(vector)
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            vector[0] = 1.0
            return tuple(vector)
        return tuple(value / norm for value in vector)


class OpenAICompatibleCreatorEmbedder:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Embedding API key is required")
        if dimensions <= 0:
            raise ValueError("Embedding dimensions must be greater than zero")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self.dimensions = dimensions
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def embed(
        self,
        texts: tuple[str, ...],
    ) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        response = await self._client.post(
            f"{self._base_url}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "input": [text if text.strip() else " " for text in texts],
                "dimensions": self.dimensions,
            },
        )
        response.raise_for_status()
        rows: list[dict[str, Any]] = sorted(
            response.json().get("data", ()),
            key=lambda row: int(row.get("index", 0)),
        )
        vectors = tuple(
            tuple(float(value) for value in row.get("embedding", ())) for row in rows
        )
        if len(vectors) != len(texts) or any(
            len(vector) != self.dimensions for vector in vectors
        ):
            raise CreatorMemoryIntegrityError(
                "Embedding response dimensions do not match configuration",
                details={
                    "expected_count": len(texts),
                    "actual_count": len(vectors),
                    "dimensions": self.dimensions,
                },
            )
        return vectors

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _tokens(text: str) -> tuple[str, ...]:
    normalized = text.lower()
    base = _TOKEN_PATTERN.findall(normalized)
    cjk = "".join(
        character for character in normalized if "\u3400" <= character <= "\u9fff"
    )
    bigrams = [cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))]
    return tuple((*base, *bigrams))
