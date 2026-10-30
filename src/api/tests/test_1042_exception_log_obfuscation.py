"""The unhandled-exception log masks credential query params, not only ``token`` (#1042)."""

from api.exception_handlers import obfuscate


def test_credential_query_params_are_masked() -> None:
    data = {"token": "t", "sig": "s", "code": "c", "ot": "o", "et": "e", "page": "2", "state": "st"}

    masked = obfuscate(data)

    assert {k: masked[k] for k in ("token", "sig", "code", "ot", "et")} == dict.fromkeys(
        ("token", "sig", "code", "ot", "et"), "********"
    )
    assert masked["page"] == "2"
    assert masked["state"] == "st"  # not a credential on its own
    assert data["sig"] == "s"  # input untouched


def test_jwt_body_fields_are_masked() -> None:
    """Refresh/access token fields in a logged request body are masked."""
    data = {"refresh": "r", "refresh_token": "rt", "access": "a", "access_token": "at", "page": "2"}

    masked = obfuscate(data)

    assert all(masked[k] == "********" for k in ("refresh", "refresh_token", "access", "access_token"))
    assert masked["page"] == "2"
