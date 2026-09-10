"""Configuration loaded from environment variables (never hard-coded)."""

from __future__ import annotations

import logging

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """FAOSTAT API and pipeline configuration.

    Populated from environment variables / a local .env file (see
    .env.example). Credentials are never hard-coded here.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    faostat_username: str | None = Field(default=None)
    faostat_password: str | None = Field(default=None)
    faostat_access_token: str | None = Field(default=None)

    faostat_base_url: str = Field(default="https://faostatservices.fao.org/api/v1")
    faostat_language: str = Field(default="en")
    faostat_timeout_seconds: float = Field(default=30.0)
    faostat_max_retries: int = Field(default=5)

    # FAOSTAT's API gateway validates a bearer token issued by an AWS
    # Cognito user pool. There is no discovery endpoint for these IDs (they
    # are auth configuration, not FAOSTAT dimension data), so they are
    # defaulted here from the pool/client that issued the token used during
    # development, and are overridable if FAO ever rotates them.
    faostat_cognito_region: str = Field(default="eu-west-1")
    faostat_cognito_client_id: str = Field(default="2csltsigao85ivhp6ojp1aic7o")


def configure_logging(level: int = logging.INFO) -> None:
    """Configure structured, timestamped logging for the pipeline."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
