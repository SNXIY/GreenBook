from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, Iterable


class ArtifactKind(StrEnum):
    TOOL_RESULT = "TOOL_RESULT"
    POST_SEARCH_RESULTS = "POST_SEARCH_RESULTS"
    POST_CONTENT = "POST_CONTENT"
    POST_SUMMARY = "POST_SUMMARY"
    ENGAGEMENT_ANALYSIS = "ENGAGEMENT_ANALYSIS"
    USER_SET = "USER_SET"
    POST_COLLECTION = "POST_COLLECTION"
    TOPIC_ANALYSIS = "TOPIC_ANALYSIS"
    OWNED_POST_SET = "OWNED_POST_SET"
    CONTENT_DRAFT = "CONTENT_DRAFT"
    DELETION_RECEIPT = "DELETION_RECEIPT"
    SCHEDULE_RECEIPT = "SCHEDULE_RECEIPT"
    PUBLICATION_RECEIPT = "PUBLICATION_RECEIPT"
    COMMENT_RECEIPT = "COMMENT_RECEIPT"
    MCP_RESULT = "MCP_RESULT"


@dataclass(frozen=True)
class ArtifactBinding:
    argument: str
    accepts: frozenset[str]
    resolver: str
    validation_example: Any
    required: bool = True
    allow_planner_value: bool = False
    target_role: str | None = None


