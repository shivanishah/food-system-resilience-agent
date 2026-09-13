"""Classify *why* a requested item never arrived, with live evidence.

Phase 2's status rule is deliberately strict: a domain that retrieves fewer
items than it requested is recorded ``partial``, never ``success``. That rule
exists because FAOSTAT's ``item=`` filter fails **silently** (see
``PHASE2.md`` section 15) -- it once cost this project the Cost of a Healthy
Diet while the run still reported success.

But "didn't arrive" has several causes, and only one of them is a defect:

``absent_from_dimension``
    FAO's own item dimension for that domain does not list the code, so the
    request should never have included it. Not a data gap -- a request bug,
    fixed by intersecting the configured scope with live discovery.
``out_of_window``
    The item exists and has observations, but none inside the requested
    years (e.g. a series FAO discontinued). A real finding, not a defect.
``absent_at_source``
    The item is in the dimension but FAO publishes no observation for it at
    all -- an empty placeholder category.
``filter_bug`` / ``unverified``
    Could not be shown to be a genuine source gap. These still count against
    the domain's status, so a regression can never hide behind this module.

Only the first three stop counting against status, and each one is written to
``data_quality/scope_gaps.csv`` with the probe that established it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, delete, insert, select

from src.api.client import FAOSTATClient
from src.database.models import scope_gaps as scope_gaps_table
from src.elt.extract import parse_year_code

logger = logging.getLogger(__name__)

# Classifications that are confirmed source behaviour rather than a defect,
# and therefore do not make a domain `partial`.
EXPLAINED = frozenset({"absent_from_dimension", "out_of_window", "absent_at_source"})


@dataclass
class ScopeGap:
    """One requested item that did not arrive, and the reason established."""

    domain: str
    item_code: str
    classification: str
    detail: str

    @property
    def counts_against_status(self) -> bool:
        return self.classification not in EXPLAINED


def _same_width_control(code: str, retrieved_codes: set[str]) -> str | None:
    """A retrieved item code of the same character width as ``code``.

    The item-filter bug is *width*-dependent: FAOSTAT compares the requested
    code against a fixed-width internal key, so a code longer than that width
    silently matches nothing. A sibling of the same width that *did* come
    back is therefore the control that tells an empty result apart from a
    swallowed one.
    """
    return next((c for c in sorted(retrieved_codes) if len(c) == len(code)), None)


def classify_missing_items(
    client: FAOSTATClient,
    domain: str,
    missing_codes: list[str],
    dimension_codes: set[str],
    retrieved_codes: set[str],
    start_year: int,
    end_year: int,
    bilateral: bool = False,
) -> list[ScopeGap]:
    """Establish, live, why each of ``missing_codes`` did not arrive.

    Bilateral domains (TM/RFM) are classified on dimension membership alone:
    the probes below request an item across every area, which for a
    reporter x partner domain is an unbounded query. Anything else there is
    left ``unverified`` and keeps counting against status.
    """
    gaps: list[ScopeGap] = []
    for code in sorted(missing_codes):
        if code not in dimension_codes:
            gaps.append(
                ScopeGap(domain, code, "absent_from_dimension",
                         f"not listed in {domain}'s own item dimension")
            )
            continue

        if bilateral:
            gaps.append(
                ScopeGap(domain, code, "unverified",
                         "in the item dimension; not probed further because an unfiltered "
                         "probe on a bilateral domain is an unbounded query")
            )
            continue

        # Probe 1: same item filter, no year bound. Rows here prove both that
        # the filter works for this code and that the gap is the year window.
        rows = client.get_data(domain, item=[code]).rows
        if rows:
            years = sorted({y for y, _t, _s in (parse_year_code(str(o.year_code)) for o in rows) if y})
            in_window = [y for y in years if start_year <= y <= end_year]
            if in_window:
                gaps.append(
                    ScopeGap(domain, code, "unverified",
                             f"returned {len(rows)} rows including years {in_window} when queried "
                             "on its own -- it should have been ingested")
                )
            else:
                gaps.append(
                    ScopeGap(domain, code, "out_of_window",
                             f"published {min(years)}-{max(years)} ({len(rows)} rows); "
                             f"nothing in the requested {start_year}-{end_year}")
                )
            continue

        # Probe 2: zero rows could mean "no data" or "the filter ate it".
        # A retrieved sibling of the same code width settles which.
        control = _same_width_control(code, retrieved_codes)
        if control is None:
            gaps.append(
                ScopeGap(domain, code, "unverified",
                         f"no rows, and no retrieved {len(code)}-character item code available "
                         "as a control for the width-dependent item-filter bug")
            )
            continue
        control_rows = client.get_data(domain, item=[control]).rows
        if control_rows:
            gaps.append(
                ScopeGap(domain, code, "absent_at_source",
                         f"no observation in any year or country; the item filter is sound at this "
                         f"code width (control item {control} returned {len(control_rows)} rows)")
            )
        else:
            gaps.append(
                ScopeGap(domain, code, "filter_bug",
                         f"no rows, and control item {control} of the same width also returned none "
                         "-- the item filter is not trustworthy at this width")
            )
    for gap in gaps:
        logger.info("Scope gap %s/%s: %s -- %s", gap.domain, gap.item_code, gap.classification, gap.detail)
    return gaps


def record_gaps(engine: Engine, pipeline_run_id: str, domain: str, gaps: Sequence[ScopeGap]) -> None:
    """Replace this (run, domain)'s recorded gaps. Replace rather than append,
    so re-running a domain in the same run leaves one unambiguous record."""
    recorded_at = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            delete(scope_gaps_table).where(
                scope_gaps_table.c.pipeline_run_id == pipeline_run_id,
                scope_gaps_table.c.domain == domain,
            )
        )
        if gaps:
            conn.execute(
                insert(scope_gaps_table),
                [
                    {
                        "pipeline_run_id": pipeline_run_id,
                        "domain": g.domain,
                        "item_code": g.item_code,
                        "classification": g.classification,
                        "counts_against_status": int(g.counts_against_status),
                        "detail": g.detail,
                        "recorded_at": recorded_at,
                    }
                    for g in gaps
                ],
            )


def load_gaps(engine: Engine, pipeline_run_id: str) -> list[ScopeGap]:
    """Every gap recorded under this run, across all domains."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                scope_gaps_table.c.domain,
                scope_gaps_table.c.item_code,
                scope_gaps_table.c.classification,
                scope_gaps_table.c.detail,
            )
            .where(scope_gaps_table.c.pipeline_run_id == pipeline_run_id)
            .order_by(scope_gaps_table.c.domain, scope_gaps_table.c.item_code)
        ).all()
    return [ScopeGap(domain=r.domain, item_code=r.item_code, classification=r.classification, detail=r.detail)
            for r in rows]


def domains_with_recorded_gaps(engine: Engine, pipeline_run_id: str) -> set[str]:
    """Domains that already have a gap record for this run -- used to decide
    which ones a report-only run still has to work out."""
    with engine.connect() as conn:
        return {
            row[0]
            for row in conn.execute(
                select(scope_gaps_table.c.domain)
                .where(scope_gaps_table.c.pipeline_run_id == pipeline_run_id)
                .distinct()
            )
        }
