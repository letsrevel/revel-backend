"""System check for CUSTOM_DOMAIN_URL_TEMPLATE (#1153)."""

import typing as t

import pytest
from django.core.checks import ERROR, WARNING, registry

from common import checks


def _messages(settings: t.Any, template: str, *, debug: bool = False) -> list[t.Any]:
    settings.CUSTOM_DOMAIN_URL_TEMPLATE = template
    settings.DEBUG = debug
    return checks.check_custom_domain_url_template(app_configs=None)


@pytest.mark.parametrize("debug", [False, True])
def test_default_template_is_silent(settings: t.Any, debug: bool) -> None:
    assert _messages(settings, "https://{hostname}", debug=debug) == []


def test_http_with_port_is_silent_in_debug(settings: t.Any) -> None:
    assert _messages(settings, "http://{hostname}:5173", debug=True) == []


@pytest.mark.parametrize(
    "template",
    [
        "https://example.com",  # no placeholder: every tenant collapses onto one origin
        "https://{hostname}/{",  # stray brace
        "https://{hostname}/{}",  # positional placeholder
        "https://{hostname}:{port}",  # unknown placeholder
        "{hostname}",  # no scheme
        "ftp://{hostname}",
        "//{hostname}",
        "",
    ],
)
@pytest.mark.parametrize("debug", [False, True])
def test_invalid_template_is_an_error(settings: t.Any, template: str, debug: bool) -> None:
    messages = _messages(settings, template, debug=debug)

    assert [m.id for m in messages] == ["common.E002"]
    assert messages[0].level == ERROR
    assert "CUSTOM_DOMAIN_URL_TEMPLATE" in messages[0].msg


def test_http_template_outside_debug_is_a_warning(settings: t.Any) -> None:
    messages = _messages(settings, "http://{hostname}")

    assert [m.id for m in messages] == ["common.W005"]
    assert messages[0].level == WARNING


def test_check_is_registered() -> None:
    assert checks.check_custom_domain_url_template in registry.registry.get_checks(include_deployment_checks=False)
