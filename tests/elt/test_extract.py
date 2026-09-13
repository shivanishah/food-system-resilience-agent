"""Tests for year-code parsing, element-name resolution, and the
client-side year re-filter -- the pure-logic parts of src/elt/extract.py
that don't require a live API call."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, insert

from src.database.models import BRONZE_TABLES, metadata
from src.elt.config import DomainSpec, PipelineConfig
from src.elt.extract import (
    _parse_requested_element,
    coverage_for_existing_bronze,
    extract_domain,
    restrict_to_item_dimension,
    parse_year_code,
    resolve_elements,
    scope_filter_items,
    scope_filter_rows,
)
from src.validation.schemas import DataObservation


def test_parse_year_code_annual():
    assert parse_year_code("2022") == (2022, "annual", None)


def test_parse_year_code_three_year_average_keyed_to_end_year():
    assert parse_year_code("19992001") == (2001, "3yr_avg", "1999-2001")


def test_parse_year_code_unrecognised_format_is_not_guessed():
    assert parse_year_code("abc") == (None, "unknown", None)
    assert parse_year_code("123") == (None, "unknown", None)


def _obs(year_code: str) -> DataObservation:
    return DataObservation(
        **{
            "Domain Code": "QCL", "Domain": "Crops and livestock products",
            "Area Code": "2", "Area": "Afghanistan",
            "Element Code": "5510", "Element": "Production",
            "Item Code": "15", "Item": "Wheat",
            "Year Code": year_code, "Year": year_code,
            "Unit": "t", "Value": "100",
        }
    )


def test_scope_filter_rows_keeps_only_requested_window():
    rows = [_obs("2013"), _obs("2014"), _obs("2024"), _obs("2025")]
    kept = scope_filter_rows(rows, 2014, 2024)
    assert [r.year_code for r in kept] == ["2014", "2024"]


def test_scope_filter_rows_keeps_three_year_average_by_end_year():
    rows = [_obs("20112013"), _obs("20222024"), _obs("20232025")]
    kept = scope_filter_rows(rows, 2014, 2024)
    assert [r.year_code for r in kept] == ["20222024"]


@dataclass
class _FakeAPIResult:
    data: list[dict]


class _FakeClient:
    """Duck-typed stand-in for FAOSTATClient.get_elements -- no HTTP calls."""

    def __init__(self, element_rows: list[dict]) -> None:
        self._element_rows = element_rows

    def get_elements(self, domain: str) -> _FakeAPIResult:
        return _FakeAPIResult(data=self._element_rows)


def test_resolve_elements_unambiguous_name():
    client = _FakeClient([{"Element Code": "5312", "Element": "Area harvested", "Unit": "ha"}])
    resolved, missing = resolve_elements(client, "QCL", ["Area harvested"])
    assert resolved == {"Area harvested": "5312"}
    assert missing == []


def test_resolve_elements_ambiguous_name_without_unit_is_reported_missing_not_guessed():
    client = _FakeClient(
        [
            {"Element Code": "5510", "Element": "Production", "Unit": "t"},
            {"Element Code": "5323", "Element": "Production", "Unit": "1000 An"},
        ]
    )
    resolved, missing = resolve_elements(client, "QCL", ["Production"])
    assert resolved == {}
    assert missing == ["Production"]


def test_resolve_elements_disambiguated_by_unit():
    client = _FakeClient(
        [
            {"Element Code": "5510", "Element": "Production", "Unit": "t"},
            {"Element Code": "5323", "Element": "Production", "Unit": "1000 An"},
        ]
    )
    resolved, missing = resolve_elements(client, "QCL", ["Production|t"])
    assert resolved == {"Production|t": "5510"}
    assert missing == []


def test_resolve_elements_name_not_found():
    client = _FakeClient([{"Element Code": "5312", "Element": "Area harvested", "Unit": "ha"}])
    resolved, missing = resolve_elements(client, "QCL", ["Nonexistent element"])
    assert resolved == {}
    assert missing == ["Nonexistent element"]


def test_parse_requested_element_plain_name():
    assert _parse_requested_element("Area harvested") == ("Area harvested", None)


def test_parse_requested_element_with_unit():
    assert _parse_requested_element("Production|t") == ("Production", "t")


# --- coverage rebuilt from Bronze (resume without re-fetching) -----------


class _FakeDiscoveryClient:
    """Duck-typed FAOSTATClient covering only the small metadata endpoints
    coverage_for_existing_bronze uses -- it must never need a data call."""

    def __init__(self) -> None:
        self.data_calls = 0

    def get_years(self, domain: str) -> _FakeAPIResult:
        return _FakeAPIResult(data=[{"Year Code": str(y), "Year": str(y)} for y in range(2013, 2026)])

    def get_elements(self, domain: str) -> _FakeAPIResult:
        return _FakeAPIResult(
            data=[
                {"Element Code": "5910", "Element": "Export quantity", "Unit": "t"},
                {"Element Code": "5922", "Element": "Export value", "Unit": "1000 USD"},
            ]
        )

    def get_dimension(self, domain: str, dimension_code: str) -> _FakeAPIResult:
        n = 3 if dimension_code == "reporterarea" else 4
        return _FakeAPIResult(data=[{"code": str(i)} for i in range(n)])

    def get_areas(self, domain: str) -> _FakeAPIResult:
        return _FakeAPIResult(data=[{"Country Code": "2"}, {"Country Code": "3"}])

    def get_items(self, domain: str) -> _FakeAPIResult:
        # 15 and 56 are configured; 99 exists in the domain but is not wanted.
        return _FakeAPIResult(data=[{"Item Code": "15"}, {"Item Code": "56"}, {"Item Code": "99"}])

    def get_data(self, *args, **kwargs):  # pragma: no cover - must not be called
        self.data_calls += 1
        raise AssertionError("coverage_for_existing_bronze must not re-request observations")


def _tm_config() -> PipelineConfig:
    return PipelineConfig(
        start_year=2014,
        end_year=2024,
        min_years_for_history=7,
        domains={
            "TM": DomainSpec(
                code="TM", priority="core", silver_table="silver_trade", bilateral=True,
                item_scope="item_set:food_commodities",
                elements=["Export quantity|t", "Export value", "Import quantity|t"],
            )
        },
        item_sets={
            "food_commodities": [
                {"item_code": "15", "item": "Wheat"},
                {"item_code": "56", "item": "Maize (corn)"},
            ]
        },
    )


def _seeded_tm_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    rows = [
        {"natural_key": "a", "domain_code": "TM", "item_code": "15", "year_code": "2015",
         "retrieved_at": "t", "pipeline_run_id": "run-1", "source_domain": "FAOSTAT_TM"},
        {"natural_key": "b", "domain_code": "TM", "item_code": "15", "year_code": "2020",
         "retrieved_at": "t", "pipeline_run_id": "run-1", "source_domain": "FAOSTAT_TM"},
        {"natural_key": "c", "domain_code": "TM", "item_code": "56", "year_code": "2023",
         "retrieved_at": "t", "pipeline_run_id": "run-1", "source_domain": "FAOSTAT_TM"},
    ]
    with engine.begin() as conn:
        conn.execute(insert(BRONZE_TABLES["TM"]), rows)
    return engine


def test_coverage_for_existing_bronze_reads_retrieved_side_from_bronze():
    config = _tm_config()
    result = coverage_for_existing_bronze(
        _FakeDiscoveryClient(), _seeded_tm_engine(), "TM", config.domains["TM"], config
    )
    assert result.bronze_rows == 3
    assert result.rows_received == 3
    assert result.coverage.retrieved_item_count == 2
    assert (result.coverage.actual_min_year, result.coverage.actual_max_year) == (2015, 2023)


def test_coverage_for_existing_bronze_keeps_requested_side_from_live_discovery():
    config = _tm_config()
    coverage = coverage_for_existing_bronze(
        _FakeDiscoveryClient(), _seeded_tm_engine(), "TM", config.domains["TM"], config
    ).coverage
    assert (coverage.requested_start_year, coverage.requested_end_year) == (2014, 2024)
    assert coverage.requested_item_count == 2
    # 2013 and 2025 exist in the domain but are outside the requested window.
    assert coverage.available_requested_years == list(range(2014, 2025))
    assert coverage.missing_requested_years == []
    assert coverage.country_count == 3
    assert coverage.partner_count == 4


def test_coverage_for_existing_bronze_reports_an_unresolvable_element():
    config = _tm_config()
    coverage = coverage_for_existing_bronze(
        _FakeDiscoveryClient(), _seeded_tm_engine(), "TM", config.domains["TM"], config
    ).coverage
    assert coverage.resolved_element_codes == {"Export quantity|t": "5910", "Export value": "5922"}
    assert coverage.missing_elements == ["Import quantity|t"]


# --- client-side item scoping (broken server-side item filter) -----------


def _item_obs(item_code: str) -> DataObservation:
    return DataObservation(
        **{
            "Domain Code": "CAHD", "Domain": "Cost and Affordability of a Healthy Diet",
            "Area Code": "10", "Area": "Australia",
            "Element Code": "6120", "Element": "Value",
            "Item Code": item_code, "Item": "an indicator",
            "Year Code": "2020", "Year": "2020",
            "Unit": "PPP", "Value": "1.5",
        }
    )


def test_scope_filter_items_keeps_only_requested_codes():
    rows = [_item_obs("70040"), _item_obs("70041"), _item_obs("7005")]
    kept = scope_filter_items(rows, {"70040", "7005"})
    assert [r.item_code for r in kept] == ["70040", "7005"]


def test_scope_filter_items_drops_prefix_matched_child_items():
    """CP's 23013 request also returns 230131/230132 from the server."""
    rows = [_item_obs("23013"), _item_obs("230131"), _item_obs("230132")]
    assert [r.item_code for r in scope_filter_items(rows, {"23013"})] == ["23013"]


