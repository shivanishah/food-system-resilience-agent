"""Phase 2 data-quality reporting: ``data_quality/*.csv`` + the
per-domain summary in ``reports/phase2_data_quality.md`` the
phase-2-data-engineering skill requires.

Every Bronze statistic here is computed with a SQL aggregate rather than by
loading the table into pandas. The Bronze layer at real scale is ~11M rows
across the 9 domains (TM's bilateral sweep alone is >6M), so a
``read_bronze`` per statistic is the same accumulate-everything-in-memory
pattern that already OOM-killed the extraction and Silver stages -- see
``src/elt/extract.py`` and ``src/elt/silver.py``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, func, select

from src.database.models import BRONZE_TABLES, SILVER_TABLES, etl_runs, silver_commodity
from src.elt.qcl_items import DEFAULT_QCL_CROP_ITEMS_PATH, load_qcl_crop_items
from src.elt.scope_gaps import ScopeGap

DATA_QUALITY_DIR = Path("data_quality")
REPORT_PATH = Path("reports/phase2_data_quality.md")


def _write_csv(df: pd.DataFrame, name: str, columns: list[str] | None = None) -> None:
    """Write one data-quality CSV.

    ``columns`` names the expected schema so that a result with no rows still
    produces a header rather than a zero-byte file -- an empty file is
    indistinguishable from a missing one and cannot be read back
    (``pandas.errors.EmptyDataError``), which is exactly what "no duplicate
    conflicts were found" looks like on a clean run.
    """
    DATA_QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    if df.empty and columns is not None:
        df = pd.DataFrame(columns=columns)
    df.to_csv(DATA_QUALITY_DIR / name, index=False)


def write_bronze_unit_inventory(engine: Engine) -> None:
    """Rows per (unit, element) per domain -- the evidence behind every unit
    validation/conversion decision in ``config/harmonisation.yaml``."""
    frames = []
    for domain, table in BRONZE_TABLES.items():
        stmt = (
            select(table.c.unit, table.c.element, func.count().label("n_rows"))
            .group_by(table.c.unit, table.c.element)
            .order_by(func.count().desc())
        )
        df = pd.read_sql(stmt, engine)
        if df.empty:
            continue
        df.insert(0, "domain", domain)
        frames.append(df)
    _write_csv(pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(), "bronze_unit_inventory.csv")


def write_bronze_missingness(engine: Engine) -> None:
    """Per-column NULL counts per Bronze domain (columns with no missing
    values are omitted). ``count(col)`` counts non-NULLs, so the difference
    from the table's row count is the missing count."""
    rows = []
    for domain, table in BRONZE_TABLES.items():
        aggregates = [func.count().label("n_total")] + [
            func.count(col).label(f"nonnull__{col.name}") for col in table.columns
        ]
        with engine.connect() as conn:
            result = conn.execute(select(*aggregates)).mappings().one()
        total = result["n_total"]
        if not total:
            continue
        for col in table.columns:
            n_missing = total - result[f"nonnull__{col.name}"]
            if n_missing:
                rows.append(
                    {"domain": domain, "column": col.name, "n_missing": int(n_missing), "n_total": int(total)}
                )
    _write_csv(pd.DataFrame(rows), "bronze_missingness.csv",
               columns=["domain", "column", "n_missing", "n_total"])


def write_bronze_flag_summary(engine: Engine) -> None:
    """FAOSTAT observation-flag distribution per domain (Bronze preserves
    every flag; Silver derives ``is_estimate`` from it)."""
    frames = []
    for domain, table in BRONZE_TABLES.items():
        stmt = (
            select(func.coalesce(table.c.flag, "(none)").label("flag"), func.count().label("n_rows"))
            .group_by(table.c.flag)
            .order_by(func.count().desc())
        )
        df = pd.read_sql(stmt, engine)
        if df.empty:
            continue
        df.insert(0, "domain", domain)
        frames.append(df)
    _write_csv(pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(), "bronze_flag_summary.csv")


def write_duplicate_conflicts(all_conflicts: dict[str, list[dict[str, Any]]]) -> None:
    """Conflicting duplicates resolved during this invocation's Silver
    transforms, plus a coverage companion file. The coverage file matters
    because a resumed run only re-transforms some domains: without it, a
    domain absent from ``duplicate_conflicts.csv`` is ambiguous between "had
    no conflicts" and "was not re-run"."""
    rows = []
    for domain, conflicts in all_conflicts.items():
        for c in conflicts:
            rows.append({"domain": domain, **c})
    _write_csv(
        pd.DataFrame(rows),
        "duplicate_conflicts.csv",
        columns=["domain", "key", "candidate_values", "candidate_flags", "winner_value", "winner_flag"],
    )
    _write_csv(
        pd.DataFrame(
            [{"domain": d, "n_conflicts": len(c)} for d, c in sorted(all_conflicts.items())]
        ),
        "duplicate_conflicts_coverage.csv",
        columns=["domain", "n_conflicts"],
    )


