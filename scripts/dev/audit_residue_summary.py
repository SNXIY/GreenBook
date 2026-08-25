"""Read-only concise residue audit; does not mutate PostgreSQL."""

from __future__ import annotations

import asyncio
import json

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(
        "postgresql://mindflow:mindflow@127.0.0.1:25432/mindflow_creator"
    )
    try:
        unknown = await connection.fetch(
            """
            select operation_id::text, execution_id::text, status,
                   reconciliation_needed, reconcile_attempts,
                   verified_status, created_at, updated_at
            from external_operation
            where evidence::text like '%RESULT_UNKNOWN%'
            order by created_at
            """
        )
        running = await connection.fetch(
            """
            select task_id::text, conversation_id::text, user_id, goal,
                   status, phase, requires_confirmation, confirmation_state,
                   active_execution_id, last_action, last_error,
                   created_at, updated_at
            from assistant_tasks
            where upper(status) = 'RUNNING'
            order by updated_at
            """
        )
        active_execution_ids = {
            str(row[0])
            for row in await connection.fetch(
                "select execution_id from execution where upper(status) in ('RUNNING','QUEUED','WAITING')"
            )
        }
        print(
            json.dumps(
                {
                    "result_unknown_count": len(unknown),
                    "result_unknown_unresolved_count": sum(
                        1
                        for row in unknown
                        if row["reconciliation_needed"]
                        or str(row["verified_status"] or "").upper()
                        not in {"VERIFIED_COMPLETED", "VERIFIED_FAILED"}
                    ),
                    "result_unknown": [dict(row) for row in unknown],
                    "running_task_count": len(running),
                    "running_task_counts_by_user": {
                        str(user): sum(1 for row in running if str(row["user_id"]) == str(user))
                        for user in sorted({row["user_id"] for row in running})
                    },
                    "running_tasks": [
                        {
                            **dict(row),
                            "active_execution_present": bool(
                                row["active_execution_id"]
                                and str(row["active_execution_id"]) in active_execution_ids
                            ),
                        }
                        for row in running
                    ],
                },
                default=str,
            )
        )
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
