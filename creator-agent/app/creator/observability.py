from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

_provider: Any = None


def configure_creator_telemetry(settings: Any) -> None:
    global _provider
    if not bool(getattr(settings, "creator_otel_enabled", False)):
        return
    if _provider is not None:
        return
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": getattr(
                    settings,
                    "creator_otel_service_name",
                    "mindflow-creator",
                ),
                "service.version": "1.0.0",
            }
        )
    )
    endpoint = str(getattr(settings, "creator_otel_exporter_endpoint", "")).strip()
    if endpoint:
        headers = _parse_headers(
            str(getattr(settings, "creator_otel_exporter_headers", ""))
        )
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, headers=headers or None)
            )
        )
    trace.set_tracer_provider(provider)
    _provider = provider


def instrument_creator_fastapi(app: Any) -> None:
    if FastAPIInstrumentor is None:
        return
    try:
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="/health,/ready,/favicon.svg",
        )
    except Exception:
        logger.exception("FastAPI OpenTelemetry instrumentation failed")


def shutdown_creator_telemetry() -> None:
    global _provider
    provider = _provider
    _provider = None
    if provider is not None:
        provider.shutdown()


@contextmanager
def creator_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    tracer = trace.get_tracer("mindflow.creator")
    with tracer.start_as_current_span(name, attributes=_clean(attributes or {})) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def set_span_attributes(span: Any, attributes: dict[str, Any]) -> None:
    for key, value in _clean(attributes).items():
        span.set_attribute(key, value)


def _clean(attributes: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in attributes.items()
        if value is not None and isinstance(value, (str, bool, int, float))
    }


def _parse_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw.split(","):
        name, separator, value = item.partition("=")
        if separator and name.strip() and value.strip():
            headers[name.strip()] = value.strip()
    return headers
