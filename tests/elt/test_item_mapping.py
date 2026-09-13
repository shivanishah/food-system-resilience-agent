"""Tests for Phase 3 STEP 1 -- dim_item_mapping.

The end-to-end tests build a tiny SQLite database shaped like the real Silver
tables and run the real DuckDB queries against it. Scenario:

* country 1 grows ONLY wheat (single-commodity country);
* country 2 grows wheat, cabbage, watermelon, pears, oil palm and jute;
* country 3 has data for 2014 and 2016 only (missing years), and its FBS
  wheat rows carry a zero kg/capita and a negative kcal (never divided);
* country 4 reports jute with zero harvested area (zero denominator);
* watermelon (567) is listed under BOTH FBS 2605 and 2625 -- FBS production
  of 2605 includes it, so equivalence must pick 2605;
* jute (780) has no FBS parent; oil palm (254) maps to 2562 but 2562 has no
  food supply, so its kcal is not derivable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.database.duckdb_conn import (
    DuckDBSettings,
    ProtectedTableError,
    duckdb_session,
    replace_sqlite_table,
    validate_gold_table_name,
)
from src.elt.config import AmbiguityConfig, ElementSpec, GoldConfig, ItemMappingConfig, KcalDerivationConfig
from src.elt.item_mapping import (
    BASIS_ALL,
    BASIS_MAIN,
    BASIS_NONE,
    MappingCoverageError,
    UnitMismatchError,
    assert_complete,
    build_item_mapping,
    choose_assignment,
    composition_parents,
    enumerate_assignments,
    select_kcal_basis,
    write_table,
)

YEARS = [2014, 2015, 2016, 2017, 2018]
KCAL_ELEMENT = "Food supply (kcal/capita/day)"
KG_ELEMENT = "Food supply quantity (kg/capita/yr)"


def _kcal_for(density_per_100g: float, kg_per_capita_yr: float) -> float:
    """kcal/capita/day that yields ``density_per_100g`` at ``kg_per_capita_yr``."""
    return density_per_100g * kg_per_capita_yr * 10 / 365


FBS_ENTRIES = [
    {"item_code": "2511", "item": "Wheat and products", "is_aggregate": False, "parent_groups": ["2903"],
     "composition": [{"item_code": "15", "item": "Wheat"}, {"item_code": "16", "item": "Flour, wheat"}]},
    {"item_code": "2562", "item": "Palm kernels", "is_aggregate": False, "parent_groups": ["2903"],
     "composition": [{"item_code": "254", "item": "Oil, palm fruit"}]},
    {"item_code": "2571", "item": "Soyabean Oil", "is_aggregate": False, "parent_groups": ["2903"],
     "composition": [{"item_code": "237", "item": "Oil, soybean"}]},
    {"item_code": "2605", "item": "Vegetables, other", "is_aggregate": False, "parent_groups": ["2903"],
     "composition": [{"item_code": "358", "item": "Cabbages"}, {"item_code": "567", "item": "Watermelons"}]},
    {"item_code": "2625", "item": "Fruits, other", "is_aggregate": False, "parent_groups": ["2903"],
     "composition": [{"item_code": "521", "item": "Pears"}, {"item_code": "567", "item": "Watermelons"}]},
    {"item_code": "2731", "item": "Bovine Meat", "is_aggregate": False, "parent_groups": ["2941"],
     "composition": [{"item_code": "867", "item": "Meat, cattle"}]},
    {"item_code": "2899", "item": "Miscellaneous", "is_aggregate": False, "parent_groups": ["2903"], "composition": []},
    {"item_code": "2903", "item": "Vegetal Products", "is_aggregate": True, "parent_groups": [], "composition": []},
    {"item_code": "2941", "item": "Animal Products", "is_aggregate": True, "parent_groups": [], "composition": []},
]
QCL_NAMES = {"15": "Wheat", "254": "Oil palm fruit", "358": "Cabbages", "521": "Pears",
             "567": "Watermelons", "780": "Jute, raw or retted"}


def _item_mapping_config(min_obs: int = 3) -> ItemMappingConfig:
    return ItemMappingConfig(
        elements={
            "qcl_production": ElementSpec(element="Production", unit="t"),
            "qcl_area_harvested": ElementSpec(element="Area harvested", unit="ha"),
            "fbs_production": ElementSpec(element="Production", canonical_unit="t"),
            "fbs_kcal": ElementSpec(element=KCAL_ELEMENT, unit="kcal/cap/d"),
            "fbs_food_kg": ElementSpec(element=KG_ELEMENT, unit="kg/cap"),
        },
        kcal=KcalDerivationConfig(min_food_kg_per_capita_yr=1.0, min_obs=min_obs, dispersion_rel_iqr_threshold=0.5),
        equivalence_tolerance=0.05,
        ambiguity=AmbiguityConfig(max_abs_deviation=0.02, max_combinations=64),
        fbs_top_groups=["Vegetal Products", "Animal Products"],
    )


@pytest.fixture
def duck_settings(tmp_path: Path) -> DuckDBSettings:
    return DuckDBSettings(memory_limit="512MB", threads=1, temp_directory=str(tmp_path / "duck_tmp"))


def _build_fixture_db(path: Path, qcl_unit: str = "t") -> None:
    production: list[tuple] = []
    food_balance: list[tuple] = []

    def qcl(area: str, item: str, year: int, tonnes: float, hectares: float) -> None:
        production.append((area, year, item, "Production", tonnes, qcl_unit))
        production.append((area, year, item, "Area harvested", hectares, "ha"))

    def fbs_prod(area: str, item: str, year: int, tonnes: float) -> None:
        food_balance.append((area, year, item, "Production", tonnes / 1000, "1000 t", tonnes, "t"))

    def fbs_supply(area: str, item: str, year: int, kcal: float, kg: float) -> None:
        food_balance.append((area, year, item, KCAL_ELEMENT, kcal, "kcal/cap/d", None, None))
        food_balance.append((area, year, item, KG_ELEMENT, kg, "kg/cap", None, None))

    for year in YEARS:
        # Country 1: single-commodity (wheat only).
        qcl("1", "15", year, 1000.0, 400.0)
        fbs_prod("1", "2511", year, 1000.0)
        fbs_supply("1", "2511", year, _kcal_for(300, 100), 100)
        # Country 2: everything.
        qcl("2", "15", year, 500.0, 200.0)
        qcl("2", "358", year, 300.0, 30.0)
        qcl("2", "567", year, 200.0, 20.0)
        qcl("2", "521", year, 100.0, 10.0)
        qcl("2", "254", year, 900.0, 90.0)
        qcl("2", "780", year, 50.0, 50.0)
        fbs_prod("2", "2511", year, 500.0)
        fbs_prod("2", "2605", year, 500.0)   # cabbage + watermelon
        fbs_prod("2", "2625", year, 100.0)   # pears only
        fbs_prod("2", "2562", year, 900.0)
        fbs_supply("2", "2511", year, _kcal_for(300, 80), 80)
        fbs_supply("2", "2605", year, _kcal_for(25, 50), 50)
        fbs_supply("2", "2625", year, _kcal_for(45, 0.5), 0.5)  # below min kg -> fallback basis
        fbs_supply("2", "2731", year, _kcal_for(170, 20), 20)
        fbs_supply("2", "2571", year, _kcal_for(900, 5), 5)
        fbs_supply("2", "2899", year, _kcal_for(110, 2), 2)
        fbs_supply("2", "2903", year, 2500.0, 500)
    for year in (2014, 2016):  # Country 3: missing years.
        qcl("3", "358", year, 60.0, 6.0)
        qcl("3", "567", year, 40.0, 4.0)
        qcl("3", "521", year, 30.0, 3.0)
        fbs_prod("3", "2605", year, 100.0)
        fbs_prod("3", "2625", year, 30.0)
        fbs_supply("3", "2605", year, _kcal_for(25, 40), 40)
        fbs_supply("3", "2625", year, _kcal_for(45, 0.4), 0.4)
    # Country 3 wheat supply rows that must never be divided.
    fbs_supply("3", "2511", 2014, 5.0, 0.0)
    fbs_supply("3", "2511", 2016, -1.0, 2.0)
    # A single palm-kernel supply row: below min_obs on every basis.
    fbs_supply("2", "2562", 2014, 1.0, 0.1)
    # Country 4: zero harvested area.
    production.append(("4", 2014, "780", "Area harvested", 0.0, "ha"))
    production.append(("4", 2014, "780", "Production", 0.0, qcl_unit))

    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE silver_production (area_code TEXT, year INTEGER, item_code TEXT, element TEXT, value REAL, unit TEXT);
        CREATE TABLE silver_food_balance (area_code TEXT, year INTEGER, item_code TEXT, element TEXT, value REAL,
                                          unit TEXT, value_canonical REAL, canonical_unit TEXT);
        CREATE TABLE silver_commodity (domain_code TEXT, item_code TEXT, item TEXT);
        CREATE TABLE silver_country (area_code TEXT, area TEXT);
        """
    )
    con.executemany("INSERT INTO silver_production VALUES (?,?,?,?,?,?)", production)
    con.executemany("INSERT INTO silver_food_balance VALUES (?,?,?,?,?,?,?,?)", food_balance)
    fbs_names = {e["item_code"]: e["item"] for e in FBS_ENTRIES}
    con.executemany(
        "INSERT INTO silver_commodity VALUES (?,?,?)",
        [("QCL", c, n) for c, n in QCL_NAMES.items()] + [("FBS", c, n) for c, n in fbs_names.items()],
    )
    con.executemany("INSERT INTO silver_country VALUES (?,?)",
                    [("1", "Monocropland"), ("2", "Mixedland"), ("3", "Gapland"), ("4", "Zeroland")])
    con.commit()
    con.close()


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.db"
    _build_fixture_db(path)
    return path


