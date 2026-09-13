"""Phase 2 orchestrator: extract -> Bronze -> Silver for all 9 FAOSTAT
domains, core domains first. Each domain's failure is isolated (caught, and
recorded in ``etl_runs``) so one bad domain never rolls back another's
successful load. A **core** domain (QCL/TM/FBS/FS/CAHD) ending ``failed``
blocks Phase 2 (non-zero exit); a **supporting** domain (CP/GT/RL/RFM) never
does.

Run the whole thing with::

    uv run python -m src.elt.pipeline

A full run is measured in hours (TM's bilateral sweep dominates), so the
domain loop is also resumable -- re-running only what actually needs redoing,
inside the *existing* run's audit trail::

    # redo one domain that failed, in the existing run
    uv run python -m src.elt.pipeline --resume --domains RFM

    # redo a Silver transform from Bronze already fetched (no API data pull)
    uv run python -m src.elt.pipeline --resume --domains TM --reuse-bronze TM

    # skip the domain loop entirely; just rebuild Silver reference tables,
    # indexes, views, validation and the data-quality report
    uv run python -m src.elt.pipeline --resume --domains none
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import Engine, select

from src.api.client import FAOSTATClient
from src.config import configure_logging
from src.database.connection import checkpoint_wal, get_engine, init_db
from src.database.models import etl_runs
from src.elt.config import PipelineConfig, load_harmonisation_config, load_pipeline_config
from src.elt.etl_runs import DomainRunResult, latest_pipeline_run_id, record_run
from src.elt.extract import DomainCoverage, coverage_for_existing_bronze, extract_domain
from src.elt.qcl_items import refresh_qcl_crop_items
from src.elt.quality_report import generate_all
from src.elt.scope_gaps import (
    ScopeGap,
    classify_missing_items,
    domains_with_recorded_gaps,
    load_gaps,
    record_gaps,
)
from src.elt.silver import DOMAIN_TRANSFORMS, refresh_silver_commodity, refresh_silver_country
from src.elt.structural import create_indexes, create_views
from src.validation.silver_schemas import validate_silver

logger = logging.getLogger(__name__)

NO_DOMAINS = "none"


def _determine_status(
    coverage: DomainCoverage, bronze_rows: int, gaps: Sequence[ScopeGap] = ()
) -> str:
    """success: real data, and every shortfall explained. partial: something
    requested did not arrive and could not be shown to be a source gap.
    failed: nothing landed at all.

    A shortfall is only forgiven once it has been *established* live as
    genuine source behaviour -- the item is absent from FAO's own dimension,
    or has no observation in the requested window, or none at all (see
    ``src/elt/scope_gaps.py``). Anything unproven still reads `partial`,
    because the one API failure mode that cost this project real data was
    silent: CAHD returned 2 of its 8 requested items, including the Cost of a
    Healthy Diet the whole affordability analysis rests on, and the run
    reported success.
    """
    if bronze_rows == 0:
        return "failed"
    if coverage.missing_elements:
        return "partial"
    if any(gap.counts_against_status for gap in gaps):
        return "partial"
    return "success"


def _classify_gaps(
    client: FAOSTATClient, domain: str, spec, coverage: DomainCoverage, pipeline_cfg: PipelineConfig
) -> list[ScopeGap]:
    """Establish why anything requested is missing. A failure to classify is
    never treated as "explained" -- it degrades to `unverified`, which keeps
    counting against the domain's status."""
    missing = sorted(set(coverage.requested_item_codes) - set(coverage.retrieved_item_codes))
    to_classify = missing + list(coverage.items_absent_from_dimension)
    if not to_classify:
        return []
    try:
        return classify_missing_items(
            client,
            domain,
            missing_codes=to_classify,
            dimension_codes=set(coverage.dimension_item_codes),
            retrieved_codes=set(coverage.retrieved_item_codes),
            start_year=pipeline_cfg.start_year,
            end_year=pipeline_cfg.end_year,
            bilateral=spec.bilateral,
        )
    except Exception as exc:  # noqa: BLE001 - an unclassifiable gap must not read as explained
        logger.warning("Domain %s: could not classify missing items %s: %s", domain, to_classify, exc)
        return [ScopeGap(domain, code, "unverified", f"classification failed: {exc}") for code in to_classify]


def _recorded_runs(engine: Engine, pipeline_run_id: str) -> pd.DataFrame:
    """Every ``etl_runs`` row for this run id, including domains recorded by
    an earlier invocation of the same (resumed) run."""
    return pd.read_sql(select(etl_runs).where(etl_runs.c.pipeline_run_id == pipeline_run_id), engine)


