"""DuckDB sessions over the SQLite database, for Gold-layer aggregation.

Phase 3 hard constraint: this machine has 8 GB of RAM and pandas full-table
loads were OS-killed twice in Phase 2. All Gold aggregation therefore runs in
DuckDB (which spills to ``temp_directory``) against the SQLite file attached
through DuckDB's ``sqlite`` extension; only small aggregated results cross
into pandas.

Reads use a ``READ_ONLY`` attach. Writes open a separate read-write session
and go through :func:`replace_sqlite_table`, which refuses Bronze/Silver
targets -- Gold scripts drop and recreate only their own tables.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import duckdb
from pydantic import BaseModel

from src.database.connection import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

SQLITE_ALIAS = "fs"
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


class DuckDBSettings(BaseModel):
    """Per-session DuckDB resource limits (``config/gold.yaml -> duckdb``)."""

    memory_limit: str = "3GB"
    threads: int = 4
    temp_directory: str = "~/duckdb_tmp"
    preserve_insertion_order: bool = False


class ProtectedTableError(ValueError):
    """Raised when Gold code tries to replace a Bronze or Silver table."""


def _configure(con: duckdb.DuckDBPyConnection, settings: DuckDBSettings) -> None:
    temp_dir = Path(settings.temp_directory).expanduser()
    temp_dir.mkdir(parents=True, exist_ok=True)
    # SET does not accept prepared parameters; every value here comes from a
    # typed config model, and the strings are quoted defensively.
    memory_limit = settings.memory_limit.replace("'", "")
    temp_path = str(temp_dir).replace("'", "")
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET threads={int(settings.threads)}")
    con.execute(f"SET temp_directory='{temp_path}'")
    con.execute(f"SET preserve_insertion_order={'true' if settings.preserve_insertion_order else 'false'}")


@contextmanager
def duckdb_session(
    settings: DuckDBSettings,
    db_path: Path = DEFAULT_DB_PATH,
    *,
    read_only: bool = True,
) -> Generator[duckdb.DuckDBPyConnection, None, None]:
    """In-memory DuckDB with the SQLite database attached as ``fs``.

    Tables are addressed as ``fs.<table>``. ``read_only=False`` is for the
    final Gold write only; keep analytical queries on a read-only session.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    con = duckdb.connect()
    try:
        _configure(con, settings)
        con.execute("LOAD sqlite")
        mode = ", READ_ONLY" if read_only else ""
        db_literal = str(db_path).replace("'", "''")
        con.execute(f"ATTACH '{db_literal}' AS {SQLITE_ALIAS} (TYPE sqlite{mode})")
        logger.info("duckdb session opened on %s (read_only=%s)", db_path, read_only)
        yield con
    finally:
        con.close()


def validate_gold_table_name(table: str, protected_prefixes: list[str]) -> None:
    """Refuse malformed identifiers and any Bronze/Silver target."""
    if not _IDENTIFIER.match(table):
        raise ValueError(f"Invalid table name {table!r}")
    if any(table.startswith(prefix) for prefix in protected_prefixes):
        raise ProtectedTableError(f"Refusing to replace protected table {table!r}")


def replace_sqlite_table(
    con: duckdb.DuckDBPyConnection,
    table: str,
    source_sql: str,
    protected_prefixes: list[str],
) -> int:
    """Drop and recreate ``fs.<table>`` from ``source_sql``; return its row count.

    ``con`` must come from a ``read_only=False`` session. ``source_sql`` is a
    SELECT over relations registered on ``con`` (never user input).
    """
    validate_gold_table_name(table, protected_prefixes)
    con.execute(f"DROP TABLE IF EXISTS {SQLITE_ALIAS}.{table}")
    con.execute(f"CREATE TABLE {SQLITE_ALIAS}.{table} AS {source_sql}")
    (rows,) = con.execute(f"SELECT COUNT(*) FROM {SQLITE_ALIAS}.{table}").fetchone()
    logger.info("wrote %s: %d rows", table, rows)
    return rows