@pytest.fixture
def result(fixture_db: Path, duck_settings: DuckDBSettings):
    with duckdb_session(duck_settings, fixture_db) as con:
        return build_item_mapping(con, _item_mapping_config(), FBS_ENTRIES)


# --- pure logic --------------------------------------------------------------


def test_composition_parents_skips_aggregates_and_lists_every_parent():
    entries = FBS_ENTRIES + [
        {"item_code": "2918", "item": "Vegetables", "is_aggregate": True, "parent_groups": [],
         "composition": [{"item_code": "358", "item": "Cabbages"}]},
    ]
    parents = composition_parents(entries, {"15", "358", "567", "780"})
    assert parents == {"15": ["2511"], "358": ["2605"], "567": ["2605", "2625"], "780": []}


def test_enumerate_assignments_is_the_full_product():
    combos = enumerate_assignments({"567": ["2605", "2625"], "568": ["2605", "2625"]}, max_combinations=64)
    assert len(combos) == 4
    assert {"567": "2625", "568": "2605"} in combos


def test_enumerate_assignments_refuses_oversized_search():
    assert enumerate_assignments({"1": ["a", "b"], "2": ["a", "b"]}, max_combinations=3) == []


def test_choose_assignment_requires_a_unique_passing_assignment():
    evidence = pd.DataFrame(
        {"assignment_id": [0, 0, 1, 1], "equivalence_ratio": [1.0, 0.99, 1.09, 0.84]}
    )
    assert choose_assignment(evidence, 0.02) == 0
    both_pass = pd.DataFrame({"assignment_id": [0, 1], "equivalence_ratio": [1.0, 1.01]})
    assert choose_assignment(both_pass, 0.02) is None
    missing_ratio = pd.DataFrame({"assignment_id": [0, 0], "equivalence_ratio": [1.0, float("nan")]})
    assert choose_assignment(missing_ratio, 0.02) is None


