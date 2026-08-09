"""Translate a RuntimeResult into a stable, user-facing Assistant response.

The presenter is deliberately read-only.  It does not invoke tools, inspect
``assistant_runs``, or mutate execution state; it only formats facts already
returned by the Runtime result boundary.
"""

from __future__ import annotations

import re
from typing import Any

from greenbook_assistant_core.time_parser import format_local_schedule_time
from pydantic import BaseModel, Field

from ..models.runtime_result import RuntimeResult


class PresentationArtifact(BaseModel):
    """A safe, UI-neutral representation of a Runtime artifact."""

    type: str
    artifact_id: str = ""
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    resource_id: str | None = None
    run_at: str | None = None
    publish_time: str | None = None
    timezone: str | None = None
    status: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AssistantResponse(BaseModel):
    """Structured response shared by HTTP, chat history, and the frontend."""

    message: str
    artifacts: list[PresentationArtifact] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    execution_id: str | None = None
    status: str = ""
    error: str | None = None
    error_code: str | None = None
    retry_available: bool = False
    approval_required: bool = False
    approval_id: str | None = None
    # Product projection fields. Runtime statuses remain internal; these
    # fields provide a stable, human-readable timeline for Assistant UI.
    task_name: str = ""
    steps: list[dict[str, Any]] = Field(default_factory=list)
    current_step: str | None = None
    completed_steps: int = 0
    total_steps: int = 0
    progress: float = 0.0


_TYPE_ALIASES = {
    "DRAFT": "POST_DRAFT",
    "POST_DRAFT": "POST_DRAFT",
    "SCHEDULE": "PUBLICATION_SCHEDULE",
    "PUBLICATION_SCHEDULE": "PUBLICATION_SCHEDULE",
    "ANALYSIS": "ANALYSIS_REPORT",
    "ANALYSIS_REPORT": "ANALYSIS_REPORT",
    "VALIDATION_REPORT": "VALIDATION_REPORT",
    "PERFORMANCE_DATA": "ANALYSIS_REPORT",
    "OPERATION_PLAN": "OPERATION_PLAN",
    "SEARCH_RESULT": "SEARCH_RESULT",
}

_WAITING_STATUSES = {"WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED"}
_IN_PROGRESS_STATUSES = {"PENDING", "RUNNING", "QUEUED", "RETRYING"}

_STEP_LABELS = {
    "GENERATE_CONTENT": "创作内容",
    "VALIDATE_QUALITY": "内容检查",
    "SCHEDULE_PUBLISH": "定时发布",
}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _compact(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", _text(value)).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _format_section_value(value: Any, limit: int = 600) -> str:
    if isinstance(value, list):
        items = [_compact(item, 180) for item in value if _compact(item, 180)]
        return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))[:limit]
    if isinstance(value, dict):
        items = [f"{key}：{_compact(item, 140)}" for key, item in value.items()]
        return "\n".join(items)[:limit]
    return _compact(value, limit)


