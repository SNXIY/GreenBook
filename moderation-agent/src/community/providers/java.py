from urllib.parse import quote

import httpx

from moderation.schemas import (
    CommunityContentRecord,
    CommunityContentSnapshot,
    ModerationTaskDetail,
    ReportEvidence,
    ViolationRecord,
)


class JavaCommunityDataProvider:
    """HTTP adapter reserved for the real Java community service."""

    def __init__(
        self,
        *,
        base_url: str,
        auth_token: str | None = None,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def apply_moderation_result(self, task: ModerationTaskDetail) -> None:
        if task.platform != "zhiguang" or not task.content_id:
            return
        reason = (
            task.human_decision.comment
            if task.human_decision and task.human_decision.comment
            else task.agent_decision.reason
            if task.agent_decision
            else task.error_message
        )
        await self._post(
            f"/api/v1/internal/moderation/tasks/{self._segment(str(task.id))}/result",
            {
                "content_id": task.content_id,
                "status": task.status.value,
                "final_action": (
                    task.final_action.value if task.final_action is not None else None
                ),
                "reason": reason,
            },
            trace_id=task.trace_id,
        )

    async def get_content_context(self, content_id: str) -> CommunityContentSnapshot:
        payload = await self._get(
            f"/api/v1/internal/moderation/contents/{self._segment(content_id)}/context"
        )
        return CommunityContentSnapshot.model_validate(payload)

    async def get_parent_comment(self, content_id: str) -> CommunityContentRecord | None:
        payload = await self._get(
            f"/api/v1/internal/moderation/contents/{self._segment(content_id)}/parent"
        )
        return CommunityContentRecord.model_validate(payload) if payload is not None else None

    async def get_conversation_context(
        self,
        content_id: str,
        limit: int,
    ) -> list[CommunityContentRecord]:
        payload = await self._get(
            f"/api/v1/internal/moderation/contents/{self._segment(content_id)}/conversation",
            params={"limit": limit},
        )
        return [CommunityContentRecord.model_validate(item) for item in payload]

    async def get_author_recent_contents(
        self,
        author_id: str,
        limit: int,
    ) -> list[CommunityContentRecord]:
        payload = await self._get(
            f"/api/v1/internal/moderation/authors/{self._segment(author_id)}/contents",
            params={"limit": limit},
        )
        return [CommunityContentRecord.model_validate(item) for item in payload]

    async def get_author_violation_history(self, author_id: str) -> list[ViolationRecord]:
        payload = await self._get(
            f"/api/v1/internal/moderation/authors/{self._segment(author_id)}/violations"
        )
        return [ViolationRecord.model_validate(item) for item in payload]

    async def get_content_reports(self, content_id: str) -> list[ReportEvidence]:
        payload = await self._get(
            f"/api/v1/internal/moderation/contents/{self._segment(content_id)}/reports"
        )
        return [ReportEvidence.model_validate(item) for item in payload]

    async def _get(self, path: str, params: dict[str, int] | None = None):
        headers = (
            {"X-Moderation-Service-Secret": self.auth_token}
            if self.auth_token
            else None
        )
        response = await self._client.get(path, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

    async def _post(
        self, path: str, payload: dict, *, trace_id: str | None = None
    ) -> None:
        headers = (
            {"X-Moderation-Service-Secret": self.auth_token}
            if self.auth_token
            else None
        )
        if trace_id:
            headers = {**(headers or {}), "X-Trace-ID": trace_id}
        response = await self._client.post(path, json=payload, headers=headers)
        response.raise_for_status()

    @staticmethod
    def _segment(value: str) -> str:
        return quote(value, safe="")
