import hashlib
import math
import re

_WORD = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]")


class HashingTextEmbedder:
    """Dependency-free feature hashing for deterministic first-version retrieval."""

    def __init__(self, dimensions: int = 256) -> None:
        if dimensions < 32:
            raise ValueError("Embedding dimensions must be at least 32")
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        lowered = text.lower()
        tokens = _WORD.findall(lowered)
        cjk = _CJK.findall(lowered)
        tokens.extend(cjk)
        tokens.extend("".join(pair) for pair in zip(cjk, cjk[1:], strict=False))
        if not tokens:
            tokens = [lowered or "empty"]

        vector = [0.0] * self.dimensions
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Vectors must have the same dimensions")
    return sum(a * b for a, b in zip(left, right, strict=True))
