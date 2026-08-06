from __future__ import annotations

from greenbook_mcp_server.tools.content import normalize_text_artifact


def test_text_artifact_mapping_excludes_media_and_local_paths() -> None:
    request = normalize_text_artifact(
        {
            "title": "Java 并发指南",
            "description": "摘要",
            "body_markdown": "# 正文",
        },
        fallback_title="fallback",
        fallback_content="fallback body",
    )

    assert request is not None
    payload = request.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert payload == {
        "title": "Java 并发指南",
        "content": "# 正文",
        "summary": "摘要",
    }
    assert not any(
        key in payload
        for key in ("imgUrls", "attachments", "cover", "contentUrl", "localFiles")
    )
