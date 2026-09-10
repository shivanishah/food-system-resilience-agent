"""One auditable etl_runs row per (pipeline_run_id, domain)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, delete, insert, select

from src.database.models import etl_runs
from src.elt.extract import DomainCoverage


@dataclass
class DomainRunResult:
    domain: str
    priority: str
    status: str  # success | partial | failed
    coverage: DomainCoverage | None
    rows_received: int
    rows_inserted: int
    bronze_rows: int
    silver_rows: int
    started_at: str
    error_message: str | None = None


def record_run(engine: Engine, pipeline_run_id: str, result: DomainRunResult) -> None:
    cov = result.coverage
    row = {
        "pipeline_run_id": pipeline_run_id,
        "domain": result.domain,
        "priority": result.priority,
        "status": result.status,
        "requested_start_year": cov.requested_start_year if cov else None,
        "requested_end_year": cov.requested_end_year if cov else None,
        "actual_min_year": cov.actual_min_year if cov else None,
        "actual_max_year": cov.actual_max_year if cov else None,
        "missing_requested_years": json.dumps(cov.missing_requested_years) if cov else None,
        "requested_item_count": cov.requested_item_count if cov else None,
        "retrieved_item_count": cov.retrieved_item_count if cov else None,
        "requested_element_count": len(cov.requested_elements) if cov and cov.requested_elements else None,
        "retrieved_element_count": len(cov.resolved_element_codes) if cov else None,
        "country_count": cov.country_count if cov else None,
        "partner_count": cov.partner_count if cov else None,
        "rows_received": result.rows_received,
        "rows_inserted": result.rows_inserted,
        "bronze_rows": result.bronze_rows,
        "silver_rows": result.silver_rows,
        "started_at": result.started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "error_message": result.error_message,
    }
    with engine.begin() as conn:
        # Replace rather than append: a resumed run (see ``--resume`` in
        # src/elt/pipeline.py) re-records a domain under the *same*
        # pipeline_run_id, and two rows for one (run, domain) would make the
        # audit trail ambiguous about which attempt actually stands.
        conn.execute(
            delete(etl_runs).where(
                etl_runs.c.pipeline_run_id == pipeline_run_id,
                etl_runs.c.domain == result.domain,
            )
        )
        conn.execute(insert(etl_runs), [row])


def latest_pipeline_run_id(engine: Engine) -> str | None:
    """The ``pipeline_run_id`` of the most recently started run, or ``None``
    if nothing has been recorded yet. Used by ``--resume`` so re-running a
    subset of domains lands in the existing run's audit trail and report
    instead of starting a new, partial one."""
    with engine.connect() as conn:
        return conn.execute(
            select(etl_runs.c.pipeline_run_id).order_by(etl_runs.c.started_at.desc()).limit(1)
        ).scalar_one_or_none()
