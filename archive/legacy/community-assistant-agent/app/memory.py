from __future__ import annotations

import hashlib
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import delete, select

from app.config import Settings
from app.database import (
    Database,
    Conversation,
    EpisodicMemory,
    MemoryProfile,
    Run,
    SemanticMemoryDocument,
    new_id,
    utc_now,
)


logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]", re.IGNORECASE)
_SENSITIVE_PATTERN = re.compile(
    r"(password|passphrase|api[\s_-]*key|access[\s_-]*key|secret|"
    r"bearer\s+[a-z0-9._-]+|token|密码|密钥|口令|身份证|银行卡|信用卡)",
    re.IGNORECASE,
)
_POINT_NAMESPACE = uuid.UUID("4c15d757-d68e-43c7-b781-e743a330243f")
_ARTIFACT_KEYS = {
    "post_id",
    "draft_id",
    "action_id",
    "creator_task_id",
}


class HashingMemoryEmbedder:
    """Deterministic local vectorizer.

    This is feature hashing, not a mock model. It provides lexical similarity and
    keeps local development self-contained. Configure the OpenAI-compatible
    provider when model-level semantic similarity is required.
    """

    name = "hashing-local"

    def __init__(self, dimensions: int) -> None:
        if dimensions < 32:
            raise ValueError("Memory embedding dimensions must be at least 32")
        self.dimensions = dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(_hash_vector(text, self.dimensions) for text in texts)

    async def close(self) -> None:
        return None


class OpenAICompatibleMemoryEmbedder:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Memory embedding API key is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self.dimensions = dimensions
        self._http = httpx.AsyncClient(timeout=timeout_seconds)

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        response = await self._http.post(
            f"{self._base_url}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "input": [text if text.strip() else " " for text in texts],
                "dimensions": self.dimensions,
            },
        )
        response.raise_for_status()
        rows = sorted(
            response.json().get("data", []),
            key=lambda item: int(item.get("index", 0)),
        )
        vectors = tuple(
            tuple(float(value) for value in item.get("embedding", []))
            for item in rows
        )
        if len(vectors) != len(texts) or any(
            len(vector) != self.dimensions for vector in vectors
        ):
            raise RuntimeError("Embedding response does not match configured dimensions")
        return vectors

    async def close(self) -> None:
        await self._http.aclose()


@dataclass(frozen=True)
class MemoryHealth:
    episodic: str
    semantic: str
    backend: str
    embedding: str


