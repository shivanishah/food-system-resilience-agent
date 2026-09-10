"""Pydantic models validating FAOSTAT API responses.

FAOSTAT's JSON keys are human-readable column headers with spaces (e.g.
``"Area Code"``), which is what the API actually returns rather than a
convention chosen here. Models use aliases to accept that shape directly
while giving downstream code normal Python attribute names.

Invalid rows are never silently dropped: :func:`validate_data_rows` returns
both the rows that validated and the ones that did not, each paired with
the error that rejected it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Tokens FAOSTAT (or an intermediate CSV/JSON round-trip) may use for a
# missing value. "0" is deliberately excluded: NULL and 0 are distinct.
_MISSING_VALUE_TOKENS = {"", "NaN", "nan", "NA", "N/A", "null", "None"}


class GroupDomain(BaseModel):
    """One row of ``GET /{lang}/groupsanddomains``."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    group_code: str = Field(alias="group_code")
    group_name: str = Field(alias="group_name")
    domain_code: str = Field(alias="domain_code")
    domain_name: str = Field(alias="domain_name")


class DomainDimension(BaseModel):
    """One row of ``GET /{lang}/definitions/domain/{domain}`` -- a
    dimension a domain exposes, e.g. {"code": "area", "id": "area"}."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    code: str
    label: str
    id: str
    subdimension_id: str | None = None


class FlagInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    flag: str = Field(alias="Flag")
    description: str = Field(alias="Flags")


class DataObservation(BaseModel):
    """One row of ``GET /{lang}/data/{domain}``.

    Single-area domains (QCL, FBS, FS, CAHD, CP, GT, RL) return an
    ``Area``/``Area Code`` pair. The bilateral trade domains (TM, RFM)
    instead return ``Reporter Countries``/``Partner Countries`` (confirmed
    live) and have no ``Area`` field at all, so ``area``/``area_code`` are
    optional and the reporter/partner fields are populated instead; every
    other field is common to both shapes and stays required.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    domain_code: str = Field(alias="Domain Code")
    domain: str = Field(alias="Domain")
    area_code: str | None = Field(default=None, alias="Area Code")
    area: str | None = Field(default=None, alias="Area")
    reporter_area_code: str | None = Field(default=None, alias="Reporter Country Code")
    reporter_area: str | None = Field(default=None, alias="Reporter Countries")
    partner_area_code: str | None = Field(default=None, alias="Partner Country Code")
    partner_area: str | None = Field(default=None, alias="Partner Countries")
    element_code: str = Field(alias="Element Code")
    element: str = Field(alias="Element")
    item_code: str = Field(alias="Item Code")
    item: str = Field(alias="Item")
    year_code: str = Field(alias="Year Code")
    year: str = Field(alias="Year")
    unit: str | None = Field(default=None, alias="Unit")
    value: float | None = Field(default=None, alias="Value")
    flag: str | None = Field(default=None, alias="Flag")
    flag_description: str | None = Field(default=None, alias="Flag Description")
    note: str | None = Field(default=None, alias="Note")

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_missing_value(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip() in _MISSING_VALUE_TOKENS:
            return None
        return v

    @field_validator("unit", "flag", "flag_description", "note", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and v == "":
            return None
        return v


@dataclass
class ValidationResult:
    """Rows that validated, plus every row that did not, paired with why."""

    valid: list[DataObservation] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def n_valid(self) -> int:
        return len(self.valid)

    @property
    def n_errors(self) -> int:
        return len(self.errors)


def validate_data_rows(rows: list[dict[str, Any]]) -> ValidationResult:
    """Validate raw data rows, reporting (not dropping) failures."""
    result = ValidationResult()
    for row in rows:
        try:
            result.valid.append(DataObservation.model_validate(row))
        except Exception as exc:  # noqa: BLE001 - report every failure mode
            result.errors.append({"row": row, "error": str(exc)})
    return result
