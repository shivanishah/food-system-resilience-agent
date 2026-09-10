"""Bronze -> Silver: clean and harmonise without expanding analytical scope.

Shared steps for every domain: aggregate-area exclusion (per
``config/harmonisation.yaml``), duplicate resolution on the domain's
dimensional key, FAOSTAT-flag preservation + ``is_estimate`` derivation, and
3-year-average years keyed to their end year. Each domain then gets a short
finalizer that selects/renames columns into its own Silver table shape --
grains genuinely differ (see PHASE2.md), so this part is not shared.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd
from sqlalchemy import Engine, delete, insert, select

from src.database.models import BRONZE_TABLES, SILVER_TABLES, silver_commodity, silver_country
from src.elt.config import HarmonisationConfig
from src.elt.extract import parse_year_code

logger = logging.getLogger(__name__)


def read_bronze(engine: Engine, domain: str) -> pd.DataFrame:
    table = BRONZE_TABLES[domain]
    return pd.read_sql(select(table), engine)


def _add_year_fields(df: pd.DataFrame) -> pd.DataFrame:
    parsed = df["year_code"].astype(str).map(parse_year_code)
    df = df.copy()
    df["year"] = [p[0] for p in parsed]
    df["year_type"] = [p[1] for p in parsed]
    df["year_span"] = [p[2] for p in parsed]
    return df[df["year"].notna()]


def _add_is_estimate(df: pd.DataFrame, harmonisation: HarmonisationConfig) -> pd.DataFrame:
    df = df.copy()
    df["is_estimate"] = df["flag"].isin(harmonisation.estimate_flags).astype(int)
    return df


def _is_aggregate_vectorized(
    codes: pd.Series, names: pd.Series, harmonisation: HarmonisationConfig
) -> pd.Series:
    """Vectorized equivalent of :meth:`HarmonisationConfig.is_aggregate` --
    a per-row Python ``.apply`` over a full Bronze table (hundreds of
    thousands of rows for QCL/TM/FBS) is orders of magnitude too slow."""
    numeric_codes = pd.to_numeric(codes, errors="coerce")
    is_agg = numeric_codes >= harmonisation.aggregate_numeric_floor
    if harmonisation.aggregate_name_denylist:
        pattern = "|".join(re.escape(term) for term in harmonisation.aggregate_name_denylist)
        is_agg = is_agg | names.str.contains(pattern, case=False, na=False, regex=True)
    if harmonisation.aggregate_allowlist_codes:
        is_agg = is_agg & ~codes.isin(harmonisation.aggregate_allowlist_codes)
    return is_agg.fillna(False)


def _drop_aggregate_rows(
    df: pd.DataFrame, harmonisation: HarmonisationConfig, bilateral: bool
) -> tuple[pd.DataFrame, int]:
    """Exclude aggregate areas from an analytical Silver table (they stay in
    Bronze and silver_country). Bilateral tables drop a row if *either* side
    is an aggregate."""
    if bilateral:
        reporter_is_agg = _is_aggregate_vectorized(df["reporter_area_code"], df["reporter_area"], harmonisation)
        partner_is_agg = _is_aggregate_vectorized(df["partner_area_code"], df["partner_area"], harmonisation)
        keep = ~(reporter_is_agg | partner_is_agg)
    else:
        keep = ~_is_aggregate_vectorized(df["area_code"], df["area"], harmonisation)
    excluded = int((~keep).sum())
    return df[keep], excluded


# FAOSTAT flag "official-ness" used to break conflicting-duplicate ties, most
# to least official. Blank/"" is an official figure with no caveat.
_FLAG_OFFICIAL_RANK = ["", "A", "X", "E", "I", "T", "M"]


def _flag_rank(flag: str | None) -> int:
    flag = flag or ""
    return _FLAG_OFFICIAL_RANK.index(flag) if flag in _FLAG_OFFICIAL_RANK else len(_FLAG_OFFICIAL_RANK)


def resolve_duplicates(df: pd.DataFrame, key_columns: list[str]) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Collapse rows sharing ``key_columns``. Same value across the group is
    an exact duplicate (silently collapsed); differing values are a
    conflict, resolved by most-official flag then largest |value|, and
    reported (never silently merged).

    Fully vectorized: a Python-level ``groupby().iterrows()`` loop is fine
    for a few thousand rows (FS/CAHD/CP) but becomes minutes-to-hours slow
    on QCL/FBS/TM-sized tables (hundreds of thousands to millions of rows).
    """
    if df.empty:
        return df, []

    group_sizes = df.groupby(key_columns, dropna=False)["value"].transform("size")
    distinct_values = df.groupby(key_columns, dropna=False)["value"].transform("nunique")
    is_conflict_row = (group_sizes > 1) & (distinct_values > 1)

    conflicts: list[dict[str, Any]] = []
    if is_conflict_row.any():
        for key, group in df[is_conflict_row].groupby(key_columns, dropna=False):
            ranked = group.assign(_rank=group["flag"].map(_flag_rank), _abs=group["value"].abs())
            winner = ranked.sort_values(["_rank", "_abs"], ascending=[True, False]).iloc[0]
            conflicts.append(
                {
                    "key": dict(zip(key_columns, key if isinstance(key, tuple) else (key,))),
                    "candidate_values": group["value"].tolist(),
                    "candidate_flags": group["flag"].tolist(),
                    "winner_value": winner["value"],
                    "winner_flag": winner["flag"],
                }
            )

    ranked_df = df.assign(_rank=df["flag"].map(_flag_rank).fillna(len(_FLAG_OFFICIAL_RANK)), _abs=df["value"].abs())
    sort_ascending = [True] * len(key_columns) + [True, False]
    ranked_df = ranked_df.sort_values(key_columns + ["_rank", "_abs"], ascending=sort_ascending, na_position="last")
    result = ranked_df.drop_duplicates(subset=key_columns, keep="first").drop(columns=["_rank", "_abs"])
    return result.reset_index(drop=True), conflicts