def test_select_kcal_basis_main_fallback_and_not_derivable():
    stats = pd.DataFrame(
        [
            {"fbs_item_code": "a", "n_nonpositive": 0, "n_main": 30, "median_main": 300.0, "q25_main": 290.0,
             "q75_main": 310.0, "n_all": 40, "median_all": 280.0, "q25_all": 200.0, "q75_all": 320.0},
            {"fbs_item_code": "b", "n_nonpositive": 2, "n_main": 4, "median_main": 50.0, "q25_main": 49.0,
             "q75_main": 51.0, "n_all": 25, "median_all": 60.0, "q25_all": 20.0, "q75_all": 90.0},
            {"fbs_item_code": "c", "n_nonpositive": 0, "n_main": 1, "median_main": 10.0, "q25_main": 10.0,
             "q75_main": 10.0, "n_all": 3, "median_all": 10.0, "q25_all": 10.0, "q75_all": 10.0},
        ]
    )
    out = select_kcal_basis(stats, KcalDerivationConfig(min_food_kg_per_capita_yr=1, min_obs=20,
                                                         dispersion_rel_iqr_threshold=0.5)).set_index("fbs_item_code")
    assert out.loc["a", "kcal_basis"] == BASIS_MAIN and out.loc["a", "kcal_per_100g"] == 300.0
    assert out.loc["a", "kcal_dispersion_high"] == 0
    assert out.loc["b", "kcal_basis"] == BASIS_ALL and out.loc["b", "kcal_per_100g"] == 60.0
    assert out.loc["b", "kcal_dispersion_high"] == 1  # (90-20)/60 > 0.5
    assert out.loc["c", "kcal_basis"] == BASIS_NONE and pd.isna(out.loc["c", "kcal_per_100g"])


