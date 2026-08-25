"""Read-only task/execution/run/projection diagnosis."""

from __future__ import annotations

import argparse
import asyncio
import json

import asyncpg


def decode(value):
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def compact(value):
    item = decode(value)
    if isinstance(item, dict):
        keys = (
            "status",
            "task_status",
            "execution_id",
            "run_id",
            "task_id",
            "objective_id",
            "last_action",
            "active_execution_id",
            "error_code",
            "error_message",
            "related_operations",
            "related_resource_ids",
        )
        result = {key: item[key] for key in keys if key in item}
        if "objectives" in item:
            result["objectives"] = [
                {
                    key: objective.get(key)
                    for key in (
                        "objective_id",
                        "description",
                        "status",
                        "related_operations",
                        "related_resource_ids",
                        "completed_at",
                    )
                    if key in objective
                }
                for objective in decode(item["objectives"]) or []
                if isinstance(objective, dict)
            ]
        return result
    if isinstance(item, list):
        return [compact(value) for value in item]
    return item


async def main(task_id: str) -> None:
    connection = await asyncpg.connect(
        "postgresql://mindflow:mindflow@127.0.0.1:25432/mindflow_creator"
    )
    try:
        task = await connection.fetchrow(
            """
            select task_id::text, conversation_id::text, status,
                   active_execution_id, last_action, last_error,
                   objectives, execution_refs, resource_index,
                   created_at, updated_at, completed_at
            from assistant_tasks where task_id::text = $1
            """,
            task_id,
        )
        executions = await connection.fetch(
            """
            select execution_id::text, task_id::text, status,
                   current_step_index, created_at, updated_at, completed_at
            from execution where task_id::text = $1 order by created_at
            """,
            task_id,
        )
        runs = await connection.fetch(
            """
            select run_id::text, conversation_id::text, status,
                   error_code, payload, created_at, updated_at
            from agent_runs
            where payload::text like $1 order by created_at
            """,
            f"%{task_id}%",
        )
        projections = await connection.fetch(
            """
            select execution_id::text, run_id::text, task_id::text,
                   status, task_status, artifacts, assistant_response,
                   created_at, updated_at
            from assistant_execution_result_projections
            where task_id::text = $1 order by created_at
            """,
            task_id,
        )
        steps = await connection.fetch(
            """
            select execution_id::text, step_id, capability, ordinal, status,
                   error_code, error_message, started_at, completed_at
            from execution_step
            where execution_id in (
              select execution_id from execution where task_id::text = $1
            )
            order by execution_id, ordinal
            """,
            task_id,
        )
        print(
            json.dumps(
                {
                    "task": {
                        key: compact(value) if key in {"objectives", "execution_refs", "resource_index"} else value
                        for key, value in dict(task).items()
                    }
                    if task
                    else None,
                    "executions": [dict(row) for row in executions],
                    "runs": [
                        {
                            **dict(row),
                            "payload": compact(row["payload"]),
                        }
                        for row in runs
                    ],
                    "projections": [
                        {
                            **dict(row),
                            "artifacts": compact(row["artifacts"]),
                            "assistant_response": compact(row["assistant_response"]),
                        }
                        for row in projections
                    ],
                    "steps": [dict(row) for row in steps],
                },
                default=str,
            )
        )
    finally:
        await connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    args = parser.parse_args()
    asyncio.run(main(args.task_id))