def _apply_unit_conversion(df: pd.DataFrame, domain: str, harmonisation: HarmonisationConfig) -> pd.DataFrame:
    df = df.copy()
    df["value_canonical"] = df["value"]
    df["canonical_unit"] = df["unit"]
    rules = harmonisation.unit_conversions.get(domain, {})
    for unit, rule in rules.items():
        mask = (df["unit"] == unit) & (df["element"].isin(rule.elements))
        df.loc[mask, "value_canonical"] = df.loc[mask, "value"] * rule.factor
        df.loc[mask, "canonical_unit"] = rule.to_unit
    return df


def refresh_silver_country(
    engine: Engine, harmonisation: HarmonisationConfig, area_reference: dict[str, dict[str, str]]
) -> int:
    """One row per distinct area code seen in any Bronze table (area /
    reporterarea / partnerarea), ISO-enriched where FAOSTAT's own area
    definitions have a match.

    Queries ``SELECT DISTINCT`` directly rather than loading each full
    Bronze table into a DataFrame (`read_bronze`) -- a table the size of
    TM's full bilateral sweep has millions of rows behind only a few hundred
    distinct area codes, so pulling every column of every row just to
    dedupe two of them is exactly the memory-hungry pattern that OOM-killed
    the extraction stage (see `src/elt/extract.py`)."""
    frames = []
    for domain, table in BRONZE_TABLES.items():
        for code_col, name_col in (
            ("area_code", "area"),
            ("reporter_area_code", "reporter_area"),
            ("partner_area_code", "partner_area"),
        ):
            df = pd.read_sql(
                select(table.c[code_col].label("code"), table.c[name_col].label("name")).distinct(), engine
            )
            if not df.empty:
                frames.append(df)
    if not frames:
        return 0
    all_areas = pd.concat(frames, ignore_index=True).dropna(subset=["code"]).drop_duplicates(subset=["code"])

    rows = []
    for _, row in all_areas.iterrows():
        code, name = row["code"], row["name"]
        ref = area_reference.get(code, {})
        rows.append(
            {
                "area_code": code,
                "area": name,
                "iso2_code": ref.get("ISO2 Code") or None,
                "iso3_code": ref.get("ISO3 Code") or None,
                "m49_code": ref.get("M49 Code") or None,
                "region": None,
                "is_aggregate": int(harmonisation.is_aggregate(code, name)),
            }
        )
    with engine.begin() as conn:
        conn.execute(delete(silver_country))
        if rows:
            conn.execute(insert(silver_country), rows)
    return len(rows)


def refresh_silver_commodity(engine: Engine, harmonisation: HarmonisationConfig) -> int:
    """One row per (domain, item) seen in Bronze, with an honest
    ``mapping_status`` -- never a silently guessed canonical mapping.

    Queries ``SELECT DISTINCT item_code, item`` directly rather than loading
    each full Bronze table (see :func:`refresh_silver_country` for why)."""
    commodity_domains = {"QCL", "TM", "FBS", "RFM"}
    rows = []
    for domain, table in BRONZE_TABLES.items():
        items = pd.read_sql(select(table.c.item_code, table.c.item).distinct(), engine)
        items = items.dropna(subset=["item_code"])
        if items.empty:
            continue
        mapping = harmonisation.commodity_mapping.get(domain, {})
        for _, row in items.iterrows():
            code = row["item_code"]
            if domain not in commodity_domains:
                status, canonical = "not_applicable", None
            elif code in mapping:
                status, canonical = "manual", mapping[code]
            else:
                status, canonical = "unmapped", None
            rows.append(
                {
                    "domain_code": domain,
                    "item_code": code,
                    "item": row["item"],
                    "canonical_commodity": canonical,
                    "mapping_status": status,
                }
            )
    with engine.begin() as conn:
        conn.execute(delete(silver_commodity))
        if rows:
            conn.execute(insert(silver_commodity), rows)
    return len(rows)