def test_select_kcal_basis_on_empty_input_keeps_schema():
    out = select_kcal_basis(pd.DataFrame(), KcalDerivationConfig(min_food_kg_per_capita_yr=1, min_obs=20,
                                                                  dispersion_rel_iqr_threshold=0.5))
    assert out.empty and "kcal_per_100g" in out.columns


def test_assert_complete_detects_a_silently_dropped_item():
    mapping = pd.DataFrame({"qcl_item_code": ["15", "780"], "fbs_item_code": ["2511", None]})
    unmapped = pd.DataFrame({"side": ["QCL"], "item_code": ["780"]})
    assert_complete({"15", "780"}, mapping, unmapped)
    with pytest.raises(MappingCoverageError):
        assert_complete({"15", "780", "999"}, mapping, unmapped)
    with pytest.raises(MappingCoverageError):
        assert_complete({"15", "780"}, mapping, pd.DataFrame({"side": ["QCL", "QCL"], "item_code": ["780", "15"]}))


# --- end-to-end on the fixture database ------------------------------------


def test_every_silver_production_item_maps_or_is_reported_unmapped(result, fixture_db: Path):
    con = sqlite3.connect(fixture_db)
    silver_codes = {r[0] for r in con.execute("SELECT DISTINCT item_code FROM silver_production")}
    con.close()
    mapped = set(result.mapping.loc[result.mapping["fbs_item_code"].notna(), "qcl_item_code"])
    reported = set(result.unmapped.loc[result.unmapped["side"] == "QCL", "item_code"])
    assert silver_codes == mapped | reported
    assert not mapped & reported


def test_ambiguous_crop_resolved_by_production_equivalence(result):
    row = result.mapping.set_index("qcl_item_code").loc["567"]
    assert row["fbs_item_code"] == "2605"
    assert row["mapping_status"] == "resolved_ambiguous"
    assert row["mapping_method"] == "production_equivalence"
    assert row["fbs_candidates"] == "2605|2625"
    selected = result.ambiguity[result.ambiguity["selected"]]
    assert set(selected["assignment"]) == {"567->2605"}


def test_unmapped_crop_and_underivable_kcal_are_needs_manual(result):
    m = result.mapping.set_index("qcl_item_code")
    assert m.loc["780", "mapping_status"] == "unmapped" and pd.isna(m.loc["780", "fbs_item_code"])
    assert m.loc["780", "needs_manual"] == 1
    assert m.loc["254", "fbs_item_code"] == "2562" and pd.isna(m.loc["254", "kcal_per_100g"])
    assert m.loc["254", "needs_manual"] == 1 and "not derivable" in m.loc["254", "needs_manual_reason"]
    assert m.loc["15", "needs_manual"] == 0


def test_kcal_density_excludes_zero_and_negative_denominators(result):
    m = result.mapping.set_index("qcl_item_code")
    assert m.loc["15", "kcal_per_100g"] == pytest.approx(300.0)
    assert m.loc["15", "kcal_n_obs"] == 10  # countries 1 and 2 x 5 years; country 3's rows excluded
    kcal = result.equivalence.set_index("fbs_item_code")
    assert kcal.loc["2511", "kcal_n_nonpositive"] == 2


def test_kcal_density_falls_back_when_consumption_is_low(result):
    m = result.mapping.set_index("qcl_item_code")
    assert m.loc["521", "kcal_basis"] == BASIS_ALL
    assert m.loc["521", "kcal_per_100g"] == pytest.approx(45.0)
    assert m.loc["358", "kcal_basis"] == BASIS_MAIN
    assert m.loc["358", "kcal_per_100g"] == m.loc["567", "kcal_per_100g"] == pytest.approx(25.0)


def test_production_equivalence_handles_missing_years(result):
    eq = result.equivalence.set_index("fbs_item_code")
    assert eq.loc["2605", "equivalence_ratio"] == pytest.approx(1.0)
    assert eq.loc["2605", "equivalence_n_obs"] == 7  # country 2: 5 years, country 3: 2 years
    assert result.mapping.set_index("qcl_item_code").loc["358", "equivalence_warning"] == 0


