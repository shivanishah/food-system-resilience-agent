"""Tests for the Phase 2 data-quality artefacts.

These exercise the SQL-aggregate implementations rather than a
load-the-table-into-pandas one: the point of the rewrite is that Bronze is
too large to read into memory, so the assertions are about the numbers being
right, and the queries never materialising rows.
"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from src.database.models import BRONZE_TABLES, metadata
from src.elt import quality_report
from src.elt.config import DomainSpec, PipelineConfig


@pytest.fixture
def data_quality_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(quality_report, "DATA_QUALITY_DIR", tmp_path)
    return tmp_path


def _bronze_row(**overrides):
    row = {
        "natural_key": overrides.pop("natural_key"),
        "domain_code": "QCL",
        "area_code": "2",
        "item_code": "15",
        "element": "Production",
        "year_code": "2020",
        "unit": "t",
        "value": 1.0,
        "flag": "A",
        "retrieved_at": "t",
        "pipeline_run_id": "run-1",
        "source_domain": "FAOSTAT_QCL",
    }
    row.update(overrides)
    return row


def _seeded_engine(rows):
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(BRONZE_TABLES["QCL"]), rows)
    return engine


def test_bronze_unit_inventory_counts_rows_per_unit_and_element(data_quality_dir):
    engine = _seeded_engine(
        [
            _bronze_row(natural_key="a"),
            _bronze_row(natural_key="b"),
            _bronze_row(natural_key="c", unit="ha", element="Area harvested"),
        ]
    )
    quality_report.write_bronze_unit_inventory(engine)
    df = pd.read_csv(data_quality_dir / "bronze_unit_inventory.csv")
    counts = dict(zip(df["unit"], df["n_rows"]))
    assert counts == {"t": 2, "ha": 1}
    assert set(df["domain"]) == {"QCL"}


def test_bronze_missingness_counts_nulls_and_omits_complete_columns(data_quality_dir):
    engine = _seeded_engine(
        [_bronze_row(natural_key="a", flag=None), _bronze_row(natural_key="b")]
    )
    quality_report.write_bronze_missingness(engine)
    df = pd.read_csv(data_quality_dir / "bronze_missingness.csv")
    flag_row = df[df["column"] == "flag"].iloc[0]
    assert (flag_row["n_missing"], flag_row["n_total"]) == (1, 2)
    assert "item_code" not in set(df["column"])


def test_bronze_flag_summary_labels_missing_flags(data_quality_dir):
    engine = _seeded_engine(
        [_bronze_row(natural_key="a", flag=None), _bronze_row(natural_key="b", flag="E")]
    )
    quality_report.write_bronze_flag_summary(engine)
    df = pd.read_csv(data_quality_dir / "bronze_flag_summary.csv")
    assert dict(zip(df["flag"], df["n_rows"])) == {"(none)": 1, "E": 1}


def _config_with_elements(elements):
    return PipelineConfig(
        start_year=2014, end_year=2024, min_years_for_history=7,
        domains={
            "QCL": DomainSpec(
                code="QCL", priority="core", silver_table="silver_production",
                bilateral=False, item_scope="item_set:crops", elements=elements,
            )
        },
        item_sets={"crops": [{"item_code": "15", "item": "Wheat"}, {"item_code": "56", "item": "Maize"}]},
    )


def test_variable_scope_marks_a_requested_element_that_never_arrived(data_quality_dir):
    engine = _seeded_engine([_bronze_row(natural_key="a", element="Production")])
    quality_report.write_variable_scope(engine, _config_with_elements(["Production|t", "Yield|kg/ha"]))
    df = pd.read_csv(data_quality_dir / "variable_scope.csv")
    elements = df[df["scope_kind"] == "element"]
    assert dict(zip(elements["requested"], elements["retrieved"])) == {
        "Production|t": True,
        "Yield|kg/ha": False,
    }


def test_variable_scope_records_items_too_for_an_element_scoped_domain(data_quality_dir):
    """QCL/TM name elements but still request a bounded item list, and a
    shortfall there is what makes their run `partial`."""
    engine = _seeded_engine([_bronze_row(natural_key="a", item_code="15")])
    quality_report.write_variable_scope(engine, _config_with_elements(["Production|t"]))
    df = pd.read_csv(data_quality_dir / "variable_scope.csv")
    assert set(df["scope_kind"]) == {"element", "item"}
    items = df[df["scope_kind"] == "item"]
    assert dict(zip(items["requested"].astype(str), items["retrieved"])) == {"15": True, "56": False}


def test_variable_scope_checks_items_for_an_indicator_domain(data_quality_dir):
    engine = _seeded_engine([_bronze_row(natural_key="a", item_code="15")])
    quality_report.write_variable_scope(engine, _config_with_elements(None))
    df = pd.read_csv(data_quality_dir / "variable_scope.csv")
    assert dict(zip(df["requested"].astype(str), df["retrieved"])) == {"15": True, "56": False}
    assert set(df["scope_kind"]) == {"item"}  # elements is None -> item scope only


def test_duplicate_conflicts_coverage_distinguishes_no_conflicts_from_not_run(data_quality_dir):
    quality_report.write_duplicate_conflicts({"QCL": [], "FBS": [{"key": {"a": 1}}]})
    coverage = pd.read_csv(data_quality_dir / "duplicate_conflicts_coverage.csv")
    assert dict(zip(coverage["domain"], coverage["n_conflicts"])) == {"QCL": 0, "FBS": 1}


# --- report cell formatting ----------------------------------------------


def test_cell_renders_a_pandas_widened_integer_without_a_decimal_point():
    assert quality_report._cell(8.0) == "8"
    assert quality_report._cell(221.0) == "221"


def test_cell_renders_a_missing_value_as_a_dash():
    assert quality_report._cell(float("nan")) == "-"
    assert quality_report._cell(None) == "-"


def test_year_list_cell_renders_full_coverage_as_a_dash():
    assert quality_report._year_list_cell("[]") == "-"
    assert quality_report._year_list_cell(None) == "-"


def test_year_list_cell_lists_the_missing_years():
    assert quality_report._year_list_cell("[2014, 2015, 2016]") == "2014, 2015, 2016"


def test_conflict_coverage_is_readable_when_no_domain_was_transformed(data_quality_dir):
    """A `--domains none` run transforms nothing, so both conflict files are
    empty -- and both must still be readable."""
    quality_report.write_duplicate_conflicts({})
    coverage = pd.read_csv(data_quality_dir / "duplicate_conflicts_coverage.csv")
    assert coverage.empty
    assert list(coverage.columns) == ["domain", "n_conflicts"]


def test_an_empty_result_still_writes_a_readable_header(data_quality_dir):
    """A clean run has no duplicate conflicts; the file must still be
    readable rather than zero bytes."""
    quality_report.write_duplicate_conflicts({"QCL": []})
    df = pd.read_csv(data_quality_dir / "duplicate_conflicts.csv")
    assert df.empty
    assert list(df.columns) == [
        "domain", "key", "candidate_values", "candidate_flags", "winner_value", "winner_flag"
    ]
