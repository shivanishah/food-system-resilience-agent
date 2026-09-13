"""Tests for the pipeline's domain-selection CLI parsing -- the part that
decides what a resumed run actually redoes."""

from __future__ import annotations

import pytest

from src.elt.extract import DomainCoverage
from src.elt.pipeline import _determine_status, _parse_domain_list, run_pipeline
from src.elt.scope_gaps import ScopeGap


def test_parse_domain_list_default_is_every_domain():
    assert _parse_domain_list(None) is None


def test_parse_domain_list_none_keyword_selects_no_domain():
    assert _parse_domain_list("none") == []
    assert _parse_domain_list("NONE") == []


def test_parse_domain_list_normalises_case_and_whitespace():
    assert _parse_domain_list(" tm , rfm ") == ["TM", "RFM"]


def test_run_pipeline_rejects_unknown_domain_before_touching_the_api():
    with pytest.raises(ValueError, match="Unknown domain"):
        run_pipeline(domains=["NOT_A_DOMAIN"])


def test_run_pipeline_rejects_unknown_reuse_bronze_domain():
    with pytest.raises(ValueError, match="Unknown domain"):
        run_pipeline(domains=["QCL"], reuse_bronze=["NOT_A_DOMAIN"])


# --- per-domain status ----------------------------------------------------


def _coverage(requested_items: int | None, retrieved_items: int, missing_elements=()):
    return DomainCoverage(
        domain="X", requested_start_year=2014, requested_end_year=2024,
        year_codes_used=[], available_requested_years=[], missing_requested_years=[],
        actual_min_year=2014, actual_max_year=2024, requested_elements=None,
        missing_elements=list(missing_elements),
        requested_item_count=requested_items, retrieved_item_count=retrieved_items,
    )


def test_status_failed_when_nothing_landed():
    assert _determine_status(_coverage(8, 0), bronze_rows=0) == "failed"


def test_status_success_when_every_requested_item_came_back():
    assert _determine_status(_coverage(8, 8), bronze_rows=100) == "success"


def test_status_success_when_no_item_list_was_requested():
    assert _determine_status(_coverage(None, 122), bronze_rows=100) == "success"


def test_status_partial_when_a_shortfall_is_not_explained():
    """CAHD returned 2 of 8 requested items -- including the Cost of a Healthy
    Diet -- and was reported as success before this check existed."""
    gaps = [ScopeGap("CAHD", "70040", "filter_bug", "swallowed by the item filter")]
    assert _determine_status(_coverage(8, 2), bronze_rows=3440, gaps=gaps) == "partial"


def test_status_success_when_every_shortfall_is_an_established_source_gap():
    """A discontinued crop and an empty placeholder category are findings, not
    defects -- once proven live."""
    gaps = [
        ScopeGap("QCL", "839", "out_of_window", "published 1961-1990, nothing in 2014-2024"),
        ScopeGap("QCL", "999", "absent_at_source", "no observation in any year or country"),
    ]
    assert _determine_status(_coverage(162, 160), bronze_rows=432189, gaps=gaps) == "success"


def test_status_partial_when_even_one_gap_is_unverified():
    gaps = [
        ScopeGap("GT", "6966", "absent_at_source", "no observation anywhere"),
        ScopeGap("GT", "1234", "unverified", "classification failed"),
    ]
    assert _determine_status(_coverage(45, 43), bronze_rows=582911, gaps=gaps) == "partial"


def test_status_partial_when_a_requested_element_did_not_resolve():
    assert _determine_status(_coverage(8, 8, missing_elements=["Yield|kg/ha"]), bronze_rows=100) == "partial"
