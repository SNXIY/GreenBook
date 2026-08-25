"""Dev-only observability endpoints: metrics scrape + per-trace timeline.

These are minimal and non-durable.  They never expose prompts, tokens, or full
bodies — only stage names, ids, statuses, and latencies.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/debug", tags=["debug"])


@router.get("/metrics")
async def metrics(request: Request) -> str:
    from greenbook_agent_core.observability.bus import observability

    return observability().render_metrics()


@router.get("/traces/{trace_id}")
async def trace_timeline(trace_id: str, request: Request) -> dict:
    from greenbook_agent_core.observability.bus import observability

    timeline = observability().traces.get(trace_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="trace not found")
    return {
        "trace_id": trace_id,
        "spans": [
            span.model_dump(mode="json", exclude={"at"}) | {"at": span.at}
            for span in timeline.spans()
        ],
    }
