import httpx
import pytest
import respx

from src.api.client import FAOSTATClient, FAOSTATClientError, TransientAPIError
from tests.conftest import make_jwt
from tests.fixtures.faostat_samples import (
    COGNITO_LOGIN_SUCCESS,
    FS_DIMENSIONS,
    GROUPS_AND_DOMAINS,
    QCL_AREAS,
    QCL_DATA_NO_ELEMENT_FILTER,
    QCL_DIMENSIONS,
    QCL_FLAGS,
    QCL_YEARS,
    TM_DIMENSIONS,
)

BASE = "https://faostatservices.fao.org/api/v1"
COGNITO_URL = "https://cognito-idp.eu-west-1.amazonaws.com/"


@pytest.fixture
def authed_settings(settings):
    """Settings with a pre-seeded, unexpired static access token so most
    tests don't need to exercise the Cognito login flow."""
    settings.faostat_access_token = make_jwt(exp_offset_seconds=3600)
    return settings


@pytest.fixture
def client(authed_settings):
    with FAOSTATClient(authed_settings) as c:
        yield c


@respx.mock
def test_authenticate_uses_static_token_when_valid(client):
    client.authenticate()
    assert client.is_authenticated is True


@respx.mock
def test_get_groups_and_domains(client):
    respx.get(f"{BASE}/en/groupsanddomains").mock(return_value=httpx.Response(200, json=GROUPS_AND_DOMAINS))
    result = client.get_groups_and_domains()
    assert len(result.data) == 2
    assert result.data[0]["domain_code"] == "QCL"


@respx.mock
def test_get_domain_metadata_is_cached(client):
    route = respx.get(f"{BASE}/en/definitions/domain/QCL").mock(
        return_value=httpx.Response(200, json=QCL_DIMENSIONS)
    )
    client.get_domain_metadata("QCL")
    client.get_domain_metadata("QCL")
    assert route.call_count == 1


@respx.mock
def test_get_areas_resolves_plain_area_dimension(client):
    respx.get(f"{BASE}/en/definitions/domain/QCL").mock(return_value=httpx.Response(200, json=QCL_DIMENSIONS))
    respx.get(f"{BASE}/en/definitions/domain/QCL/area").mock(return_value=httpx.Response(200, json=QCL_AREAS))
    result = client.get_areas("QCL")
    assert result.data[0]["Country"] == "Afghanistan"


@respx.mock
def test_get_areas_falls_back_to_reporterarea_for_tm(client):
    respx.get(f"{BASE}/en/definitions/domain/TM").mock(return_value=httpx.Response(200, json=TM_DIMENSIONS))
    route = respx.get(f"{BASE}/en/definitions/domain/TM/reporterarea").mock(
        return_value=httpx.Response(200, json=QCL_AREAS)
    )
    client.get_areas("TM")
    assert route.called


@respx.mock
def test_get_years_uses_year3_for_fs(client):
    respx.get(f"{BASE}/en/definitions/domain/FS").mock(return_value=httpx.Response(200, json=FS_DIMENSIONS))
    route = respx.get(f"{BASE}/en/definitions/domain/FS/year3").mock(
        return_value=httpx.Response(200, json=QCL_YEARS)
    )
    client.get_years("FS")
    assert route.called


@respx.mock
def test_get_elements_returns_empty_when_domain_has_none(client):
    respx.get(f"{BASE}/en/definitions/domain/FS").mock(return_value=httpx.Response(200, json=FS_DIMENSIONS))
    result = client.get_elements("FS")
    assert result.data == []


@respx.mock
def test_unresolvable_dimension_fails_fast_with_available_ids(client):
    respx.get(f"{BASE}/en/definitions/domain/QCL").mock(return_value=httpx.Response(200, json=QCL_DIMENSIONS))
    with pytest.raises(FAOSTATClientError, match="available dimension codes"):
        client._resolve_dimension_code("QCL", ("reporterarea",))


@respx.mock
def test_get_flags(client):
    respx.get(f"{BASE}/en/definitions/domain/QCL/flag").mock(return_value=httpx.Response(200, json=QCL_FLAGS))
    result = client.get_flags("QCL")
    assert {row["Flag"] for row in result.data} == {"A", "E", "M"}


