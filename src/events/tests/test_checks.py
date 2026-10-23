"""System check: Cloudflare provisioning needs a CNAME target to hand to organizers (#1151)."""

import typing as t

import pytest
from django.core.checks import WARNING, registry

from events import checks


def _run(settings: t.Any, token: str, zone: str, target: str) -> list[t.Any]:
    settings.CLOUDFLARE_API_TOKEN = token
    settings.CLOUDFLARE_ZONE_ID = zone
    settings.CUSTOM_DOMAIN_CNAME_TARGET = target
    return checks.check_custom_domain_cname_target(app_configs=None)


def test_warns_when_cloudflare_is_configured_without_a_cname_target(settings: t.Any) -> None:
    messages = _run(settings, "tok", "zone", "")

    assert [(m.id, m.level) for m in messages] == [(checks.CNAME_TARGET_MISSING_CHECK_ID, WARNING)]
    assert "CUSTOM_DOMAIN_CNAME_TARGET" in messages[0].msg


@pytest.mark.parametrize(
    ("token", "zone", "target"),
    [
        ("tok", "zone", "customers.letsrevel.io"),  # fully configured
        ("", "", ""),  # self-hosted: no Cloudflare at all
        ("tok", "", ""),  # provisioning is off unless both are set
        ("", "zone", ""),
    ],
)
def test_silent_otherwise(settings: t.Any, token: str, zone: str, target: str) -> None:
    assert _run(settings, token, zone, target) == []


def test_check_is_registered_with_django(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN, settings.CLOUDFLARE_ZONE_ID, settings.CUSTOM_DOMAIN_CNAME_TARGET = "t", "z", ""

    ids = [m.id for m in registry.run_checks()]

    assert checks.CNAME_TARGET_MISSING_CHECK_ID in ids
