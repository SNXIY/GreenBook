import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config


def migration_config(database_path: Path) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    return config


def table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def column_names(database_path: Path, table: str) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def test_migrations_create_the_complete_platform_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh-migration.db"
    config = migration_config(database_path)

    command.upgrade(config, "head")
    command.check(config)

    assert {
        "moderation_task",
        "moderation_action_log",
        "moderation_policy",
        "moderation_review_case",
        "moderation_signal",
        "moderation_callback_outbox",
    }.issubset(table_names(database_path))
    assert {
        "adversarial_review",
        "policy_rag",
        "evidence_review",
        "trace_id",
    }.issubset(
        column_names(database_path, "moderation_task")
    )
    assert {
        "applicability_conditions",
        "exclusion_conditions",
        "violation_examples",
        "safe_examples",
        "severity",
        "suggested_actions",
        "tags",
        "effective_at",
        "expires_at",
    }.issubset(column_names(database_path, "moderation_policy"))


def test_second_migration_backfills_existing_tasks_and_review_cases(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "upgrade-migration.db"
    config = migration_config(database_path)
    command.upgrade(config, "0001")

    task_id = uuid4().hex
    case_id = uuid4().hex
    policy_id = uuid4().hex
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO moderation_policy (
                id, code, title, description, risk_type, default_action,
                platform, enabled, priority, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                "LEGACY-001",
                "Legacy policy",
                "Legacy privacy rule",
                "PRIVACY",
                "REJECT",
                "default",
                1,
                100,
                1,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO moderation_task (
                id, thread_id, content, metadata, platform, status, risk_type,
                version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                "legacy-thread",
                "Legacy content",
                "{}",
                "default",
                "COMPLETED",
                "ABUSE",
                1,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO moderation_review_case (
                id, original_task_id, content, normalized_content, content_hash,
                platform, agent_risk_type, agent_action, final_action,
                reviewer_id, matched_policy_ids, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                task_id,
                "Legacy content",
                "legacy content",
                "legacy-hash",
                "default",
                "ABUSE",
                "REJECT",
                "PASS",
                "legacy-reviewer",
                "[]",
                now,
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        migrated_task = connection.execute(
            "SELECT content_type, final_risk_type, adversarial_review, "
            "evidence_review, trace_id "
            "FROM moderation_task WHERE id = ?",
            (task_id,),
        ).fetchone()
        migrated_case = connection.execute(
            "SELECT final_risk_type FROM moderation_review_case WHERE id = ?",
            (case_id,),
        ).fetchone()
        migrated_policy = connection.execute(
            "SELECT severity, suggested_actions, effective_at FROM moderation_policy WHERE id = ?",
            (policy_id,),
        ).fetchone()

    assert migrated_task == ("TEXT", "ABUSE", None, None, task_id)
    assert migrated_case == ("ABUSE",)
    assert migrated_policy is not None
    assert migrated_policy[0] == "CRITICAL"
    assert json.loads(migrated_policy[1]) == ["REJECT"]
    assert migrated_policy[2] is not None