class AssistantMemory:
    # 任务完成后写入
    # Postgres：权威存储（情节记忆等）
    # Qdrant（若开启）：语义向量索引，加速相似检索
    # 下次执行任务前检索

    # recall 按用户/上下文把相关记忆捞出来
    # 塞进规划/回答的 prompt 里
    # 注释里也写了：Postgres 是主库，Qdrant 只是加速索引。Qdrant 挂了还可以退回用 Postgres 召回；本地没配复杂 embedding 时，可用 hashing 向量做简单相似匹配。
    """Four-layer memory coordinator.

    Run/checkpoint state remains working memory, messages remain conversational
    context, this service adds durable episodes and a rebuildable semantic index.
    PostgreSQL is authoritative; Qdrant is only an acceleration index.
    """

    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self._embedder = (
            OpenAICompatibleMemoryEmbedder(
                base_url=settings.memory_embedding_base_url,
                api_key=settings.memory_embedding_api_key,
                model=settings.memory_embedding_model,
                dimensions=settings.memory_embedding_dimensions,
                timeout_seconds=settings.memory_embedding_timeout_seconds,
            )
            if settings.memory_embedding_provider == "openai"
            else HashingMemoryEmbedder(settings.memory_embedding_dimensions)
        )
        self._qdrant: httpx.AsyncClient | None = None
        self._semantic_status = (
            "STARTING" if settings.semantic_memory_enabled else "DISABLED"
        )

    async def start(self) -> None:
        if not self.settings.semantic_memory_enabled:
            return
        try:
            headers = (
                {"api-key": self.settings.memory_qdrant_api_key}
                if self.settings.memory_qdrant_api_key
                else None
            )
            self._qdrant = httpx.AsyncClient(
                base_url=self.settings.memory_qdrant_url.rstrip("/"),
                headers=headers,
                timeout=30.0,
            )
            await self._ensure_collection()
            self._semantic_status = "UP"
            await self.purge_expired(limit=1_000)
            await self.reindex_pending(limit=200)
        except Exception:
            self._semantic_status = "DOWN"
            if self._qdrant is not None:
                await self._qdrant.aclose()
                self._qdrant = None
            if self.settings.semantic_memory_required:
                raise
            logger.exception(
                "Assistant semantic memory is unavailable; PostgreSQL recall remains active"
            )

    async def close(self) -> None:
        if self._qdrant is not None:
            await self._qdrant.aclose()
            self._qdrant = None
        await self._embedder.close()

    def health(self) -> MemoryHealth:
        return MemoryHealth(
            episodic="UP" if self.settings.episodic_memory_enabled else "DISABLED",
            semantic=self._semantic_status,
            backend="qdrant" if self._qdrant is not None else "postgresql-fallback",
            embedding=self._embedder.name,
        )

    async def recall(
        self,
        *,
        user_id: str,
        tenant_id: str,
        query: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.settings.episodic_memory_enabled or is_sensitive_memory(query):
            return []
        profile = await self._profile(user_id)
        if not profile.episodic_enabled:
            return []
        recall_limit = max(
            1,
            min(20, limit or self.settings.episodic_memory_recall_limit),
        )
        now = utc_now()
        async with self.database.sessions() as session:
            documents = (
                await session.scalars(
                    select(SemanticMemoryDocument)
                    .where(
                        SemanticMemoryDocument.user_id == user_id,
                        SemanticMemoryDocument.tenant_id == tenant_id,
                        SemanticMemoryDocument.expires_at > now,
                    )
                    .order_by(SemanticMemoryDocument.created_at.desc())
                    .limit(120)
                )
            ).all()
        if not documents:
            return []

        vector_scores: dict[str, float] = {}
        if profile.semantic_enabled and self._qdrant is not None:
            try:
                vector_scores = await self._vector_search(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    query=query,
                    limit=min(40, recall_limit * 5),
                )
            except Exception:
                self._semantic_status = "DEGRADED"
                logger.exception("Semantic recall failed; using PostgreSQL fallback")

        query_tokens = set(_tokens(query))
        ranked: list[tuple[float, SemanticMemoryDocument]] = []
        for document in documents:
            lexical = _overlap_score(query_tokens, set(_tokens(document.content)))
            vector = vector_scores.get(document.id, 0.0)
            recency = _recency_score(document.created_at, now)
            if vector <= 0 and lexical <= 0:
                continue
            score = vector * 0.68 + lexical * 0.22 + recency * 0.10
            ranked.append((score, document))
        ranked.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
        selected = [document for _, document in ranked[:recall_limit]]
        if not selected:
            return []

        episode_ids = [
            document.source_id
            for document in selected
            if document.source_type == "EPISODE"
        ]
        episode_by_id: dict[str, EpisodicMemory] = {}
        async with self.database.sessions() as session, session.begin():
            if episode_ids:
                episodes = (
                    await session.scalars(
                        select(EpisodicMemory).where(EpisodicMemory.id.in_(episode_ids))
                    )
                ).all()
                for episode in episodes:
                    episode.last_recalled_at = now
                    episode.recall_count += 1
                    episode_by_id[episode.id] = episode

        recalled: list[dict[str, Any]] = []
        for document in selected:
            episode = episode_by_id.get(document.source_id)
            recalled.append(
                {
                    "memory_id": document.id,
                    "kind": document.kind,
                    "title": document.title,
                    "content": _truncate(document.content, 1_600),
                    "source": document.source_type,
                    "occurred_at": (
                        episode.occurred_at.isoformat()
                        if episode is not None
                        else document.created_at.isoformat()
                    ),
                    "tools": list(episode.tool_names) if episode is not None else [],
                }
            )
        return bound_recalled_memories(
            recalled,
            max_chars=self.settings.memory_context_max_chars,
        )

    async def record_completed_run(
        self,
        run_id: str,
        outputs: list[dict[str, Any]],
    ) -> EpisodicMemory | None:
        if not self.settings.episodic_memory_enabled:
            return None
        await self.purge_expired(limit=500)
        tool_names = sorted(
            {
                str(item.get("tool"))
                for item in outputs
                if str(item.get("tool") or "").strip()
            }
        )
        async with self.database.sessions() as session:
            run = await session.get(Run, run_id)
            if run is None or run.status != "COMPLETED":
                return None
            if not tool_names and (run.intent or "").upper() == "ANSWER":
                return None
            conversation = await session.get(Conversation, run.conversation_id)
            tenant_id = (
                conversation.tenant_id if conversation is not None else "zhiguang"
            )
            profile = await session.get(MemoryProfile, run.user_id)
            if profile is not None and not profile.episodic_enabled:
                return None
            if is_sensitive_memory(f"{run.prompt}\n{run.final_response or ''}"):
                return None
            existing = await session.scalar(
                select(EpisodicMemory).where(EpisodicMemory.run_id == run_id)
            )
            if existing is not None:
                document = await session.scalar(
                    select(SemanticMemoryDocument).where(
                        SemanticMemoryDocument.source_type == "EPISODE",
                        SemanticMemoryDocument.source_id == existing.id,
                    )
                )
                if document is not None and document.index_status != "INDEXED":
                    await self._index_document(document)
                return existing

        refs = _artifact_refs(outputs)
        now = utc_now()
        expires_at = now + timedelta(
            days=self.settings.episodic_memory_retention_days
        )
        goal = _truncate(_clean(run.prompt), 1_000)
        result = _truncate(_clean(run.final_response), 2_400)
        summary = result or _truncate(_clean(run.summary), 2_400)
        intent = _clean(run.intent) or None
        importance = min(
            1.0,
            0.45
            + (0.15 if any("publish" in name for name in tool_names) else 0)
            + (0.10 if len(tool_names) >= 3 else 0),
        )
        episode_id = new_id()
        content = _semantic_content(
            goal=goal,
            summary=summary,
            intent=intent,
            tool_names=tool_names,
        )
        document: SemanticMemoryDocument | None = None
        async with self.database.sessions() as session, session.begin():
            episode = EpisodicMemory(
                id=episode_id,
                user_id=run.user_id,
                tenant_id=tenant_id,
                run_id=run.id,
                conversation_id=run.conversation_id,
                intent=intent,
                goal=goal,
                summary=summary,
                outcome="COMPLETED",
                tool_names=tool_names,
                artifact_refs=refs,
                importance=importance,
                occurred_at=run.completed_at or now,
                expires_at=expires_at,
            )
            session.add(episode)
            semantic_enabled = profile is None or profile.semantic_enabled
            document = SemanticMemoryDocument(
                id=new_id(),
                user_id=run.user_id,
                tenant_id=tenant_id,
                kind="TASK_KNOWLEDGE",
                title=_truncate(goal, 240),
                content=content,
                tags=[value for value in [intent, *tool_names] if value],
                source_type="EPISODE",
                source_id=episode_id,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                index_status="PENDING" if semantic_enabled else "DISABLED",
                expires_at=expires_at,
            )
            session.add(document)
        if document is not None and semantic_enabled:
            await self._index_document(document)
        return episode

    async def reindex_pending(self, *, limit: int) -> int:
        if self._qdrant is None:
            return 0
        async with self.database.sessions() as session:
            documents = (
                await session.scalars(
                    select(SemanticMemoryDocument)
                    .where(
                        SemanticMemoryDocument.index_status.in_(["PENDING", "FAILED"]),
                        SemanticMemoryDocument.expires_at > utc_now(),
                    )
                    .order_by(SemanticMemoryDocument.updated_at)
                    .limit(limit)
                )
            ).all()
        indexed = 0
        for document in documents:
            if await self._index_document(document):
                indexed += 1
        return indexed

    async def purge_expired(self, *, limit: int) -> int:
        now = utc_now()
        async with self.database.sessions() as session, session.begin():
            documents = (
                await session.scalars(
                    select(SemanticMemoryDocument)
                    .where(SemanticMemoryDocument.expires_at <= now)
                    .order_by(SemanticMemoryDocument.expires_at)
                    .limit(limit)
                )
            ).all()
            document_ids = [document.id for document in documents]
            for document in documents:
                await session.delete(document)
            episodes = (
                await session.scalars(
                    select(EpisodicMemory)
                    .where(EpisodicMemory.expires_at <= now)
                    .order_by(EpisodicMemory.expires_at)
                    .limit(limit)
                )
            ).all()
            for episode in episodes:
                await session.delete(episode)
        await self._delete_points(document_ids)
        return len(episodes)

    async def delete_episode(self, *, user_id: str, episode_id: str) -> bool:
        document_id: str | None = None
        async with self.database.sessions() as session, session.begin():
            episode = await session.scalar(
                select(EpisodicMemory).where(
                    EpisodicMemory.id == episode_id,
                    EpisodicMemory.user_id == user_id,
                )
            )
            if episode is None:
                return False
            document = await session.scalar(
                select(SemanticMemoryDocument).where(
                    SemanticMemoryDocument.source_type == "EPISODE",
                    SemanticMemoryDocument.source_id == episode.id,
                )
            )
            if document is not None:
                document_id = document.id
                await session.delete(document)
            await session.delete(episode)
        if document_id is not None:
            await self._delete_points([document_id])
        return True

    async def clear_episodes(self, *, user_id: str) -> int:
        async with self.database.sessions() as session, session.begin():
            episode_ids = list(
                await session.scalars(
                    select(EpisodicMemory.id).where(EpisodicMemory.user_id == user_id)
                )
            )
            document_ids = list(
                await session.scalars(
                    select(SemanticMemoryDocument.id).where(
                        SemanticMemoryDocument.user_id == user_id,
                        SemanticMemoryDocument.source_type == "EPISODE",
                    )
                )
            )
            await session.execute(
                delete(SemanticMemoryDocument).where(
                    SemanticMemoryDocument.user_id == user_id,
                    SemanticMemoryDocument.source_type == "EPISODE",
                )
            )
            await session.execute(
                delete(EpisodicMemory).where(EpisodicMemory.user_id == user_id)
            )
        await self._delete_points(document_ids)
        return len(episode_ids)

    async def sync_semantic_setting(self, *, user_id: str, enabled: bool) -> None:
        async with self.database.sessions() as session, session.begin():
            documents = (
                await session.scalars(
                    select(SemanticMemoryDocument).where(
                        SemanticMemoryDocument.user_id == user_id,
                        SemanticMemoryDocument.expires_at > utc_now(),
                    )
                )
            ).all()
            for document in documents:
                document.index_status = "PENDING" if enabled else "DISABLED"
                document.index_error = None
        if not enabled:
            await self._delete_user_points(user_id)
            return
        if self._qdrant is not None:
            for document in documents:
                await self._index_document(document)

    async def _profile(self, user_id: str) -> MemoryProfile:
        async with self.database.sessions() as session:
            profile = await session.get(MemoryProfile, user_id)
        return profile or MemoryProfile(
            user_id=user_id,
            episodic_enabled=True,
            semantic_enabled=True,
        )

    async def _ensure_collection(self) -> None:
        client = self._require_qdrant()
        collection = self.settings.memory_qdrant_collection
        response = await client.get(f"/collections/{collection}")
        if response.status_code == 404:
            response = await client.put(
                f"/collections/{collection}",
                json={
                    "vectors": {
                        "size": self._embedder.dimensions,
                        "distance": "Cosine",
                    },
                    "on_disk_payload": True,
                },
            )
            response.raise_for_status()
            response = await client.get(f"/collections/{collection}")
        response.raise_for_status()
        info = response.json().get("result", {})
        vectors = (
            info.get("config", {})
            .get("params", {})
            .get("vectors", {})
        )
        actual = vectors.get("size") if isinstance(vectors, dict) else None
        if actual is not None and int(actual) != self._embedder.dimensions:
            raise RuntimeError(
                "Assistant memory Qdrant dimensions do not match configuration"
            )
        existing = set((info.get("payload_schema") or {}).keys())
        for name in ("tenant_id", "user_id", "document_id"):
            if name not in existing:
                index_response = await client.put(
                    f"/collections/{collection}/index",
                    params={"wait": "true"},
                    json={
                        "field_name": name,
                        "field_schema": "keyword",
                    },
                )
                if index_response.status_code not in {200, 201, 409}:
                    index_response.raise_for_status()

    async def _index_document(self, document: SemanticMemoryDocument) -> bool:
        if self._qdrant is None:
            return False
        async with self.database.sessions() as session:
            profile = await session.get(MemoryProfile, document.user_id)
            if profile is not None and (
                not profile.episodic_enabled or not profile.semantic_enabled
            ):
                async with self.database.sessions() as update_session, update_session.begin():
                    current = await update_session.get(
                        SemanticMemoryDocument, document.id
                    )
                    if current is not None:
                        current.index_status = "DISABLED"
                        current.index_error = None
                return False
        try:
            vector = (await self._embedder.embed((document.content,)))[0]
            response = await self._qdrant.put(
                f"/collections/{self.settings.memory_qdrant_collection}/points",
                params={"wait": "true"},
                json={
                    "points": [
                        {
                            "id": _point_id(document.id),
                            "vector": list(vector),
                            "payload": {
                            "document_id": document.id,
                            "tenant_id": document.tenant_id,
                            "user_id": document.user_id,
                            "kind": document.kind,
                            "expires_at": document.expires_at.isoformat(),
                            },
                        }
                    ]
                },
            )
            response.raise_for_status()
        except Exception as exc:
            async with self.database.sessions() as session, session.begin():
                current = await session.get(SemanticMemoryDocument, document.id)
                if current is not None:
                    current.index_status = "FAILED"
                    current.index_error = _truncate(str(exc), 2_000)
            if self.settings.semantic_memory_required:
                raise
            logger.exception("Failed to index assistant memory document %s", document.id)
            return False
        async with self.database.sessions() as session, session.begin():
            current = await session.get(SemanticMemoryDocument, document.id)
            if current is not None:
                current.index_status = "INDEXED"
                current.index_error = None
        return True

    async def _vector_search(
        self,
        *,
        user_id: str,
        tenant_id: str,
        query: str,
        limit: int,
    ) -> dict[str, float]:
        vector = (await self._embedder.embed((query,)))[0]
        response = await self._require_qdrant().post(
            f"/collections/{self.settings.memory_qdrant_collection}/points/query",
            json={
                "query": list(vector),
                "filter": {
                    "must": [
                        {"key": "tenant_id", "match": {"value": tenant_id}},
                        {"key": "user_id", "match": {"value": user_id}},
                    ]
                },
                "with_payload": True,
                "limit": limit,
                "score_threshold": self.settings.memory_semantic_score_threshold,
            },
        )
        response.raise_for_status()
        points = response.json().get("result", {}).get("points", [])
        return {
            str(point["payload"]["document_id"]): max(
                0.0, float(point.get("score", 0.0))
            )
            for point in points
            if point.get("payload", {}).get("document_id")
        }

    async def _delete_points(self, document_ids: list[str]) -> None:
        if self._qdrant is None or not document_ids:
            return
        try:
            response = await self._qdrant.post(
                f"/collections/{self.settings.memory_qdrant_collection}/points/delete",
                params={"wait": "true"},
                json={
                    "points": [
                        _point_id(document_id) for document_id in document_ids
                    ]
                },
            )
            response.raise_for_status()
        except Exception:
            logger.exception("Failed to remove deleted assistant memories from Qdrant")

    async def _delete_user_points(self, user_id: str) -> None:
        if self._qdrant is None:
            return
        try:
            response = await self._qdrant.post(
                f"/collections/{self.settings.memory_qdrant_collection}/points/delete",
                params={"wait": "true"},
                json={
                    "filter": {
                        "must": [
                            {"key": "user_id", "match": {"value": user_id}}
                        ]
                    }
                },
            )
            response.raise_for_status()
        except Exception:
            logger.exception("Failed to disable semantic index for user %s", user_id)

    def _require_qdrant(self) -> httpx.AsyncClient:
        if self._qdrant is None:
            raise RuntimeError("Assistant semantic memory is unavailable")
        return self._qdrant


def is_sensitive_memory(value: str) -> bool:
    return bool(_SENSITIVE_PATTERN.search(value or ""))


def bound_recalled_memories(
    memories: list[dict[str, Any]], *, max_chars: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    used = 0
    for memory in memories:
        candidate = dict(memory)
        candidate["content"] = _truncate(str(candidate.get("content") or ""), 1_600)
        cost = len(str(candidate))
        if result and used + cost > max_chars:
            break
        if not result and cost > max_chars:
            candidate["content"] = _truncate(
                candidate["content"], max(200, max_chars - 500)
            )
            cost = len(str(candidate))
        result.append(candidate)
        used += cost
    return result


def _semantic_content(
    *,
    goal: str,
    summary: str,
    intent: str | None,
    tool_names: list[str],
) -> str:
    lines = [f"目标：{goal}"]
    if intent:
        lines.append(f"任务类型：{intent}")
    if tool_names:
        lines.append(f"执行能力：{', '.join(tool_names)}")
    if summary:
        lines.append(f"结果：{summary}")
    return "\n".join(lines)


def _artifact_refs(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in _ARTIFACT_KEYS and isinstance(child, (str, int)):
                    ref = {"type": key, "id": str(child)}
                    found[(key, str(child))] = ref
                else:
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return list(found.values())[:30]


def _hash_vector(text: str, dimensions: int) -> tuple[float, ...]:
    vector = [0.0] * dimensions
    tokens = _tokens(text)
    if not tokens:
        vector[0] = 1.0
        return tuple(vector)
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % dimensions
        sign = 1.0 if digest[8] & 1 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        vector[0] = 1.0
        return tuple(vector)
    return tuple(value / norm for value in vector)


def _tokens(text: str) -> tuple[str, ...]:
    normalized = _clean(text).lower()
    base = _TOKEN_PATTERN.findall(normalized)
    cjk = "".join(
        character for character in normalized if "\u3400" <= character <= "\u9fff"
    )
    bigrams = [cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))]
    return tuple((*base, *bigrams))


def _overlap_score(query: set[str], document: set[str]) -> float:
    if not query or not document:
        return 0.0
    return len(query & document) / math.sqrt(len(query) * len(document))


def _recency_score(created_at: datetime, now: datetime) -> float:
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - created_at).total_seconds() / 86_400)
    return math.exp(-age_days / 30.0)


def _point_id(document_id: str) -> str:
    return str(uuid.uuid5(_POINT_NAMESPACE, document_id))


def _clean(value: Any) -> str:
    return str(value or "").replace("\x00", " ").strip()


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"