def write_commodity_mapping(engine: Engine) -> None:
    df = pd.read_sql(select(silver_commodity), engine)
    _write_csv(df, "commodity_mapping.csv")


def write_variable_scope(engine: Engine, pipeline_cfg) -> None:
    """Requested vs. retrieved analytical variables per domain.

    ``config/variables.yaml`` names either elements (QCL/TM/FBS/RFM/GT/RL)
    or, for the indicator domains whose ``elements`` is null (FS/CAHD/CP),
    the items themselves. Either way this records what the project asked
    for, whether it came back, and -- per the phase-2 rule that a missing
    required variable is logged and never silently substituted -- what did
    not.
    """
    rows = []
    for domain, spec in pipeline_cfg.domains.items():
        table = BRONZE_TABLES[domain]
        scopes: list[tuple[str, list[tuple[str, str]], Any]] = []
        if isinstance(spec.elements, list):
            scopes.append(
                ("element", [(name.split("|", 1)[0], name) for name in spec.elements], table.c.element)
            )
        item_codes = pipeline_cfg.resolve_item_codes(domain)
        if item_codes is not None:
            # Both dimensions are recorded where both are bounded: a domain
            # scoped by element (QCL/TM) still requests a specific item list,
            # and a shortfall there is exactly what makes its run `partial`.
            scopes.append(("item", [(code, code) for code in item_codes], table.c.item_code))
        for scope_kind, requested, column in scopes:
            with engine.connect() as conn:
                retrieved = {v for (v,) in conn.execute(select(column).distinct()) if v is not None}
            for key, requested_label in requested:
                rows.append(
                    {
                        "domain": domain,
                        "scope_kind": scope_kind,
                        "requested": requested_label,
                        "retrieved": key in retrieved,
                    }
                )
    _write_csv(pd.DataFrame(rows), "variable_scope.csv",
               columns=["domain", "scope_kind", "requested", "retrieved"])


def write_historical_coverage(engine: Engine, pipeline_cfg) -> None:
    """Per-country and per-country-commodity year coverage in
    ``silver_production`` -- Phase 3's stability metrics need a minimum
    number of years, so which series have one is a Phase 2 fact to report,
    not a Phase 3 surprise."""
    expected_years = len(pipeline_cfg.requested_years)
    min_years = pipeline_cfg.min_years_for_history
    production = SILVER_TABLES["silver_production"]

    for keys, filename in (
        ([production.c.area_code], "historical_coverage_country.csv"),
        ([production.c.area_code, production.c.item_code], "historical_coverage_country_commodity.csv"),
    ):
        stmt = select(
            *keys,
            func.min(production.c.year).label("first_year"),
            func.max(production.c.year).label("last_year"),
            func.count(func.distinct(production.c.year)).label("n_years"),
        ).group_by(*keys)
        df = pd.read_sql(stmt, engine)
        if not df.empty:
            df["expected_years"] = expected_years
            df["coverage_pct"] = (df["n_years"] / expected_years * 100).round(1)
            df["sufficient_history"] = df["n_years"] >= min_years
        _write_csv(
            df,
            filename,
            columns=[k.name for k in keys]
            + ["first_year", "last_year", "n_years", "expected_years", "coverage_pct", "sufficient_history"],
        )


def write_unit_conversions_applied(engine: Engine, harmonisation) -> None:
    """Bronze rows each configured unit-conversion rule actually matched."""
    rows = []
    for domain, rules in harmonisation.unit_conversions.items():
        table = BRONZE_TABLES[domain]
        for unit, rule in rules.items():
            stmt = (
                select(func.count())
                .select_from(table)
                .where(table.c.unit == unit, table.c.element.in_(rule.elements))
            )
            with engine.connect() as conn:
                n = conn.execute(stmt).scalar_one()
            if n:
                rows.append(
                    {"domain": domain, "from_unit": unit, "to_unit": rule.to_unit,
                     "factor": rule.factor, "elements": ", ".join(rule.elements), "n_rows": int(n)}
                )
    _write_csv(pd.DataFrame(rows), "unit_conversions_applied.csv",
               columns=["domain", "from_unit", "to_unit", "factor", "elements", "n_rows"])


def write_silver_validation(validation_rows: list[dict[str, Any]]) -> None:
    _write_csv(pd.DataFrame(validation_rows), "silver_validation.csv",
               columns=["table", "check", "severity", "n_violations", "detail"])


