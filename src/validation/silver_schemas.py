"""Silver-table validation, extending the Phase 1 Pydantic approach (no
pandera). Checks are domain-aware: required columns, non-null keys, year in
range, area codes resolving to ``silver_country``, and non-negativity only
where scientifically valid (e.g. not FBS stock variation, not price/emission
changes). Hard violations on core tables fail the run; everything else is
reported.

Every check is a SQL ``COUNT`` over the Silver table, never a full
``read_sql`` of it: ``silver_trade`` alone holds millions of bilateral rows,
and loading each Silver table into a DataFrame just to count violations is
the same accumulate-in-memory pattern that already OOM-killed the extraction
and Silver stages (see ``src/elt/extract.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, Table, and_, func, select

from src.database.models import SILVER_TABLES, silver_country
from src.elt.config import HarmonisationConfig, PipelineConfig
from src.elt.qcl_items import DEFAULT_QCL_CROP_ITEMS_PATH, load_qcl_crop_items


@dataclass
class ValidationFinding:
    table: str
    check: str
    severity: str  # "hard" | "soft"
    n_violations: int
    detail: str = ""


@dataclass
class ValidationReport:
    findings: list[ValidationFinding] = field(default_factory=list)

    @property
    def hard_failures(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.severity == "hard" and f.n_violations > 0]

    def to_rows(self) -> list[dict[str, Any]]:
        return [
            {"table": f.table, "check": f.check, "severity": f.severity,
             "n_violations": f.n_violations, "detail": f.detail}
            for f in self.findings
        ]


_KEY_COLUMNS = {
    "silver_production": ["area_code", "item_code", "element", "year"],
    "silver_trade": ["reporter_area_code", "partner_area_code", "item_code", "element", "year"],
    "silver_food_balance": ["area_code", "item_code", "element", "year"],
    "silver_food_security": ["area_code", "item_code", "year"],
    "silver_affordability": ["area_code", "item_code", "year"],
    "silver_fertilizer_trade": ["reporter_area_code", "partner_area_code", "item_code", "element", "year"],
    "silver_prices": ["area_code", "item_code", "year"],
    "silver_emissions": ["area_code", "item_code", "element", "year"],
    "silver_land_use": ["area_code", "item_code", "element", "year"],
}

_AREA_COLUMNS = {
    "silver_production": ["area_code"],
    "silver_trade": ["reporter_area_code", "partner_area_code"],
    "silver_food_balance": ["area_code"],
    "silver_food_security": ["area_code"],
    "silver_affordability": ["area_code"],
    "silver_fertilizer_trade": ["reporter_area_code", "partner_area_code"],
    "silver_prices": ["area_code"],
    "silver_emissions": ["area_code"],
    "silver_land_use": ["area_code"],
}

_DOMAIN_FOR_TABLE = {
    "silver_production": "QCL",
    "silver_trade": "TM",
    "silver_food_balance": "FBS",
    "silver_food_security": "FS",
    "silver_affordability": "CAHD",
    "silver_fertilizer_trade": "RFM",
    "silver_prices": "CP",
    "silver_emissions": "GT",
    "silver_land_use": "RL",
}

_CORE_TABLES = {"silver_production", "silver_trade", "silver_food_balance", "silver_food_security", "silver_affordability"}


def _count_where(engine: Engine, table: Table, condition) -> int:
    """Number of rows in ``table`` matching a violation ``condition``."""
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(table).where(condition)).scalar_one())


def validate_silver(
    engine: Engine, pipeline_cfg: PipelineConfig, harmonisation: HarmonisationConfig
) -> ValidationReport:
    """Run all Silver checks and return the combined report.

    Never raises: the caller (``src/elt/pipeline.py``) decides whether
    ``report.hard_failures`` should block the run, so the report can still be
    written to ``data_quality/silver_validation.csv`` either way.
    """
    report = ValidationReport()
    crop_codes = {row["item_code"] for row in load_qcl_crop_items(DEFAULT_QCL_CROP_ITEMS_PATH)}
    known_areas = select(silver_country.c.area_code).where(silver_country.c.area_code.isnot(None))

    for table_name, table in SILVER_TABLES.items():
        is_core = table_name in _CORE_TABLES

        for key_col in _KEY_COLUMNS[table_name]:
            report.findings.append(
                ValidationFinding(
                    table_name,
                    f"non_null:{key_col}",
                    "hard" if is_core else "soft",
                    _count_where(engine, table, table.c[key_col].is_(None)),
                )
            )

        report.findings.append(
            ValidationFinding(
                table_name,
                "year_in_requested_range",
                "soft",
                _count_where(
                    engine,
                    table,
                    ~table.c.year.between(pipeline_cfg.start_year, pipeline_cfg.end_year),
                ),
            )
        )

        n_unresolved = sum(
            _count_where(engine, table, table.c[col].notin_(known_areas))
            for col in _AREA_COLUMNS[table_name]
        )
        report.findings.append(
            ValidationFinding(table_name, "area_code_resolves_to_silver_country", "soft", n_unresolved)
        )

        domain = _DOMAIN_FOR_TABLE[table_name]
        non_negative_elements = harmonisation.non_negative_elements.get(domain, [])
        if non_negative_elements and "element" in table.c:
            n_negative = _count_where(
                engine,
                table,
                and_(table.c.element.in_(non_negative_elements), table.c.value < 0),
            )
        else:
            n_negative = 0
        report.findings.append(
            ValidationFinding(table_name, "non_negative_where_required", "soft", n_negative)
        )

        if table_name == "silver_production":
            report.findings.append(
                ValidationFinding(
                    table_name,
                    "qcl_crops_only_no_livestock_leak",
                    "hard",
                    _count_where(engine, table, table.c.item_code.notin_(crop_codes)),
                    detail="silver_production must contain only the 162-code crop-only item set",
                )
            )

    return report
