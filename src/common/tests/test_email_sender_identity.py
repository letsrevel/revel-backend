import typing as t

from django.conf import settings


def test_default_from_email_is_lets_revel() -> None:
    assert settings.DEFAULT_FROM_EMAIL.startswith("Let's Revel <")
    assert "revel@letsrevel.io" in settings.DEFAULT_FROM_EMAIL