def _variable_scope_lines(pipeline_cfg) -> list[str]:
    """Markdown for the requested/retrieved/unavailable variable breakdown
    the phase-2 reporting rules require, read back from the CSV just written
    so the report and the CSV can never disagree."""
    path = DATA_QUALITY_DIR / "variable_scope.csv"
    if not path.exists():
        return []
    scope = pd.read_csv(path)
    if scope.empty:
        return []
    lines = [
        "",
        "## Requested vs retrieved analytical variables",
        "",
        "A domain appears once per bounded dimension: `element` where `config/variables.yaml` names "
        "elements, and `item` wherever a bounded item list was requested (for the indicator domains "
        "FS/CAHD/CP the item *is* the analytical variable). FBS has no `item` row because its item "
        "scope is `all`.",
        "",
        "| Domain | Scope kind | Requested | Retrieved | Unavailable (not substituted) |",
        "|---|---|---|---|---|",
    ]
    for (domain, scope_kind), group in scope.groupby(["domain", "scope_kind"], sort=True):
        unavailable = group.loc[~group["retrieved"], "requested"].tolist()
        lines.append(
            f"| {domain} | {scope_kind} | {len(group)} | {int(group['retrieved'].sum())} | "
            f"{', '.join(str(u) for u in unavailable) if unavailable else '-'} |"
        )
    return lines


def _unavailable_by_domain() -> dict[str, list[str]]:
    """Requested variables that did not come back, per domain, read from the
    CSV just written."""
    path = DATA_QUALITY_DIR / "variable_scope.csv"
    if not path.exists():
        return {}
    scope = pd.read_csv(path)
    if scope.empty:
        return {}
    absent = scope[~scope["retrieved"]]
    return {
        domain: [f"{r.scope_kind} {r.requested}" for r in group.itertuples()]
        for domain, group in absent.groupby("domain")
    }


