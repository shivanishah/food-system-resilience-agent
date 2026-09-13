"""Derives the FBS item composition from FAOSTAT's own item definitions.

Confirmed live against FBS's ``item`` dimension: every non-aggregate FBS
commodity carries a ``Description`` of the form::

    Default composition: 15 Wheat, 16 Flour, wheat, 17 Bran, wheat, ...

i.e. the FAO Commodity List (FCL) codes that FAO rolls into that FBS item.
QCL primary crops use the same FCL codes, so this is FAO's own
QCL -> FBS correspondence -- the mapping is read from source metadata, never
name-matched or hand-typed.

FBS's ``itemgroup`` dimension separately lists which FBS items are rollups
(``Grand Total``, ``Vegetal Products``, ``Cereals - Excluding Beer`` ...) and
which rollups each item belongs to. Both are written to
``config/fbs_item_composition.yaml``; regenerate by re-running this module
if FAO revises the definitions -- do not hand-edit.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from src.api.client import FAOSTATClient

DEFAULT_FBS_COMPOSITION_PATH = Path("config/fbs_item_composition.yaml")

COMPOSITION_PREFIX = "Default composition:"

# One "<code> <name>" member. Names contain commas ("Flour, wheat"), so a
# member ends only where the next ", <digits> " begins -- a comma followed by
# a bare number is the only unambiguous boundary in FAO's format.
_MEMBER_PATTERN = re.compile(r"(?:^|,\s*)(\d+)\s+(.*?)(?=,\s*\d+\s|\Z)", re.DOTALL)


def parse_composition(description: str) -> list[dict[str, str]]:
    """Parse an FBS ``Description`` into its member FCL items.

    Returns ``[]`` when the description is not a ``Default composition:``
    list (aggregates and a few special items carry an empty description).
    """
    text = (description or "").strip()
    if not text.startswith(COMPOSITION_PREFIX):
        return []
    body = text[len(COMPOSITION_PREFIX):].strip()
    return [
        {"item_code": match.group(1), "item": match.group(2).strip()}
        for match in _MEMBER_PATTERN.finditer(body)
    ]


def derive_fbs_composition(
    item_rows: list[dict[str, Any]], itemgroup_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Combine the raw FBS ``item`` and ``itemgroup`` payloads.

    One entry per FBS item: its composition members, whether it is itself a
    rollup group, and which rollup groups contain it. Sorted by item code.
    """
    group_codes = {row["Item Group Code"] for row in itemgroup_rows}
    parents: dict[str, set[str]] = {}
    for row in itemgroup_rows:
        parents.setdefault(row["Item Code"], set()).add(row["Item Group Code"])

    entries = []
    for row in item_rows:
        code = row["Item Code"]
        entries.append(
            {
                "item_code": code,
                "item": row["Item"],
                "is_aggregate": code in group_codes,
                "parent_groups": sorted(parents.get(code, set()) - {code}, key=int),
                "composition": parse_composition(row.get("Description", "")),
            }
        )
    return sorted(entries, key=lambda e: int(e["item_code"]))


def refresh_fbs_composition(
    client: FAOSTATClient, output_path: Path = DEFAULT_FBS_COMPOSITION_PATH
) -> list[dict[str, Any]]:
    """Re-derive the FBS composition live and write it to ``output_path``."""
    items = client.get_items("FBS").data
    itemgroups = client.get_dimension("FBS", "itemgroup").data
    entries = derive_fbs_composition(items, itemgroups)
    doc = {
        "_comment": (
            "Auto-derived by src/elt/fbs_composition.py from FAOSTAT FBS item "
            "definitions ('Default composition:' descriptions) and the FBS "
            "itemgroup dimension. Member codes are FAO Commodity List codes, "
            "shared with QCL primary crops. Regenerate by re-running that "
            "module -- do not hand-edit."
        ),
        "n_items": len(entries),
        "n_with_composition": sum(1 for e in entries if e["composition"]),
        "items": entries,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
    return entries


def load_fbs_composition(path: Path = DEFAULT_FBS_COMPOSITION_PATH) -> list[dict[str, Any]]:
    """Load the cached FBS composition (does not hit the API)."""
    with path.open() as f:
        doc = yaml.safe_load(f)
    return doc["items"]


if __name__ == "__main__":
    with FAOSTATClient() as _client:
        _client.authenticate()
        _entries = refresh_fbs_composition(_client)
    print(
        f"Wrote {len(_entries)} FBS items "
        f"({sum(1 for e in _entries if e['composition'])} with a composition) "
        f"to {DEFAULT_FBS_COMPOSITION_PATH}"
    )
