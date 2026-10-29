from common.country import normalize_country_code


def test_normalize_country_code() -> None:
    assert normalize_country_code("el") == "GR"
    assert normalize_country_code(" at ") == "AT"
    assert normalize_country_code(None) == ""
