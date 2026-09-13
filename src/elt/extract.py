"""Discovery-bounded extraction: per domain, resolve the configured scope
against what FAOSTAT actually has, fetch it, and record coverage.

Nothing here assumes a requested year/item/element exists -- each is
discovered live and compared to the request (CLAUDE.md's "never treat an
unavailable year as an application error"). The 2014-2024 boundary is
enforced twice: once in the year codes sent to the API, and again
client-side on every row (:func:`scope_filter_rows`) as a belt-and-braces
check against a source that quietly returned something out of range.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Engine, func, select

from src.api.client import FAOSTATClient
from src.database.models import BRONZE_TABLES
from src.elt.bronze import append_bronze_batch, truncate_bronze
from src.elt.config import DomainSpec, PipelineConfig
from src.validation.schemas import DataObservation

logger = logging.getLogger(__name__)

# Areas requested per API call in the bilateral (reporter x partner) cross
# product. Empirically calibrated live against TM: a 20x20 reporter x
# partner grid of major traders (full item/year scope) took ~41s and a
# 198x221 (full) grid 504'd after ~90s regardless of item count -- the
# server-side cost scales with the reporter x partner product itself, not
# just response size. 15x15 stayed a comfortable ~13s even for major traders.
BILATERAL_CHUNK_SIZE = 15

# Items requested per API call for bilateral domains, on top of the
# reporter/partner chunking above -- keeps a single request's total data
# volume bounded even when a chunk happens to contain several major traders.
BILATERAL_ITEM_CHUNK_SIZE = 40

# Areas requested per API call for single-area domains. Calibrated live
# against FBS (the densest domain -- ~123 items x 21 elements): 20 countries
# with the full item/element scope took ~50s, 60 dropped the connection.
AREA_CHUNK_SIZE = 20

# Items requested per API call for single-area domains, on top of the area
# chunking above -- keeps a request's total volume bounded even for an
# "all items" scope domain (FBS) instead of sending its whole item list.
SINGLE_AREA_ITEM_CHUNK_SIZE = 40


def parse_year_code(year_code: str) -> tuple[int | None, str, str | None]:
    """Parse a FAOSTAT year code into (representative_year, year_type, year_span).

    A 4-digit code is an annual year. An 8-digit code is a 3-year average
    (FAOSTAT concatenates start+end, e.g. "19992001" = 1999-2001); it is
    keyed to its *end* year, per PHASE2.md. Anything else is unparseable and
    returned as (None, "unknown", None) rather than guessed.
    """
    code = year_code.strip()
    if len(code) == 4 and code.isdigit():
        return int(code), "annual", None
    if len(code) == 8 and code.isdigit():
        start, end = int(code[:4]), int(code[4:])
        return end, "3yr_avg", f"{start}-{end}"
    return None, "unknown", None


@dataclass
class DomainCoverage:
    domain: str
    requested_start_year: int
    requested_end_year: int
    year_codes_used: list[str]
    available_requested_years: list[int]
    missing_requested_years: list[int]
    actual_min_year: int | None
    actual_max_year: int | None
    requested_elements: list[str] | None
    resolved_element_codes: dict[str, str] = field(default_factory=dict)
    missing_elements: list[str] = field(default_factory=list)
    requested_item_count: int | None = None
    retrieved_item_count: int = 0
    country_count: int = 0
    partner_count: int | None = None
    # Item-level detail behind the counts above, used to classify any gap
    # (src/elt/scope_gaps.py). `requested_item_codes` is the scope actually
    # sent to the API -- i.e. after dropping codes FAO's own item dimension
    # for this domain does not list.
    requested_item_codes: list[str] = field(default_factory=list)
    retrieved_item_codes: list[str] = field(default_factory=list)
    dimension_item_codes: list[str] = field(default_factory=list)
    items_absent_from_dimension: list[str] = field(default_factory=list)


@dataclass
class ExtractResult:
    domain: str
    coverage: DomainCoverage
    rows_received: int
    bronze_rows: int


@dataclass
class _RunningStats:
    """Accumulates coverage stats as batches stream to Bronze, so a whole
    domain's rows never have to sit in memory at once (a real domain's full
    scope -- e.g. TM's bilateral sweep -- is too large for that; confirmed
    live, an accumulate-then-write design OOM-killed a full pipeline run)."""

    rows_received: int = 0
    rows_written: int = 0
    item_codes_seen: set[str] = field(default_factory=set)
    years_seen: set[int] = field(default_factory=set)

    def record(self, batch_rows: list[DataObservation], inserted: int) -> None:
        self.rows_received += len(batch_rows)
        self.rows_written += inserted
        for obs in batch_rows:
            self.item_codes_seen.add(obs.item_code)
            rep_year, _year_type, _span = parse_year_code(str(obs.year_code))
            if rep_year is not None:
                self.years_seen.add(rep_year)


def discover_year_codes(
    client: FAOSTATClient, domain: str, start_year: int, end_year: int
) -> tuple[list[str], list[int], list[int]]:
    """Return (year codes to request, available requested years, missing
    requested years) by discovering the domain's actual year dimension."""
    rows = client.get_years(domain).data
    requested = set(range(start_year, end_year + 1))
    matches: list[tuple[str, int]] = []
    for row in rows:
        code = str(row.get("Year Code") or row.get("Year") or "")
        rep_year, _year_type, _span = parse_year_code(code)
        if rep_year is not None and rep_year in requested:
            matches.append((code, rep_year))
    year_codes = [code for code, _ in matches]
    available = sorted({year for _, year in matches})
    missing = sorted(requested - set(available))
    return year_codes, available, missing


