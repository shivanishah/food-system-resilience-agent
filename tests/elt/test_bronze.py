"""Tests for Bronze natural-key construction and idempotent loading."""

from __future__ import annotations

from sqlalchemy import create_engine, select

from src.database.models import BRONZE_TABLES, metadata
from src.elt.bronze import load_bronze, observation_to_row
from src.validation.schemas import DataObservation


def _wheat_obs(value: str = "100", flag: str = "A") -> DataObservation:
    return DataObservation(
        **{
            "Domain Code": "QCL", "Domain": "Crops and livestock products",
            "Area Code": "2", "Area": "Afghanistan",
            "Element Code": "5510", "Element": "Production",
            "Item Code": "15", "Item": "Wheat",
            "Year Code": "2022", "Year": "2022",
            "Unit": "t", "Value": value, "Flag": flag,
        }
    )


def test_observation_to_row_natural_key_includes_value_flag_note():
    row_a = observation_to_row(_wheat_obs(value="100", flag="A"), "QCL", "2026-01-01T00:00:00Z", "run-1")
    row_b = observation_to_row(_wheat_obs(value="200", flag="A"), "QCL", "2026-01-01T00:00:00Z", "run-1")
    assert row_a["natural_key"] != row_b["natural_key"]


def test_observation_to_row_same_observation_same_natural_key_regardless_of_provenance():
    row_a = observation_to_row(_wheat_obs(), "QCL", "2026-01-01T00:00:00Z", "run-1")
    row_b = observation_to_row(_wheat_obs(), "QCL", "2026-01-02T00:00:00Z", "run-2")
    assert row_a["natural_key"] == row_b["natural_key"]


def _memory_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def test_load_bronze_is_idempotent_on_rerun():
    engine = _memory_engine()
    obs = [_wheat_obs()]
    first = load_bronze(engine, "QCL", obs, "run-1")
    second = load_bronze(engine, "QCL", obs, "run-2")
    assert first == 1
    assert second == 1  # delete-and-reload, not an accumulating duplicate
    with engine.connect() as conn:
        rows = conn.execute(select(BRONZE_TABLES["QCL"])).fetchall()
    assert len(rows) == 1


def test_load_bronze_distinct_observations_both_kept():
    engine = _memory_engine()
    obs = [_wheat_obs(value="100"), _wheat_obs(value="200")]
    inserted = load_bronze(engine, "QCL", obs, "run-1")
    assert inserted == 2
