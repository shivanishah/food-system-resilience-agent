"""Tests for parsing FAO's FBS 'Default composition:' item definitions."""

from __future__ import annotations

from src.elt.fbs_composition import derive_fbs_composition, parse_composition


def test_parse_composition_keeps_commas_inside_member_names():
    # Shaped after the confirmed live description of FBS 2511.
    text = "Default composition: 15 Wheat, 16 Flour, wheat, 17 Bran, wheat, 115 Food preparations, flour, malt extract"
    members = parse_composition(text)
    assert [m["item_code"] for m in members] == ["15", "16", "17", "115"]
    assert members[1]["item"] == "Flour, wheat"
    assert members[3]["item"] == "Food preparations, flour, malt extract"


def test_parse_composition_returns_empty_for_non_composition_text():
    assert parse_composition("") == []
    assert parse_composition(None) == []  # type: ignore[arg-type]
    assert parse_composition("Some free-text description 15 Wheat") == []


def test_parse_composition_single_member():
    assert parse_composition("Default composition: 156 Sugar cane") == [{"item_code": "156", "item": "Sugar cane"}]


def test_derive_fbs_composition_flags_aggregates_and_parent_groups():
    items = [
        {"Item Code": "2511", "Item": "Wheat and products", "Description": "Default composition: 15 Wheat"},
        {"Item Code": "2905", "Item": "Cereals - Excluding Beer", "Description": ""},
        {"Item Code": "2901", "Item": "Grand Total", "Description": ""},
    ]
    itemgroups = [
        {"Item Group Code": "2905", "Item Code": "2511"},
        {"Item Group Code": "2901", "Item Code": "2511"},
        {"Item Group Code": "2901", "Item Code": "2905"},
    ]
    entries = {e["item_code"]: e for e in derive_fbs_composition(items, itemgroups)}
    assert entries["2511"]["is_aggregate"] is False
    assert entries["2511"]["parent_groups"] == ["2901", "2905"]
    assert entries["2511"]["composition"] == [{"item_code": "15", "item": "Wheat"}]
    assert entries["2905"]["is_aggregate"] is True
    assert entries["2901"]["is_aggregate"] is True
    assert entries["2905"]["composition"] == []
