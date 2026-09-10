import httpx
import pytest
import respx

from src.api.auth import AuthenticationError, FAOSTATAuthenticator, decode_jwt_exp
from tests.conftest import make_jwt
from tests.fixtures.faostat_samples import COGNITO_LOGIN_FAILURE, COGNITO_LOGIN_SUCCESS

COGNITO_URL = "https://cognito-idp.eu-west-1.amazonaws.com/"


def test_decode_jwt_exp_reads_expiry():
    token = make_jwt(exp_offset_seconds=3600)
    exp = decode_jwt_exp(token)
    assert exp is not None
    assert exp > 0


def test_decode_jwt_exp_returns_none_for_malformed_token():
    assert decode_jwt_exp("not-a-jwt") is None
    assert decode_jwt_exp("") is None


def test_seed_from_static_token_accepts_unexpired(settings):
    auth = FAOSTATAuthenticator(settings)
    token = make_jwt(exp_offset_seconds=3600)
    assert auth.seed_from_static_token(token) is True
    assert auth.is_token_valid() is True


def test_seed_from_static_token_rejects_expired(settings):
    auth = FAOSTATAuthenticator(settings)
    token = make_jwt(exp_offset_seconds=-10)
    assert auth.seed_from_static_token(token) is False
    assert auth.is_token_valid() is False


@respx.mock
def test_login_success_populates_token(settings):
    respx.post(COGNITO_URL).mock(return_value=httpx.Response(200, json=COGNITO_LOGIN_SUCCESS))
    auth = FAOSTATAuthenticator(settings)
    token = auth.get_valid_access_token()
    assert token == "fake-access-token"
    assert auth.token.refresh_token == "fake-refresh-token"
    assert auth.is_token_valid() is True


@respx.mock
def test_login_failure_raises_and_does_not_leak_password(settings):
    respx.post(COGNITO_URL).mock(return_value=httpx.Response(400, json=COGNITO_LOGIN_FAILURE))
    auth = FAOSTATAuthenticator(settings)
    with pytest.raises(AuthenticationError) as excinfo:
        auth.get_valid_access_token()
    assert settings.faostat_password not in str(excinfo.value)


def test_missing_credentials_raises_before_any_request(settings):
    settings.faostat_username = None
    settings.faostat_password = None
    auth = FAOSTATAuthenticator(settings)
    with pytest.raises(AuthenticationError):
        auth.get_valid_access_token()


@respx.mock
def test_cached_valid_token_is_not_refetched(settings):
    route = respx.post(COGNITO_URL).mock(return_value=httpx.Response(200, json=COGNITO_LOGIN_SUCCESS))
    auth = FAOSTATAuthenticator(settings)
    auth.get_valid_access_token()
    auth.get_valid_access_token()
    assert route.call_count == 1