def test_client_item_filter_is_rejected_for_a_bilateral_domain():
    """Fetching a reporter x partner sweep unfiltered by item would multiply
    an already multi-hour extraction by the whole item dimension."""
    config = _tm_config()
    spec = config.domains["TM"].model_copy(update={"item_filter": "client"})
    with pytest.raises(ValueError, match="not supported for a bilateral domain"):
        extract_domain(_FakeDiscoveryClient(), _seeded_tm_engine(), "run-1", "TM", spec, config)


def test_restrict_to_item_dimension_drops_codes_the_domain_does_not_list():
    """TM's commodity list is copied from QCL's crops, and 8 of those 162 are
    not in TM's item dimension at all -- requesting them produced an
    unexplainable shortfall on every run."""
    requestable, absent, dimension = restrict_to_item_dimension(
        _FakeDiscoveryClient(), "TM", ["15", "56", "254"]
    )
    assert requestable == ["15", "56"]
    assert absent == ["254"]
    assert set(dimension) == {"15", "56", "99"}


def test_restrict_to_item_dimension_passes_an_unbounded_scope_through():
    assert restrict_to_item_dimension(_FakeDiscoveryClient(), "FBS", None) == (None, [], [])


def test_coverage_for_existing_bronze_records_the_item_codes_behind_the_counts():
    config = _tm_config()
    coverage = coverage_for_existing_bronze(
        _FakeDiscoveryClient(), _seeded_tm_engine(), "TM", config.domains["TM"], config
    ).coverage
    assert coverage.requested_item_codes == ["15", "56"]
    assert coverage.retrieved_item_codes == ["15", "56"]
    assert coverage.items_absent_from_dimension == []
