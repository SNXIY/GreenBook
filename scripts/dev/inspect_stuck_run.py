"""Read-only diagnosis of a real browser run that did not converge."""

from __future__ import annotations

import asyncio
import json
import os

import asyncpg


RUN_ID = "fda21065-821e-429d-bdfc-4e37751e1e55"
EXECUTION_ID = "b1c7eefb-25b5-44f9-a494-7a59e0302e1c"
TASK_ID = "c86b9ccd-b784-46a5-9f40-d770d074b835"


def decode(value):
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return str(value)


def summary(value):
    item = decode(value)
    if isinstance(item, dict):
        keys = ("status", "task_status", "semantic_action", "capability", "execution_id", "run_id", "task_id", "objective_id", "error_code", "error_message", "state", "actions", "entities", "related_operations", "related_resource_ids", "last_action", "active_execution_id")
        result = {key: item[key] for key in keys if key in item}
        if "objectives" in item:
            result["objectives"] = [
                {key: objective.get(key) for key in ("objective_id", "description", "intent", "status", "related_operations", "related_resource_ids", "completed_at") if key in objective}
                for objective in (decode(item["objectives"]) or [])
                if isinstance(objective, dict)
            ]
        return result or {"keys": sorted(item)[:80]}
    return item


async def main() -> None:
    url = os.environ.get("GREENBOOK_AGENT_DATABASE_URL", "postgresql://mindflow:mindflow@127.0.0.1:25432/mindflow_creator")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    try:
        output = {}
        output["execution"] = [dict(row) for row in await conn.fetch(
            "select execution_id::text, task_id::text, status, current_step_index, version, created_at, updated_at, completed_at from execution where execution_id::text=$1",
            EXECUTION_ID,
        )]
        output["execution_step"] = [dict(row) for row in await conn.fetch(
            "select step_execution_id::text, step_id, execution_id::text, capability, ordinal, status, retry_count, error_code, error_message, started_at, completed_at from execution_step where execution_id::text=$1 order by ordinal",
            EXECUTION_ID,
        )]
        task = await conn.fetchrow(
            "select task_id::text, status, active_execution_id::text, last_action, objectives, execution_refs, resource_index, updated_at, completed_at from assistant_tasks where task_id::text=$1",
            TASK_ID,
        )
        output["assistant_task"] = dict(task) if task else None
        run = await conn.fetchrow(
            "select run_id::text, conversation_id::text, status, error_code, error_message, payload, created_at, updated_at from agent_runs where run_id::text=$1",
            RUN_ID,
        )
        output["agent_run"] = dict(run) if run else None
        projections = await conn.fetch(
            "select execution_id::text, run_id::text, task_id::text, status, task_status, artifacts, assistant_response, created_at, updated_at from assistant_execution_result_projections where run_id::text=$1 order by created_at",
            RUN_ID,
        )
        output["result_projections"] = [dict(row) for row in projections]
        observations = await conn.fetch(
            "select * from agent_action_observations where execution_id::text=$1 or run_id::text=$2",
            EXECUTION_ID,
            RUN_ID,
        )
        output["observations"] = [dict(row) for row in observations]
        for key, value in list(output.items()):
            if key in {"assistant_task", "agent_run"} and value is not None:
                output[key] = {field: summary(item) if field in {"objectives", "execution_refs", "resource_index", "payload"} else item for field, item in value.items()}
            elif key == "result_projections":
                output[key] = [{field: summary(item) if field in {"artifacts", "assistant_response"} else item for field, item in row.items()} for row in value]
            elif key == "observations":
                output[key] = [{field: summary(item) if isinstance(item, (dict, list, str)) and field not in {"observation_id", "execution_id", "run_id", "task_id", "created_at", "updated_at"} else item for field, item in row.items()} for row in value]
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