def _backfill_missing_gap_records(
    client: FAOSTATClient, engine: Engine, run_id: str, pipeline_cfg: PipelineConfig
) -> None:
    """Work out the scope gaps for any domain in this run that has none
    recorded yet.

    A run that touches only some domains would otherwise leave the report
    showing that slice's gaps alone -- which is how a regenerated report came
    to list TM's 8 untracked commodities and silently drop QCL 839 and
    GT 6966. Everything needed is cheap: the requested scope comes from
    config, the retrieved codes from a ``SELECT DISTINCT`` over Bronze, and
    only genuinely missing items are ever probed.
    """
    already = domains_with_recorded_gaps(engine, run_id)
    for domain, spec in pipeline_cfg.domains.items():
        if domain in already:
            continue
        try:
            result = coverage_for_existing_bronze(client, engine, domain, spec, pipeline_cfg)
            if result.bronze_rows == 0:
                continue  # nothing was ingested; there is no shortfall to explain
            record_gaps(engine, run_id, domain, _classify_gaps(client, domain, spec, result.coverage, pipeline_cfg))
        except Exception:  # noqa: BLE001 - a reporting gap must not fail the run
            logger.exception("Could not work out scope gaps for domain %s", domain)


def run_pipeline(
    domains: Sequence[str] | None = None,
    reuse_bronze: Sequence[str] = (),
    pipeline_run_id: str | None = None,
) -> int:
    """Run Phase 2 end-to-end.

    ``domains`` restricts the extract/Silver loop (default: all configured
    domains); an empty sequence runs no domain and only rebuilds the
    post-domain artefacts. ``reuse_bronze`` names domains whose Bronze is
    already complete, so only their Silver transform re-runs and no
    observations are re-requested from the API. ``pipeline_run_id`` adopts an
    existing run's id instead of minting a new one.

    Returns a process exit code (0 = ok, 2 = a core domain failed, 3 = a hard
    Silver validation check failed).
    """
    configure_logging()
    pipeline_cfg = load_pipeline_config()
    harmonisation = load_harmonisation_config()
    engine = get_engine()
    init_db(engine)

    selected = list(pipeline_cfg.domains) if domains is None else list(domains)
    unknown = [d for d in list(selected) + list(reuse_bronze) if d not in pipeline_cfg.domains]
    if unknown:
        raise ValueError(f"Unknown domain(s): {unknown}; configured: {sorted(pipeline_cfg.domains)}")
    reuse_bronze = {d for d in reuse_bronze if d in selected}

    # Core domains first (failure-blocking priority), but TM runs last
    # regardless of priority: it's the full reporter x partner bilateral
    # sweep and empirically the slowest domain by a wide margin (see
    # src/elt/extract.py's BILATERAL_CHUNK_SIZE comment) -- nothing else
    # depends on it, so running it last surfaces every other domain's
    # result sooner instead of blocking behind it.
    def _domain_sort_key(d: str) -> tuple[int, int]:
        if d == "TM":
            return (2, 0)
        return (0 if pipeline_cfg.domains[d].priority == "core" else 1, 0)

    domain_order = sorted(selected, key=_domain_sort_key)
    run_results: dict[str, DomainRunResult] = {}
    all_conflicts: dict[str, list] = {}

    with FAOSTATClient() as client:
        client.authenticate()
        run_id = pipeline_run_id or client.pipeline_run_id
        logger.info(
            "Phase 2 run %s: domains=%s reuse_bronze=%s",
            run_id, domain_order or "(none)", sorted(reuse_bronze) or "(none)",
        )
        refresh_qcl_crop_items(client)
        area_reference = {row["Country Code"]: row for row in client.get_areas("QCL").data}

        for domain in domain_order:
            spec = pipeline_cfg.domains[domain]
            started_at = datetime.now(UTC).isoformat()
            logger.info("=== Domain %s (%s) ===", domain, spec.priority)
            try:
                if domain in reuse_bronze:
                    logger.info("Bronze[%s]: reusing existing rows, no API data pull", domain)
                    extract_result = coverage_for_existing_bronze(client, engine, domain, spec, pipeline_cfg)
                else:
                    extract_result = extract_domain(client, engine, run_id, domain, spec, pipeline_cfg)
                logger.info("Bronze[%s]: %d rows received, %d inserted", domain,
                            extract_result.rows_received, extract_result.bronze_rows)
                silver_rows, conflicts = DOMAIN_TRANSFORMS[domain](engine, harmonisation)
                logger.info("Silver[%s]: %d rows written", domain, silver_rows)
                all_conflicts[domain] = conflicts
                gaps = _classify_gaps(client, domain, spec, extract_result.coverage, pipeline_cfg)
                record_gaps(engine, run_id, domain, gaps)
                status = _determine_status(extract_result.coverage, extract_result.bronze_rows, gaps)
                run_results[domain] = DomainRunResult(
                    domain=domain, priority=spec.priority, status=status,
                    coverage=extract_result.coverage, rows_received=extract_result.rows_received,
                    rows_inserted=extract_result.bronze_rows, bronze_rows=extract_result.bronze_rows,
                    silver_rows=silver_rows, started_at=started_at,
                )
            except Exception as exc:  # noqa: BLE001 - isolate one domain's failure from the rest
                logger.exception("Domain %s failed", domain)
                run_results[domain] = DomainRunResult(
                    domain=domain, priority=spec.priority, status="failed", coverage=None,
                    rows_received=0, rows_inserted=0, bronze_rows=0, silver_rows=0,
                    started_at=started_at, error_message=str(exc),
                )
            # Written immediately, not batched until every domain finishes:
            # a later domain crashing (confirmed live -- TM was killed
            # mid-Silver on the 2026-09-06 run) must not erase the audit
            # trail for domains that already succeeded.
            record_run(engine, run_id, run_results[domain])
            # Fold this domain's writes back into the database before the next
            # one starts: a large Silver rewrite leaves a WAL of comparable
            # size, and on a near-full disk several of those in a row is how a
            # run ends up killed (see src/database/connection.py).
            checkpoint_wal(engine)

        _backfill_missing_gap_records(client, engine, run_id, pipeline_cfg)
        refresh_silver_country(engine, harmonisation, area_reference)
        refresh_silver_commodity(engine, harmonisation)

    # Before the reporting stage, which scans every Bronze table: a domain
    # that rewrote a large Silver table has left a WAL of comparable size,
    # and reading through it is what got this stage OS-killed twice.
    checkpoint_wal(engine)
    create_indexes(engine)
    create_views(engine)

    validation_report = validate_silver(engine, pipeline_cfg, harmonisation)
    generate_all(
        engine, run_id, pipeline_cfg, harmonisation, all_conflicts,
        validation_report.to_rows(), load_gaps(engine, run_id),
    )

    # Judged on every domain recorded under this run id, not only the ones
    # this invocation touched, so a resumed run still reports the whole of
    # Phase 2 rather than the slice it happened to redo.
    recorded = _recorded_runs(engine, run_id)
    for _, r in recorded.sort_values("domain").iterrows():
        logger.info(
            "Domain %s [%s]: status=%s bronze_rows=%s silver_rows=%s",
            r["domain"], r["priority"], r["status"], r["bronze_rows"], r["silver_rows"],
        )

    missing = sorted(set(pipeline_cfg.domains) - set(recorded["domain"]))
    if missing:
        logger.warning("No etl_runs record for domain(s) %s under run %s", missing, run_id)

    core_failed = sorted(
        recorded.loc[(recorded["priority"] == "core") & (recorded["status"] == "failed"), "domain"]
    )
    if core_failed:
        logger.error("Phase 2 blocked: core domain(s) failed: %s", core_failed)
        return 2
    if validation_report.hard_failures:
        logger.error(
            "Phase 2 blocked: hard Silver validation failures: %s",
            [(f.table, f.check, f.n_violations) for f in validation_report.hard_failures],
        )
        return 3
    logger.info("Phase 2 complete: pipeline_run_id=%s", run_id)
    return 0


