"""SQLAlchemy Core table definitions for the Bronze -> Silver ELT layer.

Bronze tables share one column shape (a superset covering both single-area
and bilateral domains, plus the CP/GT/CAHD extra dimensions) generated once
per domain so the 9 tables aren't hand-duplicated. Silver tables are declared
explicitly because their grain genuinely differs per domain (see PHASE2.md).
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

BRONZE_DOMAINS = ["QCL", "TM", "FBS", "FS", "CAHD", "RFM", "CP", "GT", "RL"]


def _bronze_columns() -> list[Column]:
    """Fresh Column instances for one Bronze table (Columns can't be shared
    across Table objects, so this is called once per domain)."""
    return [
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("natural_key", String, nullable=False),
        Column("domain_code", String, nullable=False),
        Column("area_code", String),
        Column("area", String),
        Column("reporter_area_code", String),
        Column("reporter_area", String),
        Column("partner_area_code", String),
        Column("partner_area", String),
        Column("item_code", String),
        Column("item", String),
        Column("element_code", String),
        Column("element", String),
        Column("month_code", String),
        Column("month", String),
        Column("source_code", String),
        Column("source", String),
        Column("release_code", String),
        Column("release", String),
        Column("year_code", String),
        Column("year", String),
        Column("unit", String),
        Column("value", Float),
        Column("flag", String),
        Column("flag_description", String),
        Column("note", String),
        Column("retrieved_at", String, nullable=False),
        Column("pipeline_run_id", String, nullable=False),
        Column("source_domain", String, nullable=False),
        Column("request_parameters", Text),
    ]


BRONZE_TABLES: dict[str, Table] = {}
for _domain in BRONZE_DOMAINS:
    _tablename = f"bronze_{_domain.lower()}"
    BRONZE_TABLES[_domain] = Table(
        _tablename,
        metadata,
        *_bronze_columns(),
        UniqueConstraint("natural_key", name=f"uq_{_tablename}_natural_key"),
        # The Silver transform for large domains (esp. TM's full bilateral
        # sweep) reads Bronze one item_code at a time to keep memory bounded
        # (src/elt/silver.py::transform_trade) -- without this index that's
        # a full table scan per item against a multi-GB table.
        Index(f"idx_{_tablename}_item_code", "item_code"),
    )

# -- Silver: shared dimension tables --------------------------------------

silver_country = Table(
    "silver_country",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("area_code", String, nullable=False, unique=True),
    Column("area", String, nullable=False),
    Column("iso2_code", String),
    Column("iso3_code", String),
    Column("m49_code", String),
    Column("region", String),
    Column("is_aggregate", Integer, nullable=False),
)

silver_commodity = Table(
    "silver_commodity",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("domain_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("item", String, nullable=False),
    Column("canonical_commodity", String),
    Column("mapping_status", String, nullable=False),
    UniqueConstraint("domain_code", "item_code", name="uq_silver_commodity_domain_item"),
)

# -- Silver: analytical tables ---------------------------------------------

silver_production = Table(
    "silver_production",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("area_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("element", String, nullable=False),
    Column("year", Integer, nullable=False),
    Column("year_type", String, nullable=False),
    Column("year_span", String),
    Column("value", Float),
    Column("unit", String),
    Column("value_canonical", Float),
    Column("canonical_unit", String),
    Column("flag", String),
    Column("flag_description", String),
    Column("note", String),
    Column("is_estimate", Integer, nullable=False),
    UniqueConstraint("area_code", "item_code", "element", "year", name="uq_silver_production"),
)

silver_trade = Table(
    "silver_trade",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reporter_area_code", String, nullable=False),
    Column("partner_area_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("element", String, nullable=False),
    Column("year", Integer, nullable=False),
    Column("year_type", String, nullable=False),
    Column("year_span", String),
    Column("value", Float),
    Column("unit", String),
    Column("flag", String),
    Column("flag_description", String),
    Column("note", String),
    Column("is_estimate", Integer, nullable=False),
    UniqueConstraint(
        "reporter_area_code", "partner_area_code", "item_code", "element", "year",
        name="uq_silver_trade",
    ),
)

silver_food_balance = Table(
    "silver_food_balance",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("area_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("element", String, nullable=False),
    Column("year", Integer, nullable=False),
    Column("year_type", String, nullable=False),
    Column("year_span", String),
    Column("value", Float),
    Column("unit", String),
    Column("value_canonical", Float),
    Column("canonical_unit", String),
    Column("flag", String),
    Column("flag_description", String),
    Column("note", String),
    Column("is_estimate", Integer, nullable=False),
    UniqueConstraint("area_code", "item_code", "element", "year", name="uq_silver_food_balance"),
)

silver_food_security = Table(
    "silver_food_security",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("area_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("indicator", String, nullable=False),
    Column("year", Integer, nullable=False),
    Column("year_type", String, nullable=False),
    Column("year_span", String),
    Column("value", Float),
    Column("unit", String),
    Column("flag", String),
    Column("flag_description", String),
    Column("note", String),
    Column("is_estimate", Integer, nullable=False),
    UniqueConstraint("area_code", "item_code", "year", name="uq_silver_food_security"),
)

silver_affordability = Table(
    "silver_affordability",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("area_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("indicator", String, nullable=False),
    Column("release", String),
    Column("year", Integer, nullable=False),
    Column("value", Float),
    Column("unit", String),
    Column("flag", String),
    Column("flag_description", String),
    Column("note", String),
    Column("is_estimate", Integer, nullable=False),
    UniqueConstraint("area_code", "item_code", "year", name="uq_silver_affordability"),
)

silver_fertilizer_trade = Table(
    "silver_fertilizer_trade",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reporter_area_code", String, nullable=False),
    Column("partner_area_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("element", String, nullable=False),
    Column("year", Integer, nullable=False),
    Column("year_type", String, nullable=False),
    Column("year_span", String),
    Column("value", Float),
    Column("unit", String),
    Column("flag", String),
    Column("flag_description", String),
    Column("note", String),
    Column("is_estimate", Integer, nullable=False),
    UniqueConstraint(
        "reporter_area_code", "partner_area_code", "item_code", "element", "year",
        name="uq_silver_fertilizer_trade",
    ),
)

silver_prices = Table(
    "silver_prices",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("area_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("indicator", String, nullable=False),
    Column("month_code", String),
    Column("year", Integer, nullable=False),
    Column("value", Float),
    Column("unit", String),
    Column("flag", String),
    Column("flag_description", String),
    Column("note", String),
    Column("is_estimate", Integer, nullable=False),
    UniqueConstraint("area_code", "item_code", "month_code", "year", name="uq_silver_prices"),
)

silver_emissions = Table(
    "silver_emissions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("area_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("item", String, nullable=False),
    Column("element", String, nullable=False),
    Column("source_code", String),
    Column("year", Integer, nullable=False),
    Column("value", Float),
    Column("unit", String),
    Column("flag", String),
    Column("flag_description", String),
    Column("note", String),
    Column("is_estimate", Integer, nullable=False),
    UniqueConstraint(
        "area_code", "item_code", "element", "source_code", "year", name="uq_silver_emissions"
    ),
)

silver_land_use = Table(
    "silver_land_use",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("area_code", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("item", String, nullable=False),
    Column("element", String, nullable=False),
    Column("year", Integer, nullable=False),
    Column("value", Float),
    Column("unit", String),
    Column("flag", String),
    Column("flag_description", String),
    Column("note", String),
    Column("is_estimate", Integer, nullable=False),
    UniqueConstraint("area_code", "item_code", "element", "year", name="uq_silver_land_use"),
)

SILVER_TABLES: dict[str, Table] = {
    "silver_production": silver_production,
    "silver_trade": silver_trade,
    "silver_food_balance": silver_food_balance,
    "silver_food_security": silver_food_security,
    "silver_affordability": silver_affordability,
    "silver_fertilizer_trade": silver_fertilizer_trade,
    "silver_prices": silver_prices,
    "silver_emissions": silver_emissions,
    "silver_land_use": silver_land_use,
}

# -- Pipeline auditing -------------------------------------------------------

etl_runs = Table(
    "etl_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pipeline_run_id", String, nullable=False),
    Column("domain", String, nullable=False),
    Column("priority", String, nullable=False),
    Column("status", String, nullable=False),
    # Nullable: a domain that failed before extraction ever ran has no
    # coverage record at all (see src/elt/etl_runs.py).
    Column("requested_start_year", Integer),
    Column("requested_end_year", Integer),
    Column("actual_min_year", Integer),
    Column("actual_max_year", Integer),
    Column("missing_requested_years", Text),
    Column("requested_item_count", Integer),
    Column("retrieved_item_count", Integer),
    Column("requested_element_count", Integer),
    Column("retrieved_element_count", Integer),
    Column("country_count", Integer),
    Column("partner_count", Integer),
    Column("rows_received", Integer),
    Column("rows_inserted", Integer),
    Column("bronze_rows", Integer),
    Column("silver_rows", Integer),
    Column("started_at", String, nullable=False),
    Column("finished_at", String),
    Column("error_message", Text),
)


# One row per requested item that did not arrive, with the live evidence for
# why (see src/elt/scope_gaps.py). Persisted rather than kept in memory so a
# report regenerated later -- or by a run that only touched some domains --
# still shows every gap in the run, not just the latest slice.
scope_gaps = Table(
    "scope_gaps",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pipeline_run_id", String, nullable=False),
    Column("domain", String, nullable=False),
    Column("item_code", String, nullable=False),
    Column("classification", String, nullable=False),
    Column("counts_against_status", Integer, nullable=False),
    Column("detail", Text, nullable=False),
    Column("recorded_at", String, nullable=False),
    UniqueConstraint("pipeline_run_id", "domain", "item_code", name="uq_scope_gaps"),
)
