from __future__ import annotations

import re


class CreatorPrivacySanitizer:
    """Removes common direct identifiers before building retrieval queries."""

    _patterns = (
        re.compile(r"1[3-9]\d{9}"),
        re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
        re.compile(r"\b\d{17}[\dXx]\b"),
    )

    def sanitize(self, text: str) -> str:
        sanitized = text or ""
        for pattern in self._patterns:
            sanitized = pattern.sub("[已脱敏]", sanitized)
        return sanitized
