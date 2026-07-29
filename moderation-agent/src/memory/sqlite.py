from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.store.memory import InMemoryStore

from core.settings import settings


def get_sqlite_saver() -> AbstractAsyncContextManager[AsyncSqliteSaver]:
    """Initialize and return a SQLite saver instance."""
    Path(settings.SQLITE_DB_PATH).expanduser().parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(settings.SQLITE_DB_PATH)


@asynccontextmanager
async def get_sqlite_store():
    """Yield the in-memory store used with the local SQLite checkpointer."""
    yield InMemoryStore()
