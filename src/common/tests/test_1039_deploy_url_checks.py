"""Production deploy-URL system checks (#1039)."""

import typing as t

import pytest
from django.core.checks import ERROR, WARNING, registry

from common import checks

PROD = {
    "DEBUG": False,
    "BASE_URL": "https://api.letsrevel.io",
    "FRONTEND_BASE_URL": "https://letsrevel.io",
    "DEFAULT_FROM_EMAIL": "Let's Revel <revel@letsrevel.io>",
}


def _ids(settings: t.Any, **overrides: t.Any) -> list[str]:
    for key, value in {**PROD, **overrides}.items():
        setattr(settings, key, value)
    return [m.id for m in checks.check_deploy_urls(app_configs=None) if m.id]


def test_correct_production_config_is_silent(settings: t.Any) -> None:
    assert _ids(settings) == []


def test_self_hosted_config_is_silent(settings: t.Any) -> None:
    assert (
        _ids(
            settings,
            BASE_URL="https://api.example.org",
            FRONTEND_BASE_URL="https://events.example.org",
            DEFAULT_FROM_EMAIL="Events <events@example.org>",
        )
        == []
    )


@pytest.mark.parametrize("base_url", ["http://localhost:8000", "https://127.0.0.1", "http://[::1]:8000", ""])
def test_local_or_empty_base_url_is_an_error(settings: t.Any, base_url: str) -> None:
    for key, value in {**PROD, "BASE_URL": base_url}.items():
        setattr(settings, key, value)
    messages = checks.check_deploy_urls(app_configs=None)
    assert [m.id for m in messages] == ["common.E001"]  # W002/W004 stay quiet behind the error
    errors = messages
    assert errors[0].level == ERROR
    assert "BASE_URL" in errors[0].msg
    assert "HTTPS" in (errors[0].hint or "")


def test_schemeless_base_url_error_names_the_missing_scheme(settings: t.Any) -> None:
    for key, value in {**PROD, "BASE_URL": "api.example.org"}.items():
        setattr(settings, key, value)
    messages = checks.check_deploy_urls(app_configs=None)
    assert [m.id for m in messages] == ["common.E001"]
    assert "scheme" in messages[0].msg
    assert "not a public address" not in messages[0].msg


def test_http_base_url_is_a_warning(settings: t.Any) -> None:
    ids = _ids(settings, BASE_URL="http://api.letsrevel.io")
    assert ids == ["common.W002"]
    assert checks.check_deploy_urls(app_configs=None)[0].level == WARNING


@pytest.mark.parametrize("frontend", ["http://localhost:5173", "http://127.0.0.1:5173"])
def test_local_frontend_base_url_is_a_warning(settings: t.Any, frontend: str) -> None:
    assert _ids(settings, FRONTEND_BASE_URL=frontend) == ["common.W003"]
    hint = checks.check_deploy_urls(app_configs=None)[0].hint or ""
    assert "SiteSettings" in hint


def test_letsrevel_sender_on_foreign_host_is_a_warning(settings: t.Any) -> None:
    ids = _ids(settings, BASE_URL="https://api.example.org", FRONTEND_BASE_URL="https://example.org")
    assert ids == ["common.W004"]


def test_lookalike_host_does_not_count_as_letsrevel(settings: t.Any) -> None:
    assert _ids(settings, BASE_URL="https://api.notletsrevel.io") == ["common.W004"]


def test_demo_host_on_letsrevel_is_silent(settings: t.Any) -> None:
    assert _ids(settings, BASE_URL="https://demo-api.letsrevel.io") == []


def test_debug_silences_every_check(settings: t.Any) -> None:
    assert (
        _ids(
            settings,
            DEBUG=True,
            BASE_URL="http://localhost:8000",
            FRONTEND_BASE_URL="http://localhost:5173",
        )
        == []
    )


def test_check_is_registered_with_djangos_registry() -> None:
    assert checks.check_deploy_urls in registry.registry.registered_checks
