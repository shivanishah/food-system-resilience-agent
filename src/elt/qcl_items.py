"""Derives the QCL crops-only item list from FAOSTAT's own classification.

Confirmed live against QCL's ``itemgroup`` dimension: item group ``QC``
("Crops, primary") has 176 member item codes, 14 of which are themselves
rollup group codes (e.g. "Cereals, primary", "Oilcrops Primary") rather than
individual crops. Removing those leaves 162 leaf crop items. ``QA``/``QL``/
``QP`` (live animals / livestock primary / livestock processed) and ``QD``
(crops processed, e.g. beer, oils, cotton lint) are excluded simply by only
taking ``QC``'s members -- no substring matching on item names is used.

This is reproducible and regenerable: re-run :func:`derive_crop_items` (or
this module as a script) if FAO revises the QCL classification, and
``config/qcl_crop_items.yaml`` will be rewritten from scratch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.api.client import FAOSTATClient

CROP_ITEMGROUP = "QC"
EXCLUDED_ITEMGROUPS = ("QA", "QL", "QP", "QD")
EXCLUDED_ITEMGROUPS_MEANING = {
    "QA": "Live Animals",
    "QL": "Livestock primary",
    "QP": "Livestock processed",
    "QD": "Crops processed (not primary production)",
}

DEFAULT_QCL_CROP_ITEMS_PATH = Path("config/qcl_crop_items.yaml")


def derive_crop_items(itemgroup_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the leaf crop items under ``QC``, sorted by item code.

    ``itemgroup_rows`` is the raw ``GET .../QCL/itemgroup`` payload: one row
    per (item group, member item) pair. A row is a *leaf crop* if its item
    group is ``QC`` and its item code is not itself a group code anywhere in
    the payload (i.e. it is not a rollup/aggregate).
    """
    all_group_codes = {row["Item Group Code"] for row in itemgroup_rows}
    qc_members = [row for row in itemgroup_rows if row["Item Group Code"] == CROP_ITEMGROUP]
    leaf = [row for row in qc_members if row["Item Code"] not in all_group_codes]
    items = [{"item_code": row["Item Code"], "item": row["Item"]} for row in leaf]
    return sorted(items, key=lambda r: r["item_code"])


def refresh_qcl_crop_items(
    client: FAOSTATClient, output_path: Path = DEFAULT_QCL_CROP_ITEMS_PATH
) -> list[dict[str, str]]:
    """Re-derive the crop item list live and write it to ``output_path``."""
    rows = client.get_dimension("QCL", "itemgroup").data
    items = derive_crop_items(rows)
    doc = {
        "_comment": (
            "Auto-derived by src/elt/qcl_items.py from FAOSTAT QCL's own "
            "itemgroup classification: item group QC (\"Crops, primary\") "
            "members minus rows that are themselves rollup group codes. "
            "Regenerate by re-running that module if FAO revises the "
            "classification -- do not hand-edit."
        ),
        "source_itemgroup": CROP_ITEMGROUP,
        "excluded_itemgroups": list(EXCLUDED_ITEMGROUPS),
        "excluded_itemgroups_meaning": EXCLUDED_ITEMGROUPS_MEANING,
        "n_items": len(items),
        "items": items,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
    return items


def load_qcl_crop_items(path: Path = DEFAULT_QCL_CROP_ITEMS_PATH) -> list[dict[str, str]]:
    """Load the cached crop item list (does not hit the API)."""
    with path.open() as f:
        doc = yaml.safe_load(f)
    return doc["items"]


if __name__ == "__main__":
    with FAOSTATClient() as _client:
        _client.authenticate()
        _items = refresh_qcl_crop_items(_client)
    print(f"Wrote {len(_items)} crop items to {DEFAULT_QCL_CROP_ITEMS_PATH}")
