"""Deterministic reference-feature extraction from raw user text.

The LLM is the primary source for ``target_reference``; this module is a
deterministic fallback so a follow-up turn still resolves when the model omits
a usable reference.  It only extracts *features* (an explicit id, an ordinal,
a recency marker, a coarse time window, a topic token, or a resource hint) —
it never decides which resource is the target.  ``TargetResolver`` owns
candidate selection and the bind/clarify boundary, so an extracted feature
cannot manufacture an identity that the model did not see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import CommandTarget, TargetKind, TargetReferenceType

# Coarse time words that map to a run_at hour window (mirrors target._time_window).
_TIME_WORDS: tuple[str, ...] = (
    "凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "早晨",
)

# Recency markers: the most recent conversation object.
_PROXIMAL_MARKERS: tuple[str, ...] = (
    "刚刚", "刚才", "最近", "最新", "最后那", "上一条",
)

_ORDINAL_CN: dict[str, int] = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# Topic tokens are ASCII tech words or short CJK phrases that name a subject.
_TOPIC_BEFORE = re.compile(
    r"([A-Za-z][A-Za-z0-9._-]*(?:\s+[A-Za-z][A-Za-z0-9._-]*){0,3}"
    r"|[一-龥]{2,8})\s*(?:那篇|这篇|那篇文章|这篇文章|那篇内容|这篇内容)"
)
_TOPIC_AFTER = re.compile(
    r"(?:那篇|这篇|那篇文章|这篇文章)\s*"
    r"([A-Za-z][A-Za-z0-9._-]*(?:\s+[A-Za-z][A-Za-z0-9._-]*){0,3}"
    r"|[一-龥]{2,8})"
)
_EXPLICIT_ID = re.compile(r"\b(\d{10,})\b")
_ORDINAL = re.compile(r"第\s*([一二三四五六七八九十两\d]+)\s*篇")
# A time word is a target reference only when it directly precedes the
# reference suffix ("下午那篇"), never when it follows it ("那篇改到下午四点",
# where 下午 is the desired new run_at, not the target).
_TIME_BEFORE_REF = re.compile(
    r"(凌晨|早上|上午|中午|下午|傍晚|晚上|早晨)\s*(?:那篇|这篇|那篇内容)"
)
_RESOURCE_HINT = re.compile(r"(?:那个|这个)\s*(草稿|发布计划|排程|定时任务|帖子|文章)")

# Generic reference grammar.  This describes a demonstrative/determiner plus
# a resource expression; it does not select a candidate.  Keeping the
# grammar here is important because the model may omit ``target`` entirely
# even though the user supplied a perfectly valid, but non-unique, reference.
_CN_GENERIC_REFERENCE = re.compile(
    r"(?:[这那](?:篇|份|个|条|项|件)(?:发布计划|定时任务|草稿|排程|帖子|文章|内容)?"
    r"|[这那](?:发布计划|定时任务|草稿|排程|帖子|文章|内容))"
)
_EN_GENERIC_REFERENCE = re.compile(
    r"\b(?:(?:this|that|the)(?:\s+one)?|one\s+of\s+the)\s+"
    r"(?P<noun>posts?|articles?|drafts?|schedules?|publications?)\b",
    re.IGNORECASE,
)

_RESOURCE_KIND_BY_WORD = {
    "草稿": TargetKind.DRAFT,
    "发布计划": TargetKind.SCHEDULE,
    "排程": TargetKind.SCHEDULE,
    "定时任务": TargetKind.SCHEDULE,
    "帖子": TargetKind.POST,
    "文章": TargetKind.POST,
    "内容": TargetKind.POST,
    "draft": TargetKind.DRAFT,
    "drafts": TargetKind.DRAFT,
    "schedule": TargetKind.SCHEDULE,
    "schedules": TargetKind.SCHEDULE,
    "publication": TargetKind.SCHEDULE,
    "publications": TargetKind.SCHEDULE,
    "post": TargetKind.POST,
    "posts": TargetKind.POST,
    "article": TargetKind.POST,
    "articles": TargetKind.POST,
}


@dataclass(frozen=True)
class ReferenceFeature:
    """One deterministic reference feature, ready to project onto CommandTarget."""

    kind: TargetKind = TargetKind.TASK
    id: str | None = None
    reference_type: TargetReferenceType = TargetReferenceType.NONE
    reference: str | None = None
    ordinal: int | None = None
    property: str | None = None
    value: str | None = None
    temporal_word: str | None = None
    topic: str | None = None
    raw: str = field(default="")

    def to_command_target(self) -> CommandTarget:
        if self.id:
            return CommandTarget(
                kind=self.kind, id=self.id, reference_type=TargetReferenceType.IDENTIFIER
            )
        if self.ordinal is not None:
            return CommandTarget(
                kind=self.kind,
                reference_type=TargetReferenceType.ORDINAL,
                ordinal=self.ordinal,
            )
        if self.temporal_word:
            return CommandTarget(
                kind=self.kind,
                reference_type=TargetReferenceType.PROPERTY,
                property="run_at",
                value=self.temporal_word,
            )
        if self.topic:
            return CommandTarget(
                kind=self.kind,
                reference_type=TargetReferenceType.PROPERTY,
                property="label",
                value=self.topic,
            )
        if self.reference_type == TargetReferenceType.ACTIVE:
            return CommandTarget(kind=self.kind, reference_type=TargetReferenceType.ACTIVE)
        return CommandTarget(kind=self.kind, reference_type=TargetReferenceType.NONE)


class ReferenceExtractor:
    """Extract at most one reference feature from a raw user message."""

    def extract(self, text: str) -> ReferenceFeature | None:
        if not text or not text.strip():
            return None
        raw = text.strip()
        kind = self._resource_kind(raw)
        explicit = _EXPLICIT_ID.search(raw)
        if explicit:
            return ReferenceFeature(kind=kind, id=explicit.group(1), raw=raw)
        ordinal = _ORDINAL.search(raw)
        if ordinal:
            parsed = self._parse_ordinal(ordinal.group(1))
            if parsed is not None:
                return ReferenceFeature(kind=kind, ordinal=parsed, raw=raw)
        temporal = _TIME_BEFORE_REF.search(raw)
        if temporal:
            return ReferenceFeature(
                kind=kind,
                temporal_word=temporal.group(1),
                reference_type=TargetReferenceType.PROPERTY,
                raw=raw,
            )
        if any(marker in raw for marker in _PROXIMAL_MARKERS):
            return ReferenceFeature(
                kind=kind, reference_type=TargetReferenceType.ACTIVE, raw=raw
            )

        generic = self._generic_reference(raw)
        topic = self._topic_token(raw, before_only=True)
        # A Latin/technical token before a demonstrative remains a useful
        # label reference (for example ``Java 那篇``).  A generic resource
        # noun wins over a broad CJK match such as the imperative verb in
        # ``删除那篇文章``; otherwise an action word would be treated as a
        # title and all valid candidates would be filtered out.
        if topic and (topic.isascii() or generic is None or not self._has_resource_noun(raw)):
            return ReferenceFeature(
                kind=kind,
                topic=topic,
                reference_type=TargetReferenceType.PROPERTY,
                property="label",
                raw=raw,
            )
        if generic is not None:
            return generic
        if kind != TargetKind.TASK:
            # "那个草稿 / 这个发布计划" names a resource kind without a subject
            # token; the active object of that kind is the only safe candidate.
            return ReferenceFeature(
                kind=kind, reference_type=TargetReferenceType.ACTIVE, raw=raw
            )
        topic = self._topic_token(raw)
        if topic:
            return ReferenceFeature(
                kind=kind,
                topic=topic,
                reference_type=TargetReferenceType.PROPERTY,
                property="label",
                raw=raw,
            )
        return None

    @staticmethod
    def _resource_kind(text: str) -> TargetKind:
        english = _EN_GENERIC_REFERENCE.search(text)
        if english:
            return _RESOURCE_KIND_BY_WORD.get(
                english.group("noun").casefold(), TargetKind.TASK
            )
        chinese = _CN_GENERIC_REFERENCE.search(text)
        if chinese:
            for word, resource_kind in _RESOURCE_KIND_BY_WORD.items():
                if word in chinese.group(0):
                    return resource_kind
            return TargetKind.TASK
        hint = _RESOURCE_HINT.search(text)
        if hint:
            word = hint.group(1)
            return _RESOURCE_KIND_BY_WORD.get(word, TargetKind.TASK)
        return TargetKind.TASK

    @classmethod
    def _generic_reference(cls, text: str) -> ReferenceFeature | None:
        chinese = _CN_GENERIC_REFERENCE.search(text)
        english = _EN_GENERIC_REFERENCE.search(text)
        if chinese is None and english is None:
            return None
        kind = cls._resource_kind(text)
        return ReferenceFeature(
            kind=kind,
            reference_type=TargetReferenceType.ACTIVE,
            raw=text,
        )

    @staticmethod
    def _has_resource_noun(text: str) -> bool:
        chinese = _CN_GENERIC_REFERENCE.search(text)
        if chinese:
            return any(
                word in chinese.group(0)
                for word in ("草稿", "发布计划", "排程", "定时任务", "帖子", "文章", "内容")
            )
        return _EN_GENERIC_REFERENCE.search(text) is not None

    @staticmethod
    def _topic_token(text: str, *, before_only: bool = False) -> str | None:
        match = _TOPIC_BEFORE.search(text)
        if match is None and not before_only:
            match = _TOPIC_AFTER.search(text)
        if not match:
            return None
        token = match.group(1).strip()
        if not token:
            return None
        # Never treat a coarse time word or recency marker as a subject token;
        # those are handled by dedicated features above.
        if token in _TIME_WORDS:
            return None
        if any(marker in token for marker in _PROXIMAL_MARKERS):
            return None
        if re.match(r"^第[一二三四五六七八九十两\d]+$", token):
            return None
        if len(token) < 2 and not token.isascii():
            return None
        return token

    @staticmethod
    def _parse_ordinal(value: str) -> int | None:
        digits = value.strip()
        if digits.isdigit():
            return int(digits)
        total = 0
        if "十" in digits:
            left, _, right = digits.partition("十")
            total += (_ORDINAL_CN.get(left, 1) if left else 1) * 10
            total += _ORDINAL_CN.get(right, 0)
            return total
        if len(digits) == 1:
            return _ORDINAL_CN.get(digits)
        return None


__all__ = ["ReferenceExtractor", "ReferenceFeature"]