def _parse_requested_element(requested: str) -> tuple[str, str | None]:
    """A requested element is either a plain name ("Area harvested") or a
    "Name|Unit" pair ("Production|t") used to disambiguate a domain (QCL)
    where multiple elements share a human name (e.g. "Production" covers
    both crop tonnage and livestock head counts) -- the unit is the only
    reliable, non-guessed disambiguator FAOSTAT itself provides."""
    if "|" in requested:
        name, unit = requested.split("|", 1)
        return name, unit
    return requested, None


def resolve_elements(
    client: FAOSTATClient, domain: str, requested_names: list[str] | None
) -> tuple[dict[str, str], list[str]]:
    """Resolve human element names to codes by live discovery.

    Returns (requested-string -> code for those that resolved, requested
    strings that didn't). A name resolves only if it names exactly one
    element (or is disambiguated with "|Unit"); an ambiguous or absent name
    is reported, never guessed by picking whichever candidate happened to
    come back first.
    """
    if not requested_names:
        return {}, []
    rows = client.get_elements(domain).data
    candidates_by_name: dict[str, list[tuple[str, str | None]]] = {}
    for row in rows:
        candidates_by_name.setdefault(row.get("Element"), []).append(
            (row.get("Element Code"), row.get("Unit"))
        )

    resolved: dict[str, str] = {}
    missing: list[str] = []
    for requested in requested_names:
        name, unit = _parse_requested_element(requested)
        candidates = candidates_by_name.get(name, [])
        if unit is not None:
            match = next((code for code, u in candidates if u == unit), None)
        elif len(candidates) == 1:
            match = candidates[0][0]
        else:
            match = None
        if match is not None:
            resolved[requested] = match
        else:
            missing.append(requested)
    if missing:
        logger.warning("Domain %s: requested elements not found/unambiguous in API: %s", domain, missing)
    return resolved, missing


def scope_filter_items(rows: list[DataObservation], keep_item_codes: set[str]) -> list[DataObservation]:
    """Client-side application of the item scope, for a domain whose
    server-side ``item=`` filter cannot be trusted (``item_filter: client``
    in ``config/variables.yaml``).

    Confirmed live on FS, CAHD and CP: FAOSTAT compares the requested item
    code against a fixed-width internal key, so a code *longer* than that
    width silently matches nothing (CAHD's "Cost of a healthy diet" item
    70040 returns zero rows while the same query with no item filter returns
    it), and a code at the width prefix-matches unrequested child items (CP's
    23013 also returns 230131/230132, its weighted-average and median
    variants). Both directions are wrong, so for those domains the request is
    sent unfiltered and the project's scope is applied here instead.
    """
    return [row for row in rows if str(row.item_code) in keep_item_codes]


