"""Typed loading of the Phase 2 scope configuration.

Translates ``config/variables.yaml`` (per-domain item/element scope, derived
from DATA.md) and ``config/harmonisation.yaml`` (Silver cleaning rules) into
Pydantic models, so the rest of the pipeline never parses YAML directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from src.elt.qcl_items import DEFAULT_QCL_CROP_ITEMS_PATH, load_qcl_crop_items

DEFAULT_VARIABLES_PATH = Path("config/variables.yaml")
DEFAULT_HARMONISATION_PATH = Path("config/harmonisation.yaml")


class DomainSpec(BaseModel):
    """Ingestion scope for one FAOSTAT domain."""

    code: str
    priority: Literal["core", "supporting"]
    silver_table: str
    bilateral: bool
    item_scope: str
    elements: list[str] | Literal["all"] | None
    # Where the domain's server-side `item=` filter is confirmed unreliable,
    # the item scope is applied client-side instead (see PHASE2.md section 15).
    item_filter: Literal["server", "client"] = "server"
    extra_dimensions: list[str] = Field(default_factory=list)


class PipelineConfig(BaseModel):
    """All per-domain scope, loaded from ``config/variables.yaml``."""

    start_year: int
    end_year: int
    min_years_for_history: int
    domains: dict[str, DomainSpec]
    item_sets: dict[str, list[dict[str, str]]]

    @property
    def requested_years(self) -> list[int]:
        return list(range(self.start_year, self.end_year + 1))

    def resolve_item_codes(self, domain_code: str) -> list[str] | None:
        """Item codes to request for ``domain_code``, or ``None`` for "all"."""
        spec = self.domains[domain_code]
        if spec.item_scope == "all":
            return None
        if spec.item_scope == "crops_only":
            return [row["item_code"] for row in load_qcl_crop_items(DEFAULT_QCL_CROP_ITEMS_PATH)]
        if spec.item_scope.startswith("item_set:"):
            set_name = spec.item_scope.removeprefix("item_set:")
            return [row["item_code"] for row in self.item_sets[set_name]]
        raise ValueError(f"Unrecognised item_scope {spec.item_scope!r} for domain {domain_code}")


def load_pipeline_config(path: Path = DEFAULT_VARIABLES_PATH) -> PipelineConfig:
    with path.open() as f:
        doc = yaml.safe_load(f)
    period = doc["analysis_period"]
    return PipelineConfig(
        start_year=period["start_year"],
        end_year=period["end_year"],
        min_years_for_history=doc["min_years_for_history"],
        domains={code: DomainSpec(code=code, **spec) for code, spec in doc["domains"].items()},
        item_sets=doc["item_sets"],
    )


class UnitConversionRule(BaseModel):
    to_unit: str
    factor: float
    elements: list[str]


class HarmonisationConfig(BaseModel):
    """Silver cleaning rules, loaded from ``config/harmonisation.yaml``."""

    aggregate_numeric_floor: int
    aggregate_name_denylist: list[str]
    aggregate_allowlist_codes: list[str]
    unit_conversions: dict[str, dict[str, UnitConversionRule]]
    estimate_flags: list[str]
    duplicate_natural_keys: dict[str, list[str]]
    non_negative_elements: dict[str, list[str]]
    commodity_mapping: dict[str, dict[str, str]]

    def is_aggregate(self, area_code: str | None, area_name: str | None) -> bool:
        """True if ``area_code``/``area_name`` looks like a FAOSTAT aggregate
        (World, a continent, a region, an income/trade grouping) rather than
        a real country."""
        if area_code in self.aggregate_allowlist_codes:
            return False
        if area_code is not None:
            try:
                if int(area_code) >= self.aggregate_numeric_floor:
                    return True
            except ValueError:
                pass
        if area_name:
            lowered = area_name.lower()
            if any(term.lower() in lowered for term in self.aggregate_name_denylist):
                return True
        return False


def load_harmonisation_config(path: Path = DEFAULT_HARMONISATION_PATH) -> HarmonisationConfig:
    with path.open() as f:
        doc = yaml.safe_load(f)
    unit_conversions = {
        domain: {unit: UnitConversionRule(**rule) for unit, rule in rules.items()}
        for domain, rules in doc["unit_conversions"].items()
    }
    return HarmonisationConfig(
        aggregate_numeric_floor=doc["aggregate_areas"]["numeric_code_floor"],
        aggregate_name_denylist=doc["aggregate_areas"]["name_denylist"],
        aggregate_allowlist_codes=doc["aggregate_areas"]["allowlist_codes"],
        unit_conversions=unit_conversions,
        estimate_flags=doc["estimate_flags"],
        duplicate_natural_keys=doc["duplicates"]["natural_keys"],
        non_negative_elements=doc["non_negative_elements"],
        commodity_mapping=doc["commodity_mapping"]["canonical"],
    )
