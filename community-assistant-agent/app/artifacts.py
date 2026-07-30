from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import Artifact, Approval, Run, RunStep


def content_hash(content: dict[str, Any]) -> str:
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def artifact_type_for_tool(tool_name: str | None) -> str:
    if not tool_name:
        return "HARNESS_OUTPUT"
    if tool_name == "creator.create_draft":
        return "CONTENT_DRAFT"
    if tool_name.startswith("community.search"):
        return "SEARCH_EVIDENCE"
    if tool_name.startswith("community.analyze"):
        return "ANALYSIS_REPORT"
    if tool_name.startswith("community.summarize"):
        return "CONTENT_SUMMARY"
    if tool_name.startswith("moderation."):
        return "MODERATION_RESULT"
    if tool_name.startswith("publication."):
        return "PUBLICATION_RECEIPT"
    if tool_name.startswith("mcp."):
        return "MCP_RESULT"
    return "TOOL_RESULT"


async def publish_step_artifact(
    session: AsyncSession,
    *,
    step: RunStep,
    output: dict[str, Any],
) -> Artifact:
    existing = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == step.run_id,
            Artifact.step_id == step.id,
            Artifact.content_hash == content_hash(output),
        )
    )
    if existing is not None:
        return existing
    parent_ids: list[str] = []
    if step.depends_on:
        candidates = list(
            (
                await session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.run_id == step.run_id,
                        Artifact.task_key.in_(list(step.depends_on)),
                    )
                    .order_by(Artifact.task_key, Artifact.version.desc())
                )
            ).all()
        )
        latest_by_task: dict[str, Artifact] = {}
        for candidate in candidates:
            latest_by_task.setdefault(candidate.task_key, candidate)
        parent_ids = [
            latest_by_task[task_id].id
            for task_id in step.depends_on
            if task_id in latest_by_task
        ]
    current_version = await session.scalar(
        select(func.max(Artifact.version)).where(
            Artifact.run_id == step.run_id,
            Artifact.task_key == (step.task_key or f"step-{step.ordinal}"),
        )
    )
    artifact = Artifact(
        run_id=step.run_id,
        step_id=step.id,
        task_key=step.task_key or f"step-{step.ordinal}",
        agent_name=step.agent_name or "Harness",
        artifact_type=artifact_type_for_tool(step.tool_name),
        parent_artifact_ids=parent_ids,
        version=int(current_version or 0) + 1,
        content=output,
        content_hash=content_hash(output),
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def publish_final_artifact(
    session: AsyncSession,
    *,
    run: Run,
    final_response: str,
) -> Artifact:
    content = {"response": final_response}
    existing = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.task_key == "__final__",
            Artifact.content_hash == content_hash(content),
        )
    )
    if existing is not None:
        return existing
    candidates = list(
        (
            await session.scalars(
                select(Artifact)
                .where(
                    Artifact.run_id == run.id,
                    Artifact.task_key != "__final__",
                )
                .order_by(Artifact.task_key, Artifact.version.desc())
            )
        ).all()
    )
    latest_by_task: dict[str, Artifact] = {}
    for candidate in candidates:
        latest_by_task.setdefault(candidate.task_key, candidate)
    parent_ids = [
        item.id
        for item in sorted(
            latest_by_task.values(),
            key=lambda value: (value.created_at, value.id),
        )
    ]
    artifact = Artifact(
        run_id=run.id,
        step_id=None,
        task_key="__final__",
        agent_name="Supervisor",
        artifact_type="FINAL_RESPONSE",
        parent_artifact_ids=parent_ids,
        version=1,
        content=content,
        content_hash=content_hash(content),
    )
    session.add(artifact)
    await session.flush()
    return artifact


async def blackboard_snapshot(
    session: AsyncSession,
    *,
    run_id: str,
) -> dict[str, Any]:
    run = await session.get(Run, run_id)
    if run is None:
        raise ValueError("任务不存在")
    steps = list(
        (
            await session.scalars(
                select(RunStep)
                .where(RunStep.run_id == run_id)
                .order_by(RunStep.ordinal)
            )
        ).all()
    )
    artifacts = list(
        (
            await session.scalars(
                select(Artifact)
                .where(Artifact.run_id == run_id)
                .order_by(Artifact.created_at, Artifact.id)
            )
        ).all()
    )
    approvals = list(
        (
            await session.scalars(
                select(Approval)
                .where(Approval.run_id == run_id)
                .order_by(Approval.created_at)
            )
        ).all()
    )
    return {
        "run_id": run.id,
        "goal": run.prompt,
        "status": run.status,
        "intent": run.intent_detail,
        "task_ledger": dict(run.task_ledger or {}),
        "progress_ledger": dict(run.progress_ledger or {}),
        "checkpoint": dict(run.checkpoint or {}),
        "tasks": [
            {
                "task_id": step.task_key,
                "ordinal": step.ordinal,
                "agent": step.agent_name,
                "tool": step.tool_name,
                "status": step.status,
                "depends_on": list(step.depends_on or []),
                "attempts": step.attempts,
                "error": step.error,
            }
            for step in steps
        ],
        "artifacts": [
            {
                "artifact_id": item.id,
                "task_id": item.task_key,
                "agent": item.agent_name,
                "type": item.artifact_type,
                "version": item.version,
                "parent_artifact_ids": list(item.parent_artifact_ids or []),
                "content_hash": item.content_hash,
                "created_at": item.created_at.isoformat(),
            }
            for item in artifacts
        ],
        "approvals": [
            {
                "approval_id": item.id,
                "action": item.action,
                "status": item.status,
                "input_hash": item.input_hash,
                "expires_at": item.expires_at.isoformat(),
            }
            for item in approvals
        ],
    }
