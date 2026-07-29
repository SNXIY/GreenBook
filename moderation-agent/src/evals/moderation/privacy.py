import hashlib
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from evals.moderation.io import DatasetValidationError
from evals.moderation.schemas import (
    EvalCaseSource,
    EvalPrivacyMode,
    ModerationEvalCase,
)
from moderation.security import SensitiveTextKind, find_sensitive_text, redact_text

_SYNTHETIC_SOURCES = {
    EvalCaseSource.POLICY_TEMPLATE,
    EvalCaseSource.LLM_GENERATED,
    EvalCaseSource.CURATED_SEED,
}
_FORMATTED_PHONE = re.compile(r"(?<!\d)1[3-9]\d(?:[\s._-]*\d){8}(?!\d)")
_FORMATTED_IDENTITY = re.compile(r"(?<!\d)\d(?:[\s._-]*\d){16}[\s._-]*[0-9Xx](?!\w)")
_OBFUSCATED_EMAIL = re.compile(
    r"(?i)(?<!\w)[a-z0-9._%+-]+\s*(?:\[at\]|\(at\)|＠)\s*"
    r"[a-z0-9.-]+\s*(?:\[dot\]|\(dot\)|\[点\])\s*[a-z]{2,}(?!\w)"
)
_CREDENTIAL = re.compile(
    r"(?i)(?:password|passwd|pwd|密码|口令|api[_ -]?key|token)"
    r"\s*[:=：]\s*([^\s,，;；]{6,128})"
)

type EvalSensitiveTextKind = SensitiveTextKind | Literal[
    "OBFUSCATED_PHONE",
    "OBFUSCATED_IDENTITY_NUMBER",
    "OBFUSCATED_EMAIL",
    "CREDENTIAL",
]


@dataclass(frozen=True, slots=True)
class PrivacyFinding:
    case_id: str
    path: str
    kind: EvalSensitiveTextKind
    redacted_value: str
    message: str


@dataclass(frozen=True, slots=True)
class PrivacyReport:
    errors: tuple[PrivacyFinding, ...]
    warnings: tuple[PrivacyFinding, ...]

    @property
    def passed(self) -> bool:
        return not self.errors


class PrivacyValidationError(DatasetValidationError):
    """Raised when an evaluation record may contain undeclared real personal data."""


def inspect_privacy(cases: Sequence[ModerationEvalCase]) -> PrivacyReport:
    errors: list[PrivacyFinding] = []
    warnings: list[PrivacyFinding] = []

    for case in cases:
        declared = set(case.privacy.synthetic_sensitive_values)
        observed: set[str] = set()
        matches: list[tuple[str, EvalSensitiveTextKind, str]] = []
        for path, value in _iter_scannable_text(case):
            for kind, sensitive_value in _find_sensitive_values(value):
                matches.append((path, kind, sensitive_value))
                observed.add(sensitive_value)

        if case.privacy.mode == EvalPrivacyMode.SYNTHETIC_ONLY:
            if case.annotation.source not in _SYNTHETIC_SOURCES:
                errors.append(
                    _finding(
                        case,
                        "privacy.mode",
                        "IDENTITY_NUMBER",
                        "not-applicable",
                        "SYNTHETIC_ONLY is forbidden for production-derived sources",
                    )
                )
            for path, kind, value in matches:
                if value not in declared:
                    errors.append(
                        _finding(
                            case,
                            path,
                            kind,
                            value,
                            "detected sensitive text is not declared as synthetic",
                        )
                    )
            for value in sorted(declared - observed):
                detected = find_sensitive_text(value)
                declared_kind: EvalSensitiveTextKind = (
                    detected[0].kind if detected else "IDENTITY_NUMBER"
                )
                warnings.append(
                    _finding(
                        case,
                        "privacy.synthetic_sensitive_values",
                        declared_kind,
                        value,
                        "declared synthetic value was not found in a scannable field",
                    )
                )
        else:
            for path, kind, value in matches:
                mode_message = (
                    "sensitive text remains in a production-redacted record"
                    if case.privacy.mode == EvalPrivacyMode.PRODUCTION_REDACTED
                    else "sensitive text found without a synthetic declaration"
                )
                errors.append(_finding(case, path, kind, value, mode_message))

    return PrivacyReport(errors=tuple(errors), warnings=tuple(warnings))


def validate_privacy(cases: Sequence[ModerationEvalCase]) -> PrivacyReport:
    report = inspect_privacy(cases)
    if report.errors:
        details = "\n".join(
            f"{finding.case_id} {finding.path}: {finding.message} "
            f"({finding.kind}={finding.redacted_value})"
            for finding in report.errors
        )
        raise PrivacyValidationError(details)
    return report


def _finding(
    case: ModerationEvalCase,
    path: str,
    kind: EvalSensitiveTextKind,
    value: str,
    message: str,
) -> PrivacyFinding:
    return PrivacyFinding(
        case_id=case.case_id,
        path=path,
        kind=kind,
        redacted_value=_safe_redaction(value),
        message=message,
    )


def _iter_scannable_text(case: ModerationEvalCase) -> Iterator[tuple[str, str]]:
    payload = case.model_dump(mode="json")
    privacy = payload.get("privacy")
    if isinstance(privacy, dict):
        privacy.pop("synthetic_sensitive_values", None)
    yield from _iter_text(payload)


def _iter_text(value: Any, path: str = "$") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_text(item, f"{path}.{key}")
        return
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            yield from _iter_text(item, f"{path}[{index}]")


def _find_sensitive_values(value: str) -> Iterator[tuple[EvalSensitiveTextKind, str]]:
    occupied: set[tuple[int, int]] = set()
    for sensitive_match in find_sensitive_text(value):
        occupied.add((sensitive_match.start, sensitive_match.end))
        yield sensitive_match.kind, sensitive_match.value

    for kind, pattern in (
        ("OBFUSCATED_PHONE", _FORMATTED_PHONE),
        ("OBFUSCATED_IDENTITY_NUMBER", _FORMATTED_IDENTITY),
        ("OBFUSCATED_EMAIL", _OBFUSCATED_EMAIL),
    ):
        for regex_match in pattern.finditer(value):
            location = (regex_match.start(), regex_match.end())
            if location in occupied:
                continue
            matched_value = regex_match.group(0)
            if kind in {"OBFUSCATED_PHONE", "OBFUSCATED_IDENTITY_NUMBER"}:
                compact = re.sub(r"[\s._-]", "", matched_value)
                if find_sensitive_text(compact) and matched_value == compact:
                    continue
            yield kind, matched_value  # type: ignore[misc]

    for credential_match in _CREDENTIAL.finditer(value):
        yield "CREDENTIAL", credential_match.group(1)


def _safe_redaction(value: str) -> str:
    redacted = redact_text(value)
    if redacted != value:
        return redacted
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"<redacted:{digest}>"