def _write_silver_table(engine: Engine, table_name: str, df: pd.DataFrame) -> int:
    table = SILVER_TABLES[table_name]
    with engine.begin() as conn:
        conn.execute(delete(table))
        if not df.empty:
            conn.execute(insert(table), df.to_dict(orient="records"))
    return len(df)


def _append_silver_rows(engine: Engine, table_name: str, df: pd.DataFrame) -> int:
    """Insert-only counterpart to :func:`_write_silver_table`, for a caller
    that already truncated the table once and is writing many chunks."""
    if df.empty:
        return 0
    with engine.begin() as conn:
        conn.execute(insert(SILVER_TABLES[table_name]), df.to_dict(orient="records"))
    return len(df)


def transform_qcl(engine: Engine, harmonisation: HarmonisationConfig) -> tuple[int, list[dict[str, Any]]]:
    df = read_bronze(engine, "QCL")
    if df.empty:
        return _write_silver_table(engine, "silver_production", df), []
    df = _add_year_fields(df)
    df, _excluded = _drop_aggregate_rows(df, harmonisation, bilateral=False)
    df, conflicts = resolve_duplicates(df, harmonisation.duplicate_natural_keys["QCL"])
    df = _add_is_estimate(df, harmonisation)
    out = df[
        ["area_code", "item_code", "element", "year", "year_type", "year_span", "value", "unit",
         "flag", "flag_description", "note", "is_estimate"]
    ].copy()
    out["value_canonical"] = out["value"]
    out["canonical_unit"] = out["unit"]
    return _write_silver_table(engine, "silver_production", out), conflicts


def transform_fbs(engine: Engine, harmonisation: HarmonisationConfig) -> tuple[int, list[dict[str, Any]]]:
    df = read_bronze(engine, "FBS")
    if df.empty:
        return _write_silver_table(engine, "silver_food_balance", df), []
    df = _add_year_fields(df)
    df, _excluded = _drop_aggregate_rows(df, harmonisation, bilateral=False)
    df, conflicts = resolve_duplicates(df, harmonisation.duplicate_natural_keys["FBS"])
    df = _add_is_estimate(df, harmonisation)
    df = _apply_unit_conversion(df, "FBS", harmonisation)
    out = df[
        ["area_code", "item_code", "element", "year", "year_type", "year_span", "value", "unit",
         "value_canonical", "canonical_unit", "flag", "flag_description", "note", "is_estimate"]
    ].copy()
    return _write_silver_table(engine, "silver_food_balance", out), conflicts


def transform_fs(engine: Engine, harmonisation: HarmonisationConfig) -> tuple[int, list[dict[str, Any]]]:
    return _transform_indicator_domain(engine, "FS", "silver_food_security", harmonisation)


def transform_cahd(engine: Engine, harmonisation: HarmonisationConfig) -> tuple[int, list[dict[str, Any]]]:
    df = read_bronze(engine, "CAHD")
    if df.empty:
        return _write_silver_table(engine, "silver_affordability", df), []
    df = _add_year_fields(df)
    df, _excluded = _drop_aggregate_rows(df, harmonisation, bilateral=False)
    df, conflicts = resolve_duplicates(df, harmonisation.duplicate_natural_keys["CAHD"])
    df = _add_is_estimate(df, harmonisation)
    df = df.rename(columns={"item": "indicator"})
    out = df[
        ["area_code", "item_code", "indicator", "release", "year", "value", "unit",
         "flag", "flag_description", "note", "is_estimate"]
    ].copy()
    return _write_silver_table(engine, "silver_affordability", out), conflicts


def transform_cp(engine: Engine, harmonisation: HarmonisationConfig) -> tuple[int, list[dict[str, Any]]]:
    df = read_bronze(engine, "CP")
    if df.empty:
        return _write_silver_table(engine, "silver_prices", df), []
    df = _add_year_fields(df)
    df, _excluded = _drop_aggregate_rows(df, harmonisation, bilateral=False)
    df, conflicts = resolve_duplicates(df, harmonisation.duplicate_natural_keys["CP"])
    df = _add_is_estimate(df, harmonisation)
    df = df.rename(columns={"item": "indicator"})
    out = df[
        ["area_code", "item_code", "indicator", "month_code", "year", "value", "unit",
         "flag", "flag_description", "note", "is_estimate"]
    ].copy()
    return _write_silver_table(engine, "silver_prices", out), conflicts


def _transform_indicator_domain(
    engine: Engine, domain: str, table_name: str, harmonisation: HarmonisationConfig
) -> tuple[int, list[dict[str, Any]]]:
    df = read_bronze(engine, domain)
    if df.empty:
        return _write_silver_table(engine, table_name, df), []
    df = _add_year_fields(df)
    df, _excluded = _drop_aggregate_rows(df, harmonisation, bilateral=False)
    df, conflicts = resolve_duplicates(df, harmonisation.duplicate_natural_keys[domain])
    df = _add_is_estimate(df, harmonisation)
    df = df.rename(columns={"item": "indicator"})
    out = df[
        ["area_code", "item_code", "indicator", "year", "year_type", "year_span", "value", "unit",
         "flag", "flag_description", "note", "is_estimate"]
    ].copy()
    return _write_silver_table(engine, table_name, out), conflicts


def transform_trade(
    engine: Engine, domain: str, table_name: str, harmonisation: HarmonisationConfig
) -> tuple[int, list[dict[str, Any]]]:
    """TM/RFM Silver transform, processed one item (commodity) at a time.

    A bilateral domain's full Bronze table can be tens of millions of rows
    (TM's full reporter x partner sweep) -- loading it into one DataFrame,
    and later converting the whole result to a list of dicts for insert, is
    the same accumulate-everything-in-memory pattern that OOM-killed the
    extraction stage (see `src/elt/extract.py`). ``item_code`` is part of
    every bilateral duplicate-resolution key (`harmonisation.yaml`), so
    chunking the read by item never splits a duplicate-detection group.
    """
    bronze_table = BRONZE_TABLES[domain]
    item_codes = pd.read_sql(select(bronze_table.c.item_code).distinct(), engine)["item_code"].tolist()

    with engine.begin() as conn:
        conn.execute(delete(SILVER_TABLES[table_name]))

    total_rows = 0
    all_conflicts: list[dict[str, Any]] = []
    for item_code in item_codes:
        df = pd.read_sql(select(bronze_table).where(bronze_table.c.item_code == item_code), engine)
        if df.empty:
            continue
        df = _add_year_fields(df)
        df, _excluded = _drop_aggregate_rows(df, harmonisation, bilateral=True)
        df, conflicts = resolve_duplicates(df, harmonisation.duplicate_natural_keys[domain])
        all_conflicts.extend(conflicts)
        df = _add_is_estimate(df, harmonisation)
        out = df[
            ["reporter_area_code", "partner_area_code", "item_code", "element", "year",
             "year_type", "year_span", "value", "unit", "flag", "flag_description", "note", "is_estimate"]
        ].copy()
        total_rows += _append_silver_rows(engine, table_name, out)
    return total_rows, all_conflicts


def transform_gt(engine: Engine, harmonisation: HarmonisationConfig) -> tuple[int, list[dict[str, Any]]]:
    df = read_bronze(engine, "GT")
    if df.empty:
        return _write_silver_table(engine, "silver_emissions", df), []
    df = _add_year_fields(df)
    df, _excluded = _drop_aggregate_rows(df, harmonisation, bilateral=False)
    df, conflicts = resolve_duplicates(df, harmonisation.duplicate_natural_keys["GT"])
    df = _add_is_estimate(df, harmonisation)
    out = df[
        ["area_code", "item_code", "item", "element", "source_code", "year", "value", "unit",
         "flag", "flag_description", "note", "is_estimate"]
    ].copy()
    return _write_silver_table(engine, "silver_emissions", out), conflicts


def transform_rl(engine: Engine, harmonisation: HarmonisationConfig) -> tuple[int, list[dict[str, Any]]]:
    df = read_bronze(engine, "RL")
    if df.empty:
        return _write_silver_table(engine, "silver_land_use", df), []
    df = _add_year_fields(df)
    df, _excluded = _drop_aggregate_rows(df, harmonisation, bilateral=False)
    df, conflicts = resolve_duplicates(df, harmonisation.duplicate_natural_keys["RL"])
    df = _add_is_estimate(df, harmonisation)
    out = df[
        ["area_code", "item_code", "item", "element", "year", "value", "unit",
         "flag", "flag_description", "note", "is_estimate"]
    ].copy()
    return _write_silver_table(engine, "silver_land_use", out), conflicts


DOMAIN_TRANSFORMS = {
    "QCL": lambda engine, h: transform_qcl(engine, h),
    "TM": lambda engine, h: transform_trade(engine, "TM", "silver_trade", h),
    "FBS": lambda engine, h: transform_fbs(engine, h),
    "FS": lambda engine, h: transform_fs(engine, h),
    "CAHD": lambda engine, h: transform_cahd(engine, h),
    "RFM": lambda engine, h: transform_trade(engine, "RFM", "silver_fertilizer_trade", h),
    "CP": lambda engine, h: transform_cp(engine, h),
    "GT": lambda engine, h: transform_gt(engine, h),
    "RL": lambda engine, h: transform_rl(engine, h),
}
