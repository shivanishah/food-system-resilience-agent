"""Tests for the QCL crops-only itemgroup-derivation algorithm."""

from __future__ import annotations

from src.elt.qcl_items import derive_crop_items

# Shaped after the confirmed live payload: QC members include both leaf crops
# and rollup sub-groups (e.g. "Cereals, primary" is itself group "1717"), and
# QA/QL are livestock groups that must never contribute items.
SAMPLE_ITEMGROUP_ROWS = [
    {"Item Group Code": "QC", "Item Group": "Crops, primary", "Item Code": "1714", "Item": "Crops, primary"},
    # "1714" is, confirmed live, itself a distinct (self-referential) item
    # group -- it must be excluded as a rollup even though it's also listed
    # as a QC member above.
    {"Item Group Code": "1714", "Item Group": "Crops, primary", "Item Code": "1714", "Item": "Crops, primary"},
    {"Item Group Code": "QC", "Item Group": "Crops, primary", "Item Code": "1717", "Item": "Cereals, primary"},
    {"Item Group Code": "QC", "Item Group": "Crops, primary", "Item Code": "15", "Item": "Wheat"},
    {"Item Group Code": "QC", "Item Group": "Crops, primary", "Item Code": "56", "Item": "Maize (corn)"},
    {"Item Group Code": "1717", "Item Group": "Cereals, primary", "Item Code": "15", "Item": "Wheat"},
    {"Item Group Code": "1717", "Item Group": "Cereals, primary", "Item Code": "56", "Item": "Maize (corn)"},
    {"Item Group Code": "QA", "Item Group": "Live Animals", "Item Code": "1756", "Item": "Live Animals"},
    {"Item Group Code": "QA", "Item Group": "Live Animals", "Item Code": "866", "Item": "Cattle"},
    {"Item Group Code": "QL", "Item Group": "Livestock primary", "Item Code": "882", "Item": "Raw milk of cattle"},
]


def test_derive_crop_items_keeps_only_leaf_crops_under_qc():
    items = derive_crop_items(SAMPLE_ITEMGROUP_ROWS)
    codes = {i["item_code"] for i in items}
    assert codes == {"15", "56"}


def test_derive_crop_items_excludes_rollup_group_codes():
    items = derive_crop_items(SAMPLE_ITEMGROUP_ROWS)
    codes = {i["item_code"] for i in items}
    # 1714 (self-referential "Crops, primary") and 1717 ("Cereals, primary")
    # are themselves item-group codes elsewhere in the payload, so they must
    # not appear as leaf items even though they're listed under QC.
    assert "1714" not in codes
    assert "1717" not in codes


def test_derive_crop_items_excludes_livestock():
    items = derive_crop_items(SAMPLE_ITEMGROUP_ROWS)
    codes = {i["item_code"] for i in items}
    assert "1756" not in codes  # Live Animals (QA)
    assert "866" not in codes  # Cattle (QA)
    assert "882" not in codes  # Raw milk of cattle (QL)


def test_derive_crop_items_sorted_by_code():
    items = derive_crop_items(SAMPLE_ITEMGROUP_ROWS)
    assert [i["item_code"] for i in items] == ["15", "56"]
