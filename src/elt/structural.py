"""Indexes and cross-domain convenience views over the Silver tables."""

from __future__ import annotations

from sqlalchemy import Engine, text

_INDEXES = [
    ("idx_silver_production_country_item_year", "silver_production", "area_code, item_code, year"),
    ("idx_silver_trade_reporter_partner_item_year", "silver_trade",
     "reporter_area_code, partner_area_code, item_code, year"),
    ("idx_silver_food_balance_country_item_year", "silver_food_balance", "area_code, item_code, year"),
    ("idx_silver_food_security_country_year", "silver_food_security", "area_code, year"),
    ("idx_silver_affordability_country_year", "silver_affordability", "area_code, year"),
    ("idx_silver_fertilizer_trade_reporter_partner_year", "silver_fertilizer_trade",
     "reporter_area_code, partner_area_code, item_code, year"),
    ("idx_silver_prices_country_year", "silver_prices", "area_code, year"),
    ("idx_silver_emissions_country_year", "silver_emissions", "area_code, year"),
    ("idx_silver_land_use_country_year", "silver_land_use", "area_code, year"),
]

_VIEWS = {
    "silver_country_year": """
        CREATE VIEW silver_country_year AS
        SELECT DISTINCT area_code, year FROM silver_production
        UNION
        SELECT DISTINCT area_code, year FROM silver_food_balance
        UNION
        SELECT DISTINCT area_code, year FROM silver_food_security
        UNION
        SELECT DISTINCT area_code, year FROM silver_affordability
    """,
    "silver_country_commodity_year": """
        CREATE VIEW silver_country_commodity_year AS
        SELECT area_code, item_code, year, 'QCL' AS source_domain FROM silver_production
        UNION ALL
        SELECT area_code, item_code, year, 'FBS' AS source_domain FROM silver_food_balance
    """,
    "silver_trade_matrix": """
        CREATE VIEW silver_trade_matrix AS
        SELECT reporter_area_code, partner_area_code, item_code, element, year, value
        FROM silver_trade
    """,
}


def create_indexes(engine: Engine) -> None:
    with engine.begin() as conn:
        for name, table, columns in _INDEXES:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"))


def create_views(engine: Engine) -> None:
    with engine.begin() as conn:
        for name, ddl in _VIEWS.items():
            conn.execute(text(f"DROP VIEW IF EXISTS {name}"))
            conn.execute(text(ddl))
