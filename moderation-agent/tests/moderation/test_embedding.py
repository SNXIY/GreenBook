import pytest

from rag.embedding import HashingTextEmbedder, cosine_similarity


def test_hashing_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashingTextEmbedder(64)
    first = embedder.embed("discount sale buy now")
    second = embedder.embed("discount sale buy now")

    assert first == second
    assert sum(value * value for value in first) == pytest.approx(1.0)


def test_related_text_has_higher_similarity_than_unrelated_text() -> None:
    embedder = HashingTextEmbedder(256)
    query = embedder.embed("discount sale buy now")
    related = embedder.embed("buy now for a sale discount")
    unrelated = embedder.embed("ordinary weather conversation")

    assert cosine_similarity(query, related) > cosine_similarity(query, unrelated)
