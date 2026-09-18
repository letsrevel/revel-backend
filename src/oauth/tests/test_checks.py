"""``OAUTH_ISSUER`` must be configured whenever the provider is switched on (R-43, spec §5)."""

import typing as t

from django.core.checks import Error

from oauth.checks import ISSUER_CHECK_ID, check_oauth_issuer_configured


def test_blank_issuer_is_an_error_while_the_provider_is_enabled(settings: t.Any) -> None:
    settings.OAUTH_ISSUER = ""
    errors = check_oauth_issuer_configured(app_configs=None)
    assert [e.id for e in errors] == [ISSUER_CHECK_ID]
    assert isinstance(errors[0], Error)


def test_configured_issuer_passes(settings: t.Any) -> None:
    settings.OAUTH_ISSUER = "https://api.example.test"
    assert check_oauth_issuer_configured(app_configs=None) == []


def test_blank_issuer_is_fine_while_the_provider_is_disabled(settings: t.Any) -> None:
    settings.OIDC_SIGNING_KEY_PATH = ""
    settings.OAUTH_ISSUER = ""
    assert check_oauth_issuer_configured(app_configs=None) == []


def test_check_is_registered_with_djangos_registry() -> None:
    from django.core.checks import registry

    assert check_oauth_issuer_configured in registry.registry.registered_checks


# --- oauth.E002: a configured key the process cannot read -------------------------------------


def test_unreadable_signing_key_is_an_error(settings: t.Any) -> None:
    from oauth.checks import SIGNING_KEY_CHECK_ID, check_oidc_signing_key_readable

    settings.OIDC_SIGNING_KEY_ERRORS = ["/app/certs/oidc.pem: Permission denied"]
    errors = check_oidc_signing_key_readable(app_configs=None)
    assert [e.id for e in errors] == [SIGNING_KEY_CHECK_ID]
    assert "Permission denied" in errors[0].msg
    assert "997" in (errors[0].hint or "")


def test_readable_signing_key_passes(settings: t.Any) -> None:
    from oauth.checks import check_oidc_signing_key_readable

    settings.OIDC_SIGNING_KEY_ERRORS = []
    assert check_oidc_signing_key_readable(app_configs=None) == []


def test_unreadable_signing_key_switches_the_provider_off(settings: t.Any) -> None:
    """The path is set, but the flag is credential *presence*: an unreadable file is absent."""
    from oauth.utils import oauth_provider_enabled

    assert oauth_provider_enabled()
    settings.OIDC_SIGNING_KEY_ERRORS = ["/app/certs/oidc.pem: Permission denied"]
    assert not oauth_provider_enabled()


def testread_pem_records_instead_of_raising(tmp_path: t.Any) -> None:
    """A missing or unreadable PEM must not crash settings import — every process shares it."""
    from revel.settings.oauth import read_pem

    errors: list[str] = []
    assert read_pem("", errors) == ""
    assert read_pem(str(tmp_path / "missing.pem"), errors) == ""
    assert len(errors) == 1 and "missing.pem" in errors[0]

    readable = tmp_path / "ok.pem"
    readable.write_text("PEM")
    assert read_pem(str(readable), errors) == "PEM"
    assert len(errors) == 1


def test_signing_key_check_is_registered_with_djangos_registry() -> None:
    from django.core.checks import registry

    from oauth.checks import check_oidc_signing_key_readable

    assert check_oidc_signing_key_readable in registry.registry.registered_checks
