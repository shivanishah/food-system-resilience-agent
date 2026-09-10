from __future__ import annotations

import base64
import json
import time

import pytest

from src.config import Settings


def make_jwt(exp_offset_seconds: float, client_id: str = "test-client-id") -> str:
    """Build an unsigned JWT-shaped token with an exp claim `exp_offset_seconds`
    from now (negative = already expired). Good enough for
    decode_jwt_exp, which never verifies the signature."""
    header = {"alg": "RS256", "kid": "test-kid"}
    payload = {
        "exp": time.time() + exp_offset_seconds,
        "iat": time.time(),
        "client_id": client_id,
        "token_use": "access",
    }

    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64(header)}.{b64(payload)}.fake-signature"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        faostat_username="test-user",
        faostat_password="test-pass",
        faostat_access_token=None,
        faostat_base_url="https://faostatservices.fao.org/api/v1",
        faostat_cognito_region="eu-west-1",
        faostat_cognito_client_id="test-client-id",
        faostat_max_retries=3,
        faostat_timeout_seconds=5.0,
        _env_file=None,
    )