def _payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract tool data while accepting both result and artifact envelopes."""

    for key in ("data", "payload"):
        value = raw.get(key)
        if isinstance(value, dict):
            return dict(value)
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        tool_result = metadata.get("tool_result")
        if isinstance(tool_result, dict):
            return dict(tool_result)
    return {}


def _normalise_artifact(raw: Any) -> PresentationArtifact | None:
    if isinstance(raw, PresentationArtifact):
        return raw
    if not isinstance(raw, dict):
        return None

    data = _payload(raw)
    raw_type = _text(raw.get("type") or raw.get("artifact_type")) or "ARTIFACT"
    artifact_type = _TYPE_ALIASES.get(raw_type.upper(), raw_type.upper())
    title = _text(raw.get("title") or data.get("title")) or None
    content = _text(
        raw.get("content")
        or data.get("content")
        or data.get("body")
        or data.get("body_markdown")
    ) or None
    summary = _text(raw.get("summary") or data.get("summary")) or None
    publish_time = _text(
        raw.get("publish_time")
        or data.get("publish_time")
        or raw.get("run_at")
        or data.get("run_at")
        or data.get("publish_at")
    ) or None
    run_at = publish_time
    timezone = _text(raw.get("timezone") or data.get("timezone")) or None
    status = _text(raw.get("status") or data.get("status")) or None
    resource_id = _text(
        raw.get("resource_id")
        or data.get("resource_id")
        or data.get("draft_id")
        or data.get("schedule_id")
        or data.get("post_id")
    ) or None
    artifact_id = _text(raw.get("artifact_id"))

    return PresentationArtifact(
        type=artifact_type,
        artifact_id=artifact_id,
        title=title,
        content=content,
        summary=summary,
        resource_id=resource_id,
        run_at=run_at,
        publish_time=publish_time,
        timezone=timezone,
        status=status,
        payload=data,
    )


def _status_text(status: str, *, scheduled: bool = False) -> str:
    if scheduled:
        return "等待发布"
    return {
        "COMPLETED": "已完成",
        "WAITING_APPROVAL": "等待审批",
        "WAITING_HUMAN": "等待人工处理",
        "PAUSED": "已暂停",
        "FAILED": "执行失败",
        "PARTIAL": "部分完成",
        "PARTIAL_FAILURE": "部分失败",
        "CANCELLED": "已取消",
    }.get(status, status or "未知")


def _user_facing_error(result: RuntimeResult) -> str:
    messages = {
        "TIMEOUT": "内容生成等待时间过长，草稿尚未完成。",
        "CREATOR_UNAVAILABLE": "创作服务暂时不可用，可以稍后重试。",
        "TOOL_EXECUTION_FAILED": "内容生成服务没有正常返回结果。",
        "TOOL_ARGUMENT_VALIDATION_FAILED": "文章信息不完整，暂时无法生成草稿。",
    }
    return messages.get(
        _text(result.error_code).upper(),
        _text(result.error_message or result.error or result.content)
        or "任务执行未完成，请稍后重试。",
    )


class ExecutionResultPresenter:
    """Pure ``RuntimeResult -> AssistantResponse`` conversion."""

    def present(self, result: RuntimeResult) -> AssistantResponse:
        status = _text(result.status).upper()
        raw_artifacts: list[Any] = list(result.artifacts)
        has_schedule_artifact = any(
            isinstance(item, dict)
            and _text(item.get("type") or item.get("artifact_type")).upper()
            in {"SCHEDULE", "PUBLICATION_SCHEDULE"}
            for item in raw_artifacts
        )
        if result.schedule and not has_schedule_artifact:
            raw_artifacts.append({"type": "PUBLICATION_SCHEDULE", **result.schedule})
        artifacts = [
            artifact
            for artifact in (_normalise_artifact(item) for item in raw_artifacts)
            if artifact is not None
        ]

        has_approval = bool(result.approval_id or result.approval_data or result.approval)
        if status in _IN_PROGRESS_STATUSES and not result.success:
            return self._in_progress(result, status, artifacts)
        if not result.success and status not in _WAITING_STATUSES and not has_approval:
            return self._failure(result, status, artifacts)
        if status in _WAITING_STATUSES or has_approval:
            return self._waiting(result, status, artifacts)
        return self._success(result, status, artifacts)

    @staticmethod
    def _projection_meta(result: RuntimeResult) -> dict[str, Any]:
        projected_steps: list[dict[str, Any]] = []
        for step in result.steps:
            capability = _text(step.get("capability") or step.get("step_id"))
            projected_steps.append({
                "step_id": _text(step.get("step_id")),
                "label": _STEP_LABELS.get(capability.upper(), capability),
                "status": _text(step.get("status")).upper() or "PENDING",
                "error": _text(step.get("error_message") or step.get("error")) or None,
            })
        total = len(projected_steps)
        completed = sum(item["status"] == "COMPLETED" for item in projected_steps)
        current = next(
            (
                item["label"]
                for item in projected_steps
                if item["status"] == "RUNNING"
            ),
            next(
                (
                    item["label"]
                    for item in projected_steps
                    if item["status"] == "PENDING"
                ),
                None,
            ),
        )
        return {
            "task_name": _text(result.summary or result.content)[:120],
            "steps": projected_steps,
            "current_step": current,
            "completed_steps": completed,
            "total_steps": total,
            "progress": completed / total if total else 0.0,
        }

    def _in_progress(
        self,
        result: RuntimeResult,
        status: str,
        artifacts: list[PresentationArtifact],
    ) -> AssistantResponse:
        """Present an accepted execution without inventing completion.

        A detached Runtime returns before the first long-running tool has
        finished.  Keeping that state explicit prevents the old generic
        ``已完成`` sentence from being shown while the execution is still
        RUNNING.
        """
        capabilities = {
            _text(step.get("capability")).upper()
            for step in result.steps
            if isinstance(step, dict)
        }
        if "GENERATE_CONTENT" in capabilities:
            message = "正在创作内容，完成后自动进行质量检查并安排发布时间。"
        else:
            message = "正在分析需求，随后开始执行。"
        return AssistantResponse(
            message=message,
            artifacts=artifacts,
            next_actions=["查看执行进度"],
            execution_id=result.execution_id,
            status=status or "RUNNING",
            **self._projection_meta(result),
        )

    def _failure(
        self,
        result: RuntimeResult,
        status: str,
        artifacts: list[PresentationArtifact],
    ) -> AssistantResponse:
        failure = result.failure_state or {}
        if not failure and result.steps:
            failure = next(
                (
                    step for step in result.steps
                    if _text(step.get("status")).upper() in {"FAILED", "FAILED_RETRYABLE"}
                ),
                {},
            )
        capability = _text(failure.get("capability") or failure.get("step_id"))
        lines = ["执行失败。"]
        if capability:
            lines.append(
                f"失败步骤：{_STEP_LABELS.get(capability.upper(), capability)}"
            )
        reason = _user_facing_error(result)
        lines.append(f"原因：{reason}")
        return AssistantResponse(
            message="\n".join(lines),
            artifacts=artifacts,
            next_actions=["重试执行"],
            execution_id=result.execution_id,
            status=status or "FAILED",
            error=reason,
            error_code=result.error_code or None,
            retry_available=True,
            approval_required=False,
            approval_id=result.approval_id,
            **self._projection_meta(result),
        )

    def _waiting(
        self,
        result: RuntimeResult,
        status: str,
        artifacts: list[PresentationArtifact],
    ) -> AssistantResponse:
        approval_payload = result.approval_data or result.approval or {}
        operation = _text(approval_payload.get("operation"))
        detail = f"（{operation}）" if operation else ""
        message = f"任务已暂停，等待你的确认{detail}。"
        return AssistantResponse(
            message=message,
            artifacts=artifacts,
            next_actions=["approve", "modify"],
            execution_id=result.execution_id,
            status=status or "WAITING_APPROVAL",
            approval_required=True,
            approval_id=result.approval_id or _text(approval_payload.get("approval_id")) or None,
            **self._projection_meta(result),
        )

    def _success(
        self,
        result: RuntimeResult,
        status: str,
        artifacts: list[PresentationArtifact],
    ) -> AssistantResponse:
        drafts = [item for item in artifacts if item.type == "POST_DRAFT"]
        schedules = [item for item in artifacts if item.type == "PUBLICATION_SCHEDULE"]
        analyses = [
            item for item in artifacts
            if item.type in {"ANALYSIS_REPORT", "OPERATION_PLAN", "VALIDATION_REPORT"}
        ]

        sections: list[str] = []
        next_actions: list[str] = []
        if analyses:
            sections.append(self._render_analysis(result, status, analyses))
            next_actions.append("查看分析结果")
        if drafts or schedules:
            sections.append(self._render_publish(result, status, drafts, schedules))
            next_actions.extend(["查看草稿", "修改草稿"])
            if not schedules:
                next_actions.append("安排发布时间")

        if sections:
            message = "\n\n".join(sections)
        else:
            fallback = _text(result.content)
            # Runtime's old generic sentence is an implementation detail.  A
            # presenter must not make it the only user-facing explanation.
            if not fallback or fallback.startswith(("已完成：", "已完成:", "执行已完成")):
                fallback = "执行已完成。"
            message = fallback
            next_actions = ["查看执行详情"] if result.execution_id else []

        return AssistantResponse(
            message=message,
            artifacts=artifacts,
            next_actions=next_actions,
            execution_id=result.execution_id,
            status=status or "COMPLETED",
            **self._projection_meta(result),
        )

    def _render_publish(
        self,
        result: RuntimeResult,
        status: str,
        drafts: list[PresentationArtifact],
        schedules: list[PresentationArtifact],
    ) -> str:
        lines = [
            "已为你创建发布任务。" if schedules else "已为你生成帖子草稿。",
            "",
            "草稿：",
        ]
        draft = drafts[0] if drafts else None
        if draft is not None:
            lines.append(f"标题：{draft.title or draft.summary or '未命名草稿'}")
            summary = _compact(draft.content or draft.summary)
            if summary:
                lines.append(f"内容摘要：{summary}")
            if draft.resource_id:
                lines.append(f"草稿 ID：{draft.resource_id}")
        else:
            lines.append("标题：未命名草稿")

        if schedules:
            schedule = schedules[0]
            run_at = schedule.run_at or ""
            timezone = schedule.timezone or "Asia/Shanghai"
            local_time = format_local_schedule_time(run_at, timezone) if run_at else "待确定"
            lines.extend([
                "",
                f"发布时间：{local_time}",
                f"状态：{_status_text(status, scheduled=True)}",
            ])
            if schedule.resource_id:
                lines.append(f"定时任务 ID：{schedule.resource_id}")
        else:
            lines.append(f"状态：{_status_text(status)}")
        return "\n".join(lines)

    def _render_analysis(
        self,
        result: RuntimeResult,
        status: str,
        artifacts: list[PresentationArtifact],
    ) -> str:
        lines = ["分析与运营计划已生成。", ""]
        for artifact in artifacts:
            label = {
                "ANALYSIS_REPORT": "分析结果",
                "OPERATION_PLAN": "运营计划",
                "VALIDATION_REPORT": "校验结果",
            }.get(artifact.type, artifact.type)
            lines.append(f"{label}：")
            payload = artifact.payload
            summary = _compact(artifact.summary or payload.get("summary"))
            if summary:
                lines.append(summary)
            for key, title in (
                ("growth_directions", "增长方向"),
                ("growth_topics", "增长方向"),
                ("recall_plan", "召回方案"),
                ("weekly_plan", "未来一周运营计划"),
                ("operation_plan", "运营计划"),
            ):
                value = payload.get(key)
                if value:
                    lines.append(f"{title}：{_format_section_value(value)}")
            if artifact.content and artifact.content != artifact.summary:
                lines.append(_compact(artifact.content, 600))
            lines.append("")
        lines.append(f"状态：{_status_text(status)}")
        return "\n".join(lines).strip()


def present_execution_result(result: RuntimeResult) -> AssistantResponse:
    """Convenience function for route/tests that do not own a presenter."""

    return ExecutionResultPresenter().present(result)


# Short alias for callers that use the product-layer name from the design
# document.
ExecutionPresenter = ExecutionResultPresenter


__all__ = [
    "AssistantResponse",
    "ExecutionResultPresenter",
    "ExecutionPresenter",
    "PresentationArtifact",
    "present_execution_result",
]
