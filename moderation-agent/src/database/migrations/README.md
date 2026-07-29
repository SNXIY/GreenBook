# Database migrations

Run `alembic upgrade head` before starting the service. The migration URL is
read from `MODERATION_DATABASE_URL`, falling back to `sqlalchemy.url` in
`alembic.ini`. `MODERATION_AUTO_CREATE_SCHEMA=true` can create missing tables
for a fresh local database, but `create_all()` does not add columns to existing
tables and is not a replacement for migrations.

The current moderation schema head is `0006`, which adds the bounded Evidence Reviewer audit
summary. Reviewer iteration details remain append-only `EVIDENCE_REVIEWED` action-log events.

The repository-local SQLite fallback is `data/databases/moderation.db`.

For a database created by an older version with the four moderation tables but
without an Alembic version, first run `alembic stamp 0001`, then
`alembic upgrade head`.
