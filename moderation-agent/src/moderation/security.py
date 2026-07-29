import re
from dataclasses import dataclass
from typing import Any, Literal

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_IDENTITY_18 = re.compile(r"(?<!\d)(\d{3})\d{11}(\d{3}[0-9Xx])(?!\w)")
_IDENTITY_15 = re.compile(r"(?<!\d)(\d{3})\d{8}(\d{4})(?!\d)")
_EMAIL = re.compile(
    r"(?i)(?<![\w.+-])([a-z0-9._%+-])(?:[a-z0-9._%+-]*)@([a-z0-9.-]+\.[a-z]{2,})(?![\w.-])"
)

SensitiveTextKind = Literal["PHONE", "IDENTITY_NUMBER", "EMAIL"]


@dataclass(frozen=True, slots=True)
class SensitiveTextMatch:
    kind: SensitiveTextKind
    value: str
    start: int
    end: int


def redact_text(value: str) -> str:
    value = _PHONE.sub(r"\1****\2", value)
    value = _IDENTITY_18.sub(r"\1***********\2", value)
    value = _IDENTITY_15.sub(r"\1********\2", value)
    return _EMAIL.sub(r"\1***@\2", value)


def redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    return value


def find_sensitive_text(value: str) -> list[SensitiveTextMatch]:
    matches: list[SensitiveTextMatch] = []
    patterns: tuple[tuple[SensitiveTextKind, re.Pattern[str]], ...] = (
        ("PHONE", _PHONE),
        ("IDENTITY_NUMBER", _IDENTITY_18),
        ("IDENTITY_NUMBER", _IDENTITY_15),
        ("EMAIL", _EMAIL),
    )
    for kind, pattern in patterns:
        matches.extend(
            SensitiveTextMatch(
                kind=kind,
                value=match.group(0),
                start=match.start(),
                end=match.end(),
            )
            for match in pattern.finditer(value)
        )
    return sorted(matches, key=lambda item: (item.start, item.end, item.kind))
