import asyncio
import asyncpg


async def main() -> None:
    conn = await asyncpg.connect("postgresql://mindflow:mindflow@127.0.0.1:25432/mindflow_creator")
    rows = await conn.fetch("SELECT conversation_id::text, active_post_id, created_at, updated_at FROM assistant_conversations WHERE user_id=$1 AND active_post_id IS NOT NULL ORDER BY updated_at DESC", "6")
    print([dict(row) for row in rows])
    await conn.close()


asyncio.run(main())
