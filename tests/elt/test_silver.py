"""Tests for the Bronze -> Silver harmonisation logic: aggregate detection,
duplicate resolution, and unit conversion."""

from __future__ import annotations

import pandas as pd

from src.elt.config import HarmonisationConfig, UnitConversionRule
from src.elt.silver import _apply_unit_conversion, _drop_aggregate_rows, resolve_duplicates


def _harmonisation(**overrides) -> HarmonisationConfig:
    defaults = dict(
        aggregate_numeric_floor=5000,
        aggregate_name_denylist=["World", "Africa", "income"],
        aggregate_allowlist_codes=[],
        unit_conversions={
            "FBS": {
                "1000 t": UnitConversionRule(to_unit="t", factor=1000, elements=["Production"]),
            }
        },
        estimate_flags=["E", "I", "M", "T"],
        duplicate_natural_keys={"QCL": ["area_code", "item_code", "element", "year"]},
        non_negative_elements={"QCL": ["Production"]},
        commodity_mapping={"QCL": {"15": "Wheat"}},
    )
    defaults.update(overrides)
    return HarmonisationConfig(**defaults)


def test_is_aggregate_by_numeric_floor():
    h = _harmonisation()
    assert h.is_aggregate("5000", "World") is True
    assert h.is_aggregate("2", "Afghanistan") is False


def test_is_aggregate_by_name_denylist():
    h = _harmonisation()
    assert h.is_aggregate("999", "Low-income food-deficit countries") is True


def test_is_aggregate_allowlist_overrides_numeric_floor():
    h = _harmonisation(aggregate_allowlist_codes=["5001"])
    assert h.is_aggregate("5001", "China, mainland") is False


def test_drop_aggregate_rows_single_area():
    h = _harmonisation()
    df = pd.DataFrame(
        {"area_code": ["2", "5000"], "area": ["Afghanistan", "World"], "value": [1, 2]}
    )
    kept, excluded = _drop_aggregate_rows(df, h, bilateral=False)
    assert list(kept["area_code"]) == ["2"]
    assert excluded == 1


def test_drop_aggregate_rows_bilateral_drops_if_either_side_is_aggregate():
    h = _harmonisation()
    df = pd.DataFrame(
        {
            "reporter_area_code": ["2", "5000", "3"],
            "reporter_area": ["Afghanistan", "World", "Albania"],
            "partner_area_code": ["3", "4", "5000"],
            "partner_area": ["Albania", "Algeria", "World"],
            "value": [1, 2, 3],
        }
    )
    kept, excluded = _drop_aggregate_rows(df, h, bilateral=True)
    assert len(kept) == 1
    assert excluded == 2


def test_resolve_duplicates_exact_duplicate_collapses_silently():
    df = pd.DataFrame(
        {
            "area_code": ["2", "2"], "item_code": ["15", "15"], "element": ["Production", "Production"],
            "year": [2022, 2022], "value": [100.0, 100.0], "flag": ["A", "A"],
        }
    )
    result, conflicts = resolve_duplicates(df, ["area_code", "item_code", "element", "year"])
    assert len(result) == 1
    assert conflicts == []


def test_resolve_duplicates_conflict_resolved_by_official_flag_then_largest_value():
    df = pd.DataFrame(
        {
            "area_code": ["2", "2"], "item_code": ["15", "15"], "element": ["Production", "Production"],
            "year": [2022, 2022], "value": [100.0, 90.0], "flag": ["E", "A"],
        }
    )
    result, conflicts = resolve_duplicates(df, ["area_code", "item_code", "element", "year"])
    assert len(result) == 1
    assert len(conflicts) == 1
    # "A" (official) outranks "E" (estimated) regardless of magnitude.
    assert result.iloc[0]["value"] == 90.0
    assert result.iloc[0]["flag"] == "A"


def test_resolve_duplicates_same_official_rank_largest_abs_value_wins():
    df = pd.DataFrame(
        {
            "area_code": ["2", "2"], "item_code": ["15", "15"], "element": ["Production", "Production"],
            "year": [2022, 2022], "value": [100.0, 90.0], "flag": ["E", "E"],
        }
    )
    result, conflicts = resolve_duplicates(df, ["area_code", "item_code", "element", "year"])
    assert result.iloc[0]["value"] == 100.0
    assert len(conflicts) == 1


def test_apply_unit_conversion_only_matching_unit_and_element():
    h = _harmonisation()
    df = pd.DataFrame(
        {
            "unit": ["1000 t", "t", "1000 t"],
            "element": ["Production", "Production", "Food"],
            "value": [10.0, 20.0, 30.0],
        }
    )
    out = _apply_unit_conversion(df, "FBS", h)
    assert list(out["value_canonical"]) == [10000.0, 20.0, 30.0]
    assert list(out["canonical_unit"]) == ["t", "t", "1000 t"]
