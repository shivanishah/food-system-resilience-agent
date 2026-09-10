"""Pure FAOSTAT API path/query builders (no HTTP calls, no dimension IDs).

Endpoint shapes were confirmed live against
``https://faostatservices.fao.org/api/v1`` during Phase 1 exploration:

* ``GET /{lang}/groupsanddomains`` -- group/domain catalogue.
* ``GET /{lang}/domains`` -- flat domain catalogue.
* ``GET /{lang}/definitions/domain/{domain}`` -- the dimensions a domain
  actually exposes (names vary by domain: e.g. ``area`` for QCL/FBS/FS/etc.
  vs. ``reporterarea``/``partnerarea`` for the bilateral TM/RFM matrices,
  and ``year`` vs. FS's ``year3``).
* ``GET /{lang}/definitions/domain/{domain}/{dimension_code}`` -- the
  values for one of those dimensions (areas, items, elements, years, ...).
* ``GET /{lang}/definitions/domain/{domain}/flag`` -- FAOSTAT observation
  flags for a domain.
* ``GET /{lang}/data/{domain}`` -- observations, filtered by dimension
  query parameters (comma-separated codes).
"""

from __future__ import annotations

from collections.abc import Iterable


def groups_and_domains(lang: str) -> str:
    return f"/{lang}/groupsanddomains"


def domains(lang: str) -> str:
    return f"/{lang}/domains"


def domain_dimensions(lang: str, domain: str) -> str:
    return f"/{lang}/definitions/domain/{domain}"


def domain_dimension_values(lang: str, domain: str, dimension_code: str) -> str:
    return f"/{lang}/definitions/domain/{domain}/{dimension_code}"


def domain_flags(lang: str, domain: str) -> str:
    return domain_dimension_values(lang, domain, "flag")


def data(lang: str, domain: str) -> str:
    return f"/{lang}/data/{domain}"


def _as_code_list(value: str | int | Iterable[str | int] | None) -> str | None:
    """Join one or many codes into FAOSTAT's comma-separated filter format."""
    if value is None:
        return None
    if isinstance(value, (str, int)):
        return str(value)
    codes = [str(v) for v in value]
    return ",".join(codes) if codes else None


def build_data_params(**filters: str | int | Iterable[str | int] | None) -> dict[str, str]:
    """Build query params for the data endpoint, dropping unset filters."""
    params: dict[str, str] = {}
    for key, value in filters.items():
        joined = _as_code_list(value)
        if joined is not None:
            params[key] = joined
    return params
