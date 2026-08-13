from __future__ import annotations

import logging
import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)

_TRACE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key", "x-auth-token"}


class TraceMiddleware(BaseHTTPMiddleware):
    """Injects trace_id and sanitizes sensitive headers from logs."""

    async def dispatch(self, request: Request, call_next):
        requested = request.headers.get("X-Trace-ID", "")
        trace_id = requested if _TRACE_ID_RE.fullmatch(requested) else str(uuid.uuid4())
        request.state.trace_id = trace_id

        response = await call_next(request)
        response.headers.setdefault("X-Trace-ID", trace_id)
        return response


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip sensitive header values before logging."""
    return {
        k: ("***" if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }
