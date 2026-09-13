"""Phase 3 STEP 1 -- ``dim_item_mapping``: QCL crop -> FBS commodity + kcal.

``silver_production`` is keyed by QCL primary-crop codes; ``silver_food_balance``
by FBS commodity-group codes. Nothing downstream that weights production by
food energy can work until they join. This module builds that join.

**Mapping source.** FAO's own FBS item definitions (``Default composition:``
lists, see :mod:`src.elt.fbs_composition`). A QCL crop maps to the single
non-aggregate FBS item whose composition lists its code. Nothing is
name-matched.

**Ambiguity.** A crop listed under more than one FBS item is assigned by
*production equivalence*: FBS quantities are in primary-commodity equivalent,
so an FBS item's production must equal the summed QCL production of its
crops. Every candidate assignment is tested; one is accepted only if it is the
unique assignment that brings every affected FBS item within tolerance of 1.
Otherwise the crop stays ``ambiguous_unresolved`` / ``needs_manual``.

**kcal_per_100g.** Derived per FBS item from FBS food supply::

    (kcal/capita/day * 365) / (kg/capita/yr * 10)

median over country-years (thresholds in ``config/gold.yaml``). Because FBS
quantities are primary-equivalent, this is food energy delivered per 100 g of
*primary crop* (e.g. rice paddy, not milled rice) -- directly applicable to
QCL tonnage. Every crop in one FBS group gets that group's density. Where it
cannot be derived the row is flagged ``needs_manual``; no value is invented.

**Completeness.** Every item in ``silver_production`` either maps or appears
in the unmapped report; the build aborts otherwise. Unmapped FBS items are
reported too, with FAO's own reason (aggregate, animal product, processed
derivative ...).

All aggregation runs in DuckDB over the attached SQLite file; only results of
a few hundred rows reach pandas.
"""

from __future__ import annotations

import argparse
import itertools
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from src.database.connection import DEFAULT_DB_PATH
from src.database.duckdb_conn import SQLITE_ALIAS as FS
from src.database.duckdb_conn import duckdb_session, replace_sqlite_table
from src.elt.config import GoldConfig, ItemMappingConfig, KcalDerivationConfig, load_gold_config
from src.elt.fbs_composition import DEFAULT_FBS_COMPOSITION_PATH, load_fbs_composition

logger = logging.getLogger(__name__)

TABLE_NAME = "dim_item_mapping"
DEFAULT_DATA_QUALITY_DIR = Path("data_quality")
DEFAULT_REPORT_PATH = Path("reports/phase3_item_mapping.md")

# kcal/capita/day -> kcal/capita/yr, and kg -> units of 100 g.
DAYS_PER_YEAR = 365
HUNDRED_GRAMS_PER_KG = 10

STATUS_MAPPED = "mapped"
STATUS_RESOLVED = "resolved_ambiguous"
STATUS_AMBIGUOUS = "ambiguous_unresolved"
STATUS_UNMAPPED = "unmapped"

METHOD_COMPOSITION = "fao_default_composition"
METHOD_EQUIVALENCE = "production_equivalence"

BASIS_MAIN = "consumers_above_min_kg"
BASIS_ALL = "all_consumers"
BASIS_NONE = "not_derivable"

# Staged copies of the Silver rows this step reads, keyed by the config
# element-key prefix (``qcl_production`` -> ``qcl``).
_STAGE_TABLES = {"qcl": "stg_qcl", "fbs": "stg_fbs"}

MAPPING_COLUMNS = [
    "qcl_item_code", "qcl_item", "fbs_item_code", "fbs_item", "kcal_per_100g",
    "mapping_status", "mapping_method", "fbs_candidates",
    "kcal_basis", "kcal_n_obs", "kcal_q25", "kcal_q75", "kcal_dispersion_high",
    "equivalence_ratio", "equivalence_n_obs", "equivalence_share_within_tol",
    "equivalence_warning", "needs_manual", "needs_manual_reason", "built_at",
]
UNMAPPED_COLUMNS = ["side", "item_code", "item", "reason_class", "fao_group", "detail"]
KCAL_COLUMNS = [
    "fbs_item_code", "kcal_per_100g", "kcal_basis", "kcal_n_obs", "kcal_q25", "kcal_q75",
    "kcal_dispersion_high", "kcal_n_nonpositive",
]


class UnitMismatchError(ValueError):
    """A Silver element does not carry the unit ``config/gold.yaml`` expects."""


class MappingCoverageError(AssertionError):
    """A ``silver_production`` item neither maps nor appears as unmapped."""


@dataclass
class ItemMappingResult:
    mapping: pd.DataFrame
    unmapped: pd.DataFrame
    equivalence: pd.DataFrame
    ambiguity: pd.DataFrame
    country_coverage: pd.DataFrame
    coverage: dict[str, Any]
    rows_in: dict[str, int]


# --- pure logic (unit-tested without a database) ---------------------------


def composition_parents(fbs_entries: list[dict[str, Any]], qcl_codes: set[str]) -> dict[str, list[str]]:
    """For each QCL code, the non-aggregate FBS items whose FAO default
    composition lists it (``[]`` if none)."""
    parents: dict[str, set[str]] = {code: set() for code in qcl_codes}
    for entry in fbs_entries:
        if entry["is_aggregate"]:
            continue
        for member in entry["composition"]:
            if member["item_code"] in parents:
                parents[member["item_code"]].add(entry["item_code"])
    return {code: sorted(found, key=int) for code, found in parents.items()}


def enumerate_assignments(
    ambiguous: dict[str, list[str]], max_combinations: int
) -> list[dict[str, str]]:
    """Every joint assignment of ambiguous crops to one of their candidates.

    Returns ``[]`` when the search space exceeds ``max_combinations`` -- the
    crops then stay unresolved rather than being assigned by a partial search.
    """
    codes = sorted(ambiguous, key=int)
    n_combinations = math.prod(len(ambiguous[code]) for code in codes)
    if n_combinations > max_combinations:
        logger.warning(
            "ambiguity search space %d exceeds max_combinations=%d; leaving %d crops unresolved",
            n_combinations, max_combinations, len(codes),
        )
        return []
    return [dict(zip(codes, combo)) for combo in itertools.product(*(ambiguous[c] for c in codes))]


def choose_assignment(evidence: pd.DataFrame, max_abs_deviation: float) -> int | None:
    """The unique ``assignment_id`` whose every affected FBS item has an
    equivalence ratio within ``max_abs_deviation`` of 1, else ``None``.

    A missing ratio (no overlapping country-years) counts as a failure.
    """
    if evidence.empty:
        return None
    within = (evidence["equivalence_ratio"] - 1).abs() <= max_abs_deviation
    passes = within.groupby(evidence["assignment_id"]).all()
    winners = passes[passes].index.tolist()
    return int(winners[0]) if len(winners) == 1 else None


def select_kcal_basis(stats: pd.DataFrame, cfg: KcalDerivationConfig) -> pd.DataFrame:
    """Pick one kcal_per_100g per FBS item from :func:`kcal_statistics` output.

    Primary basis: country-years eating >= ``min_food_kg_per_capita_yr``.
    Fallback: every consuming country-year. Neither reaching ``min_obs`` ->
    ``not_derivable`` with a NULL value.
    """
    rows = []
    for rec in stats.to_dict("records"):
        if rec["n_main"] >= cfg.min_obs:
            basis, suffix = BASIS_MAIN, "main"
        elif rec["n_all"] >= cfg.min_obs:
            basis, suffix = BASIS_ALL, "all"
        else:
            basis, suffix = BASIS_NONE, None
        if suffix is None:
            value = q25 = q75 = None
            n_obs = max(int(rec["n_main"]), int(rec["n_all"]))
            dispersion_high = None
        else:
            value, q25, q75 = rec[f"median_{suffix}"], rec[f"q25_{suffix}"], rec[f"q75_{suffix}"]
            n_obs = int(rec[f"n_{suffix}"])
            dispersion_high = int((q75 - q25) / value > cfg.dispersion_rel_iqr_threshold) if value else None
        rows.append(
            {
                "fbs_item_code": rec["fbs_item_code"],
                "kcal_per_100g": value,
                "kcal_basis": basis,
                "kcal_n_obs": n_obs,
                "kcal_q25": q25,
                "kcal_q75": q75,
                "kcal_dispersion_high": dispersion_high,
                "kcal_n_nonpositive": int(rec["n_nonpositive"]),
            }
        )
    return pd.DataFrame(rows, columns=KCAL_COLUMNS)


def fbs_unmapped_items(
    fbs_items: pd.DataFrame,
    fbs_entries: list[dict[str, Any]],
    mapped_fbs_codes: set[str],
    qcl_codes: set[str],
    top_group_names: list[str],
) -> pd.DataFrame:
    """FBS items present in Silver that no QCL crop maps to, with FAO's reason."""
    by_code = {e["item_code"]: e for e in fbs_entries}
    top_groups = {e["item_code"]: e["item"] for e in fbs_entries if e["item"] in top_group_names and e["is_aggregate"]}
    rows = []
    for rec in fbs_items.to_dict("records"):
        code = rec["item_code"]
        if code in mapped_fbs_codes:
            continue
        entry = by_code.get(code)
        members = [m["item_code"] for m in entry["composition"]] if entry else []
        group = next((top_groups[g] for g in (entry or {}).get("parent_groups", []) if g in top_groups), None)
        if entry is None:
            reason, detail = "absent_from_fao_metadata", "not in FBS item definitions"
        elif entry["is_aggregate"]:
            reason, detail = "aggregate", "FAO itemgroup rollup of other FBS items"
        elif not members:
            reason, detail = "no_composition", "FAO publishes no default composition"
        elif set(members) & qcl_codes:
            reason, detail = "members_assigned_elsewhere", "its QCL crops were resolved to another FBS item"
        else:
            preview = ", ".join(members[:8]) + (" ..." if len(members) > 8 else "")
            reason, detail = "no_qcl_crop_member", f"composition FCL codes (none a QCL crop): {preview}"
        rows.append(
            {"side": "FBS", "item_code": code, "item": rec["item"], "reason_class": reason,
             "fao_group": group, "detail": detail}
        )
    return pd.DataFrame(rows, columns=UNMAPPED_COLUMNS)


def qcl_unmapped_items(mapping: pd.DataFrame) -> pd.DataFrame:
    """QCL crops without an FBS item, from the assembled mapping."""
    missing = mapping[mapping["fbs_item_code"].isna()]
    return pd.DataFrame(
        {
            "side": "QCL",
            "item_code": missing["qcl_item_code"],
            "item": missing["qcl_item"],
            "reason_class": missing["mapping_status"],
            "fao_group": None,
            "detail": missing["needs_manual_reason"],
        },
        columns=UNMAPPED_COLUMNS,
    )


def assert_complete(silver_codes: set[str], mapping: pd.DataFrame, unmapped: pd.DataFrame) -> None:
    """Every ``silver_production`` item maps or is listed as unmapped."""
    mapped = set(mapping.loc[mapping["fbs_item_code"].notna(), "qcl_item_code"])
    reported = set(unmapped.loc[unmapped["side"] == "QCL", "item_code"])
    both = mapped & reported
    lost = silver_codes - mapped - reported
    if lost or both:
        raise MappingCoverageError(
            f"silver_production items neither mapped nor reported: {sorted(lost, key=int)}; "
            f"both mapped and reported unmapped: {sorted(both, key=int)}"
        )


# --- DuckDB queries ---------------------------------------------------------


def stage_inputs(con: duckdb.DuckDBPyConnection, cfg: ItemMappingConfig) -> dict[str, int]:
    """Copy just the Silver rows this step reads into DuckDB temp tables.

    One scan of each Silver table; every later query hits the staged copy.
    Returns rows staged per table (the step's rows-in).
    """
    qcl_elements = [s.element for k, s in cfg.elements.items() if k.startswith("qcl_")]
    fbs_elements = [s.element for k, s in cfg.elements.items() if k.startswith("fbs_")]
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE stg_qcl AS
            SELECT area_code, year, item_code, element, value, unit
            FROM {FS}.silver_production
            WHERE element IN ({', '.join('?' * len(qcl_elements))})""",
        qcl_elements,
    )
    con.execute(
        f"""CREATE OR REPLACE TEMP TABLE stg_fbs AS
            SELECT area_code, year, item_code, element, value, unit, value_canonical, canonical_unit
            FROM {FS}.silver_food_balance
            WHERE element IN ({', '.join('?' * len(fbs_elements))})""",
        fbs_elements,
    )
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _STAGE_TABLES.values()}
    logger.info("rows in (staged): %s", counts)
    return counts


def assert_units(con: duckdb.DuckDBPyConnection, cfg: ItemMappingConfig) -> None:
    """Every configured element carries exactly its configured unit."""
    for key, spec in cfg.elements.items():
        table = _STAGE_TABLES[key.split("_", 1)[0]]
        column, expected = ("canonical_unit", spec.canonical_unit) if spec.canonical_unit else ("unit", spec.unit)
        found = {r[0] for r in con.execute(f"SELECT DISTINCT {column} FROM {table} WHERE element = ?", [spec.element]).fetchall()}
        if found != {expected}:
            raise UnitMismatchError(f"{key}: element {spec.element!r} has {column} {sorted(map(str, found))}, expected {expected!r}")


def silver_items(con: duckdb.DuckDBPyConnection, table: str, domain: str) -> pd.DataFrame:
    """``item_code`` + ``item`` for every item present in a Silver table."""
    return con.execute(
        f"""SELECT t.item_code, c.item
            FROM (SELECT DISTINCT item_code FROM {FS}.{table}) t
            LEFT JOIN {FS}.silver_commodity c ON c.item_code = t.item_code AND c.domain_code = ?
            ORDER BY CAST(t.item_code AS INTEGER)""",
        [domain],
    ).df()


def _pairs_frame(assignment: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame(sorted(assignment.items()), columns=["qcl_item_code", "fbs_item_code"])


def production_equivalence(
    con: duckdb.DuckDBPyConnection, assignment: dict[str, str], cfg: ItemMappingConfig
) -> pd.DataFrame:
    """Per FBS item: median over country-years of
    FBS production / summed QCL production of the crops assigned to it."""
    con.register("_pairs", _pairs_frame(assignment))
    try:
        return con.execute(
            """
            WITH qcl AS (
                SELECT s.area_code, s.year, m.fbs_item_code, SUM(s.value) AS qcl_production
                FROM stg_qcl s JOIN _pairs m ON s.item_code = m.qcl_item_code
                WHERE s.element = ? AND s.value IS NOT NULL
                GROUP BY ALL
            ),
            fbs AS (
                SELECT area_code, year, item_code AS fbs_item_code, value_canonical AS fbs_production
                FROM stg_fbs WHERE element = ? AND value_canonical IS NOT NULL
            )
            SELECT fbs_item_code,
                   COUNT(*) AS equivalence_n_obs,
                   MEDIAN(fbs_production / qcl_production) AS equivalence_ratio,
                   AVG(CASE WHEN ABS(fbs_production / qcl_production - 1) <= ? THEN 1.0 ELSE 0.0 END)
                       AS equivalence_share_within_tol
            FROM qcl JOIN fbs USING (area_code, year, fbs_item_code)
            WHERE qcl_production > 0
            GROUP BY fbs_item_code
            """,
            [cfg.elements["qcl_production"].element, cfg.elements["fbs_production"].element, cfg.equivalence_tolerance],
        ).df()
    finally:
        con.unregister("_pairs")


def resolve_ambiguous(
    con: duckdb.DuckDBPyConnection,
    direct: dict[str, str],
    ambiguous: dict[str, list[str]],
    cfg: ItemMappingConfig,
) -> tuple[dict[str, str], pd.DataFrame]:
    """Assign multi-parent crops by production equivalence (see module doc).

    Returns the accepted assignment (``{}`` if none is unique) and the
    evidence: one row per (candidate assignment, affected FBS item).
    """
    evidence_columns = ["assignment_id", "assignment", "fbs_item_code", "equivalence_ratio", "equivalence_n_obs", "selected"]
    if not ambiguous:
        return {}, pd.DataFrame(columns=evidence_columns)
    affected = sorted({c for candidates in ambiguous.values() for c in candidates}, key=int)
    assignments = enumerate_assignments(ambiguous, cfg.ambiguity.max_combinations)
    rows = []
    for assignment_id, assignment in enumerate(assignments):
        eq = production_equivalence(con, {**direct, **assignment}, cfg).set_index("fbs_item_code").reindex(affected)
        label = "; ".join(f"{q}->{f}" for q, f in sorted(assignment.items(), key=lambda kv: int(kv[0])))
        for fbs_code in affected:
            n_obs = eq.at[fbs_code, "equivalence_n_obs"]
            rows.append(
                {
                    "assignment_id": assignment_id,
                    "assignment": label,
                    "fbs_item_code": fbs_code,
                    "equivalence_ratio": eq.at[fbs_code, "equivalence_ratio"],
                    "equivalence_n_obs": 0 if pd.isna(n_obs) else int(n_obs),
                }
            )
    evidence = pd.DataFrame(rows, columns=evidence_columns[:-1])
    winner = choose_assignment(evidence, cfg.ambiguity.max_abs_deviation)
    evidence["selected"] = evidence["assignment_id"] == winner
    if winner is None:
        logger.warning("no unique assignment for ambiguous crops %s", sorted(ambiguous, key=int))
        return {}, evidence
    logger.info("ambiguous crops resolved by production equivalence: %s", assignments[winner])
    return assignments[winner], evidence


def kcal_statistics(con: duckdb.DuckDBPyConnection, cfg: ItemMappingConfig) -> pd.DataFrame:
    """Per FBS item, kcal-per-100g quantiles on both bases (see config).

    A country-year with zero or negative kcal or kg is never divided --
    it is counted in ``n_nonpositive`` and excluded explicitly.
    """
    min_kg = cfg.kcal.min_food_kg_per_capita_yr
    return con.execute(
        f"""
        WITH kcal AS (SELECT area_code, year, item_code, value AS kcal FROM stg_fbs WHERE element = ?),
             kg AS (SELECT area_code, year, item_code, value AS kg FROM stg_fbs WHERE element = ?),
             paired AS (
                SELECT item_code, kg,
                       CASE WHEN kcal > 0 AND kg > 0
                            THEN kcal * {DAYS_PER_YEAR} / (kg * {HUNDRED_GRAMS_PER_KG}) END AS density
                FROM kcal JOIN kg USING (area_code, year, item_code)
                WHERE kcal IS NOT NULL AND kg IS NOT NULL
             )
        SELECT item_code AS fbs_item_code,
               COUNT(*) FILTER (WHERE density IS NULL) AS n_nonpositive,
               COUNT(density) FILTER (WHERE kg >= ?) AS n_main,
               MEDIAN(density) FILTER (WHERE kg >= ?) AS median_main,
               QUANTILE_CONT(density, 0.25) FILTER (WHERE kg >= ?) AS q25_main,
               QUANTILE_CONT(density, 0.75) FILTER (WHERE kg >= ?) AS q75_main,
               COUNT(density) AS n_all,
               MEDIAN(density) AS median_all,
               QUANTILE_CONT(density, 0.25) AS q25_all,
               QUANTILE_CONT(density, 0.75) AS q75_all
        FROM paired
        GROUP BY item_code
        """,
        [cfg.elements["fbs_kcal"].element, cfg.elements["fbs_food_kg"].element, min_kg, min_kg, min_kg, min_kg],
    ).df()


def coverage_by_element(con: duckdb.DuckDBPyConnection, mapping: pd.DataFrame) -> pd.DataFrame:
    """Share of pooled QCL area / tonnage on mapped and on kcal-bearing crops."""
    con.register("_m", mapping[["qcl_item_code", "fbs_item_code", "kcal_per_100g"]])
    try:
        return con.execute(
            """
            SELECT s.element, s.unit,
                   SUM(s.value) AS total,
                   SUM(s.value) FILTER (WHERE m.fbs_item_code IS NOT NULL) / NULLIF(SUM(s.value), 0) AS share_on_mapped,
                   SUM(s.value) FILTER (WHERE m.kcal_per_100g IS NOT NULL) / NULLIF(SUM(s.value), 0) AS share_on_kcal
            FROM stg_qcl s JOIN _m m ON s.item_code = m.qcl_item_code
            WHERE s.value IS NOT NULL
            GROUP BY s.element, s.unit
            """
        ).df()
    finally:
        con.unregister("_m")


def country_area_coverage(
    con: duckdb.DuckDBPyConnection, mapping: pd.DataFrame, cfg: ItemMappingConfig
) -> pd.DataFrame:
    """Per country: share of harvested area (pooled years) on crops with a
    kcal density -- where a kcal-weighted diversity would see least of the
    country's cropland. A country with zero total area gets NULL, not 0."""
    con.register("_m", mapping[["qcl_item_code", "kcal_per_100g"]])
    try:
        return con.execute(
            f"""
            SELECT s.area_code, c.area,
                   COUNT(DISTINCT s.year) AS n_years,
                   COUNT(DISTINCT s.item_code) AS n_crops,
                   SUM(s.value) AS area_ha_pooled,
                   SUM(s.value) FILTER (WHERE m.kcal_per_100g IS NOT NULL) / NULLIF(SUM(s.value), 0)
                       AS share_area_on_kcal_crops
            FROM stg_qcl s
            JOIN _m m ON s.item_code = m.qcl_item_code
            LEFT JOIN {FS}.silver_country c ON c.area_code = s.area_code
            WHERE s.element = ? AND s.value IS NOT NULL
            GROUP BY s.area_code, c.area
            ORDER BY share_area_on_kcal_crops ASC NULLS FIRST, s.area_code
            """,
            [cfg.elements["qcl_area_harvested"].element],
        ).df()
    finally:
        con.unregister("_m")


# --- assembly ---------------------------------------------------------------


def assemble_mapping(
    qcl_items: pd.DataFrame,
    fbs_items: pd.DataFrame,
    parents: dict[str, list[str]],
    assignment: dict[str, str],
    resolved: dict[str, str],
    kcal: pd.DataFrame,
    equivalence: pd.DataFrame,
    cfg: ItemMappingConfig,
    built_at: str,
) -> pd.DataFrame:
    """One row per QCL crop in Silver, with every decision made explicit."""
    fbs_names = dict(zip(fbs_items["item_code"], fbs_items["item"]))
    kcal_by = kcal.set_index("fbs_item_code").to_dict("index") if not kcal.empty else {}
    eq_by = equivalence.set_index("fbs_item_code").to_dict("index") if not equivalence.empty else {}
    rows = []
    for rec in qcl_items.to_dict("records"):
        code = rec["item_code"]
        candidates = parents.get(code, [])
        fbs_code = assignment.get(code)
        if fbs_code is not None:
            status = STATUS_RESOLVED if code in resolved else STATUS_MAPPED
            method = METHOD_EQUIVALENCE if code in resolved else METHOD_COMPOSITION
        elif len(candidates) > 1:
            status, method = STATUS_AMBIGUOUS, None
        else:
            status, method = STATUS_UNMAPPED, None
        k = kcal_by.get(fbs_code, {}) if fbs_code else {}
        e = eq_by.get(fbs_code, {}) if fbs_code else {}
        kcal_value = k.get("kcal_per_100g")
        kcal_value = None if kcal_value is None or pd.isna(kcal_value) else float(kcal_value)
        ratio = e.get("equivalence_ratio")
        if status == STATUS_UNMAPPED:
            reason = "no FBS item lists this crop in its FAO default composition; kcal_per_100g not derivable from FBS"
        elif status == STATUS_AMBIGUOUS:
            reason = f"listed under FBS items {', '.join(candidates)}; production equivalence found no unique assignment"
        elif kcal_value is None:
            reason = (
                f"FBS item {fbs_code} has fewer than {cfg.kcal.min_obs} country-years with positive "
                f"food supply (kcal and kg); kcal_per_100g not derivable"
            )
        else:
            reason = None
        rows.append(
            {
                "qcl_item_code": code,
                "qcl_item": rec["item"],
                "fbs_item_code": fbs_code,
                "fbs_item": fbs_names.get(fbs_code) if fbs_code else None,
                "kcal_per_100g": kcal_value,
                "mapping_status": status,
                "mapping_method": method,
                "fbs_candidates": "|".join(candidates) if candidates else None,
                "kcal_basis": k.get("kcal_basis", BASIS_NONE if fbs_code else None),
                "kcal_n_obs": k.get("kcal_n_obs"),
                "kcal_q25": k.get("kcal_q25"),
                "kcal_q75": k.get("kcal_q75"),
                "kcal_dispersion_high": k.get("kcal_dispersion_high"),
                "equivalence_ratio": ratio,
                "equivalence_n_obs": e.get("equivalence_n_obs"),
                "equivalence_share_within_tol": e.get("equivalence_share_within_tol"),
                "equivalence_warning": (
                    None if ratio is None or pd.isna(ratio) else int(abs(ratio - 1) > cfg.equivalence_tolerance)
                ),
                "needs_manual": int(reason is not None),
                "needs_manual_reason": reason,
                "built_at": built_at,
            }
        )
    df = pd.DataFrame(rows, columns=MAPPING_COLUMNS)
    for column in ("kcal_n_obs", "equivalence_n_obs", "kcal_dispersion_high", "equivalence_warning", "needs_manual"):
        df[column] = df[column].astype("Int64")
    for column in ("kcal_per_100g", "kcal_q25", "kcal_q75", "equivalence_ratio", "equivalence_share_within_tol"):
        df[column] = df[column].astype("Float64")
    return df


def summarise_coverage(
    mapping: pd.DataFrame, fbs_items: pd.DataFrame, fbs_unmapped: pd.DataFrame, by_element: pd.DataFrame
) -> dict[str, Any]:
    """Headline coverage numbers for the report and the CLI summary."""
    n_qcl = len(mapping)
    n_mapped = int(mapping["fbs_item_code"].notna().sum())
    n_kcal = int(mapping["kcal_per_100g"].notna().sum())
    n_fbs = len(fbs_items)
    n_fbs_mapped = n_fbs - len(fbs_unmapped)
    summary: dict[str, Any] = {
        "qcl_items": n_qcl,
        "qcl_mapped": n_mapped,
        "qcl_mapped_pct": 100 * n_mapped / n_qcl if n_qcl else None,
        "qcl_with_kcal": n_kcal,
        "qcl_with_kcal_pct": 100 * n_kcal / n_qcl if n_qcl else None,
        "qcl_needs_manual": int(mapping["needs_manual"].sum()),
        "fbs_items": n_fbs,
        "fbs_with_qcl_crop": n_fbs_mapped,
        "fbs_with_qcl_crop_pct": 100 * n_fbs_mapped / n_fbs if n_fbs else None,
    }
    for rec in by_element.to_dict("records"):
        key = rec["element"].lower().replace(" ", "_")
        for share in ("mapped", "kcal"):
            value = rec[f"share_on_{share}"]
            summary[f"{key}_share_on_{share}_pct"] = None if pd.isna(value) else 100 * value
    return summary


def build_item_mapping(
    con: duckdb.DuckDBPyConnection, cfg: ItemMappingConfig, fbs_entries: list[dict[str, Any]]
) -> ItemMappingResult:
    """Build ``dim_item_mapping`` and its reports on a read-only session."""
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows_in = stage_inputs(con, cfg)
    assert_units(con, cfg)

    qcl_items = silver_items(con, "silver_production", "QCL")
    fbs_items = silver_items(con, "silver_food_balance", "FBS")
    qcl_codes = set(qcl_items["item_code"])
    logger.info("items in Silver: %d QCL, %d FBS", len(qcl_items), len(fbs_items))

    parents = composition_parents(fbs_entries, qcl_codes)
    direct = {code: found[0] for code, found in parents.items() if len(found) == 1}
    ambiguous = {code: found for code, found in parents.items() if len(found) > 1}
    logger.info("composition: %d single-parent, %d multi-parent, %d no parent",
                len(direct), len(ambiguous), sum(1 for f in parents.values() if not f))

    resolved, ambiguity = resolve_ambiguous(con, direct, ambiguous, cfg)
    assignment = {**direct, **resolved}
    kcal = select_kcal_basis(kcal_statistics(con, cfg), cfg.kcal)
    equivalence = production_equivalence(con, assignment, cfg)

    mapping = assemble_mapping(qcl_items, fbs_items, parents, assignment, resolved, kcal, equivalence, cfg, built_at)
    fbs_unmapped = fbs_unmapped_items(fbs_items, fbs_entries, set(assignment.values()), qcl_codes, cfg.fbs_top_groups)
    unmapped = pd.concat([qcl_unmapped_items(mapping), fbs_unmapped], ignore_index=True)
    assert_complete(qcl_codes, mapping, unmapped)

    fbs_names = dict(zip(fbs_items["item_code"], fbs_items["item"]))
    equivalence = equivalence.assign(fbs_item=equivalence["fbs_item_code"].map(fbs_names))
    equivalence = equivalence.merge(kcal, on="fbs_item_code", how="left").sort_values(
        "fbs_item_code", key=lambda s: s.astype(int)
    )
    by_element = coverage_by_element(con, mapping)
    coverage = summarise_coverage(mapping, fbs_items, fbs_unmapped, by_element)
    country_coverage = country_area_coverage(con, mapping, cfg)
    logger.info("rows out: %d mapping rows, %d unmapped rows (%d QCL, %d FBS)",
                len(mapping), len(unmapped), int((unmapped["side"] == "QCL").sum()), len(fbs_unmapped))
    return ItemMappingResult(mapping, unmapped, equivalence, ambiguity, country_coverage, coverage, rows_in)


# --- outputs ----------------------------------------------------------------


def write_table(result: ItemMappingResult, gold: GoldConfig, db_path: Path) -> int:
    """Drop and recreate ``dim_item_mapping`` in the SQLite database."""
    with duckdb_session(gold.duckdb, db_path, read_only=False) as con:
        con.register("_mapping", result.mapping)
        return replace_sqlite_table(con, TABLE_NAME, "SELECT * FROM _mapping", gold.protected_table_prefixes)


def write_csvs(result: ItemMappingResult, out_dir: Path) -> None:
    """Evidence CSVs; an empty frame still writes its header."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result.mapping.to_csv(out_dir / "dim_item_mapping.csv", index=False)
    result.unmapped.to_csv(out_dir / "item_mapping_unmapped.csv", index=False)
    result.equivalence.to_csv(out_dir / "item_mapping_equivalence.csv", index=False)
    result.ambiguity.to_csv(out_dir / "item_mapping_ambiguity.csv", index=False)
    result.country_coverage.to_csv(out_dir / "item_mapping_country_area_coverage.csv", index=False)


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def _md_table(df: pd.DataFrame, digits: int = 1) -> str:
    if df.empty:
        return "_none_\n"
    header = "| " + " | ".join(df.columns) + " |"
    rule = "|" + "|".join("---" for _ in df.columns) + "|"
    body = ["| " + " | ".join(_fmt(v, digits) for v in row) + " |" for row in df.astype(object).itertuples(index=False)]
    return "\n".join([header, rule, *body]) + "\n"


def render_report(result: ItemMappingResult, cfg: ItemMappingConfig) -> str:
    """Human-readable summary of the build (``reports/phase3_item_mapping.md``)."""
    cov = result.coverage
    m = result.mapping
    lines = [
        "# Phase 3 · Step 1 — `dim_item_mapping`",
        "",
        f"Built {m['built_at'].iloc[0] if len(m) else '-'}. Generated by `src/elt/item_mapping.py` — do not hand-edit.",
        "",
        "## Coverage",
        "",
        f"- QCL crops in `silver_production`: **{cov['qcl_items']}**",
        f"- mapped to an FBS item: **{cov['qcl_mapped']} ({_fmt(cov['qcl_mapped_pct'])}%)**",
        f"- with a kcal_per_100g: **{cov['qcl_with_kcal']} ({_fmt(cov['qcl_with_kcal_pct'])}%)**",
        f"- flagged needs_manual: **{cov['qcl_needs_manual']}**",
        f"- FBS items in `silver_food_balance`: {cov['fbs_items']}; with ≥1 QCL crop: "
        f"{cov['fbs_with_qcl_crop']} ({_fmt(cov['fbs_with_qcl_crop_pct'])}%)",
        "",
        "Pooled 2014–2024, all countries in Silver (China 351 not yet de-duplicated — Step 2):",
        "",
        _md_table(pd.DataFrame(
            [{"measure": k.replace("_share_on_mapped_pct", ""), "on mapped crops %": v,
              "on kcal crops %": cov[k.replace("mapped", "kcal")]}
             for k, v in cov.items() if k.endswith("_share_on_mapped_pct")]
        )),
        "## Method",
        "",
        "- **Mapping:** FAO FBS item definitions (`Default composition:`), cached in "
        f"`{DEFAULT_FBS_COMPOSITION_PATH}`. No name matching.",
        "- **kcal_per_100g:** median over country-years of (kcal/cap/day × 365) / (kg/cap/yr × 10) for the "
        f"crop's FBS item; country-years eating ≥ {cfg.kcal.min_food_kg_per_capita_yr} kg/cap/yr, falling back "
        f"to all consumers below {cfg.kcal.min_obs} observations. FBS quantities are primary-equivalent, so this "
        "is food energy per 100 g of primary crop. All crops in one FBS group share its density.",
        f"- **Equivalence check:** median(FBS production / Σ mapped QCL production); warning beyond ±{cfg.equivalence_tolerance}.",
        "",
        "## Needs manual review",
        "",
        _md_table(m.loc[m["needs_manual"] == 1, ["qcl_item_code", "qcl_item", "fbs_item_code", "mapping_status", "needs_manual_reason"]]),
        "## Ambiguous crops (production-equivalence evidence)",
        "",
        _md_table(result.ambiguity, digits=3),
        "## Unmapped — QCL side",
        "",
        _md_table(result.unmapped.loc[result.unmapped["side"] == "QCL", ["item_code", "item", "reason_class"]]),
        "## Unmapped — FBS side",
        "",
        _md_table(result.unmapped.loc[result.unmapped["side"] == "FBS", ["item_code", "item", "reason_class", "fao_group"]]),
        "## kcal density per FBS item (with warnings)",
        "",
        _md_table(result.equivalence[[
            "fbs_item_code", "fbs_item", "kcal_per_100g", "kcal_basis", "kcal_n_obs", "kcal_q25", "kcal_q75",
            "kcal_dispersion_high", "equivalence_ratio", "equivalence_n_obs",
        ]], digits=2),
        "## Countries with least harvested area on kcal-bearing crops (lowest 15)",
        "",
        _md_table(result.country_coverage.head(15), digits=3),
    ]
    return "\n".join(lines)


def run(db_path: Path, data_quality_dir: Path, report_path: Path, gold_path: Path | None = None) -> ItemMappingResult:
    """Build, write the Gold table, CSVs and report."""
    gold = load_gold_config(gold_path) if gold_path else load_gold_config()
    fbs_entries = load_fbs_composition()
    with duckdb_session(gold.duckdb, db_path, read_only=True) as con:
        result = build_item_mapping(con, gold.item_mapping, fbs_entries)
    write_table(result, gold, db_path)
    write_csvs(result, data_quality_dir)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(result, gold.item_mapping))
    logger.info("wrote %s and CSVs under %s", report_path, data_quality_dir)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dim_item_mapping (Phase 3 Step 1).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--data-quality-dir", type=Path, default=DEFAULT_DATA_QUALITY_DIR)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = run(args.db, args.data_quality_dir, args.report)
    for key, value in result.coverage.items():
        print(f"{key}: {_fmt(value)}")


if __name__ == "__main__":
    main()
