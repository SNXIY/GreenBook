"""Read-only inspection for the live RESULT_UNKNOWN baseline evidence."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import asyncpg


RUNS = {
    "03f95969-9b0c-47c2-9f79-927cd65a3e41",
    "be031d82-dc86-4a73-bc33-982fc8cbf318",
    "e0a07ab7-e009-4bb9-bd44-a8eaaac1b3bc",
}
EXECUTIONS = {
    "dea59d65-2aa9-4f3a-896e-f26e03b8fe07",
    "dcc75ea7-32d5-443b-b9e4-852508e7c745",
    "b212ff22-677e-482d-88ff-c063906c02ac",
}
TASKS = {
    "dd324af3-3703-459f-b301-cb29917b03f9",
    "83943b17-e0bb-46cb-b005-2f665be18d9c",
    "1755cc4c-c447-40cf-ae84-fb87c36bfe58",
}
OPERATIONS = {
    "greenbook:create_draft:d62e8a5db07959840d3ef1e0645cb7b9",
}


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return str(value)


async def main() -> None:
    database_url = os.environ.get(
        "GREENBOOK_AGENT_DATABASE_URL",
        "postgresql://mindflow:mindflow@127.0.0.1:25432/mindflow_creator",
    )
    database_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(database_url)
    try:
        tables = await conn.fetch(
            """
            select table_name
            from information_schema.tables
            where table_schema = 'public'
              and (table_name like '%execution%' or table_name like '%operation%'
                   or table_name like '%observation%' or table_name like '%agent%'
                   or table_name like '%task%')
            order by table_name
            """
        )
        print(json.dumps({"tables": [row["table_name"] for row in tables]}, ensure_ascii=False))

        operation_rows = await conn.fetch(
            """
            select operation_id, execution_id, step_id, tool_name, status,
                   external_operation_id, receipt_id, idempotency_key,
                   resource_type, resource_id, semantic_action,
                   side_effect_started, reconciliation_needed,
                   reconcile_attempts, verified_status, verified_reason,
                   next_reconcile_at, created_at, updated_at, evidence
            from external_operation
            where operation_id = any($1::text[])
               or execution_id = any($2::text[])
            order by created_at
            """,
            list(OPERATIONS),
            list(RUNS | EXECUTIONS),
        )
        print(json.dumps({"external_operation": [dict(row) for row in operation_rows]}, default=str, ensure_ascii=False, indent=2))

        for table in ("execution", "execution_step", "execution_event", "execution_queue_message", "action_observation", "agent_run", "assistant_tasks"):
            exists = await conn.fetchval(
                "select exists(select 1 from information_schema.tables where table_schema='public' and table_name=$1)",
                table,
            )
            if not exists:
                continue
            columns = await conn.fetch(
                """
                select column_name
                from information_schema.columns
                where table_schema='public' and table_name=$1
                order by ordinal_position
                """,
                table,
            )
            names = [row["column_name"] for row in columns]
            predicates = []
            args: list[Any] = []
            for name in ("execution_id", "run_id", "task_id", "operation_id"):
                if name in names:
                    predicates.append(f'"{name}"::text = any(${len(args)+1}::text[])')
                    args.append(list(RUNS | EXECUTIONS | TASKS | OPERATIONS))
            if not predicates:
                continue
            selected = ", ".join(f'"{name}"' for name in names)
            query = f'select {selected} from "{table}" where ' + " or ".join(predicates) + " limit 100"
            rows = await conn.fetch(query, *args)
            print(json.dumps({table: [dict(row) for row in rows]}, default=str, ensure_ascii=False, indent=2))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
