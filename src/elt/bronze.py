"""Bronze load: raw (but scoped) API values, preserved close to source.

Delete-and-reload per domain: ``bronze_<domain>`` is truncated, then bulk
inserted with ``INSERT OR IGNORE`` against ``UNIQUE(natural_key)``. Re-running
the pipeline reproduces identical Bronze content and is safe to repeat.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, delete, insert

from src.database.models import BRONZE_TABLES
from src.validation.schemas import DataObservation

logger = logging.getLogger(__name__)

_NATURAL_KEY_FIELDS = (
    "domain_code", "area_code", "reporter_area_code", "partner_area_code",
    "item_code", "element_code", "month_code", "source_code", "release_code",
    "year_code", "value", "flag", "note",
)


def _extra(obs: DataObservation, *keys: str) -> str | None:
    """Read one of ``keys`` from an ``extra="allow"`` field not declared on
    :class:`DataObservation` (e.g. CP's ``Months Code``, GT's ``Source Code``,
    CAHD's ``Release Code``)."""
    extras = obs.model_extra or {}
    for key in keys:
        if key in extras and extras[key] not in (None, ""):
            return str(extras[key])
    return None


def observation_to_row(obs: DataObservation, domain: str, retrieved_at: str, pipeline_run_id: str) -> dict[str, Any]:
    """Map one validated :class:`DataObservation` to a ``bronze_<domain>`` row."""
    row = {
        "domain_code": domain,
        "area_code": obs.area_code,
        "area": obs.area,
        "reporter_area_code": obs.reporter_area_code,
        "reporter_area": obs.reporter_area,
        "partner_area_code": obs.partner_area_code,
        "partner_area": obs.partner_area,
        "item_code": obs.item_code,
        "item": obs.item,
        "element_code": obs.element_code,
        "element": obs.element,
        "month_code": _extra(obs, "Months Code"),
        "month": _extra(obs, "Months"),
        "source_code": _extra(obs, "Source Code"),
        "source": _extra(obs, "Source"),
        "release_code": _extra(obs, "Release Code"),
        "release": _extra(obs, "Release"),
        "year_code": obs.year_code,
        "year": obs.year,
        "unit": obs.unit,
        "value": obs.value,
        "flag": obs.flag,
        "flag_description": obs.flag_description,
        "note": obs.note,
        "retrieved_at": retrieved_at,
        "pipeline_run_id": pipeline_run_id,
        "source_domain": f"FAOSTAT_{domain}",
        "request_parameters": None,
    }
    key_parts = ["" if row.get(f) is None else str(row.get(f)) for f in _NATURAL_KEY_FIELDS]
    row["natural_key"] = "|".join(key_parts)
    return row


def truncate_bronze(engine: Engine, domain: str) -> None:
    """Clear ``bronze_<domain>`` at the start of a domain's load."""
    with engine.begin() as conn:
        conn.execute(delete(BRONZE_TABLES[domain]))


def append_bronze_batch(
    engine: Engine, domain: str, observations: list[DataObservation], pipeline_run_id: str
) -> int:
    """Insert one fetched batch. Call :func:`truncate_bronze` once first --
    this does not delete anything itself, so a domain's full extraction can
    stream many batches through this without ever holding every observation
    in memory at once (a real domain-sized list, e.g. TM's full bilateral
    sweep, is too large to accumulate before writing -- confirmed live: an
    accumulate-then-write design OOM-killed the pipeline mid-TM-fetch)."""
    if not observations:
        return 0
    table = BRONZE_TABLES[domain]
    retrieved_at = datetime.now(UTC).isoformat()
    rows = [observation_to_row(obs, domain, retrieved_at, pipeline_run_id) for obs in observations]
    with engine.begin() as conn:
        result = conn.execute(insert(table).prefix_with("OR IGNORE"), rows)
        inserted = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else len(rows)
    return inserted


def load_bronze(engine: Engine, domain: str, observations: list[DataObservation], pipeline_run_id: str) -> int:
    """Truncate and reload ``bronze_<domain>`` in one call; returns rows
    inserted. Convenience wrapper for tests and small one-shot loads -- the
    live pipeline streams via :func:`truncate_bronze` + repeated
    :func:`append_bronze_batch` calls instead (see ``src/elt/extract.py``)."""
    truncate_bronze(engine, domain)
    inserted = append_bronze_batch(engine, domain, observations, pipeline_run_id)
    logger.info("Bronze[%s]: %d rows received, %d inserted", domain, len(observations), inserted)
    return inserted
