"""Authentication against the FAOSTAT API's AWS Cognito-issued bearer tokens.

The FAOSTAT API gateway (`FAOSTAT_BASE_URL`) requires an
``Authorization: Bearer <token>`` header on every request, including
discovery endpoints. There is no login endpoint on the FAOSTAT gateway
itself: the token is issued directly by an AWS Cognito user pool via the
``USER_PASSWORD_AUTH`` flow, confirmed live by decoding the ``iss`` /
``client_id`` claims of a token minted through the FAO developer portal.

Credentials are only ever read from environment variables (via
:class:`src.config.Settings`) and are never logged.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass

import httpx

from src.config import Settings

logger = logging.getLogger(__name__)

COGNITO_TARGET_INITIATE_AUTH = "AWSCognitoIdentityProviderService.InitiateAuth"
# Tokens observed to be valid for 3600s; refresh a little early to avoid
# racing a request against expiry.
TOKEN_REFRESH_MARGIN_SECONDS = 60


class AuthenticationError(RuntimeError):
    """Raised when FAOSTAT/Cognito authentication fails."""


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: float


def _cognito_endpoint(region: str) -> str:
    return f"https://cognito-idp.{region}.amazonaws.com/"


def decode_jwt_exp(token: str) -> float | None:
    """Return the ``exp`` claim (epoch seconds) of a JWT, or ``None``.

    Only the payload segment is base64-decoded to read the expiry; the
    signature is not (and cannot be, without Cognito's public keys)
    verified client-side. This is used purely to decide whether a token
    needs refreshing, not to authorize anything.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = payload.get("exp")
    return float(exp) if exp is not None else None


class FAOSTATAuthenticator:
    """Obtains and refreshes bearer tokens for the FAOSTAT API."""

    def __init__(self, settings: Settings, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = http_client or httpx.Client(timeout=settings.faostat_timeout_seconds)
        self._owns_client = http_client is None
        self._token: TokenBundle | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @property
    def token(self) -> TokenBundle | None:
        return self._token

    def is_token_valid(self) -> bool:
        if self._token is None:
            return False
        return time.time() < (self._token.expires_at - TOKEN_REFRESH_MARGIN_SECONDS)

    def get_valid_access_token(self) -> str:
        """Return a currently-valid access token, refreshing/logging in as needed."""
        if self.is_token_valid():
            return self._token.access_token  # type: ignore[union-attr]

        if self._token and self._token.refresh_token:
            try:
                self._refresh()
                return self._token.access_token
            except AuthenticationError:
                logger.warning("Token refresh failed; falling back to full login")

        self._login()
        return self._token.access_token  # type: ignore[union-attr]

    def seed_from_static_token(self, access_token: str) -> bool:
        """Adopt a pre-issued ``FAOSTAT_ACCESS_TOKEN`` if it is not expired.

        Returns True if the token was usable, False if it was expired (or
        malformed) and a fresh login is required instead.
        """
        exp = decode_jwt_exp(access_token)
        if exp is None or time.time() >= (exp - TOKEN_REFRESH_MARGIN_SECONDS):
            logger.info("Provided FAOSTAT_ACCESS_TOKEN is missing/expired; will log in instead")
            return False
        self._token = TokenBundle(access_token=access_token, refresh_token=None, expires_at=exp)
        logger.info("Using provided FAOSTAT_ACCESS_TOKEN (valid for %.0fs)", exp - time.time())
        return True

    def _login(self) -> None:
        username = self._settings.faostat_username
        password = self._settings.faostat_password
        if not username or not password:
            raise AuthenticationError(
                "No valid FAOSTAT_ACCESS_TOKEN and FAOSTAT_USERNAME/FAOSTAT_PASSWORD "
                "are not both set; cannot authenticate."
            )
        logger.info("Authenticating to FAOSTAT via Cognito USER_PASSWORD_AUTH")
        result = self._cognito_initiate_auth(
            {
                "AuthFlow": "USER_PASSWORD_AUTH",
                "ClientId": self._settings.faostat_cognito_client_id,
                "AuthParameters": {"USERNAME": username, "PASSWORD": password},
            }
        )
        self._store_auth_result(result)

    def _refresh(self) -> None:
        logger.info("Refreshing FAOSTAT access token via Cognito REFRESH_TOKEN_AUTH")
        result = self._cognito_initiate_auth(
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": self._settings.faostat_cognito_client_id,
                "AuthParameters": {"REFRESH_TOKEN": self._token.refresh_token},  # type: ignore[union-attr]
            }
        )
        # A refresh response has no new RefreshToken; keep the existing one.
        refresh_token = self._token.refresh_token if self._token else None  # type: ignore[union-attr]
        self._store_auth_result(result, fallback_refresh_token=refresh_token)

    def _cognito_initiate_auth(self, body: dict) -> dict:
        url = _cognito_endpoint(self._settings.faostat_cognito_region)
        try:
            response = self._client.post(
                url,
                content=json.dumps(body),
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": COGNITO_TARGET_INITIATE_AUTH,
                },
            )
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"Cognito auth request failed: {exc}") from exc

        if response.status_code != 200:
            # Never echo credentials; Cognito's error body does not contain
            # them, only an error type/message (e.g. NotAuthorizedException).
            raise AuthenticationError(
                f"Cognito authentication failed ({response.status_code}): {response.text[:300]}"
            )
        return response.json()

    def _store_auth_result(self, payload: dict, fallback_refresh_token: str | None = None) -> None:
        result = payload.get("AuthenticationResult", {})
        access_token = result.get("AccessToken")
        if not access_token:
            raise AuthenticationError("Cognito response did not include an AccessToken")
        expires_in = result.get("ExpiresIn", 3600)
        self._token = TokenBundle(
            access_token=access_token,
            refresh_token=result.get("RefreshToken", fallback_refresh_token),
            expires_at=time.time() + float(expires_in),
        )
        logger.info("Obtained FAOSTAT access token (expires in %ss)", expires_in)