def test_fbs_side_unmapped_items_carry_fao_reasons(result):
    fbs = result.unmapped[result.unmapped["side"] == "FBS"].set_index("item_code")
    assert fbs.loc["2731", "reason_class"] == "no_qcl_crop_member"
    assert fbs.loc["2731", "fao_group"] == "Animal Products"
    assert fbs.loc["2571", "fao_group"] == "Vegetal Products"
    assert fbs.loc["2899", "reason_class"] == "no_composition"
    assert fbs.loc["2903", "reason_class"] == "aggregate"
    assert not {"2511", "2605", "2625", "2562"} & set(fbs.index)


def test_country_coverage_single_commodity_missing_years_and_zero_area(result):
    cov = result.country_coverage.set_index("area_code")
    assert cov.loc["1", "n_crops"] == 1
    assert cov.loc["1", "share_area_on_kcal_crops"] == pytest.approx(1.0)
    assert cov.loc["3", "n_years"] == 2
    assert pd.isna(cov.loc["4", "share_area_on_kcal_crops"])  # 0 ha -> NULL, never 0/0
    # Country 2: wheat+cabbage+watermelon+pears have kcal; oil palm and jute do not.
    assert cov.loc["2", "share_area_on_kcal_crops"] == pytest.approx(260 / 400)


def test_unit_mismatch_aborts_the_build(tmp_path: Path, duck_settings: DuckDBSettings):
    path = tmp_path / "bad_units.db"
    _build_fixture_db(path, qcl_unit="1000 t")
    with duckdb_session(duck_settings, path) as con, pytest.raises(UnitMismatchError):
        build_item_mapping(con, _item_mapping_config(), FBS_ENTRIES)


def test_write_table_creates_gold_table_and_leaves_silver_untouched(result, fixture_db: Path, duck_settings):
    gold = GoldConfig(duckdb=duck_settings, protected_table_prefixes=["bronze_", "silver_"],
                      item_mapping=_item_mapping_config())
    con = sqlite3.connect(fixture_db)
    before = con.execute("SELECT COUNT(*) FROM silver_production").fetchone()[0]
    con.close()
    assert write_table(result, gold, fixture_db) == len(result.mapping)
    assert write_table(result, gold, fixture_db) == len(result.mapping)  # idempotent drop/recreate
    con = sqlite3.connect(fixture_db)
    assert con.execute("SELECT COUNT(*) FROM dim_item_mapping").fetchone()[0] == len(result.mapping)
    assert con.execute("SELECT COUNT(*) FROM silver_production").fetchone()[0] == before
    con.close()


def test_replace_refuses_bronze_and_silver_targets(fixture_db: Path, duck_settings: DuckDBSettings):
    with pytest.raises(ProtectedTableError):
        validate_gold_table_name("silver_production", ["bronze_", "silver_"])
    with pytest.raises(ValueError):
        validate_gold_table_name("gold; DROP TABLE x", ["bronze_", "silver_"])
    with duckdb_session(duck_settings, fixture_db, read_only=False) as con, pytest.raises(ProtectedTableError):
        replace_sqlite_table(con, "bronze_qcl", "SELECT 1 AS x", ["bronze_", "silver_"])


# --- the real database (skipped where it is absent) -------------------------

REAL_DB = Path("data/food_system.db")
REAL_UNMAPPED_CSV = Path("data_quality/item_mapping_unmapped.csv")


def _real_mapping_available() -> bool:
    if not REAL_DB.exists() or not REAL_UNMAPPED_CSV.exists():
        return False
    con = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE name = 'dim_item_mapping'").fetchone() is not None
    finally:
        con.close()


@pytest.mark.skipif(not _real_mapping_available(), reason="real database / dim_item_mapping not built")
def test_real_db_every_silver_production_item_maps_or_is_reported():
    con = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    try:
        silver = {r[0] for r in con.execute("SELECT DISTINCT item_code FROM silver_production")}
        mapped = {r[0] for r in con.execute("SELECT qcl_item_code FROM dim_item_mapping WHERE fbs_item_code IS NOT NULL")}
    finally:
        con.close()
    unmapped = pd.read_csv(REAL_UNMAPPED_CSV, dtype=str)
    reported = set(unmapped.loc[unmapped["side"] == "QCL", "item_code"])
    assert silver - mapped - reported == set()
    assert mapped & reported == set()
