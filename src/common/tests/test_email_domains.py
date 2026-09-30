"""Tests for apex_email_domain / org_email_domain."""

from django.test import override_settings

from common.utils import apex_email_domain, org_email_domain


@override_settings(DEFAULT_FROM_EMAIL="Let's Revel <revel@Example.ORG>", ORG_EMAIL_DOMAIN="")
def test_apex_domain_from_display_address() -> None:
    assert apex_email_domain() == "example.org"


@override_settings(DEFAULT_FROM_EMAIL="bare@letsrevel.io", ORG_EMAIL_DOMAIN="")
def test_org_domain_falls_back_to_apex() -> None:
    assert apex_email_domain() == "letsrevel.io"
    assert org_email_domain() == "letsrevel.io"


@override_settings(DEFAULT_FROM_EMAIL="Revel <revel@letsrevel.io>", ORG_EMAIL_DOMAIN="org.letsrevel.io")
def test_org_domain_override() -> None:
    assert org_email_domain() == "org.letsrevel.io"
    assert apex_email_domain() == "letsrevel.io"