class ArtifactBinder:
    """Resolve typed tool arguments from immutable upstream artifacts."""

    def bind(
        self,
        *,
        bindings: Iterable[ArtifactBinding],
        arguments: dict[str, Any],
        artifacts: list[dict[str, Any]],
        binding_sources: dict[str, list[str]] | None = None,
        resolved_targets: dict[str, dict[str, Any] | Any] | None = None,
        required_target_roles: frozenset[str] = frozenset(),
        optional_target_roles: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        resolved = dict(arguments)
        targets = resolved_targets or {}
        missing_roles = set(required_target_roles) - set(targets)
        if missing_roles:
            raise ValueError(
                "Missing required target roles: " + ", ".join(sorted(missing_roles))
            )
        sources = binding_sources or {}
        for binding in bindings:
            allowed_tasks = set(sources.get(binding.argument, []))
            has_declared_sources = (
                binding_sources is not None
                and binding.argument in binding_sources
            )
            candidates = [
                item
                for item in artifacts
                if str(item.get("artifact_type") or "") in binding.accepts
                and isinstance(item.get("result"), dict)
                and (
                    not has_declared_sources
                    or str(item.get("task_id") or "") in allowed_tasks
                )
            ]
            target_resolver = binding.resolver.startswith("target_")
            # A target-bound argument may consume a newer artifact only when
            # the compiled DAG explicitly named its producer.  This preserves
            # the selected cross-run target while allowing a verified read or
            # revision in the current run to advance that target's version.
            if target_resolver and not has_declared_sources:
                candidates = []
            if not candidates and binding.required and not target_resolver:
                raise ValueError(
                    f"参数 {binding.argument} 缺少 Artifact："
                    f"{sorted(binding.accepts)}"
                )
            try:
                value = self._resolve(
                    binding.resolver,
                    candidates,
                    resolved_targets=targets,
                    target_role=binding.target_role,
                )
            except ValueError:
                # Optional target-bound arguments model an optional mode of a
                # tool, not an additional mandatory target type. For example,
                # publication.update_schedule always needs a SCHEDULE, while
                # draft_id/content_sha256 are needed only when replacing the
                # scheduled draft at the same time. A schedule-only update must
                # therefore skip those DRAFT bindings instead of failing type
                # validation.
                if target_resolver and not binding.required:
                    continue
                raise
            if value is None:
                if binding.required:
                    raise ValueError(
                        f"Artifact 无法生成必需参数 {binding.argument}"
                    )
                continue
            resolved[binding.argument] = value
        return resolved

    def _resolve(
        self,
        resolver: str,
        candidates: list[dict[str, Any]],
        *,
        resolved_targets: dict[str, dict[str, Any] | Any],
        target_role: str | None = None,
    ) -> Any:
        if resolver == "user_ids":
            return self._unique_values(candidates, "users", ("user_id", "userId"))
        if resolver == "owned_post_ids":
            if any(bool(item["result"].get("truncated")) for item in candidates):
                raise ValueError("资源清单不完整，拒绝绑定批量删除参数")
            return self._unique_values(candidates, "posts", ("id", "post_id"))
        if resolver == "target_draft_id":
            artifact_value = self._latest_field(
                candidates,
                ("draft_id", "draftId"),
            )
            if artifact_value not in {None, ""}:
                return str(artifact_value)
            return self._target_value(resolved_targets, role=target_role or "CONTENT", field="target_id")
        if resolver == "target_content_sha256":
            artifact_value = self._latest_field(
                candidates,
                ("content_sha256", "contentSha256"),
            )
            if artifact_value not in {None, ""}:
                return str(artifact_value).lower()
            value = self._target_value(
                resolved_targets,
                role=target_role or "CONTENT",
                field="content_sha256",
            )
            return str(value).lower() if value else None
        if resolver == "target_schedule_action_id":
            artifact_value = self._latest_field(
                candidates,
                ("action_id", "actionId", "schedule_id", "scheduleId"),
            )
            if artifact_value not in {None, ""}:
                return str(artifact_value)
            return self._target_value(
                resolved_targets,
                role=target_role or "SCHEDULE",
                field="target_id",
            )
        if resolver == "draft_items":
            items: list[dict[str, str]] = []
            for candidate in candidates:
                result = candidate["result"]
                draft_id = result.get("draft_id") or result.get("draftId")
                sha = result.get("content_sha256") or result.get("contentSha256")
                if draft_id and sha:
                    items.append(
                        {
                            "draft_id": str(draft_id),
                            "expected_content_sha256": str(sha).lower(),
                        }
                    )
            deduplicated = {item["draft_id"]: item for item in items}
            return list(deduplicated.values()) or None
        if resolver == "creator_references":
            return self._creator_references(candidates)
        if resolver == "summary_reply":
            summary = self._latest_field(candidates, ("summary",))
            return str(summary).strip() if summary else None
        raise ValueError(f"Unknown Artifact binding resolver: {resolver}")

    @staticmethod
    def _target_value(
        resolved_targets: dict[str, dict[str, Any] | Any],
        *,
        role: str,
        field: str,
    ) -> Any:
        target = resolved_targets.get(role)
        if target is None:
            raise ValueError(f"Missing resolved target role: {role}")
        if hasattr(target, "model_dump"):
            target = target.model_dump(mode="json")
        value = target.get(field)
        if value in {None, ""}:
            raise ValueError("无法确定当前操作目标：TargetBinding 缺少必要字段")
        return value

    @staticmethod
    def _latest_field(
        candidates: list[dict[str, Any]],
        aliases: tuple[str, ...],
    ) -> Any:
        for candidate in reversed(candidates):
            result = candidate["result"]
            for alias in aliases:
                if result.get(alias) not in {None, ""}:
                    return result[alias]
        return None

    @staticmethod
    def _unique_values(
        candidates: list[dict[str, Any]],
        collection: str,
        aliases: tuple[str, ...],
    ) -> list[str] | None:
        values: list[str] = []
        for candidate in candidates:
            for item in list(candidate["result"].get(collection) or []):
                if not isinstance(item, dict):
                    continue
                value = next((item.get(alias) for alias in aliases if item.get(alias)), None)
                if value is not None:
                    values.append(str(value))
        unique = list(dict.fromkeys(values))
        return unique or None

    @staticmethod
    def _creator_references(
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        for candidate in candidates:
            kind = str(candidate.get("artifact_type") or "")
            result = candidate["result"]
            if kind == ArtifactKind.POST_SEARCH_RESULTS:
                references.extend(
                    item for item in list(result.get("results") or [])
                    if isinstance(item, dict)
                )
            elif kind == ArtifactKind.POST_CONTENT:
                references.append(dict(result))
            elif kind == ArtifactKind.POST_SUMMARY:
                references.append(
                    {
                        "id": result.get("post_id"),
                        "title": result.get("title"),
                        "description": result.get("summary"),
                        "content_sha256": result.get("source_content_sha256"),
                    }
                )
            elif kind == ArtifactKind.POST_COLLECTION:
                references.extend(
                    {
                        "id": entry.get("post_id") or entry.get("id"),
                        "title": entry.get("title") or "社区帖子",
                        "description": entry.get("description"),
                        "tags": list(entry.get("tags") or []),
                        "type": entry.get("type"),
                        "author_id": entry.get("author_id"),
                    }
                    for entry in list(result.get("posts") or [])
                    if isinstance(entry, dict)
                    and (entry.get("post_id") or entry.get("id"))
                )
            elif kind in {
                ArtifactKind.ENGAGEMENT_ANALYSIS,
                ArtifactKind.TOPIC_ANALYSIS,
                ArtifactKind.USER_SET,
            }:
                references.append(
                    {
                        "id": f"artifact:{candidate.get('task_id') or kind}",
                        "title": {
                            ArtifactKind.ENGAGEMENT_ANALYSIS: "社区活跃度分析",
                            ArtifactKind.TOPIC_ANALYSIS: "社区主题分析",
                            ArtifactKind.USER_SET: "社区用户分析",
                        }[kind],
                        "description": "当前任务中由社区真实数据生成的结构化分析产物。",
                        "body_markdown": json.dumps(result, ensure_ascii=False),
                    }
                )
            elif kind == ArtifactKind.CONTENT_DRAFT:
                references.append(
                    {
                        "id": result.get("draft_id") or result.get("draftId"),
                        "title": result.get("title") or "社区草稿",
                        "description": result.get("description"),
                        "body_markdown": result.get("body_markdown")
                        or result.get("bodyMarkdown"),
                        "tags": list(result.get("tags") or []),
                        "content_sha256": result.get("content_sha256")
                        or result.get("contentSha256"),
                    }
                )
        return references[-10:]


artifact_binder = ArtifactBinder()