def scope_filter_rows(rows: list[DataObservation], start_year: int, end_year: int) -> list[DataObservation]:
    """Client-side re-enforcement of the requested year window."""
    kept = []
    for row in rows:
        rep_year, _year_type, _span = parse_year_code(str(row.year_code))
        if rep_year is not None and start_year <= rep_year <= end_year:
            kept.append(row)
    return kept


def restrict_to_item_dimension(
    client: FAOSTATClient, domain: str, item_codes: list[str] | None
) -> tuple[list[str] | None, list[str], list[str]]:
    """Intersect a configured item scope with the domain's own item dimension.

    Returns ``(requestable, absent, dimension_codes)``. Asking for a code the
    source does not list is a request bug, not a data gap: the project's TM
    commodity subset is derived from the QCL crop list, and 8 of those 162
    crops are simply not traded bilaterally in FAOSTAT's TM dimension. Sending
    them produced an unexplainable shortfall on every run; dropping them here
    -- and recording which -- makes the request honest.
    """
    if item_codes is None:
        return None, [], []
    dimension_codes = [str(row["Item Code"]) for row in client.get_items(domain).data]
    known = set(dimension_codes)
    requestable = [code for code in item_codes if code in known]
    absent = [code for code in item_codes if code not in known]
    if absent:
        logger.info(
            "Domain %s: %d configured item(s) absent from its item dimension, not requested: %s",
            domain, len(absent), absent,
        )
    return requestable, absent, dimension_codes


