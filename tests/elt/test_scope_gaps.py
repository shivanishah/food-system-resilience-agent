"""Tests for why-is-this-item-missing classification.

The safety property under test: a gap is only ever forgiven when it has been
positively established as source behaviour. Everything else must keep
counting against the domain's status, because FAOSTAT's item filter fails
silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.elt.scope_gaps import ScopeGap, classify_missing_items
from src.validation.schemas import DataObservation


def _obs(item_code: str, year_code: str) -> DataObservation:
    return DataObservation(
        **{
            "Domain Code": "QCL", "Domain": "Crops",
            "Area Code": "2", "Area": "Afghanistan",
            "Element Code": "5510", "Element": "Production",
            "Item Code": item_code, "Item": "an item",
            "Year Code": year_code, "Year": year_code,
            "Unit": "t", "Value": "1",
        }
    )


@dataclass
class _FakeDataResult:
    rows: list[DataObservation]


class _FakeClient:
    """Returns whatever rows the test declares for a given item code."""

    def __init__(self, rows_by_item: dict[str, list[DataObservation]]) -> None:
        self._rows_by_item = rows_by_item
        self.calls: list[str] = []

    def get_data(self, domain: str, **kwargs) -> _FakeDataResult:
        item = kwargs.get("item", [None])[0]
        self.calls.append(item)
        return _FakeDataResult(rows=self._rows_by_item.get(item, []))


def _classify(client, missing, dimension, retrieved, bilateral=False):
    return classify_missing_items(
        client, "QCL", missing, set(dimension), set(retrieved), 2014, 2024, bilateral=bilateral
    )


def test_code_not_in_the_domain_dimension_is_a_request_bug_not_a_data_gap():
    gaps = _classify(_FakeClient({}), ["254"], dimension={"15"}, retrieved={"15"})
    assert [(g.item_code, g.classification) for g in gaps] == [("254", "absent_from_dimension")]
    assert gaps[0].counts_against_status is False


def test_series_published_only_outside_the_window_is_explained():
    """QCL item 839 (balata gums) has 272 rows spanning 1961-1990."""
    client = _FakeClient({"839": [_obs("839", "1961"), _obs("839", "1990")]})
    gaps = _classify(client, ["839"], dimension={"839", "15"}, retrieved={"15"})
    assert gaps[0].classification == "out_of_window"
    assert gaps[0].counts_against_status is False
    assert "1961-1990" in gaps[0].detail


def test_empty_item_with_a_working_same_width_control_is_a_source_gap():
    """GT item 6966 returns nothing anywhere; a same-width sibling proves the
    filter itself is sound at that code width."""
    client = _FakeClient({"6966": [], "5058": [_obs("5058", "2020")]})
    gaps = _classify(client, ["6966"], dimension={"6966", "5058"}, retrieved={"5058"})
    assert gaps[0].classification == "absent_at_source"
    assert gaps[0].counts_against_status is False
    assert "5058" in gaps[0].detail


def test_empty_item_whose_control_is_also_empty_is_blamed_on_the_filter():
    client = _FakeClient({"6966": [], "5058": []})
    gaps = _classify(client, ["6966"], dimension={"6966", "5058"}, retrieved={"5058"})
    assert gaps[0].classification == "filter_bug"
    assert gaps[0].counts_against_status is True


def test_item_returning_in_window_rows_on_its_own_is_unverified_not_forgiven():
    """If querying it alone returns data for the requested years, it should
    have been ingested -- that is a defect, never an explained gap."""
    client = _FakeClient({"15": [_obs("15", "2020")]})
    gaps = _classify(client, ["15"], dimension={"15", "56"}, retrieved={"56"})
    assert gaps[0].classification == "unverified"
    assert gaps[0].counts_against_status is True


def test_no_same_width_control_available_stays_unverified():
    client = _FakeClient({"6966": []})
    gaps = _classify(client, ["6966"], dimension={"6966", "15"}, retrieved={"15"})
    assert gaps[0].classification == "unverified"
    assert gaps[0].counts_against_status is True


def test_bilateral_domain_is_never_probed_with_an_unbounded_query():
    """An unfiltered probe on a reporter x partner domain is unbounded, so a
    code in the dimension is left unverified rather than guessed at."""
    client = _FakeClient({"15": [_obs("15", "2020")]})
    gaps = _classify(client, ["15"], dimension={"15"}, retrieved=set(), bilateral=True)
    assert gaps[0].classification == "unverified"
    assert gaps[0].counts_against_status is True
    assert client.calls == []  # no probe was issued at all


def test_absent_from_dimension_still_short_circuits_on_a_bilateral_domain():
    gaps = _classify(_FakeClient({}), ["254"], dimension={"15"}, retrieved={"15"}, bilateral=True)
    assert gaps[0].classification == "absent_from_dimension"
    assert gaps[0].counts_against_status is False


def test_scope_gap_counts_against_status_only_for_unexplained_classes():
    assert ScopeGap("d", "1", "out_of_window", "").counts_against_status is False
    assert ScopeGap("d", "1", "absent_at_source", "").counts_against_status is False
    assert ScopeGap("d", "1", "absent_from_dimension", "").counts_against_status is False
    assert ScopeGap("d", "1", "filter_bug", "").counts_against_status is True
    assert ScopeGap("d", "1", "unverified", "").counts_against_status is True


# --- persistence -----------------------------------------------------------


def _memory_engine():
    from sqlalchemy import create_engine

    from src.database.models import metadata

    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


def test_recorded_gaps_survive_to_be_loaded_back():
    from src.elt.scope_gaps import domains_with_recorded_gaps, load_gaps, record_gaps

    engine = _memory_engine()
    record_gaps(engine, "run-1", "QCL", [ScopeGap("QCL", "839", "out_of_window", "1961-1990")])
    record_gaps(engine, "run-1", "GT", [ScopeGap("GT", "6966", "absent_at_source", "nothing anywhere")])

    loaded = load_gaps(engine, "run-1")
    assert [(g.domain, g.item_code, g.classification) for g in loaded] == [
        ("GT", "6966", "absent_at_source"),
        ("QCL", "839", "out_of_window"),
    ]
    assert domains_with_recorded_gaps(engine, "run-1") == {"QCL", "GT"}


def test_a_report_only_run_keeps_gaps_from_domains_it_did_not_touch():
    """The bug this fixes: a run that touched only TM regenerated the report
    with TM's gaps alone, silently dropping QCL's and GT's."""
    from src.elt.scope_gaps import load_gaps, record_gaps

    engine = _memory_engine()
    record_gaps(engine, "run-1", "QCL", [ScopeGap("QCL", "839", "out_of_window", "1961-1990")])
    record_gaps(engine, "run-1", "TM", [ScopeGap("TM", "254", "absent_from_dimension", "not listed")])

    assert {g.domain for g in load_gaps(engine, "run-1")} == {"QCL", "TM"}


