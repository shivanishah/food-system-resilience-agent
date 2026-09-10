"""Sample FAOSTAT API payloads, shaped after real live responses captured
during Phase 1 exploration (trimmed to a few rows each)."""

GROUPS_AND_DOMAINS = {
    "metadata": {"processing_time": 15, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
    "data": [
        {
            "group_code": "Q",
            "group_name": "Production",
            "domain_code": "QCL",
            "domain_name": "Crops and livestock products",
            "state_current": "final",
            "year_current": "2024",
        },
        {
            "group_code": "T",
            "group_name": "Trade",
            "domain_code": "TM",
            "domain_name": "Detailed trade matrix",
            "state_current": "final",
            "year_current": "2023",
        },
    ],
}

QCL_DIMENSIONS = {
    "metadata": {"processing_time": 9, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
    "data": [
        {"code": "area", "label": "Country/Region", "id": "area", "subdimension_id": "area"},
        {"code": "areagroup", "label": "Country Group", "id": "area", "subdimension_id": "area"},
        {"code": "element", "label": "Element", "id": "element", "subdimension_id": "element"},
        {"code": "item", "label": "Item", "id": "item", "subdimension_id": "item"},
        {"code": "year", "label": "Year", "id": "year", "subdimension_id": "year"},
    ],
}

TM_DIMENSIONS = {
    "metadata": {"processing_time": 11, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
    "data": [
        {"code": "reporterarea", "label": "Reporter Countries", "id": "reporterarea", "subdimension_id": "reporterarea"},
        {"code": "partnerarea", "label": "Partner Countries", "id": "partnerarea", "subdimension_id": "partnerarea"},
        {"code": "element", "label": "Element", "id": "element", "subdimension_id": "element"},
        {"code": "item", "label": "Item", "id": "item", "subdimension_id": "item"},
        {"code": "year", "label": "Year", "id": "year", "subdimension_id": "year"},
    ],
}

FS_DIMENSIONS = {
    "metadata": {"processing_time": 10, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
    "data": [
        {"code": "area", "label": "Country/Region", "id": "area", "subdimension_id": "area"},
        {"code": "item", "label": "Item", "id": "item", "subdimension_id": "item"},
        {"code": "year3", "label": "Year", "id": "year3", "subdimension_id": "year3"},
    ],
}

QCL_AREAS = {
    "metadata": {"processing_time": 16, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
    "data": [
        {"Country Code": "2", "Country": "Afghanistan", "M49 Code": "004", "ISO2 Code": "AF", "ISO3 Code": "AFG"},
        {"Country Code": "3", "Country": "Albania", "M49 Code": "008", "ISO2 Code": "AL", "ISO3 Code": "ALB"},
    ],
}

QCL_YEARS = {
    "metadata": {"processing_time": 18, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
    "data": [{"Year Code": str(y), "Year": str(y)} for y in range(2014, 2025)],
}

QCL_FLAGS = {
    "metadata": {"processing_time": 18, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
    "data": [
        {"Flag": "A", "Flags": "Official figure"},
        {"Flag": "E", "Flags": "Estimated value"},
        {"Flag": "M", "Flags": "Missing value; data cannot exist"},
    ],
}

QCL_DATA_NO_ELEMENT_FILTER = {
    "metadata": {"processing_time": 115, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
    "data": [
        {
            "Domain Code": "QCL", "Domain": "Crops and livestock products",
            "Area Code": "2", "Area": "Afghanistan",
            "Element Code": "5312", "Element": "Area harvested",
            "Item Code": "15", "Item": "Wheat",
            "Year Code": "2022", "Year": "2022",
            "Unit": "ha", "Value": "1859339", "Flag": "A",
            "Flag Description": "Official value", "Note": "",
        },
        {
            "Domain Code": "QCL", "Domain": "Crops and livestock products",
            "Area Code": "2", "Area": "Afghanistan",
            "Element Code": "5412", "Element": "Yield",
            "Item Code": "15", "Item": "Wheat",
            "Year Code": "2022", "Year": "2022",
            "Unit": "kg/ha", "Value": "2045.3", "Flag": "A",
            "Flag Description": "Official value", "Note": "",
        },
        {
            "Domain Code": "QCL", "Domain": "Crops and livestock products",
            "Area Code": "2", "Area": "Afghanistan",
            "Element Code": "5510", "Element": "Production",
            "Item Code": "15", "Item": "Wheat",
            "Year Code": "2022", "Year": "2022",
            "Unit": "t", "Value": "3802895", "Flag": "A",
            "Flag Description": "Official value", "Note": "",
        },
    ],
}

# Confirmed live: an `element` filter on QCL returns zero rows even though
# matching data exists without it.
QCL_DATA_WITH_ELEMENT_FILTER_BUG = {
    "metadata": {"processing_time": 99, "datasource": "PRODUCTION", "output_type": "OBJECTS"},
    "data": [],
}

COGNITO_LOGIN_SUCCESS = {
    "AuthenticationResult": {
        "AccessToken": "fake-access-token",
        "RefreshToken": "fake-refresh-token",
        "IdToken": "fake-id-token",
        "TokenType": "Bearer",
        "ExpiresIn": 3600,
    },
    "ChallengeParameters": {},
}

COGNITO_LOGIN_FAILURE = {
    "__type": "NotAuthorizedException",
    "message": "Incorrect username or password.",
}
