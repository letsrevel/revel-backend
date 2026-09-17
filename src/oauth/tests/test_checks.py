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
