"""SQLite engine/session management for the Bronze -> Silver database."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text

from src.database.models import metadata

DEFAULT_DB_PATH = Path("data/food_system.db")


def get_engine(db_path: Path = DEFAULT_DB_PATH) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _set_wal_mode(dbapi_connection, _connection_record) -> None:
        # WAL mode lets a read-only connection (e.g. a notebook, or a
        # monitoring query) query the database concurrently with the
        # pipeline's writes instead of hitting "database is locked" --
        # confirmed live: a concurrent read killed a domain's write mid-run
        # under the default rollback-journal mode.
        dbapi_connection.execute("PRAGMA journal_mode=WAL")
        dbapi_connection.execute("PRAGMA busy_timeout=30000")

    return engine


def init_db(engine: Engine) -> None:
    """Create every declared table that doesn't already exist, then any
    declared index missing from a table that already existed.

    ``create_all`` builds a table's indexes only when it builds the table, so
    an index added to ``src/database/models.py`` after a database was first
    populated would otherwise never appear -- confirmed live: the
    ``idx_bronze_*_item_code`` indexes were absent from a 4 GB database,
    leaving the item-at-a-time Silver transform for TM doing a full scan of a
    6M-row table per commodity.
    """
    metadata.create_all(engine)
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in existing_indexes:
                index.create(bind=engine)


def checkpoint_wal(engine: Engine) -> None:
    """Fold the write-ahead log back into the main database file.

    A domain that rewrites a large Silver table leaves a WAL of comparable
    size (confirmed live: re-running TM's Silver transform left an 838 MB
    WAL). Every subsequent read then has to consult those WAL frames, and on
    a memory-constrained machine the reporting stage that follows -- which
    scans every Bronze table -- was killed by the OS twice in a row until the
    log was checkpointed first.
    """
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
