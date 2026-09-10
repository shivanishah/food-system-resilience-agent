"""Tests for engine setup and the index backfill in init_db."""

from __future__ import annotations

from sqlalchemy import Index, inspect, text

from src.database.connection import get_engine, init_db
from src.database.models import BRONZE_TABLES, metadata


def test_init_db_creates_declared_tables(tmp_path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    assert "bronze_qcl" in inspect(engine).get_table_names()


def test_get_engine_enables_wal_mode(tmp_path):
    engine = get_engine(tmp_path / "test.db")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"


def test_init_db_backfills_an_index_added_after_the_table_existed(tmp_path):
    """create_all only builds a table's indexes when it builds the table, so
    an index declared later must still be created on the existing table."""
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)

    table = BRONZE_TABLES["QCL"]
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX idx_bronze_qcl_item_code"))
    assert "idx_bronze_qcl_item_code" not in {
        idx["name"] for idx in inspect(engine).get_indexes(table.name)
    }

    init_db(engine)
    assert "idx_bronze_qcl_item_code" in {
        idx["name"] for idx in inspect(engine).get_indexes(table.name)
    }


def test_init_db_is_idempotent_when_every_index_already_exists(tmp_path):
    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    init_db(engine)  # must not raise "index already exists"
    assert isinstance(metadata.sorted_tables[0].indexes, set)
    assert all(isinstance(i, Index) for t in metadata.sorted_tables for i in t.indexes)


def test_checkpoint_wal_leaves_the_database_readable(tmp_path):
    from sqlalchemy import select

    from src.database.connection import checkpoint_wal
    from src.database.models import BRONZE_TABLES

    engine = get_engine(tmp_path / "test.db")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(
            BRONZE_TABLES["QCL"].insert(),
            [{"natural_key": "a", "domain_code": "QCL", "retrieved_at": "t",
              "pipeline_run_id": "r", "source_domain": "FAOSTAT_QCL"}],
        )
    checkpoint_wal(engine)
    with engine.connect() as conn:
        assert len(conn.execute(select(BRONZE_TABLES["QCL"])).fetchall()) == 1
