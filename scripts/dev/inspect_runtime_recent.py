"""Read-only recent runtime projection probe for unattended evaluation."""

from __future__ import annotations

import asyncio
import json

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(
        "postgresql://mindflow:mindflow@127.0.0.1:25432/mindflow_creator"
    )
    try:
        tables = {}
        for table in ("assistant_runs", "assistant_tasks", "execution"):
            rows = await connection.fetch(
                """
                select column_name, data_type
                from information_schema.columns
                where table_name = $1
                order by ordinal_position
                """,
                table,
            )
            tables[table] = [dict(row) for row in rows]
        recent = await connection.fetch(
            """
            select run_id::text, conversation_id::text, status, error_code, created_at
            from assistant_runs
            order by created_at desc
            limit 30
            """
        )
        print(json.dumps({"tables": tables, "recent_runs": [dict(row) for row in recent]}, default=str))
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
