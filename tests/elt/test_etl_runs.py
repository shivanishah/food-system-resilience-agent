"""Tests for the etl_runs audit trail, including the resume semantics that
let one domain be re-recorded under an existing pipeline run."""

from __future__ import annotations

from sqlalchemy import create_engine, select

from src.database.models import etl_runs, metadata
from src.elt.etl_runs import DomainRunResult, latest_pipeline_run_id, record_run


def _memory_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def _result(domain: str, status: str, silver_rows: int = 0, started_at: str = "2026-01-01T00:00:00+00:00"):
    return DomainRunResult(
        domain=domain, priority="core", status=status, coverage=None,
        rows_received=0, rows_inserted=0, bronze_rows=0, silver_rows=silver_rows,
        started_at=started_at,
    )


def test_record_run_writes_one_row_per_domain():
    engine = _memory_engine()
    record_run(engine, "run-1", _result("QCL", "success"))
    record_run(engine, "run-1", _result("TM", "success"))
    with engine.connect() as conn:
        assert len(conn.execute(select(etl_runs)).fetchall()) == 2


def test_record_run_replaces_a_domain_re_recorded_under_the_same_run():
    engine = _memory_engine()
    record_run(engine, "run-1", _result("TM", "failed"))
    record_run(engine, "run-1", _result("TM", "success", silver_rows=42))
    with engine.connect() as conn:
        rows = conn.execute(select(etl_runs.c.domain, etl_runs.c.status, etl_runs.c.silver_rows)).fetchall()
    assert rows == [("TM", "success", 42)]


def test_record_run_keeps_the_same_domain_in_a_different_run():
    engine = _memory_engine()
    record_run(engine, "run-1", _result("TM", "failed"))
    record_run(engine, "run-2", _result("TM", "success"))
    with engine.connect() as conn:
        assert len(conn.execute(select(etl_runs)).fetchall()) == 2


def test_latest_pipeline_run_id_returns_none_on_an_empty_audit_trail():
    assert latest_pipeline_run_id(_memory_engine()) is None


def test_latest_pipeline_run_id_picks_the_most_recently_started_run():
    engine = _memory_engine()
    record_run(engine, "run-old", _result("QCL", "success", started_at="2026-01-01T00:00:00+00:00"))
    record_run(engine, "run-new", _result("QCL", "success", started_at="2026-02-01T00:00:00+00:00"))
    assert latest_pipeline_run_id(engine) == "run-new"
