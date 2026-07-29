from datetime import datetime, timezone
import hashlib

from app.creator.publication.service import _extract_final_document
from app.creator.runtime.models import ArtifactKind, CreatorArtifact


def _final_artifact(document: dict[str, object]) -> CreatorArtifact:
    return CreatorArtifact(
        id="artifact-final",
        tenant_id="tenant",
        creator_id="1",
        task_id="task",
        run_id="run",
        step_id="runtime:finalize",
        kind=ArtifactKind.FINAL_CONTENT,
        producer="CreatorSupervisorAgent",
        revision=1,
        content={"document": document},
        content_sha256="0" * 64,
        created_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )


def test_extract_final_document_removes_duplicate_markdown_title() -> None:
    artifact = _final_artifact(
        {
            "title": "Agent Harness 到底解决什么",
            "body_markdown": (
                "# Agent Harness 到底解决什么\n\n"
                "后端工程师第一次搭 Agent，通常会从 Prompt 开始。\n\n"
                "## 为什么离生产还很远\n\n正文。"
            ),
        }
    )

    title, body, description, digest = _extract_final_document(artifact)

    assert title == "Agent Harness 到底解决什么"
    assert body.startswith("后端工程师第一次搭 Agent")
    assert not body.startswith("# Agent Harness")
    assert description == "后端工程师第一次搭 Agent，通常会从 Prompt 开始。"
    assert digest == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_extract_final_document_prefers_explicit_summary() -> None:
    artifact = _final_artifact(
        {
            "title": "# 可发布标题",
            "body_markdown": "可发布标题\n\n这是正文。",
            "summary": "这是经过确认的摘要。",
        }
    )

    title, body, description, _ = _extract_final_document(artifact)

    assert title == "可发布标题"
    assert body == "这是正文。"
    assert description == "这是经过确认的摘要。"
