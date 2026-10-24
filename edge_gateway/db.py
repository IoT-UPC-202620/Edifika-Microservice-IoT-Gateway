"""SQLite database setup (Peewee)."""

from pathlib import Path

from peewee import SqliteDatabase

database = SqliteDatabase(None)


def init_database(path: str) -> None:
    """Bind the shared database to a file and create the schema.

    WAL mode lets the MQTT thread, the scheduler and the REST workers use the
    database concurrently; a busy timeout absorbs short write contention.
    """
    from .models import ALL_MODELS

    if not database.is_closed():
        database.close()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    database.init(
        path,
        pragmas={
            "journal_mode": "wal",
            "foreign_keys": 1,
            "busy_timeout": 5000,
            "synchronous": "normal",
        },
    )
    with database.connection_context():
        database.create_tables(ALL_MODELS)
        migrate_schema()


def migrate_schema() -> None:
    from playhouse.migrate import SqliteMigrator, migrate
    from .models import ALL_MODELS

    migrator = SqliteMigrator(database)
    ops = []
    tables = database.get_tables()
    for model in ALL_MODELS:
        table_name = model._meta.table_name
        if table_name not in tables:
            continue
        existing_cols = {c.name for c in database.get_columns(table_name)}
        for field in model._meta.fields.values():
            if field.column_name not in existing_cols:
                ops.append(migrator.add_column(table_name, field.column_name, field))
    if ops:
        migrate(*ops)


def probe() -> dict:
    """Health probe: the database answers a trivial query."""
    with database.connection_context():
        database.execute_sql("SELECT 1")
    return {"ok": True}