def extract_domain(
    client: FAOSTATClient,
    engine: Engine,
    pipeline_run_id: str,
    domain: str,
    spec: DomainSpec,
    pipeline_cfg: PipelineConfig,
) -> ExtractResult:
    """Discover the configured scope for one domain and stream it straight
    to Bronze, batch by batch (never holding a whole domain's rows in
    memory -- see :class:`_RunningStats`)."""
    year_codes, available_years, missing_years = discover_year_codes(
        client, domain, pipeline_cfg.start_year, pipeline_cfg.end_year
    )

    if spec.elements in (None, "all"):
        resolved_elements, missing_elements = {}, []
        element_codes: list[str] | None = None
    else:
        resolved_elements, missing_elements = resolve_elements(client, domain, spec.elements)
        element_codes = list(resolved_elements.values()) or None

    item_codes, absent_from_dimension, dimension_codes = restrict_to_item_dimension(
        client, domain, pipeline_cfg.resolve_item_codes(domain)
    )

    # A domain flagged `item_filter: client` is requested without an item
    # filter and scoped here instead (see :func:`scope_filter_items`).
    client_side_items = spec.item_filter == "client" and item_codes is not None
    keep_item_codes = set(item_codes) if client_side_items else None
    if client_side_items and spec.bilateral:
        raise ValueError(
            f"Domain {domain}: item_filter='client' is not supported for a bilateral domain -- "
            "fetching a full reporter x partner sweep unfiltered by item would multiply an "
            "already multi-hour extraction by the whole item dimension."
        )

    truncate_bronze(engine, domain)
    stats = _RunningStats()

    if spec.bilateral:
        country_count, partner_count = _extract_bilateral(
            client, engine, pipeline_run_id, domain, item_codes, element_codes, year_codes, pipeline_cfg, stats
        )
    else:
        area_rows = client.get_areas(domain).data
        area_codes = [row["Country Code"] for row in area_rows]
        # An "all items" scope (e.g. FBS) still needs its item list chunked:
        # a wide, densely-populated domain times out even with a modest area
        # chunk if every item and element goes in one request (confirmed
        # live on FBS: 20 countries x all ~123 items x all 21 elements took
        # ~50s; 60 countries dropped the connection entirely).
        if client_side_items:
            item_batches: list[list[str] | None] = [None]
        else:
            request_item_codes = item_codes
            if request_item_codes is None:
                request_item_codes = [row["Item Code"] for row in client.get_items(domain).data]
            item_batches = list(_chunk(request_item_codes, SINGLE_AREA_ITEM_CHUNK_SIZE))

        # Routed through client.get_data() (not get_data_chunked, which calls
        # the raw unfiltered fetch) so the confirmed element-filter server
        # bug workaround (see FAOSTATClient.get_data) is applied per batch.
        for area_batch in _chunk(area_codes, AREA_CHUNK_SIZE):
            for item_batch in item_batches:
                result = client.get_data(
                    domain, area=area_batch, item=item_batch, element=element_codes, year=year_codes
                )
                _store_batch(
                    engine, pipeline_run_id, domain, pipeline_cfg, result.rows, stats, keep_item_codes
                )
        country_count = len(area_codes)
        partner_count = None

    coverage = DomainCoverage(
        domain=domain,
        requested_start_year=pipeline_cfg.start_year,
        requested_end_year=pipeline_cfg.end_year,
        year_codes_used=year_codes,
        available_requested_years=available_years,
        missing_requested_years=missing_years,
        actual_min_year=min(stats.years_seen) if stats.years_seen else None,
        actual_max_year=max(stats.years_seen) if stats.years_seen else None,
        requested_elements=spec.elements if isinstance(spec.elements, list) else None,
        resolved_element_codes=resolved_elements,
        missing_elements=missing_elements,
        requested_item_count=len(item_codes) if item_codes is not None else None,
        retrieved_item_count=len(stats.item_codes_seen),
        country_count=country_count,
        partner_count=partner_count,
        requested_item_codes=list(item_codes) if item_codes is not None else [],
        retrieved_item_codes=sorted(stats.item_codes_seen),
        dimension_item_codes=dimension_codes,
        items_absent_from_dimension=absent_from_dimension,
    )
    return ExtractResult(
        domain=domain, coverage=coverage, rows_received=stats.rows_received, bronze_rows=stats.rows_written
    )