def _parse_domain_list(raw: str | None) -> list[str] | None:
    """``None`` -> every configured domain; ``"none"`` -> no domain at all."""
    if raw is None:
        return None
    if raw.strip().lower() == NO_DOMAINS:
        return []
    return [d.strip().upper() for d in raw.split(",") if d.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--domains",
        help=f"Comma-separated domains to run (default: all). Use '{NO_DOMAINS}' to skip the domain "
             "loop and only rebuild reference tables, indexes, views, validation and reports.",
    )
    parser.add_argument(
        "--reuse-bronze",
        help="Comma-separated domains whose Bronze rows are already complete: their Silver transform "
             "re-runs but no observations are re-requested from the API.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Record results under the most recent existing pipeline_run_id instead of a new one, so a "
             "re-run of part of Phase 2 lands in the same audit trail and report.",
    )
    parser.add_argument(
        "--pipeline-run-id",
        help="Explicit pipeline_run_id to record under (overrides --resume).",
    )
    args = parser.parse_args(argv)

    run_id = args.pipeline_run_id
    if run_id is None and args.resume:
        run_id = latest_pipeline_run_id(get_engine())
        if run_id is None:
            parser.error("--resume: no previous run found in etl_runs")

    return run_pipeline(
        domains=_parse_domain_list(args.domains),
        reuse_bronze=_parse_domain_list(args.reuse_bronze) or (),
        pipeline_run_id=run_id,
    )


if __name__ == "__main__":
    sys.exit(main())
