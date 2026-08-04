"""Operation-aware target resolution for multi-object conversations.

Two layers:
- ``resolve_target``: pick which Task/Goal the turn addresses (Layer A)
- ``resolve``: pick draft/schedule/post inside an already-bound Goal (Layer B)
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.conversation_workspace import ConversationWorkspace, WorkspaceEntity
from app.domain import (
    ConversationGoal,
    IntentDelta,
    PendingClarification,
    TargetBinding,
    TargetCandidate,
    TargetContext,
)
from app.goal_resolver import GoalResolver


class TargetResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected: TargetCandidate | None = None
    candidates: list[TargetCandidate] = Field(default_factory=list, max_length=8)
    clarification: PendingClarification | None = None
    error: str | None = None


ReferenceKind = Literal[
    "DIRECT_REFERENCE",
    "TEMPORAL_REFERENCE",
    "ORDINAL_REFERENCE",
    "SEMANTIC_REFERENCE",
]

ResolutionMethod = Literal[
    "EXPLICIT_REFERENCE",
    "ACTIVE_TASK",
    "INDEX_REFERENCE",
    "SEMANTIC_MATCH",
    "AMBIGUOUS",
]

EntityTargetType = Literal["TASK", "GOAL", "ARTIFACT", "SCHEDULE", "POST"]


class EntityTargetCandidate(BaseModel):
    """Task-level candidate with full handle hierarchy for two-phase resolution."""

    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(min_length=1, max_length=128)
    target_type: EntityTargetType
    task_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    artifact_id: str | None = Field(default=None, max_length=128)
    schedule_id: str | None = Field(default=None, max_length=128)
    label: str | None = Field(default=None, max_length=240)
    labels: list[str] = Field(default_factory=list, max_length=32)
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=500)


class EntityTargetResolution(BaseModel):
    """Layer-A result: which Task/Goal (and optional nested handles)."""

    model_config = ConfigDict(extra="forbid")

    target_id: str | None = None
    target_type: EntityTargetType | None = None
    task_id: str | None = None
    goal_id: str | None = None
    artifact_id: str | None = None
    schedule_id: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    resolution_method: ResolutionMethod
    candidates: list[EntityTargetCandidate] = Field(default_factory=list, max_length=8)
    clarification: PendingClarification | None = None
    reference_kinds: list[ReferenceKind] = Field(default_factory=list, max_length=8)


@dataclass(frozen=True)
class ReferenceAnalysis:
    """Structured deixis / anchor analysis — not keyword if-ladders for ACTIVE_TASK."""

    kinds: frozenset[ReferenceKind]
    ordinal: int | None
    has_discriminative_anchor: bool
    anchor_text: str
    deixis_strength: Literal["NONE", "WEAK", "STRONG"] = "NONE"


class TaskLike(Protocol):
    task_id: str
    artifact_id: str | None
    schedule_id: str | None
    summary: str | None


# Feature extractors return reference *kinds*. ACTIVE_TASK is gated by
# ReferenceAnalysis, never by ad-hoc business substring checks in callers.
_STRONG_TEMPORAL_DEIXIS = re.compile(
    r"(?:刚才|剛剛|刚刚|上次|上一轮|上一輪|上一个|上一個)",
    re.IGNORECASE,
)
_WEAK_TEMPORAL_DEIXIS = re.compile(
    r"(?:之前|先前|前面)",
    re.IGNORECASE,
)
_TEMPORAL_DEIXIS = re.compile(
    r"(?:刚才|剛剛|刚刚|上次|上一轮|上一輪|上一个|上一個|之前|先前|前面|刚|剛)",
    re.IGNORECASE,
)
# Proximal demonstratives bind the active focus; distal ones stay weak.
_STRONG_DIRECT_DEIXIS = re.compile(
    r"(?:这个|這個|这篇|這篇|这份|這份)",
    re.IGNORECASE,
)
_WEAK_DIRECT_DEIXIS = re.compile(
    r"(?:那个|那個|那篇|该|該)",
    re.IGNORECASE,
)
_DIRECT_DEIXIS = re.compile(
    r"(?:这个|這個|这篇|這篇|那个|那個|那篇|这份|這份|该|該)",
    re.IGNORECASE,
)
_ORDINAL = re.compile(
    r"(?:第\s*)([一二两三四五六七八九十\d]+)\s*(?:个|個|项|項|篇|条|條)?",
    re.IGNORECASE,
)
_GENERIC_ENTITY = re.compile(
    r"(?:帖子|帖|文章|草稿|任务|任務|内容|內容|它)",
    re.IGNORECASE,
)
_MUTATION_NOISE = re.compile(
    r"(?:修改|改一下|改成|改为|改為|调整|調整|更新|替换|替換|重写|重寫|"
    r"追加|加入|加上|增加|补充|補充|发布|發佈|时间|時間|定时|定時|"
    r"分钟|分鐘|之后|之後|推迟|延后|延後|延迟|延遲|提前|请|請|把|将|將|"
    r"创建|對應|对应|一下|改一改)",
    re.IGNORECASE,
)
_SCHEDULE_FILLER = re.compile(
    r"(?:[一二三四五六七八九十\d]+\s*(?:分钟|分鐘|小时|小時|点|點)|"
    r"晚上|早上|上午|下午|今晚|明天|后天|後天)",
    re.IGNORECASE,
)
_TYPED_ID = re.compile(
    r"\b(?:draft|post|schedule|artifact|goal):([a-z0-9_-]+)\b",
    re.IGNORECASE,
)


class TargetResolver:
    """Resolve targets at Task scope (Layer A) and entity scope (Layer B)."""

    CONFIDENCE_THRESHOLD = 0.62
    CLOSE_SCORE_GAP = 0.12
    EXPLICIT_TITLE_MIN_CHARS = 4
    EXPLICIT_TITLE_THRESHOLD = 0.82
    SEMANTIC_THRESHOLD = 0.58
    SEMANTIC_GAP = 0.10

    def resolve(
        self,
        *,
        message: str,
        intent_delta: IntentDelta,
        goal: ConversationGoal,
        workspace: ConversationWorkspace,
        artifacts: Iterable[dict[str, Any]] = (),
        target_history: Iterable[TargetBinding] = (),
    ) -> TargetResolution:
        operation = intent_delta.operation
        if operation == "CREATE_POST":
            return TargetResolution()

        context = self._target_context(goal=goal, workspace=workspace)
        candidates = self._collect_candidates(
            message=message,
            operation=operation,
            context=context,
            workspace=workspace,
            artifacts=artifacts,
            target_history=target_history,
        )
        explicit_ref = self._explicit_reference(message)
        if explicit_ref:
            explicit = [
                item
                for item in candidates
                if item.target_id.lower() == explicit_ref.lower()
            ]
            if not explicit:
                return TargetResolution(
                    error=(
                        "The explicitly referenced target is not available "
                        "in this conversation."
                    )
                )
            return TargetResolution(selected=explicit[0], candidates=candidates[:8])

        if not candidates:
            return TargetResolution()

        if operation == "PUBLISH_NOW":
            schedules = [item for item in candidates if item.type == "SCHEDULE"]
            if schedules:
                candidates = schedules

        ranked = sorted(candidates, key=lambda item: item.score, reverse=True)[:8]
        top = ranked[0]
        same_type = [item for item in ranked if item.type == top.type]
        close = len(same_type) > 1 and (
            top.score - same_type[1].score < self.CLOSE_SCORE_GAP
        )
        explicitly_ambiguous = (
            len(
                [
                    item
                    for item in same_type
                    if item.label and self._explicit_label_overlap(message, item.label)
                ]
            )
            > 1
        )
        ambiguous = (
            top.score < self.CONFIDENCE_THRESHOLD or close or explicitly_ambiguous
        )
        if ambiguous:
            candidates_for_question = [item for item in ranked if item.type == top.type]
            return TargetResolution(
                candidates=candidates_for_question,
                clarification=PendingClarification(
                    question="Which target should continue this operation?",
                    candidates=candidates_for_question,
                    delta_id=intent_delta.delta_id,
                ),
            )
        return TargetResolution(selected=top, candidates=ranked)

    def resolve_target(
        self,
        *,
        message: str,
        active_task: TaskLike | None,
        active_tasks: Sequence[TaskLike],
        goals: Sequence[ConversationGoal],
        conversation_context: dict[str, Any] | None = None,
        candidate_targets: Sequence[WorkspaceEntity] | None = None,
        artifacts: Iterable[dict[str, Any]] = (),
        schedules: Iterable[dict[str, Any]] = (),
    ) -> EntityTargetResolution:
        """Layer A: resolve which Task/Goal the turn addresses."""

        del conversation_context, schedules
        candidates = self.build_entity_candidates(
            active_tasks=active_tasks,
            goals=goals,
            candidate_targets=candidate_targets,
            artifacts=artifacts,
        )
        analysis = self.analyze_reference(message)
        kinds = sorted(analysis.kinds)

        if not candidates:
            return EntityTargetResolution(
                resolution_method="AMBIGUOUS",
                confidence=0.0,
                candidates=[],
                reference_kinds=kinds,  # type: ignore[arg-type]
            )

        explicit = self._match_explicit(message, candidates)
        if explicit is not None:
            return self._hit(
                explicit,
                method="EXPLICIT_REFERENCE",
                confidence=explicit.score,
                candidates=candidates,
                kinds=kinds,
            )

        if "ORDINAL_REFERENCE" in analysis.kinds and analysis.ordinal is not None:
            # Ordinals refer to conversation chronology (第一篇 = earliest),
            # not the focus stack (where active/newest is first).
            goals_by_id = {goal.goal_id: goal for goal in goals}
            order_index = {
                task.task_id: index for index, task in enumerate(active_tasks)
            }
            index_rows = sorted(
                candidates,
                key=lambda item: (
                    (
                        goals_by_id[item.task_id].updated_at
                        if item.task_id in goals_by_id
                        and goals_by_id[item.task_id].updated_at is not None
                        else datetime.max.replace(tzinfo=timezone.utc)
                    ),
                    order_index.get(item.task_id, 10_000),
                    item.task_id,
                ),
            )
            index = analysis.ordinal - 1
            if 0 <= index < len(index_rows):
                selected = index_rows[index]
                return self._hit(
                    selected.model_copy(
                        update={
                            "score": 0.93,
                            "reason": f"index reference #{analysis.ordinal}",
                        }
                    ),
                    method="INDEX_REFERENCE",
                    confidence=0.93,
                    candidates=candidates,
                    kinds=kinds,
                )
            return self._ambiguous(candidates, kinds, confidence=0.4)

        if self._allows_active_task(analysis, task_count=len(candidates)):
            if active_task is not None:
                selected = next(
                    (
                        item
                        for item in candidates
                        if item.task_id == active_task.task_id
                    ),
                    None,
                )
                if selected is not None:
                    return self._hit(
                        selected.model_copy(
                            update={
                                "score": 0.9,
                                "reason": "active task via reference analysis",
                            }
                        ),
                        method="ACTIVE_TASK",
                        confidence=0.9,
                        candidates=candidates,
                        kinds=kinds,
                    )

        semantic = self._match_semantic(message, candidates, analysis)
        if semantic is not None:
            return self._hit(
                semantic,
                method="SEMANTIC_MATCH",
                confidence=semantic.score,
                candidates=candidates,
                kinds=kinds,
            )

        return self._ambiguous(candidates, kinds, confidence=0.45)

    def build_entity_candidates(
        self,
        *,
        active_tasks: Sequence[TaskLike],
        goals: Sequence[ConversationGoal],
        candidate_targets: Sequence[WorkspaceEntity] | None = None,
        artifacts: Iterable[dict[str, Any]] = (),
    ) -> list[EntityTargetCandidate]:
        """Build Task-level candidates preserving task/goal/artifact/schedule ids."""

        goals_by_id = {goal.goal_id: goal for goal in goals}
        ordered_ids: list[str] = []
        for task in active_tasks:
            if task.task_id not in ordered_ids:
                ordered_ids.append(task.task_id)
        for goal in goals:
            if goal.goal_id not in ordered_ids:
                ordered_ids.append(goal.goal_id)

        entities_by_goal: dict[str, list[WorkspaceEntity]] = {}
        for entity in candidate_targets or []:
            goal_id = str(getattr(entity, "goal_id", None) or "")
            if goal_id:
                entities_by_goal.setdefault(goal_id, []).append(entity)

        artifact_by_goal: dict[str, list[dict[str, Any]]] = {}
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            goal_id = str(artifact.get("goal_id") or "")
            if goal_id:
                artifact_by_goal.setdefault(goal_id, []).append(artifact)

        tasks_by_id = {task.task_id: task for task in active_tasks}
        rows: list[EntityTargetCandidate] = []
        for task_id in ordered_ids:
            goal = goals_by_id.get(task_id)
            task = tasks_by_id.get(task_id)
            if goal is None and task is None:
                continue
            context = goal.target_context if goal is not None else None
            content = context.content_target if context else None
            schedule = context.schedule_target if context else None
            publication = context.publication_target if context else None

            artifact_id = (
                (task.artifact_id if task else None)
                or (content.artifact_id if content else None)
                or (content.target_id if content else None)
            )
            schedule_id = (
                (task.schedule_id if task else None)
                or (schedule.target_id if schedule else None)
                or (content.schedule_id if content else None)
            )
            labels = self._labels_for_goal(
                goal, task, entities_by_goal.get(task_id, [])
            )
            for artifact in artifact_by_goal.get(task_id, []):
                title = str(
                    (artifact.get("content") or {}).get("title")
                    or artifact.get("title")
                    or ""
                ).strip()
                if title and title not in labels:
                    labels.append(title)

            label = labels[0] if labels else (task.summary if task else task_id)
            target_type: EntityTargetType = "TASK"
            if publication is not None and publication.target_id:
                target_type = "POST"
            elif schedule_id:
                target_type = "SCHEDULE"
            elif artifact_id:
                target_type = "ARTIFACT"

            rows.append(
                EntityTargetCandidate(
                    target_id=task_id,
                    target_type=target_type,
                    task_id=task_id,
                    goal_id=task_id,
                    artifact_id=artifact_id,
                    schedule_id=schedule_id,
                    label=label,
                    labels=labels[:32],
                    score=0.0,
                    reason="task candidate",
                )
            )
        return rows

    def analyze_reference(self, message: str) -> ReferenceAnalysis:
        """Classify reference kinds from structural features."""

        text = message or ""
        kinds: set[ReferenceKind] = set()
        ordinal = None
        ordinal_match = _ORDINAL.search(text)
        if ordinal_match:
            kinds.add("ORDINAL_REFERENCE")
            ordinal = self._parse_ordinal_token(ordinal_match.group(1))

        deixis_strength: Literal["NONE", "WEAK", "STRONG"] = "NONE"
        if _STRONG_TEMPORAL_DEIXIS.search(text):
            kinds.add("TEMPORAL_REFERENCE")
            deixis_strength = "STRONG"
        elif _WEAK_TEMPORAL_DEIXIS.search(text):
            kinds.add("TEMPORAL_REFERENCE")
            deixis_strength = "WEAK"
        if _STRONG_DIRECT_DEIXIS.search(text):
            kinds.add("DIRECT_REFERENCE")
            if deixis_strength != "STRONG":
                deixis_strength = "STRONG"
        elif _WEAK_DIRECT_DEIXIS.search(text):
            kinds.add("DIRECT_REFERENCE")
            if deixis_strength == "NONE":
                deixis_strength = "WEAK"
        elif _DIRECT_DEIXIS.search(text):
            kinds.add("DIRECT_REFERENCE")
            if deixis_strength == "NONE":
                deixis_strength = "WEAK"

        anchor = self._extract_anchor_text(text)
        has_discriminative = self._is_discriminative_anchor(anchor)
        if has_discriminative:
            kinds.add("SEMANTIC_REFERENCE")

        return ReferenceAnalysis(
            kinds=frozenset(kinds),
            ordinal=ordinal,
            has_discriminative_anchor=has_discriminative,
            anchor_text=anchor,
            deixis_strength=deixis_strength,
        )

    def resolve_selection(
        self,
        *,
        message: str,
        clarification: PendingClarification,
    ) -> TargetCandidate | None:
        text = message.strip().lower()
        candidates = clarification.candidates
        if not candidates:
            return None
        ordinal = self._ordinal(text)
        if ordinal is not None and 1 <= ordinal <= len(candidates):
            return candidates[ordinal - 1]
        for index, candidate in enumerate(candidates):
            letter = chr(ord("a") + index)
            if re.search(rf"(?:^|\s|[,，]){letter}(?:$|\s|[)）])", text):
                return candidate
            if candidate.target_id.lower() in text:
                return candidate
            label = "".join(str(candidate.label or "").lower().split())
            compact_text = "".join(text.split())
            if label and (label in compact_text or compact_text in label):
                return candidate
        return None

    @staticmethod
    def _allows_active_task(analysis: ReferenceAnalysis, *, task_count: int) -> bool:
        """ACTIVE_TASK only when reference analysis permits — never by default."""

        if "ORDINAL_REFERENCE" in analysis.kinds and analysis.ordinal is not None:
            return False
        # Strong deixis (刚才/上一个/这个…) wins over leftover schedule/mutation noise
        # in the anchor; explicit title hits are already handled earlier.
        if analysis.deixis_strength == "STRONG":
            return True
        if analysis.has_discriminative_anchor:
            return False
        if analysis.deixis_strength == "WEAK" and task_count == 1:
            return True
        return False

    def _match_explicit(
        self,
        message: str,
        candidates: list[EntityTargetCandidate],
    ) -> EntityTargetCandidate | None:
        typed = _TYPED_ID.search(message or "")
        if typed:
            target_id = typed.group(1)
            hits = [
                item
                for item in candidates
                if target_id in self._identity_tokens(item)
            ]
            if len(hits) == 1:
                return hits[0].model_copy(
                    update={"score": 1.0, "reason": "explicit typed id"}
                )
            if len(hits) > 1:
                return None

        scored: list[EntityTargetCandidate] = []
        folded_message = GoalResolver._normalize(message or "")
        anchor = GoalResolver._normalize(self._extract_anchor_text(message or ""))
        anchor_tokens = GoalResolver._word_tokens(anchor) if anchor else set()
        message_tokens = GoalResolver._word_tokens(folded_message)
        for item in candidates:
            best = 0.0
            best_label = ""
            for label in item.labels or ([item.label] if item.label else []):
                folded_label = GoalResolver._normalize(label or "")
                if len(folded_label) < self.EXPLICIT_TITLE_MIN_CHARS:
                    continue
                if folded_label in folded_message or folded_message in folded_label:
                    span = (
                        len(folded_label)
                        if folded_label in folded_message
                        else len(folded_message)
                    )
                    score = min(1.0, 0.75 + span / max(len(folded_label), 1) * 0.25)
                else:
                    score = self._best_title_fragment_score(
                        folded_message, folded_label
                    )
                    label_tokens = GoalResolver._word_tokens(folded_label)
                    # Titles often insert filler between key tokens ("学好 Java：…学习路线图").
                    # Require 2+ distinctive token hits in the utterance, not contiguous span.
                    strong_overlap = {
                        token
                        for token in (message_tokens & label_tokens)
                        if len(token) >= 3 or re.fullmatch(r"[a-z][a-z0-9]{1,}", token)
                    }
                    if len(strong_overlap) >= 2:
                        score = max(
                            score,
                            min(1.0, 0.86 + 0.03 * min(len(strong_overlap), 4)),
                        )
                    elif any(len(token) >= 4 for token in strong_overlap):
                        score = max(score, 0.84)
                    if anchor_tokens:
                        overlap = anchor_tokens & label_tokens
                        if len(overlap) >= 2:
                            coverage = len(overlap) / max(len(anchor_tokens), 1)
                            if coverage >= 0.5:
                                score = max(
                                    score,
                                    min(1.0, 0.84 + coverage * 0.16),
                                )
                if score > best:
                    best = score
                    best_label = label
            if best >= self.EXPLICIT_TITLE_THRESHOLD:
                scored.append(
                    item.model_copy(
                        update={
                            "score": best,
                            "reason": f"explicit title fragment: {best_label[:80]}",
                        }
                    )
                )
        if not scored:
            return None
        ranked = sorted(scored, key=lambda row: row.score, reverse=True)
        if len(ranked) > 1 and ranked[0].score - ranked[1].score < 0.05:
            return None
        return ranked[0]

    @classmethod
    def _best_title_fragment_score(cls, message: str, label: str) -> float:
        """Score longest label fragment contained in the utterance."""

        if not message or not label:
            return 0.0
        best = 0
        max_len = min(len(label), max(len(message), cls.EXPLICIT_TITLE_MIN_CHARS))
        for length in range(cls.EXPLICIT_TITLE_MIN_CHARS, max_len + 1):
            for index in range(0, len(label) - length + 1):
                frag = label[index : index + length]
                if frag in message:
                    best = max(best, length)
        if best < cls.EXPLICIT_TITLE_MIN_CHARS:
            return 0.0
        return min(1.0, 0.72 + best / max(len(label), 1) * 0.28)

    def _match_semantic(
        self,
        message: str,
        candidates: list[EntityTargetCandidate],
        analysis: ReferenceAnalysis,
    ) -> EntityTargetCandidate | None:
        anchor = analysis.anchor_text or message
        folded_anchor = GoalResolver._normalize(anchor)
        if len(folded_anchor) < 2:
            return None
        scored: list[EntityTargetCandidate] = []
        for item in candidates:
            best = 0.0
            for label in item.labels or ([item.label] if item.label else []):
                sim = GoalResolver._similarity(
                    folded_anchor,
                    GoalResolver._normalize(label or ""),
                )
                best = max(best, sim)
            if best >= self.SEMANTIC_THRESHOLD:
                scored.append(
                    item.model_copy(
                        update={
                            "score": best,
                            "reason": "semantic label overlap",
                        }
                    )
                )
        if not scored:
            return None
        ranked = sorted(scored, key=lambda row: row.score, reverse=True)
        if len(ranked) > 1 and ranked[0].score - ranked[1].score < self.SEMANTIC_GAP:
            return None
        return ranked[0]

    def _hit(
        self,
        selected: EntityTargetCandidate,
        *,
        method: ResolutionMethod,
        confidence: float,
        candidates: list[EntityTargetCandidate],
        kinds: list[str],
    ) -> EntityTargetResolution:
        return EntityTargetResolution(
            target_id=selected.target_id,
            target_type=selected.target_type,
            task_id=selected.task_id,
            goal_id=selected.goal_id,
            artifact_id=selected.artifact_id,
            schedule_id=selected.schedule_id,
            confidence=confidence,
            resolution_method=method,
            candidates=candidates[:8],
            reference_kinds=kinds,  # type: ignore[arg-type]
        )

    def _ambiguous(
        self,
        candidates: list[EntityTargetCandidate],
        kinds: list[str],
        *,
        confidence: float,
    ) -> EntityTargetResolution:
        clarification_candidates = [
            TargetCandidate(
                target_id=item.task_id,
                artifact_id=item.artifact_id,
                type="DRAFT",
                label=item.label,
                score=item.score or 0.5,
                reason=item.reason or "ambiguous task",
            )
            for item in candidates[:8]
        ]
        return EntityTargetResolution(
            resolution_method="AMBIGUOUS",
            confidence=confidence,
            candidates=candidates[:8],
            clarification=PendingClarification(
                question="你指的是哪一个任务？",
                candidates=clarification_candidates,
            ),
            reference_kinds=kinds,  # type: ignore[arg-type]
        )

    @staticmethod
    def _labels_for_goal(
        goal: ConversationGoal | None,
        task: TaskLike | None,
        entities: Sequence[WorkspaceEntity],
    ) -> list[str]:
        labels: list[str] = []

        def add(value: str | None) -> None:
            text = str(value or "").strip()
            if text and text not in labels:
                labels.append(text)

        if goal is not None:
            add(goal.summary)
            for title in goal.artifact_titles or []:
                add(title)
            for topic in goal.artifact_topics or []:
                add(topic)
            for alias in goal.aliases or []:
                add(alias)
        if task is not None:
            add(task.summary)
        for entity in entities:
            add(getattr(entity, "label", None))
        return labels

    @staticmethod
    def _identity_tokens(item: EntityTargetCandidate) -> set[str]:
        """Ids a typed reference (draft:/schedule:/…) may legally resolve to."""

        tokens = {
            item.task_id,
            item.goal_id,
            item.artifact_id or "",
            item.schedule_id or "",
            item.target_id,
        }
        artifact = item.artifact_id or ""
        if artifact.startswith("artifact-"):
            tokens.add(artifact[len("artifact-") :])
        # Common durable id shapes used in workspace bindings.
        for value in list(tokens):
            if value.startswith("draft:"):
                tokens.add(value.split(":", 1)[1])
            if value.startswith("schedule:"):
                tokens.add(value.split(":", 1)[1])
        return {token for token in tokens if token}

    @staticmethod
    def _extract_anchor_text(message: str) -> str:
        text = message or ""
        text = _MUTATION_NOISE.sub(" ", text)
        text = _SCHEDULE_FILLER.sub(" ", text)
        text = _GENERIC_ENTITY.sub(" ", text)
        text = _TEMPORAL_DEIXIS.sub(" ", text)
        text = _DIRECT_DEIXIS.sub(" ", text)
        text = _ORDINAL.sub(" ", text)
        return " ".join(text.split())

    @classmethod
    def _is_discriminative_anchor(cls, anchor: str) -> bool:
        folded = GoalResolver._normalize(anchor)
        if len(folded) < cls.EXPLICIT_TITLE_MIN_CHARS:
            return False
        tokens = GoalResolver._word_tokens(folded)
        return any(len(token) >= 2 for token in tokens)

    @staticmethod
    def _parse_ordinal_token(raw: str) -> int | None:
        if raw.isdigit():
            return int(raw)
        values = {
            "一": 1,
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        return values.get(raw)

    @staticmethod
    def _target_context(
        *, goal: ConversationGoal, workspace: ConversationWorkspace
    ) -> TargetContext:
        workspace_context = workspace.target_context
        goal_context = goal.target_context
        return TargetContext(
            content_target=workspace_context.content_target or goal_context.content_target,
            schedule_target=workspace_context.schedule_target
            or goal_context.schedule_target,
            publication_target=(
                workspace_context.publication_target or goal_context.publication_target
            ),
            interaction_target=(
                workspace_context.interaction_target or goal_context.interaction_target
            ),
        )

    def _collect_candidates(
        self,
        *,
        message: str,
        operation: str,
        context: TargetContext,
        workspace: ConversationWorkspace,
        artifacts: Iterable[dict[str, Any]],
        target_history: Iterable[TargetBinding],
    ) -> list[TargetCandidate]:
        rows: OrderedDict[str, TargetCandidate] = OrderedDict()
        text = message.strip().lower()
        allowed = self._allowed_types(operation)

        def add(
            *,
            target_id: str,
            artifact_id: str | None,
            target_type: str,
            score: float,
            reason: str,
            label: str | None = None,
            content_artifact_id: str | None = None,
            content_artifact_version: int | None = None,
        ) -> None:
            if not target_id or target_type not in allowed:
                return
            candidate = TargetCandidate(
                target_id=target_id,
                artifact_id=artifact_id,
                type=target_type,  # type: ignore[arg-type]
                label=label,
                score=max(0.0, min(1.0, score)),
                reason=reason,
                content_artifact_id=content_artifact_id,
                content_artifact_version=content_artifact_version,
            )
            current = rows.get(target_id)
            if current is None or candidate.score > current.score:
                rows[target_id] = candidate
            elif current.label is None and label:
                rows[target_id] = current.model_copy(update={"label": label})

        preferred = context.for_operation(operation)
        if preferred is not None:
            add(
                target_id=(preferred.schedule_id or preferred.target_id)
                if operation in {"UPDATE_SCHEDULE", "CANCEL_SCHEDULE", "PUBLISH_NOW"}
                and preferred.target_type == "DRAFT"
                else preferred.target_id,
                artifact_id=preferred.artifact_id,
                target_type=(
                    "SCHEDULE"
                    if operation in {"UPDATE_SCHEDULE", "CANCEL_SCHEDULE"}
                    and preferred.target_type == "DRAFT"
                    else preferred.target_type
                ),
                score=0.97,
                reason="operation-scoped TargetContext",
                content_artifact_id=preferred.content_artifact_id,
                content_artifact_version=preferred.content_artifact_version,
            )

        entities = [
            item
            for item in workspace.entities
            if item.kind in allowed and item.actionable
        ]
        for entity in entities:
            label = entity.label.lower()
            explicit_id = entity.entity_id.lower() in text
            explicit_title = bool(label) and label in text
            overlap = self._overlap(text, label)
            score = 1.0 if explicit_id or explicit_title else 0.66 + min(0.18, overlap)
            reason = (
                "explicit target reference"
                if explicit_id or explicit_title
                else f"workspace candidate: {entity.label}"
            )
            if entity.ref in workspace.focus_refs:
                score = max(score, 0.84)
            add(
                target_id=entity.entity_id,
                artifact_id=entity.source_artifact_id,
                target_type=entity.kind,
                score=score,
                reason=reason,
                label=entity.label,
                content_artifact_id=entity.content_artifact_id,
                content_artifact_version=entity.content_artifact_version,
            )

        for binding in target_history:
            binding_type = self._binding_type_for_operation(binding, operation)
            if binding_type in allowed:
                add(
                    target_id=(binding.schedule_id or binding.target_id)
                    if binding_type == "SCHEDULE"
                    else binding.target_id,
                    artifact_id=binding.artifact_id,
                    target_type=binding_type,
                    score=0.70,
                    reason="Goal target history",
                    content_artifact_id=binding.content_artifact_id,
                    content_artifact_version=binding.content_artifact_version,
                )

        for artifact in artifacts:
            content = artifact.get("content") or artifact.get("result") or {}
            if not isinstance(content, dict):
                continue
            artifact_type = str(artifact.get("artifact_type") or "")
            target_type = str(artifact.get("target_type") or "")
            if not target_type:
                target_type = (
                    "SCHEDULE" if artifact_type == "SCHEDULE_RECEIPT" else "DRAFT"
                )
            if target_type == "SCHEDULE":
                target_id = str(
                    content.get("action_id") or content.get("schedule_id") or ""
                )
            else:
                target_id = str(
                    content.get("draft_id")
                    or content.get("post_id")
                    or content.get("id")
                    or ""
                )
            if target_id:
                add(
                    target_id=target_id,
                    artifact_id=str(
                        artifact.get("id") or artifact.get("artifact_id") or ""
                    )
                    or None,
                    target_type=target_type,
                    score=0.68,
                    reason="artifact candidate",
                    content_artifact_id=(
                        str(content.get("content_artifact_id"))
                        if content.get("content_artifact_id")
                        else None
                    ),
                    content_artifact_version=(
                        int(content.get("content_artifact_version"))
                        if content.get("content_artifact_version") is not None
                        else None
                    ),
                )
        return list(rows.values())

    @staticmethod
    def _allowed_types(operation: str) -> set[str]:
        if operation in {"UPDATE_SCHEDULE", "CANCEL_SCHEDULE"}:
            return {"SCHEDULE"}
        if operation in {"APPEND_CONTENT", "REPLACE_CONTENT", "UPDATE_TITLE"}:
            return {"DRAFT", "POST"}
        if operation == "PUBLISH_NOW":
            return {"SCHEDULE", "DRAFT", "POST"}
        return {"DRAFT", "POST", "ARTIFACT"}

    @staticmethod
    def _binding_type_for_operation(binding: TargetBinding, operation: str) -> str:
        if operation in {"UPDATE_SCHEDULE", "CANCEL_SCHEDULE"} and binding.schedule_id:
            return "SCHEDULE"
        return binding.target_type

    @staticmethod
    def _explicit_reference(message: str) -> str | None:
        match = re.search(
            r"\b(?:draft|post|schedule|artifact):([a-z0-9_-]+)\b",
            message.lower(),
        )
        return match.group(1) if match else None

    @staticmethod
    def _overlap(message: str, label: str) -> float:
        tokens = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", message.lower()))
        label_tokens = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", label.lower()))
        if not tokens or not label_tokens:
            return 0.0
        return len(tokens & label_tokens) / max(1, len(label_tokens)) * 0.35

    @staticmethod
    def _explicit_label_overlap(message: str, label: str) -> bool:
        message_folded = message.casefold()
        label_folded = label.casefold()
        message_words = set(re.findall(r"[a-z0-9+#._-]{2,}", message_folded))
        label_words = set(re.findall(r"[a-z0-9+#._-]{2,}", label_folded))
        if message_words & label_words:
            return True
        chinese_phrases = re.findall(r"[\u4e00-\u9fff]{2,}", message_folded)
        return any(phrase in label_folded for phrase in chinese_phrases)

    @staticmethod
    def _ordinal(text: str) -> int | None:
        match = re.search(
            r"(?:第\s*)([一二三四五六七八九十\d]+)\s*(?:个|項|项|篇|条|條)?",
            text,
        )
        if not match:
            return None
        raw = match.group(1)
        if raw.isdigit():
            return int(raw)
        values = {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        return values.get(raw)


__all__ = [
    "TargetResolution",
    "TargetResolver",
    "EntityTargetCandidate",
    "EntityTargetResolution",
    "ReferenceAnalysis",
    "ReferenceKind",
    "ResolutionMethod",
]