def coverage_for_existing_bronze(
    client: FAOSTATClient,
    engine: Engine,
    domain: str,
    spec: DomainSpec,
    pipeline_cfg: PipelineConfig,
) -> ExtractResult:
    """Rebuild a domain's :class:`DomainCoverage` from Bronze rows that a
    previous run already fetched, without re-requesting any observations.

    Used when a run is resumed after the expensive extraction stage already
    completed (see ``--reuse-bronze`` in ``src/elt/pipeline.py``): re-pulling
    a full bilateral sweep purely to regenerate an audit record would cost
    hours of API time for data already sitting in Bronze.

    The *requested* side of coverage (year codes, element resolution,
    reporter/partner/area counts) still comes from live discovery -- those
    are small metadata endpoints, and taking them from the API keeps the
    resulting ``etl_runs`` row identical in meaning to one written by a
    normal run. Only the *retrieved* side (years/items actually present,
    row count) is read back out of Bronze.
    """
    table = BRONZE_TABLES[domain]
    year_codes, available_years, missing_years = discover_year_codes(
        client, domain, pipeline_cfg.start_year, pipeline_cfg.end_year
    )
    if spec.elements in (None, "all"):
        resolved_elements, missing_elements = {}, []
    else:
        resolved_elements, missing_elements = resolve_elements(client, domain, spec.elements)

    item_codes, absent_from_dimension, dimension_codes = restrict_to_item_dimension(
        client, domain, pipeline_cfg.resolve_item_codes(domain)
    )

    with engine.connect() as conn:
        bronze_rows = conn.execute(select(func.count()).select_from(table)).scalar_one()
        retrieved_item_codes = sorted(
            str(code)
            for (code,) in conn.execute(select(table.c.item_code).distinct())
            if code is not None
        )
        observed_year_codes = [
            str(code)
            for (code,) in conn.execute(select(table.c.year_code).distinct())
            if code is not None
        ]

    years_seen = {
        rep_year
        for rep_year, _type, _span in (parse_year_code(code) for code in observed_year_codes)
        if rep_year is not None
    }

    if spec.bilateral:
        country_count = len(client.get_dimension(domain, "reporterarea").data)
        partner_count: int | None = len(client.get_dimension(domain, "partnerarea").data)
    else:
        country_count = len(client.get_areas(domain).data)
        partner_count = None

    coverage = DomainCoverage(
        domain=domain,
        requested_start_year=pipeline_cfg.start_year,
        requested_end_year=pipeline_cfg.end_year,
        year_codes_used=year_codes,
        available_requested_years=available_years,
        missing_requested_years=missing_years,
        actual_min_year=min(years_seen) if years_seen else None,
        actual_max_year=max(years_seen) if years_seen else None,
        requested_elements=spec.elements if isinstance(spec.elements, list) else None,
        resolved_element_codes=resolved_elements,
        missing_elements=missing_elements,
        requested_item_count=len(item_codes) if item_codes is not None else None,
        retrieved_item_count=len(retrieved_item_codes),
        country_count=country_count,
        partner_count=partner_count,
        requested_item_codes=list(item_codes) if item_codes is not None else [],
        retrieved_item_codes=retrieved_item_codes,
        dimension_item_codes=dimension_codes,
        items_absent_from_dimension=absent_from_dimension,
    )
    return ExtractResult(
        domain=domain, coverage=coverage, rows_received=bronze_rows, bronze_rows=bronze_rows
    )


def _store_batch(
    engine: Engine,
    pipeline_run_id: str,
    domain: str,
    pipeline_cfg: PipelineConfig,
    rows: list[DataObservation],
    stats: _RunningStats,
    keep_item_codes: set[str] | None = None,
) -> None:
    kept = rows if keep_item_codes is None else scope_filter_items(rows, keep_item_codes)
    kept = scope_filter_rows(kept, pipeline_cfg.start_year, pipeline_cfg.end_year)
    inserted = append_bronze_batch(engine, domain, kept, pipeline_run_id)
    stats.record(kept, inserted)


def _extract_bilateral(
    client: FAOSTATClient,
    engine: Engine,
    pipeline_run_id: str,
    domain: str,
    item_codes: list[str] | None,
    element_codes: list[str] | None,
    year_codes: list[str],
    pipeline_cfg: PipelineConfig,
    stats: _RunningStats,
) -> tuple[int, int]:
    """Fetch a bilateral (reporter x partner) domain across ALL reporters and
    ALL partners, chunking both sides (and items) so no single request's
    code lists get unreasonably long, streaming each batch to Bronze as it
    arrives. Returns (reporter_count, partner_count)."""
    reporter_codes = [row["Reporter Country Code"] for row in client.get_dimension(domain, "reporterarea").data]
    partner_codes = [row["Partner Country Code"] for row in client.get_dimension(domain, "partnerarea").data]
    item_batches = _chunk(item_codes, BILATERAL_ITEM_CHUNK_SIZE) if item_codes is not None else [None]

    for reporter_batch in _chunk(reporter_codes, BILATERAL_CHUNK_SIZE):
        for partner_batch in _chunk(partner_codes, BILATERAL_CHUNK_SIZE):
            for item_batch in item_batches:
                result = client.get_data(
                    domain,
                    reporterarea=reporter_batch,
                    partnerarea=partner_batch,
                    item=item_batch,
                    element=element_codes,
                    year=year_codes,
                )
                _store_batch(engine, pipeline_run_id, domain, pipeline_cfg, result.rows, stats)
    return len(reporter_codes), len(partner_codes)


def _chunk(values: list[str], size: int) -> list[list[str]]:
    return [values[i : i + size] for i in range(0, len(values), size)]
