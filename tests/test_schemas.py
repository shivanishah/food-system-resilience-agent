from src.validation.schemas import validate_data_rows
from tests.fixtures.faostat_samples import QCL_DATA_NO_ELEMENT_FILTER


def test_valid_rows_pass_validation():
    result = validate_data_rows(QCL_DATA_NO_ELEMENT_FILTER["data"])
    assert result.n_valid == 3
    assert result.n_errors == 0
    row = result.valid[0]
    assert row.area == "Afghanistan"
    assert row.item == "Wheat"
    assert row.value == 1859339.0


def test_empty_string_value_becomes_none_not_zero():
    rows = [dict(QCL_DATA_NO_ELEMENT_FILTER["data"][0])]
    rows[0]["Value"] = ""
    result = validate_data_rows(rows)
    assert result.n_valid == 1
    assert result.valid[0].value is None


def test_zero_value_stays_zero():
    rows = [dict(QCL_DATA_NO_ELEMENT_FILTER["data"][0])]
    rows[0]["Value"] = "0"
    result = validate_data_rows(rows)
    assert result.valid[0].value == 0.0


def test_invalid_row_is_reported_not_dropped():
    rows = [{"Domain Code": "QCL"}]  # missing every other required field
    result = validate_data_rows(rows)
    assert result.n_valid == 0
    assert result.n_errors == 1
    assert result.errors[0]["row"] == rows[0]
    assert "error" in result.errors[0]


def test_mixed_valid_and_invalid_rows_both_reported():
    rows = [QCL_DATA_NO_ELEMENT_FILTER["data"][0], {"bad": "row"}]
    result = validate_data_rows(rows)
    assert result.n_valid == 1
    assert result.n_errors == 1


def test_bilateral_trade_row_has_no_area_field_but_still_validates():
    # Confirmed live shape for TM/RFM: reporter/partner countries instead
    # of a single Area.
    row = {
        "Domain Code": "TM", "Domain": "Detailed trade matrix",
        "Reporter Country Code": "7", "Reporter Countries": "Angola",
        "Partner Country Code": "21", "Partner Countries": "Brazil",
        "Element Code": "5610", "Element": "Import quantity",
        "Item Code": "231", "Item": "Almonds, shelled",
        "Year Code": "2022", "Year": "2022",
        "Unit": "t", "Value": "0.01", "Flag": "A", "Flag Description": "Official value",
    }
    result = validate_data_rows([row])
    assert result.n_valid == 1
    obs = result.valid[0]
    assert obs.area is None
    assert obs.reporter_area == "Angola"
    assert obs.partner_area == "Brazil"
