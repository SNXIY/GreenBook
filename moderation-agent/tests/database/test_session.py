from pathlib import Path

from database.session import ensure_sqlite_directory


def test_ensure_sqlite_directory_creates_parent(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "moderation.db"

    ensure_sqlite_directory(f"sqlite+aiosqlite:///{database_path.as_posix()}")

    assert database_path.parent.is_dir()