def _cell(value: Any) -> str:
    """Render one etl_runs value for a Markdown table.

    ``pandas`` widens an integer column containing a NULL to float, so raw
    values arrive here as `8.0` / `nan` / `221.0`; a report is read by people
    and should not show either artefact.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _year_list_cell(value: Any) -> str:
    """``missing_requested_years`` is stored as a JSON list; an empty one
    means full coverage and reads better as a dash than as `[]`."""
    if value in (None, "", "[]"):
        return "-"
    try:
        years = json.loads(value)
    except (TypeError, ValueError):
        return str(value)
    return ", ".join(str(y) for y in years) if years else "-"


def write_scope_gaps(gaps: Sequence[ScopeGap]) -> None:
    """Every requested item that did not arrive, with the live probe that
    established why (see ``src/elt/scope_gaps.py``)."""
    _write_csv(
        pd.DataFrame(
            [
                {
                    "domain": g.domain,
                    "item_code": g.item_code,
                    "classification": g.classification,
                    "counts_against_status": g.counts_against_status,
                    "detail": g.detail,
                }
                for g in gaps
            ]
        ),
        "scope_gaps.csv",
        columns=["domain", "item_code", "classification", "counts_against_status", "detail"],
    )


def _scope_gap_lines(gaps: Sequence[ScopeGap]) -> list[str]:
    if not gaps:
        return []
    lines = [
        "",
        "## Requested items that did not arrive, and why",
        "",
        "A shortfall is only forgiven once it has been *established* live as genuine source "
        "behaviour. Anything unproven keeps the domain at `partial`, because the one FAOSTAT "
        "failure mode that cost this project real data was silent (see `PHASE2.md` section 15).",
        "",
        "| Domain | Item | Classification | Counts against status | Evidence |",
        "|---|---|---|---|---|",
    ]
    for g in sorted(gaps, key=lambda g: (g.domain, g.item_code)):
        lines.append(
            f"| {g.domain} | {g.item_code} | `{g.classification}` | "
            f"{'yes' if g.counts_against_status else 'no'} | {g.detail} |"
        )
    lines += [
        "",
        "Classifications: `absent_from_dimension` - FAO's own item dimension for that domain does "
        "not list the code, so it is no longer requested; `out_of_window` - the series exists but "
        "has no observation in the requested years; `absent_at_source` - in the dimension, but FAO "
        "publishes no observation for it at all; `filter_bug` / `unverified` - not shown to be a "
        "source gap, and therefore still counted.",
    ]
    return lines


def render_report(
    engine: Engine, pipeline_run_id: str, pipeline_cfg, gaps: Sequence[ScopeGap] = ()
) -> None:
    runs = pd.read_sql(select(etl_runs).where(etl_runs.c.pipeline_run_id == pipeline_run_id), engine)
    runs = runs.sort_values("domain")

    lines = [
        "# Phase 2 Data Quality Report",
        "",
        f"Pipeline run: `{pipeline_run_id}`",
        "",
        f"Requested analytical period: **{pipeline_cfg.start_year}-{pipeline_cfg.end_year}** "
        "for every domain. Years a domain does not publish are reported as missing, never "
        "interpolated or treated as an error.",
        "",
        "## Per-domain summary",
        "",
        "| Domain | Priority | Status | Requested years | Actual years | Missing years | "
        "Requested items | Retrieved items | Requested elements | Retrieved elements | "
        "Countries | Partners | Bronze rows | Silver rows |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in runs.iterrows():
        # A domain that names no elements (its analytical variable is the
        # item) gets "-" in both element columns rather than a bare 0, which
        # would read as "asked for some, got none".
        if r["requested_element_count"] is None or (
            isinstance(r["requested_element_count"], float) and math.isnan(r["requested_element_count"])
        ):
            requested_elements = retrieved_elements = "-"
        else:
            requested_elements = _cell(r["requested_element_count"])
            retrieved_elements = _cell(r["retrieved_element_count"])
        lines.append(
            f"| {r['domain']} | {r['priority']} | {r['status']} | "
            f"{_cell(r['requested_start_year'])}-{_cell(r['requested_end_year'])} | "
            f"{_cell(r['actual_min_year'])}-{_cell(r['actual_max_year'])} | "
            f"{_year_list_cell(r['missing_requested_years'])} | "
            f"{_cell(r['requested_item_count'])} | {_cell(r['retrieved_item_count'])} | "
            f"{requested_elements} | {retrieved_elements} | "
            f"{_cell(r['country_count'])} | {_cell(r['partner_count'])} | "
            f"{_cell(r['bronze_rows'])} | {_cell(r['silver_rows'])} |"
        )

    scope_lines = _variable_scope_lines(pipeline_cfg)
    unavailable = _unavailable_by_domain()
    failures = runs[runs["status"] != "success"]
    if not failures.empty:
        lines += [
            "",
            "### Domains not fully successful",
            "",
            "`partial` means something requested did not come back. It is recorded rather than "
            "rounded up to `success` precisely because the one API failure mode that cost this "
            "project real data was silent (see `PHASE2.md` section 15).",
            "",
        ]
        for _, r in failures.iterrows():
            missing = unavailable.get(r["domain"], [])
            detail = r["error_message"] or (
                f"did not return {', '.join(missing)}" if missing else "see coverage columns above"
            )
            lines.append(f"- **{r['domain']}** ({r['priority']}): `{r['status']}` - {detail}")

    lines += _scope_gap_lines(gaps)
    lines += scope_lines

    crop_items = load_qcl_crop_items(DEFAULT_QCL_CROP_ITEMS_PATH)
    lines += [
        "",
        "## QCL crops-only scope",
        "",
        f"- Crop items included: {len(crop_items)} (FAOSTAT itemgroup QC, leaf items only)",
        "- Livestock itemgroups excluded: QA (Live Animals, 22 items), QL (Livestock primary, 55), "
        "QP (Livestock processed, 30); QD (Crops processed, 24) also excluded",
    ]

    lines += ["", "## TM / RFM bilateral scope", ""]
    for label, note in (
        ("TM", "food/crop commodities (`item_set:food_commodities`, derived from the QCL crops-only list)"),
        ("RFM", "fertilizer products only (`item_set:rfm_items`) -- kept in `silver_fertilizer_trade`, "
                "never merged into TM's food-trade structure"),
    ):
        row_df = runs[runs["domain"] == label]
        if row_df.empty:
            continue
        r = row_df.iloc[0]
        lines.append(
            f"- **{label}**: {_cell(r['retrieved_item_count'])} commodities x "
            f"{_cell(r['country_count'])} reporters x {_cell(r['partner_count'])} partners -> "
            f"{_cell(r['bronze_rows'])} bilateral Bronze rows, {_cell(r['silver_rows'])} Silver rows. "
            f"Scope: {note}."
        )
    lines += [
        "",
        "RFM's ingested scope is its **full** item dimension as listed in `config/variables.yaml` "
        "(25 fertilizer products), not a sampled validation subset. RFM remains a supporting/stretch "
        "domain and is not used in any core food-resilience calculation.",
    ]

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n")


def generate_all(
    engine: Engine,
    pipeline_run_id: str,
    pipeline_cfg,
    harmonisation,
    all_conflicts: dict[str, list[dict[str, Any]]],
    validation_rows: list[dict[str, Any]],
    gaps: Sequence[ScopeGap] = (),
) -> None:
    write_bronze_unit_inventory(engine)
    write_bronze_missingness(engine)
    write_bronze_flag_summary(engine)
    write_duplicate_conflicts(all_conflicts)
    write_commodity_mapping(engine)
    write_variable_scope(engine, pipeline_cfg)
    write_historical_coverage(engine, pipeline_cfg)
    write_unit_conversions_applied(engine, harmonisation)
    write_silver_validation(validation_rows)
    write_scope_gaps(gaps)
    render_report(engine, pipeline_run_id, pipeline_cfg, gaps)
