"""Tests for Silver-table validation, using a small in-memory SQLite DB
seeded directly (no live API calls)."""

from __future__ import annotations

from sqlalchemy import create_engine, insert

from src.database.models import metadata, silver_country, silver_production
from src.elt.config import HarmonisationConfig, PipelineConfig
from src.validation.silver_schemas import validate_silver


def _pipeline_cfg() -> PipelineConfig:
    return PipelineConfig(
        start_year=2014, end_year=2024, min_years_for_history=7,
        domains={}, item_sets={},
    )


def _harmonisation() -> HarmonisationConfig:
    return HarmonisationConfig(
        aggregate_numeric_floor=5000, aggregate_name_denylist=["World"],
        aggregate_allowlist_codes=[], unit_conversions={}, estimate_flags=["E"],
        duplicate_natural_keys={}, non_negative_elements={"QCL": ["Production"]},
        commodity_mapping={},
    )


def _seed_engine(crop_item_codes: set[str]) -> tuple:
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(silver_country), [{"area_code": "2", "area": "Afghanistan", "is_aggregate": 0}])
    return engine, crop_item_codes


def test_validate_silver_flags_livestock_leak_as_hard_failure(monkeypatch, tmp_path):
    crop_yaml = tmp_path / "qcl_crop_items.yaml"
    crop_yaml.write_text("items:\n- item_code: '15'\n  item: Wheat\n")
    monkeypatch.setattr("src.validation.silver_schemas.DEFAULT_QCL_CROP_ITEMS_PATH", crop_yaml)

    engine, _ = _seed_engine({"15"})
    with engine.begin() as conn:
        conn.execute(
            insert(silver_production),
            [
                {
                    "area_code": "2", "item_code": "866", "element": "Production", "year": 2022,
                    "year_type": "annual", "value": 100.0, "unit": "t", "value_canonical": 100.0,
                    "canonical_unit": "t", "is_estimate": 0,
                }
            ],
        )
    report = validate_silver(engine, _pipeline_cfg(), _harmonisation())
    leak_findings = [f for f in report.findings if f.check == "qcl_crops_only_no_livestock_leak"]
    assert len(leak_findings) == 1
    assert leak_findings[0].severity == "hard"
    assert leak_findings[0].n_violations == 1
    assert leak_findings[0] in report.hard_failures


def test_validate_silver_no_violations_for_clean_data(monkeypatch, tmp_path):
    crop_yaml = tmp_path / "qcl_crop_items.yaml"
    crop_yaml.write_text("items:\n- item_code: '15'\n  item: Wheat\n")
    monkeypatch.setattr("src.validation.silver_schemas.DEFAULT_QCL_CROP_ITEMS_PATH", crop_yaml)

    engine, _ = _seed_engine({"15"})
    with engine.begin() as conn:
        conn.execute(
            insert(silver_production),
            [
                {
                    "area_code": "2", "item_code": "15", "element": "Production", "year": 2022,
                    "year_type": "annual", "value": 100.0, "unit": "t", "value_canonical": 100.0,
                    "canonical_unit": "t", "is_estimate": 0,
                }
            ],
        )
    report = validate_silver(engine, _pipeline_cfg(), _harmonisation())
    assert report.hard_failures == []


def test_validate_silver_reports_negative_value_where_non_negative_required(monkeypatch, tmp_path):
    crop_yaml = tmp_path / "qcl_crop_items.yaml"
    crop_yaml.write_text("items:\n- item_code: '15'\n  item: Wheat\n")
    monkeypatch.setattr("src.validation.silver_schemas.DEFAULT_QCL_CROP_ITEMS_PATH", crop_yaml)

    engine, _ = _seed_engine({"15"})
    with engine.begin() as conn:
        conn.execute(
            insert(silver_production),
            [
                {
                    "area_code": "2", "item_code": "15", "element": "Production", "year": 2022,
                    "year_type": "annual", "value": -5.0, "unit": "t", "value_canonical": -5.0,
                    "canonical_unit": "t", "is_estimate": 0,
                }
            ],
        )
    report = validate_silver(engine, _pipeline_cfg(), _harmonisation())
    non_neg = [f for f in report.findings if f.check == "non_negative_where_required" and f.table == "silver_production"]
    assert non_neg[0].n_violations == 1
    assert non_neg[0].severity == "soft"