def test_re_recording_a_domain_replaces_rather_than_appends():
    from src.elt.scope_gaps import load_gaps, record_gaps

    engine = _memory_engine()
    record_gaps(engine, "run-1", "GT", [ScopeGap("GT", "6966", "unverified", "first attempt")])
    record_gaps(engine, "run-1", "GT", [ScopeGap("GT", "6966", "absent_at_source", "proven")])
    loaded = load_gaps(engine, "run-1")
    assert len(loaded) == 1
    assert loaded[0].classification == "absent_at_source"


def test_gaps_are_scoped_to_their_pipeline_run():
    from src.elt.scope_gaps import load_gaps, record_gaps

    engine = _memory_engine()
    record_gaps(engine, "run-1", "GT", [ScopeGap("GT", "6966", "absent_at_source", "x")])
    record_gaps(engine, "run-2", "GT", [ScopeGap("GT", "6966", "absent_at_source", "x")])
    assert len(load_gaps(engine, "run-1")) == 1


def test_recording_no_gaps_clears_a_domains_previous_record():
    from src.elt.scope_gaps import load_gaps, record_gaps

    engine = _memory_engine()
    record_gaps(engine, "run-1", "QCL", [ScopeGap("QCL", "839", "out_of_window", "x")])
    record_gaps(engine, "run-1", "QCL", [])
    assert load_gaps(engine, "run-1") == []