@respx.mock
def test_get_data_validates_rows(client):
    respx.get(f"{BASE}/en/data/QCL").mock(return_value=httpx.Response(200, json=QCL_DATA_NO_ELEMENT_FILTER))
    result = client.get_data("QCL", area="2", item="15", year="2022")
    assert len(result.rows) == 3
    assert result.rows[0].area == "Afghanistan"


@respx.mock
def test_get_data_qcl_element_filter_workaround(client):
    route = respx.get(f"{BASE}/en/data/QCL").mock(return_value=httpx.Response(200, json=QCL_DATA_NO_ELEMENT_FILTER))
    result = client.get_data("QCL", area="2", item="15", year="2022", element="5510")

    assert len(result.rows) == 1
    assert result.rows[0].element == "Production"
    sent_params = route.calls.last.request.url.params
    assert "element" not in sent_params


@respx.mock
def test_get_data_element_filter_workaround_applies_to_non_qcl_domains(client):
    """Confirmed live: the element-filter bug also affects TM (and FBS) --
    not just QCL -- so the workaround must not be domain-restricted."""
    tm_data = {
        "metadata": {"processing_time": 1, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
        "data": [
            {
                "Domain Code": "TM", "Domain": "Detailed trade matrix",
                "Reporter Country Code": "231", "Reporter Countries": "United States of America",
                "Partner Country Code": "110", "Partner Countries": "Japan",
                "Element Code": "5910", "Element": "Export quantity",
                "Item Code": "15", "Item": "Wheat",
                "Year Code": "2020", "Year": "2020",
                "Unit": "t", "Value": "2629651.03", "Flag": "A", "Flag Description": "Official value",
            },
            {
                "Domain Code": "TM", "Domain": "Detailed trade matrix",
                "Reporter Country Code": "231", "Reporter Countries": "United States of America",
                "Partner Country Code": "110", "Partner Countries": "Japan",
                "Element Code": "5922", "Element": "Export value",
                "Item Code": "15", "Item": "Wheat",
                "Year Code": "2020", "Year": "2020",
                "Unit": "1000 USD", "Value": "635652", "Flag": "A", "Flag Description": "Official value",
            },
        ],
    }
    route = respx.get(f"{BASE}/en/data/TM").mock(return_value=httpx.Response(200, json=tm_data))
    result = client.get_data(
        "TM", reporterarea="231", partnerarea="110", item="15", year="2020", element="5910"
    )
    assert len(result.rows) == 1
    assert result.rows[0].element == "Export quantity"
    sent_params = route.calls.last.request.url.params
    assert "element" not in sent_params


@respx.mock
def test_get_data_chunked_batches_area_codes(client):
    route = respx.get(f"{BASE}/en/data/QCL").mock(return_value=httpx.Response(200, json=QCL_DATA_NO_ELEMENT_FILTER))
    client.get_data_chunked("QCL", area_codes=list(range(1, 5)), chunk_size=2, item="15", year="2022")
    assert route.call_count == 2


@respx.mock
def test_retries_on_5xx_then_succeeds(client):
    route = respx.get(f"{BASE}/en/data/QCL")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=QCL_DATA_NO_ELEMENT_FILTER),
    ]
    result = client.get_data("QCL", area="2", item="15", year="2022")
    assert len(result.rows) == 3
    assert route.call_count == 2


@respx.mock
def test_exhausting_retries_raises_transient_error(client):
    respx.get(f"{BASE}/en/data/QCL").mock(return_value=httpx.Response(503))
    with pytest.raises(TransientAPIError):
        client.get_data("QCL", area="2", item="15", year="2022")


@respx.mock
def test_terminal_4xx_does_not_retry(client):
    route = respx.get(f"{BASE}/en/data/QCL").mock(return_value=httpx.Response(404, text="not found"))
    with pytest.raises(FAOSTATClientError):
        client.get_data("QCL", area="2", item="15", year="2022")
    assert route.call_count == 1


@respx.mock
def test_401_forces_relogin_and_retries(authed_settings):
    respx.post(COGNITO_URL).mock(return_value=httpx.Response(200, json=COGNITO_LOGIN_SUCCESS))
    route = respx.get(f"{BASE}/en/data/QCL")
    route.side_effect = [
        httpx.Response(401, text="unauthorized"),
        httpx.Response(200, json=QCL_DATA_NO_ELEMENT_FILTER),
    ]
    with FAOSTATClient(authed_settings) as client:
        result = client.get_data("QCL", area="2", item="15", year="2022")
    assert len(result.rows) == 3
    assert route.call_count == 2
