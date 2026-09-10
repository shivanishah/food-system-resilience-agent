"""Reusable FAOSTAT API client.

Wraps discovery (groups/domains, areas, items, elements, years, flags) and
data retrieval behind one authenticated, retrying, logged HTTP client, so
that notebooks and pipeline code never issue raw HTTP calls themselves.

Two behaviours were confirmed only by exercising the live API and are
handled explicitly rather than assumed:

* FAOSTAT dimension codes are **not** uniform across domains -- most
  domains expose a plain ``area`` dimension, but the bilateral trade
  domains (``TM``, ``RFM``) split it into ``reporterarea``/``partnerarea``,
  and ``FS`` calls its year dimension ``year3``. :meth:`get_domain_metadata`
  discovers the real codes per domain instead of assuming ``area``/``year``
  everywhere, and :meth:`get_areas`/:meth:`get_years` fail fast (naming the
  actual available dimension codes) rather than guessing.
* An ``element`` filter on the data endpoint reproducibly returns zero rows
  even when matching data exists without it -- a confirmed server-side bug
  (verified live on QCL, TM and FBS), not a genuine empty result.
  :meth:`get_data` works around it, for every domain, by re-fetching without
  the filter and applying it client-side, logging that it did so.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.api import endpoints
from src.api.auth import AuthenticationError, FAOSTATAuthenticator
from src.api.pagination import chunk_codes
from src.config import Settings
from src.validation.schemas import DataObservation, ValidationResult, validate_data_rows

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_AUTH_STATUS_CODES = {401, 403}

# Comma-separated code lists longer than this are split into multiple
# requests (see src.api.pagination) to stay well clear of URL-length limits;
# no server-side page size was found on any endpoint.
DEFAULT_CODE_CHUNK_SIZE = 200


class FAOSTATAPIError(Exception):
    """Base class for FAOSTAT API errors."""


class TransientAPIError(FAOSTATAPIError):
    """A retryable failure: network error, timeout, 429, or 5xx."""


class FAOSTATClientError(FAOSTATAPIError):
    """A non-retryable client error (4xx other than auth)."""


@dataclass
class APIResult:
    """A validated discovery-endpoint response plus its provenance."""

    data: list[dict[str, Any]]
    metadata: dict[str, Any]
    request_id: str


@dataclass
class DataResult:
    """A validated data-endpoint response plus its provenance."""

    rows: list[DataObservation]
    validation: ValidationResult
    metadata: dict[str, Any]
    request_id: str


@dataclass
class _RequestRecord:
    request_id: str
    method: str
    path: str
    status_code: int | None
    attempt: int
    elapsed_seconds: float
    timestamp: float


class FAOSTATClient:
    """Reusable, authenticated client for the FAOSTAT REST API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.pipeline_run_id = str(uuid.uuid4())
        self._http = httpx.Client(
            base_url=self.settings.faostat_base_url,
            timeout=self.settings.faostat_timeout_seconds,
        )
        self._auth = FAOSTATAuthenticator(self.settings, http_client=self._http)
        self._domain_dimension_cache: dict[str, list[dict[str, Any]]] = {}
        self.request_log: list[_RequestRecord] = []

    def __enter__(self) -> "FAOSTATClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @property
    def is_authenticated(self) -> bool:
        return self._auth.is_token_valid()

    def authenticate(self, force: bool = False) -> None:
        """Ensure a valid bearer token is available.

        Tries ``FAOSTAT_ACCESS_TOKEN`` first (if set and unexpired), then
        falls back to a Cognito login with ``FAOSTAT_USERNAME`` /
        ``FAOSTAT_PASSWORD``.
        """
        if force:
            self._auth._token = None  # noqa: SLF001 - intentional forced reset
        if self._auth.is_token_valid():
            return
        static_token = self.settings.faostat_access_token
        if static_token and self._auth.seed_from_static_token(static_token):
            return
        self._auth.get_valid_access_token()

    # -- discovery -----------------------------------------------------

    def get_groups_and_domains(self) -> APIResult:
        return self._get_discovery(endpoints.groups_and_domains(self.settings.faostat_language))

    def get_domain_metadata(self, domain: str) -> list[dict[str, Any]]:
        """Return the dimensions ``domain`` exposes, cached per domain."""
        if domain not in self._domain_dimension_cache:
            result = self._get_discovery(
                endpoints.domain_dimensions(self.settings.faostat_language, domain)
            )
            self._domain_dimension_cache[domain] = result.data
        return self._domain_dimension_cache[domain]

    def _resolve_dimension_code(self, domain: str, candidates: tuple[str, ...]) -> str:
        """Pick the first of ``candidates`` that is an actual dimension
        *code* for ``domain``.

        Matched on ``code`` (the path segment, e.g. ``"area"``), not
        ``id``: several dimensions share an ``id`` with a "group" variant
        (e.g. QCL's ``area`` and ``areagroup`` both have ``id == "area"``),
        so matching on ``id`` alone can silently resolve to the group
        dimension instead of the one callers actually want.
        """
        dims = self.get_domain_metadata(domain)
        codes = {d.get("code") for d in dims}
        for candidate in candidates:
            if candidate in codes:
                return candidate
        available = sorted(c for c in codes if c)
        raise FAOSTATClientError(
            f"Domain {domain!r} has none of {candidates} as a dimension code; "
            f"available dimension codes: {available}"
        )

    def get_areas(self, domain: str) -> APIResult:
        """Areas for ``domain``. For bilateral domains (TM/RFM) this
        returns the *reporter* areas; use
        ``get_dimension(domain, "partnerarea")`` for partners."""
        code = self._resolve_dimension_code(domain, ("area", "reporterarea"))
        return self.get_dimension(domain, code)

    def get_items(self, domain: str) -> APIResult:
        code = self._resolve_dimension_code(domain, ("item",))
        return self.get_dimension(domain, code)

    def get_elements(self, domain: str) -> APIResult:
        """Elements for ``domain``, or an empty result if the domain has
        none (e.g. FS/CAHD, whose "items" are indicators, not element x
        item pairs)."""
        dims = self.get_domain_metadata(domain)
        codes = {d.get("code") for d in dims}
        if "element" not in codes:
            logger.info("Domain %s has no element dimension", domain)
            return APIResult(data=[], metadata={}, request_id=str(uuid.uuid4()))
        return self.get_dimension(domain, "element")

    def get_years(self, domain: str) -> APIResult:
        code = self._resolve_dimension_code(domain, ("year", "year3"))
        return self.get_dimension(domain, code)

    def get_flags(self, domain: str) -> APIResult:
        return self._get_discovery(endpoints.domain_flags(self.settings.faostat_language, domain))

    def get_dimension(self, domain: str, dimension_code: str) -> APIResult:
        """Low-level fetch of one dimension's values for ``domain``."""
        return self._get_discovery(
            endpoints.domain_dimension_values(self.settings.faostat_language, domain, dimension_code)
        )

    # -- data ------------------------------------------------------------

    def get_data(
        self,
        domain: str,
        area: str | int | Iterable[str | int] | None = None,
        item: str | int | Iterable[str | int] | None = None,
        element: str | int | Iterable[str | int] | None = None,
        year: str | int | Iterable[str | int] | None = None,
        **extra_filters: str | int | Iterable[str | int] | None,
    ) -> DataResult:
        """Retrieve and validate observations for ``domain``.

        ``area``/``item``/``element``/``year`` cover the common single-area
        domains (QCL, FBS, FS, CAHD, CP, GT, RL). Bilateral domains (TM,
        RFM) take ``reporterarea=``/``partnerarea=`` via ``extra_filters``
        instead of ``area``.
        """
        if element is not None:
            # Confirmed live on QCL, TM and FBS (and assumed universal): an
            # `element` filter on the data endpoint reproducibly returns zero
            # rows even when matching data exists without it -- a
            # server-side bug, not a genuine empty result. Re-fetch
            # unfiltered and apply the element filter client-side instead.
            logger.warning(
                "Working around confirmed FAOSTAT server bug: an 'element' "
                "filter on %s returns zero rows even when matching data "
                "exists. Re-fetching without it and filtering client-side.",
                domain,
            )
            requested_elements = {
                str(e) for e in (element if not isinstance(element, (str, int)) else [element])
            }
            unfiltered = self._fetch_data(domain, area=area, item=item, year=year, **extra_filters)
            rows = [r for r in unfiltered.data if str(r.get("Element Code")) in requested_elements]
            return self._to_data_result(rows, unfiltered.metadata, unfiltered.request_id)

        result = self._fetch_data(domain, area=area, item=item, element=element, year=year, **extra_filters)
        return self._to_data_result(result.data, result.metadata, result.request_id)

    def _fetch_data(
        self,
        domain: str,
        **filters: str | int | Iterable[str | int] | None,
    ) -> APIResult:
        params = endpoints.build_data_params(**filters)
        return self._get_discovery(endpoints.data(self.settings.faostat_language, domain), params=params)

    @staticmethod
    def _to_data_result(rows: list[dict[str, Any]], metadata: dict[str, Any], request_id: str) -> DataResult:
        validation = validate_data_rows(rows)
        if validation.n_errors:
            logger.warning("%d/%d rows failed validation", validation.n_errors, len(rows))
        return DataResult(rows=validation.valid, validation=validation, metadata=metadata, request_id=request_id)

    def get_data_chunked(
        self,
        domain: str,
        area_codes: list[str | int],
        chunk_size: int = DEFAULT_CODE_CHUNK_SIZE,
        **other_filters: str | int | Iterable[str | int] | None,
    ) -> DataResult:
        """Fetch ``area_codes`` in batches and concatenate the results.

        Use for area lists long enough to risk hitting URL-length limits;
        the API itself has no server-side pagination to page through.
        """
        all_rows: list[dict[str, Any]] = []
        last_metadata: dict[str, Any] = {}
        for batch in chunk_codes([str(c) for c in area_codes], chunk_size=chunk_size):
            result = self._fetch_data(domain, area=batch, **other_filters)
            all_rows.extend(result.data)
            last_metadata = result.metadata
        return self._to_data_result(all_rows, last_metadata, str(uuid.uuid4()))

    # -- HTTP plumbing -----------------------------------------------------

    def _get_discovery(self, path: str, params: dict[str, str] | None = None) -> APIResult:
        request_id = str(uuid.uuid4())
        payload = self._request("GET", path, params=params, request_id=request_id)
        return APIResult(
            data=payload.get("data", []),
            metadata=payload.get("metadata", {}),
            request_id=request_id,
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None,
        request_id: str,
    ) -> dict[str, Any]:
        attempt_counter = {"n": 0}

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.settings.faostat_max_retries),
            wait=wait_exponential_jitter(initial=1, max=20),
            retry=retry_if_exception_type(TransientAPIError),
        )
        def _call() -> dict[str, Any]:
            attempt_counter["n"] += 1
            return self._request_once(method, path, params, request_id, attempt_counter["n"])

        return _call()

    def _request_once(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None,
        request_id: str,
        attempt: int,
    ) -> dict[str, Any]:
        self.authenticate()
        start = time.monotonic()
        try:
            response = self._http.request(
                method,
                path,
                params=params,
                headers={"Authorization": f"Bearer {self._auth.get_valid_access_token()}"},
            )
        except httpx.TimeoutException as exc:
            self._log_request(request_id, method, path, None, attempt, start)
            raise TransientAPIError(f"Timeout calling {path}: {exc}") from exc
        except httpx.HTTPError as exc:
            self._log_request(request_id, method, path, None, attempt, start)
            raise TransientAPIError(f"Network error calling {path}: {exc}") from exc

        self._log_request(request_id, method, path, response.status_code, attempt, start)

        if response.status_code in _AUTH_STATUS_CODES:
            logger.warning("Auth error %s on %s; forcing re-authentication", response.status_code, path)
            self.authenticate(force=True)
            raise TransientAPIError(f"Authentication error {response.status_code} on {path}")

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", 1))
            logger.warning("Rate limited on %s; honouring Retry-After=%ss", path, retry_after)
            time.sleep(retry_after)
            raise TransientAPIError(f"Rate limited on {path}")

        if response.status_code in _RETRYABLE_STATUS_CODES:
            raise TransientAPIError(f"Server error {response.status_code} on {path}")

        if response.status_code >= 400:
            raise FAOSTATClientError(
                f"FAOSTAT API returned {response.status_code} for {path}: {response.text[:300]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise FAOSTATClientError(f"Non-JSON response from {path}: {exc}") from exc

    def _log_request(
        self,
        request_id: str,
        method: str,
        path: str,
        status_code: int | None,
        attempt: int,
        start: float,
    ) -> None:
        elapsed = time.monotonic() - start
        record = _RequestRecord(
            request_id=request_id,
            method=method,
            path=path,
            status_code=status_code,
            attempt=attempt,
            elapsed_seconds=elapsed,
            timestamp=time.time(),
        )
        self.request_log.append(record)
        logger.info(
            "run=%s request=%s %s %s -> %s (attempt %d, %.2fs)",
            self.pipeline_run_id,
            request_id,
            method,
            path,
            status_code,
            attempt,
            elapsed,
        )
